# -*- coding: utf-8 -*-
"""HTTP 层：接口契约、错误码映射、管理端点。用假 gateway 保证不发出真实请求。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.mcp_registry import MCPRegistry, MCPServer
from app.memory_layers import MemoryLayers
from app.main import create_app
from app.schemas import AllProvidersFailed, AttemptRecord, BudgetExceeded
from conftest import build_gateway, make_config


def _client(script=None, memory_root=None):
    cfg = make_config()
    gw, calls = build_gateway(cfg, script or {})
    mcp = MCPRegistry()
    mcp.register(MCPServer(name='fs', command='npx', tools=['read_file'],
                           clients=['claude', 'workbuddy']))
    mcp.register(MCPServer(name='solo', command='uvx', tools=['read_file'],
                           clients=['workbuddy']))
    app = create_app(config=cfg, gateway=gw, mcp=mcp, memory_root=memory_root)
    return TestClient(app), gw, calls


def test_health_exposes_breakers():
    c, _, _ = _client()
    r = c.get('/health')
    assert r.status_code == 200
    body = r.json()
    assert body['status'] == 'ok'
    assert 'alpha' in body['providers_configured']


def test_models_endpoint():
    c, _, _ = _client()
    data = c.get('/v1/models').json()
    assert data['object'] == 'list'
    assert {m['id'] for m in data['data']} >= {'default', 'solo'}


def test_chat_completions_happy_path():
    c, _, _ = _client()
    r = c.post('/v1/chat/completions', json={
        'model': 'default', 'messages': [{'role': 'user', 'content': 'hello'}]})
    assert r.status_code == 200
    body = r.json()
    assert body['choices'][0]['message']['content'] == 'ok from alpha'
    assert body['gateway']['provider'] == 'alpha'
    assert body['gateway']['degraded'] is False
    assert body['usage']['total_tokens'] == 15


def test_caller_header_flows_into_accounting():
    c, gw, _ = _client()
    c.post('/v1/chat/completions',
           headers={'X-Caller': 'agent-sales'},
           json={'model': 'default', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert 'agent-sales' in gw.quota.report()['callers']


def test_degraded_response_marks_gateway_meta():
    from app.schemas import ProviderError
    c, _, _ = _client({'alpha': ProviderError('alpha', 'HTTP 503', status_code=503)})
    r = c.post('/v1/chat/completions', json={
        'model': 'default', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert r.status_code == 200
    body = r.json()
    assert body['gateway']['provider'] == 'beta'
    assert body['gateway']['degraded'] is True
    assert body['gateway']['attempts'][0]['ok'] is False


def test_all_providers_failed_maps_to_502():
    from app.schemas import ProviderError
    c, _, _ = _client({p: ProviderError(p, 'down') for p in ('alpha', 'beta', 'gamma')})
    r = c.post('/v1/chat/completions', json={
        'model': 'default', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert r.status_code == 502
    detail = r.json()['detail']
    assert 'attempts' in detail and len(detail['attempts']) == 3


def test_budget_exceeded_maps_to_429():
    c, gw, _ = _client()
    gw.quota.record('alpha', 'm-a', '-', 10_000_000, 0)
    r = c.post('/v1/chat/completions', json={
        'model': 'default', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert r.status_code == 429


def test_validation_error_on_missing_messages():
    c, _, _ = _client()
    r = c.post('/v1/chat/completions', json={'model': 'default'})
    assert r.status_code == 422


def test_usage_endpoint():
    c, _, _ = _client()
    c.post('/v1/chat/completions', json={
        'model': 'default', 'messages': [{'role': 'user', 'content': 'hi'}]})
    rep = c.get('/admin/usage').json()
    assert rep['global']['tokens'] == 15


def test_mcp_audit_reports_conflicts():
    c, _, _ = _client()
    body = c.get('/admin/mcp').json()
    assert body['audit']['total'] == 2
    assert body['audit']['conflicts'][0]['tool'] == 'read_file'


def test_mcp_toggle_and_export():
    c, _, _ = _client()
    assert c.post('/admin/mcp/solo/toggle?enabled=false').json()['enabled'] is False
    assert c.post('/admin/mcp/ghost/toggle').status_code == 404
    exp = c.get('/admin/mcp/export/claude').json()
    assert 'fs' in exp['mcpServers']
    assert 'solo' not in exp['mcpServers']        # solo 只挂 workbuddy


def test_context_endpoint_handles_missing_dirs(tmp_path):
    c, _, _ = _client(memory_root=str(tmp_path / 'nonexistent'))
    body = c.get('/admin/context').json()
    assert body['total_chars'] == 0
    assert [b['key'] for b in body['blocks']] == ['L1', 'L2']


def test_context_endpoint_reads_layered_files(tmp_path):
    root = tmp_path / 'shared'
    (root / 'rules').mkdir(parents=True)
    (root / 'memory').mkdir(parents=True)
    (root / 'rules' / 'AGENTS.md').write_text('规则：永远别删数据。', encoding='utf-8')
    import datetime
    today = datetime.date.today().isoformat()
    (root / 'memory' / f'{today}.md').write_text('今天做了网关。', encoding='utf-8')

    c, _, _ = _client(memory_root=str(root))
    body = c.get('/admin/context').json()
    texts = ' '.join(b['content'] for b in body['blocks'])
    assert '永远别删数据' in texts
    assert '今天做了网关' in texts
    assert body['total_chars'] > 0
