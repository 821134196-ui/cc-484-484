"""核心领域服务：验票放行、批次库存、分割称重、冻结传播、召回、扫描与守恒审计。

角色约定：
- role='stall'  摊主（携带 stall_id）：登记到货、登记分割、销售、余料/废弃登记、报标签脱落
- role='admin'  市场管理员：验票放行入柜、抽查、冻结/解封、召回、补签标签
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from typing import Iterable, Optional

from . import config
from .adapters import MockScale, MockTempProbe
from .database import connect, next_no, tx
from .docs import make_cert_svg, make_label_svg, make_photo_svg
from .errors import *

EPS = 1e-6

ROLE_ADMIN = "admin"
ROLE_STALL = "stall"


def _now() -> datetime:
    return datetime.now()


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class MarketService:
    def __init__(self, db_path=None, scale: Optional[MockScale] = None,
                 probe: Optional[MockTempProbe] = None):
        self.db_path = db_path
        self.scale = scale or MockScale()
        self.probe = probe or MockTempProbe()

    # ---------- 连接 ----------
    def conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    # ---------- 基础资料 ----------
    def create_stall(self, code: str, name: str, operator: str) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO stalls(code,name,operator) VALUES(?,?,?)",
                (code, name, operator))
            return cur.lastrowid

    def create_supplier(self, code: str, name: str, origin: str) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO suppliers(code,name,origin) VALUES(?,?,?)",
                (code, name, origin))
            return cur.lastrowid

    def create_cell(self, cabinet: str, slot: str, temp: float = 2.0) -> int:
        code = f"C{cabinet[0]}-{slot}"
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO cells(cabinet,slot,code,temp,temp_ok,temp_at) "
                "VALUES(?,?,?,?,1,?)",
                (cabinet, slot, code, temp, _fmt(_now())))
            return cur.lastrowid

    def list_stalls(self):
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM stalls ORDER BY code")]

    def list_suppliers(self):
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM suppliers ORDER BY code")]

    def list_cells(self):
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM cells ORDER BY code")]

    # ---------- 事件 ----------
    @staticmethod
    def _event(c, kind: str, message: str, level: str = "warn",
               ref_type: Optional[str] = None, ref_id: Optional[int] = None):
        c.execute(
            "INSERT INTO events(kind,level,message,ref_type,ref_id) VALUES(?,?,?,?,?)",
            (kind, level, message, ref_type, ref_id))

    def list_events(self, limit: int = 100, stall_code: Optional[str] = None):
        if stall_code:
            # 摊主仅见本摊位对象（批次/托盘/分割/余料/抽查/召回）相关事件；
            # 市场级事件（票据/格位/冻结单）的原因已直接展示在批次与托盘上。
            sql = (
                "SELECT * FROM events WHERE "
                "(CASE ref_type "
                "  WHEN 'batch' THEN (SELECT s.code FROM batches b "
                "      JOIN stalls s ON s.id=b.stall_id WHERE b.id=events.ref_id) "
                "  WHEN 'tray' THEN (SELECT s.code FROM trays t "
                "      JOIN stalls s ON s.id=t.stall_id WHERE t.id=events.ref_id) "
                "  WHEN 'cut' THEN (SELECT s.code FROM cuts cu "
                "      JOIN stalls s ON s.id=cu.stall_id WHERE cu.id=events.ref_id) "
                "  WHEN 'remainder' THEN (SELECT s.code FROM remainders r "
                "      JOIN stalls s ON s.id=r.stall_id WHERE r.id=events.ref_id) "
                "  WHEN 'inspection' THEN (SELECT s.code FROM inspections i "
                "      JOIN stalls s ON s.id=i.stall_id WHERE i.id=events.ref_id) "
                "  WHEN 'recall' THEN (SELECT MIN(s.code) FROM recall_items ri "
                "      JOIN trays t ON t.id=ri.tray_id JOIN stalls s ON s.id=t.stall_id "
                "      WHERE ri.recall_id=events.ref_id) "
                "END) = ? ORDER BY id DESC LIMIT ?")
            with self.conn() as c:
                return [dict(r) for r in c.execute(sql, (stall_code, limit))]
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    # =====================================================================
    # 到货验票
    # =====================================================================
    def register_arrival(self, *, cert_no: str, supplier_code: str, category: str,
                         declared_weight: float, stall_code: str,
                         issued_at: Optional[datetime] = None,
                         valid_days: int = config.CERT_DEFAULT_VALID_DAYS,
                         arrived_at: Optional[datetime] = None,
                         role: str = ROLE_STALL, stall_code_actor: Optional[str] = None,
                         doc_file: Optional[str] = None) -> int:
        """摊主登记到货（票据+批次，状态 pending，等待管理员验票）。

        票号重复：同一票号已存在 → DuplicateCertError（唯一约束 + 显式提示）。
        """
        stall = self._require_stall_by_code(stall_code)
        self._assert_stall(role, stall_code_actor, stall["code"])
        issued = issued_at or _now() - timedelta(hours=2)
        valid_until = issued + timedelta(days=valid_days)
        arrived = arrived_at or _now()

        # 预查（友好提示）；并发行仍由 BEGIN IMMEDIATE + cert_no UNIQUE 兜底
        with self.conn() as pc:
            old = pc.execute("SELECT id FROM certs WHERE cert_no=?",
                             (cert_no,)).fetchone()
        if old:
            self._log_dup(cert_no, old["id"])
            raise DuplicateCertError(
                f"检疫票号 {cert_no} 已存在，禁止一票多货重复登记",
                extra={"cert_no": cert_no})

        try:
            with tx(self.conn()) as c:
                sup = c.execute("SELECT code FROM suppliers WHERE code=?",
                                (supplier_code,)).fetchone()
                if not sup:
                    raise ValidationError(f"供应商代号 {supplier_code} 不存在")

                doc = doc_file or f"cert_{cert_no}.svg"
                cur = c.execute(
                    "INSERT INTO certs(cert_no,supplier_code,category,declared_weight,"
                    "issued_at,valid_until,doc_file) VALUES(?,?,?,?,?,?,?)",
                    (cert_no, supplier_code, category, declared_weight,
                     _fmt(issued), _fmt(valid_until), doc))
                cert_id = cur.lastrowid
                date_part = arrived.strftime("%Y%m%d")
                n = c.execute("SELECT COUNT(*) k FROM batches WHERE batch_no LIKE ?",
                              (f"B{date_part}-%",)).fetchone()["k"] + 1
                batch_no = f"B{date_part}-{n:03d}"
                cur = c.execute(
                    "INSERT INTO batches(batch_no,cert_id,stall_id,supplier_code,category,"
                    "declared_weight,arrived_at,status) VALUES(?,?,?,?,?,?,?,'pending')",
                    (batch_no, cert_id, stall["id"], supplier_code, category,
                     declared_weight, _fmt(arrived)))
                batch_id = cur.lastrowid
                self._event(c, "arrival", f"摊位 {stall['code']} 登记到货 {batch_no}，"
                                          f"票号 {cert_no}，{category} {declared_weight}kg",
                            "info", "batch", batch_id)
        except sqlite3.IntegrityError:
            with self.conn() as rc:
                row = rc.execute("SELECT id FROM certs WHERE cert_no=?",
                                 (cert_no,)).fetchone()
            self._log_dup(cert_no, row["id"] if row else None)
            raise DuplicateCertError(
                f"检疫票号 {cert_no} 已存在，禁止一票多货重复登记",
                extra={"cert_no": cert_no})

        # 本地虚构票据（事务外文件 IO）
        make_cert_svg(config.DOCS_DIR / doc, cert_no=cert_no,
                      supplier=supplier_code, category=category,
                      weight=declared_weight, issued=_fmt(issued),
                      valid_until=_fmt(valid_until))
        make_photo_svg(config.PHOTOS_DIR / f"arrival_{batch_no}.svg",
                       title=f"到货现场 {batch_no}",
                       lines=[f"票号 {cert_no}", f"{category} {declared_weight}kg",
                              f"供应商 {supplier_code}"])
        return batch_id

    def _log_dup(self, cert_no: str, cert_id: Optional[int]) -> None:
        with self.conn() as c:
            self._event(c, "dup_cert",
                        f"到货登记被拒：检疫票号 {cert_no} 重复"
                        + (f"，已关联票据 #{cert_id}" if cert_id else ""),
                        "error", "cert", cert_id)

    def verify_and_store(self, batch_id: int, *, cell_code: str,
                         actual_weight: Optional[float] = None,
                         role: str = ROLE_ADMIN, actor: str = "市场管理员",
                         tolerate_diff: bool = False) -> dict:
        """管理员核验检疫票有效期与批次重量，放行后入柜。

        - 票已撤销/过期 → 拒收
        - 票货不符（品类/供应商）→ 拒收
        - 称重差异超 ±2% → 记 weight_diff 并拒收（tolerate_diff 仅记录放行）
        - 跨品类混放、冷藏超温 → 拒绝入柜
        """
        self._assert_admin(role)
        with tx(self.conn()) as c:
            b = self._batch(c, batch_id)
            if b["status"] != "pending":
                raise BusinessError(f"批次 {b['batch_no']} 当前状态 {b['status']}，不可验票",
                                    code="invalid_state")
            cert = c.execute("SELECT * FROM certs WHERE id=?", (b["cert_id"],)).fetchone()
            now = _now()

            # 1) 票状态与有效期
            if cert["status"] == "revoked":
                return self._reject(c, b, f"检疫票 {cert['cert_no']} 已撤销："
                                          f"{cert['revoked_reason']}")
            valid_until = datetime.strptime(cert["valid_until"], "%Y-%m-%d %H:%M:%S")
            if now > valid_until:
                c.execute("UPDATE certs SET status='expired' WHERE id=?", (cert["id"],))
                self._event(c, "cert_expired", f"检疫票 {cert['cert_no']} 已过有效期 "
                                               f"({cert['valid_until']})", "error",
                            "cert", cert["id"])
                return self._reject(c, b, f"检疫票 {cert['cert_no']} 已过有效期 "
                                          f"{cert['valid_until']}")

            # 2) 票货相符（供应商、品类）
            if cert["category"] != b["category"]:
                self._event(c, "goods_mismatch",
                            f"批次 {b['batch_no']} 票货不符：票面 {cert['category']} / "
                            f"实到 {b['category']}", "error", "batch", b["id"])
                return self._reject(c, b, f"票货不符：票面品类 {cert['category']}，"
                                          f"实到 {b['category']}")
            if cert["supplier_code"] != b["supplier_code"]:
                self._event(c, "goods_mismatch",
                            f"批次 {b['batch_no']} 票货不符：票面供应商 "
                            f"{cert['supplier_code']} / 实到 {b['supplier_code']}",
                            "error", "batch", b["id"])
                return self._reject(c, b, "票货不符：供应商代号与票面不一致")

            # 3) 过磅称重（模拟适配器回执）
            receipt = self.scale.weigh(actual_weight if actual_weight is not None
                                       else b["declared_weight"])
            aw = receipt.weight
            diff = aw - cert["declared_weight"]
            ratio = abs(diff) / cert["declared_weight"]
            if ratio > config.WEIGHT_TOLERANCE + EPS:
                self._event(c, "weight_diff",
                            f"批次 {b['batch_no']} 称重差异：票面 "
                            f"{cert['declared_weight']}kg / 实测 {aw}kg，"
                            f"差异 {diff:+.3f}kg（{ratio*100:.1f}%），回执 {receipt.receipt_no}",
                            "warn", "batch", b["id"])
                if not tolerate_diff:
                    return self._reject(
                        c, b, f"称重差异超标：票面 {cert['declared_weight']}kg / "
                              f"实测 {aw}kg（{ratio*100:.1f}% > "
                              f"{config.WEIGHT_TOLERANCE*100:.0f}%）")

            # 4) 格位：测温 + 混放检查
            cell = c.execute("SELECT * FROM cells WHERE code=?", (cell_code,)).fetchone()
            if not cell:
                raise NotFound(f"冷藏格位 {cell_code} 不存在")
            trec = self.probe.read(cell["code"])
            c.execute("UPDATE cells SET temp=?,temp_ok=?,temp_at=? WHERE id=?",
                      (trec.temp, 1 if trec.ok else 0, _fmt(now), cell["id"]))
            if not trec.ok:
                self._event(c, "temp",
                            f"格位 {cell['code']} 测温 {trec.temp}℃ 超限，"
                            f"批次 {b['batch_no']} 暂不入柜（回执 {trec.receipt_no}）",
                            "error", "cell", cell["id"])
                raise TempError(f"冷藏超温：{cell['code']} 当前 {trec.temp}℃，"
                                f"允许 {config.COLD_TEMP_MIN}~{config.COLD_TEMP_MAX}℃")
            mix = self._mix_conflict(c, cell["id"], b["category"])
            if mix is not None:
                self._event(c, "mixed",
                            f"跨批次混放冲突：格位 {cell['code']} 已有 "
                            f"{mix['category']} 批次 {mix['batch_no']}，"
                            f"不可放入 {b['category']} 批次 {b['batch_no']}",
                            "error", "cell", cell["id"])
                raise MixedCellError(
                    f"跨品类混放被拒：{cell['code']} 在柜批次 {mix['batch_no']}"
                    f"（{mix['category']}）与本批 {b['category']} 品类不同")

            # 5) 放行入柜
            c.execute(
                "UPDATE batches SET status='accepted',arrived_weight=?,accepted_at=?,"
                "cell_id=? WHERE id=?",
                (aw, _fmt(now), cell["id"], b["id"]))
            c.execute(
                "INSERT INTO cell_occupancy(cell_id,batch_id,weight_in,placed_at) "
                "VALUES(?,?,?,?)",
                (cell["id"], b["id"], aw, _fmt(now)))
            self._event(c, "accepted",
                        f"批次 {b['batch_no']} 验票放行入柜 {cell['code']}，"
                        f"实测 {aw}kg（称重回执 {receipt.receipt_no}，"
                        f"测温回执 {trec.receipt_no}）", "info", "batch", b["id"])
            return {"batch_id": b["id"], "batch_no": b["batch_no"], "status": "accepted",
                    "arrived_weight": aw, "cell": cell["code"],
                    "scale_receipt": receipt.receipt_no, "temp_receipt": trec.receipt_no,
                    "temp": trec.temp}

    def _reject(self, c, b: sqlite3.Row, reason: str) -> dict:
        c.execute("UPDATE batches SET status='rejected',reject_reason=? WHERE id=?",
                  (reason, b["id"]))
        self._event(c, "rejected", f"批次 {b['batch_no']} 拒收：{reason}", "error",
                    "batch", b["id"])
        return {"batch_id": b["id"], "batch_no": b["batch_no"], "status": "rejected",
                "reason": reason}

    @staticmethod
    def _mix_conflict(c, cell_id: int, category: str):
        """返回格位内品类不同的在柜批次（跨批次混放）；同品类共柜允许。"""
        rows = c.execute(
            "SELECT b.id,b.batch_no,b.category FROM cell_occupancy o "
            "JOIN batches b ON b.id=o.batch_id "
            "WHERE o.cell_id=? AND o.cleared_at IS NULL "
            "AND o.weight_in-o.weight_out > 0.001 AND b.category <> ?",
            (cell_id, category)).fetchall()
        return rows[0] if rows else None

    # =====================================================================
    # 批次库存 / 重量链路
    # =====================================================================
    @staticmethod
    def _batch(c, batch_id: int) -> sqlite3.Row:
        r = c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not r:
            raise NotFound(f"批次 #{batch_id} 不存在")
        return r

    @staticmethod
    def _tray(c, tray_id: int) -> sqlite3.Row:
        r = c.execute("SELECT * FROM trays WHERE id=?", (tray_id,)).fetchone()
        if not r:
            raise NotFound(f"托盘 #{tray_id} 不存在")
        return r

    @staticmethod
    def _remainder(c, rem_id: int) -> sqlite3.Row:
        r = c.execute("SELECT * FROM remainders WHERE id=?", (rem_id,)).fetchone()
        if not r:
            raise NotFound(f"余料 #{rem_id} 不存在")
        return r

    def remaining_weight(self, c, batch_id: int) -> float:
        """批次可分割余量 = 到货重量 - 已分割毛重 - 批次级废弃。"""
        b = self._batch(c, batch_id)
        if not b["arrived_weight"]:
            return 0.0
        used_cut = c.execute(
            "SELECT COALESCE(SUM(gross_weight),0) s FROM cuts "
            "WHERE batch_id=? AND source_remainder_id IS NULL", (batch_id,)).fetchone()["s"]
        used_batch_waste = c.execute(
            "SELECT COALESCE(SUM(weight),0) s FROM wastes "
            "WHERE batch_id=? AND source_type='batch'", (batch_id,)).fetchone()["s"]
        return round(b["arrived_weight"] - used_cut - used_batch_waste, 3)

    def weight_balance(self, batch_id: int) -> dict:
        """重量守恒视图：到货 = 余量 + 在架/冻结托盘 + 已售 + 待处理余料 + 废弃。"""
        with self.conn() as c:
            b = self._batch(c, batch_id)
            arrived = b["arrived_weight"] or 0.0
            remaining = self.remaining_weight(c, batch_id)
            tray_alive = c.execute(
                "SELECT COALESCE(SUM(weight),0) s FROM trays "
                "WHERE batch_id=? AND status IN ('in_stock','frozen')",
                (batch_id,)).fetchone()["s"]
            sold = c.execute(
                "SELECT COALESCE(SUM(weight),0) s FROM trays "
                "WHERE batch_id=? AND status='sold'", (batch_id,)).fetchone()["s"]
            rem = c.execute(
                "SELECT COALESCE(SUM(weight),0) s FROM remainders "
                "WHERE batch_id=? AND status='pending'", (batch_id,)).fetchone()["s"]
            waste = c.execute(
                "SELECT COALESCE(SUM(weight),0) s FROM wastes WHERE batch_id=?",
                (batch_id,)).fetchone()["s"]
            accounted = round(remaining + tray_alive + sold + rem + waste, 3)
            return {
                "batch_id": batch_id, "batch_no": b["batch_no"],
                "arrived": round(arrived, 3), "remaining": remaining,
                "trays_alive": round(tray_alive, 3), "sold": round(sold, 3),
                "remainders_pending": round(rem, 3), "waste": round(waste, 3),
                "accounted": accounted,
                "conserved": abs(accounted - arrived) < 1e-3,
                "gap": round(arrived - accounted, 3),
            }

    def conservation_audit(self, stall_code: Optional[str] = None) -> list[dict]:
        with self.conn() as c:
            if stall_code:
                ids = [r["id"] for r in c.execute(
                    "SELECT b.id FROM batches b JOIN stalls s ON s.id=b.stall_id "
                    "WHERE b.status IN ('accepted','empty') AND s.code=?",
                    (stall_code,))]
            else:
                ids = [r["id"] for r in c.execute(
                    "SELECT id FROM batches WHERE status IN ('accepted','empty')")]
        return [self.weight_balance(i) for i in ids]

    def list_batches(self, stall_code: Optional[str] = None) -> list[dict]:
        """批次库存视图（含余量、冻结原因、票据缩略条）。"""
        sql = ("SELECT b.*, s.code stall_code, s.name stall_name, ce.cert_no, "
               "ce.status cert_status, ce.valid_until, ce.revoked_reason, ce.doc_file, "
               "cl.code cell_code FROM batches b "
               "JOIN stalls s ON s.id=b.stall_id "
               "JOIN certs ce ON ce.id=b.cert_id "
               "LEFT JOIN cells cl ON cl.id=b.cell_id ")
        args = ()
        if stall_code:
            sql += "WHERE s.code=? "
            args = (stall_code,)
        sql += "ORDER BY b.id DESC"
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(sql, args)]
            for r in rows:
                r["remaining"] = (self.remaining_weight(c, r["id"])
                                  if r["status"] == "accepted" else None)
            return rows

    def batch_detail(self, batch_id: int, *, role: str = ROLE_ADMIN,
                     stall_code_actor: Optional[str] = None) -> dict:
        with self.conn() as c:
            row = self._batch(c, batch_id)
            if role != ROLE_ADMIN:
                stall = c.execute("SELECT code FROM stalls WHERE id=?",
                                  (row["stall_id"],)).fetchone()
                self._assert_stall(role, stall_code_actor, stall["code"])
            b = dict(row)
            cert = dict(c.execute("SELECT * FROM certs WHERE id=?",
                                  (b["cert_id"],)).fetchone())
            cuts = [dict(r) for r in c.execute(
                "SELECT * FROM cuts WHERE batch_id=? ORDER BY id", (batch_id,))]
            trays = [dict(r) for r in c.execute(
                "SELECT * FROM trays WHERE batch_id=? ORDER BY id", (batch_id,))]
            rems = [dict(r) for r in c.execute(
                "SELECT * FROM remainders WHERE batch_id=? ORDER BY id", (batch_id,))]
            wastes = [dict(r) for r in c.execute(
                "SELECT * FROM wastes WHERE batch_id=? ORDER BY id", (batch_id,))]
            b["remaining"] = self.remaining_weight(c, batch_id)
        return {"batch": b, "cert": cert, "cuts": cuts, "trays": trays,
                "remainders": rems, "wastes": wastes,
                "balance": self.weight_balance(batch_id)}

    # =====================================================================
    # 分割称重（并发以批次余量为准，同一重量不能重复分割）
    # =====================================================================
    def register_cut(self, batch_id: int, *, tray_weights: list[float],
                     remainder_weight: float = 0.0, waste_weight: float = 0.0,
                     waste_reason: str = "分割边角料",
                     role: str = ROLE_STALL, stall_code_actor: Optional[str] = None,
                     operator: Optional[str] = None) -> dict:
        if not tray_weights or any(w <= 0 for w in tray_weights):
            raise ValidationError("分割至少产出一个正重量托盘")
        if remainder_weight < 0 or waste_weight < 0:
            raise ValidationError("余料/废弃重量不能为负")
        tray_sum = round(sum(tray_weights), 3)
        gross = round(tray_sum + remainder_weight + waste_weight, 3)

        with tx(self.conn()) as c:  # BEGIN IMMEDIATE：并发提交串行化，以批次余量为准
            b = self._batch(c, batch_id)
            stall = c.execute("SELECT * FROM stalls WHERE id=?",
                              (b["stall_id"],)).fetchone()
            self._assert_stall(role, stall_code_actor, stall["code"])
            if b["status"] != "accepted":
                raise BusinessError(f"批次 {b['batch_no']} 未放行（{b['status']}），不可分割",
                                    code="invalid_state")
            if b["frozen"]:
                raise FrozenError(f"批次 {b['batch_no']} 已冻结：{b['freeze_reason']}")
            remaining = self.remaining_weight(c, batch_id)
            if gross > remaining + 1e-3:
                self._event(c, "over_cut",
                            f"批次 {b['batch_no']} 分割 {gross}kg 超过余量 "
                            f"{remaining}kg，提交被拒", "error", "batch", batch_id)
                raise InsufficientError(
                    f"批次余量不足：申请分割 {gross}kg，当前余量 {remaining}kg")

            # 分割守恒（毛重 = 托盘 + 余料 + 废弃）由加法恒等保证；记录称重回执
            r1 = self.scale.weigh(gross)
            cut_no = next_no(c, "CUT-", pad=6)
            op = operator or stall["operator"]
            cur = c.execute(
                "INSERT INTO cuts(cut_no,batch_id,stall_id,gross_weight,tray_weight,"
                "remainder_weight,waste_weight,operator) VALUES(?,?,?,?,?,?,?,?)",
                (cut_no, batch_id, stall["id"], gross, tray_sum,
                 remainder_weight, waste_weight, op))
            cut_id = cur.lastrowid

            tray_rows = []
            today = _now().strftime("%Y%m%d")
            for w in tray_weights:
                tn = next_no(c, f"T-{stall['code']}-{today}-", pad=3)
                label_file = f"label_{tn}.svg"
                cur = c.execute(
                    "INSERT INTO trays(tray_no,cut_id,batch_id,stall_id,category,"
                    "weight,label_file) VALUES(?,?,?,?,?,?,?)",
                    (tn, cut_id, batch_id, stall["id"], b["category"], w, label_file))
                tid = cur.lastrowid
                make_label_svg(config.DOCS_DIR / label_file, tray_no=tn,
                               batch_no=b["batch_no"], category=b["category"],
                               weight=w, stall=stall["code"], cut_no=cut_no)
                tray_rows.append({"id": tid, "tray_no": tn, "weight": w})

            rem_id = None
            if remainder_weight > 0:
                rm = next_no(c, "RM-", pad=6)
                cur = c.execute(
                    "INSERT INTO remainders(code,batch_id,stall_id,source_cut_id,"
                    "weight,status) VALUES(?,?,?,?,?,'pending')",
                    (rm, batch_id, stall["id"], cut_id, remainder_weight))
                rem_id = cur.lastrowid

            abnormal = 0
            if waste_weight > 0:
                ratio = waste_weight / gross
                abnormal = 1 if ratio > config.WASTE_RATIO_ALERT else 0
                wcode = next_no(c, "W-", pad=6)
                c.execute(
                    "INSERT INTO wastes(code,batch_id,stall_id,source_type,source_id,"
                    "cut_id,weight,reason,abnormal) VALUES(?,?,?,?,?,?,?,?,?)",
                    (wcode, batch_id, stall["id"], "cut", cut_id, cut_id,
                     waste_weight, waste_reason, abnormal))
                if abnormal:
                    self._event(c, "waste_abnormal",
                                f"分割 {cut_no} 废弃 {waste_weight}kg 占毛重 "
                                f"{ratio*100:.1f}%（> {config.WASTE_RATIO_ALERT*100:.0f}%）"
                                f"，列入废弃物异常", "warn", "cut", cut_id)

            # 占用格位累计取出
            c.execute(
                "UPDATE cell_occupancy SET weight_out=weight_out+? "
                "WHERE cell_id=? AND batch_id=? AND cleared_at IS NULL",
                (gross, b["cell_id"], batch_id))
            self._maybe_empty(c, b)
            self._event(c, "cut",
                        f"{stall['code']} 分割 {cut_no}：批次 {b['batch_no']} 扣减 "
                        f"{gross}kg → 托盘 {len(tray_rows)} 个 {tray_sum}kg、"
                        f"余料 {remainder_weight}kg、废弃 {waste_weight}kg"
                        f"（称重回执 {r1.receipt_no}）", "info", "cut", cut_id)
            return {"cut_id": cut_id, "cut_no": cut_no, "gross_weight": gross,
                    "remaining": self.remaining_weight(c, batch_id),
                    "trays": tray_rows, "remainder_id": rem_id,
                    "waste_abnormal": bool(abnormal), "scale_receipt": r1.receipt_no}

    def list_cuts(self, batch_id: Optional[int] = None,
                  stall_code: Optional[str] = None):
        sql = ("SELECT cu.*, b.batch_no, s.code stall_code FROM cuts cu "
               "JOIN batches b ON b.id=cu.batch_id JOIN stalls s ON s.id=cu.stall_id "
               "WHERE 1=1 ")
        args: list = []
        if batch_id:
            sql += "AND cu.batch_id=? "; args.append(batch_id)
        if stall_code:
            sql += "AND s.code=? "; args.append(stall_code)
        sql += "ORDER BY cu.id DESC"
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args)]

    # =====================================================================
    # 托盘：销售 / 余料回收 / 废弃 / 标签脱落
    # =====================================================================
    def list_trays(self, stall_code: Optional[str] = None, status: Optional[str] = None):
        sql = ("SELECT t.*, s.code stall_code, b.batch_no FROM trays t "
               "JOIN stalls s ON s.id=t.stall_id "
               "JOIN batches b ON b.id=t.batch_id WHERE 1=1 ")
        args: list = []
        if stall_code:
            sql += "AND s.code=? "; args.append(stall_code)
        if status:
            sql += "AND t.status=? "; args.append(status)
        sql += "ORDER BY t.id DESC"
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def sell_tray(self, tray_id: int, *, role: str = ROLE_STALL,
                  stall_code_actor: Optional[str] = None) -> dict:
        with tx(self.conn()) as c:
            t = self._tray(c, tray_id)
            stall = c.execute("SELECT code FROM stalls WHERE id=?",
                              (t["stall_id"],)).fetchone()
            self._assert_stall(role, stall_code_actor, stall["code"])
            if t["label_lost"]:
                raise LabelLostError(f"托盘 {t['tray_no']} 标签脱落未补签，禁止销售")
            if t["frozen"] or t["status"] == "frozen":
                raise FrozenError(f"托盘 {t['tray_no']} 已冻结：{t['freeze_reason']}")
            if t["status"] != "in_stock":
                raise BusinessError(f"托盘 {t['tray_no']} 状态 {t['status']}，不可销售",
                                    code="invalid_state")
            c.execute("UPDATE trays SET status='sold',sold_at=? WHERE id=?",
                      (_fmt(_now()), tray_id))
            self._event(c, "sold", f"托盘 {t['tray_no']} 售出 {t['weight']}kg "
                                   f"（批次链 {self._chain_text(c, t)}）", "info",
                        "tray", tray_id)
            return {"tray_id": tray_id, "tray_no": t["tray_no"], "status": "sold"}

    def tray_to_remainder(self, tray_id: int, *, role: str = ROLE_STALL,
                          stall_code_actor: Optional[str] = None) -> int:
        """未售托盘转余料回收，继续沿用批次链。"""
        with tx(self.conn()) as c:
            t = self._tray(c, tray_id)
            stall = c.execute("SELECT code FROM stalls WHERE id=?",
                              (t["stall_id"],)).fetchone()
            self._assert_stall(role, stall_code_actor, stall["code"])
            if t["frozen"] or t["status"] == "frozen":
                raise FrozenError(f"托盘 {t['tray_no']} 已冻结：{t['freeze_reason']}")
            if t["status"] != "in_stock":
                raise BusinessError(f"托盘 {t['tray_no']} 状态 {t['status']}，不可转余料",
                                    code="invalid_state")
            code = next_no(c, "RM-", pad=6)
            cur = c.execute(
                "INSERT INTO remainders(code,batch_id,stall_id,source_cut_id,"
                "source_tray_id,weight) VALUES(?,?,?,?,?,?)",
                (code, t["batch_id"], t["stall_id"], t["cut_id"], t["id"], t["weight"]))
            rem_id = cur.lastrowid
            c.execute("UPDATE trays SET status='remainder' WHERE id=?", (tray_id,))
            self._event(c, "remainder", f"托盘 {t['tray_no']} 转余料 {code}，"
                                        f"沿用批次链 {self._chain_text(c, t)}", "info",
                        "remainder", rem_id)
            return rem_id

    def rework_remainder(self, remainder_id: int, *, tray_weights: list[float],
                         waste_weight: float = 0.0,
                         waste_reason: str = "余料修整损耗",
                         role: str = ROLE_STALL,
                         stall_code_actor: Optional[str] = None) -> dict:
        """余料重新分割：批次链不变，重量守恒（余料 = 新托盘 + 废弃）。"""
        if not tray_weights or any(w <= 0 for w in tray_weights):
            raise ValidationError("返工至少产出一个正重量托盘")
        tray_sum = round(sum(tray_weights), 3)
        total = round(tray_sum + waste_weight, 3)
        with tx(self.conn()) as c:
            r = self._remainder(c, remainder_id)
            stall = c.execute("SELECT * FROM stalls WHERE id=?",
                              (r["stall_id"],)).fetchone()
            self._assert_stall(role, stall_code_actor, stall["code"])
            if r["status"] != "pending":
                raise BusinessError(f"余料 {r['code']} 状态 {r['status']}，不可返工",
                                    code="invalid_state")
            if r["frozen"]:
                raise FrozenError(f"余料 {r['code']} 已冻结：{r['freeze_reason']}")
            if abs(total - r["weight"]) > 1e-3:
                raise WeightMismatchError(
                    f"余料返工重量不守恒：余料 {r['weight']}kg，"
                    f"申报产出 {total}kg（托盘 {tray_sum} + 废弃 {waste_weight}）")
            b = self._batch(c, r["batch_id"])
            cut_no = next_no(c, "CUT-", pad=6)
            cur = c.execute(
                "INSERT INTO cuts(cut_no,batch_id,stall_id,gross_weight,"
                "source_remainder_id,tray_weight,remainder_weight,waste_weight,operator)"
                " VALUES(?,?,?,0,?,?,?,0,?)",
                (cut_no, b["id"], stall["id"], r["id"], tray_sum, waste_weight,
                 stall["operator"]))
            cut_id = cur.lastrowid
            today = _now().strftime("%Y%m%d")
            new_trays = []
            for w in tray_weights:
                tn = next_no(c, f"T-{stall['code']}-{today}-", pad=3)
                label_file = f"label_{tn}.svg"
                cur = c.execute(
                    "INSERT INTO trays(tray_no,cut_id,source_remainder_id,batch_id,"
                    "stall_id,category,weight,label_file) VALUES(?,?,?,?,?,?,?,?)",
                    (tn, cut_id, r["id"], b["id"], stall["id"], b["category"], w,
                     label_file))
                make_label_svg(config.DOCS_DIR / label_file, tray_no=tn,
                               batch_no=b["batch_no"], category=b["category"],
                               weight=w, stall=stall["code"], cut_no=cut_no,
                               remark=f"余料返工 {r['code']}")
                new_trays.append({"id": cur.lastrowid, "tray_no": tn, "weight": w})
            c.execute("UPDATE remainders SET status='reworked' WHERE id=?",
                      (remainder_id,))
            if waste_weight > 0:
                ratio = waste_weight / r["weight"]
                abnormal = 1 if ratio > config.WASTE_RATIO_ALERT else 0
                wcode = next_no(c, "W-", pad=6)
                c.execute(
                    "INSERT INTO wastes(code,batch_id,stall_id,source_type,source_id,"
                    "cut_id,weight,reason,abnormal) VALUES(?,?,?,?,?,?,?,?,?)",
                    (wcode, b["id"], stall["id"], "remainder", r["id"], cut_id,
                     waste_weight, waste_reason, abnormal))
                if abnormal:
                    self._event(c, "waste_abnormal",
                                f"余料 {r['code']} 返工废弃 {waste_weight}kg 占比 "
                                f"{ratio*100:.1f}% 异常", "warn", "remainder", r["id"])
            self._event(c, "rework", f"余料 {r['code']} 返工为 {len(new_trays)} 个托盘 "
                                     f"{tray_sum}kg、废弃 {waste_weight}kg，"
                                     f"批次链 {b['batch_no']} 不变", "info",
                        "remainder", remainder_id)
            return {"cut_id": cut_id, "trays": new_trays}

    def discard_tray(self, tray_id: int, reason: str, *, role: str = ROLE_STALL,
                     stall_code_actor: Optional[str] = None) -> dict:
        with tx(self.conn()) as c:
            t = self._tray(c, tray_id)
            stall = c.execute("SELECT code FROM stalls WHERE id=?",
                              (t["stall_id"],)).fetchone()
            self._assert_stall(role, stall_code_actor, stall["code"])
            if t["frozen"] or t["status"] == "frozen":
                raise FrozenError(f"托盘 {t['tray_no']} 已冻结：{t['freeze_reason']}，"
                                  f"须由管理员按召回/销毁流程处置")
            if t["status"] not in ("in_stock",):
                raise BusinessError(f"托盘 {t['tray_no']} 状态 {t['status']}，不可废弃",
                                    code="invalid_state")
            return self._discard_tray_row(c, t, reason, role_ok=True)

    def _discard_tray_row(self, c, t: sqlite3.Row, reason: str, *, role_ok: bool):
        wcode = next_no(c, "W-", pad=6)
        abnormal = 1 if t["weight"] > 5.0 else 0  # 单盘超 5kg 废弃预警
        c.execute(
            "INSERT INTO wastes(code,batch_id,stall_id,source_type,source_id,cut_id,"
            "weight,reason,abnormal) VALUES(?,?,?,?,?,?,?,?,?)",
            (wcode, t["batch_id"], t["stall_id"], "tray", t["id"], t["cut_id"],
             t["weight"], reason, abnormal))
        c.execute("UPDATE trays SET status='waste' WHERE id=?", (t["id"],))
        if abnormal:
            self._event(c, "waste_abnormal",
                        f"托盘 {t['tray_no']} 废弃 {t['weight']}kg 超重异常：{reason}",
                        "warn", "tray", t["id"])
        self._event(c, "waste", f"托盘 {t['tray_no']} 废弃 {t['weight']}kg：{reason}",
                    "info", "tray", t["id"])
        return {"waste_code": wcode, "weight": t["weight"], "abnormal": bool(abnormal)}

    def discard_remainder(self, remainder_id: int, reason: str, *,
                          role: str = ROLE_STALL,
                          stall_code_actor: Optional[str] = None) -> dict:
        with tx(self.conn()) as c:
            r = self._remainder(c, remainder_id)
            stall = c.execute("SELECT code FROM stalls WHERE id=?",
                              (r["stall_id"],)).fetchone()
            self._assert_stall(role, stall_code_actor, stall["code"])
            if r["frozen"]:
                raise FrozenError(f"余料 {r['code']} 已冻结：{r['freeze_reason']}")
            if r["status"] != "pending":
                raise BusinessError(f"余料 {r['code']} 状态 {r['status']}，不可废弃",
                                    code="invalid_state")
            wcode = next_no(c, "W-", pad=6)
            abnormal = 1 if r["weight"] > 8.0 else 0
            c.execute(
                "INSERT INTO wastes(code,batch_id,stall_id,source_type,source_id,"
                "weight,reason,abnormal) VALUES(?,?,?,?,?,?,?,?)",
                (wcode, r["batch_id"], r["stall_id"], "remainder", r["id"],
                 r["weight"], reason, abnormal))
            c.execute("UPDATE remainders SET status='waste' WHERE id=?",
                      (remainder_id,))
            if abnormal:
                self._event(c, "waste_abnormal",
                            f"余料 {r['code']} 废弃 {r['weight']}kg 超重异常：{reason}",
                            "warn", "remainder", r["id"])
            self._event(c, "waste", f"余料 {r['code']} 废弃 {r['weight']}kg：{reason}",
                        "info", "remainder", r["id"])
            return {"waste_code": wcode, "weight": r["weight"], "abnormal": bool(abnormal)}

    def report_label_lost(self, tray_id: int, note: str = "", *,
                          role: str = ROLE_STALL,
                          stall_code_actor: Optional[str] = None) -> dict:
        """摊主报告标签脱落：托盘被标记，补签前禁止销售。"""
        with tx(self.conn()) as c:
            t = self._tray(c, tray_id)
            stall = c.execute("SELECT code FROM stalls WHERE id=?",
                              (t["stall_id"],)).fetchone()
            self._assert_stall(role, stall_code_actor, stall["code"])
            if t["status"] not in ("in_stock", "frozen"):
                raise BusinessError(f"托盘 {t['tray_no']} 状态 {t['status']}，无需标签",
                                    code="invalid_state")
            c.execute("UPDATE trays SET label_lost=1 WHERE id=?", (tray_id,))
            self._event(c, "label_lost", f"托盘 {t['tray_no']} 标签脱落报告：{note}"
                                         f"（批次 {self._chain_text(c, t)}），补签前禁售",
                        "warn", "tray", tray_id)
            return {"tray_id": tray_id, "label_lost": True}

    def relabel(self, tray_id: int, *, role: str = ROLE_ADMIN,
                actor: str = "市场管理员") -> dict:
        """管理员核验后补签标签。"""
        self._assert_admin(role)
        with tx(self.conn()) as c:
            t = self._tray(c, tray_id)
            b = self._batch(c, t["batch_id"])
            stall = c.execute("SELECT code FROM stalls WHERE id=?",
                              (t["stall_id"],)).fetchone()
            cut_no = c.execute("SELECT cut_no FROM cuts WHERE id=?",
                               (t["cut_id"],)).fetchone()
            make_label_svg(config.DOCS_DIR / t["label_file"], tray_no=t["tray_no"],
                           batch_no=b["batch_no"], category=t["category"],
                           weight=t["weight"], stall=stall["code"],
                           cut_no=cut_no["cut_no"] if cut_no and cut_no["cut_no"] else "",
                           remark="管理员补签")
            c.execute("UPDATE trays SET label_lost=0 WHERE id=?", (tray_id,))
            self._event(c, "relabel", f"托盘 {t['tray_no']} 已由 {actor} 补签标签",
                        "info", "tray", tray_id)
            return {"tray_id": tray_id, "label_lost": False,
                    "label_file": t["label_file"]}

    def list_remainders(self, stall_code: Optional[str] = None):
        sql = ("SELECT r.*, s.code stall_code, b.batch_no FROM remainders r "
               "JOIN stalls s ON s.id=r.stall_id JOIN batches b ON b.id=r.batch_id "
               "WHERE 1=1 ")
        args: list = []
        if stall_code:
            sql += "AND s.code=? "; args.append(stall_code)
        sql += "ORDER BY r.id DESC"
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def list_wastes(self, stall_code: Optional[str] = None):
        sql = ("SELECT w.*, s.code stall_code, b.batch_no FROM wastes w "
               "JOIN stalls s ON s.id=w.stall_id JOIN batches b ON b.id=w.batch_id "
               "WHERE 1=1 ")
        args: list = []
        if stall_code:
            sql += "AND s.code=? "; args.append(stall_code)
        sql += "ORDER BY w.id DESC"
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args)]

    # =====================================================================
    # 检疫票撤销 / 市场抽查 / 召回 —— 冻结传播（管理员）
    # =====================================================================
    def revoke_cert(self, cert_no: str, reason: str, *,
                    role: str = ROLE_ADMIN, actor: str = "市场管理员") -> dict:
        self._assert_admin(role)
        with tx(self.conn()) as c:
            cert = c.execute("SELECT * FROM certs WHERE cert_no=?",
                             (cert_no,)).fetchone()
            if not cert:
                raise NotFound(f"检疫票 {cert_no} 不存在")
            if cert["status"] == "revoked":
                raise BusinessError(f"检疫票 {cert_no} 已是撤销状态",
                                    code="invalid_state")
            c.execute("UPDATE certs SET status='revoked',revoked_reason=? WHERE id=?",
                      (reason, cert["id"]))
            batch_ids = [r["id"] for r in c.execute(
                "SELECT id FROM batches WHERE cert_id=? AND status='accepted'",
                (cert["id"],))]
            result = self._freeze(c, "cert", cert["id"],
                                  f"检疫票 {cert_no} 撤销：{reason}", actor,
                                  batch_ids=batch_ids)
            self._event(c, "revoke", f"检疫票 {cert_no} 已撤销，冻结 "
                                     f"{len(result['trays'])} 个未售托盘", "error",
                        "cert", cert["id"])
            result["cert_no"] = cert_no
            return result

    def add_inspection(self, *, target: str, target_id: int, result: str,
                       note: str = "", role: str = ROLE_ADMIN,
                       actor: str = "市场管理员") -> dict:
        """市场抽查。target: batch / tray。不合格 → 相关未售托盘立即冻结。"""
        self._assert_admin(role)
        if result not in ("pass", "fail"):
            raise ValidationError("抽查结果须为 pass/fail")
        with tx(self.conn()) as c:
            batch_id = stall_id = tray_id = None
            if target == "batch":
                b = self._batch(c, target_id)
                batch_id, stall_id = b["id"], b["stall_id"]
            elif target == "tray":
                t = self._tray(c, target_id)
                tray_id, batch_id, stall_id = t["id"], t["batch_id"], t["stall_id"]
            else:
                raise ValidationError("抽查对象须为 batch 或 tray")
            no = next_no(c, "JC-", pad=6)
            cur = c.execute(
                "INSERT INTO inspections(insp_no,batch_id,tray_id,stall_id,result,"
                "note,inspector) VALUES(?,?,?,?,?,?,?)",
                (no, batch_id, tray_id, stall_id, result, note, actor))
            insp_id = cur.lastrowid
            if result != "fail":
                self._event(c, "inspection_pass", f"抽查 {no} 合格", "info",
                            "inspection", insp_id)
                return {"inspection_id": insp_id, "insp_no": no, "frozen": 0}
            if target == "tray":
                reason = f"抽查 {no} 托盘不合格：{note or '检测异常'}（同批未售托盘联动冻结）"
            else:
                reason = f"抽查 {no} 批次不合格：{note or '检测异常'}（未售托盘立即冻结）"
            res = self._freeze(c, "inspection", insp_id, reason, actor,
                               batch_ids=[batch_id])
            self._event(c, "inspection_fail",
                        f"抽查 {no} 不合格，冻结批次 {batch_id} 下 "
                        f"{len(res['trays'])} 个未售托盘", "error",
                        "inspection", insp_id)
            res.update({"inspection_id": insp_id, "insp_no": no})
            return res

    def create_recall(self, *, reason: str, tray_ids: list[int],
                      cert_no: Optional[str] = None, batch_id: Optional[int] = None,
                      role: str = ROLE_ADMIN, actor: str = "市场管理员") -> dict:
        """部分召回：仅冻结清单内未售托盘，已售托盘登记待追溯。"""
        self._assert_admin(role)
        if not tray_ids:
            raise ValidationError("召回清单不能为空")
        with tx(self.conn()) as c:
            cert_id = None
            if cert_no:
                cr = c.execute("SELECT id FROM certs WHERE cert_no=?",
                               (cert_no,)).fetchone()
                if not cr:
                    raise NotFound(f"检疫票 {cert_no} 不存在")
                cert_id = cr["id"]
            if batch_id is None:
                batch_id = self._tray(c, tray_ids[0])["batch_id"]
            no = next_no(c, "ZH-", pad=6)
            cur = c.execute(
                "INSERT INTO recalls(recall_no,cert_id,batch_id,reason) "
                "VALUES(?,?,?,?)",
                (no, cert_id, batch_id, reason))
            recall_id = cur.lastrowid
            unsold, sold_missing = [], []
            for tid in dict.fromkeys(tray_ids):  # 去重保序
                t = self._tray(c, tid)
                c.execute(
                    "INSERT OR IGNORE INTO recall_items(recall_id,tray_id) VALUES(?,?)",
                    (recall_id, tid))
                if t["status"] in ("in_stock", "frozen"):
                    unsold.append(tid)
                else:
                    sold_missing.append({"tray_id": tid, "tray_no": t["tray_no"],
                                         "status": t["status"]})
            res = self._freeze(c, "recall", recall_id,
                               f"部分召回 {no}：{reason}", actor,
                               batch_ids=[], tray_ids=unsold)
            self._event(c, "recall",
                        f"召回 {no} 建立：清单 {len(tray_ids)} 个托盘，"
                        f"冻结未售 {len(unsold)} 个，已售/不在架 {len(sold_missing)} "
                        f"个转追溯", "error", "recall", recall_id)
            return {"recall_id": recall_id, "recall_no": no,
                    "frozen_trays": res["trays"], "sold_or_missing": sold_missing}

    def destroy_recall_trays(self, recall_id: int, tray_ids: Optional[list[int]] = None,
                             *, role: str = ROLE_ADMIN,
                             actor: str = "市场管理员") -> dict:
        """召回托盘销毁：入废弃物台账（召回销毁）。"""
        self._assert_admin(role)
        destroyed = []
        with tx(self.conn()) as c:
            rec = c.execute("SELECT * FROM recalls WHERE id=?", (recall_id,)).fetchone()
            if not rec:
                raise NotFound(f"召回 #{recall_id} 不存在")
            sql = ("SELECT t.* FROM recall_items ri JOIN trays t ON t.id=ri.tray_id "
                   "WHERE ri.recall_id=? AND ri.disposition='pending'")
            args: list = [recall_id]
            if tray_ids:
                sql += " AND ri.tray_id IN (%s)" % ",".join("?" * len(tray_ids))
                args += tray_ids
            rows = c.execute(sql, tuple(args)).fetchall()
            for t in rows:
                self._discard_tray_row(c, t, f"召回 {rec['recall_no']} 销毁："
                                             f"{rec['reason']}", role_ok=True)
                c.execute(
                    "UPDATE recall_items SET disposition='destroyed' "
                    "WHERE recall_id=? AND tray_id=?", (recall_id, t["id"]))
                destroyed.append(t["tray_no"])
            self._event(c, "recall_destroy",
                        f"召回 {rec['recall_no']} 销毁 {len(destroyed)} 个托盘："
                        f"{', '.join(destroyed) or '无'}", "warn", "recall", recall_id)
        return {"destroyed": destroyed}

    def close_recall(self, recall_id: int, *, role: str = ROLE_ADMIN,
                     actor: str = "市场管理员") -> dict:
        """召回结案：未销毁托盘解除召回冻结（若仍受票撤销等其他冻结约束则继续冻结）。"""
        self._assert_admin(role)
        with tx(self.conn()) as c:
            c.execute("UPDATE recalls SET status='closed',closed_at=? WHERE id=? AND status='open'",
                      (_fmt(_now()), recall_id))
            items = [r["tray_id"] for r in c.execute(
                "SELECT tray_id FROM recall_items WHERE recall_id=?", (recall_id,))]
            c.execute("UPDATE recall_items SET disposition='released' "
                      "WHERE recall_id=? AND disposition='pending'", (recall_id,))
        self.release_freeze(scope_type="recall", ref_id=recall_id, actor=actor,
                            role=role)
        return {"recall_id": recall_id, "released_trays": items}

    def list_inspections(self, stall_code: Optional[str] = None):
        sql = ("SELECT i.*, s.code stall_code, b.batch_no, t.tray_no "
               "FROM inspections i JOIN stalls s ON s.id=i.stall_id "
               "LEFT JOIN batches b ON b.id=i.batch_id "
               "LEFT JOIN trays t ON t.id=i.tray_id WHERE 1=1 ")
        args: list = []
        if stall_code:
            sql += "AND s.code=? "; args.append(stall_code)
        sql += "ORDER BY i.id DESC"
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def list_recalls(self, stall_code: Optional[str] = None):
        with self.conn() as c:
            recs = [dict(r) for r in c.execute(
                "SELECT r.*, c.cert_no, b.batch_no FROM recalls r "
                "LEFT JOIN certs c ON c.id=r.cert_id "
                "LEFT JOIN batches b ON b.id=r.batch_id ORDER BY r.id DESC")]
            for r in recs:
                r["items"] = [dict(x) for x in c.execute(
                    "SELECT ri.*, t.tray_no, t.weight, t.status, t.freeze_reason, "
                    "s.code stall_code FROM recall_items ri "
                    "JOIN trays t ON t.id=ri.tray_id "
                    "JOIN stalls s ON s.id=t.stall_id WHERE ri.recall_id=? "
                    + ("AND s.code=? " if stall_code else "")
                    + "ORDER BY s.code, t.tray_no",
                    (r["id"], stall_code) if stall_code else (r["id"],))]
            if stall_code:
                recs = [r for r in recs if r["items"]]
            return recs

    def list_freezes(self):
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM freezes ORDER BY id DESC")]

    # ---------- 冻结内核 ----------
    def _freeze(self, c, scope_type: str, ref_id: int, reason: str, issued_by: str,
                *, batch_ids: Iterable[int] = (), tray_ids: Iterable[int] = ()) -> dict:
        """建立冻结单并沿批次链传播：批次 + 未售托盘 + 待处理余料。"""
        batch_ids = list(batch_ids)
        tray_ids = list(dict.fromkeys(tray_ids))
        for bid in batch_ids:
            tray_ids += [r["id"] for r in c.execute(
                "SELECT id FROM trays WHERE batch_id=? AND status='in_stock'", (bid,))]
            tray_ids += [r["id"] for r in c.execute(
                "SELECT id FROM trays WHERE batch_id=? AND status='frozen' "
                "AND freeze_reason IS ?", (bid, None))]
        tray_ids = list(dict.fromkeys(tray_ids))

        cur = c.execute(
            "INSERT INTO freezes(scope_type,ref_id,reason,issued_by) VALUES(?,?,?,?)",
            (scope_type, ref_id, reason, issued_by))
        freeze_id = cur.lastrowid

        frozen_batches, frozen_trays, frozen_rems = [], [], []
        for bid in batch_ids:
            c.execute("INSERT OR IGNORE INTO freeze_targets(freeze_id,target_type,"
                      "target_id) VALUES(?, 'batch', ?)", (freeze_id, bid))
            bf = c.execute("SELECT frozen FROM batches WHERE id=?", (bid,)).fetchone()
            c.execute("UPDATE batches SET frozen=1, freeze_reason=? WHERE id=? "
                      "AND frozen=0", (reason, bid))
            if bf and not bf["frozen"]:
                frozen_batches.append(bid)
            c.execute(
                "UPDATE remainders SET frozen=1,freeze_reason=? WHERE batch_id=? "
                "AND status='pending' AND frozen=0", (reason, bid))
            for r in c.execute("SELECT id FROM remainders WHERE batch_id=? "
                               "AND status='pending'", (bid,)):
                c.execute(
                    "INSERT OR IGNORE INTO freeze_targets(freeze_id,target_type,target_id)"
                    " VALUES(?, 'remainder', ?)", (freeze_id, r["id"]))
                frozen_rems.append(r["id"])

        for tid in tray_ids:
            c.execute("INSERT OR IGNORE INTO freeze_targets(freeze_id,target_type,"
                      "target_id) VALUES(?, 'tray', ?)", (freeze_id, tid))
            t = c.execute("SELECT status FROM trays WHERE id=?", (tid,)).fetchone()
            if t and t["status"] == "in_stock":
                c.execute("UPDATE trays SET status='frozen',frozen=1,freeze_reason=?,"
                          "frozen_at=? WHERE id=?", (reason, _fmt(_now()), tid))
                frozen_trays.append(tid)
            elif t and t["status"] == "frozen":
                # 已被其他原因冻结：仍登记目标，原因保留首冻原因
                frozen_trays.append(tid)
        return {"freeze_id": freeze_id, "reason": reason,
                "batches": list(dict.fromkeys(frozen_batches)),
                "trays": frozen_trays, "remainders": list(dict.fromkeys(frozen_rems))}

    def release_freeze(self, freeze_id: Optional[int] = None, *,
                       scope_type: Optional[str] = None, ref_id: Optional[int] = None,
                       role: str = ROLE_ADMIN, actor: str = "市场管理员") -> dict:
        """管理员解封：删除冻结单目标后，按是否仍受其他冻结约束逐个判定。"""
        self._assert_admin(role)
        with tx(self.conn()) as c:
            where, args = "", []
            if freeze_id:
                where, args = "id=?", [freeze_id]
            elif scope_type and ref_id is not None:
                where, args = "scope_type=? AND ref_id=?", [scope_type, ref_id]
            else:
                raise ValidationError("需指定 freeze_id 或 scope_type+ref_id")
            fz = c.execute(f"SELECT * FROM freezes WHERE {where}", args).fetchall()
            if not fz:
                raise NotFound("未找到匹配的冻结单")
            released = {"batches": [], "trays": [], "remainders": []}
            ids = [r["id"] for r in fz]
            for f in fz:
                for t in c.execute("SELECT target_type,target_id FROM freeze_targets "
                                   "WHERE freeze_id=?", (f["id"],)):
                    c.execute("DELETE FROM freeze_targets WHERE freeze_id=? AND "
                              "target_type=? AND target_id=?",
                              (f["id"], t["target_type"], t["target_id"]))
                    self._refresh_target(c, t["target_type"], t["target_id"], released)
            c.execute(f"DELETE FROM freezes WHERE id IN ({','.join('?' * len(ids))})",
                      ids)
            self._event(c, "unfreeze",
                        f"{actor} 解封冻结单 {', '.join('#'+str(i) for i in ids)}："
                        f"托盘 {len(released['trays'])}、批次 {len(released['batches'])}、"
                        f"余料 {len(released['remainders'])}", "info", "freeze",
                        ids[0])
            released["freeze_ids"] = ids
            return released

    def _refresh_target(self, c, ttype: str, tid: int, released: dict):
        """目标若无任何在效冻结单则恢复。"""
        other = c.execute(
            "SELECT 1 FROM freeze_targets WHERE target_type=? AND target_id=? LIMIT 1",
            (ttype, tid)).fetchone()
        if other:
            return
        if ttype == "tray":
            t = c.execute("SELECT status FROM trays WHERE id=?", (tid,)).fetchone()
            if t and t["status"] == "frozen":
                c.execute("UPDATE trays SET status='in_stock',frozen=0,"
                          "freeze_reason=NULL,frozen_at=NULL WHERE id=?", (tid,))
                released["trays"].append(tid)
        elif ttype == "batch":
            c.execute("UPDATE batches SET frozen=0,freeze_reason=NULL WHERE id=? "
                      "AND frozen=1", (tid,))
            released["batches"].append(tid)
        elif ttype == "remainder":
            c.execute("UPDATE remainders SET frozen=0,freeze_reason=NULL WHERE id=?",
                      (tid,))
            released["remainders"].append(tid)

    def unfreeze_target(self, target_type: str, target_id: int, *,
                        role: str = ROLE_ADMIN, actor: str = "市场管理员") -> dict:
        """管理员对单个目标解封（移除其全部冻结约束）。"""
        self._assert_admin(role)
        with tx(self.conn()) as c:
            ids = [r["freeze_id"] for r in c.execute(
                "SELECT DISTINCT freeze_id FROM freeze_targets WHERE target_type=? "
                "AND target_id=?", (target_type, target_id))]
            c.execute("DELETE FROM freeze_targets WHERE target_type=? AND target_id=?",
                      (target_type, target_id))
            released = {"batches": [], "trays": [], "remainders": []}
            self._refresh_target(c, target_type, target_id, released)
            # 清理空冻结单
            for fid in ids:
                n = c.execute("SELECT COUNT(*) k FROM freeze_targets WHERE freeze_id=?",
                              (fid,)).fetchone()["k"]
                if n == 0:
                    c.execute("DELETE FROM freezes WHERE id=?", (fid,))
            self._event(c, "unfreeze", f"{actor} 手动解封 {target_type}#{target_id}",
                        "info", "freeze", target_id)
            released["freeze_ids"] = ids
            return released

    # =====================================================================
    # 进程内扫描：票据到期 / 冷藏超温 / 冻结范围
    # =====================================================================
    def scan_cert_expiry(self) -> dict:
        now = _now()
        expired, frozen = [], 0
        with self.conn() as c:
            rows = c.execute(
                "SELECT id,cert_no,valid_until FROM certs WHERE status='valid'").fetchall()
        for r in rows:
            vu = datetime.strptime(r["valid_until"], "%Y-%m-%d %H:%M:%S")
            if now > vu:
                with tx(self.conn()) as c:
                    c.execute("UPDATE certs SET status='expired' WHERE id=?", (r["id"],))
                    batch_ids = [x["id"] for x in c.execute(
                        "SELECT id FROM batches WHERE cert_id=? AND status='accepted'",
                        (r["id"],))]
                    if batch_ids:
                        res = self._freeze(c, "cert", r["id"],
                                           f"检疫票 {r['cert_no']} 超过有效期 "
                                           f"({r['valid_until']})，在库未售品冻结",
                                           "系统扫描", batch_ids=batch_ids)
                        frozen += len(res["trays"])
                    self._event(c, "cert_expired",
                                f"扫描发现检疫票 {r['cert_no']} 到期，"
                                f"冻结未售托盘 {frozen} 个", "warn", "cert", r["id"])
                expired.append(r["cert_no"])
        return {"expired": expired, "frozen_trays": frozen}

    def scan_temps(self) -> dict:
        """全柜测温；超温格位内在批次数及其未售链冻结，恢复正常仅记录，解封由管理员执行。"""
        alerts = []
        with self.conn() as c:
            cells = c.execute("SELECT * FROM cells").fetchall()
        for cell in cells:
            r = self.probe.read(cell["code"])
            with tx(self.conn()) as c:
                c.execute("UPDATE cells SET temp=?,temp_ok=?,temp_at=? WHERE id=?",
                          (r.temp, 1 if r.ok else 0, _fmt(_now()), cell["id"]))
                if not r.ok:
                    active = [x["batch_id"] for x in c.execute(
                        "SELECT DISTINCT batch_id FROM cell_occupancy "
                        "WHERE cell_id=? AND cleared_at IS NULL "
                        "AND weight_in-weight_out > 0.001", (cell["id"],))]
                    existing = c.execute(
                        "SELECT 1 FROM freezes WHERE scope_type='temp' AND ref_id=? LIMIT 1",
                        (cell["id"],)).fetchone()
                    if active and not existing:
                        res = self._freeze(
                            c, "temp", cell["id"],
                            f"冷藏超温：{cell['code']} 测得 {r.temp}℃（允许 "
                            f"{config.COLD_TEMP_MIN}~{config.COLD_TEMP_MAX}℃），"
                            f"在柜批次链冻结", "系统扫描", batch_ids=active)
                        self._event(c, "temp",
                                    f"扫描发现 {cell['code']} 超温 {r.temp}℃，"
                                    f"冻结 {len(res['trays'])} 个未售托盘",
                                    "error", "cell", cell["id"])
                    alerts.append({"cell": cell["code"], "temp": r.temp,
                                   "batches": active})
        return {"alerts": alerts}

    def scan_freeze_scope(self) -> dict:
        """冻结范围巡检：统计每张冻结单当前覆盖目标，供管理员核对传播是否完整。"""
        with self.conn() as c:
            out = []
            for f in c.execute("SELECT * FROM freezes ORDER BY id"):
                counts = {"batch": 0, "tray": 0, "remainder": 0}
                for t in c.execute("SELECT target_type, COUNT(*) k FROM freeze_targets "
                                   "WHERE freeze_id=? GROUP BY target_type", (f["id"],)):
                    counts[t["target_type"]] = t["k"]
                out.append({"freeze_id": f["id"], "scope_type": f["scope_type"],
                            "reason": f["reason"], "issued_by": f["issued_by"],
                            "counts": counts})
            return {"freezes": out}

    # ---------- 格位视图 ----------
    def cells_view(self, stall_code: Optional[str] = None):
        with self.conn() as c:
            cells = [dict(r) for r in c.execute("SELECT * FROM cells ORDER BY code")]
            for cell in cells:
                cell["occupancy"] = [dict(r) for r in c.execute(
                    "SELECT o.*, b.batch_no, b.category, s.code stall_code, "
                    "(o.weight_in-o.weight_out) live_weight "
                    "FROM cell_occupancy o JOIN batches b ON b.id=o.batch_id "
                    "JOIN stalls s ON s.id=b.stall_id "
                    "WHERE o.cell_id=? AND o.cleared_at IS NULL "
                    + ("AND s.code=? " if stall_code else "")
                    + "ORDER BY o.id",
                    (cell["id"], stall_code) if stall_code else (cell["id"],))]
            return cells

    # ---------- 工具 ----------
    def _chain_text(self, c, t: sqlite3.Row) -> str:
        b = c.execute("SELECT batch_no FROM batches WHERE id=?",
                      (t["batch_id"],)).fetchone()
        return b["batch_no"] if b else str(t["batch_id"])

    def _maybe_empty(self, c, b: sqlite3.Row):
        """批次余量与在架托盘均清空后置 empty。"""
        if self.remaining_weight(c, b["id"]) <= 1e-3:
            alive = c.execute(
                "SELECT COUNT(*) k FROM trays WHERE batch_id=? "
                "AND status IN ('in_stock','frozen')", (b["id"],)).fetchone()["k"]
            pend = c.execute(
                "SELECT COUNT(*) k FROM remainders WHERE batch_id=? AND status='pending'",
                (b["id"],)).fetchone()["k"]
            if alive == 0 and pend == 0:
                c.execute("UPDATE batches SET status='empty' WHERE id=?", (b["id"],))
                c.execute(
                    "UPDATE cell_occupancy SET cleared_at=? WHERE batch_id=? "
                    "AND cleared_at IS NULL", (_fmt(_now()), b["id"]))

    def _require_stall_by_code(self, code: str) -> sqlite3.Row:
        with self.conn() as c:
            r = c.execute("SELECT * FROM stalls WHERE code=?", (code,)).fetchone()
        if not r:
            raise NotFound(f"摊位 {code} 不存在")
        return r

    @staticmethod
    def _assert_admin(role: str):
        if role != ROLE_ADMIN:
            raise AccessDenied("仅市场管理员可执行该操作")

    @staticmethod
    def _assert_stall(role: str, actor_stall_code: Optional[str], resource_code: str):
        if role == ROLE_ADMIN:
            return
        if role != ROLE_STALL or not actor_stall_code:
            raise AccessDenied("摊主操作需携带摊位代号")
        if actor_stall_code != resource_code:
            raise AccessDenied(
                f"摊位隔离：{actor_stall_code} 无权操作 {resource_code} 的批次或托盘")
