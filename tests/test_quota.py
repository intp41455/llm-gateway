# -*- coding: utf-8 -*-
"""额度与成本记账。使用固定的 now 保证跨日边界可测。"""
from __future__ import annotations

import time

import pytest

from app.quota import QuotaManager
from app.schemas import BudgetExceeded


def test_records_tokens_and_cost():
    q = QuotaManager(daily_tokens=1000, price_per_1k={'p': 2.0})
    rec = q.record('p', 'm', 'caller', 100, 50, now=time.time())
    assert rec.total_tokens == 150
    assert rec.cost == pytest.approx(0.3)          # 150/1000*2.0
    rep = q.report(now=time.time())
    assert rep['providers']['p']['tokens'] == 150
    assert rep['providers']['p']['by_model'] == {'m': 150}


def test_no_price_means_zero_cost():
    q = QuotaManager(price_per_1k={})
    rec = q.record('unknown', 'm', '-', 1000, 1000, now=time.time())
    assert rec.cost == 0.0


def test_check_passes_under_limit():
    q = QuotaManager(daily_tokens=1000)
    q.record('p', 'm', '-', 100, 0, now=time.time())
    q.check('p', now=time.time())                  # 不抛异常即可


def test_check_raises_when_global_exceeded():
    q = QuotaManager(daily_tokens=100)
    q.record('p', 'm', '-', 100, 0, now=time.time())
    with pytest.raises(BudgetExceeded) as ei:
        q.check('p', now=time.time())
    assert ei.value.scope == 'global'


def test_check_raises_when_per_provider_exceeded():
    q = QuotaManager(daily_tokens=0, per_provider_daily_tokens=50)
    q.record('p1', 'm', '-', 30, 0, now=time.time())
    q.record('p2', 'm', '-', 60, 0, now=time.time())
    q.check('p1', now=time.time())                 # p1 只用了 30，放行
    with pytest.raises(BudgetExceeded) as ei:
        q.check('p2', now=time.time())
    assert ei.value.scope == 'p2'
    assert ei.value.limit == 50
    assert ei.value.used == 60


def test_zero_limit_means_unlimited():
    q = QuotaManager(daily_tokens=0, per_provider_daily_tokens=0)
    q.record('p', 'm', '-', 10_000_000, 0, now=time.time())
    q.check('p', now=time.time())                  # 0 = 不限


def test_daily_rollover_resets_counter():
    q = QuotaManager(daily_tokens=100)
    base = time.time()
    q.record('p', 'm', '-', 100, 0, now=base)
    with pytest.raises(BudgetExceeded):
        q.check('p', now=base)
    q.check('p', now=base + 86400)                 # 次日额度重置


def test_report_separates_providers_and_callers():
    q = QuotaManager(price_per_1k={'p1': 1.0, 'p2': 1.0})
    now = time.time()
    q.record('p1', 'm1', 'agent-a', 100, 0, now=now)
    q.record('p1', 'm1', 'agent-b', 100, 0, now=now)
    q.record('p2', 'm2', 'agent-a', 200, 0, now=now)
    rep = q.report(now=now)
    assert rep['global']['tokens'] == 400
    assert rep['global']['calls'] == 3
    assert set(rep['providers']) == {'p1', 'p2'}
    assert rep['callers']['agent-a']['tokens'] == 300
    assert rep['callers']['agent-b']['tokens'] == 100


def test_failure_counter():
    q = QuotaManager()
    now = time.time()
    q.record_failure('p', now=now)
    q.record_failure('p', now=now)
    rep = q.report(now=now)
    assert rep['global']['failures'] == 2
    assert rep['providers']['p']['failures'] == 2


def test_usage_ratio_reported_when_limit_set():
    q = QuotaManager(daily_tokens=1000)
    now = time.time()
    q.record('p', 'm', '-', 250, 0, now=now)
    assert q.report(now=now)['global']['usage_ratio'] == 0.25
    assert QuotaManager(daily_tokens=0).report(now=now)['global']['usage_ratio'] is None
