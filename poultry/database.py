"""SQLite 连接、schema 初始化与重量链路追踪。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;

-- 摊位
CREATE TABLE IF NOT EXISTS stalls (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,          -- A01
    name        TEXT NOT NULL,                 -- 晨鸣禽档
    operator    TEXT NOT NULL,                 -- 摊主
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 供应商（虚构代号）
CREATE TABLE IF NOT EXISTS suppliers (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,          -- GY-07
    name        TEXT NOT NULL,                 -- 虚构：广源禽业合作社
    origin      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 检疫票（虚构）
CREATE TABLE IF NOT EXISTS certs (
    id              INTEGER PRIMARY KEY,
    cert_no         TEXT NOT NULL UNIQUE,      -- 票号唯一
    supplier_code   TEXT NOT NULL,
    category        TEXT NOT NULL,             -- 白条鸡/鸭/鸽...
    declared_weight REAL NOT NULL,             -- 票面重量 kg
    issued_at       TEXT NOT NULL,             -- 签发时间
    valid_until     TEXT NOT NULL,             -- 有效期截止
    status          TEXT NOT NULL DEFAULT 'valid', -- valid/revoked/expired
    revoked_reason  TEXT,
    doc_file        TEXT,                      -- 本地票据缩略条文件
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 到货批次
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY,
    batch_no        TEXT NOT NULL UNIQUE,      -- B20260930-001
    cert_id         INTEGER NOT NULL REFERENCES certs(id),
    stall_id        INTEGER NOT NULL REFERENCES stalls(id),
    supplier_code   TEXT NOT NULL,
    category        TEXT NOT NULL,
    declared_weight REAL NOT NULL,             -- 票面重量
    arrived_weight  REAL,                      -- 实际过磅重量（放行后写入）
    arrived_at      TEXT NOT NULL,
    -- 重量链路：到货 = 在柜 + 已分割毛重 + 废弃（批次级直接废弃）
    status          TEXT NOT NULL DEFAULT 'pending',
        -- pending 待验票 / accepted 已放行 / rejected 拒收 / empty 已清空
    reject_reason   TEXT,
    cell_id         INTEGER REFERENCES cells(id),  -- 入柜格位
    accepted_at     TEXT,
    frozen          INTEGER NOT NULL DEFAULT 0,    -- 批次冻结（阻断继续分割）
    freeze_reason   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_batches_stall ON batches(stall_id);
CREATE INDEX IF NOT EXISTS idx_batches_cert ON batches(cert_id);

-- 冷藏柜 / 格位
CREATE TABLE IF NOT EXISTS cells (
    id          INTEGER PRIMARY KEY,
    cabinet     TEXT NOT NULL,                 -- 1号柜
    slot        TEXT NOT NULL,                 -- A层-02
    code        TEXT NOT NULL UNIQUE,          -- C1-A02
    temp        REAL NOT NULL DEFAULT 2.0,     -- 最近温度（模拟适配器上报）
    temp_ok     INTEGER NOT NULL DEFAULT 1,    -- 最近测温是否合规
    temp_at     TEXT,
    UNIQUE(cabinet, slot)
);

-- 格位占用（同一格位同一品类同一温度状态；跨批次混放需同品类）
CREATE TABLE IF NOT EXISTS cell_occupancy (
    id          INTEGER PRIMARY KEY,
    cell_id     INTEGER NOT NULL REFERENCES cells(id),
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    weight_in   REAL NOT NULL,                 -- 放入重量
    weight_out  REAL NOT NULL DEFAULT 0,       -- 已取出重量
    placed_at   TEXT NOT NULL,
    cleared_at  TEXT,
    UNIQUE(cell_id, batch_id)
);

-- 分割记录（一次分割：从原批次扣减 gross_weight，产出 trays + remainders + waste，重量守恒）
CREATE TABLE IF NOT EXISTS cuts (
    id              INTEGER PRIMARY KEY,
    cut_no          TEXT UNIQUE,               -- CUT-...（余料返工可空）
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    stall_id        INTEGER NOT NULL REFERENCES stalls(id),
    gross_weight    REAL NOT NULL DEFAULT 0,   -- 从批次扣减的毛重（返工为0）
    source_remainder_id INTEGER REFERENCES remainders(id), -- 余料返工来源
    tray_weight     REAL NOT NULL,             -- 产出托盘合计
    remainder_weight REAL NOT NULL,            -- 余料合计
    waste_weight    REAL NOT NULL,             -- 废弃合计
    operator        TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_cuts_batch ON cuts(batch_id);

-- 销售托盘（分割产出）
CREATE TABLE IF NOT EXISTS trays (
    id              INTEGER PRIMARY KEY,
    tray_no         TEXT NOT NULL UNIQUE,      -- T-A01-0001
    cut_id          INTEGER REFERENCES cuts(id),
    source_remainder_id INTEGER REFERENCES remainders(id), -- 余料返工产出
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    stall_id        INTEGER NOT NULL REFERENCES stalls(id),
    category        TEXT NOT NULL,
    weight          REAL NOT NULL,
    label_file      TEXT,                      -- 托盘标签图
    label_lost      INTEGER NOT NULL DEFAULT 0,-- 标签脱落报告
    -- 状态：in_stock 在柜 / sold 已售 / remainder 转余料 / waste 废弃 /
    --       frozen 冻结 / recalled 召回
    status          TEXT NOT NULL DEFAULT 'in_stock',
    frozen          INTEGER NOT NULL DEFAULT 0,
    freeze_reason   TEXT,
    frozen_at       TEXT,
    sold_at         TEXT,
    parent_tray_id  INTEGER REFERENCES trays(id), -- 余料回收继续沿用批次链
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_trays_batch ON trays(batch_id);
CREATE INDEX IF NOT EXISTS idx_trays_stall_status ON trays(stall_id, status);

-- 余料
CREATE TABLE IF NOT EXISTS remainders (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,          -- RM-...
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    stall_id    INTEGER NOT NULL REFERENCES stalls(id),
    source_cut_id INTEGER REFERENCES cuts(id),
    source_tray_id INTEGER REFERENCES trays(id),
    weight      REAL NOT NULL,
    -- pending 待处理 / reworked 已重新分割（生成新托盘）/ waste 废弃 / frozen
    status      TEXT NOT NULL DEFAULT 'pending',
    frozen      INTEGER NOT NULL DEFAULT 0,
    freeze_reason TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 废弃物台账
CREATE TABLE IF NOT EXISTS wastes (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,          -- W-...
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    stall_id    INTEGER NOT NULL REFERENCES stalls(id),
    source_type TEXT NOT NULL,                 -- cut 分割损耗 / tray 托盘变质 / remainder 余料 / batch 整批
    source_id   INTEGER,
    cut_id      INTEGER REFERENCES cuts(id),
    weight      REAL NOT NULL,
    reason      TEXT NOT NULL,                 -- 边角料/变质/召回销毁/抽查不合格...
    abnormal    INTEGER NOT NULL DEFAULT 0,    -- 废弃重量异常标记
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_wastes_batch ON wastes(batch_id);

-- 冻结记录（冻结传播台账）
CREATE TABLE IF NOT EXISTS freezes (
    id          INTEGER PRIMARY KEY,
    scope_type  TEXT NOT NULL,                 -- cert 票撤销 / inspection 抽查 / recall 召回 / temp 超温
    ref_id      INTEGER,                       -- 关联 cert/inspection
    reason      TEXT NOT NULL,                 -- 文字原因（始终随冻结状态展示）
    issued_by   TEXT NOT NULL,                 -- 市场管理员
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS freeze_targets (
    id          INTEGER PRIMARY KEY,
    freeze_id   INTEGER NOT NULL REFERENCES freezes(id),
    target_type TEXT NOT NULL,                 -- batch / tray / remainder
    target_id   INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 市场抽查
CREATE TABLE IF NOT EXISTS inspections (
    id          INTEGER PRIMARY KEY,
    insp_no     TEXT NOT NULL UNIQUE,          -- JC-...
    batch_id    INTEGER REFERENCES batches(id),
    tray_id     INTEGER REFERENCES trays(id),
    stall_id    INTEGER NOT NULL REFERENCES stalls(id),
    result      TEXT NOT NULL,                 -- pass 合格 / fail 不合格
    note        TEXT,
    inspector   TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 召回（部分召回：指定托盘清单）
CREATE TABLE IF NOT EXISTS recalls (
    id          INTEGER PRIMARY KEY,
    recall_no   TEXT NOT NULL UNIQUE,          -- ZH-...
    cert_id     INTEGER REFERENCES certs(id),
    batch_id    INTEGER REFERENCES batches(id),
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',  -- open / closed
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    closed_at   TEXT
);
CREATE TABLE IF NOT EXISTS recall_items (
    id          INTEGER PRIMARY KEY,
    recall_id   INTEGER NOT NULL REFERENCES recalls(id),
    tray_id     INTEGER NOT NULL REFERENCES trays(id),
    -- pending 待处置 / destroyed 销毁 / released 解除
    disposition TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(recall_id, tray_id)
);

-- 验票/异常事件日志
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,                 -- dup_cert/goods_mismatch/weight_diff/mixed/temp/label/recall/waste_abnormal...
    level       TEXT NOT NULL DEFAULT 'warn',  -- info/warn/error
    message     TEXT NOT NULL,
    ref_type    TEXT,
    ref_id      INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 单号序列（按前缀计数）
CREATE TABLE IF NOT EXISTS seq (
    prefix TEXT PRIMARY KEY,
    n      INTEGER NOT NULL DEFAULT 0
);
"""


def connect(db_path=None) -> sqlite3.Connection:
    path = str(db_path or config.DB_PATH)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务（配合 BEGIN IMMEDIATE 做并发控制）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db(db_path=None) -> None:
    config.ensure_dirs()
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def next_no(conn: sqlite3.Connection, prefix: str, *, pad: int = 4) -> str:
    """生成前缀递增单号（须在事务内调用）。"""
    row = conn.execute(
        "INSERT INTO seq(prefix,n) VALUES(?,1) "
        "ON CONFLICT(prefix) DO UPDATE SET n=n+1 RETURNING n",
        (prefix,),
    ).fetchone()
    return f"{prefix}{row['n']:0{pad}d}"
