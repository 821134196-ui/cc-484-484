"""虚构演示数据：主数据、完整重量链路，以及八类异常样例。

一条启动命令（python3 -m poultry_system.run）即载入：
正常链 / 重复票 / 票货称重差异 / 跨批次混放 / 部分召回 /
检疫票到期冻结 / 冷藏超温冻结 / 标签脱落 / 废弃重量异常。
所有供应商、检疫票、照片均为虚构。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from . import db as dbmod
from .adapters import ScaleAdapter
from .service import MarketService, BusinessError

# ---------------------------------------------------------------- 虚构 SVG 资产
_TICKET_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="520" height="210" viewBox="0 0 520 210" font-family="sans-serif">
 <rect x="6" y="6" width="508" height="198" rx="10" fill="#ffffff" stroke="#2e7d32" stroke-width="2.5"/>
 <rect x="6" y="6" width="14" height="198" rx="7" fill="#2e7d32"/>
 <text x="34" y="40" font-size="20" font-weight="bold" fill="#2e7d32">动物检疫合格证明（虚构样例）</text>
 <text x="34" y="62" font-size="11" fill="#6b7a8d">本票据为系统演示自动生成，不代表真实检疫文书</text>
 <line x1="34" y1="72" x2="490" y2="72" stroke="#2e7d32" stroke-dasharray="4 4"/>
 <text x="34" y="98" font-size="13" fill="#1f2d3d">票号：<tspan font-weight="bold" fill="#0d3c6e">{no}</tspan></text>
 <text x="34" y="122" font-size="13">供应商代号：<tspan font-weight="bold">{sup}</tspan>　品类：<tspan font-weight="bold">{cat}</tspan></text>
 <text x="34" y="146" font-size="13">票面重量：<tspan font-weight="bold" fill="#1565c0">{kg} kg</tspan></text>
 <text x="34" y="170" font-size="13">有效期：{frm} 至 <tspan font-weight="bold">{until}</tspan></text>
 <g transform="translate(396,120)">
   <circle r="34" fill="none" stroke="#2e7d32" stroke-width="2"/>
   <circle r="27" fill="none" stroke="#2e7d32" stroke-width="1"/>
   <text x="0" y="-2" text-anchor="middle" font-size="11" font-weight="bold" fill="#2e7d32">检疫合格</text>
   <text x="0" y="13" text-anchor="middle" font-size="9" fill="#2e7d32">（虚构章）</text>
 </g>
</svg>"""

_PHOTO_CABINET = """<svg xmlns="http://www.w3.org/2000/svg" width="480" height="300" viewBox="0 0 480 300" font-family="sans-serif">
 <rect width="480" height="300" fill="#eef3f8"/>
 <text x="16" y="26" font-size="13" fill="#6b7a8d">现场照片（虚构示意图，非实物拍摄）：冷藏柜温度异常</text>
 <rect x="60" y="46" width="240" height="220" rx="10" fill="#dfeaf6" stroke="#1565c0" stroke-width="2"/>
 <rect x="76" y="66" width="208" height="86" rx="6" fill="#ffffff" stroke="#9db8d6"/>
 <rect x="76" y="166" width="208" height="86" rx="6" fill="#ffffff" stroke="#9db8d6"/>
 <text x="180" y="114" text-anchor="middle" font-size="12" fill="#6b7a8d">1 层格位</text>
 <text x="180" y="214" text-anchor="middle" font-size="12" fill="#6b7a8d">2 层格位</text>
 <g transform="translate(330,80)">
   <rect width="118" height="96" rx="10" fill="#fff" stroke="#c62828" stroke-width="2"/>
   <text x="59" y="34" text-anchor="middle" font-size="13" fill="#c62828" font-weight="bold">温度探头回执</text>
   <text x="59" y="66" text-anchor="middle" font-size="30" font-weight="bold" fill="#c62828">7.2℃</text>
   <text x="59" y="86" text-anchor="middle" font-size="11" fill="#6b7a8d">上限 4.0℃ ⚠</text>
 </g>
</svg>"""

_PHOTO_SCALE = """<svg xmlns="http://www.w3.org/2000/svg" width="480" height="260" viewBox="0 0 480 260" font-family="sans-serif">
 <rect width="480" height="260" fill="#f4f7f2"/>
 <text x="16" y="26" font-size="13" fill="#6b7a8d">现场照片（虚构示意图）：到货过磅秤重回执</text>
 <rect x="120" y="56" width="240" height="70" rx="8" fill="#ffffff" stroke="#2e7d32" stroke-width="2"/>
 <rect x="150" y="126" width="180" height="18" fill="#9db8a0"/>
 <text x="240" y="100" text-anchor="middle" font-size="30" font-weight="bold" fill="#0d3c6e">10.000 kg</text>
 <text x="240" y="180" text-anchor="middle" font-size="13" fill="#2e7d32">MOCK-SCALE-01 回执 SCALE-…（模拟适配器）</text>
 <text x="240" y="206" text-anchor="middle" font-size="12" fill="#6b7a8d">报称 9.100kg → 过磅 10.000kg：称重差异样例</text>
</svg>"""

_PHOTO_LABEL = """<svg xmlns="http://www.w3.org/2000/svg" width="480" height="260" viewBox="0 0 480 260" font-family="sans-serif">
 <rect width="480" height="260" fill="#f7f5f0"/>
 <text x="16" y="26" font-size="13" fill="#6b7a8d">现场照片（虚构示意图）：托盘标签脱落</text>
 <rect x="120" y="60" width="240" height="130" rx="10" fill="#fff" stroke="#b26a00" stroke-width="2" stroke-dasharray="6 5"/>
 <text x="240" y="110" text-anchor="middle" font-size="15" fill="#b26a00" font-weight="bold">标签缺失</text>
 <text x="240" y="140" text-anchor="middle" font-size="12" fill="#6b7a8d">托盘暂停销售，待管理员核验批次链后重贴</text>
 <path d="M330 60 l30 26" stroke="#c62828" stroke-width="3"/>
</svg>"""


def _write_assets(data_dir: Path, tickets: dict):
    tdir = data_dir / "tickets"
    pdir = data_dir / "photos"
    tdir.mkdir(parents=True, exist_ok=True)
    pdir.mkdir(parents=True, exist_ok=True)
    for no, t in tickets.items():
        (tdir / f"{no}.svg").write_text(
            _TICKET_SVG.format(no=no, sup=t["sup"], cat=t["cat"], kg=t["kg"],
                               frm=t["from"][:16].replace("T", " "),
                               until=t["until"][:16].replace("T", " ")),
            encoding="utf-8")
    (pdir / "cabinet-c02-overtemp.svg").write_text(_PHOTO_CABINET, encoding="utf-8")
    (pdir / "scale-weighing.svg").write_text(_PHOTO_SCALE, encoding="utf-8")
    (pdir / "label-lost.svg").write_text(_PHOTO_LABEL, encoding="utf-8")


# ---------------------------------------------------------------- 种子
def seed(conn, data_dir: Path) -> dict:
    dbmod.init_db(conn)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    svc = MarketService(conn)
    now = datetime.now()
    iso = lambda d: d.isoformat(timespec="seconds")
    day = lambda n: iso(now + timedelta(days=n))

    # ---- 用户 / 摊位
    svc.create_user("admin", "市场管理员·严管", "market_admin")
    svc.create_user("zhang", "摊主·老张", "stall_owner")
    svc.create_user("li", "摊主·老李", "stall_owner")
    svc.create_user("wang", "摊主·老王", "stall_owner")
    svc.create_stall("A-101", "老张禽摊", "zhang")
    svc.create_stall("A-102", "老李水禽", "li")
    svc.create_stall("A-103", "老王分割", "wang")
    svc.create_supplier("S-01", "绿野禽业合作社（虚构）", "虚构产地·清溪县")
    svc.create_supplier("S-02", "清溪水禽养殖场（虚构）", "虚构产地·云溪镇")
    svc.create_supplier("S-03", "丰禾白条鸡供应（虚构）", "虚构产地·河湾村")
    svc.create_cabinet("C-01", "一号冷藏柜", 4.0)
    svc.create_cabinet("C-02", "二号冷藏柜", 4.0)
    svc.create_locations("C-01")
    svc.create_locations("C-02")

    tickets = {
        "Q-2026-0901": {"sup": "S-01", "cat": "白条鸡", "kg": "20.000",
                        "from": day(-1), "until": day(3)},
        "Q-2026-0902": {"sup": "S-02", "cat": "鸭胸", "kg": "12.000",
                        "from": day(-1), "until": day(2)},
        "Q-2026-0903": {"sup": "S-03", "cat": "鸡腿", "kg": "8.000",
                        "from": day(-2), "until": day(4)},
        "Q-2026-0904": {"sup": "S-01", "cat": "白条鸭", "kg": "10.000",
                        "from": day(-1), "until": day(2)},
        "Q-2026-0905": {"sup": "S-02", "cat": "白条鸡", "kg": "10.000",
                        "from": day(-1), "until": day(2)},
        "Q-2026-0906": {"sup": "S-03", "cat": "鸭边腿", "kg": "6.000",
                        "from": day(-1), "until": day(3)},
    }
    _write_assets(data_dir, tickets)

    def arrive(actor, no, stall, sup, cat, declared, ticket_w, drift=0):
        """用带偏差的模拟秤登记到货（drift 只影响该票一次过磅）。"""
        saved = svc.scale
        svc.scale = ScaleAdapter("scripted", scripts={no: [drift]})
        try:
            r = svc.arrive(actor, stall_code=stall, supplier_code=sup, ticket_no=no,
                           category=cat, ticket_category=cat,
                           ticket_weight_g=ticket_w, declared_weight_g=declared,
                           valid_from=tickets[no]["from"],
                           valid_until=tickets[no]["until"],
                           ticket_image=f"tickets/{no}.svg")
            return r
        finally:
            svc.scale = saved

    summary = {}

    # ---- B1 正常完整链（含余料回收 + 再托盘）
    b1 = arrive("zhang", "Q-2026-0901", "A-101", "S-01", "白条鸡", 20000, 20000)["batch_no"]
    svc.release_to_cabinet("zhang", b1, "C-01", "1层", "A")
    c1 = svc.register_cut("zhang", batch_no=b1, weight_g=12000,
        trays=[("鸡腿分装", 5000), ("鸡胸分装", 4000), ("鸡块分装", 2500)], loss_g=500)
    svc.sell_tray("zhang", c1["trays"][0], 2000)
    rem1 = svc.recover_remainder("zhang", c1["trays"][0])["remainder_no"]
    rc1 = svc.recut_remainder("zhang", remainder_no=rem1,
                              trays=[("再分割鸡腿", 2850)], loss_g=150)
    svc.sell_tray("zhang", rc1["trays"][0], 2850)
    svc.sell_tray("zhang", c1["trays"][1], 1500)   # 鸡胸留 2500g 在售
    summary["healthy_chain"] = b1

    # ---- B2 部分召回（两张未售鸭胸托盘冻结）
    b2 = arrive("li", "Q-2026-0902", "A-102", "S-02", "鸭胸", 12000, 12000)["batch_no"]
    svc.release_to_cabinet("li", b2, "C-01", "1层", "B")
    c2 = svc.register_cut("li", batch_no=b2, weight_g=12000,
        trays=[("鸭胸托盘甲", 4000), ("鸭胸托盘乙", 4000), ("鸭腿分装", 3000)], loss_g=1000)
    svc.sell_tray("li", c2["trays"][2], 1200)
    svc.issue_recall("admin", scope="partial", reason="客户反馈局部异味，部分召回（虚构）",
                     tray_nos=[c2["trays"][0], c2["trays"][1]])
    summary["partial_recall"] = c2["trays"][:2]

    # ---- B3 检疫票在售后到期 → 扫描冻结
    b3 = arrive("wang", "Q-2026-0903", "A-103", "S-03", "鸡腿", 8000, 8000)["batch_no"]
    svc.release_to_cabinet("wang", b3, "C-01", "2层", "A")
    c3 = svc.register_cut("wang", batch_no=b3, weight_g=8000,
        trays=[("鸡腿甲", 3000), ("鸡腿乙", 3000), ("翅根分装", 1500)], loss_g=500)
    svc.sell_tray("wang", c3["trays"][0], 1000)
    svc.recover_remainder("wang", c3["trays"][0])
    conn.execute("UPDATE quarantine_tickets SET valid_until=? WHERE ticket_no='Q-2026-0903'",
                 (iso(now - timedelta(hours=2)),))
    summary["expiry_pending"] = b3

    # ---- B4 冷藏超温 → 扫描冻结
    b4 = arrive("zhang", "Q-2026-0904", "A-101", "S-01", "白条鸭", 10000, 10000)["batch_no"]
    svc.release_to_cabinet("zhang", b4, "C-02", "1层", "A")
    svc.register_cut("zhang", batch_no=b4, weight_g=10000,
                     trays=[("半片鸭甲", 4800), ("半片鸭乙", 4800)], loss_g=400)
    svc.record_temperature("C-02", 7.2)
    summary["overtemp_pending"] = b4

    # ---- B5 称重差异（报称 9.1kg / 过磅 10.0kg）→ 管理员确认 → 异常废弃
    b5 = arrive("li", "Q-2026-0905", "A-102", "S-02", "白条鸡", 9100, 10000, drift=900)["batch_no"]
    svc.resolve_weight_discrepancy("admin", b5, accept=True, note="以模拟秤过磅 10.000kg 为准")
    svc.release_to_cabinet("li", b5, "C-01", "2层", "B")
    w5 = svc.log_waste("li", source_type="batch", ref_no=b5, weight_g=1500,
                       reason="到货局部变质异味（虚构）")
    summary["abnormal_waste"] = w5["waste_no"]

    # ---- B6 跨批次混放被拦 → 改入空位；标签脱落
    b6 = arrive("wang", "Q-2026-0906", "A-103", "S-03", "鸭边腿", 6000, 6000)["batch_no"]
    try:
        svc.release_to_cabinet("wang", b6, "C-01", "1层", "A")  # 被 B1 占用
    except BusinessError:
        summary["mixed_batch_blocked"] = True
    svc.release_to_cabinet("wang", b6, "C-01", "2层", "C")
    c6 = svc.register_cut("wang", batch_no=b6, weight_g=6000,
                          trays=[("鸭边腿甲", 3000), ("鸭边腿乙", 2800)], loss_g=200)
    svc.report_label_lost("wang", c6["trays"][0], "搬运中标签脱落，见 photos/label-lost.svg")

    # ---- 重复票：同一检疫票再次到货 → 拒收 + 异常单
    try:
        arrive("zhang", "Q-2026-0901", "A-101", "S-01", "白条鸡", 20000, 20000)
    except BusinessError as e:
        summary["duplicate_ticket_blocked"] = str(e)

    # ---- 进程内扫描：票据到期 + 冷藏超温
    summary["scan"] = {"ticket_expiry": svc.scan_ticket_expiry(),
                       "overtemp": svc.scan_overtemp()}

    # ---- 汇总守恒
    summary["conservation"] = {
        b: svc.conservation_report(b)["conserved"]
        for b in (b1, b2, b3, b4, b5, b6)}
    return summary
