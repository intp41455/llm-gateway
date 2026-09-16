# -*- coding: utf-8 -*-
"""额度与成本记账。

要解决的真实问题：多个 Agent 共用几个供应商 key 时，
「今天到底花了多少钱、哪个客户端在烧钱」是算不出来的。
所以按 provider / 调用方 / 自然日 三个维度落账。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

DAY = 86400


def _today(now: float | None = None) -> str:
    return time.strftime('%Y-%m-%d', time.localtime(now if now is not None else time.time()))


@dataclass
class UsageRecord:
    provider: str
    model: str
    caller: str
    prompt_tokens: int
    completion_tokens: int
    cost: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class _Bucket:
    tokens: int = 0
    cost: float = 0.0
    calls: int = 0
    failures: int = 0
    by_model: dict[str, int] = field(default_factory=dict)


class QuotaManager:
    """线程安全（网关是 async，但记账路径足够短，用锁最省心）。"""

    def __init__(self, daily_tokens: int = 0, per_provider_daily_tokens: int = 0,
                 price_per_1k: dict[str, float] | None = None):
        self.daily_tokens = daily_tokens
        self.per_provider_daily_tokens = per_provider_daily_tokens
        self.price_per_1k = price_per_1k or {}
        self._lock = threading.Lock()
        self._global: dict[str, _Bucket] = {}          # day -> bucket
        self._provider: dict[tuple[str, str], _Bucket] = {}
        self._caller: dict[tuple[str, str], _Bucket] = {}
        self._history: list[UsageRecord] = []

    # ---------- 预算校验（调用前） ----------
    def check(self, provider: str, caller: str = '-', now: float | None = None) -> None:
        from .schemas import BudgetExceeded
        day = _today(now)
        with self._lock:
            g = self._global.get(day, _Bucket())
            p = self._provider.get((day, provider), _Bucket())
        if self.daily_tokens and g.tokens >= self.daily_tokens:
            raise BudgetExceeded('global', g.tokens, self.daily_tokens)
        if self.per_provider_daily_tokens and p.tokens >= self.per_provider_daily_tokens:
            raise BudgetExceeded(provider, p.tokens, self.per_provider_daily_tokens)

    # ---------- 记账（调用后） ----------
    def record(self, provider: str, model: str, caller: str,
               prompt_tokens: int, completion_tokens: int,
               now: float | None = None) -> UsageRecord:
        price = self.price_per_1k.get(provider, 0.0)
        cost = (prompt_tokens + completion_tokens) / 1000.0 * price
        rec = UsageRecord(provider, model, caller, prompt_tokens,
                          completion_tokens, round(cost, 6))
        day = _today(now)
        with self._lock:
            for bucket in (self._global.setdefault(day, _Bucket()),
                           self._provider.setdefault((day, provider), _Bucket()),
                           self._caller.setdefault((day, caller), _Bucket())):
                bucket.tokens += rec.total_tokens
                bucket.cost += rec.cost
                bucket.calls += 1
                bucket.by_model[model] = bucket.by_model.get(model, 0) + rec.total_tokens
            self._history.append(rec)
            if len(self._history) > 20000:
                self._history = self._history[-10000:]
        return rec

    def record_failure(self, provider: str, now: float | None = None) -> None:
        day = _today(now)
        with self._lock:
            self._global.setdefault(day, _Bucket()).failures += 1
            self._provider.setdefault((day, provider), _Bucket()).failures += 1

    # ---------- 报表 ----------
    def report(self, now: float | None = None) -> dict:
        day = _today(now)
        with self._lock:
            g = self._global.get(day, _Bucket())
            providers = {
                k[1]: {'tokens': v.tokens, 'cost': round(v.cost, 4),
                       'calls': v.calls, 'failures': v.failures,
                       'by_model': dict(v.by_model)}
                for k, v in self._provider.items() if k[0] == day
            }
            callers = {
                k[1]: {'tokens': v.tokens, 'cost': round(v.cost, 4), 'calls': v.calls}
                for k, v in self._caller.items() if k[0] == day
            }
        return {
            'day': day,
            'global': {
                'tokens': g.tokens, 'cost': round(g.cost, 4),
                'calls': g.calls, 'failures': g.failures,
                'daily_limit': self.daily_tokens,
                'usage_ratio': round(g.tokens / self.daily_tokens, 4) if self.daily_tokens else None,
            },
            'providers': providers,
            'callers': callers,
        }
