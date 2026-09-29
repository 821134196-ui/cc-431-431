"""系统测试:阈值边界、批次锁定、温度乱序/迟到、数量守恒、门店隔离、并发补货。

运行: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, domain
from app.device import DeviceAdapter
from app.tasks import scan_expiry, expiry_tier


def dt(minutes_offset: int) -> datetime:
    base = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    return base + timedelta(minutes=minutes_offset)


class FreezerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)
        db.init_db(self.conn)
        self._master_data()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _master_data(self):
        c = self.conn
        c.executemany("INSERT INTO stores(id,name,city,staff) VALUES(?,?,?,?)",
                      [("S01", "测试一店", "上海", "店员甲"), ("S02", "测试二店", "北京", "店员乙")])
        c.executemany("INSERT INTO suppliers(id,name,contact,courier) VALUES(?,?,?,?)",
                      [("SUP1", "供应商甲", "000", "配送甲"), ("SUP2", "供应商乙", "000", "配送乙")])
        c.execute("INSERT INTO products VALUES('P1','SKU1','香草雪糕','65g/支','测试牌')")
        c.execute("INSERT INTO product_suppliers VALUES('P1','SUP1')")
        c.execute("""INSERT INTO freezers(id,store_id,code,name,rows_n,cols_n,temp_min,temp_max,powered)
                     VALUES('F1','S01','A-01','一号柜',2,2,-22.0,-18.0,1)""")
        c.execute("""INSERT INTO freezers(id,store_id,code,name,rows_n,cols_n,temp_min,temp_max,powered)
                     VALUES('F2','S02','A-01','二号柜',2,2,-22.0,-18.0,1)""")
        for fid, store in (("F1", "S01"), ("F2", "S02")):
            for r in range(2):
                for col in range(2):
                    pos = f"{chr(65+r)}{col+1}"
                    c.execute("""INSERT INTO channels(id,freezer_id,store_id,pos,row_idx,col_idx,capacity)
                                 VALUES(?,?,?,?,?,?,10)""",
                              (f"CH-{fid}-{pos}", fid, store, pos, r, col))
        c.commit()

    # ---------- 小工具 ----------
    def load(self, channel="A1", batch="B001", qty=8, store="S01", fid="F1",
             expiry_offset=30, force=False):
        today = date(2026, 9, 29)
        prod = (today + timedelta(days=expiry_offset - 300)).isoformat()
        exp = (today + timedelta(days=expiry_offset)).isoformat()
        return domain.load_batch(
            self.conn, store_id=store, channel_id=f"CH-{fid}-{channel}",
            product_id="P1", batch_no=batch, prod_date=prod, expiry_date=exp,
            qty=qty, now=dt(0), force=force)

    def open_breaches(self, store="S01"):
        return self.conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE store_id=? AND kind='temp_breach' AND status='open'",
            (store,)).fetchone()["c"]

    def assert_conservation(self, bid):
        domain.check_conservation(self.conn, bid)


class TestThresholdBoundary(FreezerTestBase):
    """合格区间 -22℃~-18℃:边界点不告警,越界 0.1℃ 即告警。"""

    def test_exact_bounds_no_breach(self):
        domain.record_reading(self.conn, freezer_id="F1", temp_c=-18.0, reading_ts=dt(0))
        domain.record_reading(self.conn, freezer_id="F1", temp_c=-22.0, reading_ts=dt(1))
        domain.record_reading(self.conn, freezer_id="F1", temp_c=-20.0, reading_ts=dt(2))
        self.assertEqual(self.open_breaches(), 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM temp_incidents WHERE kind='excursion'")
            .fetchone()["c"], 0)

    def test_just_outside_bounds_breaches(self):
        domain.record_reading(self.conn, freezer_id="F1", temp_c=-17.9, reading_ts=dt(10))
        self.assertEqual(self.open_breaches(), 1)
        domain.record_reading(self.conn, freezer_id="F1", temp_c=-22.1, reading_ts=dt(11))
        self.assertEqual(self.open_breaches(), 2)

    def test_handling_closes_task_and_late_never_reopens(self):
        domain.record_reading(self.conn, freezer_id="F1", temp_c=-14.0, reading_ts=dt(-20))
        self.assertEqual(self.open_breaches(), 1)
        inc = self.conn.execute(
            "SELECT id FROM temp_incidents WHERE kind='excursion'").fetchone()
        domain.handle_incident(self.conn, incident_id=inc["id"], resolution="exchanged",
                               handled_by="店员甲", now=dt(-19))
        self.assertEqual(self.open_breaches(), 0)
        # 迟到 2 小时的超限读数:只补曲线,不重开任务
        ok = domain.record_reading(self.conn, freezer_id="F1", temp_c=-13.0,
                                   reading_ts=dt(-120), now=dt(0))
        self.assertTrue(ok)
        self.assertEqual(self.open_breaches(), 0)
        row = self.conn.execute(
            "SELECT late FROM temp_readings WHERE device_id='F1' AND temp_c=-13.0").fetchone()
        self.assertEqual(row["late"], 1)
        # 处置不可二次修改
        with self.assertRaises(domain.IllegalTransition):
            domain.handle_incident(self.conn, incident_id=inc["id"], resolution="resolved",
                                   handled_by="店员甲", now=dt(0))

    def test_new_on_time_excursion_after_handling_does_alarm(self):
        domain.record_reading(self.conn, freezer_id="F1", temp_c=-14.0, reading_ts=dt(-20))
        inc = self.conn.execute("SELECT id FROM temp_incidents").fetchone()
        domain.handle_incident(self.conn, incident_id=inc["id"], resolution="resolved",
                               handled_by="店员甲", now=dt(-19))
        # 处置之后发生的新超温(非迟到)必须正常告警
        domain.record_reading(self.conn, freezer_id="F1", temp_c=-13.5, reading_ts=dt(5))
        self.assertEqual(self.open_breaches(), 1)

    def test_duplicate_reading_idempotent(self):
        self.assertTrue(domain.record_reading(
            self.conn, freezer_id="F1", temp_c=-20.0, reading_ts=dt(0)))
        self.assertFalse(domain.record_reading(
            self.conn, freezer_id="F1", temp_c=-14.0, reading_ts=dt(0)))  # 同设备同时间


class TestOutOfOrderReadings(FreezerTestBase):
    """乱序/迟到入账:late 标记正确,历史不被覆盖。"""

    def test_late_flag(self):
        domain.record_reading(self.conn, freezer_id="F1", temp_c=-20.0, reading_ts=dt(10))
        domain.record_reading(self.conn, freezer_id="F1", temp_c=-19.5, reading_ts=dt(0))
        late = self.conn.execute(
            "SELECT late FROM temp_readings WHERE ts=?", (domain.iso(dt(0)),)).fetchone()
        self.assertEqual(late["late"], 1)
        not_late = self.conn.execute(
            "SELECT late FROM temp_readings WHERE ts=?", (domain.iso(dt(10)),)).fetchone()
        self.assertEqual(not_late["late"], 0)

    def test_backfill_plus_injection_via_adapter(self):
        dev = DeviceAdapter(self.conn)
        dev.register("F1", -22.0, -18.0, seed=1)
        n = dev.backfill("F1", hours=6, step_min=15, now=dt(200))
        self.assertGreater(n, 20)
        # 197 分钟偏移 + 1 分钟步进 => 落在 15 分钟历史栅格之外,不撞主键
        dev.inject_excursion("F1", temps=[-15.0, -14.2], at=dt(197), step_min=1)
        # 注入点早于最新历史读数 => 迟到;但无处置事件,照常告警
        self.assertGreaterEqual(self.open_breaches(), 2)

    def test_outage_stops_device_reports(self):
        domain.set_powered(self.conn, freezer_id="F1", powered=False, now=dt(0))
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM tasks WHERE kind='outage' AND status='open'").fetchone())
        dev = DeviceAdapter(self.conn)
        dev.register("F1", -22.0, -18.0, seed=2)
        n = dev.tick(now=dt(1))
        self.assertEqual(n, 0)  # 断电离线不上报
        domain.set_powered(self.conn, freezer_id="F1", powered=True, now=dt(2))
        self.assertEqual(dev.tick(now=dt(3)), 1)


class TestBatchLocking(FreezerTestBase):
    """批次锁定 + 报损数量不可回售。"""

    def test_damaged_qty_never_sellable(self):
        bid = self.load(qty=8)
        domain.register_damage(self.conn, store_id="S01", channel_id="CH-F1-A1",
                               batch_id=bid, qty=3, actor="店员甲", now=dt(0))
        # 在柜剩 5;尝试卖 6 必须失败,报损的 3 件不可能回流
        with self.assertRaises(domain.InsufficientStockError):
            domain.sell(self.conn, store_id="S01", channel_id="CH-F1-A1", qty=6, now=dt(1))
        domain.sell(self.conn, store_id="S01", channel_id="CH-F1-A1", qty=5, now=dt(2))
        self.assert_conservation(bid)

    def test_full_damage_locks_batch(self):
        bid = self.load(qty=4)
        domain.register_damage(self.conn, store_id="S01", channel_id="CH-F1-A1",
                               batch_id=bid, qty=4, actor="店员甲", now=dt(0))
        b = self.conn.execute("SELECT locked, locked_reason FROM batches WHERE id=?", (bid,)).fetchone()
        self.assertEqual(b["locked"], 1)
        with self.assertRaises(domain.BatchLockedError):
            domain.sell(self.conn, store_id="S01", channel_id="CH-F1-A1", qty=1, now=dt(1))
        with self.assertRaises(domain.BatchLockedError):
            domain.create_exchange(self.conn, store_id="S01", channel_id="CH-F1-A1",
                                   batch_id=bid, qty=1, reason="expiry",
                                   staff_user="店员甲", now=dt(1))
        # 包装破损任务
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM tasks WHERE kind='package_damage' AND status='open'").fetchone())

    def test_exchange_only_for_in_cabinet_batch(self):
        # 批次存在于 A2,却尝试对 A1 换货 -> 非本货道在柜批次
        self.load(channel="A2", batch="ELSE", qty=4)
        with self.assertRaises(domain.BatchNotInCabinetError):
            domain.create_exchange(self.conn, store_id="S01", channel_id="CH-F1-A1",
                                   batch_id="B-S01-ELSE", qty=1, reason="expiry",
                                   staff_user="店员甲", now=dt(0))


class TestQuantityConservation(FreezerTestBase):
    """收货 = 售 + 损 + 换 + 在柜;回收 ≤ 换货;拒收回柜。"""

    def test_full_lifecycle_conservation(self):
        bid = self.load(qty=10)
        domain.sell(self.conn, store_id="S01", channel_id="CH-F1-A1", qty=2, now=dt(1))
        domain.register_damage(self.conn, store_id="S01", channel_id="CH-F1-A1",
                               batch_id=bid, qty=1, actor="店员甲", now=dt(2))
        oid = domain.create_exchange(self.conn, store_id="S01", channel_id="CH-F1-A1",
                                     batch_id=bid, qty=3, reason="expiry",
                                     staff_user="店员甲", now=dt(3))
        rid = domain.courier_confirm(self.conn, order_id=oid, store_id="S01",
                                     user="配送甲", now=dt(4))
        domain.receive_recycling(self.conn, recycling_id=rid, qty=3, now=dt(5))
        b = self.conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
        self.assertEqual((b["qty_received"], b["qty_sold"], b["qty_damaged"],
                          b["qty_exchanged"], b["qty_recycled"]), (10, 2, 1, 3, 3))
        self.assert_conservation(bid)
        # 已换货出柜的货不能再卖
        with self.assertRaises(domain.InsufficientStockError):
            domain.sell(self.conn, store_id="S01", channel_id="CH-F1-A1", qty=5, now=dt(6))
        domain.sell(self.conn, store_id="S01", channel_id="CH-F1-A1", qty=4, now=dt(6))
        self.assert_conservation(bid)

    def test_reject_returns_qty(self):
        bid = self.load(qty=6)
        oid = domain.create_exchange(self.conn, store_id="S01", channel_id="CH-F1-A1",
                                     batch_id=bid, qty=4, reason="expiry",
                                     staff_user="店员甲", now=dt(0))
        domain.reject_exchange(self.conn, order_id=oid, store_id="S01",
                               user="店员甲", now=dt(1))
        self.assert_conservation(bid)
        cb = self.conn.execute(
            "SELECT qty_remaining FROM channel_batches WHERE channel_id='CH-F1-A1' AND active=1"
        ).fetchone()
        self.assertEqual(cb["qty_remaining"], 6)
        # 回柜后可正常售卖
        domain.sell(self.conn, store_id="S01", channel_id="CH-F1-A1", qty=6, now=dt(2))

    def test_over_receipt_rejected(self):
        bid = self.load(qty=5)
        oid = domain.create_exchange(self.conn, store_id="S01", channel_id="CH-F1-A1",
                                     batch_id=bid, qty=4, reason="expiry",
                                     staff_user="店员甲", now=dt(0))
        rid = domain.courier_confirm(self.conn, order_id=oid, store_id="S01",
                                     user="配送甲", now=dt(1))
        # 实收超过换货出柜数量:拒绝登记,守恒不变量不被破坏
        with self.assertRaises(domain.DomainError):
            domain.receive_recycling(self.conn, recycling_id=rid, qty=5, now=dt(2))
        self.assert_conservation(bid)

    def test_short_receipt_final_is_mismatch(self):
        bid = self.load(qty=5)
        oid = domain.create_exchange(self.conn, store_id="S01", channel_id="CH-F1-A1",
                                     batch_id=bid, qty=4, reason="expiry",
                                     staff_user="店员甲", now=dt(0))
        rid = domain.courier_confirm(self.conn, order_id=oid, store_id="S01",
                                     user="配送甲", now=dt(1))
        status = domain.receive_recycling(self.conn, recycling_id=rid, qty=3, final=True, now=dt(2))
        self.assertEqual(status, "mismatch")
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM tasks WHERE kind='recycle_mismatch' AND status='open'").fetchone())
        with self.assertRaises(domain.IllegalTransition):  # 已终结
            domain.receive_recycling(self.conn, recycling_id=rid, qty=1, now=dt(3))

    def test_partial_then_topup_settles(self):
        bid = self.load(qty=4)
        oid = domain.create_exchange(self.conn, store_id="S01", channel_id="CH-F1-A1",
                                     batch_id=bid, qty=4, reason="package_damage",
                                     staff_user="店员甲", now=dt(0))
        rid = domain.courier_confirm(self.conn, order_id=oid, store_id="S01",
                                     user="配送甲", now=dt(1))
        s1 = domain.receive_recycling(self.conn, recycling_id=rid, qty=2, final=False, now=dt(2))
        self.assertEqual(s1, "partial")
        s2 = domain.receive_recycling(self.conn, recycling_id=rid, qty=2, now=dt(3))
        self.assertEqual(s2, "settled")
        self.assertFalse(self.conn.execute(
            "SELECT 1 FROM tasks WHERE kind='recycle_mismatch' AND status='open'").fetchone())

    def test_replenish_capacity_and_concurrency(self):
        bid = self.load(qty=6)
        domain.replenish(self.conn, idem_key="R-1", store_id="S01",
                         channel_id="CH-F1-A1", batch_id=bid, qty=4, now=dt(0))
        with self.assertRaises(domain.DomainError):  # 6+4 已达容量 10
            domain.replenish(self.conn, idem_key="R-2", store_id="S01",
                             channel_id="CH-F1-A1", batch_id=bid, qty=1, now=dt(1))
        # 幂等:同键重复直接拒
        with self.assertRaises(domain.ReplenishedError):
            domain.replenish(self.conn, idem_key="R-1", store_id="S01",
                             channel_id="CH-F1-A1", batch_id=bid, qty=4, now=dt(2))
        self.assert_conservation(bid)

    def test_concurrent_replenish_succeeds_once(self):
        bid = self.load(qty=2)  # 容量 10,两线程各补 4 物理上都能放下
        results: list[str] = []
        barrier = threading.Barrier(2)

        def worker():
            conn2 = db.connect(self.tmp.name)
            barrier.wait()
            try:
                domain.replenish(conn2, idem_key="RACE-1", store_id="S01",
                                 channel_id="CH-F1-A1", batch_id=bid, qty=4, now=dt(0))
                results.append("ok")
            except domain.ReplenishedError:
                results.append("dup")
            except Exception as e:  # noqa: BLE001
                results.append(f"err:{e}")
            finally:
                conn2.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["dup", "ok"])
        b = self.conn.execute("SELECT qty_received FROM batches WHERE id=?", (bid,)).fetchone()
        self.assertEqual(b["qty_received"], 6)  # 2 初始 + 恰好一次补货 4
        cb = self.conn.execute(
            "SELECT qty_remaining FROM channel_batches WHERE channel_id='CH-F1-A1' AND active=1"
        ).fetchone()
        self.assertEqual(cb["qty_remaining"], 6)


class TestMixedBatch(FreezerTestBase):
    def test_mixed_load_opens_and_clears_task(self):
        self.load(channel="A1", batch="OLD", qty=5)
        domain.sell(self.conn, store_id="S01", channel_id="CH-F1-A1", qty=2, now=dt(1))
        # 正常路径拒绝混放
        with self.assertRaises(domain.ChannelOccupiedError):
            self.load(channel="A1", batch="NEW", qty=3)
        # 演示/整改入口 force 制造混放 -> 任务
        self.load(channel="A1", batch="NEW", qty=3, force=True)
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM tasks WHERE kind='mixed_batch' AND status='open'").fetchone())
        # 混放期间禁售
        with self.assertRaises(domain.BatchNotInCabinetError):
            domain.sell(self.conn, store_id="S01", channel_id="CH-F1-A1", qty=1, now=dt(2))
        # 把旧批次换货清空 -> 混放解除,任务关闭
        domain.create_exchange(self.conn, store_id="S01", channel_id="CH-F1-A1",
                               batch_id="B-S01-OLD", qty=3, reason="expiry",
                               staff_user="店员甲", now=dt(3))
        self.assertFalse(self.conn.execute(
            "SELECT 1 FROM tasks WHERE kind='mixed_batch' AND status='open'").fetchone())
        # 单一批次后恢复售卖
        domain.sell(self.conn, store_id="S01", channel_id="CH-F1-A1", qty=1, now=dt(4))


class TestStoreIsolation(FreezerTestBase):
    def test_cannot_touch_other_store_channel(self):
        bid = self.load(store="S01", fid="F1")
        with self.assertRaises(domain.StoreIsolationError):
            domain.sell(self.conn, store_id="S02", channel_id="CH-F1-A1", qty=1, now=dt(0))
        with self.assertRaises(domain.StoreIsolationError):
            domain.create_exchange(self.conn, store_id="S02", channel_id="CH-F1-A1",
                                   batch_id=bid, qty=1, reason="expiry",
                                   staff_user="店员乙", now=dt(0))
        with self.assertRaises(domain.StoreIsolationError):
            domain.replenish(self.conn, idem_key="X", store_id="S02",
                             channel_id="CH-F1-A1", batch_id=bid, qty=1, now=dt(0))

    def test_other_store_has_independent_inventory(self):
        self.load(store="S01", fid="F1", batch="S1B", qty=5)
        self.load(store="S02", fid="F2", batch="S2B", qty=7)
        n1 = self.conn.execute(
            "SELECT COALESCE(SUM(qty_remaining),0) s FROM channel_batches cb "
            "JOIN channels c ON c.id=cb.channel_id WHERE c.freezer_id='F1'").fetchone()["s"]
        n2 = self.conn.execute(
            "SELECT COALESCE(SUM(qty_remaining),0) s FROM channel_batches cb "
            "JOIN channels c ON c.id=cb.channel_id WHERE c.freezer_id='F2'").fetchone()["s"]
        self.assertEqual((n1, n2), (5, 7))

    def test_cross_store_courier_confirm_rejected(self):
        bid = self.load(store="S01", fid="F1", qty=3)
        oid = domain.create_exchange(self.conn, store_id="S01", channel_id="CH-F1-A1",
                                     batch_id=bid, qty=3, reason="expiry",
                                     staff_user="店员甲", now=dt(0))
        with self.assertRaises(domain.StoreIsolationError):
            domain.courier_confirm(self.conn, order_id=oid, store_id="S02",
                                   user="配送甲", now=dt(1))


class TestDualConfirmation(FreezerTestBase):
    def test_staff_then_courier_flow(self):
        bid = self.load(qty=4)
        oid = domain.create_exchange(self.conn, store_id="S01", channel_id="CH-F1-A1",
                                     batch_id=bid, qty=4, reason="temp_breach",
                                     staff_user="店员甲", now=dt(0))
        with self.assertRaises(domain.IllegalTransition):  # 店员不能自己签收
            domain.courier_confirm(self.conn, order_id=oid, store_id="S01",
                                   user="店员甲", now=dt(1))
        rid = domain.courier_confirm(self.conn, order_id=oid, store_id="S01",
                                     user="配送甲", now=dt(2))
        o = self.conn.execute("SELECT status, recycle_id FROM exchange_orders WHERE id=?",
                              (oid,)).fetchone()
        self.assertEqual(o["status"], "courier_confirmed")
        self.assertEqual(o["recycle_id"], rid)
        with self.assertRaises(domain.IllegalTransition):  # 已完成不可重复
            domain.courier_confirm(self.conn, order_id=oid, store_id="S01",
                                   user="配送乙", now=dt(3))


class TestExpiryTiers(unittest.TestCase):
    def test_tier_boundaries(self):
        self.assertEqual(expiry_tier(0), "expired")
        self.assertEqual(expiry_tier(-5), "expired")
        self.assertEqual(expiry_tier(1), "urgent")
        self.assertEqual(expiry_tier(3), "urgent")
        self.assertEqual(expiry_tier(4), "warning")
        self.assertEqual(expiry_tier(7), "warning")
        self.assertEqual(expiry_tier(8), "watch")
        self.assertEqual(expiry_tier(14), "watch")
        self.assertEqual(expiry_tier(15), "fresh")

    def test_scan_opens_and_closes_expiry_tasks(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        conn = db.connect(tmp.name)
        db.init_db(conn)
        base = FreezerTestBase()
        base.conn = conn
        base.tmp = type("T", (), {"name": tmp.name})()
        base._master_data()
        base.load(batch="EXP", qty=3, expiry_offset=2)
        opened = scan_expiry(conn, today=date(2026, 9, 29))
        self.assertGreaterEqual(opened, 1)
        self.assertTrue(conn.execute(
            "SELECT 1 FROM tasks WHERE kind='expiry' AND status='open' AND ref_id='B-S01-EXP'"
        ).fetchone())
        # 全部换货出柜 -> 下次扫描关闭任务
        domain.create_exchange(conn, store_id="S01", channel_id="CH-F1-A1",
                               batch_id="B-S01-EXP", qty=3, reason="expiry",
                               staff_user="店员甲", now=dt(0))
        scan_expiry(conn, today=date(2026, 9, 29))
        self.assertFalse(conn.execute(
            "SELECT 1 FROM tasks WHERE kind='expiry' AND status='open' AND ref_id='B-S01-EXP'"
        ).fetchone())
        conn.close()
        os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
