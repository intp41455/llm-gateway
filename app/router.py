# -*- coding: utf-8 -*-
"""路由内核：模型别名 -> 候选链 -> 熔断跳过 -> 逐级降级。

一次请求的完整路径：
  1. 别名解析：客户端说 "default"，网关翻译成 [(deepseek, deepseek-chat), (dashscope, qwen-plus)]
  2. 准入判定：熔断器 OPEN 的候选直接跳过；额度超限直接拒绝
  3. 逐级尝试：每个候选内部带指数退避重试，跨候选做降级
  4. 结果记账：token / 成本 / 延迟 / 失败原因全量落账，返回 gateway 诊断字段
"""
from __future__ import annotations

import time
import uuid
from typing import Callable

from .circuit import BreakerRegistry
from .config import GatewayConfig
from .providers import ProviderClient, make_client
from .quota import QuotaManager
from .schemas import (AllProvidersFailed, AttemptRecord, ChatCompletionRequest,
                      ChatCompletionResponse, Choice, ChatMessage, GatewayMeta,
                      ProviderError, Usage)

ClientFactory = Callable[..., ProviderClient]


class Gateway:
    def __init__(self, config: GatewayConfig,
                 client_factory: ClientFactory = make_client,
                 quota: QuotaManager | None = None,
                 breakers: BreakerRegistry | None = None):
        self.config = config
        self.client_factory = client_factory
        self.quota = quota or QuotaManager(
            daily_tokens=config.budget.daily_tokens,
            per_provider_daily_tokens=config.budget.per_provider_daily_tokens,
            price_per_1k=config.budget.price_per_1k,
        )
        self.breakers = breakers or BreakerRegistry(
            fail_threshold=config.breaker_fail_threshold,
            recovery_seconds=config.breaker_recovery_seconds,
        )
        self._clients: dict[str, ProviderClient] = {}

    def client(self, provider: str) -> ProviderClient:
        if provider not in self._clients:
            self._clients[provider] = self.client_factory(self.config.provider(provider))
        return self._clients[provider]

    # ------------------------------------------------------------------
    def resolve(self, alias: str) -> list[tuple[str, str]]:
        route = self.config.routes.get(alias) or self.config.routes.get('default')
        if route is None:
            return []
        return [(p, m) for p, m in route.chain if p in self.config.providers]

    async def complete(self, req: ChatCompletionRequest,
                       caller: str = '-') -> ChatCompletionResponse:
        candidates = self.resolve(req.model)[: max(1, req.max_attempts)]
        attempts: list[AttemptRecord] = []

        if not candidates:
            raise AllProvidersFailed([AttemptRecord(
                provider='-', model=req.model, ok=False,
                error=f'没有匹配的路由别名 {req.model!r}')])

        for idx, (prov_name, model) in enumerate(candidates):
            # 关闭降级：只认链路上第一个（但熔断/额度仍生效）
            if idx > 0 and not req.fallback:
                break

            breaker = self.breakers.get(prov_name)
            if not breaker.allow():
                attempts.append(AttemptRecord(
                    provider=prov_name, model=model, ok=False,
                    error=f'熔断中（{breaker.state.value}），跳过'))
                continue

            # 额度超限属于「配置/运营问题」，不降级掩盖，直接抛
            self.quota.check(prov_name, caller)

            t0 = time.monotonic()
            try:
                out = await self.client(prov_name).chat_with_retry(
                    model=model, messages=req.messages, temperature=req.temperature,
                    top_p=req.top_p, max_tokens=req.max_tokens)
            except ProviderError as e:
                breaker.record_failure()
                self.quota.record_failure(prov_name)
                attempts.append(AttemptRecord(
                    provider=prov_name, model=model, ok=False,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    error=str(e)[:300]))
                continue

            breaker.record_success()
            usage_raw = out.get('usage') or {}
            pt = int(usage_raw.get('prompt_tokens') or 0)
            ct = int(usage_raw.get('completion_tokens') or 0)
            self.quota.record(prov_name, model, caller, pt, ct)
            attempts.append(AttemptRecord(
                provider=prov_name, model=model, ok=True,
                latency_ms=out.get('_latency_ms', int((time.monotonic() - t0) * 1000))))

            return ChatCompletionResponse(
                id=out.get('id') or f'chatcmpl-{uuid.uuid4().hex[:24]}',
                created=int(time.time()),
                model=model,
                choices=[Choice(
                    index=0,
                    message=ChatMessage(role='assistant', content=out['content']),
                    finish_reason=out.get('finish_reason', 'stop'))],
                usage=Usage(prompt_tokens=pt, completion_tokens=ct,
                            total_tokens=pt + ct),
                gateway=GatewayMeta(
                    route=req.model, provider=prov_name, model=model,
                    attempts=attempts, degraded=idx > 0),
            )

        raise AllProvidersFailed(attempts)

    # ------------------------------------------------------------------
    def list_models(self) -> list[dict]:
        now = int(time.time())
        out = []
        for alias in self.config.routes:
            out.append({'id': alias, 'object': 'model', 'created': now,
                        'owned_by': 'gateway'})
        return out

    def health(self) -> dict:
        configured = [p.name for p in self.config.available_providers()]
        return {
            'status': 'ok',
            'providers_configured': configured,
            'providers_total': len(self.config.providers),
            'routes': {a: [{'provider': p, 'model': m} for p, m in r.chain]
                       for a, r in self.config.routes.items()},
            'breakers': self.breakers.snapshots(),
        }
