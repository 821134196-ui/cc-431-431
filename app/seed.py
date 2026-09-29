"""演示数据:3 家门店、4 家供应商、6 种商品、4 台冷柜货道矩阵,
并在时间轴上制造全部六类任务样例:

  温控  FZ-S01-A 注入一段超温读数并已处置,随后补传"迟到超限读数"
        —— 曲线可见,但处置状态不被覆盖;
  断电  FZ-S02-A 断电未恢复,outage 任务挂起,设备停止实时上报;
  混放  FZ-S01-B 的 A3 货道强制装载两个在柜批次;
  临期  过期 / 3 天 / 7 天 / 14 天四档在柜批次;
  破损  FZ-S01-A 的 A6 货道报损 3 件;
  换货  完成双向签收、待配送员签收各一单;
  回收  结清、部分到货、数量不符(mismatch)三种回收单。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from . import domain
from .device import DeviceAdapter
from .tasks import scan_expiry

SHELF_DAYS = 540  # 冰淇淋标称保质期 ~18 个月


def _d(offset: int, today: date) -> tuple[str, str]:
    expiry = today + timedelta(days=offset)
    prod = expiry - timedelta(days=SHELF_DAYS)
    return prod.isoformat(), expiry.isoformat()


def seed_all(conn: sqlite3.Connection, device: DeviceAdapter | None = None,
             today: date | None = None) -> dict:
    today = today or date.today()
    now = domain.utcnow()

    if conn.execute("SELECT COUNT(*) AS c FROM stores").fetchone()["c"]:
        return {"skipped": "已存在演示数据"}

    device = device or DeviceAdapter(conn)

    # ------------------------------------------------------------ 主数据
    stores = [
        ("S01", "上海南京西路店", "上海", "林小满"),
        ("S02", "北京国贸店", "北京", "赵子航"),
        ("S03", "广州天河店", "广州", "陈乐晴"),
    ]
    conn.executemany("INSERT INTO stores(id,name,city,staff) VALUES(?,?,?,?)", stores)

    suppliers = [
        ("SUP-MH", "梦龙冷链华东", "400-626-0001", "周达"),
        ("SUP-BX", "八喜供应链", "400-628-0002", "吴运"),
        ("SUP-GM", "光明冷饮配送", "400-611-0003", "郑驰"),
        ("SUP-WL", "和路雪华南仓配", "400-622-0004", "钱冰"),
    ]
    conn.executemany("INSERT INTO suppliers(id,name,contact,courier) VALUES(?,?,?,?)", suppliers)

    products = [
        ("P-MLV", "ML-65-V",  "梦龙香草杏仁", "65g/支", "梦龙"),
        ("P-MLC", "ML-65-C",  "梦龙松露巧克力", "65g/支", "梦龙"),
        ("P-BXV", "BX-90-V",  "八喜香草杯", "90g/杯", "八喜"),
        ("P-BXS", "BX-90-S",  "八喜草莓杯", "90g/杯", "八喜"),
        ("P-GMD", "GM-65-D",  "光明大白兔雪糕", "65g/支", "光明"),
        ("P-KAD", "WL-67-K",  "可爱多甜筒", "67g/支", "和路雪"),
    ]
    conn.executemany("INSERT INTO products(id,sku,name,spec,brand) VALUES(?,?,?,?,?)", products)
    conn.executemany(
        "INSERT INTO product_suppliers(product_id, supplier_id) VALUES(?,?)",
        [("P-MLV", "SUP-MH"), ("P-MLC", "SUP-MH"), ("P-BXV", "SUP-BX"),
         ("P-BXS", "SUP-BX"), ("P-GMD", "SUP-GM"), ("P-KAD", "SUP-WL")],
    )

    # ------------------------------------------------------------ 冷柜与货道
    def make_freezer(fid, store, code, name, rows_n, cols_n, cap, tmin=-22.0, tmax=-18.0):
        conn.execute(
            """INSERT INTO freezers(id,store_id,code,name,rows_n,cols_n,temp_min,temp_max,powered)
               VALUES(?,?,?,?,?,?,?,?,1)""",
            (fid, store, code, name, rows_n, cols_n, tmin, tmax),
        )
        chans = []
        for r in range(rows_n):
            for c in range(cols_n):
                pos = f"{chr(65 + r)}{c + 1}"
                cid = f"CH-{fid.replace('-', '')}-{pos}"
                conn.execute(
                    """INSERT INTO channels(id,freezer_id,store_id,pos,row_idx,col_idx,capacity)
                       VALUES(?,?,?,?,?,?,?)""",
                    (cid, fid, store, pos, r, c, cap),
                )
                chans.append((pos, cid))
        return chans

    a1 = make_freezer("FZ-S01-A", "S01", "A-01", "海风1号柜", 4, 6, 16)
    b1 = make_freezer("FZ-S01-B", "S01", "B-01", "海风2号柜", 3, 5, 12)
    a2 = make_freezer("FZ-S02-A", "S02", "A-01", "京韵1号柜", 3, 5, 14, -22.0, -18.0)
    a3 = make_freezer("FZ-S03-A", "S03", "A-01", "珠水1号柜", 3, 5, 14, -22.0, -18.0)
    conn.commit()

    def ch(channels, pos):
        return dict(channels)[pos]

    for fid in ("FZ-S01-A", "FZ-S01-B", "FZ-S02-A", "FZ-S03-A"):
        device.register(fid, -22.0, -18.0, seed=hash(fid) & 0xFFFF)
        device.backfill(fid, hours=30, step_min=15, now=now)

    # ------------------------------------------------------------ 批次入柜
    def load(store, channels, pos, product, batch_no, offset, qty, force=False):
        prod, exp = _d(offset, today)
        return domain.load_batch(
            conn, store_id=store, channel_id=ch(channels, pos), product_id=product,
            batch_no=batch_no, prod_date=prod, expiry_date=exp, qty=qty, now=now,
            force=force,
        )

    # —— 上海店 A 柜:临期四档 + 包装破损
    load("S01", a1, "A1", "P-MLV", "ML-V-260301", -1, 10)    # 已过期
    load("S01", a1, "A2", "P-MLC", "ML-C-260303", 2, 14)     # 3 天内(urgent)
    load("S01", a1, "A3", "P-BXV", "BX-V-260307", 6, 12)     # 7 天内(warning)
    load("S01", a1, "A4", "P-BXS", "BX-S-260313", 12, 11)    # 14 天内(watch)
    load("S01", a1, "A5", "P-GMD", "GM-D-260430", 31, 16)    # 新鲜
    load("S01", a1, "A6", "P-KAD", "WL-K-260305", 4, 10)     # urgent,随后报损
    load("S01", a1, "B1", "P-MLV", "ML-V-260420", 21, 9)
    domain.sell(conn, store_id="S01", channel_id=ch(a1, "B1"), qty=2,
                actor="pos", now=now)

    # 包装破损:3 件外盒压损、内封破裂,单独记账不可回售
    domain.register_damage(
        conn, store_id="S01", channel_id=ch(a1, "A6"),
        batch_id="B-S01-WL-K-260305", qty=3,
        note="外盒压损、内封破裂,已隔离", actor="林小满", now=now,
    )

    # —— 上海店 B 柜:换货/回收全流程 + 批次混放
    load("S01", b1, "A1", "P-MLC", "ML-C-260228", 1, 12)
    load("S01", b1, "A2", "P-BXS", "BX-S-260302", 1, 9)
    load("S01", b1, "B1", "P-MLV", "ML-V-260304", 3, 10)
    load("S01", b1, "B2", "P-BXV", "BX-V-260306", 5, 8)
    load("S01", b1, "C1", "P-GMD", "GM-D-260501", 33, 12)

    # 已完成双向确认的换货单 + 回收结清
    ex_done = domain.create_exchange(
        conn, store_id="S01", channel_id=ch(b1, "A1"),
        batch_id="B-S01-ML-C-260228", qty=8, reason="expiry",
        staff_user="林小满", now=now,
    )
    rc_done = domain.courier_confirm(
        conn, order_id=ex_done, store_id="S01", user="周达", now=now)
    domain.receive_recycling(conn, recycling_id=rc_done, qty=8, now=now)

    # 店员已确认、等待配送员签收
    domain.create_exchange(
        conn, store_id="S01", channel_id=ch(b1, "A2"),
        batch_id="B-S01-BX-S-260302", qty=5, reason="expiry",
        staff_user="林小满", now=now,
    )

    # 回收数量不符:换货 6 件,供应商只回 4 件即结清
    ex_mm = domain.create_exchange(
        conn, store_id="S01", channel_id=ch(b1, "B1"),
        batch_id="B-S01-ML-V-260304", qty=6, reason="expiry",
        staff_user="林小满", now=now,
    )
    rc_mm = domain.courier_confirm(
        conn, order_id=ex_mm, store_id="S01", user="周达", now=now)
    domain.receive_recycling(
        conn, recycling_id=rc_mm, qty=4, final=True,
        note="配送箱少 2 支,司机当场签字确认", now=now,
    )

    # 回收部分到货(挂起,不产生 mismatch 任务)
    ex_pt = domain.create_exchange(
        conn, store_id="S01", channel_id=ch(b1, "B2"),
        batch_id="B-S01-BX-V-260306", qty=4, reason="package_damage",
        staff_user="林小满", now=now,
    )
    rc_pt = domain.courier_confirm(
        conn, order_id=ex_pt, store_id="S01", user="吴运", now=now)
    domain.receive_recycling(conn, recycling_id=rc_pt, qty=2, final=False, now=now)

    # 批次混放:旧批次未清空(卖 3 件余 7),强行把新批次塞进同一货道
    load("S01", b1, "A3", "P-KAD", "WL-K-260220", 0, 10)
    domain.sell(conn, store_id="S01", channel_id=ch(b1, "A3"), qty=3,
                actor="pos", now=now)
    load("S01", b1, "A3", "P-KAD", "WL-K-260325", 23, 6, force=True)

    # —— 北京店:冷柜断电(未处置)
    load("S02", a2, "A1", "P-MLV", "ML-V-260425", 26, 14)
    load("S02", a2, "A2", "P-KAD", "WL-K-260418", 19, 10)
    load("S02", a2, "B1", "P-BXV", "BX-V-260510", 41, 12)
    domain.set_powered(conn, freezer_id="FZ-S02-A", powered=False,
                       actor="gateway", detail="门店跳闸,设备离线", now=now)

    # —— 广州店:临期 + 一单正常换货回收
    load("S03", a3, "A1", "P-GMD", "GM-D-260315", 14, 12)
    load("S03", a3, "A2", "P-MLC", "ML-C-260505", 36, 14)
    load("S03", a3, "B1", "P-BXS", "BX-S-260310", 10, 10)
    load("S03", a3, "B2", "P-KAD", "WL-K-260520", 52, 12)
    ex_gz = domain.create_exchange(
        conn, store_id="S03", channel_id=ch(a3, "A2"),
        batch_id="B-S03-ML-C-260505", qty=6, reason="package_damage",
        staff_user="陈乐晴", now=now,
    )
    rc_gz = domain.courier_confirm(
        conn, order_id=ex_gz, store_id="S03", user="周达", now=now)
    domain.receive_recycling(conn, recycling_id=rc_gz, qty=6, now=now)

    # ------------------------------------------------------------ 温控样例
    # 1) 上海 A 柜 ~40 分钟前出现一段超温(均不踩 15 分钟历史栅格)
    t0 = now - timedelta(minutes=37)
    device.inject_excursion(
        "FZ-S01-A", temps=[-17.4, -16.1, -14.8, -13.9], at=t0, step_min=5)
    # 2) 店员按 SOP 处置整段超温事件:全部 resolved,任务关闭
    incs = conn.execute(
        """SELECT id FROM temp_incidents
           WHERE freezer_id='FZ-S01-A' AND kind='excursion' AND handled_ts IS NULL
           ORDER BY ts"""
    ).fetchall()
    for inc in incs:
        domain.handle_incident(conn, incident_id=inc["id"], resolution="resolved",
                               handled_by="林小满", now=now)
    # 3) 设备随后补传 2 小时前的迟到超限读数(-122 分钟避开历史栅格):
    #    曲线可见该点,但因其迟到且事件已处置,不再重开任何任务。
    device.inject_late_after_handling(
        "FZ-S01-A", temp_c=-13.2, reading_ts=now - timedelta(minutes=122))

    # ------------------------------------------------------------ 临期扫描
    opened = scan_expiry(conn, today=today)

    conn.commit()
    return {
        "stores": 3, "suppliers": 4, "products": 6, "freezers": 4,
        "channels": 24 + 15 + 15 + 15,
        "batches": conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"],
        "readings": conn.execute("SELECT COUNT(*) c FROM temp_readings").fetchone()["c"],
        "open_tasks": conn.execute("SELECT COUNT(*) c FROM tasks WHERE status='open'").fetchone()["c"],
        "expiry_tasks_opened": opened,
    }
