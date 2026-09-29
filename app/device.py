"""本地设备适配器:模拟冷柜温控探头上报温度。

真实部署中这一层对接网关 MQTT/HTTP;此处用进程内模拟器:
* 每台冷柜一个随机游走温度序列,围绕合格区间中点波动;
* 断电的设备停止上报(设备离线),供电恢复后可补传缓存读数;
* 支持补传历史序列(backfill)、注入超限与乱序/迟到读数,
  全部经 domain.record_reading 入账,迟到语义由领域层裁决。
"""
from __future__ import annotations

import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from . import domain


class DeviceProfile:
    def __init__(self, freezer_id: str, target_c: float, noise: float = 0.35, seed: int = 0):
        self.freezer_id = freezer_id
        self.target = target_c
        self.noise = noise
        self._rand = random.Random(seed)
        self._cur = target_c

    def sample(self) -> float:
        # 随机游走 + 向目标回归
        self._cur += self._rand.uniform(-self.noise, self.noise)
        self._cur += (self.target - self._cur) * 0.25
        return round(self._cur, 1)

    def force(self, temp_c: float) -> None:
        self._cur = temp_c


class DeviceAdapter:
    """温控设备模拟器。线程安全:后台轮询线程与 Web 线程共用一个连接。"""

    def __init__(self, conn: sqlite3.Connection, interval_sec: float = 10.0):
        self.conn = conn
        self.interval = interval_sec
        self.profiles: dict[str, DeviceProfile] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 注册

    def register(self, freezer_id: str, temp_min: float, temp_max: float,
                 seed: int | None = None) -> None:
        target = round((temp_min + temp_max) / 2, 1)
        if seed is None:
            seed = abs(hash(freezer_id)) % 100000
        self.profiles[freezer_id] = DeviceProfile(freezer_id, target, seed=seed)

    # ------------------------------------------------------------ 历史补传

    def backfill(self, freezer_id: str, *, hours: float = 30, step_min: int = 15,
                 now: datetime | None = None, warm: bool = False) -> int:
        """按时间顺序补传过去 hours 小时的读数。返回入账条数。

        时间戳对齐到墙钟 step_min 网格(秒归零),因此与调用方注入的
        任意偏移读数几乎不可能撞主键,便于叠加事件场景。
        """
        prof = self.profiles[freezer_id]
        end = now or domain.utcnow()
        start0 = end - timedelta(hours=hours)
        start = start0.replace(second=0, microsecond=0)
        start = start.replace(minute=(start.minute // step_min) * step_min)
        n = int((end - start).total_seconds() / (step_min * 60))
        count = 0
        for i in range(n + 1):
            ts = start + timedelta(minutes=step_min * i)
            if warm:
                temp = round(prof.target + 6 + random.Random(i).random() * 1.5, 1)
            else:
                temp = prof.sample()
            if domain.record_reading(self.conn, freezer_id=freezer_id, temp_c=temp,
                                     reading_ts=ts, now=end):
                count += 1
        return count

    def inject_excursion(self, freezer_id: str, *, temps: list[float],
                         at: datetime | None = None, step_min: int = 5) -> None:
        """在 at(默认 25 分钟前)注入一段超限读数,按时间顺序上报。"""
        at = at or (domain.utcnow() - timedelta(minutes=25))
        for i, t in enumerate(temps):
            domain.record_reading(
                self.conn, freezer_id=freezer_id, temp_c=t,
                reading_ts=at + timedelta(minutes=step_min * i),
            )
            self.profiles[freezer_id].force(t)

    def inject_late_after_handling(self, freezer_id: str, temp_c: float,
                                   reading_ts: datetime) -> None:
        """补传一条历史读数:其时间早于库内最新读数 => late=1。
        领域层保证它不会覆盖处置后的事件状态。"""
        domain.record_reading(
            self.conn, freezer_id=freezer_id, temp_c=temp_c,
            reading_ts=reading_ts, now=domain.utcnow(),
        )

    # ------------------------------------------------------------ 实时轮询

    def tick(self, now: datetime | None = None) -> int:
        """为每台在线冷柜产生一条当前读数。返回上报条数。"""
        n = 0
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, powered FROM freezers"
            ).fetchall()
            for fz in rows:
                if not fz["powered"]:
                    continue  # 断电设备离线,不上报
                prof = self.profiles.get(fz["id"])
                if prof is None:
                    continue
                domain.record_reading(
                    self.conn, freezer_id=fz["id"], temp_c=prof.sample(),
                    reading_ts=domain.utcnow(), now=now,
                )
                n += 1
        return n

    def start(self) -> None:
        if self._thread:
            return

        def _loop():
            while not self._stop.wait(self.interval):
                try:
                    self.tick()
                except Exception as exc:  # 模拟器永不因单条失败退出
                    print(f"[device] tick 失败: {exc}")

        self._thread = threading.Thread(target=_loop, name="device-adapter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
