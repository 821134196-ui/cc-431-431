-- 冰淇淋冷柜温度与批次换货系统 数据库 Schema

-- 门店
CREATE TABLE IF NOT EXISTS stores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  address TEXT,
  created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 冷柜
CREATE TABLE IF NOT EXISTS freezers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  min_temp REAL NOT NULL DEFAULT -22.0,
  max_temp REAL NOT NULL DEFAULT -18.0,
  power_status TEXT NOT NULL DEFAULT 'on',  -- on / off
  created_at TEXT DEFAULT (datetime('now', 'localtime')),
  FOREIGN KEY (store_id) REFERENCES stores(id)
);

-- 商品
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  spec TEXT,
  barcode TEXT UNIQUE,
  created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 批次
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL,
  batch_number TEXT NOT NULL,
  production_date TEXT NOT NULL,
  expiry_date TEXT NOT NULL,
  initial_quantity INTEGER NOT NULL,
  current_quantity INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'in_stock',  -- in_stock / locked / exchanged / scrapped / recycled
  locked_reason TEXT,
  created_at TEXT DEFAULT (datetime('now', 'localtime')),
  FOREIGN KEY (product_id) REFERENCES products(id)
);

-- 货道
CREATE TABLE IF NOT EXISTS lanes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  freezer_id INTEGER NOT NULL,
  lane_number TEXT NOT NULL,
  product_id INTEGER,
  batch_id INTEGER,
  capacity INTEGER NOT NULL DEFAULT 20,
  quantity INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now', 'localtime')),
  FOREIGN KEY (freezer_id) REFERENCES freezers(id),
  FOREIGN KEY (product_id) REFERENCES products(id),
  FOREIGN KEY (batch_id) REFERENCES batches(id),
  UNIQUE(freezer_id, lane_number)
);

-- 温度读数
CREATE TABLE IF NOT EXISTS temperature_readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  freezer_id INTEGER NOT NULL,
  temperature REAL NOT NULL,
  recorded_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'device',  -- device / manual
  created_at TEXT DEFAULT (datetime('now', 'localtime')),
  FOREIGN KEY (freezer_id) REFERENCES freezers(id)
);

-- 补货记录
CREATE TABLE IF NOT EXISTS restocks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lane_id INTEGER NOT NULL,
  batch_id INTEGER NOT NULL,
  quantity INTEGER NOT NULL,
  restocked_at TEXT DEFAULT (datetime('now', 'localtime')),
  FOREIGN KEY (lane_id) REFERENCES lanes(id),
  FOREIGN KEY (batch_id) REFERENCES batches(id)
);

-- 换货单
CREATE TABLE IF NOT EXISTS exchange_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_number TEXT NOT NULL UNIQUE,
  store_id INTEGER NOT NULL,
  batch_id INTEGER NOT NULL,
  lane_id INTEGER,
  quantity INTEGER NOT NULL,
  reason TEXT NOT NULL,  -- near_expiry / packaging_damage / temperature / batch_mixing / other
  status TEXT NOT NULL DEFAULT 'pending',  -- pending / clerk_confirmed / delivery_confirmed / completed / cancelled
  clerk_confirmed_at TEXT,
  delivery_confirmed_at TEXT,
  created_at TEXT DEFAULT (datetime('now', 'localtime')),
  FOREIGN KEY (store_id) REFERENCES stores(id),
  FOREIGN KEY (batch_id) REFERENCES batches(id),
  FOREIGN KEY (lane_id) REFERENCES lanes(id)
);

-- 报损记录
CREATE TABLE IF NOT EXISTS scrapped (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  quantity INTEGER NOT NULL,
  reason TEXT,
  scrapped_at TEXT DEFAULT (datetime('now', 'localtime')),
  FOREIGN KEY (batch_id) REFERENCES batches(id)
);

-- 供应商回收
CREATE TABLE IF NOT EXISTS recycling (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  quantity INTEGER NOT NULL,
  supplier TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending / collected / reconciled
  collected_quantity INTEGER,
  reconciled_at TEXT,
  created_at TEXT DEFAULT (datetime('now', 'localtime')),
  FOREIGN KEY (batch_id) REFERENCES batches(id)
);

-- 任务
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id INTEGER,
  freezer_id INTEGER,
  type TEXT NOT NULL,  -- temp_overlimit / power_off / batch_mixing / near_expiry / packaging_damage / recycling_mismatch
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'open',  -- open / in_progress / resolved
  priority TEXT NOT NULL DEFAULT 'medium',  -- low / medium / high / critical
  created_at TEXT DEFAULT (datetime('now', 'localtime')),
  resolved_at TEXT,
  FOREIGN KEY (store_id) REFERENCES stores(id),
  FOREIGN KEY (freezer_id) REFERENCES freezers(id)
);

-- 处置记录（用于记录任务处置状态，迟到读数不得覆盖）
CREATE TABLE IF NOT EXISTS dispositions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  action TEXT NOT NULL,  -- resolved / escalated / dismissed
  note TEXT,
  disposed_at TEXT DEFAULT (datetime('now', 'localtime')),
  FOREIGN KEY (task_id) REFERENCES tasks(id)
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_freezers_store ON freezers(store_id);
CREATE INDEX IF NOT EXISTS idx_lanes_freezer ON lanes(freezer_id);
CREATE INDEX IF NOT EXISTS idx_batches_product ON batches(product_id);
CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);
CREATE INDEX IF NOT EXISTS idx_temp_readings_freezer ON temperature_readings(freezer_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_exchange_orders_store ON exchange_orders(store_id);
CREATE INDEX IF NOT EXISTS idx_exchange_orders_status ON exchange_orders(status);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(type);
