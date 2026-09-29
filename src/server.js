// HTTP 服务器 - 冰淇淋冷柜温度与批次换货系统
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { initDb, query, queryOne, execute, transaction, closeDb } = require('./db');
const { seed } = require('./seed');
const { startAllDevices, manualReading, recordReading } = require('./device-adapter');
const { runAllChecks, checkTemperature, checkPower, daysBetween } = require('./task-engine');
const { generateAllSamples } = require('./samples');

const PORT = process.env.PORT || 3000;
const PUBLIC_DIR = path.join(__dirname, '..', 'public');

// 初始化数据库和种子数据
initDb();
seed();

// 工具函数
function sendJson(res, data, statusCode = 200) {
  res.writeHead(statusCode, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(data));
}

function sendError(res, message, statusCode = 400) {
  sendJson(res, { error: message }, statusCode);
}

function parseBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch (e) {
        reject(new Error('无效的 JSON'));
      }
    });
  });
}

// 路由处理
async function handleRequest(req, res) {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const pathname = url.pathname;
  const method = req.method;

  // 静态文件
  if (method === 'GET' && !pathname.startsWith('/api/')) {
    let filePath = path.join(PUBLIC_DIR, pathname === '/' ? 'index.html' : pathname);
    if (!fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
      filePath = path.join(PUBLIC_DIR, 'index.html');
    }
    const ext = path.extname(filePath);
    const contentTypes = {
      '.html': 'text/html; charset=utf-8',
      '.css': 'text/css; charset=utf-8',
      '.js': 'application/javascript; charset=utf-8',
      '.json': 'application/json; charset=utf-8',
      '.png': 'image/png',
      '.jpg': 'image/jpeg',
      '.svg': 'image/svg+xml'
    };
    res.writeHead(200, { 'Content-Type': contentTypes[ext] || 'application/octet-stream' });
    fs.createReadStream(filePath).pipe(res);
    return;
  }

  // API 路由
  try {
    // 门店
    if (pathname === '/api/stores' && method === 'GET') {
      const stores = query('SELECT * FROM stores ORDER BY id');
      sendJson(res, stores);
      return;
    }

    // 冷柜总览
    if (pathname === '/api/freezers' && method === 'GET') {
      const storeId = url.searchParams.get('store_id');
      let freezers;
      if (storeId) {
        freezers = query(`
          SELECT f.*, s.name as store_name,
            (SELECT temperature FROM temperature_readings WHERE freezer_id = f.id ORDER BY recorded_at DESC, id DESC LIMIT 1) as current_temp,
            (SELECT COUNT(*) FROM lanes WHERE freezer_id = f.id AND quantity > 0) as stocked_lanes,
            (SELECT COUNT(*) FROM lanes WHERE freezer_id = f.id) as total_lanes
          FROM freezers f
          JOIN stores s ON f.store_id = s.id
          WHERE f.store_id = ?
          ORDER BY f.id
        `, [storeId]);
      } else {
        freezers = query(`
          SELECT f.*, s.name as store_name,
            (SELECT temperature FROM temperature_readings WHERE freezer_id = f.id ORDER BY recorded_at DESC, id DESC LIMIT 1) as current_temp,
            (SELECT COUNT(*) FROM lanes WHERE freezer_id = f.id AND quantity > 0) as stocked_lanes,
            (SELECT COUNT(*) FROM lanes WHERE freezer_id = f.id) as total_lanes
          FROM freezers f
          JOIN stores s ON f.store_id = s.id
          ORDER BY f.id
        `);
      }
      sendJson(res, freezers);
      return;
    }

    // 单个冷柜详情
    if (pathname.match(/^\/api\/freezers\/\d+$/) && method === 'GET') {
      const id = pathname.split('/').pop();
      const freezer = queryOne(`
        SELECT f.*, s.name as store_name
        FROM freezers f
        JOIN stores s ON f.store_id = s.id
        WHERE f.id = ?
      `, [id]);
      if (!freezer) {
        sendError(res, '冷柜不存在', 404);
        return;
      }
      sendJson(res, freezer);
      return;
    }

    // 货道库存
    if (pathname.match(/^\/api\/freezers\/\d+\/lanes$/) && method === 'GET') {
      const freezerId = pathname.split('/')[3];
      const lanes = query(`
        SELECT l.*, p.name as product_name, p.spec as product_spec,
          b.batch_number, b.production_date, b.expiry_date, b.status as batch_status,
          b.current_quantity as batch_quantity
        FROM lanes l
        LEFT JOIN products p ON l.product_id = p.id
        LEFT JOIN batches b ON l.batch_id = b.id
        WHERE l.freezer_id = ?
        ORDER BY l.lane_number
      `, [freezerId]);
      sendJson(res, lanes);
      return;
    }

    // 温度曲线数据
    if (pathname.match(/^\/api\/freezers\/\d+\/temperatures$/) && method === 'GET') {
      const freezerId = pathname.split('/')[3];
      const hours = parseInt(url.searchParams.get('hours') || '24');
      const since = new Date(Date.now() - hours * 60 * 60 * 1000).toISOString().replace('T', ' ').substring(0, 19);
      const readings = query(`
        SELECT id, temperature, recorded_at, source
        FROM temperature_readings
        WHERE freezer_id = ? AND recorded_at >= ?
        ORDER BY recorded_at ASC, id ASC
      `, [freezerId, since]);
      sendJson(res, readings);
      return;
    }

    // 记录温度读数
    if (pathname.match(/^\/api\/freezers\/\d+\/temperatures$/) && method === 'POST') {
      const freezerId = pathname.split('/')[3];
      const body = await parseBody(req);
      if (body.temperature === undefined) {
        sendError(res, '缺少温度参数');
        return;
      }
      const id = manualReading(freezerId, body.temperature, body.recorded_at);
      // 检查温度状态
      checkTemperature(freezerId);
      sendJson(res, { id, message: '温度读数已记录' });
      return;
    }

    // 批次列表
    if (pathname === '/api/batches' && method === 'GET') {
      const status = url.searchParams.get('status');
      const storeId = url.searchParams.get('store_id');
      let sql = `
        SELECT b.*, p.name as product_name, p.spec as product_spec,
          l.lane_number, f.name as freezer_name, s.name as store_name
        FROM batches b
        JOIN products p ON b.product_id = p.id
        LEFT JOIN lanes l ON b.id = l.batch_id
        LEFT JOIN freezers f ON l.freezer_id = f.id
        LEFT JOIN stores s ON f.store_id = s.id
        WHERE 1=1
      `;
      const params = [];
      if (status) {
        sql += ' AND b.status = ?';
        params.push(status);
      }
      if (storeId) {
        sql += ' AND s.id = ?';
        params.push(storeId);
      }
      sql += ' ORDER BY b.expiry_date ASC';
      const batches = query(sql, params);
      sendJson(res, batches);
      return;
    }

    // 临期队列
    if (pathname === '/api/batches/near-expiry' && method === 'GET') {
      const storeId = url.searchParams.get('store_id');
      const days = parseInt(url.searchParams.get('days') || '7');
      const today = new Date().toISOString().split('T')[0];
      const expiryDate = new Date(Date.now() + days * 24 * 60 * 60 * 1000).toISOString().split('T')[0];

      let sql = `
        SELECT b.*, p.name as product_name, p.spec as product_spec,
          l.lane_number, f.name as freezer_name, s.name as store_name, s.id as store_id
        FROM batches b
        JOIN products p ON b.product_id = p.id
        JOIN lanes l ON b.id = l.batch_id AND l.quantity > 0
        JOIN freezers f ON l.freezer_id = f.id
        JOIN stores s ON f.store_id = s.id
        WHERE b.status = 'in_stock'
          AND b.expiry_date <= ?
          AND b.expiry_date >= ?
      `;
      const params = [expiryDate, today];
      if (storeId) {
        sql += ' AND s.id = ?';
        params.push(storeId);
      }
      sql += ' ORDER BY b.expiry_date ASC';
      const batches = query(sql, params);

      // 计算剩余天数和分层
      const result = batches.map(b => {
        const daysLeft = daysBetween(today, b.expiry_date);
        let layer = 'normal';
        if (daysLeft <= 1) layer = 'critical';
        else if (daysLeft <= 3) layer = 'urgent';
        else if (daysLeft <= 7) layer = 'warning';
        return { ...b, days_left: daysLeft, layer };
      });

      sendJson(res, result);
      return;
    }

    // 补货
    if (pathname === '/api/restocks' && method === 'POST') {
      const body = await parseBody(req);
      const { lane_id, batch_id, quantity } = body;
      if (!lane_id || !batch_id || !quantity) {
        sendError(res, '缺少必要参数');
        return;
      }

      try {
        const result = transaction(() => {
          // 锁定货道和批次
          const lane = queryOne('SELECT * FROM lanes WHERE id = ?', [lane_id]);
          const batch = queryOne('SELECT * FROM batches WHERE id = ?', [batch_id]);

          if (!lane) throw new Error('货道不存在');
          if (!batch) throw new Error('批次不存在');
          if (batch.status !== 'in_stock') throw new Error('批次已锁定，无法补货');

          // 检查货道是否已有其他批次（批次混放检查）
          if (lane.batch_id && lane.batch_id !== batch_id) {
            throw new Error('该货道已有其他批次，禁止混放');
          }

          // 检查容量
          const newQuantity = lane.quantity + quantity;
          if (newQuantity > lane.capacity) {
            throw new Error(`超过货道容量，当前 ${lane.quantity}，容量 ${lane.capacity}`);
          }

          // 更新货道
          execute('UPDATE lanes SET product_id = ?, batch_id = ?, quantity = ? WHERE id = ?',
            [batch.product_id, batch_id, newQuantity, lane_id]);

          // 记录补货
          const restockResult = execute(
            'INSERT INTO restocks (lane_id, batch_id, quantity) VALUES (?, ?, ?)',
            [lane_id, batch_id, quantity]
          );

          return { restockId: restockResult.lastInsertRowid, newQuantity };
        });

        sendJson(res, { message: '补货成功', ...result });
      } catch (e) {
        sendError(res, e.message);
      }
      return;
    }

    // 换货单列表
    if (pathname === '/api/exchange-orders' && method === 'GET') {
      const status = url.searchParams.get('status');
      const storeId = url.searchParams.get('store_id');
      let sql = `
        SELECT eo.*, b.batch_number, b.expiry_date, p.name as product_name, p.spec as product_spec,
          l.lane_number, f.name as freezer_name, s.name as store_name
        FROM exchange_orders eo
        JOIN batches b ON eo.batch_id = b.id
        JOIN products p ON b.product_id = p.id
        LEFT JOIN lanes l ON eo.lane_id = l.id
        LEFT JOIN freezers f ON l.freezer_id = f.id
        LEFT JOIN stores s ON eo.store_id = s.id
        WHERE 1=1
      `;
      const params = [];
      if (status) {
        sql += ' AND eo.status = ?';
        params.push(status);
      }
      if (storeId) {
        sql += ' AND eo.store_id = ?';
        params.push(storeId);
      }
      sql += ' ORDER BY eo.created_at DESC';
      const orders = query(sql, params);
      sendJson(res, orders);
      return;
    }

    // 创建换货单
    if (pathname === '/api/exchange-orders' && method === 'POST') {
      const body = await parseBody(req);
      const { store_id, batch_id, lane_id, quantity, reason } = body;
      if (!store_id || !batch_id || !quantity || !reason) {
        sendError(res, '缺少必要参数');
        return;
      }

      try {
        const result = transaction(() => {
          const batch = queryOne('SELECT * FROM batches WHERE id = ?', [batch_id]);
          if (!batch) throw new Error('批次不存在');
          if (batch.status !== 'in_stock') throw new Error('批次已锁定，无法换货');

          // 检查是否有未完成的换货单
          const existing = queryOne(
            "SELECT * FROM exchange_orders WHERE batch_id = ? AND status NOT IN ('cancelled', 'completed')",
            [batch_id]
          );
          if (existing) throw new Error('该批次已有未完成的换货单');

          const orderNumber = `EX${Date.now()}${Math.floor(Math.random() * 1000)}`;
          const orderResult = execute(
            'INSERT INTO exchange_orders (order_number, store_id, batch_id, lane_id, quantity, reason, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
            [orderNumber, store_id, batch_id, lane_id || null, quantity, reason, 'pending']
          );

          // 锁定批次
          execute("UPDATE batches SET status = 'locked', locked_reason = ? WHERE id = ?",
            [`换货单 ${orderNumber}`, batch_id]);

          return { orderId: orderResult.lastInsertRowid, orderNumber };
        });

        sendJson(res, { message: '换货单创建成功', ...result });
      } catch (e) {
        sendError(res, e.message);
      }
      return;
    }

    // 换货单确认（店员/配送员双向确认）
    if (pathname.match(/^\/api\/exchange-orders\/\d+\/confirm$/) && method === 'POST') {
      const orderId = pathname.split('/')[3];
      const body = await parseBody(req);
      const { role } = body; // 'clerk' 或 'delivery'

      if (!role || !['clerk', 'delivery'].includes(role)) {
        sendError(res, '无效的角色');
        return;
      }

      try {
        const result = transaction(() => {
          const order = queryOne('SELECT * FROM exchange_orders WHERE id = ?', [orderId]);
          if (!order) throw new Error('换货单不存在');

          if (role === 'clerk') {
            if (order.status !== 'pending') throw new Error('当前状态无法确认');
            execute("UPDATE exchange_orders SET status = 'clerk_confirmed', clerk_confirmed_at = datetime('now', 'localtime') WHERE id = ?", [orderId]);
            return { status: 'clerk_confirmed' };
          } else {
            if (order.status !== 'clerk_confirmed') throw new Error('请先由店员确认');
            execute("UPDATE exchange_orders SET status = 'delivery_confirmed', delivery_confirmed_at = datetime('now', 'localtime') WHERE id = ?", [orderId]);
            return { status: 'delivery_confirmed' };
          }
        });

        sendJson(res, { message: '确认成功', ...result });
      } catch (e) {
        sendError(res, e.message);
      }
      return;
    }

    // 完成换货单
    if (pathname.match(/^\/api\/exchange-orders\/\d+\/complete$/) && method === 'POST') {
      const orderId = pathname.split('/')[3];

      try {
        const result = transaction(() => {
          const order = queryOne('SELECT * FROM exchange_orders WHERE id = ?', [orderId]);
          if (!order) throw new Error('换货单不存在');
          if (order.status !== 'delivery_confirmed') throw new Error('请先完成双向确认');

          // 扣减批次库存
          const batch = queryOne('SELECT * FROM batches WHERE id = ?', [order.batch_id]);
          const newQuantity = batch.current_quantity - order.quantity;
          if (newQuantity < 0) throw new Error('数量不足');

          execute('UPDATE batches SET current_quantity = ?, status = ? WHERE id = ?',
            [newQuantity, 'exchanged', order.batch_id]);

          // 扣减货道库存
          if (order.lane_id) {
            const lane = queryOne('SELECT * FROM lanes WHERE id = ?', [order.lane_id]);
            if (lane) {
              const laneQty = Math.max(0, lane.quantity - order.quantity);
              execute('UPDATE lanes SET quantity = ? WHERE id = ?', [laneQty, order.lane_id]);
            }
          }

          execute("UPDATE exchange_orders SET status = 'completed' WHERE id = ?", [orderId]);
          return { newQuantity };
        });

        sendJson(res, { message: '换货完成', ...result });
      } catch (e) {
        sendError(res, e.message);
      }
      return;
    }

    // 报损
    if (pathname === '/api/scrapped' && method === 'POST') {
      const body = await parseBody(req);
      const { batch_id, quantity, reason } = body;
      if (!batch_id || !quantity) {
        sendError(res, '缺少必要参数');
        return;
      }

      try {
        const result = transaction(() => {
          const batch = queryOne('SELECT * FROM batches WHERE id = ?', [batch_id]);
          if (!batch) throw new Error('批次不存在');
          if (batch.status !== 'in_stock') throw new Error('批次已锁定，无法报损');

          const newQuantity = batch.current_quantity - quantity;
          if (newQuantity < 0) throw new Error('报损数量超过库存');

          execute('UPDATE batches SET current_quantity = ?, status = ? WHERE id = ?',
            [newQuantity, 'scrapped', batch_id]);

          const scrappedResult = execute(
            'INSERT INTO scrapped (batch_id, quantity, reason) VALUES (?, ?, ?)',
            [batch_id, quantity, reason || null]
          );

          // 扣减货道库存
          const lane = queryOne('SELECT * FROM lanes WHERE batch_id = ? AND quantity > 0 LIMIT 1', [batch_id]);
          if (lane) {
            const laneQty = Math.max(0, lane.quantity - quantity);
            execute('UPDATE lanes SET quantity = ? WHERE id = ?', [laneQty, lane.id]);
          }

          return { scrappedId: scrappedResult.lastInsertRowid, newQuantity };
        });

        sendJson(res, { message: '报损成功', ...result });
      } catch (e) {
        sendError(res, e.message);
      }
      return;
    }

    // 回收记录列表
    if (pathname === '/api/recycling' && method === 'GET') {
      const status = url.searchParams.get('status');
      let sql = `
        SELECT r.*, b.batch_number, b.expiry_date, p.name as product_name, p.spec as product_spec
        FROM recycling r
        JOIN batches b ON r.batch_id = b.id
        JOIN products p ON b.product_id = p.id
        WHERE 1=1
      `;
      const params = [];
      if (status) {
        sql += ' AND r.status = ?';
        params.push(status);
      }
      sql += ' ORDER BY r.created_at DESC';
      const records = query(sql, params);
      sendJson(res, records);
      return;
    }

    // 创建回收记录
    if (pathname === '/api/recycling' && method === 'POST') {
      const body = await parseBody(req);
      const { batch_id, quantity, supplier } = body;
      if (!batch_id || !quantity || !supplier) {
        sendError(res, '缺少必要参数');
        return;
      }

      try {
        const result = transaction(() => {
          const batch = queryOne('SELECT * FROM batches WHERE id = ?', [batch_id]);
          if (!batch) throw new Error('批次不存在');
          if (batch.status !== 'in_stock') throw new Error('批次已锁定，无法回收');

          const newQuantity = batch.current_quantity - quantity;
          if (newQuantity < 0) throw new Error('回收数量超过库存');

          execute('UPDATE batches SET current_quantity = ?, status = ? WHERE id = ?',
            [newQuantity, 'recycled', batch_id]);

          const recyclingResult = execute(
            'INSERT INTO recycling (batch_id, quantity, supplier, status) VALUES (?, ?, ?, ?)',
            [batch_id, quantity, supplier, 'pending']
          );

          return { recyclingId: recyclingResult.lastInsertRowid };
        });

        sendJson(res, { message: '回收记录创建成功', ...result });
      } catch (e) {
        sendError(res, e.message);
      }
      return;
    }

    // 回收确认
    if (pathname.match(/^\/api\/recycling\/\d+\/collect$/) && method === 'POST') {
      const recyclingId = pathname.split('/')[3];
      const body = await parseBody(req);
      const { collected_quantity } = body;

      try {
        const result = transaction(() => {
          const record = queryOne('SELECT * FROM recycling WHERE id = ?', [recyclingId]);
          if (!record) throw new Error('回收记录不存在');
          if (record.status !== 'pending') throw new Error('当前状态无法确认');

          const collected = collected_quantity !== undefined ? collected_quantity : record.quantity;
          execute("UPDATE recycling SET status = 'collected', collected_quantity = ? WHERE id = ?",
            [collected, recyclingId]);

          // 检查数量是否相符
          if (collected !== record.quantity) {
            // 生成回收数量不符任务
            const batch = queryOne('SELECT * FROM batches WHERE id = ?', [record.batch_id]);
            const diff = record.quantity - collected;
            execute(
              'INSERT INTO tasks (type, title, description, priority) VALUES (?, ?, ?, ?)',
              ['recycling_mismatch',
               `回收数量不符：${batch ? batch.batch_number : ''}`,
               `回收单 ${recyclingId} 应回收 ${record.quantity}，实际回收 ${collected}，差异 ${diff}`,
               'high']
            );
          }

          return { collected, expected: record.quantity, mismatch: collected !== record.quantity };
        });

        sendJson(res, { message: '回收确认成功', ...result });
      } catch (e) {
        sendError(res, e.message);
      }
      return;
    }

    // 回收对账
    if (pathname.match(/^\/api\/recycling\/\d+\/reconcile$/) && method === 'POST') {
      const recyclingId = pathname.split('/')[3];

      try {
        const result = transaction(() => {
          const record = queryOne('SELECT * FROM recycling WHERE id = ?', [recyclingId]);
          if (!record) throw new Error('回收记录不存在');
          if (record.status !== 'collected') throw new Error('请先完成回收确认');

          execute("UPDATE recycling SET status = 'reconciled', reconciled_at = datetime('now', 'localtime') WHERE id = ?",
            [recyclingId]);
          return { status: 'reconciled' };
        });

        sendJson(res, { message: '对账完成', ...result });
      } catch (e) {
        sendError(res, e.message);
      }
      return;
    }

    // 任务列表
    if (pathname === '/api/tasks' && method === 'GET') {
      const status = url.searchParams.get('status');
      const type = url.searchParams.get('type');
      const storeId = url.searchParams.get('store_id');
      let sql = `
        SELECT t.*, s.name as store_name, f.name as freezer_name
        FROM tasks t
        LEFT JOIN stores s ON t.store_id = s.id
        LEFT JOIN freezers f ON t.freezer_id = f.id
        WHERE 1=1
      `;
      const params = [];
      if (status) {
        sql += ' AND t.status = ?';
        params.push(status);
      }
      if (type) {
        sql += ' AND t.type = ?';
        params.push(type);
      }
      if (storeId) {
        sql += ' AND t.store_id = ?';
        params.push(storeId);
      }
      sql += ' ORDER BY CASE t.priority WHEN \'critical\' THEN 0 WHEN \'high\' THEN 1 WHEN \'medium\' THEN 2 ELSE 3 END, t.created_at DESC';
      const tasks = query(sql, params);
      sendJson(res, tasks);
      return;
    }

    // 任务处置
    if (pathname.match(/^\/api\/tasks\/\d+\/dispose$/) && method === 'POST') {
      const taskId = pathname.split('/')[3];
      const body = await parseBody(req);
      const { action, note } = body;

      if (!action || !['resolved', 'escalated', 'dismissed'].includes(action)) {
        sendError(res, '无效的处置操作');
        return;
      }

      try {
        const result = transaction(() => {
          const task = queryOne('SELECT * FROM tasks WHERE id = ?', [taskId]);
          if (!task) throw new Error('任务不存在');

          const newStatus = action === 'resolved' ? 'resolved' : action === 'escalated' ? 'in_progress' : 'resolved';
          execute("UPDATE tasks SET status = ?, resolved_at = datetime('now', 'localtime') WHERE id = ?",
            [newStatus, taskId]);

          execute('INSERT INTO dispositions (task_id, action, note) VALUES (?, ?, ?)',
            [taskId, action, note || null]);

          return { status: newStatus };
        });

        sendJson(res, { message: '任务处置成功', ...result });
      } catch (e) {
        sendError(res, e.message);
      }
      return;
    }

    // 运行任务检查
    if (pathname === '/api/tasks/run-checks' && method === 'POST') {
      const results = runAllChecks();
      sendJson(res, { message: '检查完成', results });
      return;
    }

    // 生成样品数据
    if (pathname === '/api/samples/generate' && method === 'POST') {
      generateAllSamples();
      runAllChecks();
      sendJson(res, { message: '样品数据生成成功' });
      return;
    }

    // 门店隔离测试 - 获取门店统计
    if (pathname === '/api/stores/stats' && method === 'GET') {
      const stats = query(`
        SELECT s.id, s.name,
          (SELECT COUNT(*) FROM freezers WHERE store_id = s.id) as freezer_count,
          (SELECT COUNT(*) FROM lanes l JOIN freezers f ON l.freezer_id = f.id WHERE f.store_id = s.id AND l.quantity > 0) as stocked_lanes,
          (SELECT COUNT(*) FROM exchange_orders WHERE store_id = s.id AND status NOT IN ('completed', 'cancelled')) as pending_exchanges,
          (SELECT COUNT(*) FROM tasks WHERE store_id = s.id AND status != 'resolved') as open_tasks
        FROM stores s
        ORDER BY s.id
      `);
      sendJson(res, stats);
      return;
    }

    // 404
    sendError(res, '接口不存在', 404);
  } catch (e) {
    console.error('服务器错误:', e);
    sendError(res, e.message || '服务器内部错误', 500);
  }
}

// 启动服务器
const server = http.createServer(handleRequest);

server.listen(PORT, () => {
  console.log(`🚀 冰淇淋冷柜管理系统启动成功`);
  console.log(`📍 访问地址: http://localhost:${PORT}`);
  console.log(`📊 API 文档: http://localhost:${PORT}/api/stores`);

  // 启动设备模拟（每 60 秒生成一次温度读数）
  startAllDevices(60000);

  // 启动任务引擎（每 60 秒检查一次）
  const { startTaskEngine } = require('./task-engine');
  startTaskEngine(60000);
});

// 优雅关闭
process.on('SIGINT', () => {
  console.log('\n正在关闭服务器...');
  closeDb();
  process.exit(0);
});

process.on('SIGTERM', () => {
  closeDb();
  process.exit(0);
});

module.exports = { server };
