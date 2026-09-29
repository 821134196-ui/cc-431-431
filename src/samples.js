// 样品数据生成器 - 生成温控、临期和回收样例
const { query, queryOne, execute, transaction } = require('./db');
const { recordReading } = require('./device-adapter');
const { daysBetween } = require('./task-engine');

// 生成温度样品数据
function generateTemperatureSamples() {
  const freezers = query('SELECT * FROM freezers');
  const now = new Date();

  for (const freezer of freezers) {
    // 生成过去 24 小时的温度读数（每 30 分钟一条）
    for (let i = 48; i >= 0; i--) {
      const time = new Date(now.getTime() - i * 30 * 60 * 1000);
      const timeStr = time.toISOString().replace('T', ' ').substring(0, 19);

      let temp;
      if (freezer.power_status === 'off') {
        // 断电冷柜温度逐渐上升
        const hoursOff = i * 0.5;
        temp = -20 + hoursOff * 0.8 + (Math.random() - 0.5) * 0.5;
        temp = Math.min(temp, 10);
      } else {
        // 正常冷柜偶尔出现温度波动
        const base = (freezer.min_temp + freezer.max_temp) / 2;
        if (i === 10 || i === 5) {
          // 模拟温度超限
          temp = freezer.max_temp + 1.5 + (Math.random() - 0.5) * 0.5;
        } else {
          temp = base + (Math.random() - 0.5) * 1.5;
        }
      }
      temp = Math.round(temp * 100) / 100;
      recordReading(freezer.id, temp, timeStr, 'device');
    }
  }
}

// 生成临期样品数据
function generateNearExpirySamples() {
  const products = query('SELECT * FROM products');
  const lanes = query(`
    SELECT l.*, f.store_id, f.name as freezer_name
    FROM lanes l
    JOIN freezers f ON l.freezer_id = f.id
    WHERE l.quantity = 0
    LIMIT 8
  `);

  if (lanes.length === 0) return;

  const today = new Date();
  const nearExpiryDates = [
    new Date(today.getTime() + 2 * 24 * 60 * 60 * 1000),  // 2天后到期（紧急）
    new Date(today.getTime() + 5 * 24 * 60 * 60 * 1000),  // 5天后到期
    new Date(today.getTime() + 7 * 24 * 60 * 60 * 1000),  // 7天后到期
    new Date(today.getTime() + 1 * 24 * 60 * 60 * 1000),  // 1天后到期（紧急）
  ];

  transaction(() => {
    for (let i = 0; i < Math.min(lanes.length, 6); i++) {
      const lane = lanes[i];
      const product = products[i % products.length];
      const expiryDate = nearExpiryDates[i % nearExpiryDates.length];
      const productionDate = new Date(expiryDate.getTime() - 180 * 24 * 60 * 60 * 1000);

      const batchNumber = `BATCH${String(Date.now()).slice(-6)}${i}`;
      const quantity = 10 + Math.floor(Math.random() * 10);

      // 创建批次
      const batchResult = execute(
        'INSERT INTO batches (product_id, batch_number, production_date, expiry_date, initial_quantity, current_quantity, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
        [product.id, batchNumber,
         productionDate.toISOString().split('T')[0],
         expiryDate.toISOString().split('T')[0],
         quantity, quantity, 'in_stock']
      );
      const batchId = batchResult.lastInsertRowid;

      // 更新货道
      execute(
        'UPDATE lanes SET product_id = ?, batch_id = ?, quantity = ? WHERE id = ?',
        [product.id, batchId, quantity, lane.id]
      );

      // 记录补货
      execute(
        'INSERT INTO restocks (lane_id, batch_id, quantity) VALUES (?, ?, ?)',
        [lane.id, batchId, quantity]
      );
    }
  });
}

// 生成回收样品数据
function generateRecyclingSamples() {
  const batches = query(`
    SELECT b.*, p.name as product_name
    FROM batches b
    JOIN products p ON b.product_id = p.id
    WHERE b.status = 'in_stock'
    LIMIT 3
  `);

  if (batches.length === 0) return;

  const suppliers = ['光明供应商', '伊利供应商', '蒙牛供应商'];

  transaction(() => {
    for (let i = 0; i < batches.length; i++) {
      const batch = batches[i];
      const supplier = suppliers[i % suppliers.length];
      const quantity = Math.min(batch.current_quantity, 5 + Math.floor(Math.random() * 5));

      // 创建回收记录
      const result = execute(
        'INSERT INTO recycling (batch_id, quantity, supplier, status) VALUES (?, ?, ?, ?)',
        [batch.id, quantity, supplier, 'pending']
      );

      // 模拟部分回收已完成，部分有差异
      if (i === 0) {
        // 正常回收
        execute(
          "UPDATE recycling SET status = 'collected', collected_quantity = ? WHERE id = ?",
          [quantity, result.lastInsertRowid]
        );
      } else if (i === 1) {
        // 回收数量不符
        const collected = Math.max(1, quantity - 2);
        execute(
          "UPDATE recycling SET status = 'collected', collected_quantity = ? WHERE id = ?",
          [collected, result.lastInsertRowid]
        );
      }
      // i === 2 保持 pending
    }
  });
}

// 生成换货样品数据
function generateExchangeSamples() {
  const nearExpiryBatches = query(`
    SELECT b.*, p.name as product_name, l.id as lane_id, l.freezer_id, f.store_id
    FROM batches b
    JOIN products p ON b.product_id = p.id
    JOIN lanes l ON b.id = l.batch_id
    JOIN freezers f ON l.freezer_id = f.id
    WHERE b.status = 'in_stock'
      AND b.expiry_date <= date('now', '+7 days')
    LIMIT 2
  `);

  if (nearExpiryBatches.length === 0) return;

  transaction(() => {
    for (let i = 0; i < nearExpiryBatches.length; i++) {
      const batch = nearExpiryBatches[i];
      const quantity = Math.min(batch.current_quantity, 3 + Math.floor(Math.random() * 3));
      const orderNumber = `EX${String(Date.now()).slice(-8)}${i}`;

      const result = execute(
        'INSERT INTO exchange_orders (order_number, store_id, batch_id, lane_id, quantity, reason, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
        [orderNumber, batch.store_id, batch.id, batch.lane_id, quantity, 'near_expiry', 'pending']
      );

      // 部分换货单已确认
      if (i === 0) {
        execute(
          "UPDATE exchange_orders SET status = 'clerk_confirmed', clerk_confirmed_at = datetime('now', 'localtime') WHERE id = ?",
          [result.lastInsertRowid]
        );
      }
    }
  });
}

// 生成所有样品数据
function generateAllSamples() {
  generateTemperatureSamples();
  generateNearExpirySamples();
  generateRecyclingSamples();
  generateExchangeSamples();
}

module.exports = {
  generateTemperatureSamples,
  generateNearExpirySamples,
  generateRecyclingSamples,
  generateExchangeSamples,
  generateAllSamples
};
