// 冰淇淋冷柜管理系统 - 前端逻辑

// 全局状态
const state = {
  currentView: 'overview',
  currentStoreId: '',
  freezers: [],
  lanes: [],
  batches: [],
  exchangeOrders: [],
  recycling: [],
  tasks: [],
  temperatureChart: null
};

// API 封装
async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || '请求失败');
  }
  return data;
}

// 工具函数
function formatDate(dateStr) {
  if (!dateStr) return '-';
  return dateStr.split('T')[0];
}

function daysBetween(date1, date2) {
  const d1 = new Date(date1);
  const d2 = new Date(date2);
  return Math.ceil((d2 - d1) / (1000 * 60 * 60 * 24));
}

function getStatusText(status) {
  const map = {
    'pending': '待处理',
    'clerk_confirmed': '店员已确认',
    'delivery_confirmed': '配送员已确认',
    'completed': '已完成',
    'cancelled': '已取消',
    'in_stock': '在库',
    'locked': '已锁定',
    'exchanged': '已换货',
    'scrapped': '已报损',
    'recycled': '已回收',
    'collected': '已回收',
    'reconciled': '已对账',
    'open': '待处理',
    'in_progress': '处理中',
    'resolved': '已解决'
  };
  return map[status] || status;
}

function getReasonText(reason) {
  const map = {
    'near_expiry': '临期',
    'packaging_damage': '包装破损',
    'temperature': '温度异常',
    'batch_mixing': '批次混放',
    'other': '其他'
  };
  return map[reason] || reason;
}

function getTaskTypeText(type) {
  const map = {
    'temp_overlimit': '温度超限',
    'power_off': '断电',
    'batch_mixing': '批次混放',
    'near_expiry': '临期未换',
    'packaging_damage': '包装破损',
    'recycling_mismatch': '回收数量不符'
  };
  return map[type] || type;
}

// 导航切换
function setupNavigation() {
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const view = btn.dataset.view;
      switchView(view);
    });
  });

  document.getElementById('store-select').addEventListener('change', (e) => {
    state.currentStoreId = e.target.value;
    refreshAll();
  });
}

function switchView(view) {
  state.currentView = view;
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === view);
  });
  document.querySelectorAll('.view').forEach(v => {
    v.classList.toggle('active', v.id === `view-${view}`);
  });

  switch (view) {
    case 'overview':
      loadOverview();
      break;
    case 'lanes':
      loadLanes();
      break;
    case 'temperature':
      loadTemperature();
      break;
    case 'near-expiry':
      loadNearExpiry();
      break;
    case 'exchange':
      loadExchangeOrders();
      break;
    case 'recycling':
      loadRecycling();
      break;
    case 'tasks':
      loadTasks();
      break;
  }
}

// 加载门店列表
async function loadStores() {
  const stores = await api('/api/stores');
  const select = document.getElementById('store-select');
  select.innerHTML = '<option value="">全部门店</option>';
  stores.forEach(s => {
    const option = document.createElement('option');
    option.value = s.id;
    option.textContent = s.name;
    select.appendChild(option);
  });
}

// 冷柜总览
async function loadOverview() {
  const storeId = state.currentStoreId;
  const url = storeId ? `/api/freezers?store_id=${storeId}` : '/api/freezers';
  state.freezers = await api(url);

  // 统计
  const stats = await api('/api/stores/stats');
  renderStats(stats);

  // 冷柜卡片
  renderFreezerCards(state.freezers);
}

function renderStats(stats) {
  const totalFreezers = stats.reduce((sum, s) => sum + s.freezer_count, 0);
  const totalStocked = stats.reduce((sum, s) => sum + s.stocked_lanes, 0);
  const totalExchanges = stats.reduce((sum, s) => sum + s.pending_exchanges, 0);
  const totalTasks = stats.reduce((sum, s) => sum + s.open_tasks, 0);

  const container = document.getElementById('overview-stats');
  container.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">冷柜总数</div>
      <div class="stat-value">${totalFreezers}</div>
    </div>
    <div class="stat-card success">
      <div class="stat-label">有货货道</div>
      <div class="stat-value">${totalStocked}</div>
    </div>
    <div class="stat-card warning">
      <div class="stat-label">待处理换货</div>
      <div class="stat-value">${totalExchanges}</div>
    </div>
    <div class="stat-card danger">
      <div class="stat-label">待处理任务</div>
      <div class="stat-value">${totalTasks}</div>
    </div>
  `;
}

function renderFreezerCards(freezers) {
  const container = document.getElementById('freezer-grid');
  if (freezers.length === 0) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📦</div><div>暂无冷柜</div></div>';
    return;
  }

  container.innerHTML = freezers.map(f => {
    const temp = f.current_temp;
    const isAlarm = temp !== null && temp !== undefined && (temp > f.max_temp || temp < f.min_temp);
    const isOff = f.power_status === 'off';
    const cardClass = isAlarm ? 'alarm' : (isOff ? 'off' : '');
    const statusClass = isOff ? 'status-off' : (isAlarm ? 'status-alarm' : 'status-on');
    const statusText = isOff ? '已断电' : (isAlarm ? '温度异常' : '运行中');
    const tempClass = isAlarm ? 'alarm' : '';

    return `
      <div class="freezer-card ${cardClass}" onclick="viewFreezerLanes(${f.id})">
        <div class="freezer-header">
          <div class="freezer-name">${f.name}</div>
          <span class="freezer-status ${statusClass}">${statusText}</span>
        </div>
        <div class="freezer-temp ${tempClass}">${temp !== null && temp !== undefined ? temp.toFixed(1) + '°C' : '--'}</div>
        <div class="freezer-info">合格范围: ${f.min_temp}°C ~ ${f.max_temp}°C</div>
        <div class="freezer-info">货道: ${f.stocked_lanes}/${f.total_lanes} 有货</div>
        <div class="freezer-store">📍 ${f.store_name}</div>
      </div>
    `;
  }).join('');
}

function viewFreezerLanes(freezerId) {
  switchView('lanes');
  document.getElementById('lane-freezer-select').value = freezerId;
  loadLanes();
}

// 货道库存
async function loadLanes() {
  const storeId = state.currentStoreId;
  const freezers = await api(storeId ? `/api/freezers?store_id=${storeId}` : '/api/freezers');
  state.freezers = freezers;

  const select = document.getElementById('lane-freezer-select');
  const currentVal = select.value;
  select.innerHTML = freezers.map(f =>
    `<option value="${f.id}" ${f.id == currentVal ? 'selected' : ''}>${f.store_name} - ${f.name}</option>`
  ).join('');

  if (freezers.length > 0 && !select.value) {
    select.value = freezers[0].id;
  }

  await renderLanes();
}

async function renderLanes() {
  const freezerId = document.getElementById('lane-freezer-select').value;
  if (!freezerId) return;

  const lanes = await api(`/api/freezers/${freezerId}/lanes`);
  state.lanes = lanes;

  const container = document.getElementById('lane-matrix');
  if (lanes.length === 0) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📦</div><div>暂无货道</div></div>';
    return;
  }

  container.innerHTML = lanes.map(lane => {
    const hasStock = lane.quantity > 0;
    const isLocked = lane.batch_status === 'locked';
    const cardClass = hasStock ? (isLocked ? 'locked' : 'has-stock') : 'empty';
    const fillPercent = lane.capacity > 0 ? (lane.quantity / lane.capacity * 100) : 0;

    return `
      <div class="lane-card ${cardClass}">
        <div class="lane-header">
          <div class="lane-number">${lane.lane_number}</div>
          ${hasStock ? `<span class="lane-batch-status batch-${lane.batch_status}">${getStatusText(lane.batch_status)}</span>` : ''}
        </div>
        ${hasStock ? `
          <div class="lane-product">${lane.product_name || '-'}</div>
          <div class="lane-spec">${lane.product_spec || ''}</div>
          <div class="lane-batch-info">
            <div>批次: ${lane.batch_number || '-'}</div>
            <div>生产: ${formatDate(lane.production_date)}</div>
            <div>到期: ${formatDate(lane.expiry_date)}</div>
          </div>
          <div class="lane-quantity">
            <span class="quantity-text">${lane.quantity}/${lane.capacity}</span>
            <div class="quantity-bar">
              <div class="quantity-fill" style="width: ${fillPercent}%"></div>
            </div>
          </div>
          <div style="margin-top: 8px; display: flex; gap: 4px;">
            <button class="btn btn-primary btn-sm" onclick="showRestockModal(${lane.id}, ${lane.batch_id})">补货</button>
          </div>
        ` : `
          <div class="lane-product" style="color: #999;">空货道</div>
          <div style="margin-top: 8px;">
            <button class="btn btn-primary btn-sm" onclick="showRestockModal(${lane.id}, null)">入柜</button>
          </div>
        `}
      </div>
    `;
  }).join('');
}

// 补货模态框
async function showRestockModal(laneId, batchId) {
  const batches = await api('/api/batches?status=in_stock');
  const lane = state.lanes.find(l => l.id === laneId);

  const modalBody = document.getElementById('modal-body');
  modalBody.innerHTML = `
    <div class="form-group">
      <label>货道</label>
      <input type="text" value="${lane ? lane.lane_number : ''}" disabled>
    </div>
    <div class="form-group">
      <label>选择批次</label>
      <select id="restock-batch">
        ${batches.map(b => `<option value="${b.id}" ${b.id == batchId ? 'selected' : ''}>${b.product_name} - ${b.batch_number} (库存: ${b.current_quantity})</option>`).join('')}
      </select>
    </div>
    <div class="form-group">
      <label>数量</label>
      <input type="number" id="restock-quantity" min="1" value="1">
    </div>
    <div style="display: flex; gap: 8px; justify-content: flex-end;">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn btn-primary" onclick="submitRestock(${laneId})">确认补货</button>
    </div>
  `;
  document.getElementById('modal-title').textContent = '补货入柜';
  document.getElementById('modal').classList.add('active');
}

async function submitRestock(laneId) {
  const batchId = document.getElementById('restock-batch').value;
  const quantity = parseInt(document.getElementById('restock-quantity').value);

  if (!batchId || !quantity || quantity <= 0) {
    alert('请填写有效信息');
    return;
  }

  try {
    await api('/api/restocks', {
      method: 'POST',
      body: JSON.stringify({ lane_id: laneId, batch_id: parseInt(batchId), quantity })
    });
    closeModal();
    await renderLanes();
    alert('补货成功');
  } catch (e) {
    alert(e.message);
  }
}

// 温度曲线
async function loadTemperature() {
  const storeId = state.currentStoreId;
  const freezers = await api(storeId ? `/api/freezers?store_id=${storeId}` : '/api/freezers');
  const select = document.getElementById('temp-freezer-select');
  const currentVal = select.value;
  select.innerHTML = freezers.map(f =>
    `<option value="${f.id}" ${f.id == currentVal ? 'selected' : ''}>${f.store_name} - ${f.name}</option>`
  ).join('');

  if (freezers.length > 0 && !select.value) {
    select.value = freezers[0].id;
  }

  select.onchange = renderTemperatureChart;
  document.getElementById('temp-hours-select').onchange = renderTemperatureChart;

  await renderTemperatureChart();
}

async function renderTemperatureChart() {
  const freezerId = document.getElementById('temp-freezer-select').value;
  const hours = document.getElementById('temp-hours-select').value;
  if (!freezerId) return;

  const freezers = await api('/api/freezers');
  const freezer = freezers.find(f => f.id == freezerId);
  const readings = await api(`/api/freezers/${freezerId}/temperatures?hours=${hours}`);

  drawTemperatureChart(readings, freezer);
}

function drawTemperatureChart(readings, freezer) {
  const canvas = document.getElementById('temperature-chart');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);

  const width = rect.width;
  const height = rect.height;
  const padding = { top: 30, right: 30, bottom: 50, left: 60 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;

  // 清空
  ctx.clearRect(0, 0, width, height);

  if (readings.length === 0) {
    ctx.fillStyle = '#999';
    ctx.font = '14px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('暂无温度数据', width / 2, height / 2);
    return;
  }

  // 计算范围
  const temps = readings.map(r => r.temperature);
  const minTemp = Math.min(...temps, freezer.min_temp - 2);
  const maxTemp = Math.max(...temps, freezer.max_temp + 2);
  const tempRange = maxTemp - minTemp;

  // 绘制合格区间背景
  const yMin = padding.top + chartHeight - ((freezer.min_temp - minTemp) / tempRange) * chartHeight;
  const yMax = padding.top + chartHeight - ((freezer.max_temp - minTemp) / tempRange) * chartHeight;

  ctx.fillStyle = '#e8f5e9';
  ctx.fillRect(padding.left, yMax, chartWidth, yMin - yMax);

  // 绘制合格区间边界线
  ctx.strokeStyle = '#4caf50';
  ctx.lineWidth = 1;
  ctx.setLineDash([5, 5]);

  ctx.beginPath();
  ctx.moveTo(padding.left, yMin);
  ctx.lineTo(padding.left + chartWidth, yMin);
  ctx.stroke();

  ctx.beginPath();
  ctx.moveTo(padding.left, yMax);
  ctx.lineTo(padding.left + chartWidth, yMax);
  ctx.stroke();

  ctx.setLineDash([]);

  // 绘制 Y 轴标签
  ctx.fillStyle = '#666';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'right';
  const ySteps = 5;
  for (let i = 0; i <= ySteps; i++) {
    const temp = minTemp + (tempRange * i / ySteps);
    const y = padding.top + chartHeight - (i / ySteps) * chartHeight;
    ctx.fillText(temp.toFixed(1) + '°C', padding.left - 8, y + 4);
  }

  // 绘制温度曲线
  ctx.strokeStyle = '#1a237e';
  ctx.lineWidth = 2;
  ctx.beginPath();

  readings.forEach((r, i) => {
    const x = padding.left + (i / (readings.length - 1)) * chartWidth;
    const y = padding.top + chartHeight - ((r.temperature - minTemp) / tempRange) * chartHeight;

    if (i === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();

  // 绘制数据点
  readings.forEach((r, i) => {
    const x = padding.left + (i / (readings.length - 1)) * chartWidth;
    const y = padding.top + chartHeight - ((r.temperature - minTemp) / tempRange) * chartHeight;
    const isOutOfRange = r.temperature > freezer.max_temp || r.temperature < freezer.min_temp;

    ctx.beginPath();
    ctx.arc(x, y, isOutOfRange ? 5 : 3, 0, Math.PI * 2);
    ctx.fillStyle = isOutOfRange ? '#c62828' : '#1a237e';
    ctx.fill();
  });

  // 绘制 X 轴时间标签
  ctx.fillStyle = '#666';
  ctx.font = '10px sans-serif';
  ctx.textAlign = 'center';
  const labelCount = Math.min(6, readings.length);
  for (let i = 0; i < labelCount; i++) {
    const idx = Math.floor(i * (readings.length - 1) / (labelCount - 1));
    const x = padding.left + (idx / (readings.length - 1)) * chartWidth;
    const time = readings[idx].recorded_at.substring(5, 16);
    ctx.fillText(time, x, padding.top + chartHeight + 20);
  }

  // 绘制标题
  ctx.fillStyle = '#1a237e';
  ctx.font = 'bold 14px sans-serif';
  ctx.textAlign = 'left';
  ctx.fillText(`${freezer.store_name} - ${freezer.name} 温度曲线`, padding.left, 20);
}

// 临期队列
async function loadNearExpiry() {
  const storeId = state.currentStoreId;
  const url = storeId ? `/api/batches/near-expiry?store_id=${storeId}&days=7` : '/api/batches/near-expiry?days=7';
  const batches = await api(url);

  const container = document.getElementById('near-expiry-list');
  if (batches.length === 0) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">✅</div><div>暂无临期商品</div></div>';
    return;
  }

  container.innerHTML = batches.map(b => {
    const layerClass = b.layer;
    return `
      <div class="near-expiry-item ${layerClass}">
        <div class="expiry-days">${b.days_left}<br><span style="font-size:12px;font-weight:normal;">天</span></div>
        <div class="expiry-info">
          <div class="expiry-product">${b.product_name}</div>
          <div class="expiry-detail">批次: ${b.batch_number} | 到期: ${formatDate(b.expiry_date)} | 数量: ${b.current_quantity}</div>
          <div class="expiry-detail">📍 ${b.store_name} - ${b.freezer_name} 货道 ${b.lane_number}</div>
        </div>
        <div class="expiry-actions">
          <button class="btn btn-danger btn-sm" onclick="showExchangeModal(${b.id}, ${b.store_id}, ${b.lane_id}, ${b.current_quantity})">申请换货</button>
        </div>
      </div>
    `;
  }).join('');
}

// 换货模态框
async function showExchangeModal(batchId, storeId, laneId, maxQty) {
  const modalBody = document.getElementById('modal-body');
  modalBody.innerHTML = `
    <div class="form-group">
      <label>换货数量</label>
      <input type="number" id="exchange-quantity" min="1" max="${maxQty}" value="${maxQty}">
    </div>
    <div class="form-group">
      <label>换货原因</label>
      <select id="exchange-reason">
        <option value="near_expiry">临期</option>
        <option value="packaging_damage">包装破损</option>
        <option value="temperature">温度异常</option>
        <option value="other">其他</option>
      </select>
    </div>
    <div style="display: flex; gap: 8px; justify-content: flex-end;">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn btn-danger" onclick="submitExchange(${batchId}, ${storeId}, ${laneId})">确认换货</button>
    </div>
  `;
  document.getElementById('modal-title').textContent = '申请换货';
  document.getElementById('modal').classList.add('active');
}

async function submitExchange(batchId, storeId, laneId) {
  const quantity = parseInt(document.getElementById('exchange-quantity').value);
  const reason = document.getElementById('exchange-reason').value;

  if (!quantity || quantity <= 0) {
    alert('请填写有效数量');
    return;
  }

  try {
    await api('/api/exchange-orders', {
      method: 'POST',
      body: JSON.stringify({ batch_id: batchId, store_id: storeId, lane_id: laneId, quantity, reason })
    });
    closeModal();
    alert('换货申请已提交');
    switchView('exchange');
  } catch (e) {
    alert(e.message);
  }
}

// 换货单
async function loadExchangeOrders() {
  const storeId = state.currentStoreId;
  const status = document.querySelector('.filter-btn.active')?.dataset.status || '';
  let url = '/api/exchange-orders?';
  if (storeId) url += `store_id=${storeId}&`;
  if (status) url += `status=${status}&`;

  const orders = await api(url);
  state.exchangeOrders = orders;

  const container = document.getElementById('exchange-list');
  if (orders.length === 0) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><div>暂无换货单</div></div>';
    return;
  }

  container.innerHTML = orders.map(o => {
    const statusClass = `status-${o.status}`;
    const cardClass = o.status === 'completed' ? 'completed' : (o.status === 'pending' ? 'pending' : '');
    const clerkDone = ['clerk_confirmed', 'delivery_confirmed', 'completed'].includes(o.status);
    const deliveryDone = ['delivery_confirmed', 'completed'].includes(o.status);

    return `
      <div class="exchange-card ${cardClass}">
        <div class="exchange-header">
          <div class="exchange-number">${o.order_number}</div>
          <span class="exchange-status ${statusClass}">${getStatusText(o.status)}</span>
        </div>
        <div class="exchange-body">
          <div class="exchange-field">
            <div class="exchange-field-label">商品</div>
            <div class="exchange-field-value">${o.product_name}</div>
          </div>
          <div class="exchange-field">
            <div class="exchange-field-label">批次</div>
            <div class="exchange-field-value">${o.batch_number}</div>
          </div>
          <div class="exchange-field">
            <div class="exchange-field-label">数量</div>
            <div class="exchange-field-value highlight">${o.quantity}</div>
          </div>
          <div class="exchange-field">
            <div class="exchange-field-label">原因</div>
            <div class="exchange-field-value">${getReasonText(o.reason)}</div>
          </div>
          <div class="exchange-field">
            <div class="exchange-field-label">门店</div>
            <div class="exchange-field-value">${o.store_name || '-'}</div>
          </div>
          <div class="exchange-field">
            <div class="exchange-field-label">冷柜</div>
            <div class="exchange-field-value">${o.freezer_name || '-'} ${o.lane_number || ''}</div>
          </div>
        </div>
        <div class="exchange-confirmations">
          <div class="confirm-item">
            <div class="confirm-check ${clerkDone ? 'done' : 'pending'}">${clerkDone ? '✓' : '?'}</div>
            <span>店员确认 ${o.clerk_confirmed_at ? '(' + o.clerk_confirmed_at.substring(5, 16) + ')' : ''}</span>
          </div>
          <div class="confirm-item">
            <div class="confirm-check ${deliveryDone ? 'done' : 'pending'}">${deliveryDone ? '✓' : '?'}</div>
            <span>配送员确认 ${o.delivery_confirmed_at ? '(' + o.delivery_confirmed_at.substring(5, 16) + ')' : ''}</span>
          </div>
        </div>
        <div class="exchange-actions">
          ${o.status === 'pending' ? `<button class="btn btn-primary btn-sm" onclick="confirmExchange(${o.id}, 'clerk')">店员确认</button>` : ''}
          ${o.status === 'clerk_confirmed' ? `<button class="btn btn-success btn-sm" onclick="confirmExchange(${o.id}, 'delivery')">配送员确认</button>` : ''}
          ${o.status === 'delivery_confirmed' ? `<button class="btn btn-danger btn-sm" onclick="completeExchange(${o.id})">完成换货</button>` : ''}
        </div>
      </div>
    `;
  }).join('');
}

async function confirmExchange(orderId, role) {
  try {
    await api(`/api/exchange-orders/${orderId}/confirm`, {
      method: 'POST',
      body: JSON.stringify({ role })
    });
    await loadExchangeOrders();
  } catch (e) {
    alert(e.message);
  }
}

async function completeExchange(orderId) {
  if (!confirm('确认完成换货？完成后批次库存将被扣减。')) return;
  try {
    await api(`/api/exchange-orders/${orderId}/complete`, { method: 'POST' });
    await loadExchangeOrders();
    alert('换货完成');
  } catch (e) {
    alert(e.message);
  }
}

// 回收对账
async function loadRecycling() {
  const records = await api('/api/recycling');
  state.recycling = records;

  const container = document.getElementById('recycling-list');
  if (records.length === 0) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">♻️</div><div>暂无回收记录</div></div>';
    return;
  }

  container.innerHTML = records.map(r => {
    const mismatch = r.status === 'collected' && r.collected_quantity !== r.quantity;
    const cardClass = mismatch ? 'mismatch' : (r.status === 'reconciled' ? 'reconciled' : '');
    const statusClass = `status-${r.status}`;

    return `
      <div class="recycling-card ${cardClass}">
        <div class="recycling-header">
          <div class="recycling-supplier">${r.supplier}</div>
          <span class="recycling-status ${statusClass}">${getStatusText(r.status)}</span>
        </div>
        <div class="recycling-body">
          <div class="recycling-field">
            <div class="recycling-field-label">商品</div>
            <div class="recycling-field-value">${r.product_name}</div>
          </div>
          <div class="recycling-field">
            <div class="recycling-field-label">批次</div>
            <div class="recycling-field-value">${r.batch_number}</div>
          </div>
          <div class="recycling-field">
            <div class="recycling-field-label">应回收</div>
            <div class="recycling-field-value">${r.quantity}</div>
          </div>
          <div class="recycling-field">
            <div class="recycling-field-label">实际回收</div>
            <div class="recycling-field-value ${mismatch ? 'mismatch' : ''}">${r.collected_quantity !== null && r.collected_quantity !== undefined ? r.collected_quantity : '-'}</div>
          </div>
          <div class="recycling-field">
            <div class="recycling-field-label">到期日</div>
            <div class="recycling-field-value">${formatDate(r.expiry_date)}</div>
          </div>
        </div>
        <div class="recycling-actions">
          ${r.status === 'pending' ? `<button class="btn btn-primary btn-sm" onclick="showCollectModal(${r.id}, ${r.quantity})">确认回收</button>` : ''}
          ${r.status === 'collected' ? `<button class="btn btn-success btn-sm" onclick="reconcileRecycling(${r.id})">对账完成</button>` : ''}
        </div>
      </div>
    `;
  }).join('');
}

async function showCollectModal(recyclingId, expectedQty) {
  const modalBody = document.getElementById('modal-body');
  modalBody.innerHTML = `
    <div class="form-group">
      <label>应回收数量</label>
      <input type="number" value="${expectedQty}" disabled>
    </div>
    <div class="form-group">
      <label>实际回收数量</label>
      <input type="number" id="collected-quantity" min="0" value="${expectedQty}">
    </div>
    <div style="display: flex; gap: 8px; justify-content: flex-end;">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn btn-primary" onclick="submitCollect(${recyclingId})">确认</button>
    </div>
  `;
  document.getElementById('modal-title').textContent = '确认回收';
  document.getElementById('modal').classList.add('active');
}

async function submitCollect(recyclingId) {
  const collected = parseInt(document.getElementById('collected-quantity').value);
  try {
    await api(`/api/recycling/${recyclingId}/collect`, {
      method: 'POST',
      body: JSON.stringify({ collected_quantity: collected })
    });
    closeModal();
    await loadRecycling();
  } catch (e) {
    alert(e.message);
  }
}

async function reconcileRecycling(recyclingId) {
  try {
    await api(`/api/recycling/${recyclingId}/reconcile`, { method: 'POST' });
    await loadRecycling();
    alert('对账完成');
  } catch (e) {
    alert(e.message);
  }
}

// 任务中心
async function loadTasks() {
  const storeId = state.currentStoreId;
  const url = storeId ? `/api/tasks?store_id=${storeId}` : '/api/tasks';
  const tasks = await api(url);
  state.tasks = tasks;

  const container = document.getElementById('task-list');
  if (tasks.length === 0) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">✅</div><div>暂无待处理任务</div></div>';
    return;
  }

  container.innerHTML = tasks.map(t => {
    const priorityClass = `priority-${t.priority}`;
    const cardClass = t.status === 'resolved' ? 'resolved' : t.priority;

    return `
      <div class="task-card ${cardClass}">
        <div class="task-header">
          <div class="task-title">${t.title}</div>
          <span class="task-priority ${priorityClass}">${t.priority === 'critical' ? '紧急' : t.priority === 'high' ? '高' : t.priority === 'medium' ? '中' : '低'}</span>
        </div>
        <div class="task-description">${t.description || ''}</div>
        <div class="task-meta">
          <span>类型: ${getTaskTypeText(t.type)}</span>
          <span>门店: ${t.store_name || '-'}</span>
          <span>冷柜: ${t.freezer_name || '-'}</span>
          <span>状态: ${getStatusText(t.status)}</span>
        </div>
        ${t.status !== 'resolved' ? `
          <div class="task-actions">
            <button class="btn btn-sm" onclick="disposeTask(${t.id}, 'dismissed')">忽略</button>
            <button class="btn btn-primary btn-sm" onclick="disposeTask(${t.id}, 'resolved')">标记已解决</button>
          </div>
        ` : ''}
      </div>
    `;
  }).join('');
}

async function disposeTask(taskId, action) {
  try {
    await api(`/api/tasks/${taskId}/dispose`, {
      method: 'POST',
      body: JSON.stringify({ action, note: '手动处置' })
    });
    await loadTasks();
  } catch (e) {
    alert(e.message);
  }
}

async function runTaskChecks() {
  try {
    const result = await api('/api/tasks/run-checks', { method: 'POST' });
    alert(`检查完成，生成 ${result.results.length} 个新任务`);
    await loadTasks();
  } catch (e) {
    alert(e.message);
  }
}

// 模态框
function closeModal() {
  document.getElementById('modal').classList.remove('active');
}

// 筛选按钮
function setupFilters() {
  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      loadExchangeOrders();
    });
  });
}

// 刷新所有数据
function refreshAll() {
  switch (state.currentView) {
    case 'overview':
      loadOverview();
      break;
    case 'lanes':
      loadLanes();
      break;
    case 'temperature':
      loadTemperature();
      break;
    case 'near-expiry':
      loadNearExpiry();
      break;
    case 'exchange':
      loadExchangeOrders();
      break;
    case 'recycling':
      loadRecycling();
      break;
    case 'tasks':
      loadTasks();
      break;
  }
}

// 点击模态框外部关闭
document.getElementById('modal').addEventListener('click', (e) => {
  if (e.target.id === 'modal') closeModal();
});

// 初始化
async function init() {
  setupNavigation();
  setupFilters();
  await loadStores();
  await loadOverview();
}

init().catch(console.error);
