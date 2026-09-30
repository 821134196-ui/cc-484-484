"""SQLite 持久层：重量链路的唯一事实来源。

约定
----
* 所有重量以**整数克**存储，避免浮点误差；1 kg = 1000 g。
* 一条到货批次到货重量 = 已分割 + 剩余在批 + 废弃物(批) + 重量差异(仅异常记录，不守恒)。
* 托盘重量守恒：产出 = 销售 + 余料回收 + 废弃物(托) + 在售/冻结在托。
* 余料块可回收后再托盘（remainder_chain 记录链），余料本身守恒：
  回收重量 = 再托消耗 + 留存。
* 所有关键写操作在 service 层以 BEGIN IMMEDIATE 串行化，以批次余量为准。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 角色：stall_owner（摊主，可登记分割/销售/余料/废弃），market_admin（市场管理员，冻结/解封/抽查）
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('stall_owner','market_admin'))
);

-- 摊位
CREATE TABLE IF NOT EXISTS stalls (
    id      INTEGER PRIMARY KEY,
    code    TEXT NOT NULL UNIQUE,          -- A-101
    name    TEXT NOT NULL,                 -- 老张禽摊
    owner_user_id INTEGER REFERENCES users(id),
    zone    TEXT NOT NULL DEFAULT '禽肉区'
);

-- 供应商代号（虚构）
CREATE TABLE IF NOT EXISTS suppliers (
    id      INTEGER PRIMARY KEY,
    code    TEXT NOT NULL UNIQUE,          -- S-01
    name    TEXT NOT NULL,                 -- 绿野禽业合作社（虚构）
    region  TEXT NOT NULL DEFAULT '虚构产地'
);

-- 检疫票（虚构）。票号全局唯一。
CREATE TABLE IF NOT EXISTS quarantine_tickets (
    id              INTEGER PRIMARY KEY,
    ticket_no       TEXT NOT NULL UNIQUE,  -- 检疫票编号
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id),
    category        TEXT NOT NULL,         -- 品类：白条鸡/鸭胸/鸡腿...
    gross_weight_g  INTEGER NOT NULL CHECK (gross_weight_g > 0),  -- 票面重量
    issued_at       TEXT NOT NULL,         -- 签发时间 ISO
    valid_from      TEXT NOT NULL,
    valid_until     TEXT NOT NULL,         -- 有效期截止
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','revoked','expired','consumed')),
    revoke_reason   TEXT,
    ticket_image    TEXT,                  -- 本地虚构票据文件（相对 data 目录）
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 到货批次
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY,
    batch_no        TEXT NOT NULL UNIQUE,  -- B20260930-001
    stall_id        INTEGER NOT NULL REFERENCES stalls(id),
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id),
    ticket_id       INTEGER NOT NULL REFERENCES quarantine_tickets(id),
    category        TEXT NOT NULL,
    declared_weight_g INTEGER NOT NULL CHECK (declared_weight_g > 0), -- 报称（= 到货过磅）
    ticket_weight_g   INTEGER NOT NULL,    -- 冗余票面重量，便于核对
    -- 放行状态：pending 待验票 / released 放行 / rejected 拒收
    inspect_status  TEXT NOT NULL DEFAULT 'pending'
                    CHECK (inspect_status IN ('pending','released','rejected')),
    inspect_note    TEXT,                  -- 验票结论（票货不符/称重差异说明）
    arrived_at      TEXT NOT NULL,
    released_at     TEXT,
    -- 柜内位置（放行后入柜）；一个批次在一个格位，混放检测见格位占用表
    location_id     INTEGER REFERENCES cold_locations(id),
    -- 冻结：整批冻结时设置（撤销检疫票）；托盘另有独立冻结
    frozen          INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0,1)),
    freeze_reason   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 冷藏柜与格位
CREATE TABLE IF NOT EXISTS cold_cabinets (
    id      INTEGER PRIMARY KEY,
    code    TEXT NOT NULL UNIQUE,          -- C-01
    name    TEXT NOT NULL,
    max_temp_c REAL NOT NULL DEFAULT 4.0   -- 冷藏上限温度
);
CREATE TABLE IF NOT EXISTS cold_locations (
    id          INTEGER PRIMARY KEY,
    cabinet_id  INTEGER NOT NULL REFERENCES cold_cabinets(id),
    shelf       TEXT NOT NULL,             -- 1层/2层
    slot_code   TEXT NOT NULL,             -- A/B/C
    UNIQUE(cabinet_id, shelf, slot_code)
);
-- 格位当前占用（一个格位同时只允许一个未结批次，跨批次混放在此拦截）
CREATE TABLE IF NOT EXISTS location_occupancy (
    location_id INTEGER PRIMARY KEY REFERENCES cold_locations(id),
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    occupied_since TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS temperature_readings (
    id          INTEGER PRIMARY KEY,
    cabinet_id  INTEGER NOT NULL REFERENCES cold_cabinets(id),
    temp_c      REAL NOT NULL,
    recorded_at TEXT NOT NULL,
    overtemp    INTEGER NOT NULL DEFAULT 0 CHECK (overtemp IN (0,1))
);

-- 分割记录（摊主登记）：从原批次扣减重量
CREATE TABLE IF NOT EXISTS cuts (
    id          INTEGER PRIMARY KEY,
    cut_no      TEXT NOT NULL UNIQUE,      -- CUT-...
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    stall_id    INTEGER NOT NULL REFERENCES stalls(id),
    operator    TEXT NOT NULL,             -- 摊主
    weight_g    INTEGER NOT NULL CHECK (weight_g > 0),  -- 分割投入（秤重回执）
    loss_g      INTEGER NOT NULL DEFAULT 0 CHECK (loss_g >= 0), -- 分割损耗（入废弃台账）
    recut       INTEGER NOT NULL DEFAULT 0 CHECK (recut IN (0,1)), -- 1=余料再托，不重复扣批次
    scale_ref   TEXT,                      -- 称重适配器回执号
    cut_at      TEXT NOT NULL,
    note        TEXT
);

-- 销售托盘（分割产出，贴标签）。批次链经 batch_id，余料再托经 source_remainder_id
CREATE TABLE IF NOT EXISTS trays (
    id          INTEGER PRIMARY KEY,
    tray_no     TEXT NOT NULL UNIQUE,      -- T-...
    cut_id      INTEGER NOT NULL REFERENCES cuts(id),
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    stall_id    INTEGER NOT NULL REFERENCES stalls(id),
    product_name TEXT NOT NULL,            -- 鸡腿分装/鸡胸托盘
    weight_g    INTEGER NOT NULL CHECK (weight_g > 0),   -- 标签重量
    produced_at TEXT NOT NULL,
    -- on_sale 在售 / sold 售罄 / remainder 已转余料 / waste 废弃 /
    -- frozen 冻结 / released_unfreeze 由冻结恢复在售（用 status + frozen 标志表达）
    status      TEXT NOT NULL DEFAULT 'on_sale'
                CHECK (status IN ('on_sale','sold','remainder','waste','frozen')),
    frozen      INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0,1)),
    freeze_reason TEXT,                   -- 冻结文字原因（冻结时必填）
    label_intact INTEGER NOT NULL DEFAULT 1 CHECK (label_intact IN (0,1)),
    label_note  TEXT,
    recalled    INTEGER NOT NULL DEFAULT 0 CHECK (recalled IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_trays_batch ON trays(batch_id);
CREATE INDEX IF NOT EXISTS idx_trays_stall ON trays(stall_id);

-- 销售记录
CREATE TABLE IF NOT EXISTS sales (
    id          INTEGER PRIMARY KEY,
    tray_id     INTEGER NOT NULL REFERENCES trays(id),
    sold_weight_g INTEGER NOT NULL CHECK (sold_weight_g > 0),
    sold_at     TEXT NOT NULL,
    seller      TEXT NOT NULL
);

-- 余料块：销售剩余回收，沿用批次链，可再托盘
CREATE TABLE IF NOT EXISTS remainders (
    id          INTEGER PRIMARY KEY,
    remainder_no TEXT NOT NULL UNIQUE,
    tray_id     INTEGER NOT NULL REFERENCES trays(id),
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    stall_id    INTEGER NOT NULL REFERENCES stalls(id),
    weight_g    INTEGER NOT NULL CHECK (weight_g > 0),
    recovered_at TEXT NOT NULL,
    consumed_g  INTEGER NOT NULL DEFAULT 0 CHECK (consumed_g >= 0), -- 再托盘累计消耗
    status      TEXT NOT NULL DEFAULT 'held'
                CHECK (status IN ('held','consumed','waste','frozen')),
    frozen      INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0,1)),
    freeze_reason TEXT
);
-- 再托盘：余料 -> 新托盘（批次链不变）
CREATE TABLE IF NOT EXISTS remainder_trays (
    id           INTEGER PRIMARY KEY,
    remainder_id INTEGER NOT NULL REFERENCES remainders(id),
    tray_id      INTEGER NOT NULL REFERENCES trays(id),
    weight_g     INTEGER NOT NULL CHECK (weight_g > 0),
    created_at   TEXT NOT NULL
);

-- 废弃物台账
CREATE TABLE IF NOT EXISTS waste_logs (
    id          INTEGER PRIMARY KEY,
    waste_no    TEXT NOT NULL UNIQUE,
    stall_id    INTEGER NOT NULL REFERENCES stalls(id),
    batch_id    INTEGER REFERENCES batches(id),
    tray_id     INTEGER REFERENCES trays(id),
    remainder_id INTEGER REFERENCES remainders(id),
    cut_id      INTEGER REFERENCES cuts(id),
    source_type TEXT NOT NULL CHECK (source_type IN ('batch','tray','remainder','cut')),
    weight_g    INTEGER NOT NULL CHECK (weight_g > 0),
    reason      TEXT NOT NULL,             -- 变质/损耗/超温/召回销毁...
    abnormal    INTEGER NOT NULL DEFAULT 0 CHECK (abnormal IN (0,1)), -- 废弃重量异常
    abnormal_note TEXT,
    logged_at   TEXT NOT NULL,
    logged_by   TEXT NOT NULL
);

-- 市场抽查
CREATE TABLE IF NOT EXISTS inspections (
    id          INTEGER PRIMARY KEY,
    inspect_no  TEXT NOT NULL UNIQUE,
    batch_id    INTEGER REFERENCES batches(id),
    tray_id     INTEGER REFERENCES trays(id),
    stall_id    INTEGER NOT NULL REFERENCES stalls(id),
    result      TEXT NOT NULL CHECK (result IN ('pass','fail')),
    note        TEXT NOT NULL,
    inspected_at TEXT NOT NULL,
    inspector   TEXT NOT NULL
);

-- 召回（支持部分召回：指定托盘清单）
CREATE TABLE IF NOT EXISTS recalls (
    id          INTEGER PRIMARY KEY,
    recall_no   TEXT NOT NULL UNIQUE,
    scope       TEXT NOT NULL CHECK (scope IN ('batch','partial','ticket')),
    batch_id    INTEGER REFERENCES batches(id),
    ticket_id   INTEGER REFERENCES quarantine_tickets(id),
    reason      TEXT NOT NULL,
    issued_at   TEXT NOT NULL,
    issued_by   TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS recall_items (
    id          INTEGER PRIMARY KEY,
    recall_id   INTEGER NOT NULL REFERENCES recalls(id),
    tray_id     INTEGER NOT NULL REFERENCES trays(id),
    UNIQUE(recall_id, tray_id)
);

-- 冻结事件（每次冻结/解封留痕，冻结状态始终附文字原因）
CREATE TABLE IF NOT EXISTS freeze_events (
    id          INTEGER PRIMARY KEY,
    target_type TEXT NOT NULL CHECK (target_type IN ('batch','tray','remainder','cabinet')),
    target_id   INTEGER NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('freeze','unfreeze')),
    reason      TEXT NOT NULL,             -- 冻结原因（文字）/ 解封说明
    acted_by    TEXT NOT NULL,
    acted_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_freeze_target ON freeze_events(target_type, target_id, id);

-- 各类业务异常/处理事件（重复票、票货不符、称重差异、混放、超温、标签脱落）
CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,   -- duplicate_ticket/ticket_mismatch/weight_discrepancy/
                                 -- mixed_batch/overtemp/label_lost/abnormal_waste
    ref_type    TEXT,
    ref_id      INTEGER,
    stall_id    INTEGER REFERENCES stalls(id),
    detail      TEXT NOT NULL,
    handled     INTEGER NOT NULL DEFAULT 0,
    resolution  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 适配器回执（称重/温度），便于追溯
CREATE TABLE IF NOT EXISTS adapter_receipts (
    id          INTEGER PRIMARY KEY,
    adapter     TEXT NOT NULL CHECK (adapter IN ('scale','thermo')),
    ref_no      TEXT NOT NULL UNIQUE,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False：HTTP 线程池与扫描线程共享连接；
    # 所有写操作以 BEGIN IMMEDIATE 串行化，读为 WAL 快照，配合 busy_timeout。
    conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE：立即取写锁，并发分割以批次余量为准串行化。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
