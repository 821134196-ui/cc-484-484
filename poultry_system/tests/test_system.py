"""五项核心测试 + 八类异常分支。"""
from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from poultry_system import db
from poultry_system.service import MarketService, BusinessError, AuthzError
from poultry_system.tasks import ScanScheduler
from . import fixtures as fx


class TicketUniquenessTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.svc = fx.make_svc()

    def test_duplicate_ticket_rejected_and_logged(self):
        fx.arrived(self.svc, ticket="Q-DUP")
        with self.assertRaises(BusinessError):
            fx.arrived(self.svc, ticket="Q-DUP")
        n = self.conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE kind='duplicate_ticket'").fetchone()[0]
        self.assertEqual(n, 1)
        # 只有一张票、一个批次
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM quarantine_tickets WHERE ticket_no='Q-DUP'").fetchone()[0], 1)

    def test_unique_constraint_is_at_db_level(self):
        import sqlite3
        fx.arrived(self.svc, ticket="Q-UQ")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO quarantine_tickets(ticket_no,supplier_id,category,gross_weight_g,"
                "issued_at,valid_from,valid_until) VALUES('Q-UQ',1,'x',1,'t','a','b')")

    def test_expired_ticket_rejected(self):
        now = datetime.now()
        vf = (now - timedelta(days=5)).isoformat(timespec="seconds")
        uf = (now - timedelta(days=1)).isoformat(timespec="seconds")
        r = fx.arrived(self.svc, ticket="Q-OLD", valid=(vf, uf))
        self.assertEqual(r["status"], "rejected")

    def test_ticket_category_mismatch_rejected(self):
        vf, uf = fx.window()
        r = self.svc.arrive("zhang", stall_code="A-101", supplier_code="S-01",
                            ticket_no="Q-MM", category="白条鸡", ticket_category="白条鸭",
                            ticket_weight_g=10000, declared_weight_g=10000,
                            valid_from=vf, valid_until=uf)
        self.assertEqual(r["status"], "rejected")


class WeightConservationTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.svc = fx.make_svc()

    def test_full_chain_conserved_with_remainder_recut(self):
        b = fx.released(self.svc, ticket="Q-W", weight=20000)
        c = self.svc.register_cut("zhang", batch_no=b, weight_g=12000,
            trays=[("腿", 5000), ("胸", 4000), ("块", 2500)], loss_g=500)
        self.svc.sell_tray("zhang", c["trays"][0], 2000)
        rem = self.svc.recover_remainder("zhang", c["trays"][0])["remainder_no"]
        rc = self.svc.recut_remainder("zhang", remainder_no=rem,
                                      trays=[("再分腿", 2850)], loss_g=150)
        self.svc.sell_tray("zhang", rc["trays"][0], 2850)
        self.svc.sell_tray("zhang", c["trays"][1], 4000)
        rep = self.svc.conservation_report(b)
        self.assertTrue(rep["conserved"], rep)
        self.assertEqual(rep["chain_total_g"], 20000)

    def test_waste_buckets_balance(self):
        b = fx.released(self.svc, ticket="Q-W2", weight=5000)
        self.svc.register_cut("zhang", batch_no=b, weight_g=2000,
                              trays=[("小份", 2000)], loss_g=0)
        self.svc.log_waste("zhang", source_type="batch", ref_no=b,
                           weight_g=300, reason="零星损耗")
        rep = self.svc.conservation_report(b)
        self.assertTrue(rep["conserved"])
        self.assertEqual(rep["buckets"]["waste_batch_g"], 300)

    def test_cannot_over_cut(self):
        b = fx.released(self.svc, ticket="Q-W3", weight=1000)
        with self.assertRaises(BusinessError):
            self.svc.register_cut("zhang", batch_no=b, weight_g=1200,
                                  trays=[("x", 1200)])
        # 重量未被扣减
        self.assertEqual(self.svc.batch_remaining_g(
            self.conn.execute("SELECT id FROM batches WHERE batch_no=?", (b,)).fetchone()["id"]),
            1000)

    def test_cut_weight_must_close(self):
        b = fx.released(self.svc, ticket="Q-W4", weight=5000)
        with self.assertRaises(BusinessError):
            self.svc.register_cut("zhang", batch_no=b, weight_g=3000,
                                  trays=[("a", 1000), ("b", 1000)], loss_g=0)

    def test_sell_cannot_exceed_tray(self):
        b = fx.released(self.svc, ticket="Q-W5", weight=2000)
        c = self.svc.register_cut("zhang", batch_no=b, weight_g=2000,
                                  trays=[("份", 2000)])
        with self.assertRaises(BusinessError):
            self.svc.sell_tray("zhang", c["trays"][0], 2001)

    def test_abnormal_waste_flagged(self):
        b = fx.released(self.svc, ticket="Q-W6", weight=10000)
        out = self.svc.log_waste("zhang", source_type="batch", ref_no=b,
                                 weight_g=2000, reason="大批变质")
        self.assertTrue(out["abnormal"])
        n = self.conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE kind='abnormal_waste'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_weight_discrepancy_hold_then_admin_accept(self):
        r = fx.arrived(self.svc, ticket="Q-WD", weight=10000, declared=9000, drift=1000)
        b = r["batch_no"]
        # 有未处理差异时不能放行
        with self.assertRaises(BusinessError):
            self.svc.release_to_cabinet("zhang", b, "C-01", "1层", "A")
        self.svc.resolve_weight_discrepancy("admin", b, accept=True)
        self.svc.release_to_cabinet("zhang", b, "C-01", "1层", "A")
        self.assertEqual(r["measured_g"], 10000)


class FreezePropagationTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.svc = fx.make_svc()

    def _chain(self, ticket="Q-F"):
        b = fx.released(self.svc, ticket=ticket, weight=10000)
        c = self.svc.register_cut("zhang", batch_no=b, weight_g=10000,
            trays=[("a", 4000), ("b", 3000), ("c", 2500)], loss_g=500)
        self.svc.sell_tray("zhang", c["trays"][0], 1000)
        rem = self.svc.recover_remainder("zhang", c["trays"][0])["remainder_no"]
        return b, c, rem

    def test_inspection_fail_freezes_unsold_chain(self):
        b, c, rem = self._chain()
        self.svc.inspect("admin", target_type="batch", ref_no=b,
                         result="fail", note="药残超标")
        # 未售托盘 b、c 冻结；售出部分不产生影响
        for t in c["trays"][1:]:
            st = self.svc.freeze_state("tray", t)
            self.assertTrue(st["frozen"]); self.assertIn("药残超标", st["reason"])
        self.assertTrue(self.svc.freeze_state("remainder", rem)["frozen"])
        self.assertTrue(self.svc.freeze_state("batch", b)["frozen"])
        # 冻结托盘禁售
        with self.assertRaises(BusinessError):
            self.svc.sell_tray("zhang", c["trays"][1], 100)
        # 冻结链上的批次/托盘禁止自行废弃
        with self.assertRaises(BusinessError):
            self.svc.log_waste("zhang", source_type="batch", ref_no=b,
                               weight_g=10, reason="私自销毁")
        with self.assertRaises(BusinessError):
            self.svc.log_waste("zhang", source_type="tray", ref_no=c["trays"][1],
                               weight_g=10, reason="私自销毁")

    def test_revoke_ticket_freezes_chain(self):
        b, c, rem = self._chain("Q-RV")
        self.svc.revoke_ticket("admin", "Q-RV", "产地通报（虚构）")
        self.assertTrue(self.svc.freeze_state("batch", b)["frozen"])
        self.assertTrue(self.svc.freeze_state("tray", c["trays"][1])["frozen"])
        # 撤销的票不得放行新批次（同票已无新批，验证状态）
        self.assertEqual(self.conn.execute(
            "SELECT status FROM quarantine_tickets WHERE ticket_no='Q-RV'").fetchone()[0],
            "revoked")

    def test_partial_recall_freezes_only_listed(self):
        b, c, rem = self._chain("Q-PR")
        out = self.svc.issue_recall("admin", scope="partial", reason="局部异味",
                                   tray_nos=[c["trays"][1], c["trays"][2]])
        self.assertEqual(out["items"], 2)
        self.assertTrue(self.svc.freeze_state("tray", c["trays"][1])["frozen"])
        # 未列入的托盘不冻结（trays[0] 已转余料，不在部分召回清单 → 余料也不冻结）
        self.assertFalse(self.svc.freeze_state("remainder", rem)["frozen"])

    def test_cannot_unfreeze_while_root_cause_open(self):
        b, c, rem = self._chain("Q-BL")
        self.svc.inspect("admin", target_type="batch", ref_no=b, result="fail", note="x")
        with self.assertRaises(BusinessError):
            self.svc.unfreeze("admin", "tray", c["trays"][1], "试试")
        inc = self.conn.execute(
            "SELECT id FROM incidents WHERE kind='inspection_fail' LIMIT 1").fetchone()[0]
        self.svc.resolve_incident("admin", inc, "复检合格")
        out = self.svc.unfreeze("admin", "batch", b, "复检合格")
        self.assertFalse(self.svc.freeze_state("tray", c["trays"][1])["frozen"])
        self.assertTrue(any("tray" in x for x in out["unfrozen"]))

    def test_recall_must_be_lifted_before_unfreeze(self):
        b, c, rem = self._chain("Q-RC")
        self.svc.issue_recall("admin", scope="partial", reason="r",
                              tray_nos=[c["trays"][1]])
        with self.assertRaises(BusinessError):
            self.svc.unfreeze("admin", "tray", c["trays"][1], "解封")
        rc = self.conn.execute("SELECT recall_no FROM recalls").fetchone()[0]
        self.svc.lift_recall("admin", rc, "问题澄清")
        self.svc.unfreeze("admin", "tray", c["trays"][1], "召回撤回")
        self.assertFalse(self.svc.freeze_state("tray", c["trays"][1])["frozen"])

    def test_overtemp_scan_freezes_chain(self):
        b = fx.released(self.svc, ticket="Q-OT", loc=("C-01", "1层", "B"))
        self.svc.register_cut("zhang", batch_no=b, weight_g=10000,
                              trays=[("a", 5000), ("b", 4500)], loss_g=500)
        self.svc.record_temperature("C-01", 6.5)
        actions = ScanScheduler(self.svc).run_once()
        self.assertTrue(any(a["cabinet"] == "C-01" for a in actions["overtemp"]))
        self.assertTrue(self.svc.freeze_state("batch", b)["frozen"])
        # 未降温前不能解封
        with self.assertRaises(BusinessError):
            self.svc.unfreeze("admin", "batch", b, "解封")
        self.svc.record_temperature("C-01", 3.0)
        self.svc.unfreeze("admin", "batch", b, "温度恢复")
        self.assertFalse(self.svc.freeze_state("batch", b)["frozen"])

    def test_expiry_scan_freezes_open_stock_only(self):
        b = fx.released(self.svc, ticket="Q-EX", weight=5000)
        self.svc.register_cut("zhang", batch_no=b, weight_g=5000,
                              trays=[("a", 4800)], loss_g=200)
        now = datetime.now()
        self.conn.execute("UPDATE quarantine_tickets SET valid_until=? WHERE ticket_no='Q-EX'",
                          ((now - timedelta(hours=1)).isoformat(timespec="seconds"),))
        actions = ScanScheduler(self.svc).run_once()
        self.assertTrue(any(a["ticket"] == "Q-EX" for a in actions["ticket_expiry"]))
        self.assertTrue(self.svc.freeze_state("batch", b)["frozen"])


class ConcurrentCutTests(unittest.TestCase):
    """并发分割以批次余量为准：多连接竞争，总扣减不得超过到货重量。"""

    def test_concurrent_cuts_never_oversell(self):
        d = Path(tempfile.mkdtemp())
        dbp = d / "c.db"
        # 用连接 A 建库与样例
        c0 = db.connect(dbp); db.init_db(c0)
        s0 = MarketService(c0)
        s0.create_user("admin", "管理员", "market_admin")
        s0.create_user("zhang", "老张", "stall_owner")
        s0.create_stall("A-101", "老张禽摊", "zhang")
        s0.create_supplier("S-01", "绿野（虚构）")
        s0.create_cabinet("C-01", "柜", 4.0); s0.create_locations("C-01")
        vf, uf = fx.window()
        b = s0.arrive("zhang", stall_code="A-101", supplier_code="S-01", ticket_no="Q-C",
                      category="白条鸡", ticket_category="白条鸡", ticket_weight_g=10000,
                      declared_weight_g=10000, valid_from=vf, valid_until=uf)["batch_no"]
        s0.release_to_cabinet("zhang", b, "C-01", "1层", "A")
        c0.close()

        results, lock = [], threading.Lock()

        def worker(i):
            conn = db.connect(dbp)
            svc = MarketService(conn)
            try:
                # 8 个线程各想切 2000g（合计 16000 > 10000）
                r = svc.register_cut("zhang", batch_no=b, weight_g=2000,
                                     trays=[(f"份{i}", 2000)])
                with lock:
                    results.append(("ok", r["batch_remaining_g"]))
            except BusinessError as e:
                with lock:
                    results.append(("fail", str(e)))
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(worker, range(8)))

        ok = [r for r in results if r[0] == "ok"]
        # 恰好成功 5 次（5×2000=10000），其余失败
        self.assertEqual(len(ok), 5, results)
        c1 = db.connect(dbp)
        s1 = MarketService(c1)
        rep = s1.conservation_report(b)
        self.assertTrue(rep["conserved"], rep)
        self.assertEqual(s1.batch_remaining_g(
            c1.execute("SELECT id FROM batches WHERE batch_no=?", (b,)).fetchone()["id"]), 0)
        # 再切必失败
        with self.assertRaises(BusinessError):
            s1.register_cut("zhang", batch_no=b, weight_g=1, trays=[("x", 1)])
        c1.close()


class StallIsolationTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.svc = fx.make_svc()
        self.b = fx.released(self.svc, actor="zhang", ticket="Q-S1", stall="A-101")

    def test_other_owner_cannot_cut(self):
        with self.assertRaises(AuthzError):
            self.svc.register_cut("li", batch_no=self.b, weight_g=100, trays=[("x", 100)])

    def test_other_owner_cannot_arrive_for_stall(self):
        with self.assertRaises(AuthzError):
            fx.arrived(self.svc, actor="li", ticket="Q-S2", stall="A-101")

    def test_other_owner_cannot_release_or_waste(self):
        with self.assertRaises(AuthzError):
            self.svc.release_to_cabinet("li", self.b, "C-01", "1层", "C")
        with self.assertRaises(AuthzError):
            self.svc.log_waste("li", source_type="batch", ref_no=self.b,
                               weight_g=10, reason="x")

    def test_owner_cannot_freeze_or_inspect(self):
        with self.assertRaises(AuthzError):
            self.svc.inspect("zhang", target_type="batch", ref_no=self.b,
                             result="fail", note="x")
        with self.assertRaises(AuthzError):
            self.svc.unfreeze("zhang", "batch", self.b, "x")
        with self.assertRaises(AuthzError):
            self.svc.revoke_ticket("zhang", "Q-S1", "x")

    def test_admin_may_operate_across_stalls(self):
        # 管理员可代摊主操作、可冻结
        c = self.svc.register_cut("admin", batch_no=self.b, weight_g=1000,
                                  trays=[("份", 1000)])
        self.assertTrue(c["trays"])
        self.svc.inspect("admin", target_type="tray", ref_no=c["trays"][0],
                         result="fail", note="管理员抽查")
        self.assertTrue(self.svc.freeze_state("tray", c["trays"][0])["frozen"])

    def test_trays_listed_per_stall(self):
        self.svc.register_cut("zhang", batch_no=self.b, weight_g=1000,
                              trays=[("份", 1000)])
        # 老李看不到老张的托盘
        li_trays = self.svc.list_trays("A-102")
        z_trays = self.svc.list_trays("A-101")
        self.assertEqual(li_trays, [])
        self.assertEqual(len(z_trays), 1)


class MixedBatchAndLabelTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.svc = fx.make_svc()

    def test_mixed_batch_rejected_and_logged(self):
        b1 = fx.released(self.svc, ticket="Q-M1", loc=("C-01", "1层", "A"))
        b2 = fx.arrived(self.svc, ticket="Q-M2")["batch_no"]
        with self.assertRaises(BusinessError):
            self.svc.release_to_cabinet("zhang", b2, "C-01", "1层", "A")
        n = self.conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE kind='mixed_batch'").fetchone()[0]
        self.assertEqual(n, 1)
        # 换空格位可入柜
        self.svc.release_to_cabinet("zhang", b2, "C-01", "1层", "B")

    def test_label_lost_blocks_sale_until_relabel(self):
        b = fx.released(self.svc, ticket="Q-L1")
        c = self.svc.register_cut("zhang", batch_no=b, weight_g=2000,
                                  trays=[("份", 2000)])
        t = c["trays"][0]
        self.svc.report_label_lost("zhang", t, "脱落")
        with self.assertRaises(BusinessError):
            self.svc.sell_tray("zhang", t, 100)
        # 摊主无权重贴
        with self.assertRaises(AuthzError):
            self.svc.relabel("zhang", t)
        self.svc.relabel("admin", t, "核验一致")
        self.svc.sell_tray("zhang", t, 100)  # 恢复销售

    def test_only_released_batch_can_cut(self):
        r = fx.arrived(self.svc, ticket="Q-NR")
        with self.assertRaises(BusinessError):
            self.svc.register_cut("zhang", batch_no=r["batch_no"], weight_g=100,
                                  trays=[("x", 100)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
