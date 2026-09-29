// 演示数据种子模块
const { query, queryOne, execute, transaction } = require('./db');

function seed() {
  // 检查是否已有数据
  const storeCount = queryOne('SELECT COUNT(*) as c FROM stores').c;
  if (storeCount > 0) return;

  transaction(() => {
    // 门店
    const stores = [
      { name: '冰雪旗舰店', address: '上海市黄浦区南京东路 100 号' },
      { name: '冰雪社区店', address: '上海市徐汇区衡山路 200 号' },
      { name: '冰雪商场店', address: '上海市浦东新区世纪大道 300 号' }
    ];
    for (const s of stores) {
      execute('INSERT INTO stores (name, address) VALUES (?, ?)', [s.name, s.address]);
    }

    // 商品
    const products = [
      { name: '香草冰淇淋', spec: '100ml/杯', barcode: '6901234567001' },
      { name: '巧克力冰淇淋', spec: '100ml/杯', barcode: '6901234567002' },
      { name: '草莓冰淇淋', spec: '100ml/杯', barcode: '6901234567003' },
      { name: '抹茶冰淇淋', spec: '100ml/杯', barcode: '6901234567004' },
      { name: '芒果冰淇淋', spec: '100ml/杯', barcode: '6901234567005' },
      { name: '曲奇冰淇淋', spec: '120ml/杯', barcode: '6901234567006' }
    ];
    for (const p of products) {
      execute('INSERT INTO products (name, spec, barcode) VALUES (?, ?, ?)', [p.name, p.spec, p.barcode]);
    }

    // 冷柜
    const freezers = [
      { store_id: 1, name: '1号冷柜', min_temp: -22, max_temp: -18, power_status: 'on' },
      { store_id: 1, name: '2号冷柜', min_temp: -22, max_temp: -18, power_status: 'on' },
      { store_id: 2, name: '1号冷柜', min_temp: -22, max_temp: -18, power_status: 'on' },
      { store_id: 2, name: '2号冷柜', min_temp: -22, max_temp: -18, power_status: 'off' },
      { store_id: 3, name: '1号冷柜', min_temp: -22, max_temp: -18, power_status: 'on' },
      { store_id: 3, name: '2号冷柜', min_temp: -22, max_temp: -18, power_status: 'on' }
    ];
    for (const f of freezers) {
      execute('INSERT INTO freezers (store_id, name, min_temp, max_temp, power_status) VALUES (?, ?, ?, ?, ?)',
        [f.store_id, f.name, f.min_temp, f.max_temp, f.power_status]);
    }

    // 货道（每个冷柜 4 个货道：A1, A2, B1, B2）
    for (let f = 1; f <= 6; f++) {
      for (const lane of ['A1', 'A2', 'B1', 'B2']) {
        execute('INSERT INTO lanes (freezer_id, lane_number, capacity) VALUES (?, ?, ?)', [f, lane, 20]);
      }
    }
  });
}

module.exports = { seed };
