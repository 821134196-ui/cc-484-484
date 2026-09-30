"""HTTP 端到端集成测试：线程内起服务，用 urllib 走真实请求。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poultry import config
from poultry.adapters import MockScale, MockTempProbe
from poultry.database import init_db
from poultry.server import create_server
from poultry.services import MarketService


class HttpFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self._old = (config.DOCS_DIR, config.PHOTOS_DIR)
        config.DOCS_DIR = self.td / "docs"
        config.PHOTOS_DIR = self.td / "photos"
        config.ensure_dirs()
        self.db = self.td / "http.db"
        init_db(self.db)
        self.svc = MarketService(self.db, scale=MockScale(), probe=MockTempProbe())
        self.svc.create_stall("A01", "晨鸣禽档", "王晨鸣")
        self.svc.create_supplier("GY-07", "广源禽业（虚构）", "清远")
        self.svc.create_cell("1号柜", "A01")
        self.server = create_server("127.0.0.1", 0, svc=self.svc,
                                    start_worker=False, log=False)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        config.DOCS_DIR, config.PHOTOS_DIR = self._old
        self.tmp.cleanup()

    def req(self, method, path, body=None, *, role="stall", stall="A01"):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        r.add_header("X-Role", role)
        if role == "stall":
            r.add_header("X-Stall", stall)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def _accepted_batch(self):
        _, r = self.req("POST", "/api/arrivals", {
            "cert_no": "JY-HTTP-1", "supplier": "GY-07", "category": "白条鸡",
            "declared_weight": 40.0, "stall": "A01"})
        bid = r["batch_id"]
        st, vr = self.req("POST", f"/api/batches/{bid}/verify",
                          {"cell": "C1-A01"}, role="admin")
        self.assertEqual(st, 200)
        self.assertEqual(vr["status"], "accepted")
        return bid


class TestHttpFlow(HttpFixture):
    def test_full_flow_and_role_headers(self):
        # 静态页面
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/", timeout=5) as r:
            self.assertIn("text/html", r.headers["Content-Type"])

        bid = self._accepted_batch()

        # 摊主分割
        st, cut = self.req("POST", f"/api/batches/{bid}/cuts",
                           {"tray_weights": [10.0, 10.0], "waste_weight": 1.0})
        self.assertEqual(st, 201)
        self.assertEqual(len(cut["trays"]), 2)

        # 守恒审计
        _, audit = self.req("GET", "/api/audit")
        self.assertTrue(all(a["conserved"] for a in audit))

        # 销售
        tid = cut["trays"][0]["id"]
        st, _ = self.req("POST", f"/api/trays/{tid}/sell", {})
        self.assertEqual(st, 200)

        # 其他摊位不能卖第二个托盘
        st, err = self.req("POST", f"/api/trays/{cut['trays'][1]['id']}/sell",
                           {}, stall="A99")
        self.assertEqual(st, 409)
        self.assertEqual(err["error"], "access_denied")

        # 摊主不能抽查
        st, err = self.req("POST", "/api/inspections",
                           {"target": "batch", "target_id": bid, "result": "fail"})
        self.assertEqual(st, 409)

        # 管理员抽查不合格 → 冻结传播
        st, insp = self.req("POST", "/api/inspections",
                            {"target": "batch", "target_id": bid,
                             "result": "fail", "note": "检测异常"}, role="admin")
        self.assertEqual(st, 201)
        self.assertIn(cut["trays"][1]["id"], insp["trays"])

        # 冻结托盘售出被拒，且响应含文字原因
        st, err = self.req("POST", f"/api/trays/{cut['trays'][1]['id']}/sell", {})
        self.assertEqual(st, 409)
        self.assertIn("冻结", err["message"])

        # 冻结列表带原因
        _, fz = self.req("GET", "/api/freezes", role="admin")
        self.assertTrue(fz["freezes"])
        self.assertTrue(all(f["reason"] for f in fz["freezes"]))

        # 管理员解封后可售
        st, _ = self.req("POST", "/api/freezes/release",
                         {"freeze_id": fz["freezes"][0]["freeze_id"]}, role="admin")
        self.assertEqual(st, 200)
        st, _ = self.req("POST", f"/api/trays/{cut['trays'][1]['id']}/sell", {})
        self.assertEqual(st, 200)

    def test_duplicate_cert_and_partial_recall_http(self):
        bid = self._accepted_batch()
        _, cut = self.req("POST", f"/api/batches/{bid}/cuts",
                          {"tray_weights": [8.0, 8.0, 8.0]})
        # 票号重复
        st, err = self.req("POST", "/api/arrivals", {
            "cert_no": "JY-HTTP-1", "supplier": "GY-07", "category": "白条鸡",
            "declared_weight": 10.0, "stall": "A01"})
        self.assertEqual(st, 409)
        self.assertEqual(err["error"], "dup_cert")

        ids = [t["id"] for t in cut["trays"]]
        # 部分召回：第1个已售 → 追溯；其余冻结
        self.req("POST", f"/api/trays/{ids[0]}/sell", {})
        st, rec = self.req("POST", "/api/recalls",
                           {"reason": "定向召回", "tray_ids": ids}, role="admin")
        self.assertEqual(st, 201)
        self.assertEqual(len(rec["frozen_trays"]), 2)
        self.assertEqual(len(rec["sold_or_missing"]), 1)

        # 召回清单按摊位、托盘号排序
        _, recalls = self.req("GET", "/api/recalls")
        items = recalls[0]["items"]
        ordered = [i["tray_no"] for i in items]
        self.assertEqual(ordered, sorted(ordered))

        # 销毁一个 → 废弃物台账含召回记录
        st, d = self.req("POST", f"/api/recalls/{rec['recall_id']}/destroy",
                         {"tray_ids": [ids[1]]}, role="admin")
        self.assertEqual(st, 200)
        self.assertEqual(len(d["destroyed"]), 1)
        _, wastes = self.req("GET", "/api/wastes")
        self.assertTrue(any("召回" in w["reason"] for w in wastes))


    def test_read_isolation_for_stall(self):
        bid = self._accepted_batch()
        self.req("POST", f"/api/batches/{bid}/cuts", {"tray_weights": [5.0]})
        # 未携带摊位的摊主请求视为无权；携带 A99 看不到 A01 数据
        code, batches = self.req("GET", "/api/batches", stall="A99")
        self.assertEqual(code, 200)
        self.assertEqual(batches, [])
        # 伪造查询参数无效：服务端以 X-Stall 为准
        code, batches = self.req("GET", "/api/batches?stall=A01", stall="A99")
        self.assertEqual(code, 200)
        self.assertEqual(batches, [])
        # 详情越权
        code, err = self.req("GET", f"/api/batches/{bid}", stall="A99")
        self.assertEqual(code, 409)
        self.assertEqual(err["error"], "access_denied")
        # 冻结单仅管理员
        code, err = self.req("GET", "/api/freezes", stall="A01")
        self.assertEqual(code, 409)
        # 管理员不受限
        code, batches = self.req("GET", "/api/batches", role="admin")
        self.assertEqual(code, 200)
        self.assertTrue(batches)


if __name__ == "__main__":
    unittest.main(verbosity=2)
