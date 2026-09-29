// 任务计算引擎 - 处理温度超限、断电、批次混放、临期未换、包装破损、回收不符
const { query, queryOne, execute, transaction } = require('./db');

// 临期阈值（天）
const NEAR_EXPIRY_DAYS = 7;
const CRITICAL_EXPIRY_DAYS = 3;

// 计算两个日期之间的天数
function daysBetween(date1, date2) {
  const d1 = new Date(date1);
  const d2 = new Date(date2);
  return Math.ceil((d2 - d1) / (1000 * 60 * 60 * 24));
}

// 检查温度是否超限
function checkTemperature(freezerId) {
  const freezer = queryOne('SELECT * FROM freezers WHERE id = ?', [freezerId]);
  if (!freezer) return null;

  const latest = queryOne(
    'SELECT * FROM temperature_readings WHERE freezer_id = ? ORDER BY recorded_at DESC, id DESC LIMIT 1',
    [freezerId]
  );
  if (!latest) return null;

  // 检查是否已有未解决的温度任务
  const existing = queryOne(
    "SELECT * FROM tasks WHERE freezer_id = ? AND type = 'temp_overlimit' AND status != 'resolved' ORDER BY id DESC LIMIT 1",
    [freezerId]
  );

  if (latest.temperature > freezer.max_temp || latest.temperature < freezer.min_temp) {
    if (!existing) {
      const priority = Math.abs(latest.temperature - freezer.max_temp) > 3 || Math.abs(latest.temperature - freezer.min_temp) > 3 ? 'critical' : 'high';
      execute(
        'INSERT INTO tasks (store_id, freezer_id, type, title, description, priority) VALUES (?, ?, ?, ?, ?, ?)',
        [freezer.store_id, freezerId, 'temp_overlimit',
         `冷柜 ${freezer.name} 温度超限`,
         `当前温度 ${latest.temperature}°C，合格范围 ${freezer.min_temp}°C ~ ${freezer.max_temp}°C`,
         priority]
      );
      return { type: 'temp_overlimit', created: true };
    }
    return { type: 'temp_overlimit', created: false, existing: existing.id };
  } else {
    // 温度恢复正常，自动解决已有任务
    if (existing) {
      execute("UPDATE tasks SET status = 'resolved', resolved_at = datetime('now', 'localtime') WHERE id = ?", [existing.id]);
      execute('INSERT INTO dispositions (task_id, action, note) VALUES (?, ?, ?)',
        [existing.id, 'resolved', '温度恢复正常范围，自动解决']);
      return { type: 'temp_overlimit', resolved: true };
    }
  }
  return null;
}

// 检查断电
function checkPower(freezerId) {
  const freezer = queryOne('SELECT * FROM freezers WHERE id = ?', [freezerId]);
  if (!freezer) return null;

  const existing = queryOne(
    "SELECT * FROM tasks WHERE freezer_id = ? AND type = 'power_off' AND status != 'resolved' ORDER BY id DESC LIMIT 1",
    [freezerId]
  );

  if (freezer.power_status === 'off') {
    if (!existing) {
      execute(
        'INSERT INTO tasks (store_id, freezer_id, type, title, description, priority) VALUES (?, ?, ?, ?, ?, ?)',
        [freezer.store_id, freezerId, 'power_off',
         `冷柜 ${freezer.name} 断电`,
         `冷柜 ${freezer.name} 电源已关闭，温度可能上升`,
         'critical']
      );
      return { type: 'power_off', created: true };
    }
    return { type: 'power_off', created: false, existing: existing.id };
  } else {
    if (existing) {
      execute("UPDATE tasks SET status = 'resolved', resolved_at = datetime('now', 'localtime') WHERE id = ?", [existing.id]);
      execute('INSERT INTO dispositions (task_id, action, note) VALUES (?, ?, ?)',
        [existing.id, 'resolved', '电源恢复，自动解决']);
      return { type: 'power_off', resolved: true };
    }
  }
  return null;
}

// 检查批次混放（同一货道有多个批次）
function checkBatchMixing() {
  const mixedLanes = query(`
    SELECT l.id, l.freezer_id, l.lane_number, f.store_id, f.name as freezer_name,
           COUNT(DISTINCT l.batch_id) as batch_count
    FROM lanes l
    JOIN freezers f ON l.freezer_id = f.id
    WHERE l.batch_id IS NOT NULL AND l.quantity > 0
    GROUP BY l.id
    HAVING batch_count > 1
  `);

  const results = [];
  for (const lane of mixedLanes) {
    const existing = queryOne(
      "SELECT * FROM tasks WHERE freezer_id = ? AND type = 'batch_mixing' AND status != 'resolved' ORDER BY id DESC LIMIT 1",
      [lane.freezer_id]
    );
    if (!existing) {
      execute(
        'INSERT INTO tasks (store_id, freezer_id, type, title, description, priority) VALUES (?, ?, ?, ?, ?, ?)',
        [lane.store_id, lane.freezer_id, 'batch_mixing',
         `冷柜 ${lane.freezer_name} 货道 ${lane.lane_number} 批次混放`,
         `货道 ${lane.lane_number} 存在 ${lane.batch_count} 个不同批次`,
         'high']
      );
      results.push({ type: 'batch_mixing', created: true, lane: lane.lane_number });
    }
  }
  return results;
}

// 检查临期未换
function checkNearExpiry() {
  const today = new Date().toISOString().split('T')[0];
  const nearExpiryDate = new Date();
  nearExpiryDate.setDate(nearExpiryDate.getDate() + NEAR_EXPIRY_DAYS);
  const nearExpiryStr = nearExpiryDate.toISOString().split('T')[0];

  // 查找在库批次中临期但没有换货单的
  const nearExpiryBatches = query(`
    SELECT b.id, b.batch_number, b.expiry_date, b.current_quantity,
           p.name as product_name,
           l.id as lane_id, l.lane_number, l.freezer_id,
           f.store_id, f.name as freezer_name
    FROM batches b
    JOIN products p ON b.product_id = p.id
    JOIN lanes l ON b.id = l.batch_id AND l.quantity > 0
    JOIN freezers f ON l.freezer_id = f.id
    WHERE b.status = 'in_stock'
      AND b.expiry_date <= ?
      AND b.expiry_date > ?
      AND NOT EXISTS (
        SELECT 1 FROM exchange_orders eo
        WHERE eo.batch_id = b.id AND eo.status NOT IN ('cancelled', 'completed')
      )
  `, [nearExpiryStr, today]);

  const results = [];
  for (const batch of nearExpiryBatches) {
    const daysLeft = daysBetween(today, batch.expiry_date);
    const priority = daysLeft <= CRITICAL_EXPIRY_DAYS ? 'critical' : 'high';

    const existing = queryOne(
      "SELECT * FROM tasks WHERE type = 'near_expiry' AND status != 'resolved' AND description LIKE ? ORDER BY id DESC LIMIT 1",
      [`%批次 ${batch.batch_number}%`]
    );

    if (!existing) {
      execute(
        'INSERT INTO tasks (store_id, freezer_id, type, title, description, priority) VALUES (?, ?, ?, ?, ?, ?)',
        [batch.store_id, batch.freezer_id, 'near_expiry',
         `临期商品：${batch.product_name}`,
         `批次 ${batch.batch_number}（${batch.product_name}）剩余 ${daysLeft} 天到期，数量 ${batch.current_quantity}，位于 ${batch.freezer_name} 货道 ${batch.lane_number}`,
         priority]
      );
      results.push({ type: 'near_expiry', created: true, batch: batch.batch_number, daysLeft });
    }
  }
  return results;
}

// 检查包装破损
function checkPackagingDamage() {
  // 查找有包装破损换货单但未处理的
  const damaged = query(`
    SELECT eo.id, eo.batch_id, eo.quantity, eo.status,
           b.batch_number, p.name as product_name,
           f.store_id, f.name as freezer_name
    FROM exchange_orders eo
    JOIN batches b ON eo.batch_id = b.id
    JOIN products p ON b.product_id = p.id
    JOIN lanes l ON eo.lane_id = l.id
    JOIN freezers f ON l.freezer_id = f.id
    WHERE eo.reason = 'packaging_damage'
      AND eo.status = 'pending'
  `);

  const results = [];
  for (const item of damaged) {
    const existing = queryOne(
      "SELECT * FROM tasks WHERE type = 'packaging_damage' AND status != 'resolved' AND description LIKE ? ORDER BY id DESC LIMIT 1",
      [`%换货单 ${item.id}%`]
    );
    if (!existing) {
      execute(
        'INSERT INTO tasks (store_id, freezer_id, type, title, description, priority) VALUES (?, ?, ?, ?, ?, ?)',
        [item.store_id, null, 'packaging_damage',
         `包装破损：${item.product_name}`,
         `换货单 ${item.id}（批次 ${item.batch_number}）包装破损，数量 ${item.quantity}，等待处理`,
         'high']
      );
      results.push({ type: 'packaging_damage', created: true, orderId: item.id });
    }
  }
  return results;
}

// 检查回收数量不符
function checkRecyclingMismatch() {
  const mismatched = query(`
    SELECT r.id, r.batch_id, r.quantity, r.collected_quantity, r.status,
           b.batch_number, p.name as product_name
    FROM recycling r
    JOIN batches b ON r.batch_id = b.id
    JOIN products p ON b.product_id = p.id
    WHERE r.status = 'collected'
      AND r.collected_quantity IS NOT NULL
      AND r.collected_quantity != r.quantity
  `);

  const results = [];
  for (const item of mismatched) {
    const existing = queryOne(
      "SELECT * FROM tasks WHERE type = 'recycling_mismatch' AND status != 'resolved' AND description LIKE ? ORDER BY id DESC LIMIT 1",
      [`%回收单 ${item.id}%`]
    );
    if (!existing) {
      const diff = item.quantity - (item.collected_quantity || 0);
      execute(
        'INSERT INTO tasks (store_id, freezer_id, type, title, description, priority) VALUES (?, ?, ?, ?, ?, ?)',
        [null, null, 'recycling_mismatch',
         `回收数量不符：${item.product_name}`,
         `回收单 ${item.id}（批次 ${item.batch_number}）应回收 ${item.quantity}，实际回收 ${item.collected_quantity}，差异 ${diff}`,
         'high']
      );
      results.push({ type: 'recycling_mismatch', created: true, recyclingId: item.id, diff });
    }
  }
  return results;
}

// 运行所有检查
function runAllChecks() {
  const results = [];

  // 检查所有冷柜的温度和断电
  const freezers = query('SELECT id FROM freezers');
  for (const f of freezers) {
    const tempResult = checkTemperature(f.id);
    if (tempResult) results.push(tempResult);
    const powerResult = checkPower(f.id);
    if (powerResult) results.push(powerResult);
  }

  // 检查批次混放
  results.push(...checkBatchMixing());

  // 检查临期
  results.push(...checkNearExpiry());

  // 检查包装破损
  results.push(...checkPackagingDamage());

  // 检查回收不符
  results.push(...checkRecyclingMismatch());

  return results;
}

// 启动定时任务计算
function startTaskEngine(intervalMs = 60000) {
  const tick = () => {
    try {
      runAllChecks();
    } catch (e) {
      console.error('任务计算出错:', e.message);
    }
    setTimeout(tick, intervalMs);
  };
  tick();
}

module.exports = {
  checkTemperature,
  checkPower,
  checkBatchMixing,
  checkNearExpiry,
  checkPackagingDamage,
  checkRecyclingMismatch,
  runAllChecks,
  startTaskEngine,
  daysBetween,
  NEAR_EXPIRY_DAYS,
  CRITICAL_EXPIRY_DAYS
};
