"""零依赖 HTTP 服务：静态前端 + JSON API + 本地虚构票据/照片目录。

运行：python3 -m poultry_system.run（见 run.py）
鉴权（演示）：请求头 X-Actor 或查询参数 ?actor= 选择当前用户；
              市场管理员可冻结/解封/抽查/召回/撤销，摊主仅限本摊操作。
"""
from __future__ import annotations

import json
import mimetypes
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import db as dbmod
from .service import MarketService, BusinessError, AuthzError, kg

WEB_DIR = Path(__file__).parent / "web"


def _row(obj):
    """sqlite3.Row / 嵌套结构转 JSON 安全对象。"""
    if isinstance(obj, sqlite3.Row):
        obj = dict(obj)
    if isinstance(obj, dict):
        return {k: _row(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_row(v) for v in obj]
    return obj


def _tray_pairs(items):
    """兼容 [{product,weight_g}] 与 [product, weight_g] 两种托盘表达。"""
    out = []
    for t in items or []:
        if isinstance(t, dict):
            out.append((t.get("product") or t.get("product_name"), int(t["weight_g"])))
        else:
            out.append((t[0], int(t[1])))
    return out


class App:
    """HTTP 应用。每个工作线程持有独立 SQLite 连接与 MarketService，
    写事务由 BEGIN IMMEDIATE 在库级串行化（并发分割以批次余量为准）。"""

    def __init__(self, db_path, scale=None, thermo=None, scheduler=None, data_dir=None):
        self.db_path = str(db_path)
        self.scale = scale
        self.thermo = thermo
        self.scheduler = scheduler
        self.data_dir = Path(data_dir) if data_dir else None
        self._local = threading.local()
        self._conns = []
        self._conns_lock = threading.Lock()

    @property
    def svc(self) -> "MarketService":
        s = getattr(self._local, "svc", None)
        if s is None:
            conn = dbmod.connect(self.db_path)
            s = MarketService(conn, scale=self.scale, thermo=self.thermo)
            self._local.svc = s
            self._local.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return s

    def close(self):
        with self._conns_lock:
            for c in self._conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._conns.clear()

    # ------------------------------------------------------------------ GET
    def _scope_stall(self, actor, requested):
        """非管理员只能看本摊；管理员可指定任意摊位或全部。"""
        if not actor:
            return requested
        u = self.svc._one("SELECT * FROM users WHERE username=?", (actor,))
        if not u or u["role"] == "market_admin":
            return requested
        stall = self.svc._one("SELECT code FROM stalls WHERE owner_user_id=?", (u["id"],))
        return stall["code"] if stall else "__none__"

    def _assert_batch_visible(self, bno, stall):
        """非管理员访问指定批次时，批次必须属于本摊。"""
        if not stall or not bno:
            return
        own = self.svc._one(
            """SELECT 1 FROM batches b JOIN stalls s ON s.id=b.stall_id
               WHERE b.batch_no=? AND s.code=?""", (bno, stall))
        if not own:
            raise AuthzError(f"摊位隔离：批次 {bno} 不属于本摊")

    def handle_get(self, path, q, actor=""):
        svc = self.svc
        stall = self._scope_stall(actor, q.get("stall", [None])[0])
        if path == "/api/overview":
            batches = svc.list_batches(stall)
            trays = svc.list_trays(stall)
            wastes = svc.waste_ledger(stall)
            return {
                "n_batches": len(batches),
                "n_trays_on_sale": sum(1 for t in trays if t["status"] == "on_sale"),
                "n_frozen": sum(1 for t in trays if t["frozen"]),
                "n_abnormal_waste": sum(1 for w in wastes if w["abnormal"]),
                "open_incidents": svc._all(
                    "SELECT COUNT(*) c FROM incidents WHERE handled=0")[0]["c"],
                "active_recalls": svc._all(
                    "SELECT COUNT(*) c FROM recalls WHERE active=1")[0]["c"],
                "batches": batches,
            }
        if path == "/api/batches":
            return svc.list_batches(stall)
        if path == "/api/trays":
            bno = q.get("batch", [None])[0]
            self._assert_batch_visible(bno, stall)
            return svc.list_trays(stall, bno)
        if path == "/api/remainders":
            bno = q.get("batch", [None])[0]
            self._assert_batch_visible(bno, stall)
            sql = """
              SELECT rm.*, s.code stall_code, tr.tray_no src_tray_no, b.batch_no,
                 (SELECT GROUP_CONCAT(rt2.tray_id) FROM remainder_trays rt2
                  WHERE rt2.remainder_id=rm.id) child_tray_ids
              FROM remainders rm
              JOIN stalls s ON s.id=rm.stall_id
              JOIN trays tr ON tr.id=rm.tray_id
              JOIN batches b ON b.id=rm.batch_id
            """
            cond, args = [], []
            if bno:
                cond.append("b.batch_no=?"); args.append(bno)
            if stall:
                cond.append("s.code=?"); args.append(stall)
            if cond:
                sql += " WHERE " + " AND ".join(cond)
            sql += " ORDER BY rm.id"
            out = []
            for r in svc._all(sql, args):
                d = dict(r)
                children = []
                if d.get("child_tray_ids"):
                    for tid in str(d["child_tray_ids"]).split(","):
                        tr = svc._one("SELECT tray_no FROM trays WHERE id=?", (tid,))
                        if tr:
                            children.append(tr["tray_no"])
                d["child_tray_nos"] = children
                out.append(d)
            return out
        if path == "/api/recalls":
            return svc.recall_list(stall)
        if path == "/api/waste":
            return svc.waste_ledger(stall)
        if path == "/api/incidents":
            sql = """
                SELECT i.*, s.code stall_code FROM incidents i
                LEFT JOIN stalls s ON s.id=i.stall_id """
            if stall:
                sql += " WHERE s.code=? "
            sql += " ORDER BY i.handled, i.id DESC"
            rows = svc._all(sql, (stall,) if stall else ())
            return [dict(r) for r in rows]
        if path == "/api/freeze-events":
            # 非管理员仅看本摊批次/托盘/余料相关留痕；管理员看全部
            if stall:
                rows = svc._all("""
                    SELECT fe.* FROM freeze_events fe
                    WHERE (fe.target_type='batch' AND fe.target_id IN
                           (SELECT id FROM batches b JOIN stalls s ON s.id=b.stall_id WHERE s.code=?))
                       OR (fe.target_type='tray' AND fe.target_id IN
                           (SELECT id FROM trays t JOIN stalls s ON s.id=t.stall_id WHERE s.code=?))
                       OR (fe.target_type='remainder' AND fe.target_id IN
                           (SELECT id FROM remainders r JOIN stalls s ON s.id=r.stall_id WHERE s.code=?))
                    ORDER BY fe.id DESC LIMIT 100""", (stall, stall, stall))
            else:
                rows = svc._all("SELECT * FROM freeze_events ORDER BY id DESC LIMIT 100")
            return [dict(r) for r in rows]
        if path == "/api/cabinets":
            out = []
            for cab in svc._all("SELECT * FROM cold_cabinets ORDER BY code"):
                locs = []
                for l in svc._all("SELECT * FROM cold_locations WHERE cabinet_id=? ORDER BY shelf,slot_code",
                                  (cab["id"],)):
                    occ = svc._one("""SELECT o.*, b.batch_no, s.code stall_code
                                      FROM location_occupancy o
                                      JOIN batches b ON b.id=o.batch_id
                                      JOIN stalls s ON s.id=b.stall_id
                                      WHERE o.location_id=?""", (l["id"],))
                    latest = svc._one(
                        "SELECT * FROM temperature_readings WHERE cabinet_id=? ORDER BY id DESC LIMIT 1",
                        (cab["id"],))
                    locs.append({"shelf": l["shelf"], "slot": l["slot_code"],
                                 "occupied": dict(occ) if occ else None,
                                 "latest_temp": dict(latest) if latest else None})
                out.append({"code": cab["code"], "name": cab["name"],
                            "max_temp_c": cab["max_temp_c"], "locations": locs})
            return out
        if path == "/api/conservation":
            bno = q.get("batch", [""])[0]
            self._assert_batch_visible(bno, stall)
            return svc.conservation_report(bno)
        if path == "/api/meta":
            return {
                "users": [dict(r) for r in svc._all("SELECT * FROM users ORDER BY role,id")],
                "stalls": [dict(r) for r in svc._all("SELECT * FROM stalls ORDER BY code")],
                "suppliers": [dict(r) for r in svc._all("SELECT * FROM suppliers ORDER BY code")],
                "locations": [dict(r) for r in svc._all("""
                    SELECT l.id, cc.code cabinet_code, cc.name cabinet_name,
                           cc.max_temp_c, l.shelf, l.slot_code
                    FROM cold_locations l JOIN cold_cabinets cc ON cc.id=l.cabinet_id
                    ORDER BY cc.code, l.shelf, l.slot_code""")],
            }
        return None

    # ------------------------------------------------------------------ POST
    def handle_post(self, path, body, actor):
        svc = self.svc
        g = body.get
        if path == "/api/arrive":
            return svc.arrive(actor,
                stall_code=g("stall"), supplier_code=g("supplier"), ticket_no=g("ticket_no"),
                category=g("category"), ticket_category=g("ticket_category", g("category")),
                ticket_weight_g=int(g("ticket_weight_g")),
                declared_weight_g=int(g("declared_weight_g")),
                valid_from=g("valid_from"), valid_until=g("valid_until"),
                ticket_image=g("ticket_image", ""), note=g("note", ""))
        if path == "/api/discrepancy/resolve":
            return svc.resolve_weight_discrepancy(
                actor, g("batch_no"), bool(g("accept")), g("note", ""))
        if path == "/api/release":
            return svc.release_to_cabinet(
                actor, g("batch_no"), g("cabinet"), g("shelf"), g("slot"))
        if path == "/api/vacate":
            return svc.vacate_location(actor, g("batch_no"))
        if path == "/api/cut":
            return svc.register_cut(actor, batch_no=g("batch_no"),
                weight_g=int(g("weight_g")), loss_g=int(g("loss_g", 0)),
                trays=_tray_pairs(g("trays")),
                operator=g("operator"))
        if path == "/api/sell":
            return svc.sell_tray(actor, g("tray_no"), int(g("weight_g")))
        if path == "/api/remainder/recover":
            return svc.recover_remainder(actor, g("tray_no"),
                None if g("weight_g") is None else int(g("weight_g")))
        if path == "/api/remainder/recut":
            return svc.recut_remainder(actor, remainder_no=g("remainder_no"),
                trays=_tray_pairs(g("trays")),
                loss_g=int(g("loss_g", 0)))
        if path == "/api/waste":
            return svc.log_waste(actor, source_type=g("source_type"), ref_no=g("ref_no"),
                weight_g=int(g("weight_g")), reason=g("reason"))
        if path == "/api/label/lost":
            return svc.report_label_lost(actor, g("tray_no"), g("note", ""))
        if path == "/api/label/relabel":
            return svc.relabel(actor, g("tray_no"), g("note", ""))
        if path == "/api/inspect":
            return svc.inspect(actor, target_type=g("target_type"), ref_no=g("ref_no"),
                result=g("result"), note=g("note"))
        if path == "/api/ticket/revoke":
            return svc.revoke_ticket(actor, g("ticket_no"), g("reason"))
        if path == "/api/recall":
            return svc.issue_recall(actor, scope=g("scope"), reason=g("reason"),
                batch_no=g("batch_no"), ticket_no=g("ticket_no"),
                tray_nos=g("tray_nos"))
        if path == "/api/recall/lift":
            return svc.lift_recall(actor, g("recall_no"), g("note", ""))
        if path == "/api/unfreeze":
            return svc.unfreeze(actor, g("target_type"), g("ref_no"), g("note"),
                                cascade=bool(g("cascade", True)))
        if path == "/api/incident/resolve":
            return svc.resolve_incident(actor, int(g("id")), g("resolution"))
        if path == "/api/temp":
            return svc.record_temperature(g("cabinet"), float(g("temp_c")))
        if path == "/api/scan":
            if not self.scheduler:
                return {"ticket_expiry": svc.scan_ticket_expiry(),
                        "overtemp": svc.scan_overtemp()}
            return self.scheduler.run_once()
        raise BusinessError(f"未知接口：{path}")


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(_row(obj), ensure_ascii=False).encode())

        def do_HEAD(self):
            self._serve_static(split=True)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                try:
                    actor = self.headers.get("X-Actor", "") or \
                        parse_qs(parsed.query).get("actor", [""])[0]
                    self._json(app.handle_get(parsed.path, parse_qs(parsed.query), actor))
                except (BusinessError, AuthzError) as e:
                    self._json({"error": str(e), "type": type(e).__name__}, 400)
                except Exception as e:  # 未预期错误也要返回 JSON，避免连接中断
                    self._json({"error": f"服务器内部错误：{e}", "type": "ServerError"}, 500)
                return
            self._serve_static(parsed)

        def do_POST(self):
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self._json({"error": "not found"}, 404)
                return
            actor = self.headers.get("X-Actor", "") or parse_qs(parsed.query).get("actor", [""])[0]
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                self._json({"error": "请求体不是合法 JSON"}, 400)
                return
            try:
                result = app.handle_post(parsed.path, body, actor)
                self._json(result if result is not None else {"ok": True})
            except (BusinessError, AuthzError) as e:
                self._json({"error": str(e), "type": type(e).__name__}, 400)

        def _serve_static(self, parsed=None, split=False):
            parsed = parsed or urlparse(self.path)
            path = parsed.path

            def send_file(fp: Path):
                ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
                body = b"" if split else fp.read_bytes()
                self._send(200, body, ctype)

            # 本地虚构票据 / 现场照片
            if path.startswith("/data/") and app.data_dir:
                base = app.data_dir.resolve()
                fp = (base / path[len("/data/"):]).resolve()
                if fp == base or base in fp.parents:
                    if fp.is_file():
                        send_file(fp)
                        return
            if path == "/":
                path = "/index.html"
            web = WEB_DIR.resolve()
            fp = (web / path.lstrip("/")).resolve()
            if (fp == web or web in fp.parents) and fp.is_file():
                send_file(fp)
                return
            self._send(404, b"not found", "text/plain")

    return Handler


def serve(app: App, host="127.0.0.1", port=8000):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    return httpd
