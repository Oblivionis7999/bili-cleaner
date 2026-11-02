"""请求限速与风控冷却状态机。

设计依据 docs/adr/0002-risk-control.md：
- 每条请求前有基础间隔 + 随机抖动；
- 每处理 ``batch_size`` 条后休息 ``batch_pause_seconds``；
- 连续操作 ``FORCE_REST_AFTER`` 次后强制长休息；
- 收到风控码（-352/-412/-509/-799）后冷却 ``cooldown_seconds``，
  连续冷却超过 ``max_cooldowns`` 次就告诉调用方"该停了"。

同一个实例应该同时交给 ``BiliClient`` 和任务执行器使用：前者在每个 HTTP
请求前调用 :meth:`wait`，后者在风控时调用 :meth:`on_risk`，两边共享同一份
冷却状态，才不会出现"执行器以为在冷却、客户端却继续发请求"的错位。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Callable

from ..const import RISK_CODES

# 连续操作多少次后强制长休息（ADR: 连续 300 条后休息 10 分钟）
FORCE_REST_AFTER = 300
FORCE_REST_SECONDS = 600.0

# 睡眠切片长度：把长 sleep 切成小块，让暂停/停止能及时生效
_SLEEP_SLICE = 0.2


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class RateLimiter:
    """保守限速器，线程安全。"""

    def __init__(
        self,
        settings: dict,
        *,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self._lock = threading.Lock()
        # 允许测试注入假时钟/假睡眠
        self._sleeper = sleeper or time.sleep
        self._clock = clock or time.monotonic
        self._abort_check: Callable[[], bool] | None = None
        self.update_settings(settings or {})
        self.reset()

    # ------------------------------------------------------------ 配置

    def update_settings(self, settings: dict) -> None:
        """热更新限速参数（设置页修改后调用），不清空运行时计数。"""
        settings = settings or {}
        with self._lock:
            self.delay_seconds = max(0.0, _as_float(settings.get("delay_seconds"), 2.5))
            self.jitter_seconds = max(0.0, _as_float(settings.get("jitter_seconds"), 0.8))
            self.batch_size = max(1, _as_int(settings.get("batch_size"), 20))
            self.batch_pause_seconds = max(0.0, _as_float(settings.get("batch_pause_seconds"), 3.0))
            self.cooldown_seconds = max(0.0, _as_float(settings.get("cooldown_seconds"), 600.0))
            self.max_cooldowns = max(1, _as_int(settings.get("max_cooldowns"), 2))

    def reset(self) -> None:
        """重置运行时状态（每个新任务开始时调用）。"""
        with self._lock:
            self._batch_count = 0
            self._streak = 0
            self._consecutive_risk = 0
            self._cooldown_until = 0.0  # monotonic
            self._cooldown_until_ts = 0.0  # 墙上时钟，给任务展示
            self._last_request = 0.0
            self._last_risk_code: int | None = None
            self._requests = 0

    def set_abort_check(self, check: Callable[[], bool] | None) -> None:
        """注入中断检查：长睡眠中若返回 True 就提前结束（用于停止任务/退出程序）。"""
        self._abort_check = check

    @staticmethod
    def is_risk_code(code: object) -> bool:
        """判断一个 B 站业务 code 是否属于风控码。"""
        try:
            return int(code) in RISK_CODES  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    # ------------------------------------------------------------ 请求节奏

    def wait(self) -> None:
        """每条请求前调用：阻塞到下次允许发请求的时间，并结算批次/强制休息。"""
        with self._lock:
            now = self._clock()
            target = 0.0
            if self._last_request:
                target = self._last_request + self.delay_seconds
                if self.jitter_seconds:
                    target += random.uniform(0.0, self.jitter_seconds)
            target = max(target, self._cooldown_until)
            sleep_s = target - now

            # 第 N 条请求发出前先结算"上一批"的休息，避免批内多睡一次
            rest_s = 0.0
            if self._batch_count >= self.batch_size:
                self._batch_count = 0
                rest_s = self.batch_pause_seconds
            if self._streak >= FORCE_REST_AFTER:
                self._streak = 0
                rest_s = max(rest_s, FORCE_REST_SECONDS)

        if sleep_s > 0:
            self._sleep(sleep_s)
        if rest_s > 0:
            self._sleep(rest_s)

        with self._lock:
            self._last_request = self._clock()
            self._batch_count += 1
            self._streak += 1
            self._requests += 1

    def on_success(self) -> None:
        """一次写操作成功后调用：清掉连续风控计数。"""
        with self._lock:
            self._consecutive_risk = 0

    def on_risk(self, code: object) -> bool:
        """处理风控返回。

        返回 ``True`` 表示调用方应等冷却结束后重试当前对象；
        返回 ``False`` 表示连续冷却次数已用尽，应立即停止任务。
        """
        with self._lock:
            self._consecutive_risk += 1
            try:
                self._last_risk_code = int(code)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                self._last_risk_code = None
            if self._consecutive_risk > self.max_cooldowns:
                return False
            self._cooldown_until = self._clock() + self.cooldown_seconds
            self._cooldown_until_ts = time.time() + self.cooldown_seconds
            # 冷却后重新开始算批次，避免一恢复就撞上批休息
            self._batch_count = 0
            return True

    # ------------------------------------------------------------ 状态查询

    @property
    def consecutive_risks(self) -> int:
        with self._lock:
            return self._consecutive_risk

    @property
    def last_risk_code(self) -> int | None:
        with self._lock:
            return self._last_risk_code

    def cooldown_remaining(self) -> float:
        """距离冷却结束还剩多少秒（未冷却返回 0）。"""
        with self._lock:
            return max(0.0, self._cooldown_until - self._clock())

    def cooldown_until_iso(self) -> str | None:
        """冷却结束时间的 ISO 字符串；当前没有冷却则返回 None。"""
        with self._lock:
            if self._cooldown_until_ts <= 0 or self._cooldown_until - self._clock() <= 0:
                return None
            ts = self._cooldown_until_ts
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))

    def stats(self) -> dict:
        """给日志/排障用的快照。"""
        with self._lock:
            return {
                "requests": self._requests,
                "batch_count": self._batch_count,
                "streak": self._streak,
                "consecutive_risks": self._consecutive_risk,
                "last_risk_code": self._last_risk_code,
                "cooldown_remaining": round(max(0.0, self._cooldown_until - self._clock()), 2),
            }

    # ------------------------------------------------------------ 内部

    def _sleep(self, seconds: float) -> None:
        """可中断的分片睡眠。"""
        if seconds <= 0:
            return
        end = self._clock() + seconds
        while True:
            if self._abort_check is not None and self._abort_check():
                return
            remaining = end - self._clock()
            if remaining <= 0:
                return
            self._sleeper(min(remaining, _SLEEP_SLICE))
