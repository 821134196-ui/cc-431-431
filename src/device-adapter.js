// 本地设备适配器 - 模拟温度读数
const { query, queryOne, execute, transaction } = require('./db');

// 设备注册表：freezer_id -> 设备状态
const deviceRegistry = new Map();

// 初始化设备
function initDevice(freezerId) {
  if (!deviceRegistry.has(freezerId)) {
    deviceRegistry.set(freezerId, {
      freezerId,
      baseTemp: -20,
      variance: 1.5,
      lastReading: null,
      running: false
    });
  }
  return deviceRegistry.get(freezerId);
}

// 生成模拟温度读数
function generateReading(freezerId) {
  const device = initDevice(freezerId);
  const freezer = queryOne('SELECT * FROM freezers WHERE id = ?', [freezerId]);

  let temp;
  if (freezer && freezer.power_status === 'off') {
    // 断电时温度上升
    const ambient = 25;
    const lastTemp = device.lastReading || -20;
    temp = lastTemp + (ambient - lastTemp) * 0.1 + (Math.random() - 0.5) * 0.5;
    temp = Math.min(temp, ambient);
  } else {
    // 正常运行时在设定范围内波动
    const mid = (freezer.min_temp + freezer.max_temp) / 2;
    const range = (freezer.max_temp - freezer.min_temp) / 2;
    temp = mid + (Math.random() - 0.5) * 2 * range * 0.8 + (Math.random() - 0.5) * device.variance;
  }

  temp = Math.round(temp * 100) / 100;
  device.lastReading = temp;
  return temp;
}

// 记录温度读数
function recordReading(freezerId, temperature, recordedAt = null, source = 'device') {
  const timestamp = recordedAt || new Date().toISOString().replace('T', ' ').substring(0, 19);
  const result = execute(
    'INSERT INTO temperature_readings (freezer_id, temperature, recorded_at, source) VALUES (?, ?, ?, ?)',
    [freezerId, temperature, timestamp, source]
  );
  return result.lastInsertRowid;
}

// 启动设备模拟（定时生成读数）
function startDeviceSimulation(freezerId, intervalMs = 30000) {
  const device = initDevice(freezerId);
  if (device.running) return;
  device.running = true;

  const tick = () => {
    if (!device.running) return;
    const temp = generateReading(freezerId);
    recordReading(freezerId, temp);
    setTimeout(tick, intervalMs);
  };
  tick();
}

// 停止设备模拟
function stopDeviceSimulation(freezerId) {
  const device = deviceRegistry.get(freezerId);
  if (device) device.running = false;
}

// 启动所有冷柜的模拟
function startAllDevices(intervalMs = 30000) {
  const freezers = query('SELECT id FROM freezers');
  for (const f of freezers) {
    startDeviceSimulation(f.id, intervalMs);
  }
}

// 手动记录读数（用于测试或手动输入）
function manualReading(freezerId, temperature, recordedAt = null) {
  return recordReading(freezerId, temperature, recordedAt, 'manual');
}

module.exports = {
  initDevice,
  generateReading,
  recordReading,
  startDeviceSimulation,
  stopDeviceSimulation,
  startAllDevices,
  manualReading
};
