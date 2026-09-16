# -*- coding: utf-8 -*-
"""MCP 注册表：冲突检测、按客户端渲染、导入往返。"""
from __future__ import annotations

import json
import os

from app.mcp_registry import MCPRegistry, MCPServer


def reg() -> MCPRegistry:
    r = MCPRegistry()
    r.register(MCPServer(name='filesystem', transport='stdio', command='npx',
                         args=['-y', '@mcp/server-filesystem', '.'],
                         tools=['read_file', 'write_file'],
                         clients=['claude', 'workbuddy']))
    r.register(MCPServer(name='fetch', transport='stdio', command='uvx',
                         args=['mcp-server-fetch'], tools=['fetch'],
                         clients=['claude']))
    r.register(MCPServer(name='remote-search', transport='http',
                         url='https://example.com/mcp',
                         env={'Authorization': 'Bearer x'},
                         tools=['search'], clients=['workbuddy'], enabled=False))
    return r


def test_register_and_get():
    r = reg()
    assert r.get('filesystem').command == 'npx'
    assert r.get('nope') is None


def test_list_filters_by_client_and_enabled():
    r = reg()
    assert {s.name for s in r.list()} == {'filesystem', 'fetch', 'remote-search'}
    assert {s.name for s in r.list(client='claude')} == {'filesystem', 'fetch'}
    assert {s.name for s in r.list(enabled_only=True)} == {'filesystem', 'fetch'}


def test_unregister_and_toggle():
    r = reg()
    assert r.unregister('fetch') is True
    assert r.unregister('fetch') is False
    assert r.set_enabled('filesystem', False) is True
    assert r.set_enabled('ghost', False) is False


def test_render_only_own_client_servers():
    r = reg()
    cfg = r.render('claude')
    assert set(cfg['mcpServers']) == {'filesystem', 'fetch'}
    assert cfg['mcpServers']['filesystem']['command'] == 'npx'


def test_render_merges_with_existing_config():
    r = reg()
    existing = {'mcpServers': {'manual-server': {'command': 'mytool'}},
                'someOtherKey': 123}
    cfg = r.render('claude', existing)
    assert set(cfg['mcpServers']) == {'manual-server', 'filesystem', 'fetch'}
    assert cfg['someOtherKey'] == 123                      # 不丢别人的键


def test_render_http_transport_shape():
    r = reg()
    r.set_enabled('remote-search', True)
    cfg = r.render('workbuddy')
    entry = cfg['mcpServers']['remote-search']
    assert entry['type'] == 'http'
    assert entry['url'] == 'https://example.com/mcp'
    assert entry['headers']['Authorization'] == 'Bearer x'


def test_conflict_detection():
    r = MCPRegistry()
    r.register(MCPServer(name='a', command='x', tools=['search']))
    r.register(MCPServer(name='b', command='y', tools=['search', 'other']))
    conflicts = r.conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]['tool'] == 'search'
    assert conflicts[0]['servers'] == ['a', 'b']


def test_disabled_server_not_counted_in_conflicts():
    r = MCPRegistry()
    r.register(MCPServer(name='a', command='x', tools=['search']))
    r.register(MCPServer(name='b', command='y', tools=['search'], enabled=False))
    assert r.conflicts() == []


def test_client_coverage_and_audit():
    r = reg()
    cov = r.client_coverage()
    assert cov['claude'] == ['fetch', 'filesystem']
    audit = r.audit()
    assert audit['total'] == 3 and audit['enabled'] == 2
    assert audit['by_transport'] == {'stdio': 2, 'sse': 0, 'http': 1}


def test_export_and_import_roundtrip(tmp_path):
    r = reg()
    dest = str(tmp_path / 'nested' / 'mcp.json')
    r.export('claude', dest)
    assert os.path.isfile(dest)

    back = MCPRegistry.from_json(dest, client='claude')
    assert {s.name for s in back.list()} == {'filesystem', 'fetch'}
    fs = back.get('filesystem')
    assert fs.transport == 'stdio'
    assert fs.args == ['-y', '@mcp/server-filesystem', '.']
    assert fs.clients == ['claude']


def test_from_json_missing_file_returns_empty():
    assert MCPRegistry.from_json('C:/definitely/not/here.json').list() == []


def test_to_dicts_serializable():
    r = reg()
    d = r.to_dicts()
    assert json.dumps(d, ensure_ascii=False)          # 不抛异常
    assert {x['name'] for x in d} == {'filesystem', 'fetch', 'remote-search'}
