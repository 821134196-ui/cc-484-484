"""HTTP 服务：JSON API + 本地静态页面/票据/照片（仅标准库）。

角色通过请求头传入：
  X-Role: admin | stall
  X-Stall: A01        （摊主身份，摊位隔离）
  X-Actor: 姓名       （操作人/管理员）
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config
from .errors import AccessDenied, BusinessError
from .scanner import ScanWorker

SAFE_NAME = re.compile(r"^[\w\-.一-龥]+$")


def _json_default(o):
    return str(o)


class Handler(BaseHTTPRequestHandler):
    server_version = "PoultryMarket/1.0"

    # ---- 基础收发 ----
    def _send(self, obj, status: int = 200, ctype: str = "application/json"):
        if ctype == "application/json":
            body = json.dumps(obj, ensure_ascii=False, default=_json_default).encode()
        else:
            body = obj if isinstance(obj, bytes) else str(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self._send({"error": "not_found", "message": f"{path.name} 不存在"}, 404)
            return
        self._send(data, 200, ctype)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise BusinessError("请求体须为 JSON", code="validation_error")

    def _role(self, body: dict) -> tuple[str, str | None, str]:
        role = self.headers.get("X-Role") or body.get("role") or "stall"
        stall = self.headers.get("X-Stall") or body.get("stall_actor")
        actor = self.headers.get("X-Actor") or body.get("actor") or (
            "市场管理员" if role == "admin" else (stall or "摊主"))
        return role, stall, actor

    def log_message(self, fmt, *args):  # 安静日志
        self.server.app_log(f"{self.address_string()} {fmt % args}")

    # ---- 路由 ----
    def _identity(self) -> tuple[str, str | None]:
        role = self.headers.get("X-Role", "stall")
        stall = self.headers.get("X-Stall") if role == "stall" else None
        return role, stall

    def do_GET(self):
        try:
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)
            svc = self.server.svc
            role, actor_stall = self._identity()
            # 查询参数 stall 仅管理员可用；摊主强制限定为自身摊位
            stall = None
            if role == "stall":
                stall = actor_stall
            elif (q.get("stall") or [None])[0]:
                stall = q["stall"][0]
            if p in ("/", "/index.html"):
                return self._file(config.STATIC_DIR / "index.html", "text/html")
            if p.startswith("/static/"):
                return self._static(p[8:])
            if p.startswith("/docs/"):
                return self._data_file(p[6:], config.DOCS_DIR, "image/svg+xml")
            if p.startswith("/photos/"):
                return self._data_file(p[8:], config.PHOTOS_DIR, "image/svg+xml")
            if p == "/api/meta":
                return self._send({"stalls": svc.list_stalls(),
                                   "suppliers": svc.list_suppliers(),
                                   "cells": svc.list_cells(),
                                   "config": {"weight_tolerance": config.WEIGHT_TOLERANCE,
                                              "cold_min": config.COLD_TEMP_MIN,
                                              "cold_max": config.COLD_TEMP_MAX}})
            if p == "/api/batches":
                return self._send(svc.list_batches(stall))
            m = re.fullmatch(r"/api/batches/(\d+)", p)
            if m:
                return self._send(svc.batch_detail(
                    int(m.group(1)), role=role, stall_code_actor=actor_stall))
            if p == "/api/cuts":
                return self._send(svc.list_cuts(stall_code=stall))
            if p == "/api/trays":
                return self._send(svc.list_trays(stall,
                                                 (q.get("status") or [None])[0]))
            if p == "/api/remainders":
                return self._send(svc.list_remainders(stall))
            if p == "/api/wastes":
                return self._send(svc.list_wastes(stall))
            if p == "/api/inspections":
                return self._send(svc.list_inspections(stall))
            if p == "/api/recalls":
                return self._send(svc.list_recalls(stall))
            if p == "/api/freezes":
                if role != "admin":
                    raise AccessDenied("冻结单仅市场管理员可查看")
                return self._send({"freezes": svc.scan_freeze_scope()["freezes"],
                                   "log": svc.list_freezes()})
            if p == "/api/events":
                return self._send(svc.list_events(
                    int((q.get("limit") or ["100"])[0]), stall_code=stall))
            if p == "/api/audit":
                return self._send(svc.conservation_audit(stall))
            if p == "/api/cells":
                return self._send(svc.cells_view(stall))
            self._send({"error": "not_found", "message": p}, 404)
        except BusinessError as e:
            self._send({"error": e.code, "message": e.message, "extra": e.extra}, 409)
        except Exception as e:  # noqa
            self.server.app_log(f"GET {self.path} 异常：{e!r}")
            self._send({"error": "server_error", "message": str(e)}, 500)

    def do_POST(self):
        try:
            p = urlparse(self.path).path
            body = self._body()
            svc = self.server.svc
            role, stall, actor = self._role(body)

            if p == "/api/arrivals":
                bid = svc.register_arrival(
                    cert_no=body["cert_no"], stall_code=body["stall"],
                    supplier_code=body["supplier"], category=body["category"],
                    declared_weight=float(body["declared_weight"]),
                    role=role, stall_code_actor=stall,
                    valid_days=int(body.get("valid_days", config.CERT_DEFAULT_VALID_DAYS)))
                return self._send({"batch_id": bid}, 201)

            m = re.fullmatch(r"/api/batches/(\d+)/verify", p)
            if m:
                return self._send(svc.verify_and_store(
                    int(m.group(1)), cell_code=body["cell"],
                    actual_weight=(float(body["actual_weight"])
                                   if body.get("actual_weight") else None),
                    role=role, actor=actor,
                    tolerate_diff=bool(body.get("tolerate", False))))

            m = re.fullmatch(r"/api/batches/(\d+)/cuts", p)
            if m:
                tw = body.get("tray_weights")
                if isinstance(tw, str):
                    tw = [float(x) for x in tw.split(",") if x.strip()]
                return self._send(svc.register_cut(
                    int(m.group(1)),
                    tray_weights=[float(x) for x in tw],
                    remainder_weight=float(body.get("remainder_weight", 0)),
                    waste_weight=float(body.get("waste_weight", 0)),
                    waste_reason=body.get("waste_reason", "分割边角料"),
                    role=role, stall_code_actor=stall), 201)

            m = re.fullmatch(r"/api/trays/(\d+)/(sell|remainder|waste|label-lost|relabel)", p)
            if m:
                tid, action = int(m.group(1)), m.group(2)
                if action == "sell":
                    return self._send(svc.sell_tray(tid, role=role,
                                                    stall_code_actor=stall))
                if action == "remainder":
                    return self._send({"remainder_id": svc.tray_to_remainder(
                        tid, role=role, stall_code_actor=stall)})
                if action == "waste":
                    return self._send(svc.discard_tray(
                        tid, body.get("reason", "托盘变质废弃"),
                        role=role, stall_code_actor=stall))
                if action == "label-lost":
                    return self._send(svc.report_label_lost(
                        tid, body.get("note", ""), role=role,
                        stall_code_actor=stall))
                return self._send(svc.relabel(tid, role=role, actor=actor))

            m = re.fullmatch(r"/api/remainders/(\d+)/(rework|waste)", p)
            if m:
                rid, action = int(m.group(1)), m.group(2)
                if action == "rework":
                    tw = body.get("tray_weights")
                    if isinstance(tw, str):
                        tw = [float(x) for x in tw.split(",") if x.strip()]
                    return self._send(svc.rework_remainder(
                        rid, tray_weights=[float(x) for x in tw],
                        waste_weight=float(body.get("waste_weight", 0)),
                        role=role, stall_code_actor=stall), 201)
                return self._send(svc.discard_remainder(
                    rid, body.get("reason", "余料废弃"),
                    role=role, stall_code_actor=stall))

            if p == "/api/inspections":
                return self._send(svc.add_inspection(
                    target=body["target"], target_id=int(body["target_id"]),
                    result=body["result"], note=body.get("note", ""),
                    role=role, actor=actor), 201)

            if p == "/api/recalls":
                return self._send(svc.create_recall(
                    reason=body["reason"],
                    tray_ids=[int(x) for x in body["tray_ids"]],
                    cert_no=body.get("cert_no"),
                    batch_id=body.get("batch_id"), role=role, actor=actor), 201)

            m = re.fullmatch(r"/api/recalls/(\d+)/(destroy|close)", p)
            if m:
                rid = int(m.group(1))
                if m.group(2) == "destroy":
                    return self._send(svc.destroy_recall_trays(
                        rid, [int(x) for x in body.get("tray_ids", [])] or None,
                        role=role, actor=actor))
                return self._send(svc.close_recall(rid, role=role, actor=actor))

            m = re.fullmatch(r"/api/certs/([\w\-]+)/revoke", p)
            if m:
                return self._send(svc.revoke_cert(m.group(1), body["reason"],
                                                  role=role, actor=actor))

            if p == "/api/freezes/release":
                if body.get("freeze_id"):
                    return self._send(svc.release_freeze(
                        int(body["freeze_id"]), role=role, actor=actor))
                return self._send(svc.unfreeze_target(
                    body["target_type"], int(body["target_id"]),
                    role=role, actor=actor))

            if p == "/api/scan":
                if role != "admin":
                    raise AccessDenied("扫描任务由市场管理员触发")
                w = self.server.worker
                return self._send(w.run_once())

            self._send({"error": "not_found", "message": p}, 404)
        except BusinessError as e:
            self._send({"error": e.code, "message": e.message, "extra": e.extra}, 409)
        except KeyError as e:
            self._send({"error": "validation_error",
                        "message": f"缺少字段：{e.args[0]}"}, 400)
        except Exception as e:  # noqa
            self.server.app_log(f"POST {self.path} 异常：{e!r}")
            self._send({"error": "server_error", "message": str(e)}, 500)

    # ---- 静态/数据文件 ----
    def _static(self, name: str):
        root = config.STATIC_DIR.resolve()
        target = (root / name).resolve()
        if root not in target.parents or not target.is_file():
            return self._send({"error": "not_found"}, 404)
        ctype = {"css": "text/css", "js": "application/javascript",
                 "html": "text/html", "svg": "image/svg+xml"}.get(
            target.suffix.lstrip("."), "application/octet-stream")
        self._file(target, ctype)

    def _data_file(self, name: str, directory: Path, ctype: str):
        if not SAFE_NAME.match(name):
            return self._send({"error": "bad_name"}, 400)
        self._file(directory / name, ctype)


def create_server(host: str = "127.0.0.1", port: int = 8080, *,
                  svc=None, start_worker: bool = True,
                  log: bool = True) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.svc = svc
    lock = threading.Lock()

    def app_log(msg: str):
        if log:
            with lock:
                print(f"[{threading.current_thread().name}] {msg}", flush=True)

    server.app_log = app_log
    if start_worker:
        server.worker = ScanWorker(svc, logger=app_log)
        server.worker.start()
    else:
        server.worker = None
    return server
