# -*- coding: utf-8 -*-
"""路由与降级：网关最核心的行为。"""
from __future__ import annotations

import pytest

from app.schemas import AllProvidersFailed, BudgetExceeded, ChatCompletionRequest, ProviderError
from conftest import FakeClient, build_gateway, make_config, run  # noqa: F401


def req(**kw):
    base = {'model': 'default', 'messages': [{'role': 'user', 'content': 'hi'}]}
    base.update(kw)
    return ChatCompletionRequest(**base)


# ---------------------------------------------------------------- 正常路径
def test_first_candidate_success_not_degraded(cfg):
    gw, calls = build_gateway(cfg)
    resp = run(gw.complete(req()))
    assert resp.gateway.provider == 'alpha'
    assert resp.gateway.degraded is False
    assert [c['provider'] for c in calls] == ['alpha']
    assert resp.choices[0].message.content == 'ok from alpha'


def test_unknown_alias_falls_back_to_default_route(cfg):
    gw, _ = build_gateway(cfg)
    resp = run(gw.complete(req(model='完全不存在的别名')))
    assert resp.gateway.provider == 'alpha'
    assert resp.gateway.route == '完全不存在的别名'


# ---------------------------------------------------------------- 降级路径
def test_failover_to_second_provider(cfg):
    gw, calls = build_gateway(cfg, {
        'alpha': ProviderError('alpha', 'HTTP 500', status_code=500),
    })
    resp = run(gw.complete(req()))
    assert resp.gateway.provider == 'beta'
    assert resp.gateway.degraded is True
    assert [c['provider'] for c in calls] == ['alpha', 'beta']
    assert [a.ok for a in resp.gateway.attempts] == [False, True]
    assert 'HTTP 500' in resp.gateway.attempts[0].error


def test_failover_cascades_to_third(cfg):
    gw, calls = build_gateway(cfg, {
        'alpha': ProviderError('alpha', 'boom'),
        'beta': ProviderError('beta', 'boom'),
    })
    resp = run(gw.complete(req()))
    assert resp.gateway.provider == 'gamma'
    assert [c['provider'] for c in calls] == ['alpha', 'beta', 'gamma']
    assert len(resp.gateway.attempts) == 3


def test_all_failed_raises_with_attempts(cfg):
    gw, _ = build_gateway(cfg, {p: ProviderError(p, 'down') for p in ('alpha', 'beta', 'gamma')})
    with pytest.raises(AllProvidersFailed) as ei:
        run(gw.complete(req()))
    assert len(ei.value.attempts) == 3
    assert all(not a.ok for a in ei.value.attempts)


# ---------------------------------------------------------------- 降级开关
def test_fallback_disabled_stops_at_first_failure(cfg):
    gw, calls = build_gateway(cfg, {'alpha': ProviderError('alpha', 'down')})
    with pytest.raises(AllProvidersFailed):
        run(gw.complete(req(fallback=False)))
    assert [c['provider'] for c in calls] == ['alpha']


def test_fallback_disabled_success_still_works(cfg):
    gw, calls = build_gateway(cfg)
    resp = run(gw.complete(req(fallback=False)))
    assert resp.gateway.provider == 'alpha'


def test_max_attempts_limits_candidates(cfg):
    gw, calls = build_gateway(cfg, {p: ProviderError(p, 'down') for p in ('alpha', 'beta', 'gamma')})
    with pytest.raises(AllProvidersFailed):
        run(gw.complete(req(max_attempts=2)))
    assert [c['provider'] for c in calls] == ['alpha', 'beta']


# ---------------------------------------------------------------- 熔断联动
def test_breaker_opens_after_threshold_then_skips(cfg):
    gw, calls = build_gateway(cfg, {p: ProviderError(p, 'down')
                                    for p in ('alpha', 'beta', 'gamma')})
    # 阈值 3：前 3 次都会尝试 alpha，第 4 次开始跳过
    for _ in range(3):
        with pytest.raises(AllProvidersFailed):
            run(gw.complete(req()))
    assert [c['provider'] for c in calls].count('alpha') == 3

    calls.clear()
    with pytest.raises(AllProvidersFailed):
        run(gw.complete(req()))
    assert 'alpha' not in [c['provider'] for c in calls]
    assert gw.breakers.get('alpha').state.value == 'open'


def test_breaker_recovers_after_success(cfg):
    gw, _ = build_gateway(cfg, {'alpha': ProviderError('alpha', 'down')})
    with pytest.raises(AllProvidersFailed):
        run(gw.complete(req(max_attempts=1, fallback=False)))
    assert gw.breakers.get('alpha').consecutive_failures == 1

    # 换成成功脚本后再打一次，连续失败应清零。
    # 注意：gateway 会缓存 client，所以必须同时清掉缓存才会生效。
    gw.client_factory = lambda c: FakeClient(c, {}, [])
    gw._clients.clear()
    run(gw.complete(req(model='solo')))
    assert gw.breakers.get('alpha').consecutive_failures == 0
    assert gw.breakers.get('alpha').state.value == 'closed'


# ---------------------------------------------------------------- 额度
def test_budget_exceeded_raises_429_semantics(cfg):
    gw, calls = build_gateway(cfg)
    run(gw.complete(req()))
    # 手工把已用 token 顶到上限
    gw.quota.record('alpha', 'm-a', '-', 200000, 0)
    with pytest.raises(BudgetExceeded):
        run(gw.complete(req()))
    assert len(calls) == 1     # 第二次没有真的发出去


# ---------------------------------------------------------------- 记账
def test_usage_recorded_and_priced(cfg):
    gw, _ = build_gateway(cfg)
    resp = run(gw.complete(req(), caller='agent-x'))
    assert resp.usage.prompt_tokens == 10
    assert resp.usage.completion_tokens == 5
    assert resp.usage.total_tokens == 15
    rep = gw.quota.report()
    assert rep['global']['tokens'] == 15
    assert rep['providers']['alpha']['tokens'] == 15
    # price_per_1k alpha = 1.0 -> 15/1000*1.0
    assert rep['providers']['alpha']['cost'] == pytest.approx(0.015, rel=1e-6)
    assert 'agent-x' in rep['callers']


def test_failures_counted_in_report(cfg):
    gw, _ = build_gateway(cfg, {'alpha': ProviderError('alpha', 'down')})
    run(gw.complete(req()))
    rep = gw.quota.report()
    assert rep['global']['failures'] == 1
    assert rep['providers']['alpha']['failures'] == 1


# ---------------------------------------------------------------- 元信息
def test_response_shape_matches_openai(cfg):
    gw, _ = build_gateway(cfg)
    resp = run(gw.complete(req()))
    d = resp.model_dump()
    assert d['object'] == 'chat.completion'
    assert set(d) >= {'id', 'created', 'model', 'choices', 'usage'}
    assert d['choices'][0]['message']['role'] == 'assistant'


def test_health_and_models(cfg):
    gw, _ = build_gateway(cfg)
    h = gw.health()
    assert h['status'] == 'ok'
    assert 'alpha' in h['providers_configured']
    assert h['routes']['default'][0] == {'provider': 'alpha', 'model': 'm-a'}
    assert {m['id'] for m in gw.list_models()} >= {'default', 'solo'}
