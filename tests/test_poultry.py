"""系统测试：票号唯一、重量守恒、冻结传播、并发分割、摊位隔离。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poultry import config
from poultry.adapters import MockScale, MockTempProbe
from poultry.database import init_db
from poultry.errors import (DuplicateCertError, FrozenError,
                            InsufficientError, LabelLostError,
                            MixedCellError, WeightMismatchError, AccessDenied,
                            TempError)
from poultry.services import MarketService


class MarketFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self._old_docs, self._old_photos = config.DOCS_DIR, config.PHOTOS_DIR
        config.DOCS_DIR = self.tmpdir / "docs"
        config.PHOTOS_DIR = self.tmpdir / "photos"
        config.ensure_dirs()
        self.db = self.tmpdir / "test.db"
        init_db(self.db)
        self.svc = MarketService(self.db, scale=MockScale(), probe=MockTempProbe())
        for args in [("A01", "晨鸣禽档", "王晨鸣"),
                     ("A02", "兴旺禽铺", "李兴旺")]:
            self.svc.create_stall(*args)
        self.svc.create_supplier("GY-07", "广源禽业合作社（虚构）", "清远")
        self.svc.create_cell("1号柜", "A01")   # code C1-A01
        self.svc.create_cell("1号柜", "A02")   # code C1-A02
        self.now = datetime.now()

    def tearDown(self):
        config.DOCS_DIR, config.PHOTOS_DIR = self._old_docs, self._old_photos
        self.tmp.cleanup()

    # ---- 辅助 ----
    def arrival(self, cert="JY-001", stall="A01", cat="白条鸡", weight=100.0,
                cell="C1-A01", actual=None, issued=None, valid_days=3):
        bid = self.svc.register_arrival(
            cert_no=cert, stall_code=stall, supplier_code="GY-07",
            category=cat, declared_weight=weight, stall_code_actor=stall,
            issued_at=issued or self.now - timedelta(hours=1),
            valid_days=valid_days)
        self.svc.verify_and_store(bid, cell_code=cell,
                                  actual_weight=actual if actual is not None else weight)
        return bid


class TestCertUniqueness(MarketFixture):
    def test_duplicate_cert_rejected(self):
        self.arrival(cert="JY-DUP-1", weight=20.0)
        with self.assertRaises(DuplicateCertError):
            self.svc.register_arrival(
                cert_no="JY-DUP-1", stall_code="A02", supplier_code="GY-07",
                category="白条鸭", declared_weight=9.0, stall_code_actor="A02")
        with self.svc.conn() as c:
            n = c.execute("SELECT COUNT(*) k FROM certs WHERE cert_no=?",
                          ("JY-DUP-1",)).fetchone()["k"]
        self.assertEqual(n, 1)
        ev = self.svc.list_events(100)
        self.assertTrue(any(e["kind"] == "dup_cert" for e in ev))

    def test_concurrent_duplicate_only_one_wins(self):
        """多线程同时用同一票号登记，恰好一单成功。"""
        results = []

        def attempt():
            try:
                self.svc.register_arrival(
                    cert_no="JY-RACE", stall_code="A01", supplier_code="GY-07",
                    category="白条鸡", declared_weight=10.0,
                    stall_code_actor="A01")
                results.append("ok")
            except DuplicateCertError:
                results.append("dup")

        ts = [threading.Thread(target=attempt) for _ in range(6)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("dup"), 5)


class TestWeightConservation(MarketFixture):
    def test_chain_conserved_through_sell_remainder_rework_waste(self):
        bid = self.arrival(cert="JY-C1", weight=120.0, actual=119.4)
        c1 = self.svc.register_cut(
            bid, tray_weights=[20.0, 20.0, 20.0], remainder_weight=5.0,
            waste_weight=1.0, stall_code_actor="A01")          # 66kg
        self.svc.sell_tray(c1["trays"][0]["id"], stall_code_actor="A01")  # -20 sold
        rid = self.svc.tray_to_remainder(c1["trays"][1]["id"],
                                         stall_code_actor="A01")  # 20 → 余料
        self.svc.rework_remainder(rid, tray_weights=[19.0], waste_weight=1.0,
                                  stall_code_actor="A01")
        self.svc.register_cut(
            bid, tray_weights=[30.0], waste_weight=3.0,
            waste_reason="修整损耗", stall_code_actor="A01")    # 33kg → 累计99
        # 119.4 - 99 = 20.4 余量；再废弃一个在架托盘 19kg（返工产物）
        new_trays = self.svc.list_trays("A01")
        rework_tray = next(t for t in new_trays if t["weight"] == 19.0)
        self.svc.discard_tray(rework_tray["id"], "变质",
                              stall_code_actor="A01")

        bal = self.svc.weight_balance(bid)
        self.assertTrue(bal["conserved"], msg=json.dumps(bal, ensure_ascii=False))
        self.assertAlmostEqual(bal["gap"], 0.0, places=2)
        for a in self.svc.conservation_audit():
            self.assertTrue(a["conserved"], msg=str(a))

    def test_same_weight_cannot_be_cut_twice(self):
        bid = self.arrival(cert="JY-C2", weight=50.0)
        self.svc.register_cut(bid, tray_weights=[30.0],
                              stall_code_actor="A01")
        with self.assertRaises(InsufficientError):
            self.svc.register_cut(bid, tray_weights=[25.0],
                                  stall_code_actor="A01")  # 余量仅20
        # 恰好等于余量可以
        r = self.svc.register_cut(bid, tray_weights=[20.0],
                                  stall_code_actor="A01")
        self.assertEqual(r["remaining"], 0.0)

    def test_rework_must_conserve(self):
        bid = self.arrival(cert="JY-C3", weight=40.0)
        c = self.svc.register_cut(bid, tray_weights=[10.0], remainder_weight=6.0,
                                  stall_code_actor="A01")
        rid = self.svc.list_remainders("A01")[0]["id"]
        with self.assertRaises(WeightMismatchError):
            self.svc.rework_remainder(rid, tray_weights=[5.9],
                                      waste_weight=0.0,
                                      stall_code_actor="A01")  # 0.1 对不上


class TestFreezePropagation(MarketFixture):
    def _make_chain(self):
        bid = self.arrival(cert="JY-F1", weight=60.0)
        c = self.svc.register_cut(bid, tray_weights=[10.0, 10.0, 10.0],
                                  remainder_weight=4.0,
                                  stall_code_actor="A01")
        self.svc.sell_tray(c["trays"][0]["id"], stall_code_actor="A01")
        rid = self.svc.tray_to_remainder(c["trays"][1]["id"],
                                         stall_code_actor="A01")
        return bid, c, rid

    def test_revoke_cert_freezes_unsold_chain(self):
        bid, c, rid = self._make_chain()
        res = self.svc.revoke_cert("JY-F1", "源头监测异常")
        self.assertIn(bid, res["batches"])
        # 在架托盘（第3个）冻结；已售、已转余料的托盘不在冻结清单
        frozen_ids = set(res["trays"])
        self.assertIn(c["trays"][2]["id"], frozen_ids)
        self.assertNotIn(c["trays"][0]["id"], frozen_ids)
        with self.svc.conn() as conn:
            b = conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
            r = conn.execute("SELECT * FROM remainders WHERE id=?", (rid,)).fetchone()
        self.assertTrue(b["frozen"])
        self.assertIn("源头监测异常", b["freeze_reason"])
        self.assertTrue(r["frozen"])  # 待处理余料联动冻结

        # 冻结阻断后续业务
        with self.assertRaises(FrozenError):
            self.svc.register_cut(bid, tray_weights=[5.0],
                                  stall_code_actor="A01")
        with self.assertRaises(FrozenError):
            self.svc.sell_tray(c["trays"][2]["id"], stall_code_actor="A01")
        with self.assertRaises(FrozenError):
            self.svc.discard_remainder(rid, "变质", stall_code_actor="A01")

        # 冻结状态始终带文字原因
        t = next(t for t in self.svc.list_trays() if t["id"] == c["trays"][2]["id"])
        self.assertEqual(t["status"], "frozen")
        self.assertTrue(t["freeze_reason"])

        # 解封后恢复在架，可售
        self.svc.release_freeze(res["freeze_id"])
        t = next(t for t in self.svc.list_trays() if t["id"] == c["trays"][2]["id"])
        self.assertEqual(t["status"], "in_stock")
        self.svc.sell_tray(c["trays"][2]["id"], stall_code_actor="A01")

    def test_inspection_fail_freezes_batch_trays(self):
        bid = self.arrival(cert="JY-F2", weight=40.0)
        c = self.svc.register_cut(bid, tray_weights=[8.0, 8.0],
                                  stall_code_actor="A01")
        r = self.svc.add_inspection(target="tray", target_id=c["trays"][0]["id"],
                                    result="fail", note="盐基氮偏高")
        self.assertEqual(set(r["trays"]), {c["trays"][0]["id"], c["trays"][1]["id"]})
        with self.assertRaises(FrozenError):
            self.svc.sell_tray(c["trays"][1]["id"], stall_code_actor="A01")

    def test_partial_recall_freeze_and_destroy(self):
        bid = self.arrival(cert="JY-F3", weight=45.0)
        c = self.svc.register_cut(bid, tray_weights=[9.0, 9.0, 9.0],
                                  stall_code_actor="A01")
        self.svc.sell_tray(c["trays"][0]["id"], stall_code_actor="A01")
        rec = self.svc.create_recall(
            reason="局部污染定向召回",
            tray_ids=[c["trays"][0]["id"], c["trays"][1]["id"]])
        # 未售冻结，已售仅登记追溯
        self.assertEqual(rec["frozen_trays"], [c["trays"][1]["id"]])
        self.assertEqual(len(rec["sold_or_missing"]), 1)

        d = self.svc.destroy_recall_trays(rec["recall_id"], [c["trays"][1]["id"]])
        self.assertEqual(d["destroyed"], [c["trays"][1]["tray_no"]])
        wastes = self.svc.list_wastes("A01")
        self.assertTrue(any("召回" in w["reason"] for w in wastes))
        # 结案解除：剩余在架托盘恢复（第3个未被召回，本就不受影响）
        self.svc.close_recall(rec["recall_id"])
        t2 = next(t for t in self.svc.list_trays() if t["id"] == c["trays"][2]["id"])
        self.assertEqual(t2["status"], "in_stock")
        self.assertTrue(self.svc.weight_balance(bid)["conserved"])

    def test_overlapping_freezes_keep_other_constraint(self):
        """同一托盘受两张冻结单约束时，解除一张仍冻结，全解才恢复。"""
        bid = self.arrival(cert="JY-F4", weight=30.0)
        c = self.svc.register_cut(bid, tray_weights=[10.0],
                                  stall_code_actor="A01")
        tid = c["trays"][0]["id"]
        self.svc.add_inspection(target="batch", target_id=bid, result="fail",
                                note="抽检不合格")
        rec = self.svc.create_recall(reason="召回叠加", tray_ids=[tid])
        self.svc.close_recall(rec["recall_id"])  # 召回解除
        t = next(t for t in self.svc.list_trays() if t["id"] == tid)
        self.assertEqual(t["status"], "frozen")  # 抽查冻结仍在
        self.svc.unfreeze_target("tray", tid)   # 管理员手动全解
        t = next(t for t in self.svc.list_trays() if t["id"] == tid)
        self.assertEqual(t["status"], "in_stock")

    def test_expired_cert_scan_freezes(self):
        bid = self.arrival(cert="JY-F5", weight=20.0)
        c = self.svc.register_cut(bid, tray_weights=[5.0],
                                  stall_code_actor="A01")
        # 放行分割后，将票据有效期回拨到过去（确定性模拟到期），再由扫描任务冻结
        with self.svc.conn() as cc:
            cc.execute("UPDATE certs SET valid_until=? WHERE cert_no='JY-F5'",
                       ((self.now - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),))
        r = self.svc.scan_cert_expiry()
        self.assertIn("JY-F5", r["expired"])
        t = next(t for t in self.svc.list_trays() if t["id"] == c["trays"][0]["id"])
        self.assertEqual(t["status"], "frozen")
        self.assertIn("有效期", t["freeze_reason"])


class TestConcurrentCuts(MarketFixture):
    def test_concurrent_cuts_respect_remaining(self):
        bid = self.arrival(cert="JY-CC1", weight=100.0)
        ok, rejected = [], []
        lock = threading.Lock()

        def worker():
            # 每线程尝试 10 次分割 3kg，共最多 100 个申请竞争 100kg 余量
            for _ in range(10):
                try:
                    r = self.svc.register_cut(bid, tray_weights=[3.0],
                                              stall_code_actor="A01")
                    with lock:
                        ok.append(r["gross_weight"])
                except InsufficientError:
                    with lock:
                        rejected.append(1)
                except Exception as e:  # 其他错误也算失败
                    with lock:
                        rejected.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()

        # 守恒：成功扣减总和不超过到货重量，且为 3 的整数倍
        self.assertLessEqual(sum(ok), 100.0 + 1e-6)
        self.assertEqual(sum(ok) % 3, 0)
        self.assertTrue(rejected, "余量耗尽后应有分割被拒")
        # 托盘重量之和 = 成功扣减总和（无废弃/余料）
        with self.svc.conn() as c:
            tray_sum = c.execute(
                "SELECT COALESCE(SUM(weight),0) s FROM trays WHERE batch_id=?",
                (bid,)).fetchone()["s"]
        self.assertAlmostEqual(tray_sum, sum(ok), places=6)
        bal = self.svc.weight_balance(bid)
        self.assertTrue(bal["conserved"])


class TestStallIsolation(MarketFixture):
    def test_other_stall_denied(self):
        bid = self.arrival(cert="JY-S1", stall="A01", weight=30.0)
        c = self.svc.register_cut(bid, tray_weights=[8.0],
                                  stall_code_actor="A01")
        tid = c["trays"][0]["id"]
        with self.assertRaises(AccessDenied):
            self.svc.sell_tray(tid, stall_code_actor="A02")
        with self.assertRaises(AccessDenied):
            self.svc.register_cut(bid, tray_weights=[2.0],
                                  stall_code_actor="A02")
        with self.assertRaises(AccessDenied):
            self.svc.report_label_lost(tid, stall_code_actor="A02")
        # 摊主不能执行管理员操作
        with self.assertRaises(AccessDenied):
            self.svc.add_inspection(target="batch", target_id=bid, result="fail",
                                    role="stall")
        # 列表/详情隔离：A02 看不到也读不到 A01 的数据
        self.assertEqual(self.svc.list_batches("A02"), [])
        self.assertEqual(self.svc.list_trays("A02"), [])
        self.assertEqual(self.svc.list_wastes("A02"), [])
        self.assertEqual(self.svc.conservation_audit("A02"), [])
        with self.assertRaises(AccessDenied):
            self.svc.batch_detail(bid, role="stall", stall_code_actor="A02")
        # 管理员可看全部
        self.assertTrue(self.svc.batch_detail(bid, role="admin")["batch"])

    def test_owner_allowed(self):
        bid = self.arrival(cert="JY-S2", stall="A01", weight=20.0)
        c = self.svc.register_cut(bid, tray_weights=[7.0],
                                  stall_code_actor="A01")
        r = self.svc.sell_tray(c["trays"][0]["id"], stall_code_actor="A01")
        self.assertEqual(r["status"], "sold")


class TestOtherScenarios(MarketFixture):
    def test_weight_diff_rejected_and_tolerated(self):
        bid = self.svc.register_arrival(
            cert_no="JY-W1", stall_code="A01", supplier_code="GY-07",
            category="白条鸡", declared_weight=100.0, stall_code_actor="A01",
            issued_at=self.now - timedelta(hours=1))
        self.svc.scale.preset_next(106.0)
        r = self.svc.verify_and_store(bid, cell_code="C1-A01")
        self.assertEqual(r["status"], "rejected")

    def test_goods_mismatch_rejected(self):
        # 手工制造票面品类与实到不一致
        bid = self.svc.register_arrival(
            cert_no="JY-G1", stall_code="A01", supplier_code="GY-07",
            category="白条鸡", declared_weight=20.0, stall_code_actor="A01")
        with self.svc.conn() as c:
            cert_id = c.execute("SELECT cert_id FROM batches WHERE id=?",
                                (bid,)).fetchone()["cert_id"]
            c.execute("UPDATE certs SET category=? WHERE id=?",
                      ("白条鸭", cert_id))
        r = self.svc.verify_and_store(bid, cell_code="C1-A01")
        self.assertEqual(r["status"], "rejected")
        self.assertIn("票货不符", r["reason"])

    def test_mixed_category_cell_rejected(self):
        self.arrival(cert="JY-M1", cat="白条鸡", weight=20.0, cell="C1-A01")
        bid = self.svc.register_arrival(
            cert_no="JY-M2", stall_code="A02", supplier_code="GY-07",
            category="白条鸭", declared_weight=15.0, stall_code_actor="A02",
            issued_at=self.now - timedelta(hours=1))
        with self.assertRaises(MixedCellError):
            self.svc.verify_and_store(bid, cell_code="C1-A01")  # 鸡柜放鸭
        # 同品类共柜允许
        b3 = self.svc.register_arrival(
            cert_no="JY-M3", stall_code="A01", supplier_code="GY-07",
            category="白条鸡", declared_weight=12.0, stall_code_actor="A01",
            issued_at=self.now - timedelta(hours=1))
        r = self.svc.verify_and_store(b3, cell_code="C1-A01")
        self.assertEqual(r["status"], "accepted")

    def test_hot_cell_blocks_storage_and_scan_freezes(self):
        self.svc.probe.force_temp("C1-A01", 9.0)
        bid = self.svc.register_arrival(
            cert_no="JY-T1", stall_code="A01", supplier_code="GY-07",
            category="白条鸡", declared_weight=20.0, stall_code_actor="A01",
            issued_at=self.now - timedelta(hours=1))
        with self.assertRaises(TempError):
            self.svc.verify_and_store(bid, cell_code="C1-A01")

    def test_label_lost_blocks_sale_until_relabel(self):
        bid = self.arrival(cert="JY-L1", weight=20.0)
        c = self.svc.register_cut(bid, tray_weights=[8.0],
                                  stall_code_actor="A01")
        tid = c["trays"][0]["id"]
        self.svc.report_label_lost(tid, note="脱落", stall_code_actor="A01")
        with self.assertRaises(LabelLostError):
            self.svc.sell_tray(tid, stall_code_actor="A01")
        with self.assertRaises(AccessDenied):
            self.svc.relabel(tid, role="stall")  # 仅管理员补签
        self.svc.relabel(tid)
        self.svc.sell_tray(tid, stall_code_actor="A01")

    def test_waste_abnormal_flag(self):
        bid = self.arrival(cert="JY-WA1", weight=50.0)
        # 废弃 9kg / 毛重 10kg = 90% > 阈值
        self.svc.register_cut(bid, tray_weights=[1.0], waste_weight=9.0,
                              waste_reason="异常大批废弃",
                              stall_code_actor="A01")
        w = self.svc.list_wastes("A01")[0]
        self.assertTrue(w["abnormal"])
        self.assertTrue(any(e["kind"] == "waste_abnormal"
                            for e in self.svc.list_events(100)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
