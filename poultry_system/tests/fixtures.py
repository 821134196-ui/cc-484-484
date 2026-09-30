"""测试公共夹具：内存库 + 已放行的标准批次。"""
from __future__ import annotations

from datetime import datetime, timedelta

from poultry_system import db
from poultry_system.adapters import ScaleAdapter
from poultry_system.service import MarketService


def make_svc():
    conn = db.connect(":memory:")
    db.init_db(conn)
    svc = MarketService(conn, scale=ScaleAdapter("precise"))
    svc.create_user("admin", "管理员", "market_admin")
    svc.create_user("zhang", "老张", "stall_owner")
    svc.create_user("li", "老李", "stall_owner")
    svc.create_stall("A-101", "老张禽摊", "zhang")
    svc.create_stall("A-102", "老李禽摊", "li")
    svc.create_supplier("S-01", "绿野禽业（虚构）")
    svc.create_supplier("S-02", "清溪水禽（虚构）")
    svc.create_cabinet("C-01", "一号柜", 4.0)
    svc.create_locations("C-01")
    return conn, svc


def window(days_from=-1, days_until=2):
    now = datetime.now()
    return ((now + timedelta(days=days_from)).isoformat(timespec="seconds"),
            (now + timedelta(days=days_until)).isoformat(timespec="seconds"))


def arrived(svc, actor="zhang", *, ticket="Q-1", stall="A-101", supplier="S-01",
            cat="白条鸡", weight=10000, declared=None, valid=None, drift=None):
    vf, uf = valid or window()
    if drift is not None:
        svc.scale = ScaleAdapter("scripted", scripts={ticket: [drift]})
    r = svc.arrive(actor, stall_code=stall, supplier_code=supplier, ticket_no=ticket,
                   category=cat, ticket_category=cat, ticket_weight_g=weight,
                   declared_weight_g=declared if declared is not None else weight,
                   valid_from=vf, valid_until=uf, ticket_image=f"tickets/{ticket}.svg")
    if drift is not None:
        svc.scale = ScaleAdapter("precise")
    return r


def released(svc, **kw):
    loc = kw.pop("loc", ("C-01", "1层", "A"))
    actor = kw.pop("actor", "zhang")
    r = arrived(svc, actor=actor, **kw)
    assert r["status"] != "rejected", r
    b = r["batch_no"]
    svc.release_to_cabinet(actor, b, *loc)
    return b


def conserved(svc, b):
    return svc.conservation_report(b)["conserved"]
