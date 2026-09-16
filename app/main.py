# -*- coding: utf-8 -*-
"""多模型 LLM 网关 · HTTP 入口（FastAPI）

对外接口刻意做成 OpenAI 兼容，客户端只需改 base_url：
  POST /v1/chat/completions    对话补全（核心）
  GET  /v1/models              可用模型别名列表
  GET  /health                 存活 + 各 provider 熔断状态
  GET  /admin/usage            额度/成本报表
  GET  /admin/mcp              MCP 注册表审计（冲突 / 客户端覆盖）
  GET  /admin/context          记忆分层装配预览
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import GatewayConfig, load_config
from .mcp_registry import MCPRegistry, MCPServer
from .memory_layers import MemoryLayers
from .router import Gateway
from .schemas import (AllProvidersFailed, BudgetExceeded, ChatCompletionRequest,
                      ChatCompletionResponse)


# ---------------------------------------------------------------- 结构化日志
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'ts': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(record.created)),
            'level': record.levelname,
            'event': record.getMessage(),
        }
        for k in ('caller', 'route', 'provider', 'model', 'latency_ms', 'ok', 'degraded'):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        if record.exc_info:
            payload['exc'] = self.formatException(record.exc_info)[:500]
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = 'INFO') -> logging.Logger:
    logger = logging.getLogger('gateway')
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        logger.addHandler(h)
        logger.setLevel(level)
        logger.propagate = False
    return logger


logger = setup_logging()


# ---------------------------------------------------------------- MCP 预置
def default_mcp_registry() -> MCPRegistry:
    reg = MCPRegistry()
    reg.register(MCPServer(
        name='filesystem', transport='stdio', command='npx',
        args=['-y', '@modelcontextprotocol/server-filesystem', '.'],
        tools=['read_file', 'write_file', 'list_directory'],
        clients=['claude', 'workbuddy', 'codex'], note='本地文件读写'))
    reg.register(MCPServer(
        name='fetch', transport='stdio', command='uvx', args=['mcp-server-fetch'],
        tools=['fetch'], clients=['claude', 'workbuddy'], note='网页抓取'))
    reg.register(MCPServer(
        name='sqlite', transport='stdio', command='uvx', args=['mcp-server-sqlite'],
        tools=['read_query'], clients=['codex'], note='只读 SQL 查询'))
    return reg


# ---------------------------------------------------------------- 应用工厂
def create_app(config: GatewayConfig | None = None,
               gateway: Gateway | None = None,
               mcp: MCPRegistry | None = None,
               memory_root: str | None = None) -> FastAPI:
    cfg = config or load_config()
    gw = gateway or Gateway(cfg)
    mcp_reg = mcp if mcp is not None else default_mcp_registry()
    mem = MemoryLayers(memory_root or os.environ.get(
        'GATEWAY_MEMORY_ROOT', os.path.expanduser('~/.shared-agents')))

    app = FastAPI(title='Multi-Model LLM Gateway',
                  version='1.0.0',
                  description='多模型路由 · 失败降级 · 额度记账 · MCP 工具链治理')

    app.state.gateway = gw
    app.state.mcp = mcp_reg
    app.state.memory = mem

    @app.get('/health')
    def health():
        return gw.health()

    @app.get('/v1/models')
    def models():
        return {'object': 'list', 'data': gw.list_models()}

    @app.post('/v1/chat/completions', response_model=ChatCompletionResponse)
    async def chat_completions(req: ChatCompletionRequest,
                               x_caller: str | None = Header(default=None),
                               request: Request = None):
        caller = x_caller or (request.client.host if request and request.client else '-')
        t0 = time.monotonic()
        try:
            resp = await gw.complete(req, caller=caller)
        except BudgetExceeded as e:
            logger.warning('budget_exceeded', extra={'caller': caller, 'route': req.model})
            raise HTTPException(status_code=429, detail=str(e))
        except AllProvidersFailed as e:
            logger.error('all_providers_failed',
                         extra={'caller': caller, 'route': req.model, 'ok': False})
            raise HTTPException(status_code=502, detail={
                'message': str(e),
                'attempts': [a.model_dump() for a in e.attempts],
            })
        meta = resp.gateway
        logger.info('chat_completion',
                    extra={'caller': caller, 'route': req.model,
                           'provider': meta.provider if meta else '',
                           'model': meta.model if meta else '',
                           'latency_ms': int((time.monotonic() - t0) * 1000),
                           'ok': True,
                           'degraded': meta.degraded if meta else False})
        return resp

    @app.get('/admin/usage')
    def usage():
        return gw.quota.report()

    @app.get('/admin/mcp')
    def mcp_audit():
        return {'audit': mcp_reg.audit(), 'servers': mcp_reg.to_dicts()}

    @app.post('/admin/mcp/{name}/toggle')
    def mcp_toggle(name: str, enabled: bool = True):
        if not mcp_reg.set_enabled(name, enabled):
            raise HTTPException(status_code=404, detail=f'未注册的 server: {name}')
        return {'name': name, 'enabled': enabled}

    @app.get('/admin/mcp/export/{client}')
    def mcp_export(client: str):
        return mcp_reg.render(client)

    @app.get('/admin/context')
    def context(budget: int = 12000):
        ctx = mem.assemble(budget_chars=budget)
        return {'describe': mem.describe(), 'blocks': ctx.blocks,
                'total_chars': ctx.total_chars, 'source_files': ctx.source_files}

    return app


app = create_app()


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1',
                port=int(os.environ.get('GATEWAY_PORT', 8200)))
