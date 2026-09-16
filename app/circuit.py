# -*- coding: utf-8 -*-
"""熔断器：CLOSED -> OPEN -> HALF_OPEN 三态。

为什么需要：某个供应商挂了以后，如果每个请求都老老实实等它超时（60s），
网关会被拖死。熔断后直接跳过它，把请求给备用链路，恢复期到了再放一个试探请求。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class BreakerState(str, Enum):
    CLOSED = 'closed'        # 正常放行
    OPEN = 'open'            # 熔断，直接拒绝
    HALF_OPEN = 'half_open'  # 试探放行一个


@dataclass
class CircuitBreaker:
    name: str
    fail_threshold: int = 3
    recovery_seconds: float = 30.0
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    total_calls: int = 0
    total_failures: int = 0
    total_trips: int = 0
    _half_open_inflight: bool = False

    # ---------- 准入 ----------
    def allow(self, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        if self.state is BreakerState.CLOSED:
            return True
        if self.state is BreakerState.OPEN:
            if now - self.opened_at >= self.recovery_seconds:
                self.state = BreakerState.HALF_OPEN
                self._half_open_inflight = True
                return True
            return False
        # HALF_OPEN：只放一个试探请求过去
        if not self._half_open_inflight:
            self._half_open_inflight = True
            return True
        return False

    # ---------- 结果上报 ----------
    def record_success(self) -> None:
        self.total_calls += 1
        self.consecutive_failures = 0
        self._half_open_inflight = False
        self.state = BreakerState.CLOSED

    def record_failure(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self.total_calls += 1
        self.total_failures += 1
        self._half_open_inflight = False
        self.consecutive_failures += 1
        # 半开状态一旦失败，立刻重新熔断；闭态则累计到阈值才熔断
        should_open = (self.state is BreakerState.HALF_OPEN
                       or self.consecutive_failures >= self.fail_threshold)
        if should_open and self.state is not BreakerState.OPEN:
            self.state = BreakerState.OPEN
            self.opened_at = now
            self.total_trips += 1

    def snapshot(self) -> dict:
        return {
            'name': self.name,
            'state': self.state.value,
            'consecutive_failures': self.consecutive_failures,
            'total_calls': self.total_calls,
            'total_failures': self.total_failures,
            'total_trips': self.total_trips,
        }


class BreakerRegistry:
    """每个 provider 一个熔断器。"""

    def __init__(self, fail_threshold: int = 3, recovery_seconds: float = 30.0):
        self.fail_threshold = fail_threshold
        self.recovery_seconds = recovery_seconds
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name=name,
                fail_threshold=self.fail_threshold,
                recovery_seconds=self.recovery_seconds,
            )
        return self._breakers[name]

    def snapshots(self) -> list[dict]:
        return [b.snapshot() for b in self._breakers.values()]
