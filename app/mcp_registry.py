# -*- coding: utf-8 -*-
"""MCP 工具链治理：一份规范注册表 -> 渲染成 N 个客户端各自的配置格式。

真实痛点：Claude / Codex / WorkBuddy / 各类 IDE 插件的 MCP 配置格式不统一、
各自一份 JSON，加一个 server 要改 N 处，还会出现同名工具冲突和重复 server。
这里的做法是**单一事实来源（SSOT）**：只维护一份注册表，按客户端渲染导出。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

Transport = Literal['stdio', 'sse', 'http']


@dataclass
class MCPServer:
    name: str
    transport: Transport = 'stdio'
    command: str = ''
    args: list[str] = field(default_factory=list)
    url: str = ''
    env: dict[str, str] = field(default_factory=dict)
    tools: list[str] = field(default_factory=list)     # 声明的工具名，用于冲突检测
    clients: list[str] = field(default_factory=list)   # 哪些客户端挂载它
    enabled: bool = True
    note: str = ''

    def to_wire(self, client: str = 'generic') -> dict[str, Any]:
        """渲染成 MCP 客户端配置里的单个条目。"""
        if self.transport == 'stdio':
            entry: dict[str, Any] = {'command': self.command}
            if self.args:
                entry['args'] = list(self.args)
            if self.env:
                entry['env'] = dict(self.env)
        else:
            entry = {'url': self.url, 'transport': self.transport}
            if client in ('workbuddy', 'generic') and self.env:
                entry['headers'] = dict(self.env)
        if client == 'workbuddy':
            entry['type'] = self.transport
        return entry


class MCPRegistry:
    def __init__(self):
        self._servers: dict[str, MCPServer] = {}

    # ---------- 增删改查 ----------
    def register(self, server: MCPServer) -> None:
        self._servers[server.name] = server

    def unregister(self, name: str) -> bool:
        return self._servers.pop(name, None) is not None

    def set_enabled(self, name: str, enabled: bool) -> bool:
        s = self._servers.get(name)
        if not s:
            return False
        s.enabled = enabled
        return True

    def get(self, name: str) -> MCPServer | None:
        return self._servers.get(name)

    def list(self, client: str | None = None, enabled_only: bool = False) -> list[MCPServer]:
        out = []
        for s in self._servers.values():
            if enabled_only and not s.enabled:
                continue
            if client and client not in s.clients:
                continue
            out.append(s)
        return sorted(out, key=lambda x: x.name)

    # ---------- 治理检查 ----------
    def conflicts(self) -> list[dict]:
        """同一工具名被多个启用中的 server 声明 -> 客户端会歧义。"""
        owner: dict[str, list[str]] = {}
        for s in self._servers.values():
            if not s.enabled:
                continue
            for t in s.tools:
                owner.setdefault(t, []).append(s.name)
        return [{'tool': t, 'servers': names}
                for t, names in sorted(owner.items()) if len(names) > 1]

    def client_coverage(self) -> dict[str, list[str]]:
        cov: dict[str, list[str]] = {}
        for s in self.list(enabled_only=True):
            for c in (s.clients or ['<未指定>']):
                cov.setdefault(c, []).append(s.name)
        return {k: sorted(v) for k, v in sorted(cov.items())}

    def audit(self) -> dict:
        return {
            'total': len(self._servers),
            'enabled': len([s for s in self._servers.values() if s.enabled]),
            'by_transport': {
                t: len([s for s in self._servers.values() if s.transport == t])
                for t in ('stdio', 'sse', 'http')},
            'conflicts': self.conflicts(),
            'client_coverage': self.client_coverage(),
        }

    # ---------- 渲染导出 ----------
    def render(self, client: str = 'generic',
               existing: dict[str, Any] | None = None) -> dict[str, Any]:
        """渲染成某客户端可直接写入的配置 dict。

        existing 传入客户端现有配置时会**合并**而不是覆盖其他 server 条目。
        """
        merged: dict[str, Any] = dict(existing or {})
        servers = dict(merged.get('mcpServers') or {})
        for s in self.list(client=client, enabled_only=True):
            servers[s.name] = s.to_wire(client)
        merged['mcpServers'] = servers
        return merged

    def export(self, client: str, dest: str, existing_path: str | None = None) -> str:
        existing = None
        if existing_path and os.path.isfile(existing_path):
            with open(existing_path, encoding='utf-8') as f:
                existing = json.load(f)
        cfg = self.render(client, existing)
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        with open(dest, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return dest

    # ---------- 导入 ----------
    @classmethod
    def from_json(cls, path: str, client: str = 'generic') -> 'MCPRegistry':
        """从某个客户端的现有 mcpServers 配置反向构建注册表。"""
        reg = cls()
        if not os.path.isfile(path):
            return reg
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
        for name, entry in (raw.get('mcpServers') or {}).items():
            if 'command' in entry:
                s = MCPServer(name=name, transport='stdio',
                              command=entry.get('command', ''),
                              args=list(entry.get('args') or []),
                              env=dict(entry.get('env') or {}),
                              clients=[client])
            else:
                s = MCPServer(name=name,
                              transport=entry.get('type') or entry.get('transport') or 'http',
                              url=entry.get('url', ''),
                              env=dict(entry.get('headers') or entry.get('env') or {}),
                              clients=[client])
            reg.register(s)
        return reg

    def to_dicts(self) -> list[dict]:
        return [asdict(s) for s in self.list()]
