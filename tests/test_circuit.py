# -*- coding: utf-8 -*-
"""熔断器三态机。用注入的 now 参数控制时间，不依赖 sleep。"""
from __future__ import annotations

from app.circuit import BreakerRegistry, BreakerState, CircuitBreaker


def test_starts_closed_and_allows():
    b = CircuitBreaker('p', fail_threshold=3, recovery_seconds=30)
    assert b.state is BreakerState.CLOSED
    assert b.allow(now=0.0) is True


def test_opens_after_threshold_failures():
    b = CircuitBreaker('p', fail_threshold=3, recovery_seconds=30)
    b.record_failure(now=0.0)
    b.record_failure(now=0.1)
    assert b.state is BreakerState.CLOSED
    b.record_failure(now=0.2)
    assert b.state is BreakerState.OPEN
    assert b.total_trips == 1
    assert b.allow(now=0.3) is False


def test_success_resets_failure_streak():
    b = CircuitBreaker('p', fail_threshold=3, recovery_seconds=30)
    b.record_failure(now=0.0)
    b.record_failure(now=0.1)
    b.record_success()
    b.record_failure(now=0.2)
    assert b.state is BreakerState.CLOSED
    assert b.consecutive_failures == 1


def test_half_open_after_recovery_window():
    b = CircuitBreaker('p', fail_threshold=2, recovery_seconds=30)
    b.record_failure(now=0.0)
    b.record_failure(now=1.0)
    assert b.state is BreakerState.OPEN
    assert b.allow(now=10.0) is False        # 还没到恢复期
    assert b.allow(now=31.5) is True         # 到期 -> 半开，放一个
    assert b.state is BreakerState.HALF_OPEN
    assert b.allow(now=31.6) is False        # 半开只放一个


def test_half_open_success_closes():
    b = CircuitBreaker('p', fail_threshold=2, recovery_seconds=30)
    b.record_failure(now=0.0)
    b.record_failure(now=1.0)
    b.allow(now=40.0)                        # 进入半开并占用试探名额
    b.record_success()
    assert b.state is BreakerState.CLOSED
    assert b.total_trips == 1


def test_half_open_failure_reopens_immediately():
    b = CircuitBreaker('p', fail_threshold=5, recovery_seconds=30)
    for i in range(5):
        b.record_failure(now=float(i))
    assert b.state is BreakerState.OPEN
    b.allow(now=100.0)                       # 半开
    assert b.state is BreakerState.HALF_OPEN
    b.record_failure(now=101.0)              # 试探失败
    assert b.state is BreakerState.OPEN
    assert b.total_trips == 2
    assert b.allow(now=102.0) is False


def test_snapshot_fields():
    b = CircuitBreaker('p', fail_threshold=1, recovery_seconds=30)
    b.record_failure(now=0.0)
    s = b.snapshot()
    assert s == {'name': 'p', 'state': 'open', 'consecutive_failures': 1,
                 'total_calls': 1, 'total_failures': 1, 'total_trips': 1}


def test_registry_isolates_providers():
    reg = BreakerRegistry(fail_threshold=1, recovery_seconds=30)
    a, b = reg.get('a'), reg.get('b')
    a.record_failure(now=0.0)
    assert a.state is BreakerState.OPEN
    assert b.state is BreakerState.CLOSED
    assert reg.get('a') is a                 # 同一个实例
    assert len(reg.snapshots()) == 2
