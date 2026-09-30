"""虚构演示数据：一条启动命令载入重复票、跨品类混放、部分召回等样例。

所有供应商、票据均为虚构。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import config
from .adapters import MockScale, MockTempProbe
from .database import init_db
from .errors import (BusinessError, DuplicateCertError, MixedCellError,
                     TempError)
from .services import MarketService

STALLS = [
    ("A01", "晨鸣禽档", "王晨鸣"),
    ("A02", "兴旺禽铺", "李兴旺"),
    ("B01", "鲜鸭坊", "周慧"),
]
SUPPLIERS = [
    ("GY-07", "广源禽业合作社（虚构）", "广东清远"),
    ("LF-12", "联丰肉禽场（虚构）", "广东佛山"),
    ("HG-03", "禾岗生态鸽场（虚构）", "广东从化"),
]
CELLS = [("1号柜", "A01"), ("1号柜", "A02"), ("1号柜", "A03"), ("1号柜", "A04"),
         ("2号柜", "B01"), ("2号柜", "B02")]


def seed(service: MarketService) -> dict:
    now = datetime.now()
    summary: dict = {"scenarios": []}

    for args in STALLS:
        service.create_stall(*args)
    for args in SUPPLIERS:
        service.create_supplier(*args)
    for cab, slot in CELLS:
        service.create_cell(cab, slot)

    def arrive(cert_no, *, stall, sup, cat, weight, cell, actual=None,
               issued=None, valid_days=3, tolerate=False):
        bid = service.register_arrival(
            cert_no=cert_no, stall_code=stall, supplier_code=sup, category=cat,
            declared_weight=weight, issued_at=issued or now - timedelta(hours=2),
            valid_days=valid_days, stall_code_actor=stall)
        r = service.verify_and_store(bid, cell_code=cell, actual_weight=actual,
                                     tolerate_diff=tolerate)
        return bid, r

    # ---- 正常链路：A01 白条鸡，分割→销售→余料回收返工→异常废弃 ----
    b1, _ = arrive("JY20260930-101", stall="A01", sup="GY-07", cat="白条鸡",
                   weight=120.0, cell="C1-A01", actual=119.6)
    c1 = service.register_cut(b1, tray_weights=[12.5, 12.0, 11.5],
                              remainder_weight=2.0, waste_weight=1.0,
                              stall_code_actor="A01")
    service.sell_tray(c1["trays"][0]["id"], stall_code_actor="A01")
    rid = service.tray_to_remainder(c1["trays"][1]["id"], stall_code_actor="A01")
    service.rework_remainder(rid, tray_weights=[11.8], waste_weight=0.2,
                             stall_code_actor="A01")
    # 异常废弃：3kg 废弃占毛重 27%
    service.register_cut(b1, tray_weights=[8.0], remainder_weight=0.0,
                         waste_weight=3.0, waste_reason="分割修整损耗偏大",
                         stall_code_actor="A01")
    summary["normal_batch"] = b1

    # ---- 同品类跨批次共柜允许：A01 第二单仍放 C1-A01 ----
    b2, _ = arrive("JY20260930-102", stall="A01", sup="GY-07", cat="白条鸡",
                   weight=80.0, cell="C1-A01", actual=79.8)
    c2 = service.register_cut(b2, tray_weights=[8.0, 8.0, 8.0, 8.0],
                              stall_code_actor="A01")
    service.sell_tray(c2["trays"][0]["id"], stall_code_actor="A01")
    summary["partial_recall_batch"] = b2

    # ---- 部分召回：清单含 2 个未售（冻结）+ 1 个已售（转追溯）----
    recall = service.create_recall(
        reason="供应商通报局部污染风险，按托盘号定向召回",
        tray_ids=[c2["trays"][1]["id"], c2["trays"][2]["id"],
                  c2["trays"][0]["id"]])
    summary["scenarios"].append(f"部分召回 {recall['recall_no']}："
                                f"冻结未售 {len(recall['frozen_trays'])} 个，"
                                f"已售追溯 {len(recall['sold_or_missing'])} 个")

    # ---- A02 白条鸭：抽查托盘不合格 → 同批未售托盘联动冻结 ----
    b3, _ = arrive("JY20260930-103", stall="A02", sup="LF-12", cat="白条鸭",
                   weight=60.0, cell="C1-A02", actual=59.7)
    c3 = service.register_cut(b3, tray_weights=[6.0, 6.0],
                              stall_code_actor="A02")
    service.add_inspection(target="tray", target_id=c3["trays"][0]["id"],
                           result="fail", note="挥发性盐基氮偏高")
    summary["inspection_fail_batch"] = b3

    # ---- B01 乳鸽：检疫票撤销 → 未售托盘立即冻结 ----
    b4, _ = arrive("JY20260930-104", stall="B01", sup="HG-03", cat="乳鸽",
                   weight=40.0, cell="C2-B01", actual=39.9)
    c4 = service.register_cut(b4, tray_weights=[5.0, 5.0, 5.0],
                              stall_code_actor="B01")
    service.sell_tray(c4["trays"][0]["id"], stall_code_actor="B01")
    service.revoke_cert("JY20260930-104", reason="官方通报源头养殖场疫病监测异常")
    summary["revoked_cert_batch"] = b4

    # ---- C1-A03：入柜后冷藏超温（探头预置 9.5℃），扫描线程启动即冻结 ----
    b5, _ = arrive("JY20260930-105", stall="A01", sup="GY-07", cat="白条鸡",
                   weight=50.0, cell="C1-A03", actual=49.8)
    c5 = service.register_cut(b5, tray_weights=[5.0, 5.0],
                              stall_code_actor="A01")
    service.report_label_lost(c5["trays"][1]["id"], note="转运中标签脱落，"
                                                         "等待管理员补签",
                              stall_code_actor="A01")
    service.probe.force_temp("C1-A03", 9.5)
    summary["temp_cell"] = "C1-A03"

    # ---- 票据即将到期（约 70 秒后），由进程内扫描自动冻结 ----
    arrive("JY20260930-106", stall="A02", sup="LF-12", cat="白条鸭",
           weight=35.0, cell="C2-B02", actual=34.9,
           issued=now - timedelta(days=3) + timedelta(seconds=70))
    summary["cert_expiring"] = "JY20260930-106（约 70 秒后到期）"

    # ---- 称重差异：票面 100kg，预置秤读数 106kg（+6%）→ 拒收 ----
    service.scale.preset_next(106.0)
    b7 = service.register_arrival(cert_no="JY20260930-107", stall_code="A01",
                                  supplier_code="GY-07", category="白条鸡",
                                  declared_weight=100.0, stall_code_actor="A01")
    service.verify_and_store(b7, cell_code="C1-A04")
    summary["scenarios"].append("称重差异样例：JY20260930-107 +6% 已拒收")

    # ---- 票号重复：用同一票号再次登记 → 拒绝并记录 ----
    try:
        service.register_arrival(cert_no="JY20260930-101", stall_code="A02",
                                 supplier_code="LF-12", category="白条鸭",
                                 declared_weight=30.0, stall_code_actor="A02")
    except DuplicateCertError:
        summary["scenarios"].append("重复票样例：JY20260930-101 二次登记已拒绝")

    # ---- 跨品类混放：白条鹅放进白条鸡格位 C1-A01 被拒，改放 C1-A04 放行 ----
    b9 = service.register_arrival(cert_no="JY20260930-108", stall_code="A02",
                                  supplier_code="LF-12", category="白条鹅",
                                  declared_weight=28.0, stall_code_actor="A02")
    try:
        service.verify_and_store(b9, cell_code="C1-A01")
    except MixedCellError:
        summary["scenarios"].append("跨品类混放样例：白条鹅→C1-A01（白条鸡）被拒")
    service.verify_and_store(b9, cell_code="C1-A04")
    summary["scenarios"].append("混放批次改入 C1-A04 后放行")

    return summary


def build_fresh_service() -> MarketService:
    """删除旧库，建表并播种（演示启动用）。"""
    config.ensure_dirs()
    for p in (config.DB_PATH,):
        if p.exists():
            p.unlink()
    for suffix in ("-wal", "-shm"):
        wp = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
        if wp.exists():
            wp.unlink()
    init_db(config.DB_PATH)
    svc = MarketService(config.DB_PATH, scale=MockScale(), probe=MockTempProbe())
    svc.seed_summary = seed(svc)
    return svc
