# -*- coding: utf-8 -*-
"""配置加载：YAML 覆盖、默认路由生成、key 缺失时优雅降级。"""
from __future__ import annotations

import os

import pytest

from app.config import GatewayConfig, load_config


@pytest.fixture
def yaml_file(tmp_path, monkeypatch):
    def _write(text: str) -> str:
        p = tmp_path / 'providers.yaml'
        p.write_text(text, encoding='utf-8')
        return str(p)

    return _write


def test_missing_file_falls_back_to_builtins():
    cfg = load_config('C:/definitely/not/here.yaml')
    assert isinstance(cfg, GatewayConfig)
    assert {'deepseek', 'dashscope', 'openai', 'senseaudio'} <= set(cfg.providers)
    assert 'default' in cfg.routes


def test_default_route_ordered_by_priority():
    cfg = load_config('C:/definitely/not/here.yaml')
    chain = cfg.routes['default'].chain
    # priority: deepseek 10 < dashscope 20 < openai 30 < senseaudio 90
    assert chain[0][0] == 'deepseek'
    assert [p for p, _ in chain] == sorted(
        [p for p, _ in chain],
        key=lambda n: cfg.providers[n].priority)


def test_explicit_routes_override(yaml_file):
    path = yaml_file("""
providers:
  p1: {base_url: 'https://p1/v1', api_key_env: '', models: [m1]}
  p2: {base_url: 'https://p2/v1', api_key_env: '', models: [m2]}
routes:
  fast: [p1:m1, p2:m2]
""")
    cfg = load_config(path)
    assert cfg.routes['fast'].chain == [('p1', 'm1'), ('p2', 'm2')]


def test_route_dict_form(yaml_file):
    path = yaml_file("""
providers:
  p1: {base_url: 'https://p1/v1', api_key_env: '', models: [m1]}
routes:
  alias: [{provider: p1, model: m1}]
""")
    assert load_config(path).routes['alias'].chain == [('p1', 'm1')]


def test_base_url_trailing_slash_stripped(yaml_file):
    path = yaml_file("""
providers:
  p1: {base_url: 'https://p1/v1///', api_key_env: '', models: [m1]}
""")
    assert load_config(path).providers['p1'].base_url == 'https://p1/v1'


def test_provider_merges_with_builtin(yaml_file):
    path = yaml_file("""
providers:
  deepseek:
    priority: 1
""")
    cfg = load_config(path)
    ds = cfg.providers['deepseek']
    assert ds.priority == 1
    assert ds.base_url == 'https://api.deepseek.com/v1'    # 内置值保留
    assert 'deepseek-chat' in ds.models


def test_budget_parsed(yaml_file):
    path = yaml_file("""
providers:
  p1: {base_url: 'https://p1/v1', api_key_env: '', models: [m1]}
budget:
  daily_tokens: 12345
  per_provider_daily_tokens: 678
  price_per_1k: {p1: 3.5}
""")
    b = load_config(path).budget
    assert (b.daily_tokens, b.per_provider_daily_tokens) == (12345, 678)
    assert b.price_per_1k == {'p1': 3.5}


def test_breaker_settings_from_yaml(yaml_file):
    path = yaml_file("""
breaker_fail_threshold: 7
breaker_recovery_seconds: 5.5
""")
    cfg = load_config(path)
    assert cfg.breaker_fail_threshold == 7
    assert cfg.breaker_recovery_seconds == 5.5


def test_missing_api_key_env_marks_unconfigured(yaml_file, monkeypatch):
    monkeypatch.delenv('MY_FAKE_KEY_XYZ', raising=False)
    path = yaml_file("""
providers:
  nokey: {base_url: 'https://p/v1', api_key_env: MY_FAKE_KEY_XYZ, models: [m]}
""")
    cfg = load_config(path)
    assert cfg.providers['nokey'].configured is False
    assert 'nokey' not in [p.name for p in cfg.available_providers()]

    monkeypatch.setenv('MY_FAKE_KEY_XYZ', 'sk-xxx')
    cfg2 = load_config(path)
    assert cfg2.providers['nokey'].configured is True
    assert 'nokey' in [p.name for p in cfg2.available_providers()]


def test_provider_without_key_env_considered_ok(yaml_file):
    path = yaml_file("""
providers:
  local: {base_url: 'http://localhost:11434/v1', api_key_env: '', models: [llama3]}
""")
    cfg = load_config(path)
    assert cfg.providers['local'].configured is True


def test_disabled_provider_excluded(yaml_file):
    path = yaml_file("""
providers:
  'off':
    base_url: 'https://p/v1'
    api_key_env: ''
    models: [m]
    enabled: false
""")
    cfg = load_config(path)
    assert cfg.providers['off'].enabled is False
    assert 'off' not in [p.name for p in cfg.available_providers()]


def test_yaml_boolean_key_pitfall_is_normalized(yaml_file):
    """YAML 1.1 会把不带引号的 on/off/yes/no 当布尔值。

    这是一个真实踩过的坑：provider 名写成 `off:`（无引号）时会被解析成 False，
    配置里就再也找不到这个名字。config 层统一 str() 兜底，行为至少可预测。
    """
    path = yaml_file("""
providers:
  off: {base_url: 'https://p/v1', api_key_env: '', models: [m]}
""")
    cfg = load_config(path)
    assert 'off' not in cfg.providers          # YAML 已把 key 变成布尔
    assert 'False' in cfg.providers           # 但仍能读到，只是名字是 'False'
    # 正确写法是加引号
    path2 = yaml_file("""
providers:
  'off': {base_url: 'https://p/v1', api_key_env: '', models: [m]}
""")
    assert 'off' in load_config(path2).providers


def test_env_var_selects_config_path(yaml_file, monkeypatch):
    path = yaml_file("""
providers:
  fromenv: {base_url: 'https://e/v1', api_key_env: '', models: [m]}
routes: {default: ['fromenv:m']}
""")
    monkeypatch.setenv('GATEWAY_CONFIG', path)
    cfg = load_config()
    assert cfg.routes['default'].chain == [('fromenv', 'm')]


def test_real_project_config_loads():
    """仓库里那份 providers.yaml 必须是可加载的。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, 'config', 'providers.yaml')
    if not os.path.isfile(path):
        pytest.skip('配置文件不存在')
    cfg = load_config(path)
    assert 'default' in cfg.routes
    assert len(cfg.routes['default'].chain) >= 2      # 至少两级降级
    for prov, model in cfg.routes['default'].chain:
        assert prov in cfg.providers
