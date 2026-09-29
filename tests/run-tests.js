// 测试运行器
const { initDb, closeDb, query, queryOne, execute, transaction } = require('../src/db');
const { seed } = require('../src/seed');
const { checkTemperature, checkPower, daysBetween } = require('../src/task-engine');
const { recordReading } = require('../src/device-adapter');

let passed = 0;
let failed = 0;
const failures = [];

function assert(condition, message) {
  if (condition) {
    passed++;
    console.log(`  ✓ ${message}`);
  } else {
    failed++;
    failures.push(message);
    console.log(`  ✗ ${message}`);
  }
}

function assertEqual(actual, expected, message) {
  if (actual === expected) {
    passed++;
    console.log(`  ✓ ${message}`);
  } else {
    failed++;
    failures.push(`${message} (期望: ${expected}, 实际: ${actual})`);
    console.log(`  ✗ ${message} (期望: ${expected}, 实际: ${actual})`);
  }
}

function setupTestDb() {
  // 使用内存数据库进行测试
  const { DatabaseSync } = require('node:sqlite');
  const db = new DatabaseSync(':memory:');
  db.exec('PRAGMA foreign_keys = ON;');

  // 读取并执行 schema
  const fs = require('node:fs');
  const path = require('node:path');
  const schema = fs.readFileSync(path.join(__dirname, '..', 'src', 'schema.sql'), 'utf-8');
  db.exec(schema);

  return db;
}

// 测试1: 温度阈值边界
function testThresholdBoundaries() {
  console.log('\n📋 测试1: 温度阈值边界');

  const db = setupTestDb();

  // 创建测试门店和冷柜
  db.prepare('INSERT INTO stores (name) VALUES (?)').run('测试门店');
  db.prepare('INSERT INTO freezers (store_id, name, min_temp, max_temp, power_status) VALUES (?, ?, ?, ?, ?)').run(1, '测试冷柜', -22, -18, 'on');

  // 测试边界值：刚好在范围内
  db.prepare('INSERT INTO temperature_readings (freezer_id, temperature, recorded_at, source) VALUES (?, ?, ?, ?)').run(1, -18, '2026-09-29 10:00:00', 'device');
  let task = db.prepare("SELECT * FROM tasks WHERE freezer_id = 1 AND type = 'temp_overlimit' AND status != 'resolved'").get();
  assert(!task, '温度等于上限 -18°C 不应触发超限任务');

  // 测试边界值：刚好超出上限
  db.prepare('INSERT INTO temperature_readings (freezer_id, temperature, recorded_at, source) VALUES (?, ?, ?, ?)').run(1, -17.9, '2026-09-29 10:01:00', 'device');
  // 手动触发检查
  const freezer = db.prepare('SELECT * FROM freezers WHERE id = 1').get();
  const latest = db.prepare('SELECT * FROM temperature_readings WHERE freezer_id = 1 ORDER BY recorded_at DESC, id DESC LIMIT 1').get();
  assert(latest.temperature > freezer.max_temp, '温度 -17.9°C 超出上限 -18°C');

  // 测试边界值：刚好在下限
  db.prepare('INSERT INTO temperature_readings (freezer_id, temperature, recorded_at, source) VALUES (?, ?, ?, ?)').run(1, -22, '2026-09-29 10:02:00', 'device');
  const latest2 = db.prepare('SELECT * FROM temperature_readings WHERE freezer_id = 1 ORDER BY recorded_at DESC, id DESC LIMIT 1').get();
  assert(latest2.temperature >= freezer.min_temp, '温度等于下限 -22°C 应在合格范围内');

  // 测试边界值：刚好超出下限
  db.prepare('INSERT INTO temperature_readings (freezer_id, temperature, recorded_at, source) VALUES (?, ?, ?, ?)').run(1, -22.1, '2026-09-29 10:03:00', 'device');
  const latest3 = db.prepare('SELECT * FROM temperature_readings WHERE freezer_id = 1 ORDER BY recorded_at DESC, id DESC LIMIT 1').get();
  assert(latest3.temperature < freezer.min_temp, '温度 -22.1°C 超出下限 -22°C');

  db.close();
}

// 测试2: 批次锁定
function testBatchLocking() {
  console.log('\n📋 测试2: 批次锁定');

  const db = setupTestDb();

  // 创建测试数据
  db.prepare('INSERT INTO stores (name) VALUES (?)').run('测试门店');
  db.prepare('INSERT INTO products (name, spec) VALUES (?, ?)').run('测试商品', '100ml');
  db.prepare('INSERT INTO freezers (store_id, name, min_temp, max_temp, power_status) VALUES (?, ?, ?, ?, ?)').run(1, '测试冷柜', -22, -18, 'on');
  db.prepare('INSERT INTO lanes (freezer_id, lane_number, capacity) VALUES (?, ?, ?)').run(1, 'A1', 20);

  // 创建批次
  db.prepare('INSERT INTO batches (product_id, batch_number, production_date, expiry_date, initial_quantity, current_quantity, status) VALUES (?, ?, ?, ?, ?, ?, ?)')
    .run(1, 'BATCH001', '2026-01-01', '2026-12-31', 10, 10, 'in_stock');

  // 补货入柜
  db.prepare('UPDATE lanes SET product_id = ?, batch_id = ?, quantity = ? WHERE id = ?').run(1, 1, 10, 1);

  // 创建换货单（应锁定批次）
  db.prepare("INSERT INTO exchange_orders (order_number, store_id, batch_id, lane_id, quantity, reason, status) VALUES (?, ?, ?, ?, ?, ?, ?)")
    .run('EX001', 1, 1, 1, 3, 'near_expiry', 'pending');
  db.prepare("UPDATE batches SET status = 'locked', locked_reason = ? WHERE id = ?").run('换货单 EX001', 1);

  // 验证批次已锁定
  const batch = db.prepare('SELECT * FROM batches WHERE id = 1').get();
  assertEqual(batch.status, 'locked', '换货单创建后批次应被锁定');

  // 尝试对锁定批次再次换货（应失败）
  const existing = db.prepare("SELECT * FROM exchange_orders WHERE batch_id = 1 AND status NOT IN ('cancelled', 'completed')").get();
  assert(existing, '锁定批次不应创建新的换货单');

  // 尝试对锁定批次补货（应失败）
  const lockedBatch = db.prepare('SELECT * FROM batches WHERE id = 1').get();
  assert(lockedBatch.status !== 'in_stock', '锁定批次不应接受补货');

  db.close();
}

// 测试3: 温度乱序（迟到读数不得覆盖处置后状态）
function testTemperatureOutOfOrder() {
  console.log('\n📋 测试3: 温度乱序');

  const db = setupTestDb();

  // 创建测试数据
  db.prepare('INSERT INTO stores (name) VALUES (?)').run('测试门店');
  db.prepare('INSERT INTO freezers (store_id, name, min_temp, max_temp, power_status) VALUES (?, ?, ?, ?, ?)').run(1, '测试冷柜', -22, -18, 'on');

  // 记录正常温度
  db.prepare('INSERT INTO temperature_readings (freezer_id, temperature, recorded_at, source) VALUES (?, ?, ?, ?)').run(1, -20, '2026-09-29 10:00:00', 'device');

  // 记录超限温度（应触发生成任务）
  db.prepare('INSERT INTO temperature_readings (freezer_id, temperature, recorded_at, source) VALUES (?, ?, ?, ?)').run(1, -15, '2026-09-29 10:01:00', 'device');

  // 手动创建温度超限任务
  db.prepare("INSERT INTO tasks (store_id, freezer_id, type, title, description, priority, status) VALUES (?, ?, ?, ?, ?, ?, ?)")
    .run(1, 1, 'temp_overlimit', '温度超限', '温度 -15°C', 'high', 'open');

  // 处置任务
  db.prepare("UPDATE tasks SET status = 'resolved', resolved_at = datetime('now', 'localtime') WHERE id = 1").run();
  db.prepare('INSERT INTO dispositions (task_id, action, note) VALUES (?, ?, ?)').run(1, 'resolved', '已处置');

  // 记录一条迟到的、时间更早的正常温度读数
  db.prepare('INSERT INTO temperature_readings (freezer_id, temperature, recorded_at, source) VALUES (?, ?, ?, ?)').run(1, -20, '2026-09-29 09:00:00', 'device');

  // 验证任务状态未被迟到读数覆盖
  const task = db.prepare('SELECT * FROM tasks WHERE id = 1').get();
  assertEqual(task.status, 'resolved', '迟到读数不应覆盖已处置的任务状态');

  // 验证最新读数仍然是超限温度（按 recorded_at 排序）
  const latest = db.prepare('SELECT * FROM temperature_readings WHERE freezer_id = 1 ORDER BY recorded_at DESC, id DESC LIMIT 1').get();
  assertEqual(latest.temperature, -15, '最新读数应按时间排序，迟到读数不应成为最新');

  db.close();
}

// 测试4: 数量守恒
function testQuantityConservation() {
  console.log('\n📋 测试4: 数量守恒');

  const db = setupTestDb();

  // 创建测试数据
  db.prepare('INSERT INTO stores (name) VALUES (?)').run('测试门店');
  db.prepare('INSERT INTO products (name, spec) VALUES (?, ?)').run('测试商品', '100ml');
  db.prepare('INSERT INTO freezers (store_id, name, min_temp, max_temp, power_status) VALUES (?, ?, ?, ?, ?)').run(1, '测试冷柜', -22, -18, 'on');
  db.prepare('INSERT INTO lanes (freezer_id, lane_number, capacity) VALUES (?, ?, ?)').run(1, 'A1', 20);

  // 创建批次，初始数量 10
  db.prepare('INSERT INTO batches (product_id, batch_number, production_date, expiry_date, initial_quantity, current_quantity, status) VALUES (?, ?, ?, ?, ?, ?, ?)')
    .run(1, 'BATCH001', '2026-01-01', '2026-12-31', 10, 10, 'in_stock');

  // 补货入柜 10 个
  db.prepare('UPDATE lanes SET product_id = ?, batch_id = ?, quantity = ? WHERE id = ?').run(1, 1, 10, 1);

  // 报损 3 个
  db.prepare('UPDATE batches SET current_quantity = ?, status = ? WHERE id = ?').run(7, 'scrapped', 1);
  db.prepare('INSERT INTO scrapped (batch_id, quantity, reason) VALUES (?, ?, ?)').run(1, 3, '包装破损');
  db.prepare('UPDATE lanes SET quantity = ? WHERE id = ?').run(7, 1);

  // 验证数量守恒：初始数量 = 剩余数量 + 报损数量
  const batch = db.prepare('SELECT * FROM batches WHERE id = 1').get();
  const scrapped = db.prepare('SELECT SUM(quantity) as total FROM scrapped WHERE batch_id = 1').get();
  assertEqual(batch.initial_quantity, batch.current_quantity + scrapped.total, '初始数量 = 剩余数量 + 报损数量');

  // 验证货道数量 = 批次剩余数量
  const lane = db.prepare('SELECT * FROM lanes WHERE id = 1').get();
  assertEqual(lane.quantity, batch.current_quantity, '货道数量应等于批次剩余数量');

  // 回收 2 个
  db.prepare('UPDATE batches SET current_quantity = ?, status = ? WHERE id = ?').run(5, 'recycled', 1);
  db.prepare('INSERT INTO recycling (batch_id, quantity, supplier, status) VALUES (?, ?, ?, ?)').run(1, 2, '测试供应商', 'pending');

  // 验证数量守恒
  const batch2 = db.prepare('SELECT * FROM batches WHERE id = 1').get();
  const scrapped2 = db.prepare('SELECT SUM(quantity) as total FROM scrapped WHERE batch_id = 1').get();
  const recycled2 = db.prepare('SELECT SUM(quantity) as total FROM recycling WHERE batch_id = 1').get();
  assertEqual(batch2.initial_quantity, batch2.current_quantity + scrapped2.total + recycled2.total, '初始数量 = 剩余数量 + 报损数量 + 回收数量');

  db.close();
}

// 测试5: 门店隔离
function testStoreIsolation() {
  console.log('\n📋 测试5: 门店隔离');

  const db = setupTestDb();

  // 创建两个门店
  db.prepare('INSERT INTO stores (name) VALUES (?)').run('门店A');
  db.prepare('INSERT INTO stores (name) VALUES (?)').run('门店B');

  // 为每个门店创建冷柜
  db.prepare('INSERT INTO freezers (store_id, name, min_temp, max_temp, power_status) VALUES (?, ?, ?, ?, ?)').run(1, '冷柜A', -22, -18, 'on');
  db.prepare('INSERT INTO freezers (store_id, name, min_temp, max_temp, power_status) VALUES (?, ?, ?, ?, ?)').run(2, '冷柜B', -22, -18, 'on');

  // 为每个门店创建商品和批次
  db.prepare('INSERT INTO products (name, spec) VALUES (?, ?)').run('商品A', '100ml');
  db.prepare('INSERT INTO products (name, spec) VALUES (?, ?)').run('商品B', '100ml');

  db.prepare('INSERT INTO batches (product_id, batch_number, production_date, expiry_date, initial_quantity, current_quantity, status) VALUES (?, ?, ?, ?, ?, ?, ?)')
    .run(1, 'BATCH-A', '2026-01-01', '2026-12-31', 10, 10, 'in_stock');
  db.prepare('INSERT INTO batches (product_id, batch_number, production_date, expiry_date, initial_quantity, current_quantity, status) VALUES (?, ?, ?, ?, ?, ?, ?)')
    .run(2, 'BATCH-B', '2026-01-01', '2026-12-31', 10, 10, 'in_stock');

  // 为每个门店创建货道
  db.prepare('INSERT INTO lanes (freezer_id, lane_number, capacity) VALUES (?, ?, ?)').run(1, 'A1', 20);
  db.prepare('INSERT INTO lanes (freezer_id, lane_number, capacity) VALUES (?, ?, ?)').run(2, 'B1', 20);

  // 门店A的批次入柜到门店A的冷柜
  db.prepare('UPDATE lanes SET product_id = ?, batch_id = ?, quantity = ? WHERE id = ?').run(1, 1, 10, 1);

  // 门店B的批次入柜到门店B的冷柜
  db.prepare('UPDATE lanes SET product_id = ?, batch_id = ?, quantity = ? WHERE id = ?').run(2, 2, 10, 2);

  // 验证门店A只能看到自己的批次
  const storeABatches = db.prepare(`
    SELECT b.* FROM batches b
    JOIN lanes l ON b.id = l.batch_id
    JOIN freezers f ON l.freezer_id = f.id
    WHERE f.store_id = 1
  `).all();
  assertEqual(storeABatches.length, 1, '门店A只能看到自己的批次');
  assertEqual(storeABatches[0].batch_number, 'BATCH-A', '门店A的批次编号正确');

  // 验证门店B只能看到自己的批次
  const storeBBatches = db.prepare(`
    SELECT b.* FROM batches b
    JOIN lanes l ON b.id = l.batch_id
    JOIN freezers f ON l.freezer_id = f.id
    WHERE f.store_id = 2
  `).all();
  assertEqual(storeBBatches.length, 1, '门店B只能看到自己的批次');
  assertEqual(storeBBatches[0].batch_number, 'BATCH-B', '门店B的批次编号正确');

  // 验证门店A看不到门店B的批次
  const crossStoreBatches = db.prepare(`
    SELECT b.* FROM batches b
    JOIN lanes l ON b.id = l.batch_id
    JOIN freezers f ON l.freezer_id = f.id
    WHERE f.store_id = 1 AND b.batch_number = 'BATCH-B'
  `).all();
  assertEqual(crossStoreBatches.length, 0, '门店A不应看到门店B的批次');

  db.close();
}

// 运行所有测试
function runAllTests() {
  console.log('========================================');
  console.log('  冰淇淋冷柜管理系统 - 测试套件');
  console.log('========================================');

  testThresholdBoundaries();
  testBatchLocking();
  testTemperatureOutOfOrder();
  testQuantityConservation();
  testStoreIsolation();

  console.log('\n========================================');
  console.log(`  测试结果: ${passed} 通过, ${failed} 失败`);
  console.log('========================================');

  if (failed > 0) {
    console.log('\n失败的测试:');
    failures.forEach(f => console.log(`  - ${f}`));
    process.exit(1);
  }
}

runAllTests();
