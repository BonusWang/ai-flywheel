# Change: Antigravity 会话原生采集与高保真沉淀流水线（P1）

## Why

对照真实多客户端会话的实证回放显示，自动沉淀流水线与高质量手工沉淀之间仍存在四处结构性断层：

1. **Antigravity 会话缺少原生 JSONL 解析与被动采集支持**：`adapters/sessions.py` 仅适配 Codex / Claude Code 的 JSONL 结构；面对 Antigravity `transcript.jsonl`（以 `USER_INPUT`、`PLANNER_RESPONSE`、`GENERIC` 等步态类型记录对话与工具输出）时，未过滤 `GENERIC` 原始工具块会导致提取文本膨胀超过 10 倍，且无法抽取规范的 `antigravity:<uuid>` 身份、可读标题与静默窗口保护。
2. **P02 硬截断 3000 字符导致长排查关键事实大面积丢失**：`adapters/runners/prompts.py` 将单会话送审文本硬截断为 `P02_SESSION_TEXT_LIMIT = 3000`（头 1450 + 尾 1450）。在 16k–20k 字符的真实排障会话抽样中，中段核实过的多张核心业务表名、根因异常与策略结论丢失率达 50%–100%，即使 P03 已支持四域增量落盘也无从触发。
3. **单一低配模型与 220–450 字草稿上限制约文档深度**：`ApiRunnerConfig` 仅支持全局单一 `model` / `reasoning_effort`，无法为深度审查（P02）与写入规划（P03）分层配置高能力模型；同时 P03 提示词将 `content_preview` 限制在 220–450 字，导致产出退化为浅摘要并由适配器填入占位句。
4. **多事件拆卡、事故台账路由与单批名额存在瓶颈**：P02/P03 缺少显式的多事件拆卡约束，挂载 Wiki 的协作事故台账（`type: incident`）尚未纳入受控路由与强制敏感审批，且四域增量表页与主经验卡共用窄单批上限容易互相挤占；此外前端自动化页面尚未展示 P0 新增的进程启动时间与漏跑状态。

## What Changes

- **Antigravity 原生会话解析与被动采集（`adapters/sessions.py` + `core/triage.py` + `core/queue_service.py`）**：
  - 支持原生解析 Antigravity `transcript.jsonl`：映射 `USER_INPUT` → `[user]`、`PLANNER_RESPONSE` → `[assistant]`，剥离 `GENERIC` / `CHECKPOINT` / `SYSTEM` 等非对话与原始工具输出块；
  - 从目录结构归一化 `antigravity:<uuid>` 身份并提取首条有效用户请求作可读标题；增加静默窗口（默认 900 秒，可配置）避免扫描正在活跃写入的会话。
- **P02 高保真输入与按阶段模型分层（`config.py` + `flywheel.yaml.example` + `adapters/runners/clients.py` + `prompts.py` + `normalizers.py`）**：
  - 将 P02 单会话送审预算升级为可配置 `runner.api.p02_session_char_limit`（默认 `16000` 字符）；当队列项带有 P0 传入的 `structured_brief` 时，将其置顶注入 P02 输入以防截断丢失关键实体；
  - `ApiRunnerConfig` 新增可选阶段覆盖项 `review_model`、`review_reasoning_effort`、`plan_model`、`plan_reasoning_effort`（未设时回退到默认 `model` / `reasoning_effort`），并将实际生效的 `(model, reasoning_effort)` 纳入 `api_cache` 缓存键与节点遥测；
  - P02 输出契约新增可选 `events`（`delivery` / `incident` / `decision` / `domain_fact`），支持同一会话内独立业务交付与协作事故分拆。
- **P03 契约级草稿深度、受控事故台账路由与领域页独立预算（`adapters/runners/prompts.py` + `adapters/wiki/router.py` + `gate.py` + `models.py` + `core/pipeline/coordinator.py` + `helpers.py`）**：
  - 解除 220–450 字浅摘要约束，引导 P03 按挂载 Wiki 契约产出高信息密度、符合反废话纪律的 T5 / T5b / 领域增量与事故草稿；
  - 在挂载 Schema 治理 Wiki 声明了 `incident` 页面类型且 `allowed_write_prefixes` 允许对应路径前缀时，支持 `incident` 受控路由与写前校验，且 `is_sensitive_write()` 对 `incident` 写入项**强制判定为敏感（必须人工审批）**；
  - `WorkflowConfig` 新增 `max_domain_writes_per_run`（默认 `8`），将项目领域增量页名额与主卡片/规则名额分离计数（仍受 `knowledge_sink.max_write_set` 与高写入审批阈值约束），防止四域表页挤占主经验卡。
- **前端自动化可观测对齐（`frontend/src/types.ts` + `frontend/src/pages/AutomationsPage.tsx`）**：
  - 对齐 P0 新增的 `started_at`、`misfire_grace_seconds`、`catchup_missed_schedule`、`missed_today` 响应字段并在自动化控制台展示。

## Capabilities

### Modified Capabilities
- `distill-queue-pipeline`: 支持 Antigravity `transcript.jsonl` 原生对话抽取、身份归一化与活跃会话静默窗口保护。
- `session-review-contract`: 升级 P02 单会话高保真输入预算（可配置 `p02_session_char_limit` + `structured_brief` 置顶注入）、阶段级模型覆盖与可选 `events` 多事件拆解。
- `write-plan-contract`: 解除 220–450 字浅摘要上限、注入多事件拆卡与精简写作规范、支持阶段级规划模型覆盖与领域页独立名额计数。
- `knowledge-write-gate`: 支持 Schema 声明的 `incident` 页面类型受控路由与写前门禁，且全链路强制归类为敏感待审批写入。
- `automation-console`: 前端控制台类型与界面展示服务启动时间、宽限补跑配置及当日漏跑状态。

## Impact

- 后端：`backend/ai_flywheel/adapters/sessions.py`、`backend/ai_flywheel/core/triage.py`、`backend/ai_flywheel/core/queue_service.py`、`backend/ai_flywheel/config.py`、`backend/ai_flywheel/models.py`、`backend/ai_flywheel/adapters/runners/clients.py`、`backend/ai_flywheel/adapters/runners/prompts.py`、`backend/ai_flywheel/adapters/runners/normalizers.py`、`backend/ai_flywheel/adapters/runners/outcomes.py`、`backend/ai_flywheel/adapters/wiki/router.py`、`backend/ai_flywheel/adapters/wiki/gate.py`、`backend/ai_flywheel/adapters/wiki/t5_adapter.py`、`backend/ai_flywheel/core/pipeline/coordinator.py`、`backend/ai_flywheel/core/pipeline/helpers.py`
- 配置模板：`flywheel.yaml.example`
- 前端：`frontend/src/types.ts`、`frontend/src/pages/AutomationsPage.tsx`
- 测试：`tests/test_agy_sessions.py`（新增）、`tests/test_p02_review_contract.py`、`tests/test_p03_plan_contract.py`、`tests/test_wiki_route_declaration.py`、`tests/test_arch_invariants.py`
- 兼容性：所有新增配置字段均带向后兼容默认值；未配置阶段模型或未在 Wiki Schema 中开放 `incident` 路由时，既有行为零破坏。
