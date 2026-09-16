# -*- coding: utf-8 -*-
"""供应商适配器。

所有供应商都走 OpenAI 兼容协议（/chat/completions），
所以只需要一个实现 + 一份 base_url/api_key 配置。这是选型上刻意的收敛。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from .config import ProviderConfig
from .schemas import ChatMessage, ProviderError

# 状态码 -> 是否值得重试
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524}


class ProviderClient:
    def __init__(self, cfg: ProviderConfig, transport: httpx.AsyncBaseTransport | None = None,
                 client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._transport = transport
        self._client = client

    def _headers(self) -> dict[str, str]:
        h = {'Content-Type': 'application/json'}
        if self.cfg.api_key:
            h['Authorization'] = f'Bearer {self.cfg.api_key}'
        return h

    async def chat(self, model: str, messages: list[ChatMessage],
                   temperature: float = 1.0, top_p: float = 1.0,
                   max_tokens: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'model': model,
            'messages': [m.model_dump(exclude_none=True) for m in messages],
            'temperature': temperature,
            'top_p': top_p,
            'stream': False,
        }
        if max_tokens:
            payload['max_tokens'] = max_tokens

        url = f'{self.cfg.base_url}/chat/completions'
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self.cfg.timeout, transport=self._transport)
        try:
            resp = await client.post(url, json=payload, headers=self._headers())
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise ProviderError(self.cfg.name, f'网络异常: {type(e).__name__}: {e}',
                                retryable=True) from e
        finally:
            if own_client:
                await client.aclose()

        if resp.status_code >= 400:
            snippet = ''
            try:
                snippet = json.dumps(resp.json(), ensure_ascii=False)[:300]
            except Exception:
                snippet = resp.text[:300]
            raise ProviderError(
                self.cfg.name,
                f'HTTP {resp.status_code}: {snippet}',
                status_code=resp.status_code,
                retryable=resp.status_code in _RETRYABLE_STATUS,
            )

        try:
            data = resp.json()
            content = data['choices'][0]['message']['content']
        except Exception as e:
            raise ProviderError(self.cfg.name, f'响应结构异常: {resp.text[:200]}',
                                retryable=True) from e

        return {
            'content': content,
            'usage': data.get('usage') or {},
            'finish_reason': data['choices'][0].get('finish_reason', 'stop'),
            'id': data.get('id', ''),
            'raw': data,
        }

    async def chat_with_retry(self, **kw) -> dict[str, Any]:
        """同 provider 内重试（指数退避）。跨 provider 的降级在 router 里做。"""
        last: ProviderError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            t0 = time.monotonic()
            try:
                out = await self.chat(**kw)
                out['_latency_ms'] = int((time.monotonic() - t0) * 1000)
                return out
            except ProviderError as e:
                last = e
                if not e.retryable or attempt >= self.cfg.max_retries:
                    break
                await asyncio.sleep(min(0.2 * (2 ** attempt), 2.0))
        assert last is not None
        raise last


def make_client(cfg: ProviderConfig, **kw) -> ProviderClient:
    return ProviderClient(cfg, **kw)
