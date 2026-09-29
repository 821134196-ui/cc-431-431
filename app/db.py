"""SQLite 连接与表结构。

所有业务表均带 store_id(除供应商/商品等全局主数据外),
温度读数带 UNIQUE(device_id, ts) 约束以支持"乱序入账、迟到读数幂等",
补货与换货以显式行锁 + 状态机保证并发只成功一次。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("FREEZER_DB", os.path.join(os.path.dirname(__file__), "..", "data.db"))

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS stores (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    city        TEXT NOT NULL,
    staff       TEXT NOT NULL              -- 当班店员
);

CREATE TABLE IF NOT EXISTS suppliers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    contact     TEXT NOT NULL,
    courier     TEXT NOT NULL              -- 默认配送员
);

CREATE TABLE IF NOT EXISTS products (
    id          TEXT PRIMARY KEY,
    sku         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    spec        TEXT NOT NULL,             -- 规格,如 65g/支
    brand       TEXT NOT NULL
);

-- 商品默认供应商(换货出柜后据此生成回收单)
CREATE TABLE IF NOT EXISTS product_suppliers (
    product_id   TEXT PRIMARY KEY REFERENCES products(id),
    supplier_id  TEXT NOT NULL REFERENCES suppliers(id)
);

CREATE TABLE IF NOT EXISTS freezers (
    id          TEXT PRIMARY KEY,
    store_id    TEXT NOT NULL REFERENCES stores(id),
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    rows_n      INTEGER NOT NULL,
    cols_n      INTEGER NOT NULL,
    temp_min    REAL NOT NULL,             -- 合格温度下限 ℃
    temp_max    REAL NOT NULL,             -- 合格温度上限 ℃
    powered     INTEGER NOT NULL DEFAULT 1,
    UNIQUE(store_id, code)
);

CREATE TABLE IF NOT EXISTS channels (
    id          TEXT PRIMARY KEY,
    freezer_id  TEXT NOT NULL REFERENCES freezers(id),
    store_id    TEXT NOT NULL REFERENCES stores(id),
    pos         TEXT NOT NULL,             -- 矩阵坐标 A1..
    row_idx     INTEGER NOT NULL,
    col_idx     INTEGER NOT NULL,
    capacity    INTEGER NOT NULL,
    product_id  TEXT REFERENCES products(id), -- 货道绑定的商品规格
    UNIQUE(freezer_id, pos)
);

CREATE TABLE IF NOT EXISTS batches (
    id            TEXT PRIMARY KEY,
    store_id      TEXT NOT NULL REFERENCES stores(id),
    product_id    TEXT NOT NULL REFERENCES products(id),
    batch_no      TEXT NOT NULL,
    prod_date     TEXT NOT NULL,           -- YYYY-MM-DD
    expiry_date   TEXT NOT NULL,
    qty_received  INTEGER NOT NULL,        -- 入柜数量
    qty_sold      INTEGER NOT NULL DEFAULT 0,
    qty_damaged   INTEGER NOT NULL DEFAULT 0,
    qty_exchanged INTEGER NOT NULL DEFAULT 0, -- 临期换货出柜数量
    qty_recycled  INTEGER NOT NULL DEFAULT 0, -- 供应商已回收数量
    locked        INTEGER NOT NULL DEFAULT 0, -- 批次锁定:处置后冻结
    locked_reason TEXT,
    created_ts    TEXT NOT NULL,
    UNIQUE(store_id, batch_no)
);

-- 货道装载记录:一个货道同时仍有两个未清空批次即构成"批次混放"。
CREATE TABLE IF NOT EXISTS channel_batches (
    channel_id  TEXT NOT NULL REFERENCES channels(id),
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    store_id    TEXT NOT NULL REFERENCES stores(id),
    qty_loaded  INTEGER NOT NULL,
    qty_remaining INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    loaded_ts   TEXT NOT NULL,
    PRIMARY KEY (channel_id, batch_id)
);

CREATE TABLE IF NOT EXISTS stock_moves (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id    TEXT NOT NULL,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- load/sell/replenish/damage/exchange_out/recycle
    channel_id  TEXT,
    batch_id    TEXT,
    qty         INTEGER NOT NULL,
    ref         TEXT,                      -- 关联单号
    actor       TEXT
);

CREATE TABLE IF NOT EXISTS temp_readings (
    device_id   TEXT NOT NULL,             -- = freezer_id
    ts          TEXT NOT NULL,             -- ISO8601,读数真实产生时间
    temp_c      REAL NOT NULL,
    received_ts TEXT NOT NULL,             -- 系统收账时间
    late        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, ts)            -- 幂等:同设备同时间只入一条
);

-- 处置记录:断电/超限等状态以最后处置为准,迟到读数不得回改。
CREATE TABLE IF NOT EXISTS temp_incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    freezer_id  TEXT NOT NULL,
    store_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- excursion / outage / restored
    ts          TEXT NOT NULL,
    detail      TEXT,
    handled_ts  TEXT,                      -- 处置时间
    handled_by  TEXT,
    resolution  TEXT,                      -- resolved/discarded/exchanged
    UNIQUE(freezer_id, kind, ts)
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    -- temp_breach 温度超限 / outage 冷柜断电 / mixed_batch 批次混放 /
    -- expiry 临期未换 / package_damage 包装破损 / recycle_mismatch 回收数量不符
    severity    TEXT NOT NULL DEFAULT 'normal',
    title       TEXT NOT NULL,
    detail      TEXT,
    ref_type    TEXT,
    ref_id      TEXT,
    status      TEXT NOT NULL DEFAULT 'open',   -- open/done/ignored
    created_ts  TEXT NOT NULL,
    closed_ts   TEXT,
    UNIQUE(store_id, kind, ref_type, ref_id)
);

CREATE TABLE IF NOT EXISTS exchange_orders (
    id            TEXT PRIMARY KEY,
    store_id      TEXT NOT NULL REFERENCES stores(id),
    freezer_id    TEXT NOT NULL REFERENCES freezers(id),
    channel_id    TEXT NOT NULL REFERENCES channels(id),
    batch_id      TEXT NOT NULL REFERENCES batches(id),
    qty           INTEGER NOT NULL,
    reason        TEXT NOT NULL,           -- expiry/package_damage/temp_breach
    status        TEXT NOT NULL DEFAULT 'pending',
    -- pending -> staff_confirmed -> courier_confirmed(完成) / rejected
    staff_user    TEXT,
    staff_ts      TEXT,
    courier_user  TEXT,
    courier_ts    TEXT,
    created_ts    TEXT NOT NULL,
    completed_ts  TEXT,
    recycle_id    TEXT                     -- 完成后生成的回收单
);

CREATE TABLE IF NOT EXISTS recycling_orders (
    id            TEXT PRIMARY KEY,
    store_id      TEXT NOT NULL REFERENCES stores(id),
    supplier_id   TEXT NOT NULL REFERENCES suppliers(id),
    batch_id      TEXT NOT NULL REFERENCES batches(id),
    qty_expected  INTEGER NOT NULL,        -- 换货出柜应回收数量
    qty_received  INTEGER NOT NULL DEFAULT 0, -- 供应商实收
    status        TEXT NOT NULL DEFAULT 'open', -- open/partial/settled/mismatch
    created_ts    TEXT NOT NULL,
    settled_ts    TEXT,
    note          TEXT
);

CREATE INDEX IF NOT EXISTS idx_readings_dev_ts ON temp_readings(device_id, ts);
CREATE INDEX IF NOT EXISTS idx_batches_store ON batches(store_id);
CREATE INDEX IF NOT EXISTS idx_cb_batch ON channel_batches(batch_id);
CREATE INDEX IF NOT EXISTS idx_tasks_store ON tasks(store_id, status);

-- 幂等键:补货等单据全局只入账一次(并发安全)
CREATE TABLE IF NOT EXISTS idempotency (
    scope       TEXT NOT NULL,
    idem_key    TEXT NOT NULL,
    ts          TEXT NOT NULL,
    PRIMARY KEY (scope, idem_key)
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection):
    """显式事务(IMMEDIATE):补货并发时第二个事务在锁上等待,
    随后通过状态/库存校验失败,保证只能成功一次。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
