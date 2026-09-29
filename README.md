# 冰淇淋冷柜温度与批次换货系统

管理门店、冷柜、货道、商品批次、温度读数、补货、临期换货、报损和供应商回收的完整系统。

## 功能特性

- **冷柜总览**：门店冷柜状态一览，温度异常/断电实时告警
- **货道库存**：货道矩阵展示，绑定商品规格与批次，入柜记录数量、生产日期和到期日
- **温度曲线**：按设备与时间入账，直接标出合格区间，超限点高亮显示
- **临期队列**：按剩余天数分层（紧急/紧迫/预警），支持一键申请换货
- **换货单**：店员和配送员双向确认，突出批次、数量和签收状态
- **回收对账**：供应商回收记录，数量不符自动生成任务
- **任务中心**：温度超限、冷柜断电、批次混放、临期未换、包装破损、回收数量不符自动生成任务

## 业务规则

- 每个货道绑定商品规格与批次，入柜时记录数量、生产日期和到期日
- 温度读数按设备与时间入账，换货只能针对在柜批次
- 已报损数量不能重新计入可售库存
- 温度超限、冷柜断电、批次混放、临期未换、包装破损和回收数量不符分别生成任务
- 迟到读数不得覆盖处置后的状态
- 换货单需要店员和配送员双向确认
- 并发补货只能成功一次（事务保证）

## 技术栈

- **后端**：Node.js + 内置 HTTP 服务器
- **数据库**：SQLite（node:sqlite）
- **前端**：香草 HTML/CSS/JavaScript（无框架依赖）
- **配色**：香草白、海军蓝、草莓红

## 快速开始

### 单条命令启动

```bash
npm start
```

或

```bash
node start.js
```

启动后访问 http://localhost:3000

系统会自动：
1. 初始化数据库
2. 加载演示数据（3家门店、6台冷柜、6种商品）
3. 生成样品数据（温控、临期、回收）
4. 运行任务检查
5. 启动服务器

### 运行测试

```bash
npm test
```

测试覆盖：
- 温度阈值边界
- 批次锁定
- 温度乱序（迟到读数不覆盖处置状态）
- 数量守恒
- 门店隔离

## 项目结构

```
/workspace/
├── start.js              # 单条本地启动命令
├── package.json
├── src/
│   ├── server.js         # HTTP 服务器与 API 路由
│   ├── db.js             # 数据库操作
│   ├── schema.sql        # 数据库 Schema
│   ├── seed.js           # 演示数据种子
│   ├── device-adapter.js # 本地设备适配器（温度模拟）
│   ├── task-engine.js    # 任务计算引擎
│   └── samples.js        # 样品数据生成器
├── public/
│   ├── index.html        # 前端页面
│   ├── css/
│   │   └── style.css     # 样式（香草白/海军蓝/草莓红）
│   └── js/
│       └── app.js        # 前端逻辑
└── tests/
    └── run-tests.js      # 测试套件
```

## API 接口

### 门店
- `GET /api/stores` - 门店列表
- `GET /api/stores/stats` - 门店统计

### 冷柜
- `GET /api/freezers` - 冷柜列表（支持 store_id 筛选）
- `GET /api/freezers/:id` - 冷柜详情
- `GET /api/freezers/:id/lanes` - 货道库存
- `GET /api/freezers/:id/temperatures` - 温度曲线数据
- `POST /api/freezers/:id/temperatures` - 记录温度读数

### 批次
- `GET /api/batches` - 批次列表
- `GET /api/batches/near-expiry` - 临期队列

### 补货
- `POST /api/restocks` - 补货入柜

### 换货单
- `GET /api/exchange-orders` - 换货单列表
- `POST /api/exchange-orders` - 创建换货单
- `POST /api/exchange-orders/:id/confirm` - 确认换货（店员/配送员）
- `POST /api/exchange-orders/:id/complete` - 完成换货

### 报损
- `POST /api/scrapped` - 报损

### 回收
- `GET /api/recycling` - 回收记录
- `POST /api/recycling` - 创建回收记录
- `POST /api/recycling/:id/collect` - 确认回收
- `POST /api/recycling/:id/reconcile` - 回收对账

### 任务
- `GET /api/tasks` - 任务列表
- `POST /api/tasks/:id/dispose` - 任务处置
- `POST /api/tasks/run-checks` - 运行任务检查

### 样品
- `POST /api/samples/generate` - 生成样品数据

## 临期分层

| 层级 | 剩余天数 | 颜色 |
|------|----------|------|
| 紧急 | ≤1天 | 草莓红 |
| 紧迫 | ≤3天 | 橙色 |
| 预警 | ≤7天 | 黄色 |

## 温度合格区间

默认合格范围：-22°C ~ -18°C

温度曲线中：
- 绿色背景：合格区间
- 蓝色曲线：温度曲线
- 红色点：超限点
