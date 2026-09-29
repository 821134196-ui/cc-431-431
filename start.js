// 单条本地启动命令 - 生成温控、临期和回收样例并启动服务器
const { initDb } = require('./src/db');
const { seed } = require('./src/seed');
const { generateAllSamples } = require('./src/samples');
const { runAllChecks } = require('./src/task-engine');

console.log('========================================');
console.log('  冰淇淋冷柜温度与批次换货系统');
console.log('========================================');
console.log('');

// 初始化数据库
console.log('[1/4] 初始化数据库...');
initDb();
console.log('  ✓ 数据库初始化完成');

// 种子数据
console.log('[2/4] 加载演示数据...');
seed();
console.log('  ✓ 演示数据加载完成');

// 生成样品数据
console.log('[3/4] 生成样品数据（温控、临期、回收）...');
generateAllSamples();
console.log('  ✓ 样品数据生成完成');

// 运行任务检查
console.log('[4/4] 运行任务检查...');
runAllChecks();
console.log('  ✓ 任务检查完成');

console.log('');
console.log('========================================');
console.log('  启动服务器...');
console.log('========================================');
console.log('');

// 启动服务器
require('./src/server');
