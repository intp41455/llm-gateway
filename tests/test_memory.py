# -*- coding: utf-8 -*-
"""记忆分层装配：L1 必读 / L2 近 N 天 / L3 只给检索指引。"""
from __future__ import annotations

import datetime
import os

from app.memory_layers import DEFAULT_LAYERS, MemoryLayers


def build(tmp_path, rules=(), memory=None, refs=()):
    root = tmp_path / 'shared'
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    (root / 'memory').mkdir(parents=True, exist_ok=True)
    (root / 'references').mkdir(parents=True, exist_ok=True)
    for name, text in rules:
        (root / 'rules' / name).write_text(text, encoding='utf-8')
    for name, text in (memory or []):
        (root / 'memory' / name).write_text(text, encoding='utf-8')
    for name, text in refs:
        (root / 'references' / name).write_text(text, encoding='utf-8')
    return MemoryLayers(str(root))


def test_missing_dirs_yield_empty_context(tmp_path):
    ml = MemoryLayers(str(tmp_path / 'nothing'))
    ctx = ml.assemble()
    assert ctx.total_chars == 0
    assert ctx.source_files == []
    assert ctx.to_prompt() == ''


def test_l1_always_included(tmp_path):
    ml = build(tmp_path, rules=[('AGENTS.md', '规则一：不许删数据。')])
    ctx = ml.assemble()
    assert '不许删数据' in ctx.to_prompt()
    keys = [b['key'] for b in ctx.blocks]
    assert keys == ['L1', 'L2']


def test_l1_sorted_deterministically(tmp_path):
    ml = build(tmp_path, rules=[('b.md', 'BBB'), ('a.md', 'AAA')])
    ctx = ml.assemble()
    text = ctx.blocks[0]['content']
    assert text.index('AAA') < text.index('BBB')


def test_l2_reads_today_and_memory_md(tmp_path):
    today = datetime.date.today().isoformat()
    ml = build(tmp_path, memory=[(f'{today}.md', '今天干了啥'), ('MEMORY.md', '长期结论')])
    ctx = ml.assemble()
    text = ctx.to_prompt()
    assert '今天干了啥' in text and '长期结论' in text


def test_l2_recent_days_window(tmp_path):
    old = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    today = datetime.date.today().isoformat()
    ml = build(tmp_path, memory=[(f'{old}.md', '很久以前的事'), (f'{today}.md', '今天的事')])
    text = ml.assemble().to_prompt()
    assert '今天的事' in text
    assert '很久以前的事' not in text


def test_l2_recent_days_configurable(tmp_path):
    d5 = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    ml = build(tmp_path, memory=[(f'{d5}.md', '五天前')])
    assert '五天前' not in ml.assemble().to_prompt()
    ml2 = MemoryLayers(ml.root, recent_days=10)
    assert '五天前' in ml2.assemble().to_prompt()


def test_l3_never_inlined_but_hinted(tmp_path):
    ml = build(tmp_path, refs=[('huge.md', '原始语料' * 1000)])
    ctx = ml.assemble()
    assert '原始语料' not in ctx.to_prompt()          # 不整包注入
    assert [b['key'] for b in ctx.blocks] == ['L1', 'L2']
    hint = ml.retrieve_hint('考勤规则')
    assert hint['mode'] == 'on_demand'
    assert '考勤规则' in hint['query']


def test_oversized_layer_keeps_tail(tmp_path):
    """超限时保留尾部——最新内容更有价值。"""
    ml = build(tmp_path, rules=[('big.md', 'OLD' + 'x' * 5000 + 'NEWEST')])
    ml.layers = [type(DEFAULT_LAYERS[0])(**{**DEFAULT_LAYERS[0].__dict__,
                                           'max_chars': 100})]
    ctx = ml.assemble()
    content = ctx.blocks[0]['content']
    assert len(content) <= 100
    assert 'NEWEST' in content
    assert not content.startswith('OLD')


def test_budget_caps_total(tmp_path):
    ml = build(tmp_path,
               rules=[('a.md', 'A' * 5000)],
               memory=[(datetime.date.today().isoformat(), 'B' * 5000)])
    ctx = ml.assemble(budget_chars=300)
    assert ctx.total_chars <= 300


def test_to_prompt_has_layer_headers(tmp_path):
    today = datetime.date.today().isoformat()
    ml = build(tmp_path, rules=[('AGENTS.md', '规则内容')], memory=[(f'{today}.md', '记忆内容')])
    p = ml.assemble().to_prompt()
    assert '# [L1] 规则层' in p
    assert '# [L2] 记忆层' in p
    assert p.index('规则层') < p.index('记忆层')     # L1 在 L2 前面


def test_empty_layer_emits_no_header(tmp_path):
    """空层不输出标题——避免 prompt 里出现一堆空壳小标题。"""
    ml = build(tmp_path, rules=[('AGENTS.md', '规则内容')])
    p = ml.assemble().to_prompt()
    assert '# [L1] 规则层' in p
    assert '# [L2] 记忆层' not in p


def test_describe_shape(tmp_path):
    d = MemoryLayers(str(tmp_path)).describe()
    assert [l['key'] for l in d['layers']] == ['L1', 'L2', 'L3']
    assert d['layers'][2]['read_mode'] == 'on_demand'
    assert d['layers'][2]['max_chars'] == 0
