# CLAUDE.md

本文件为 Claude Code（及后续 AI 编码代理）在本仓库工作时提供指引。

## 项目目的

**my-ai-gateway**：多模型 LLM 网关。把多个 LLM 供应商（DeepSeek / 通义 DashScope / OpenAI / 思音 SenseAudio）收敛成一个 **OpenAI 兼容入口**，解决多 Agent 共用多家模型的问题：

- **失败降级**：模型别名后挂有序候选链，逐级重试（见 `config/providers.yaml` 的 `routes:`）。
- **熔断**：某 provider 连续失败 3 次直接跳过，30 秒后半开试探（`app/circuit.py`）。
- **额度记账**：按 provider / 调用方 / 自然日三维落账，含成本估算（`app/quota.py`）。
- **MCP 工具链治理**：单一注册表（SSOT）渲染成各客户端配置，自动查工具名冲突（`app/mcp_registry.py`）。
- **记忆分层**：L1 规则层（必读）/ L2 记忆层（近 N 天）/ L3 资料层（只给检索指引）（`app/memory_layers.py`）。

对外接口完全兼容 OpenAI，客户端只需改 `base_url`。

## 技术栈

- **Python ≥ 3.10**（Docker 镜像用 3.11-slim），`pyproject.toml` 管理（可用 `uv`，已有 `uv.lock`）。
- **FastAPI + uvicorn**：HTTP 服务入口（`app/main.py`）。
- **httpx**：调用上游供应商（`app/providers.py`，统一 OpenAI 协议 + 指数退避重试）。
- **Pydantic v2**：对外契约与异常类型（`app/schemas.py`）。
- **PyYAML**：外置配置解析（`app/config.py` 读取 `config/providers.yaml`）。
- **pytest / pytest-asyncio**：测试（dev 可选依赖）。
- **flask + requests**：仅在遗留代码中使用（根目录 `app.py` 与 `legacy/flask_proxy_original.py`），不属于主链路。

## 目录结构

```
app/            FastAPI 网关主体
  main.py           应用工厂 create_app() + 结构化 JSON 日志 + 路由
  config.py         配置加载 + provider 内置默认 + 路由表
  schemas.py        对外契约（OpenAI 兼容）+ 异常类型
  providers.py      供应商适配器（统一 OpenAI 协议 + 指数退避）
  router.py         路由内核：别名解析 → 熔断准入 → 逐级降级 → 记账
  circuit.py        熔断器三态机 CLOSED/OPEN/HALF_OPEN
  quota.py          额度与成本记账（provider / caller / day 三维）
  mcp_registry.py   MCP 注册表：冲突检测 + 多客户端渲染 + 配置往返
  memory_layers.py  记忆分层装配 L1/L2/L3
config/
  providers.yaml    全部外置配置（providers / routes / budget / breaker 参数）
tests/            离线可跑的测试（mock provider，不发真实请求）
legacy/           历史遗留（flask_proxy_original.py），不参与构建
app.py            根目录遗留 Flask 单供应商代理脚本，非入口
docs/             架构图（architecture.png / .html / .json）
Dockerfile        镜像构建（端口 8200，非 root 用户 gateway）
```

## 安装 / 构建 / 运行 / 测试

```bash
# 安装（dev 依赖含 pytest）
pip install -e ".[dev]"

# 配置任意一个 provider 的 key（api_key 一律从环境变量读，不进配置文件）
export DEEPSEEK_API_KEY=sk-xxx        # Windows: set DEEPSEEK_API_KEY=sk-xxx

# 启动（默认端口 8200，可用 GATEWAY_PORT 覆盖）
python -m uvicorn app.main:app --port 8200
# 或
python -m app.main

# 测试（全部离线，不需要网络和真实 key，84 个测试）
python -m pytest -q

# Docker
docker build -t ai-gateway .
docker run -p 8200:8200 -e DEEPSEEK_API_KEY=sk-xxx ai-gateway
```

核心调用示例：

```bash
curl -X POST http://127.0.0.1:8200/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Caller: my-agent" \
  -d '{"model":"default","messages":[{"role":"user","content":"你好"}]}'
```

接口清单：`POST /v1/chat/completions`（核心）、`GET /v1/models`、`GET /health`、`GET /admin/usage`、`GET /admin/mcp`、`POST /admin/mcp/{name}/toggle?enabled=`、`GET /admin/mcp/export/{client}`、`GET /admin/context`。

## 关键约定与坑点

1. **配置外置**：换模型/换供应商只改 `config/providers.yaml`，不动代码。`api_key` 一律从环境变量读（每个 provider 的 `api_key_env` 字段指定变量名），绝不写进配置文件。
2. **YAML 坑**：不带引号的 `on` / `off` / `yes` / `no` 会被 YAML 1.1 解析成布尔值，用作 provider 名时会变成 `True` / `False`。遇到这类命名请加引号：`'off': {...}`。
3. **响应多一个 `gateway` 字段**：除标准 OpenAI 响应外，还带 `route` / `provider` / `model` / `degraded` / `attempts`（每次尝试的 provider、模型、是否成功、延迟、错误），排障时看它确认走了哪条降级链路。
4. **额度超限 ≠ 供应商失败**：额度超限是运营/配置问题，直接 429 暴露（`BudgetExceeded`），不降级；供应商失败是外部不可控因素，才逐级降级（全失败时 502 + `attempts` 明细）。
5. **`usage` 缺失按 0 记账**：部分兼容端点响应没有 `usage` 字段，实现上按 0 记账而不是报错，保留 `raw` 响应便于核对。
6. **测试必须保持离线**：通过 `tests/conftest.py` 的 `client_factory` 注入 `FakeClient`（脚本化行为：dict=成功、Exception=失败、list=按次序模拟先失败后成功），不发起真实 HTTP 请求。新增测试不要破坏这个约定。
7. **已知边界**（README「已知边界」）：
   - 只支持非流式（`stream=false`）；
   - 熔断状态在进程内存，多副本部署需换 Redis；
   - 额度统计是「近似实时」，并发下 `check` 与 `record` 之间不保证原子配额；
   - 成本估算用配置里的单价表（`budget.price_per_1k`），不是账单级精确值。
8. **记忆分层根目录**：`MemoryLayers` 默认读环境变量 `GATEWAY_MEMORY_ROOT`，未设置时用 `~/.shared-agents`。
9. **遗留代码勿混淆**：根目录 `app.py`（Flask）和 `legacy/` 是历史单供应商代理，真正的入口是 `app/main.py`（FastAPI，`create_app()` 应用工厂）。
10. **MCP 预置 server**：`default_mcp_registry()`（`app/main.py`）内置 filesystem / fetch / sqlite 三个 server 及各自的 `clients` 覆盖清单，审计与渲染逻辑在 `app/mcp_registry.py`。
