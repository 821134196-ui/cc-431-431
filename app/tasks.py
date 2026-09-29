"""进程内任务引擎。

六类任务来源:
  temp_breach      温度读数超限        -> domain.record_reading
  outage           冷柜断电            -> domain.set_powered
  mixed_batch      货道批次混放        -> domain.reconcile_channel_mix
  expiry           临期未换            -> 本引擎 scan_expiry
  package_damage   包装破损报损        -> domain.register_damage
  recycle_mismatch 供应商回收数量不符  -> domain.receive_recycling

临期分层(按到期日相对今天的剩余天数):
  expired  已过期(<=0)   草莓红,必须立即换货/报损
  urgent   <= 3 天       草莓红
  warning  <= 7 天       琥珀
  watch    <= 14 天      海军蓝
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta
from typing import Iterable

from . import domain

EXPIRY_WATCH_DAYS = 14
EXPIRY_WARN_DAYS = 7
EXPIRY_URGENT_DAYS = 3

TIER_LABELS = {
    "expired": "已过期",
    "urgent": f"{EXPIRY_URGENT_DAYS}天内到期",
    "warning": f"{EXPIRY_WARN_DAYS}天内到期",
    "watch": f"{EXPIRY_WATCH_DAYS}天内到期",
    "fresh": "充足",
}


def expiry_tier(days_left: int) -> str:
    if days_left <= 0:
        return "expired"
    if days_left <= EXPIRY_URGENT_DAYS:
        return "urgent"
    if days_left <= EXPIRY_WARN_DAYS:
        return "warning"
    if days_left <= EXPIRY_WATCH_DAYS:
        return "watch"
    return "fresh"


def days_left(expiry_date: str, today: date | None = None) -> int:
    today = today or date.today()
    y, m, d = map(int, expiry_date.split("-"))
    return (date(y, m, d) - today).days


def in_cabinet_batches(conn: sqlite3.Connection, store_id: str | None = None,
                       today: date | None = None) -> list[sqlite3.Row]:
    """在柜批次清单(含剩余天数/分层/货道位置),临期队列的数据源。"""
    today = today or date.today()
    sql = """
        SELECT b.id, b.store_id, b.batch_no, b.prod_date, b.expiry_date,
               p.name AS product_name, p.spec, p.brand,
               b.qty_received, b.qty_sold, b.qty_damaged, b.qty_exchanged,
               b.qty_recycled, b.locked,
               cb.qty_remaining, c.pos AS channel_pos, f.code AS freezer_code,
               s.name AS store_name
        FROM channel_batches cb
        JOIN batches b ON b.id=cb.batch_id
        JOIN products p ON p.id=b.product_id
        JOIN channels c ON c.id=cb.channel_id
        JOIN freezers f ON f.id=c.freezer_id
        JOIN stores s ON s.id=b.store_id
        WHERE cb.active=1 AND cb.qty_remaining>0
    """
    args: list = []
    if store_id:
        sql += " AND b.store_id=?"
        args.append(store_id)
    sql += " ORDER BY b.expiry_date, f.code, c.pos"
    rows = []
    for r in conn.execute(sql, args).fetchall():
        d = dict(r)
        d["days_left"] = days_left(r["expiry_date"], today)
        d["tier"] = expiry_tier(d["days_left"])
        rows.append(d)
    return rows


def scan_expiry(conn: sqlite3.Connection, *, today: date | None = None) -> int:
    """扫描所有门店在柜批次,为临期/过期批次开 expiry 任务,
    已不在临期窗口(或已不在柜)的任务关闭。返回新开/刷新的任务数。"""
    today = today or date.today()
    opened = 0
    with domain.tx(conn):
        rows = conn.execute(
            """SELECT b.*, s.name AS store_name,
                      (SELECT GROUP_CONCAT(c.pos || ':' || cb.qty_remaining, '，')
                         FROM channel_batches cb JOIN channels c ON c.id=cb.channel_id
                        WHERE cb.batch_id=b.id AND cb.active=1 AND cb.qty_remaining>0) AS locs
                 FROM batches b JOIN stores s ON s.id=b.store_id
                WHERE b.locked=0
                  AND EXISTS (SELECT 1 FROM channel_batches cb2
                               WHERE cb2.batch_id=b.id AND cb2.active=1
                                 AND cb2.qty_remaining>0)"""
        ).fetchall()
        flagged: set[tuple[str, str]] = set()
        for b in rows:
            dleft = days_left(b["expiry_date"], today)
            tier = expiry_tier(dleft)
            if tier in ("fresh",):
                continue
            pname = conn.execute("SELECT name FROM products WHERE id=?", (b["product_id"],)).fetchone()["name"]
            sev = "high" if tier in ("expired", "urgent") else "normal"
            title = f"临期未换:{b['batch_no']}({pname}) 剩余 {dleft} 天"
            if tier == "expired":
                title = f"已过期:{b['batch_no']}({pname}) 过期 {-dleft} 天"
            detail = (f"批次 {b['batch_no']}({pname}) 到期日 {b['expiry_date']},"
                      f"在柜位置/余量:{b['locs'] or '无'};分层:{TIER_LABELS[tier]}。")
            domain._add_task(
                conn, b["store_id"], "expiry", title, detail,
                "batch", b["id"], severity=sev,
                now=datetime.combine(today, datetime.min.time()),
            )
            flagged.add((b["store_id"], b["id"]))
            opened += 1
        # 关闭已解除的临期任务
        for t in conn.execute("SELECT * FROM tasks WHERE kind='expiry' AND status='open'").fetchall():
            if (t["store_id"], t["ref_id"]) not in flagged:
                still = conn.execute(
                    """SELECT 1 FROM channel_batches cb WHERE cb.batch_id=?
                       AND cb.active=1 AND cb.qty_remaining>0""",
                    (t["ref_id"],),
                ).fetchone()
                dleft = days_left(
                    conn.execute("SELECT expiry_date FROM batches WHERE id=?",
                                 (t["ref_id"],)).fetchone()["expiry_date"], today)
                if not still or expiry_tier(dleft) == "fresh":
                    domain._close_task(
                        conn, t["store_id"], "expiry", "batch", t["ref_id"],
                        now=datetime.combine(today, datetime.min.time()),
                    )
    return opened


class TaskEngine:
    """后台周期扫描(本进程内)。Web/CLI 也可直接调用 scan_expiry。"""

    def __init__(self, conn: sqlite3.Connection, interval_sec: float = 60.0):
        self.conn = conn
        self.interval = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self, today: date | None = None) -> int:
        return scan_expiry(self.conn, today=today)

    def start(self) -> None:
        if self._thread:
            return

        def _loop():
            while not self._stop.wait(self.interval):
                try:
                    self.run_once()
                except Exception as exc:
                    print(f"[tasks] 扫描失败: {exc}")

        self._thread = threading.Thread(target=_loop, name="task-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
