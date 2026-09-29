// 数据库操作模块
const { DatabaseSync } = require('node:sqlite');
const fs = require('node:fs');
const path = require('node:path');

const DB_PATH = path.join(__dirname, '..', 'data', 'freezer.db');
const SCHEMA_PATH = path.join(__dirname, 'schema.sql');

let db = null;

function getDb() {
  if (db) return db;
  const dir = path.dirname(DB_PATH);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  db = new DatabaseSync(DB_PATH);
  db.exec('PRAGMA journal_mode = WAL;');
  db.exec('PRAGMA foreign_keys = ON;');
  return db;
}

function initDb() {
  const database = getDb();
  const schema = fs.readFileSync(SCHEMA_PATH, 'utf-8');
  database.exec(schema);
  return database;
}

function closeDb() {
  if (db) {
    db.close();
    db = null;
  }
}

// 通用查询
function query(sql, params = []) {
  return getDb().prepare(sql).all(...params);
}

function queryOne(sql, params = []) {
  return getDb().prepare(sql).get(...params);
}

function execute(sql, params = []) {
  return getDb().prepare(sql).run(...params);
}

// 事务
function transaction(fn) {
  const database = getDb();
  database.exec('BEGIN');
  try {
    const result = fn();
    database.exec('COMMIT');
    return result;
  } catch (e) {
    database.exec('ROLLBACK');
    throw e;
  }
}

module.exports = {
  getDb,
  initDb,
  closeDb,
  query,
  queryOne,
  execute,
  transaction,
  DB_PATH
};
