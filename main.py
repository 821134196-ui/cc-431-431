#!/usr/bin/env python3
"""单条本地启动命令:

    python3 main.py            # 初始化 SQLite + 写入温控/临期/回收演示样例 + 启动 Web
    python3 main.py --reset    # 删除旧库重建
    python3 main.py --no-serve # 只生成演示数据并打印摘要
    python3 main.py --port 8080

环境变量 FREEZER_DB 可覆盖数据库路径(默认 ./data.db)。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from app import db
from app.device import DeviceAdapter
from app.seed import seed_all
from app.tasks import TaskEngine
from app.web import serve


def bootstrap(reset: bool = False):
    path = db.DB_PATH
    if reset and os.path.exists(path):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass
    conn = db.connect(path)
    db.init_db(conn)
    device = DeviceAdapter(conn, interval_sec=10)
    summary = seed_all(conn, device=device)
    return conn, device, summary


def main() -> int:
    ap = argparse.ArgumentParser(description="冰淇淋冷柜温度与批次换货系统")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reset", action="store_true", help="重建演示数据库")
    ap.add_argument("--no-serve", action="store_true", help="只生成演示数据")
    args = ap.parse_args()

    conn, device, summary = bootstrap(args.reset)
    print("── 演示数据 ──")
    for k, v in summary.items():
        print(f"  {k:22s} {v}")
    if summary.get("skipped"):
        print("  (如需重建: python3 main.py --reset)")

    if args.no_serve:
        print("✓ 样例已写入", os.path.abspath(db.DB_PATH))
        return 0

    engine = TaskEngine(conn, interval_sec=60)
    engine.start()
    device.start()
    try:
        serve(conn, host=args.host, port=args.port)
    finally:
        device.stop()
        engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
