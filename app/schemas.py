# -*- coding: utf-8 -*-
"""对外契约：完全兼容 OpenAI /chat/completions，额外挂网关自己的诊断字段。

兼容性很重要——客户端（各类 Agent / IDE 插件）只要把 base_url 指过来就能用，
不用改一行代码。这是网关能被真实接入的前提。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal['system', 'user', 'assistant', 'tool']
    content: str | None = None
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str = 'default'
    messages: list[ChatMessage]
    temperature: float = 1.0
    top_p: float = 1.0
    max_tokens: int | None = None
    stream: bool = False
    # ---- 网关扩展（非 OpenAI 字段，客户端可忽略） ----
    fallback: bool = Field(default=True, description='失败时是否尝试链路中的下一个供应商')
    max_attempts: int = Field(default=3, description='最多尝试几个候选')


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Choice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = 'stop'


class AttemptRecord(BaseModel):
    provider: str
    model: str
    ok: bool
    latency_ms: int = 0
    error: str | None = None


class GatewayMeta(BaseModel):
    """网关诊断信息：出问题时能一眼看出走的是哪条链路、为什么降级。"""
    route: str = ''
    provider: str = ''
    model: str = ''
    attempts: list[AttemptRecord] = Field(default_factory=list)
    degraded: bool = False


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = 'chat.completion'
    created: int
    model: str
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)
    gateway: GatewayMeta | None = None


class ProviderError(Exception):
    """供应商调用异常。retryable 决定是否值得在同 provider 内重试。"""

    def __init__(self, provider: str, message: str,
                 status_code: int | None = None, retryable: bool = True):
        super().__init__(f'[{provider}] {message}')
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable


class AllProvidersFailed(Exception):
    def __init__(self, attempts: list[AttemptRecord]):
        self.attempts = attempts
        detail = '; '.join(f'{a.provider}:{a.model} -> {a.error}' for a in attempts)
        super().__init__(f'全部候选供应商均失败（{len(attempts)} 次尝试）: {detail}')


class BudgetExceeded(Exception):
    def __init__(self, scope: str, used: int, limit: int):
        super().__init__(f'额度超限 [{scope}] 已用 {used} / 上限 {limit}')
        self.scope, self.used, self.limit = scope, used, limit
