# -*- coding: utf-8 -*-
"""测试夹具：用假的 provider client 替换真实 HTTP 调用，让路由逻辑可离线测试。"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import BudgetConfig, GatewayConfig, ProviderConfig, RouteConfig  # noqa: E402
from app.providers import ProviderClient  # noqa: E402
from app.schemas import ChatMessage, ProviderError  # noqa: E402


class FakeClient:
    """行为由 script[provider] 决定：
       dict          -> 成功，返回该 dict
       Exception     -> 抛出（必须抛 ProviderError 才是「供应商失败」）
       list          -> 按调用次序依次取用，可模拟「先失败后成功」
    """

    def __init__(self, cfg: ProviderConfig, script: dict, calls: list):
        self.cfg = cfg
        self.script = script
        self.calls = calls

    async def chat_with_retry(self, model: str, messages: list[ChatMessage], **kw) -> dict:
        self.calls.append({'provider': self.cfg.name, 'model': model})
        behavior = self.script.get(self.cfg.name, {
            'content': f'ok from {self.cfg.name}',
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
        })
        if isinstance(behavior, list):
            behavior = behavior.pop(0) if behavior else ProviderError(
                self.cfg.name, '脚本耗尽')
        if isinstance(behavior, Exception):
            raise behavior
        if callable(behavior):
            behavior = behavior(model)
        out = dict(behavior)
        out.setdefault('_latency_ms', 1)
        return out


def make_config(**over) -> GatewayConfig:
    providers = {
        'alpha': ProviderConfig(name='alpha', base_url='http://a/v1',
                                api_key_env='', models=['m-a'], priority=10, max_retries=0),
        'beta': ProviderConfig(name='beta', base_url='http://b/v1',
                               api_key_env='', models=['m-b'], priority=20, max_retries=0),
        'gamma': ProviderConfig(name='gamma', base_url='http://c/v1',
                                api_key_env='', models=['m-g'], priority=30, max_retries=0),
    }
    routes = {
        'default': RouteConfig('default', [('alpha', 'm-a'), ('beta', 'm-b'),
                                           ('gamma', 'm-g')]),
        'solo': RouteConfig('solo', [('alpha', 'm-a')]),
    }
    cfg = GatewayConfig(providers=providers, routes=routes,
                        budget=BudgetConfig(daily_tokens=100000,
                                            per_provider_daily_tokens=0,
                                            price_per_1k={'alpha': 1.0}),
                        breaker_fail_threshold=over.pop('breaker_fail_threshold', 3),
                        breaker_recovery_seconds=over.pop('breaker_recovery_seconds', 30.0))
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def cfg():
    return make_config()


def build_gateway(cfg, script: dict | None = None):
    """返回 (gateway, calls 列表)。"""
    from app.router import Gateway
    from app.circuit import BreakerRegistry
    from app.quota import QuotaManager

    calls: list[dict] = []
    script = script or {}
    factory = lambda c: FakeClient(c, script, calls)  # noqa: E731
    gw = Gateway(
        cfg,
        client_factory=factory,
        quota=QuotaManager(daily_tokens=cfg.budget.daily_tokens,
                           per_provider_daily_tokens=cfg.budget.per_provider_daily_tokens,
                           price_per_1k=cfg.budget.price_per_1k),
        breakers=BreakerRegistry(cfg.breaker_fail_threshold, cfg.breaker_recovery_seconds),
    )
    return gw, calls


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)
