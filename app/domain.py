"""领域层:库存、温度、换货、报损、回收的全部业务规则。

核心不变量(对每个批次恒成立):
    qty_received = qty_sold + qty_damaged + qty_exchanged
                   + 在柜剩余(channel_batches.qty_remaining 之和)
    qty_recycled <= qty_exchanged      -- 回收是换货出柜的子集
    qty_damaged / qty_exchanged 永不回流可售库存

关键规则:
* 换货只能针对在柜、未锁定且余量充足的批次;
* 温度迟到读数只入历史账,不得新建/覆盖已处置事件;
* 补货以 IMMEDIATE 事务 + 幂等键保证并发只成功一次;
* 所有写操作强制 store_id 过滤,跨店访问抛 StoreIsolationError。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from .db import tx


# ---------------------------------------------------------------- 异常

class DomainError(Exception):
    """业务规则错误(调用方应作为 4xx 展示)。"""


class NotFound(DomainError):
    pass


class StoreIsolationError(DomainError):
    pass


class ChannelOccupiedError(DomainError):
    pass


class BatchNotInCabinetError(DomainError):
    pass


class BatchLockedError(DomainError):
    pass


class InsufficientStockError(DomainError):
    pass


class ReplenishedError(DomainError):
    pass


class IllegalTransition(DomainError):
    pass


# ---------------------------------------------------------------- 时间

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dt(now: datetime | None) -> datetime:
    return now or utcnow()


# ---------------------------------------------------------------- 内部工具

def _check_store(conn: sqlite3.Connection, table: str, row_id: str, store_id: str) -> None:
    """主数据/单据门店隔离校验。"""
    row = conn.execute(f"SELECT store_id FROM {table} WHERE id=?", (row_id,)).fetchone()
    if row is None:
        raise NotFound(f"{table} {row_id} 不存在")
    if row["store_id"] != store_id:
        raise StoreIsolationError(f"对象 {row_id} 不属于门店 {store_id}")


def _batch_in_cabinet(conn: sqlite3.Connection, channel_id: str, batch_id: str):
    return conn.execute(
        """SELECT cb.* FROM channel_batches cb
           WHERE cb.channel_id=? AND cb.batch_id=? AND cb.active=1""",
        (channel_id, batch_id),
    ).fetchone()


def _conservation_ok(conn: sqlite3.Connection, batch_id: str) -> bool:
    b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    remaining = conn.execute(
        "SELECT COALESCE(SUM(qty_remaining),0) AS s FROM channel_batches WHERE batch_id=? AND active=1",
        (batch_id,),
    ).fetchone()["s"]
    accounted = b["qty_sold"] + b["qty_damaged"] + b["qty_exchanged"] + remaining
    return (accounted == b["qty_received"]) and (b["qty_recycled"] <= b["qty_exchanged"])


def check_conservation(conn: sqlite3.Connection, batch_id: str) -> None:
    """供测试/对账调用:不守恒直接抛错。"""
    b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if b is None:
        raise NotFound(batch_id)
    remaining = conn.execute(
        "SELECT COALESCE(SUM(qty_remaining),0) AS s FROM channel_batches WHERE batch_id=? AND active=1",
        (batch_id,),
    ).fetchone()["s"]
    accounted = b["qty_sold"] + b["qty_damaged"] + b["qty_exchanged"] + remaining
    if accounted != b["qty_received"]:
        raise DomainError(
            f"批次 {b['batch_no']} 数量不守恒:收货 {b['qty_received']} != "
            f"售{b['qty_sold']}+损{b['qty_damaged']}+换{b['qty_exchanged']}+在柜{remaining}={accounted}"
        )
    if b["qty_recycled"] > b["qty_exchanged"]:
        raise DomainError(f"批次 {b['batch_no']} 回收 {b['qty_recycled']} 超出换货 {b['qty_exchanged']}")


def _add_task(conn, store_id, kind, title, detail, ref_type, ref_id,
              severity="normal", now=None):
    """任务去重:同 kind+ref 只保留一条,重复告警刷新详情而不是新开。"""
    cur = conn.execute(
        """INSERT INTO tasks(store_id, kind, severity, title, detail, ref_type, ref_id, created_ts)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(store_id, kind, ref_type, ref_id) DO UPDATE SET
             severity=excluded.severity, title=excluded.title, detail=excluded.detail
           WHERE tasks.status='open'""",
        (store_id, kind, severity, title, detail, ref_type, str(ref_id), iso(_dt(now))),
    )
    return cur.lastrowid


def _close_task(conn, store_id, kind, ref_type, ref_id, now=None):
    conn.execute(
        """UPDATE tasks SET status='done', closed_ts=?
           WHERE store_id=? AND kind=? AND ref_type=? AND ref_id=? AND status='open'""",
        (iso(_dt(now)), store_id, kind, ref_type, str(ref_id)),
    )


def reconcile_channel_mix(conn, channel_id: str, *, now=None) -> None:
    """检测/解除"批次混放"。须在已开启的事务内调用(纯 SQL)。

    同一货道存在 2 个以上有余量的在柜批次 => 开 mixed_batch 任务;
    余量归零的绑定置 inactive;混放消除后自动关任务。
    """
    ch = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if ch is None:
        return
    conn.execute(
        "UPDATE channel_batches SET active=0 WHERE channel_id=? AND qty_remaining<=0",
        (channel_id,),
    )
    live = conn.execute(
        "SELECT batch_id FROM channel_batches WHERE channel_id=? AND active=1 AND qty_remaining>0",
        (channel_id,),
    ).fetchall()
    if len(live) >= 2:
        ids = ", ".join(r["batch_id"] for r in live)
        _add_task(
            conn, ch["store_id"], "mixed_batch",
            f"批次混放:货道 {ch['pos']} 同时存在 {len(live)} 个批次",
            f"冷柜货道 {ch['pos']} 混放批次:{ids}。请先将较早批次换货或报损清空。",
            "channel", channel_id, severity="high", now=now,
        )
    else:
        _close_task(conn, ch["store_id"], "mixed_batch", "channel", channel_id, now)


# ---------------------------------------------------------------- 入柜 / 售卖 / 补货

def load_batch(conn, *, store_id: str, channel_id: str, product_id: str, batch_no: str,
               prod_date: str, expiry_date: str, qty: int, now: datetime | None = None,
               force: bool = False) -> str:
    """商品批次入柜。货道必须没有其他有余量的在柜批次(否则需先清空)。

    force=True 仅供对账/演示制造"批次混放"现场,正常业务路径不允许。
    返回 batch_id。
    """
    if qty <= 0:
        raise DomainError("入柜数量必须为正")
    _check_store(conn, "channels", channel_id, store_id)
    ch = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    cap = ch["capacity"]
    if qty > cap:
        raise DomainError(f"装载 {qty} 超过货道容量 {cap}")

    other = conn.execute(
        """SELECT cb.batch_id FROM channel_batches cb
           JOIN batches b ON b.id=cb.batch_id
           WHERE cb.channel_id=? AND cb.active=1 AND cb.qty_remaining>0""",
        (channel_id,),
    ).fetchall()
    if other and not force:
        raise ChannelOccupiedError(
            f"货道 {ch['pos']} 仍有在柜批次,请先清空(售卖/换货/报损)再装载新批次"
        )

    with tx(conn):
        # 再次加锁校验(与并发补货互斥)
        conn.execute("SELECT id FROM channels WHERE id=?", (channel_id,)).fetchone()
        bid = f"B-{store_id}-{batch_no}"
        ts = iso(_dt(now))
        conn.execute(
            """INSERT INTO batches(id, store_id, product_id, batch_no, prod_date, expiry_date,
                                   qty_received, created_ts)
               VALUES(?,?,?,?,?,?,?,?)""",
            (bid, store_id, product_id, batch_no, prod_date, expiry_date, qty, ts),
        )
        if ch["product_id"]:
            if ch["product_id"] != product_id:
                raise DomainError(f"货道 {ch['pos']} 已绑定其他商品规格,不可混放不同商品")
        else:
            conn.execute("UPDATE channels SET product_id=? WHERE id=?", (product_id, channel_id))
        conn.execute(
            """INSERT INTO channel_batches(channel_id, batch_id, store_id, qty_loaded,
                                           qty_remaining, active, loaded_ts)
               VALUES(?,?,?,?,?,1,?)""",
            (channel_id, bid, store_id, qty, qty, ts),
        )
        conn.execute(
            """INSERT INTO stock_moves(store_id, ts, kind, channel_id, batch_id, qty, actor)
               VALUES(?,?, 'load', ?,?,?, 'system')""",
            (store_id, ts, channel_id, bid, qty),
        )
        reconcile_channel_mix(conn, channel_id, now=now)
    return bid


def sell(conn, *, store_id: str, channel_id: str, qty: int,
         actor: str = "pos", now: datetime | None = None) -> None:
    """正常售卖:锁定批次与已报损/换货数量都不可能被卖出。"""
    if qty <= 0:
        raise DomainError("售卖数量必须为正")
    _check_store(conn, "channels", channel_id, store_id)
    with tx(conn):
        # 即使批次已全部报损/换货(绑定 inactive),锁定状态仍须拦截售卖
        locked_row = conn.execute(
            """SELECT b.batch_no FROM channel_batches cb
               JOIN batches b ON b.id=cb.batch_id
               WHERE cb.channel_id=? AND b.locked=1 LIMIT 1""",
            (channel_id,),
        ).fetchone()
        if locked_row:
            raise BatchLockedError(f"货道批次 {locked_row['batch_no']} 已锁定处置,不可售卖")
        binding = conn.execute(
            """SELECT cb.* FROM channel_batches cb
               JOIN batches b ON b.id=cb.batch_id
               WHERE cb.channel_id=? AND cb.active=1 AND cb.qty_remaining>0
               ORDER BY cb.loaded_ts""",
            (channel_id,),
        ).fetchall()
        if not binding:
            raise BatchNotInCabinetError("货道无在柜批次")
        if len(binding) > 1:
            # 混放现场不允许售卖,必须先整改
            raise BatchNotInCabinetError("货道存在批次混放,暂停售卖,等待整改")
        cb = binding[0]
        if cb["qty_remaining"] < qty:
            raise InsufficientStockError(f"在柜余量 {cb['qty_remaining']} 不足 {qty}")
        conn.execute("UPDATE channel_batches SET qty_remaining=qty_remaining-? WHERE channel_id=? AND batch_id=?",
                     (qty, channel_id, cb["batch_id"]))
        conn.execute("UPDATE batches SET qty_sold=qty_sold+? WHERE id=?", (qty, cb["batch_id"]))
        conn.execute(
            """INSERT INTO stock_moves(store_id, ts, kind, channel_id, batch_id, qty, actor)
               VALUES(?,?,'sell',?,?,?,?)""",
            (store_id, iso(_dt(now)), channel_id, cb["batch_id"], qty, actor),
        )
        reconcile_channel_mix(conn, channel_id, now=now)


def replenish(conn, *, idem_key: str, store_id: str, channel_id: str, batch_id: str,
              qty: int, actor: str = "courier", now: datetime | None = None) -> None:
    """并发补货:同一 idem_key 全局只成功一次。

    使用 BEGIN IMMEDIATE 抢写锁 + 幂等表,第二个并发请求拿锁后发现键已存在而失败,
    因此不会重复增加库存。
    """
    if qty <= 0:
        raise DomainError("补货数量必须为正")
    _check_store(conn, "channels", channel_id, store_id)
    _check_store(conn, "batches", batch_id, store_id)
    with tx(conn):
        try:
            conn.execute(
                "INSERT INTO idempotency(scope, idem_key, ts) VALUES('replenish',?,?)",
                (idem_key, iso(_dt(now))),
            )
        except sqlite3.IntegrityError:
            raise ReplenishedError(f"补货单 {idem_key} 已处理,不能重复入账")
        cb = _batch_in_cabinet(conn, channel_id, batch_id)
        if cb is None:
            raise BatchNotInCabinetError("只能对在柜批次补货")
        total = conn.execute(
            "SELECT COALESCE(SUM(qty_remaining),0) AS s FROM channel_batches WHERE channel_id=? AND active=1",
            (channel_id,),
        ).fetchone()["s"]
        ch = conn.execute("SELECT capacity FROM channels WHERE id=?", (channel_id,)).fetchone()
        if total + qty > ch["capacity"]:
            raise DomainError(f"补货后 {total + qty} 超过货道容量 {ch['capacity']}")
        conn.execute(
            "UPDATE channel_batches SET qty_remaining=qty_remaining+? WHERE channel_id=? AND batch_id=?",
            (qty, channel_id, batch_id),
        )
        conn.execute("UPDATE batches SET qty_received=qty_received+? WHERE id=?", (qty, batch_id))
        conn.execute(
            """INSERT INTO stock_moves(store_id, ts, kind, channel_id, batch_id, qty, ref, actor)
               VALUES(?,?,'replenish',?,?,?,?,?)""",
            (store_id, iso(_dt(now)), channel_id, batch_id, qty, idem_key, actor),
        )


# ---------------------------------------------------------------- 报损

def register_damage(conn, *, store_id: str, channel_id: str, batch_id: str, qty: int,
                    note: str = "", actor: str = "staff", now: datetime | None = None) -> None:
    """包装破损/报损:数量从可售库存扣除,单独记账,永不回流。

    剩余为 0 时锁定批次。生成 package_damage 任务。
    """
    if qty <= 0:
        raise DomainError("报损数量必须为正")
    _check_store(conn, "channels", channel_id, store_id)
    _check_store(conn, "batches", batch_id, store_id)
    with tx(conn):
        cb = _batch_in_cabinet(conn, channel_id, batch_id)
        if cb is None:
            raise BatchNotInCabinetError("批次不在柜,无法报损")
        if cb["qty_remaining"] < qty:
            raise InsufficientStockError(f"在柜余量 {cb['qty_remaining']} 不足报损 {qty}")
        b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if b["locked"]:
            raise BatchLockedError("批次已锁定")
        ts = iso(_dt(now))
        conn.execute("UPDATE channel_batches SET qty_remaining=qty_remaining-? WHERE channel_id=? AND batch_id=?",
                     (qty, channel_id, batch_id))
        conn.execute("UPDATE batches SET qty_damaged=qty_damaged+? WHERE id=?", (qty, batch_id))
        conn.execute(
            """INSERT INTO stock_moves(store_id, ts, kind, channel_id, batch_id, qty, ref, actor)
               VALUES(?,?,'damage',?,?,?,?,?)""",
            (store_id, ts, channel_id, batch_id, qty, note, actor),
        )
        remaining_after = cb["qty_remaining"] - qty
        if remaining_after == 0:
            conn.execute(
                "UPDATE batches SET locked=1, locked_reason='全部报损' WHERE id=?", (batch_id,))
        reconcile_channel_mix(conn, channel_id, now=now)
        _add_task(
            conn, store_id, "package_damage",
            f"包装破损:{b['batch_no']} 报损 {qty} 件",
            f"货道 {channel_id} 批次 {b['batch_no']} 报损 {qty} 件。{note}".strip(),
            "batch", batch_id, severity="high", now=now,
        )


# ---------------------------------------------------------------- 温度

def record_reading(conn, *, freezer_id: str, temp_c: float, reading_ts: datetime,
                   now: datetime | None = None) -> bool:
    """温度读数入账。

    * (device_id, ts) 主键保证重复上报幂等;
    * reading_ts 早于该设备已入账最新读数 => 标记 late=1(迟到/乱序);
    * 超限时生成 excursion 事件 + temp_breach 任务;
    * 迟到读数若落在"已处置事件"之后到达,只补历史曲线,
      绝不新建事件、不改写处置状态。
    返回是否实际入账(False=重复读数被忽略)。
    """
    fz = conn.execute("SELECT * FROM freezers WHERE id=?", (freezer_id,)).fetchone()
    if fz is None:
        raise NotFound(f"冷柜 {freezer_id} 不存在")
    ts = iso(reading_ts)
    recv = iso(_dt(now))
    with tx(conn):
        latest = conn.execute(
            "SELECT MAX(ts) AS m FROM temp_readings WHERE device_id=?", (freezer_id,)
        ).fetchone()["m"]
        late = 1 if (latest and ts < latest) else 0
        cur = conn.execute(
            """INSERT OR IGNORE INTO temp_readings(device_id, ts, temp_c, received_ts, late)
               VALUES(?,?,?,?,?)""",
            (freezer_id, ts, float(temp_c), recv, late),
        )
        if cur.rowcount == 0:
            return False  # 同设备同时间重复读数

        out_of_range = temp_c < fz["temp_min"] or temp_c > fz["temp_max"]
        if out_of_range:
            # 已处置事件的时间戳 >= 本读数时间 => 处置已涵盖该读数,迟到数据不得回改
            handled = conn.execute(
                """SELECT 1 FROM temp_incidents
                   WHERE freezer_id=? AND handled_ts IS NOT NULL AND ts >= ? LIMIT 1""",
                (freezer_id, ts),
            ).fetchone()
            if late and handled:
                return True  # 仅补曲线
            direction = "偏高" if temp_c > fz["temp_max"] else "偏低"
            detail = f"{ts} 温度 {temp_c:+.1f}℃ {direction}(合格区间 {fz['temp_min']:g}~{fz['temp_max']:g}℃)"
            conn.execute(
                """INSERT OR IGNORE INTO temp_incidents(freezer_id, store_id, kind, ts, detail)
                   VALUES(?,?, 'excursion', ?,?)""",
                (freezer_id, fz["store_id"], ts, detail),
            )
            inc = conn.execute(
                "SELECT id FROM temp_incidents WHERE freezer_id=? AND kind='excursion' AND ts=?",
                (freezer_id, ts),
            ).fetchone()
            sev = "high" if abs(temp_c - (fz["temp_max"] if temp_c > fz["temp_max"] else fz["temp_min"])) >= 3 else "normal"
            _add_task(
                conn, fz["store_id"], "temp_breach",
                f"温度超限:{fz['code']} {temp_c:+.1f}℃",
                detail + ("(迟到读数)" if late else ""),
                "incident", inc["id"], severity=sev, now=now,
            )
    return True


def set_powered(conn, *, freezer_id: str, powered: bool, actor: str = "device",
                detail: str = "", now: datetime | None = None) -> None:
    """冷柜断电/恢复供电。断电立即生成 outage 事件与任务,
    恢复生成 restored 事件;状态以显式动作为准,读数不能推断供电。"""
    fz = conn.execute("SELECT * FROM freezers WHERE id=?", (freezer_id,)).fetchone()
    if fz is None:
        raise NotFound(f"冷柜 {freezer_id} 不存在")
    ts = iso(_dt(now))
    with tx(conn):
        if bool(fz["powered"]) == powered:
            return
        conn.execute("UPDATE freezers SET powered=? WHERE id=?", (1 if powered else 0, freezer_id))
        kind = "restored" if powered else "outage"
        conn.execute(
            """INSERT OR IGNORE INTO temp_incidents(freezer_id, store_id, kind, ts, detail)
               VALUES(?,?,?,?,?)""",
            (freezer_id, fz["store_id"], kind, ts, detail or ("供电恢复" if powered else "冷柜断电")),
        )
        if not powered:
            inc = conn.execute(
                "SELECT id FROM temp_incidents WHERE freezer_id=? AND kind='outage' AND ts=?",
                (freezer_id, ts),
            ).fetchone()
            _add_task(
                conn, fz["store_id"], "outage",
                f"冷柜断电:{fz['code']}",
                f"{fz['name']}({fz['code']}) 于 {ts} 断电,需立即处置在柜冰淇淋。{detail}".strip(),
                "incident", inc["id"], severity="high", now=now,
            )
        else:
            conn.execute(
                """UPDATE temp_incidents SET detail=detail||' / 恢复:'||?
                   WHERE freezer_id=? AND kind='outage' AND handled_ts IS NULL""",
                (ts, freezer_id),
            )


def handle_incident(conn, *, incident_id: int, resolution: str, handled_by: str,
                    now: datetime | None = None) -> None:
    """处置温控事件:resolved(恢复正常)/ discarded(报损)/ exchanged(换货)。

    处置后事件冻结,任何迟到读数都无法再改变它。
    """
    if resolution not in ("resolved", "discarded", "exchanged"):
        raise DomainError("处置方式必须为 resolved/discarded/exchanged")
    with tx(conn):
        inc = conn.execute("SELECT * FROM temp_incidents WHERE id=?", (incident_id,)).fetchone()
        if inc is None:
            raise NotFound("事件不存在")
        if inc["handled_ts"]:
            raise IllegalTransition("事件已处置,结果不可更改")
        conn.execute(
            "UPDATE temp_incidents SET handled_ts=?, handled_by=?, resolution=? WHERE id=?",
            (iso(_dt(now)), handled_by, resolution, incident_id),
        )
        _close_task(conn, inc["store_id"],
                    "temp_breach" if inc["kind"] == "excursion" else "outage",
                    "incident", incident_id, now)


# ---------------------------------------------------------------- 换货单

def create_exchange(conn, *, store_id: str, channel_id: str, batch_id: str, qty: int,
                    reason: str, staff_user: str, now: datetime | None = None) -> str:
    """店员发起换货。仅在柜、未锁定批次可换;创建即把数量从可售库存划出
    (qty_exchanged),保证后续售卖/重复换货不可能再动用这批货。"""
    if reason not in ("expiry", "package_damage", "temp_breach"):
        raise DomainError("换货原因非法")
    if qty <= 0:
        raise DomainError("换货数量必须为正")
    if not staff_user:
        raise DomainError("缺少店员")
    _check_store(conn, "channels", channel_id, store_id)
    _check_store(conn, "batches", batch_id, store_id)
    with tx(conn):
        b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if b["locked"]:
            raise BatchLockedError(f"批次 {b['batch_no']} 已锁定,不能换货")
        cb = _batch_in_cabinet(conn, channel_id, batch_id)
        if cb is None:
            raise BatchNotInCabinetError("换货只能针对在柜批次")
        if cb["qty_remaining"] < qty:
            raise InsufficientStockError(
                f"在柜可换余量 {cb['qty_remaining']} 不足 {qty}(已报损数量不可计入)"
            )
        oid = f"EX-{store_id}-{_dt(now).strftime('%m%d%H%M%S')}-{batch_id[-4:]}"
        ts = iso(_dt(now))
        conn.execute(
            """INSERT INTO exchange_orders(id, store_id, freezer_id, channel_id, batch_id, qty,
                                           reason, status, staff_user, staff_ts, created_ts)
               SELECT ?,?,?,?,?,?,?, 'staff_confirmed', ?,?,?
               FROM channels WHERE id=?""",
            (oid, store_id, conn.execute("SELECT freezer_id FROM channels WHERE id=?",
                                         (channel_id,)).fetchone()["freezer_id"],
             channel_id, batch_id, qty, reason, staff_user, ts, ts, channel_id),
        )
        conn.execute("UPDATE channel_batches SET qty_remaining=qty_remaining-? WHERE channel_id=? AND batch_id=?",
                     (qty, channel_id, batch_id))
        conn.execute("UPDATE batches SET qty_exchanged=qty_exchanged+? WHERE id=?", (qty, batch_id))
        conn.execute(
            """INSERT INTO stock_moves(store_id, ts, kind, channel_id, batch_id, qty, ref, actor)
               VALUES(?,?,'exchange_out',?,?,?,?,?)""",
            (store_id, ts, channel_id, batch_id, qty, oid, staff_user),
        )
        reconcile_channel_mix(conn, channel_id, now=now)
    return oid


def staff_confirm(conn, *, order_id: str, store_id: str, user: str) -> None:
    """店员确认(若换货由系统代建待店员确认时使用;正常路径创建即店员确认)。"""
    with tx(conn):
        o = conn.execute("SELECT * FROM exchange_orders WHERE id=?", (order_id,)).fetchone()
        if o is None:
            raise NotFound("换货单不存在")
        if o["store_id"] != store_id:
            raise StoreIsolationError("跨店换货单")
        if o["status"] not in ("pending", "staff_confirmed"):
            raise IllegalTransition(f"换货单状态 {o['status']} 不可店员确认")
        conn.execute(
            "UPDATE exchange_orders SET status='staff_confirmed', staff_user=?, staff_ts=COALESCE(staff_ts, ?) WHERE id=?",
            (user, iso(utcnow()), order_id),
        )


def courier_confirm(conn, *, order_id: str, store_id: str, user: str,
                    now: datetime | None = None) -> str:
    """配送员取货确认。必须已有店员确认,且配送员与店员不能为同一人。
    完成后开出供应商回收单。返回回收单 id。"""
    if not user:
        raise DomainError("缺少配送员")
    with tx(conn):
        o = conn.execute("SELECT * FROM exchange_orders WHERE id=?", (order_id,)).fetchone()
        if o is None:
            raise NotFound("换货单不存在")
        if o["store_id"] != store_id:
            raise StoreIsolationError("跨店换货单")
        if o["status"] != "staff_confirmed":
            raise IllegalTransition("需先由店员确认,配送员才能签收")
        if o["staff_user"] == user:
            raise IllegalTransition("配送员与店员不能为同一人,双向确认无效")
        b = conn.execute("SELECT * FROM batches WHERE id=?", (o["batch_id"],)).fetchone()
        supplier = conn.execute(
            "SELECT supplier_id FROM product_suppliers WHERE product_id=?", (b["product_id"],)
        ).fetchone()
        if supplier is None:
            raise DomainError("商品未配置供应商,无法生成回收单")
        rid = f"RC-{store_id}-{_dt(now).strftime('%m%d%H%M%S')}-{o['id'][-4:]}"
        ts = iso(_dt(now))
        conn.execute(
            """INSERT INTO recycling_orders(id, store_id, supplier_id, batch_id, qty_expected,
                                            status, created_ts)
               VALUES(?,?,?,?,?, 'open', ?)""",
            (rid, store_id, supplier["supplier_id"], o["batch_id"], o["qty"], ts),
        )
        conn.execute(
            """UPDATE exchange_orders SET status='courier_confirmed', courier_user=?,
                   courier_ts=?, completed_ts=?, recycle_id=? WHERE id=?""",
            (user, ts, ts, rid, order_id),
        )
    return rid


def reject_exchange(conn, *, order_id: str, store_id: str, user: str,
                    now: datetime | None = None) -> None:
    """拒收:换货单作废,划出的数量归还在柜可售库存(守恒回滚)。"""
    with tx(conn):
        o = conn.execute("SELECT * FROM exchange_orders WHERE id=?", (order_id,)).fetchone()
        if o is None:
            raise NotFound("换货单不存在")
        if o["store_id"] != store_id:
            raise StoreIsolationError("跨店换货单")
        if o["status"] not in ("pending", "staff_confirmed"):
            raise IllegalTransition("已完成的换货单不可拒收")
        conn.execute("UPDATE exchange_orders SET status='rejected' WHERE id=?", (order_id,))
        conn.execute(
            "UPDATE channel_batches SET qty_remaining=qty_remaining+? WHERE channel_id=? AND batch_id=?",
            (o["qty"], o["channel_id"], o["batch_id"]),
        )
        conn.execute("UPDATE batches SET qty_exchanged=qty_exchanged-? WHERE id=?",
                     (o["qty"], o["batch_id"]))
        conn.execute(
            """INSERT INTO stock_moves(store_id, ts, kind, channel_id, batch_id, qty, ref, actor)
               VALUES(?,?,'exchange_out',?,?,?, ?,?)""",
            (store_id, iso(_dt(now)), o["channel_id"], o["batch_id"], -o["qty"],
             order_id + ":rejected", user),
        )
        reconcile_channel_mix(conn, o["channel_id"], now=now)


# ---------------------------------------------------------------- 供应商回收

def receive_recycling(conn, *, recycling_id: str, qty: int, final: bool = True,
                      note: str = "", now: datetime | None = None) -> str:
    """供应商登记实收数量。

    * 实收 == 应收 => settled;
    * 实收 < 应收且 final=True => mismatch,生成 recycle_mismatch 任务;
    * 实收 < 应收且 final=False => partial,等待补交;
    * 实收 > 应收 => 直接 mismatch。
    只有实收数量计入 batches.qty_recycled。
    """
    if qty < 0:
        raise DomainError("回收数量不能为负")
    with tx(conn):
        r = conn.execute("SELECT * FROM recycling_orders WHERE id=?", (recycling_id,)).fetchone()
        if r is None:
            raise NotFound("回收单不存在")
        if r["status"] in ("settled", "mismatch"):
            raise IllegalTransition(f"回收单已终结({r['status']})")
        total_received = r["qty_received"] + qty
        ts = iso(_dt(now))
        if total_received > r["qty_expected"]:
            # 实收超过换货出柜数量会破坏 recycled <= exchanged 不变量,直接拒收
            raise DomainError(
                f"实收 {total_received} 超过应收 {r['qty_expected']},请核对后重新登记"
            )
        if total_received == r["qty_expected"]:
            status = "settled"
        else:
            status = "partial" if not final else "mismatch"
        conn.execute(
            "UPDATE recycling_orders SET qty_received=?, status=?, settled_ts=?, note=? WHERE id=?",
            (total_received, status, ts if status in ("settled", "mismatch") else None, note, recycling_id),
        )
        conn.execute("UPDATE batches SET qty_recycled=qty_recycled+? WHERE id=?",
                     (qty, r["batch_id"]))
        conn.execute(
            """INSERT INTO stock_moves(store_id, ts, kind, channel_id, batch_id, qty, ref, actor)
               VALUES(?,?,'recycle',NULL,?,?,?, 'supplier')""",
            (r["store_id"], ts, r["batch_id"], qty, recycling_id),
        )
        if status == "mismatch":
            diff = total_received - r["qty_expected"]
            _add_task(
                conn, r["store_id"], "recycle_mismatch",
                f"回收数量不符:{r['id']} 差 {diff:+d}",
                f"回收单 {r['id']} 应收 {r['qty_expected']} 件,实收 {total_received} 件"
                f"(差异 {diff:+d})。{note}".strip(),
                "recycling", r["id"], severity="high", now=now,
            )
        else:
            _close_task(conn, r["store_id"], "recycle_mismatch", "recycling", r["id"], now)
        return status
