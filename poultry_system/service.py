"""核心领域服务：验票放行、重量链路、分割/托盘/余料/废弃、抽查、撤销、召回、冻结传播。

角色边界
========
* stall_owner 摊主：登记到货、分割、销售、余料回收、废弃、标签脱落报告；只能操作本摊位。
* market_admin 市场管理员：放行争议处理、抽查、检疫票撤销、召回、冻结/解封。

重量单位统一为克（int）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional

from . import db as dbmod

# 验票容差：差异超过 max(200g, 票面 0.5%) 即记称重差异
WEIGH_TOL_G = 200
WEIGH_TOL_RATIO = 0.005
# 单批废弃累计超过到货 10% 或单次超过 5%，记废弃重量异常
WASTE_BATCH_RATIO = 0.10
WASTE_SINGLE_RATIO = 0.05


class BusinessError(Exception):
    """业务规则冲突（重量不足、重复票、冻结阻断等）。"""


class AuthzError(Exception):
    """角色或摊位越权。"""


class _DuplicateTicket(Exception):
    """内部：事务内检出票号重复，触发回滚后在新事务补登异常单。"""


class _MixedBatch(Exception):
    """内部：事务内检出跨批次混放，回滚后在新事务补登异常单。"""
    def __init__(self, label, other_batch_no, stall_id, location_id):
        super().__init__(label)
        self.label = label
        self.other_batch_no = other_batch_no
        self.stall_id = stall_id
        self.location_id = location_id


def kg(g: Optional[int]) -> Optional[float]:
    return None if g is None else round(g / 1000.0, 3)


# --------------------------------------------------------------------------- 基础
class MarketService:
    def __init__(self, conn: sqlite3.Connection, scale=None, thermo=None,
                 now_func: Callable[[], datetime] = datetime.now):
        self.c = conn
        self.scale = scale
        self.thermo = thermo
        self.now = now_func

    def ts(self) -> str:
        return self.now().isoformat(timespec="seconds")

    def _one(self, sql: str, args=()):
        row = self.c.execute(sql, args).fetchone()
        return row

    def _all(self, sql: str, args=()):
        return self.c.execute(sql, args).fetchall()

    # ------------------------------------------------------------- 主数据（种子/管理）
    def create_user(self, username, display_name, role):
        cur = self.c.execute(
            "INSERT INTO users(username,display_name,role) VALUES(?,?,?)",
            (username, display_name, role))
        return cur.lastrowid

    def create_stall(self, code, name, owner_username, zone="禽肉区"):
        u = self._one("SELECT id FROM users WHERE username=?", (owner_username,))
        cur = self.c.execute(
            "INSERT INTO stalls(code,name,owner_user_id,zone) VALUES(?,?,?,?)",
            (code, name, u["id"], zone))
        return cur.lastrowid

    def create_supplier(self, code, name, region="虚构产地"):
        cur = self.c.execute(
            "INSERT INTO suppliers(code,name,region) VALUES(?,?,?)",
            (code, name, region))
        return cur.lastrowid

    def create_cabinet(self, code, name, max_temp_c=4.0):
        cur = self.c.execute(
            "INSERT INTO cold_cabinets(code,name,max_temp_c) VALUES(?,?,?)",
            (code, name, max_temp_c))
        return cur.lastrowid

    def create_locations(self, cabinet_code, shelves=("1层", "2层"), slots=("A", "B", "C")):
        cab = self._one("SELECT id FROM cold_cabinets WHERE code=?", (cabinet_code,))
        ids = []
        for sh in shelves:
            for sl in slots:
                cur = self.c.execute(
                    "INSERT INTO cold_locations(cabinet_id,shelf,slot_code) VALUES(?,?,?)",
                    (cab["id"], sh, sl))
                ids.append(cur.lastrowid)
        return ids

    def get_stall(self, code):
        return self._one("SELECT * FROM stalls WHERE code=?", (code,))

    def get_supplier(self, code):
        return self._one("SELECT * FROM suppliers WHERE code=?", (code,))

    def get_location(self, cabinet_code, shelf, slot):
        return self._one(
            """SELECT l.*, cc.code cabinet_code, cc.name cabinet_name, cc.max_temp_c
               FROM cold_locations l JOIN cold_cabinets cc ON cc.id=l.cabinet_id
               WHERE cc.code=? AND shelf=? AND slot_code=?""",
            (cabinet_code, shelf, slot))

    # ------------------------------------------------------------- 权限
    def _actor(self, username) -> sqlite3.Row:
        u = self._one("SELECT * FROM users WHERE username=?", (username,))
        if not u:
            raise AuthzError(f"用户不存在：{username}")
        return u

    def _require_admin(self, actor_name):
        u = self._actor(actor_name)
        if u["role"] != "market_admin":
            raise AuthzError("仅市场管理员可执行冻结/解封/抽查/召回/撤销")
        return u

    def _stall_row(self, stall_id) -> sqlite3.Row:
        row = self._one("SELECT * FROM stalls WHERE id=?", (stall_id,))
        if not row:
            raise BusinessError("摊位不存在")
        return row

    def _require_stall_owner(self, actor_name, stall_id):
        """摊主只能操作自己摊位；管理员可代操作。返回 (user, stall)。"""
        u = self._actor(actor_name)
        stall = self._stall_row(stall_id)
        if u["role"] == "market_admin":
            return u, stall
        if u["id"] != stall["owner_user_id"]:
            raise AuthzError(f"摊位隔离：{actor_name} 无权操作摊位 {stall['code']}")
        return u, stall

    # ------------------------------------------------------------- 到货 + 验票
    def arrive(self, actor_name: str, *, stall_code: str, supplier_code: str,
               ticket_no: str, category: str, ticket_category: str,
               ticket_weight_g: int, declared_weight_g: int,
               valid_from: str, valid_until: str,
               ticket_image: str = "", note: str = "") -> dict:
        """登记到货并验票。返回 {'batch_no', 'status', 'incidents':[...]}。

        核验：票号唯一、检疫票有效期、票货（品类/重量）相符、过磅称重差异。
        有称重差异的批次挂起待管理员处理；票货不符直接拒收；其余待入柜放行。
        """
        stall = self.get_stall(stall_code)
        supplier = self.get_supplier(supplier_code)
        if not stall or not supplier:
            raise BusinessError("摊位或供应商代号不存在")
        u = self._actor(actor_name)
        if u["role"] == "stall_owner" and u["id"] != stall["owner_user_id"]:
            raise AuthzError(f"摊位隔离：不能为他摊登记到货")

        # 称重适配器回执（过磅实测）
        receipt_no, measured_g = "", declared_weight_g
        if self.scale is not None:
            receipt = self.scale.weigh_arrival(ticket_no, declared_weight_g)
            receipt_no = receipt["ref_no"]
            measured_g = receipt["measured_g"]
            self._save_receipt("scale", receipt_no, json.dumps(receipt, ensure_ascii=False))

        result = {"batch_no": None, "status": "pending", "incidents": [],
                  "measured_g": measured_g, "ticket_weight_g": ticket_weight_g}

        try:
            with dbmod.transaction(self.c):
                # 1) 票号重复（UNIQUE 约束兜底并发；此处给可读错误）
                dup = self._one("SELECT id FROM quarantine_tickets WHERE ticket_no=?",
                                (ticket_no,))
                if dup:
                    result["status"] = "rejected"
                    raise _DuplicateTicket(ticket_no)

                # 检疫票入库（active）
                cur = self.c.execute(
                    """INSERT INTO quarantine_tickets
                       (ticket_no,supplier_id,category,gross_weight_g,issued_at,
                        valid_from,valid_until,status,ticket_image)
                       VALUES(?,?,?,?,?,?,?, 'active',?)""",
                    (ticket_no, supplier["id"], ticket_category, ticket_weight_g,
                     valid_from, valid_from, valid_until, ticket_image))
                ticket_id = cur.lastrowid

                seq = self._seq("arrival")
                batch_no = f"B{self.now():%Y%m%d}-{seq:03d}"
                # 以过磅实测重量作为入库锚点（重量守恒基准）
                cur = self.c.execute(
                    """INSERT INTO batches
                       (batch_no,stall_id,supplier_id,ticket_id,category,
                        declared_weight_g,ticket_weight_g,inspect_status,arrived_at,inspect_note)
                       VALUES(?,?,?,?,?,?,?, 'pending',?,?)""",
                    (batch_no, stall["id"], supplier["id"], ticket_id, category,
                     measured_g, ticket_weight_g, self.ts(), note or None))
                batch_id = cur.lastrowid
                result["batch_no"] = batch_no

                # 2) 票货不符：品类不一致
                if category != ticket_category:
                    self._reject_batch(batch_id,
                        f"票货不符：到货品类“{category}”与票面品类“{ticket_category}”不一致")
                    self._incident("ticket_mismatch", "batch", batch_id, stall["id"],
                                   f"批次 {batch_no} 品类与检疫票 {ticket_no} 不符，拒收",
                                   detail_extra={"ticket": ticket_no})
                    result["status"] = "rejected"
                    result["incidents"].append("ticket_mismatch")

                # 3) 票货重量不符
                tol = max(WEIGH_TOL_G, int(ticket_weight_g * WEIGH_TOL_RATIO))
                diff_ticket = measured_g - ticket_weight_g
                if abs(diff_ticket) > tol:
                    msg = (f"票货不符：过磅 {kg(measured_g)}kg 与票面 {kg(ticket_weight_g)}kg "
                           f"差异 {diff_ticket:+d}g，超容差 ±{tol}g")
                    if result["status"] != "rejected":
                        self._reject_batch(batch_id, msg)
                        result["status"] = "rejected"
                    self._incident("ticket_mismatch", "batch", batch_id, stall["id"],
                                   f"批次 {batch_no} {msg}",
                                   detail_extra={"ticket": ticket_no, "diff_g": diff_ticket})
                    result["incidents"].append("ticket_mismatch")

                # 4) 称重差异：报称 vs 过磅
                diff_scale = measured_g - declared_weight_g
                if abs(diff_scale) > WEIGH_TOL_G:
                    self._incident("weight_discrepancy", "batch", batch_id, stall["id"],
                                   f"批次 {batch_no} 报称 {kg(declared_weight_g)}kg 与过磅 "
                                   f"{kg(measured_g)}kg 差异 {diff_scale:+d}g，暂挂待管理员确认",
                                   detail_extra={"declared_g": declared_weight_g,
                                                 "measured_g": measured_g,
                                                 "scale_receipt": receipt_no})
                    self.c.execute("UPDATE batches SET inspect_note=? WHERE id=?",
                                   (f"称重差异待确认（回执 {receipt_no}）", batch_id))
                    result["incidents"].append("weight_discrepancy")

                # 5) 检疫票有效期
                expired = valid_until < self.ts()
                early = valid_from > self.ts()
                if expired or early:
                    why = "已过期" if expired else "尚未生效"
                    self._reject_batch(batch_id,
                                       f"检疫票 {ticket_no} {why}（有效期至 {valid_until}）")
                    result["status"] = "rejected"
                    result["incidents"].append("ticket_invalid_period")
        except _DuplicateTicket:
            # 事务已回滚；在新事务补登异常单，避免重复登记痕迹丢失
            with dbmod.transaction(self.c):
                dup = self._one("SELECT id, supplier_id FROM quarantine_tickets "
                                "WHERE ticket_no=?", (ticket_no,))
                self._incident("duplicate_ticket", "ticket", dup["id"], stall["id"],
                               f"检疫票号 {ticket_no} 重复登记，系统拒绝重复入库",
                               detail_extra={"supplier": supplier_code})
            raise BusinessError(f"票号重复：{ticket_no}（已生成异常单，到货拒收）")
        except sqlite3.IntegrityError as e:
            if "quarantine_tickets.ticket_no" not in str(e):
                raise
            # 并发重复票兜底：补登异常单（独立事务）后拒绝
            with dbmod.transaction(self.c):
                dup = self._one("SELECT id FROM quarantine_tickets WHERE ticket_no=?",
                                (ticket_no,))
                self._incident("duplicate_ticket", "ticket", dup["id"], stall["id"],
                               f"检疫票号 {ticket_no} 并发重复登记，UNIQUE 约束拦截",
                               detail_extra={"supplier": supplier_code})
            raise BusinessError(f"票号重复：{ticket_no}（并发拦截，已生成异常单）")

        return result

    def _reject_batch(self, batch_id, note):
        self.c.execute(
            "UPDATE batches SET inspect_status='rejected', inspect_note=? WHERE id=?",
            (note, batch_id))

    def resolve_weight_discrepancy(self, actor_name, batch_no, accept: bool, note=""):
        """管理员处理称重差异：accept=True 以过磅重量为准放行，False 拒收。"""
        self._require_admin(actor_name)
        with dbmod.transaction(self.c):
            b = self._one("SELECT * FROM batches WHERE batch_no=?", (batch_no,))
            if not b:
                raise BusinessError("批次不存在")
            inc = self._one(
                """SELECT * FROM incidents WHERE kind='weight_discrepancy'
                   AND ref_type='batch' AND ref_id=? AND handled=0""", (b["id"],))
            if accept:
                self.c.execute(
                    "UPDATE batches SET inspect_status='pending', inspect_note=? WHERE id=?",
                    (f"称重差异已管理员确认，以过磅 {kg(b['declared_weight_g'])}kg 为准。{note}",
                     b["id"]))
                if inc:
                    self._resolve_incident_row(inc, f"管理员确认过磅重量。{note}")
                return {"status": "pending", "batch_no": batch_no}
            self._reject_batch(b["id"], f"称重差异拒收。{note}")
            if inc:
                self._resolve_incident_row(inc, f"拒收。{note}")
            return {"status": "rejected", "batch_no": batch_no}

    def release_to_cabinet(self, actor_name, batch_no, cabinet_code, shelf, slot) -> dict:
        """放行并入冷藏格位。格位被占用即跨批次混放，拒绝并记异常。"""
        try:
            with dbmod.transaction(self.c):
                b = self._one("SELECT * FROM batches WHERE batch_no=?", (batch_no,))
                if not b:
                    raise BusinessError("批次不存在")
                # 摊主可申请入柜（本摊），管理员亦可
                self._require_stall_owner(actor_name, b["stall_id"])
                if b["inspect_status"] == "rejected":
                    raise BusinessError(f"批次已拒收：{b['inspect_note']}")
                if b["inspect_status"] == "released":
                    raise BusinessError("批次已放行入柜")

                t = self._one("SELECT * FROM quarantine_tickets WHERE id=?", (b["ticket_id"],))
                if t["status"] != "active":
                    raise BusinessError(f"检疫票状态 {t['status']}，不得放行")
                if t["valid_until"] < self.ts():
                    raise BusinessError("检疫票已过有效期，不得放行")
                blocking = self._all(
                    """SELECT id FROM incidents
                       WHERE ref_type='batch' AND ref_id=? AND handled=0
                         AND kind IN ('weight_discrepancy')""", (b["id"],))
                if blocking:
                    raise BusinessError("存在未处理的称重差异，需管理员确认后方可放行")

                loc = self.get_location(cabinet_code, shelf, slot)
                if not loc:
                    raise BusinessError("冷藏格位不存在")
                occ = self._one("SELECT * FROM location_occupancy WHERE location_id=?",
                                (loc["id"],))
                if occ:
                    other = self._one("SELECT batch_no FROM batches WHERE id=?",
                                      (occ["batch_id"],))
                    raise _MixedBatch(
                        f"{cabinet_code}-{shelf}{slot}", other["batch_no"],
                        b["stall_id"], loc["id"])

                self.c.execute(
                    """UPDATE batches SET inspect_status='released', released_at=?,
                       location_id=? WHERE id=?""",
                    (self.ts(), loc["id"], b["id"]))
                self.c.execute(
                    "INSERT INTO location_occupancy(location_id,batch_id,occupied_since) VALUES(?,?,?)",
                    (loc["id"], b["id"], self.ts()))
                return {"batch_no": batch_no,
                        "location": f"{cabinet_code}-{shelf}{slot}", "status": "released"}
        except _MixedBatch as m:
            # 混放判定独立提交，避免随业务回滚丢失
            with dbmod.transaction(self.c):
                self._incident(
                    "mixed_batch", "location", m.location_id, m.stall_id,
                    f"批次 {batch_no} 欲入格位 {m.label}，该格位仍由批次 "
                    f"{m.other_batch_no} 占用，判定跨批次混放，拒绝入柜")
            raise BusinessError(
                f"跨批次混放：格位 {m.label} 已被 {m.other_batch_no} 占用（已登记异常）")

    def vacate_location(self, actor_name, batch_no):
        """批次清空后腾出格位。"""
        with dbmod.transaction(self.c):
            b = self._one("SELECT * FROM batches WHERE batch_no=?", (batch_no,))
            if not b:
                raise BusinessError("批次不存在")
            self._require_stall_owner(actor_name, b["stall_id"])
            if b["location_id"]:
                self.c.execute("DELETE FROM location_occupancy WHERE batch_id=?", (b["id"],))
                self.c.execute("UPDATE batches SET location_id=NULL WHERE id=?", (b["id"],))
            return {"batch_no": batch_no, "vacated": True}

    # ------------------------------------------------------------- 分割
    def batch_remaining_g(self, batch_id) -> int:
        row = self._one(
            """SELECT
                 b.declared_weight_g
                 - COALESCE((SELECT SUM(weight_g) FROM cuts
                             WHERE batch_id=b.id AND recut=0),0)
                 - COALESCE((SELECT SUM(weight_g) FROM waste_logs
                              WHERE batch_id=b.id AND source_type='batch'),0) AS remaining
               FROM batches b WHERE b.id=?""", (batch_id,))
        return row["remaining"]

    def register_cut(self, actor_name, *, batch_no, weight_g, trays, loss_g=0,
                     operator=None) -> dict:
        """登记分割：从原批次扣减重量，一次生成若干托盘标签。

        weight_g == 各托盘重量之和 + loss_g（分割损耗，立即入废弃台账）。
        并发提交以批次余量为准：BEGIN IMMEDIATE 下重读余量，不足则整笔失败。
        trays: [(product_name, weight_g), ...]
        """
        if weight_g <= 0:
            raise BusinessError("分割重量必须为正")
        tray_total = sum(int(w) for _, w in trays)
        if tray_total + int(loss_g) != weight_g:
            raise BusinessError(
                f"重量不闭合：分割 {weight_g}g ≠ 托盘 {tray_total}g + 损耗 {loss_g}g")
        if not trays:
            raise BusinessError("至少生成一个托盘")

        receipt_no = ""
        if self.scale is not None:
            r = self.scale.weigh_cut(batch_no, weight_g)
            receipt_no = r["ref_no"]
            if abs(r["measured_g"] - weight_g) > WEIGH_TOL_G:
                raise BusinessError(
                    f"秤重回执与申报不符：{r['measured_g']}g vs {weight_g}g（请重新称重）")
            self._save_receipt("scale", receipt_no, json.dumps(r, ensure_ascii=False))

        with dbmod.transaction(self.c):
            b = self._one("SELECT * FROM batches WHERE batch_no=?", (batch_no,))
            if not b:
                raise BusinessError("批次不存在")
            u, stall = self._require_stall_owner(actor_name, b["stall_id"])
            if b["inspect_status"] != "released":
                raise BusinessError("批次未放行，不得分割")
            if b["frozen"]:
                raise BusinessError(f"批次已冻结：{b['freeze_reason']}")

            remaining = self.batch_remaining_g(b["id"])
            if weight_g > remaining:
                # 同一重量不能被重复分割 / 并发竞争失败
                raise BusinessError(
                    f"批次余量不足：申请分割 {weight_g}g，当前余量仅 {remaining}g"
                    "（并发提交以批次余量为准，本次失败）")

            cut_no = f"CUT-{b['batch_no']}-{self._seq('cut')}"
            cur = self.c.execute(
                """INSERT INTO cuts(cut_no,batch_id,stall_id,operator,weight_g,loss_g,recut,
                                    scale_ref,cut_at)
                   VALUES(?,?,?,?,?,?,0,?,?)""",
                (cut_no, b["id"], stall["id"], operator or u["display_name"],
                 weight_g, int(loss_g), receipt_no or None, self.ts()))
            cut_id = cur.lastrowid

            tray_nos = []
            for product, tw in trays:
                tray_no = f"T-{stall['code'].replace('-', '')}-{self._seq('tray'):04d}"
                self.c.execute(
                    """INSERT INTO trays
                       (tray_no,cut_id,batch_id,stall_id,product_name,weight_g,produced_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (tray_no, cut_id, b["id"], stall["id"], product, int(tw), self.ts()))
                tray_nos.append(tray_no)

            if loss_g:
                self._log_waste(stall["id"], b["id"], None, None, "cut",
                                int(loss_g), "分割损耗", operator or u["display_name"],
                                cut_id=cut_id, in_tx=True)

            return {"cut_no": cut_no, "batch_no": batch_no, "trays": tray_nos,
                    "weight_g": weight_g, "loss_g": loss_g,
                    "batch_remaining_g": self.batch_remaining_g(b["id"])}

    # ------------------------------------------------------------- 销售
    def _tray_remaining_g(self, t):
        sold = self._one("SELECT COALESCE(SUM(sold_weight_g),0) v FROM sales WHERE tray_id=?",
                         (t["id"],))["v"]
        waste = self._one(
            "SELECT COALESCE(SUM(weight_g),0) v FROM waste_logs WHERE tray_id=? AND source_type='tray'",
            (t["id"],))["v"]
        rec = self._one(
            "SELECT COALESCE(SUM(weight_g),0) v FROM remainders WHERE tray_id=?",
            (t["id"],))["v"]
        return t["weight_g"] - sold - waste - rec

    def sell_tray(self, actor_name, tray_no, weight_g):
        if weight_g <= 0:
            raise BusinessError("销售重量必须为正")
        with dbmod.transaction(self.c):
            t = self._one("SELECT * FROM trays WHERE tray_no=?", (tray_no,))
            if not t:
                raise BusinessError("托盘不存在")
            u, stall = self._require_stall_owner(actor_name, t["stall_id"])
            if t["frozen"]:
                raise BusinessError(f"托盘冻结中，禁止销售：{t['freeze_reason']}")
            if not t["label_intact"]:
                raise BusinessError("标签脱落待重贴，暂停销售")
            if t["status"] in ("sold", "waste"):
                raise BusinessError(f"托盘已{t['status']}")
            rem = self._tray_remaining_g(t)
            if weight_g > rem:
                raise BusinessError(f"托盘可售 {rem}g，申报销售 {weight_g}g")
            self.c.execute(
                "INSERT INTO sales(tray_id,sold_weight_g,sold_at,seller) VALUES(?,?,?,?)",
                (t["id"], weight_g, self.ts(), u["display_name"]))
            if weight_g == rem:
                self.c.execute("UPDATE trays SET status='sold' WHERE id=?", (t["id"],))
            return {"tray_no": tray_no, "sold_g": weight_g, "remaining_g": rem - weight_g}

    # ------------------------------------------------------------- 余料
    def recover_remainder(self, actor_name, tray_no, weight_g=None):
        """销售剩余回收为余料块，批次链不变。默认回收托盘全部剩余。"""
        with dbmod.transaction(self.c):
            t = self._one("SELECT * FROM trays WHERE tray_no=?", (tray_no,))
            if not t:
                raise BusinessError("托盘不存在")
            u, _ = self._require_stall_owner(actor_name, t["stall_id"])
            if t["frozen"]:
                raise BusinessError(f"托盘冻结中：{t['freeze_reason']}")
            rem = self._tray_remaining_g(t)
            if rem <= 0:
                raise BusinessError("托盘无剩余可回收")
            weight_g = rem if weight_g is None else int(weight_g)
            if weight_g != rem:
                raise BusinessError(f"余料回收须为托盘全部剩余 {rem}g（收到 {weight_g}g）")
            no = f"R-{t['tray_no'].lstrip('T-')}-{self._seq('rem')}"
            self.c.execute(
                """INSERT INTO remainders
                   (remainder_no,tray_id,batch_id,stall_id,weight_g,recovered_at,status)
                   VALUES(?,?,?,?,?,?,'held')""",
                (no, t["id"], t["batch_id"], t["stall_id"], weight_g, self.ts()))
            self.c.execute("UPDATE trays SET status='remainder' WHERE id=?", (t["id"],))
            rid = self._one("SELECT id FROM remainders WHERE remainder_no=?", (no,))["id"]
            if t["frozen"]:
                self._freeze("remainder", rid, t["freeze_reason"], u["display_name"])
            return {"remainder_no": no, "weight_g": weight_g}

    def recut_remainder(self, actor_name, *, remainder_no, trays, loss_g=0):
        """余料再托盘：从余料块扣减，生成新托盘，继续沿用原批次链。

        再托的 cut 行置 recut=1：重量来自余料块而非批次，不重复扣批次。
        损耗走 source_type='remainder' 废弃，保证余料块自身守恒。
        """
        tray_total = sum(int(w) for _, w in trays)
        weight_g = tray_total + int(loss_g)
        r0 = self._one("SELECT * FROM remainders WHERE remainder_no=?", (remainder_no,))
        if not r0:
            raise BusinessError("余料不存在")
        with dbmod.transaction(self.c):
            r = self._one("SELECT * FROM remainders WHERE id=?", (r0["id"],))
            u, stall = self._require_stall_owner(actor_name, r["stall_id"])
            if r["frozen"]:
                raise BusinessError(f"余料冻结中：{r['freeze_reason']}")
            used_waste = self._one(
                "SELECT COALESCE(SUM(weight_g),0) v FROM waste_logs "
                "WHERE remainder_id=? AND source_type='remainder'", (r["id"],))["v"]
            avail = r["weight_g"] - r["consumed_g"] - used_waste
            if weight_g > avail:
                raise BusinessError(f"余料余量 {avail}g，不足 {weight_g}g")
            b = self._one("SELECT * FROM batches WHERE id=?", (r["batch_id"],))
            new_cut_no = f"CUT-R{r['id']}-{self._seq('cut')}"
            cur = self.c.execute(
                """INSERT INTO cuts(cut_no,batch_id,stall_id,operator,weight_g,loss_g,recut,
                                    cut_at,note)
                   VALUES(?,?,?,?,?,?,1,?,?)""",
                (new_cut_no, b["id"], stall["id"], u["display_name"], weight_g, int(loss_g),
                 self.ts(), f"余料 {remainder_no} 再托盘"))
            cut_id = cur.lastrowid
            tray_nos = []
            for product, tw in trays:
                tray_no = f"T-{stall['code'].replace('-','')}-{self._seq('tray'):04d}"
                self.c.execute(
                    """INSERT INTO trays
                       (tray_no,cut_id,batch_id,stall_id,product_name,weight_g,produced_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (tray_no, cut_id, b["id"], stall["id"], product, int(tw), self.ts()))
                tid = self._one("SELECT id FROM trays WHERE tray_no=?", (tray_no,))["id"]
                self.c.execute(
                    "INSERT INTO remainder_trays(remainder_id,tray_id,weight_g,created_at) "
                    "VALUES(?,?,?,?)", (r["id"], tid, int(tw), self.ts()))
                tray_nos.append(tray_no)
            self.c.execute(
                "UPDATE remainders SET consumed_g=consumed_g+? WHERE id=?",
                (tray_total, r["id"]))
            if loss_g:
                self._log_waste(stall["id"], b["id"], None, r["id"], "remainder",
                                int(loss_g), f"余料 {remainder_no} 再托损耗",
                                u["display_name"], cut_id=cut_id)
            # 结存为 0 则结状态
            rr = self._one("SELECT * FROM remainders WHERE id=?", (r["id"],))
            if rr["consumed_g"] + self._one(
                    "SELECT COALESCE(SUM(weight_g),0) v FROM waste_logs "
                    "WHERE remainder_id=? AND source_type='remainder'", (r["id"],))["v"] \
                    >= rr["weight_g"]:
                self.c.execute("UPDATE remainders SET status='consumed' WHERE id=?", (r["id"],))
            return {"cut_no": new_cut_no, "trays": tray_nos, "from_remainder": remainder_no}

    # ------------------------------------------------------------- 废弃物
    def log_waste(self, actor_name, *, source_type, ref_no, weight_g, reason):
        if weight_g <= 0:
            raise BusinessError("废弃重量必须为正")
        with dbmod.transaction(self.c):
            batch_id = tray_id = rem_id = stall_id = None
            if source_type == "batch":
                row = self._one("SELECT * FROM batches WHERE batch_no=?", (ref_no,))
                if not row:
                    raise BusinessError("批次不存在")
                batch_id, stall_id = row["id"], row["stall_id"]
            elif source_type == "tray":
                row = self._one("SELECT * FROM trays WHERE tray_no=?", (ref_no,))
                if not row:
                    raise BusinessError("托盘不存在")
                tray_id, batch_id, stall_id = row["id"], row["batch_id"], row["stall_id"]
            elif source_type == "remainder":
                row = self._one("SELECT * FROM remainders WHERE remainder_no=?", (ref_no,))
                if not row:
                    raise BusinessError("余料不存在")
                rem_id, batch_id, stall_id = row["id"], row["batch_id"], row["stall_id"]
            else:
                raise BusinessError("来源类型非法")
            u, _ = self._require_stall_owner(actor_name, stall_id)

            # 冻结/召回/检疫在查货物不得自行废弃，须先按流程解封
            if source_type == "batch":
                brow = self._one("SELECT frozen, freeze_reason FROM batches WHERE id=?",
                                 (batch_id,))
                if brow["frozen"]:
                    raise BusinessError(f"批次冻结中，禁止废弃：{brow['freeze_reason']}")
            elif source_type == "tray":
                trow = self._one("SELECT frozen, freeze_reason FROM trays WHERE id=?",
                                 (tray_id,))
                if trow["frozen"]:
                    raise BusinessError(f"托盘冻结/召回中，禁止废弃：{trow['freeze_reason']}")
            else:
                rrow = self._one("SELECT frozen, freeze_reason FROM remainders WHERE id=?",
                                 (rem_id,))
                if rrow["frozen"]:
                    raise BusinessError(f"余料冻结中，禁止废弃：{rrow['freeze_reason']}")

            # 容量校验
            if source_type == "batch":
                avail = self.batch_remaining_g(batch_id)
                if weight_g > avail:
                    raise BusinessError(f"批次余量 {avail}g，不足废弃 {weight_g}g")
            elif source_type == "tray":
                t = self._one("SELECT * FROM trays WHERE id=?", (tray_id,))
                avail = self._tray_remaining_g(t)
                if weight_g > avail:
                    raise BusinessError(f"托盘余量 {avail}g，不足废弃 {weight_g}g")
            else:
                r = self._one("SELECT * FROM remainders WHERE id=?", (rem_id,))
                used = self._one(
                    "SELECT COALESCE(SUM(weight_g),0) v FROM waste_logs WHERE remainder_id=? AND source_type='remainder'",
                    (rem_id,))["v"]
                avail = r["weight_g"] - r["consumed_g"] - used
                if weight_g > avail:
                    raise BusinessError(f"余料余量 {avail}g，不足废弃 {weight_g}g")

            out = self._log_waste(stall_id, batch_id, tray_id, rem_id, source_type,
                                  weight_g, reason, u["display_name"])
            if source_type == "tray" and self._tray_remaining_g(
                    self._one("SELECT * FROM trays WHERE id=?", (tray_id,))) == 0:
                self.c.execute("UPDATE trays SET status='waste' WHERE id=?", (tray_id,))
            if source_type == "remainder":
                r = self._one("SELECT * FROM remainders WHERE id=?", (rem_id,))
                used = self._one(
                    "SELECT COALESCE(SUM(weight_g),0) v FROM waste_logs WHERE remainder_id=? AND source_type='remainder'",
                    (rem_id,))["v"]
                if r["consumed_g"] + used >= r["weight_g"]:
                    self.c.execute("UPDATE remainders SET status='waste' WHERE id=?", (rem_id,))
            return out

    def _log_waste(self, stall_id, batch_id, tray_id, rem_id, source_type,
                   weight_g, reason, who, cut_id=None, in_tx=True):
        b = self._one("SELECT declared_weight_g FROM batches WHERE id=?", (batch_id,))
        abnormal = 0
        note = None
        if b:
            single_ratio = weight_g / b["declared_weight_g"]
            cum = self._one(
                "SELECT COALESCE(SUM(weight_g),0) v FROM waste_logs WHERE batch_id=?",
                (batch_id,))["v"]
            cum_ratio = (cum + weight_g) / b["declared_weight_g"]
            if single_ratio > WASTE_SINGLE_RATIO or cum_ratio > WASTE_BATCH_RATIO:
                abnormal = 1
                note = (f"废弃重量异常：单次占比 {single_ratio:.1%}，"
                        f"累计占比 {cum_ratio:.1%}（阈值单次 {WASTE_SINGLE_RATIO:.0%}/"
                        f"累计 {WASTE_BATCH_RATIO:.0%}），需市场管理员核查")
        no = f"W-{self._seq('waste'):05d}"
        self.c.execute(
            """INSERT INTO waste_logs
               (waste_no,stall_id,batch_id,tray_id,remainder_id,cut_id,source_type,
                weight_g,reason,abnormal,abnormal_note,logged_at,logged_by)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (no, stall_id, batch_id, tray_id, rem_id, cut_id, source_type, weight_g, reason,
             abnormal, note, self.ts(), who))
        if abnormal:
            self._incident("abnormal_waste", "waste",
                           self._one("SELECT id FROM waste_logs WHERE waste_no=?", (no,))["id"],
                           stall_id, f"{no} {note}")
        return {"waste_no": no, "weight_g": weight_g, "abnormal": bool(abnormal),
                "abnormal_note": note}

    # ------------------------------------------------------------- 标签
    def report_label_lost(self, actor_name, tray_no, note=""):
        """摊主报告标签脱落：托盘暂停销售（非管理员冻结），待管理员核验重贴。"""
        with dbmod.transaction(self.c):
            t = self._one("SELECT * FROM trays WHERE tray_no=?", (tray_no,))
            if not t:
                raise BusinessError("托盘不存在")
            self._require_stall_owner(actor_name, t["stall_id"])
            self.c.execute(
                "UPDATE trays SET label_intact=0, label_note=? WHERE id=?",
                (f"标签脱落：{note}" or "标签脱落", t["id"]))
            self._incident("label_lost", "tray", t["id"], t["stall_id"],
                           f"托盘 {tray_no} 标签脱落，暂停销售，待管理员核验重贴。{note}")
            return {"tray_no": tray_no, "label_intact": False}

    def relabel(self, actor_name, tray_no, note=""):
        """管理员核验后重贴标签，恢复销售资格。"""
        self._require_admin(actor_name)
        with dbmod.transaction(self.c):
            t = self._one("SELECT * FROM trays WHERE tray_no=?", (tray_no,))
            if not t:
                raise BusinessError("托盘不存在")
            self.c.execute(
                "UPDATE trays SET label_intact=1, label_note=? WHERE id=?",
                (f"管理员核验重贴：{note}", t["id"]))
            inc = self._one(
                """SELECT * FROM incidents WHERE kind='label_lost' AND ref_type='tray'
                   AND ref_id=? AND handled=0""", (t["id"],))
            if inc:
                self._resolve_incident_row(inc, f"已核验重贴。{note}")
            return {"tray_no": tray_no, "label_intact": True}

    # ------------------------------------------------------------- 抽查 / 撤销 / 召回
    def inspect(self, actor_name, *, target_type, ref_no, result, note):
        """市场抽查。target_type=batch|tray；不合格立即冻结相关未售托盘。"""
        admin = self._require_admin(actor_name)
        with dbmod.transaction(self.c):
            batch_id = tray_id = stall_id = None
            if target_type == "batch":
                b = self._one("SELECT * FROM batches WHERE batch_no=?", (ref_no,))
                if not b:
                    raise BusinessError("批次不存在")
                batch_id, stall_id = b["id"], b["stall_id"]
            else:
                t = self._one("SELECT * FROM trays WHERE tray_no=?", (ref_no,))
                if not t:
                    raise BusinessError("托盘不存在")
                tray_id, batch_id, stall_id = t["id"], t["batch_id"], t["stall_id"]
            no = f"INSP-{self._seq('insp'):04d}"
            self.c.execute(
                """INSERT INTO inspections
                   (inspect_no,batch_id,tray_id,stall_id,result,note,inspected_at,inspector)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (no, batch_id, tray_id, stall_id, result, note, self.ts(), admin["display_name"]))
            if result == "fail":
                if target_type == "batch":
                    reason = f"市场抽查不合格（{no}）：{note}，批次链未售部分立即冻结"
                    self._freeze_batch_chain(batch_id, reason, admin["display_name"],
                                             include_batch=True)
                    self._incident("inspection_fail", "batch", batch_id, stall_id, reason)
                else:
                    reason = f"市场抽查不合格（{no}）：{note}，该托盘立即冻结"
                    self._freeze("tray", tray_id, reason, admin["display_name"])
                    self._incident("inspection_fail", "tray", tray_id, stall_id, reason)
            return {"inspect_no": no, "result": result}

    def revoke_ticket(self, actor_name, ticket_no, reason):
        """检疫票撤销：该票所有批次的未售托盘/余料立即冻结。"""
        admin = self._require_admin(actor_name)
        with dbmod.transaction(self.c):
            t = self._one("SELECT * FROM quarantine_tickets WHERE ticket_no=?", (ticket_no,))
            if not t:
                raise BusinessError("检疫票不存在")
            self.c.execute(
                "UPDATE quarantine_tickets SET status='revoked', revoke_reason=? WHERE id=?",
                (reason, t["id"]))
            batches = self._all("SELECT * FROM batches WHERE ticket_id=?", (t["id"],))
            n = 0
            for b in batches:
                r = f"检疫票 {ticket_no} 已撤销（{reason}），批次 {b['batch_no']} 未售部分立即冻结"
                n += self._freeze_batch_chain(b["id"], r, admin["display_name"],
                                              include_batch=True)
                self._incident("ticket_revoked", "batch", b["id"], b["stall_id"], r)
            return {"ticket": ticket_no, "frozen_targets": n}

    def issue_recall(self, actor_name, *, scope, reason, batch_no=None, ticket_no=None,
                     tray_nos=None) -> dict:
        """召回：batch/ticket 范围或 partial 指定托盘；相关未售托盘立即冻结。"""
        admin = self._require_admin(actor_name)
        with dbmod.transaction(self.c):
            batch_id = ticket_id = None
            targets = []
            if scope == "partial":
                if not tray_nos:
                    raise BusinessError("部分召回必须指定托盘号")
                for tn in tray_nos:
                    t = self._one("SELECT * FROM trays WHERE tray_no=?", (tn,))
                    if not t:
                        raise BusinessError(f"托盘不存在：{tn}")
                    targets.append(t)
                batch_id = targets[0]["batch_id"]
            elif scope == "batch":
                b = self._one("SELECT * FROM batches WHERE batch_no=?", (batch_no,))
                if not b:
                    raise BusinessError("批次不存在")
                batch_id = b["id"]
                targets = self._all(
                    "SELECT * FROM trays WHERE batch_id=? AND status IN ('on_sale','remainder')",
                    (batch_id,))
            elif scope == "ticket":
                t = self._one("SELECT * FROM quarantine_tickets WHERE ticket_no=?", (ticket_no,))
                if not t:
                    raise BusinessError("检疫票不存在")
                ticket_id = t["id"]
                bs = self._all("SELECT id FROM batches WHERE ticket_id=?", (t["id"],))
                ids = [x["id"] for x in bs]
                targets = self._all(
                    "SELECT * FROM trays WHERE batch_id IN (%s) AND status IN ('on_sale','remainder')"
                    % ",".join("?" * len(ids)), ids) if ids else []
            else:
                raise BusinessError("召回范围非法")

            no = f"RC-{self._seq('recall'):04d}"
            self.c.execute(
                """INSERT INTO recalls(recall_no,scope,batch_id,ticket_id,reason,issued_at,issued_by)
                   VALUES(?,?,?,?,?,?,?)""",
                (no, scope, batch_id, ticket_id, reason, self.ts(), admin["display_name"]))
            recall_id = self._one("SELECT id FROM recalls WHERE recall_no=?", (no,))["id"]

            frozen = 0
            for t in targets:
                self.c.execute(
                    "INSERT OR IGNORE INTO recall_items(recall_id,tray_id) VALUES(?,?)",
                    (recall_id, t["id"]))
                self.c.execute("UPDATE trays SET recalled=1 WHERE id=?", (t["id"],))
                if t["status"] in ("on_sale", "remainder") and not t["frozen"]:
                    self._freeze("tray", t["id"],
                                 f"召回 {no}（{scope}）：{reason}", admin["display_name"])
                    frozen += 1
            # 关联余料一并冻结
            for t in targets:
                for r in self._all(
                        "SELECT * FROM remainders WHERE tray_id=? AND frozen=0", (t["id"],)):
                    self._freeze("remainder", r["id"],
                                 f"召回 {no}（{scope}）：{reason}", admin["display_name"])
            return {"recall_no": no, "scope": scope, "items": len(targets), "frozen": frozen}

    def lift_recall(self, actor_name, recall_no, note=""):
        """管理员撤回召回（之后仍需逐目标解封并过冻结校验）。"""
        admin = self._require_admin(actor_name)
        with dbmod.transaction(self.c):
            self.c.execute("UPDATE recalls SET active=0 WHERE recall_no=?", (recall_no,))
            return {"recall_no": recall_no, "active": False}

    # ------------------------------------------------------------- 冻结原语
    def _freeze(self, target_type, target_id, reason, by):
        table = {"batch": "batches", "tray": "trays",
                 "remainder": "remainders", "cabinet": "cold_cabinets"}[target_type]
        cur = self.c.execute(
            f"SELECT frozen FROM {table} WHERE id=?", (target_id,))
        row = cur.fetchone()
        if not row or row["frozen"]:
            return 0
        self.c.execute(
            f"UPDATE {table} SET frozen=1, freeze_reason=? WHERE id=?", (reason, target_id))
        if target_type == "remainder":
            self.c.execute("UPDATE remainders SET status='frozen' WHERE id=? AND status='held'",
                           (target_id,))
        self.c.execute(
            """INSERT INTO freeze_events(target_type,target_id,action,reason,acted_by,acted_at)
               VALUES(?,?,'freeze',?,?,?)""",
            (target_type, target_id, reason, by, self.ts()))
        return 1

    def _unfreeze(self, target_type, target_id, note, by):
        table = {"batch": "batches", "tray": "trays",
                 "remainder": "remainders", "cabinet": "cold_cabinets"}[target_type]
        self.c.execute(
            f"UPDATE {table} SET frozen=0, freeze_reason=NULL WHERE id=? AND frozen=1",
            (target_id,))
        if target_type == "remainder":
            r = self._one("SELECT weight_g, consumed_g FROM remainders WHERE id=?", (target_id,))
            used = self._one(
                "SELECT COALESCE(SUM(weight_g),0) v FROM waste_logs WHERE remainder_id=? AND source_type='remainder'",
                (target_id,))["v"]
            status = "held" if r["weight_g"] - r["consumed_g"] - used > 0 else "consumed"
            self.c.execute("UPDATE remainders SET status=? WHERE id=?", (status, target_id))
        self.c.execute(
            """INSERT INTO freeze_events(target_type,target_id,action,reason,acted_by,acted_at)
               VALUES(?,?,'unfreeze',?,?,?)""",
            (target_type, target_id, f"解封：{note}", by, self.ts()))

    def _freeze_batch_chain(self, batch_id, reason, by, include_batch=True) -> int:
        n = 0
        b = self._one("SELECT inspect_status FROM batches WHERE id=?", (batch_id,))
        if include_batch and b["inspect_status"] == "released":
            n += self._freeze("batch", batch_id, reason, by)
        for t in self._all(
                "SELECT * FROM trays WHERE batch_id=? AND status IN ('on_sale','remainder')",
                (batch_id,)):
            n += self._freeze("tray", t["id"], reason, by)
        for r in self._all(
                "SELECT id FROM remainders WHERE batch_id=? AND status IN ('held','frozen')",
                (batch_id,)):
            n += self._freeze("remainder", r["id"], reason, by)
        return n

    def unfreeze(self, actor_name, target_type, ref_no, note, cascade=True) -> dict:
        """管理员解封。存在未消除根因（票撤销/过期、生效召回、未处理抽查、超温未恢复）时拒绝。"""
        admin = self._require_admin(actor_name)
        with dbmod.transaction(self.c):
            target_id, batch_id = self._resolve_target(target_type, ref_no)
            self._check_unfreeze_blockers(target_type, target_id, batch_id)
            self._unfreeze(target_type, target_id, note, admin["display_name"])
            unfrozen = [f"{target_type}:{ref_no}"]
            if cascade and target_type == "batch":
                for t in self._all(
                        "SELECT tray_no FROM trays WHERE batch_id=? AND frozen=1", (target_id,)):
                    try:
                        self._check_unfreeze_blockers(
                            "tray",
                            self._one("SELECT id FROM trays WHERE tray_no=?",
                                      (t["tray_no"],))["id"], target_id)
                        self._unfreeze("tray", self._one(
                            "SELECT id FROM trays WHERE tray_no=?", (t["tray_no"],))["id"],
                            f"随批次 {ref_no} 解封：{note}", admin["display_name"])
                        unfrozen.append("tray:" + t["tray_no"])
                    except BusinessError:
                        pass
                for r in self._all(
                        "SELECT remainder_no FROM remainders WHERE batch_id=? AND frozen=1",
                        (target_id,)):
                    rid = self._one("SELECT id FROM remainders WHERE remainder_no=?",
                                    (r["remainder_no"],))["id"]
                    try:
                        self._check_unfreeze_blockers("remainder", rid, target_id)
                        self._unfreeze("remainder", rid,
                                       f"随批次 {ref_no} 解封：{note}", admin["display_name"])
                        unfrozen.append("remainder:" + r["remainder_no"])
                    except BusinessError:
                        pass
            return {"unfrozen": unfrozen}

    def _resolve_target(self, target_type, ref_no):
        if target_type == "batch":
            row = self._one("SELECT id FROM batches WHERE batch_no=?", (ref_no,))
            if not row:
                raise BusinessError("批次不存在")
            return row["id"], row["id"]
        if target_type == "tray":
            row = self._one("SELECT id,batch_id FROM trays WHERE tray_no=?", (ref_no,))
            if not row:
                raise BusinessError("托盘不存在")
            return row["id"], row["batch_id"]
        if target_type == "remainder":
            row = self._one("SELECT id,batch_id FROM remainders WHERE remainder_no=?", (ref_no,))
            if not row:
                raise BusinessError("余料不存在")
            return row["id"], row["batch_id"]
        raise BusinessError("解冻目标类型非法")

    def _check_unfreeze_blockers(self, target_type, target_id, batch_id):
        if target_type == "tray":
            t = self._one("SELECT * FROM trays WHERE id=?", (target_id,))
            batch_id = t["batch_id"]
            if self._one("SELECT 1 FROM recall_items WHERE tray_id=? AND EXISTS("
                         "SELECT 1 FROM recalls r WHERE r.id=recall_id AND r.active=1)",
                         (target_id,)):
                raise BusinessError("托盘处于生效召回中，不能解封")
            if self._one("""SELECT 1 FROM incidents WHERE handled=0 AND ref_type='tray'
                            AND ref_id=? AND kind='inspection_fail'""", (target_id,)):
                raise BusinessError("托盘抽查不合格尚未处理")
        if target_type == "remainder":
            r = self._one("SELECT * FROM remainders WHERE id=?", (target_id,))
            batch_id = r["batch_id"]
            if self._one("""SELECT 1 FROM recall_items ri JOIN trays t ON t.id=ri.tray_id
                            JOIN recalls rc ON rc.id=ri.recall_id
                            WHERE t.id=? AND rc.active=1""", (r["tray_id"],)):
                raise BusinessError("余料来源托盘处于生效召回中，不能解封")
        b = self._one("SELECT * FROM batches WHERE id=?", (batch_id,))
        ticket = self._one("SELECT * FROM quarantine_tickets WHERE id=?", (b["ticket_id"],))
        if ticket["status"] == "revoked":
            raise BusinessError(f"检疫票已撤销（{ticket['revoke_reason']}），不得解封")
        if ticket["status"] == "expired":
            raise BusinessError("检疫票已过期，不得解封")
        if self._one("""SELECT 1 FROM incidents WHERE handled=0 AND ref_type='batch'
                        AND ref_id=? AND kind='inspection_fail'""", (batch_id,)):
            raise BusinessError("批次抽查不合格尚未处理")
        # 超温冻结须温度恢复
        ev = self._one("""SELECT * FROM freeze_events WHERE target_type=? AND target_id=?
                          ORDER BY id DESC LIMIT 1""", (target_type, target_id))
        if ev and "超温" in (ev["reason"] or ""):
            cab = self._one(
                """SELECT cc.* FROM batches b JOIN cold_locations l ON l.id=b.location_id
                   JOIN cold_cabinets cc ON cc.id=l.cabinet_id WHERE b.id=?""", (batch_id,))
            if cab:
                latest = self._one(
                    "SELECT * FROM temperature_readings WHERE cabinet_id=? ORDER BY id DESC LIMIT 1",
                    (cab["id"],))
                if latest and latest["temp_c"] > cab["max_temp_c"]:
                    raise BusinessError(
                        f"冷藏柜 {cab['code']} 仍超温（{latest['temp_c']}℃ > {cab['max_temp_c']}℃）")

    def resolve_incident(self, actor_name, incident_id, resolution):
        self._require_admin(actor_name)
        with dbmod.transaction(self.c):
            inc = self._one("SELECT * FROM incidents WHERE id=?", (incident_id,))
            if not inc:
                raise BusinessError("异常单不存在")
            self._resolve_incident_row(inc, resolution)
            return {"id": incident_id, "handled": True}

    def _resolve_incident_row(self, inc, resolution):
        self.c.execute("UPDATE incidents SET handled=1, resolution=? WHERE id=?",
                       (resolution, inc["id"]))

    # ------------------------------------------------------------- 扫描（任务线程复用）
    def scan_ticket_expiry(self) -> list:
        """检疫票到期扫描：仍有未结重量的票置过期并冻结其链。返回动作清单。"""
        actions = []
        now = self.ts()
        with dbmod.transaction(self.c):
            tickets = self._all(
                "SELECT * FROM quarantine_tickets WHERE status='active' AND valid_until < ?",
                (now,))
            for t in tickets:
                bs = self._all("SELECT * FROM batches WHERE ticket_id=?", (t["id"],))
                open_weight = 0
                for b in bs:
                    open_weight += self._open_weight(b["id"])
                if open_weight <= 0:
                    self.c.execute(
                        "UPDATE quarantine_tickets SET status='expired' WHERE id=?", (t["id"],))
                    actions.append({"ticket": t["ticket_no"], "action": "expired_no_stock"})
                    continue
                self.c.execute(
                    "UPDATE quarantine_tickets SET status='expired' WHERE id=?", (t["id"],))
                for b in bs:
                    if self._open_weight(b["id"]) > 0:
                        n = self._freeze_batch_chain(
                            b["id"],
                            f"检疫票 {t['ticket_no']} 已于 {t['valid_until']} 到期，"
                            "未售部分立即冻结（到期扫描）",
                            "系统扫描")
                        actions.append({"ticket": t["ticket_no"],
                                        "batch": b["batch_no"], "frozen": n})
            return actions

    def scan_overtemp(self) -> list:
        """冷藏超温扫描：最新读数超上限的柜，冻结其内批次链。"""
        actions = []
        with dbmod.transaction(self.c):
            cabs = self._all("SELECT * FROM cold_cabinets")
            for cab in cabs:
                latest = self._one(
                    "SELECT * FROM temperature_readings WHERE cabinet_id=? ORDER BY id DESC LIMIT 1",
                    (cab["id"],))
                if not latest or latest["temp_c"] <= cab["max_temp_c"]:
                    continue
                occ = self._all("SELECT * FROM location_occupancy WHERE location_id IN ("
                                "SELECT id FROM cold_locations WHERE cabinet_id=?)", (cab["id"],))
                for o in occ:
                    b = self._one("SELECT * FROM batches WHERE id=?", (o["batch_id"],))
                    reason = (f"冷藏超温：{cab['code']} {cab['name']} 读数 {latest['temp_c']}℃ "
                              f"> 上限 {cab['max_temp_c']}℃（{latest['recorded_at']}），批次链冻结")
                    n = self._freeze_batch_chain(b["id"], reason, "系统扫描")
                    if n > 0:
                        # 仅在本次新冻结时记异常/动作，重复扫描保持幂等
                        self._incident("overtemp", "cabinet", cab["id"], b["stall_id"], reason)
                        actions.append({"cabinet": cab["code"], "batch": b["batch_no"],
                                        "temp_c": latest["temp_c"], "frozen": n})
            return actions

    def record_temperature(self, cabinet_code, temp_c, recorded_at=None):
        cab = self._one("SELECT * FROM cold_cabinets WHERE code=?", (cabinet_code,))
        if not cab:
            raise BusinessError("冷藏柜不存在")
        ts = recorded_at or self.ts()
        over = 1 if temp_c > cab["max_temp_c"] else 0
        self.c.execute(
            """INSERT INTO temperature_readings(cabinet_id,temp_c,recorded_at,overtemp)
               VALUES(?,?,?,?)""", (cab["id"], temp_c, ts, over))
        if self.thermo is not None:
            r = self.thermo.last_receipt(cabinet_code, temp_c)
            self._save_receipt("thermo", r["ref_no"], json.dumps(r, ensure_ascii=False))
        return {"cabinet": cabinet_code, "temp_c": temp_c, "overtemp": bool(over)}

    # ------------------------------------------------------------- 核算/查询
    def _open_weight(self, batch_id) -> int:
        """批次链上仍未售出/废弃的重量。"""
        b = self._one("SELECT declared_weight_g FROM batches WHERE id=?", (batch_id,))
        sold = self._one(
            "SELECT COALESCE(SUM(s.sold_weight_g),0) v FROM sales s JOIN trays t ON t.id=s.tray_id WHERE t.batch_id=?",
            (batch_id,))["v"]
        waste = self._one(
            "SELECT COALESCE(SUM(weight_g),0) v FROM waste_logs WHERE batch_id=?",
            (batch_id,))["v"]
        return b["declared_weight_g"] - sold - waste

    def conservation_report(self, batch_no) -> dict:
        """重量守恒核算。

        到货 D = 未分割留存 + 批次直接废弃 + 分割损耗废弃 + 销售
                 + 托盘废弃 + 余料废弃 + 托盘在售结存 + 余料结存
        （销售/托盘/余料桶均已摊平“余料再托”的嵌套：再托不重复扣批次。）
        """
        b = self._one("SELECT * FROM batches WHERE batch_no=?", (batch_no,))
        if not b:
            raise BusinessError("批次不存在")
        bid = b["id"]

        def waste(source):
            return self._one(
                "SELECT COALESCE(SUM(weight_g),0) v FROM waste_logs "
                "WHERE batch_id=? AND source_type=?", (bid, source))["v"]

        cuts_original = self._one(
            "SELECT COALESCE(SUM(weight_g),0) v FROM cuts WHERE batch_id=? AND recut=0",
            (bid,))["v"]
        batch_waste = waste("batch")
        cut_loss = waste("cut")
        tray_waste = waste("tray")
        rem_waste = waste("remainder")
        remaining_batch = b["declared_weight_g"] - cuts_original - batch_waste

        sold = self._one(
            "SELECT COALESCE(SUM(s.sold_weight_g),0) v FROM sales s "
            "JOIN trays t ON t.id=s.tray_id WHERE t.batch_id=?", (bid,))["v"]

        held_trays = sum(self._tray_remaining_g(t)
                         for t in self._all("SELECT * FROM trays WHERE batch_id=?", (bid,)))
        held_remainders = 0
        for r in self._all("SELECT * FROM remainders WHERE batch_id=?", (bid,)):
            used_w = self._one(
                "SELECT COALESCE(SUM(weight_g),0) v FROM waste_logs "
                "WHERE remainder_id=? AND source_type='remainder'", (r["id"],))["v"]
            held_remainders += r["weight_g"] - r["consumed_g"] - used_w

        buckets = {
            "sold_g": sold,
            "waste_batch_g": batch_waste,
            "waste_cut_g": cut_loss,
            "waste_tray_g": tray_waste,
            "waste_remainder_g": rem_waste,
            "held_on_trays_g": held_trays,
            "held_remainders_g": held_remainders,
            "uncut_on_batch_g": remaining_batch,
        }
        total = sum(buckets.values())
        return {
            "batch_no": batch_no,
            "declared_g": b["declared_weight_g"],
            "buckets": buckets,
            "chain_total_g": total,
            "conserved": total == b["declared_weight_g"]
                         and remaining_batch == self.batch_remaining_g(bid),
        }

    def freeze_state(self, target_type, ref_no):
        tid, _ = self._resolve_target(target_type, ref_no)
        table = {"batch": "batches", "tray": "trays", "remainder": "remainders"}[target_type]
        row = self._one(f"SELECT frozen, freeze_reason FROM {table} WHERE id=?", (tid,))
        return {"frozen": bool(row["frozen"]), "reason": row["freeze_reason"]}

    # ------------------------------------------------------------- 编号/回执
    def _seq(self, kind: str) -> int:
        table = {"arrival": "batches", "cut": "cuts", "tray": "trays", "waste": "waste_logs",
                 "rem": "remainders", "insp": "inspections", "recall": "recalls"}[kind]
        return self._one(f"SELECT COUNT(*) c FROM {table}")["c"] + 1

    def _incident(self, kind, ref_type, ref_id, stall_id, detail, detail_extra=None):
        if detail_extra:
            detail = detail + " | " + json.dumps(detail_extra, ensure_ascii=False)
        cur = self.c.execute(
            """INSERT INTO incidents(kind,ref_type,ref_id,stall_id,detail)
               VALUES(?,?,?,?,?)""", (kind, ref_type, ref_id, stall_id, detail))
        return kind

    def _save_receipt(self, adapter, ref_no, payload):
        self.c.execute(
            "INSERT INTO adapter_receipts(adapter,ref_no,payload) VALUES(?,?,?)",
            (adapter, ref_no, payload))

    # ------------------------------------------------------------- 页面数据
    def list_batches(self, stall_code=None):
        sql = """
          SELECT b.*, s.code stall_code, s.name stall_name, sup.code supplier_code,
                 sup.name supplier_name, t.ticket_no, t.status ticket_status,
                 t.valid_until, t.ticket_image, cc.code cabinet_code, l.shelf, l.slot_code
          FROM batches b
          JOIN stalls s ON s.id=b.stall_id
          JOIN suppliers sup ON sup.id=b.supplier_id
          JOIN quarantine_tickets t ON t.id=b.ticket_id
          LEFT JOIN cold_locations l ON l.id=b.location_id
          LEFT JOIN cold_cabinets cc ON cc.id=l.cabinet_id
        """
        args = ()
        if stall_code:
            sql += " WHERE s.code=?"
            args = (stall_code,)
        sql += " ORDER BY b.id"
        out = []
        for r in self._all(sql, args):
            d = dict(r)
            d["remaining_g"] = self.batch_remaining_g(r["id"])
            d["kg"] = kg(r["declared_weight_g"])
            out.append(d)
        return out

    def list_trays(self, stall_code=None, batch_no=None):
        sql = """
          SELECT tr.*, s.code stall_code, b.batch_no, t.ticket_no
          FROM trays tr
          JOIN stalls s ON s.id=tr.stall_id
          JOIN batches b ON b.id=tr.batch_id
          JOIN quarantine_tickets t ON t.id=b.ticket_id
        """
        cond, args = [], []
        if stall_code:
            cond.append("s.code=?"); args.append(stall_code)
        if batch_no:
            cond.append("b.batch_no=?"); args.append(batch_no)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY s.code, tr.tray_no"
        out = []
        for r in self._all(sql, args):
            d = dict(r)
            d["remaining_g"] = self._tray_remaining_g(r)
            out.append(d)
        return out

    def recall_list(self, stall_code=None):
        """召回清单：按摊位、托盘号排序；含已售/在冻/已废状态与文字原因。"""
        sql = """
          SELECT rc.recall_no, rc.scope, rc.reason, rc.active, rc.issued_at,
                 rc.issued_by, tr.tray_no, tr.weight_g, tr.status tray_status,
                 tr.frozen, tr.freeze_reason, tr.recalled, s.code stall_code,
                 s.name stall_name, b.batch_no,
                 COALESCE((SELECT SUM(sl.sold_weight_g) FROM sales sl WHERE sl.tray_id=tr.id),0) sold_g
          FROM recall_items ri
          JOIN recalls rc ON rc.id=ri.recall_id
          JOIN trays tr ON tr.id=ri.tray_id
          JOIN stalls s ON s.id=tr.stall_id
          JOIN batches b ON b.id=tr.batch_id
        """
        args = ()
        if stall_code:
            sql += " WHERE s.code=?"; args = (stall_code,)
        sql += " ORDER BY s.code, tr.tray_no, rc.recall_no"
        rows = self._all(sql, args)
        return [dict(r) for r in rows]

    def waste_ledger(self, stall_code=None):
        sql = """
          SELECT w.*, s.code stall_code, b.batch_no, tr.tray_no, rm.remainder_no
          FROM waste_logs w
          JOIN stalls s ON s.id=w.stall_id
          LEFT JOIN batches b ON b.id=w.batch_id
          LEFT JOIN trays tr ON tr.id=w.tray_id
          LEFT JOIN remainders rm ON rm.id=w.remainder_id
        """
        args = ()
        if stall_code:
            sql += " WHERE s.code=?"; args = (stall_code,)
        sql += " ORDER BY w.id"
        return [dict(r) for r in self._all(sql, args)]
