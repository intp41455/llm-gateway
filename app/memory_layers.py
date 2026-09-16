# -*- coding: utf-8 -*-
"""跨客户端统一记忆分层：规则层 / 记忆层 / 资料层。

这是我实际在用的方案——多个 AI 客户端（不同模型、不同上下文窗口）共享同一套
上下文约束。核心是**分层 + 单一事实来源**：

  L1 规则层  —— 所有客户端必读的唯一规则源。稳定、人工维护、不进检索。
  L2 记忆层  —— 追加式的日志与结论，按时间组织，增量为王。
  L3 资料层  —— 原始语料（会话记录/文档），体量最大，走检索而不是全量注入。

为什么要分层：如果把 L3 也塞进 prompt，上下文会被淹没；如果只有 L1，
模型不知道最近发生了什么。分层让「稳定约束」和「动态上下文」各归其位。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass
class LayerSpec:
    key: str
    title: str
    root: str
    read_mode: str            # always | recent | on_demand
    max_chars: int
    description: str


DEFAULT_LAYERS: list[LayerSpec] = [
    LayerSpec('L1', '规则层', 'rules', 'always', 4000,
              '所有客户端必读的唯一规则源，人工维护，不进检索'),
    LayerSpec('L2', '记忆层', 'memory', 'recent', 6000,
              '追加式日志与结论，按日期检索最近 N 天'),
    LayerSpec('L3', '资料层', 'references', 'on_demand', 0,
              '原始语料，只经检索注入，不整包塞进上下文'),
]


@dataclass
class AssembledContext:
    blocks: list[dict] = field(default_factory=list)
    total_chars: int = 0
    source_files: list[str] = field(default_factory=list)

    def to_prompt(self) -> str:
        parts = []
        for b in self.blocks:
            if b['content'].strip():
                parts.append(f"# [{b['key']}] {b['title']}\n{b['content'].strip()}")
        return '\n\n'.join(parts)


class MemoryLayers:
    def __init__(self, root: str, layers: list[LayerSpec] | None = None,
                 recent_days: int = 3):
        self.root = root
        self.layers = layers or DEFAULT_LAYERS
        self.recent_days = recent_days

    def _read(self, path: str, limit: int) -> str:
        if not os.path.isfile(path):
            return ''
        with open(path, encoding='utf-8', errors='replace') as f:
            txt = f.read()
        # 超限时保留尾部——最新内容更有价值
        return txt[-limit:] if limit and len(txt) > limit else txt

    def _layer_files(self, spec: LayerSpec, today: date | None = None) -> list[str]:
        base = os.path.join(self.root, spec.root)
        if not os.path.isdir(base):
            return []
        if spec.read_mode == 'always':
            return [os.path.join(base, f) for f in sorted(os.listdir(base))
                    if f.endswith('.md')]
        if spec.read_mode == 'recent':
            today = today or date.today()
            out = []
            for i in range(self.recent_days):
                d = today - timedelta(days=i)
                for candidate in (f'{d.isoformat()}.md', 'MEMORY.md'):
                    p = os.path.join(base, candidate)
                    if os.path.isfile(p) and p not in out:
                        out.append(p)
            return out
        return []   # on_demand：由检索层按需注入

    def assemble(self, budget_chars: int = 12000,
                 today: date | None = None) -> AssembledContext:
        ctx = AssembledContext()
        used = 0
        for spec in self.layers:
            if spec.read_mode == 'on_demand':
                continue
            cap = min(spec.max_chars or budget_chars, budget_chars - used)
            if cap <= 0:
                break
            chunks = []
            for p in self._layer_files(spec, today):
                c = self._read(p, cap - sum(len(x) for x in chunks))
                if c:
                    chunks.append(c)
                    ctx.source_files.append(p)
            content = '\n\n'.join(chunks)
            ctx.blocks.append({'key': spec.key, 'title': spec.title,
                               'content': content, 'chars': len(content)})
            used += len(content)
        ctx.total_chars = used
        return ctx

    def retrieve_hint(self, query: str) -> dict:
        """L3 不整包注入，只给出「该去检索什么」的指引。"""
        return {
            'layer': 'L3',
            'mode': 'on_demand',
            'query': query,
            'instruction': '原始语料请通过检索获取（向量+关键词混合召回），'
                           '命中片段须携带来源引用，不要凭空生成。',
        }

    def describe(self) -> dict:
        return {
            'root': self.root,
            'recent_days': self.recent_days,
            'layers': [{'key': s.key, 'title': s.title, 'root': s.root,
                        'read_mode': s.read_mode, 'max_chars': s.max_chars,
                        'description': s.description} for s in self.layers],
        }
