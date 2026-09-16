# -*- coding: utf-8 -*-
"""网关配置：provider 注册表 + 模型别名路由表 + 额度策略。

设计原则：**配置外置**。上线换模型/换供应商只改 YAML，不动代码。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'providers.yaml')

# 常见供应商的内置默认（api_key 一律从环境变量读，不进配置文件）
BUILTIN_PROVIDERS: dict[str, dict[str, Any]] = {
    'deepseek': {
        'base_url': 'https://api.deepseek.com/v1',
        'api_key_env': 'DEEPSEEK_API_KEY',
        'models': ['deepseek-chat', 'deepseek-reasoner'],
    },
    'dashscope': {
        'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'api_key_env': 'DASHSCOPE_API_KEY',
        'models': ['qwen-plus', 'qwen-turbo', 'qwen-max'],
    },
    'openai': {
        'base_url': 'https://api.openai.com/v1',
        'api_key_env': 'OPENAI_API_KEY',
        'models': ['gpt-4o-mini', 'gpt-4o'],
    },
    'senseaudio': {
        'base_url': 'https://api.senseaudio.cn/v1',
        'api_key_env': 'SENSEAUDIO_API_KEY',
        'models': ['senseaudio-s2'],
    },
}


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key_env: str = ''
    models: list[str] = field(default_factory=list)
    timeout: float = 60.0
    max_retries: int = 2
    priority: int = 100          # 数字越小越优先
    enabled: bool = True

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, '') if self.api_key_env else ''

    @property
    def configured(self) -> bool:
        """没配 key 的 provider 视为不可用（不报错，直接跳过）。"""
        return bool(self.api_key) or not self.api_key_env


@dataclass
class RouteConfig:
    """一个对外模型别名 -> 有序的候选链 [[provider, model], ...]。"""
    alias: str
    chain: list[tuple[str, str]]


@dataclass
class BudgetConfig:
    daily_tokens: int = 2_000_000        # 全局日 token 上限（0 = 不限）
    per_provider_daily_tokens: int = 0
    price_per_1k: dict[str, float] = field(default_factory=dict)   # 供应商 -> 元/1K token


@dataclass
class GatewayConfig:
    providers: dict[str, ProviderConfig]
    routes: dict[str, RouteConfig]
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    breaker_fail_threshold: int = 3       # 连续失败几次熔断
    breaker_recovery_seconds: float = 30.0

    def provider(self, name: str) -> ProviderConfig:
        return self.providers[name]

    def available_providers(self) -> list[ProviderConfig]:
        return sorted(
            (p for p in self.providers.values() if p.enabled and p.configured),
            key=lambda p: p.priority)


def _load_yaml(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def load_config(path: str | None = None,
                overrides: dict[str, Any] | None = None) -> GatewayConfig:
    path = path or os.environ.get('GATEWAY_CONFIG', DEFAULT_CONFIG_PATH)
    raw = _load_yaml(path)
    if overrides:
        raw = {**raw, **overrides}

    # ---- providers ----
    prov_raw: dict[str, Any] = dict(BUILTIN_PROVIDERS)
    for name, cfg in (raw.get('providers') or {}).items():
        prov_raw[name] = {**(prov_raw.get(name) or {}), **(cfg or {})}
    providers: dict[str, ProviderConfig] = {}
    for name, cfg in prov_raw.items():
        # YAML 1.1 会把不带引号的 on/off/yes/no 解析成布尔值，
        # 导致 provider 名变成 True/False。这里统一强制成字符串。
        key = str(name)
        providers[key] = ProviderConfig(
            name=key,
            base_url=str(cfg['base_url']).rstrip('/'),
            api_key_env=cfg.get('api_key_env', ''),
            models=list(cfg.get('models') or []),
            timeout=float(cfg.get('timeout', 60.0)),
            max_retries=int(cfg.get('max_retries', 2)),
            priority=int(cfg.get('priority', 100)),
            enabled=bool(cfg.get('enabled', True)),
        )

    # ---- routes ----
    routes: dict[str, RouteConfig] = {}
    for alias, chain in (raw.get('routes') or {}).items():
        parsed: list[tuple[str, str]] = []
        for item in chain:
            if isinstance(item, str):
                # "deepseek:deepseek-chat"
                prov, _, model = item.partition(':')
                parsed.append((prov, model or prov))
            else:
                parsed.append((item['provider'], item['model']))
        routes[alias] = RouteConfig(alias=alias, chain=parsed)

    if not routes:
        # 默认路由：每个 provider 的第一个模型组成链（按 priority 排序）
        chain = [(p.name, p.models[0]) for p in sorted(
            providers.values(), key=lambda x: x.priority) if p.models]
        routes['default'] = RouteConfig(alias='default', chain=chain)
        for p in providers.values():
            for m in p.models:
                routes.setdefault(m, RouteConfig(alias=m, chain=[(p.name, m)]))

    bud = raw.get('budget') or {}
    budget = BudgetConfig(
        daily_tokens=int(bud.get('daily_tokens', 2_000_000)),
        per_provider_daily_tokens=int(bud.get('per_provider_daily_tokens', 0)),
        price_per_1k={k: float(v) for k, v in (bud.get('price_per_1k') or {}).items()},
    )

    return GatewayConfig(
        providers=providers,
        routes=routes,
        budget=budget,
        breaker_fail_threshold=int(raw.get('breaker_fail_threshold', 3)),
        breaker_recovery_seconds=float(raw.get('breaker_recovery_seconds', 30.0)),
    )
