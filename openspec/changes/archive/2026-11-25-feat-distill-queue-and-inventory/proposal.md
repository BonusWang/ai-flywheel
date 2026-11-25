# Proposal: 沉淀队列机制、双端归档触发与 Workflows 库存看板

## Why

在完成 `feat-data-foundation-and-modeling` 数据底座（`sys_dict_items`、`distill_queue`、`sessions` 主档扩展与 `audit_events` 结构化状态轨迹）之后，AI Flywheel 仍缺少运行时的显式队列状态机与库存可观测界面，表现为四个直接痛点：

1. **流水线黑盒、无库存与进度感知**：控制台无法直观看到 Codex / Claude Code 分别积压了多少有效待处理对话（Lag）、哪些已被规则排除、哪些正在处理、哪些已沉淀到哪一次批次、还需几批能清空库存。
2. **接入端触发不精准、脏会话污染**：Claude Code 当前将 `Stop`（每轮回复结束）接入入队，半成品对话频繁涌入；Codex 活跃会话与已归档会话（`archived_sessions/`）未做严格区分，且缺少“取消归档 / 续聊撤回”和“再次归档增量入队”机制。
3. **缺少对话内显式控制与 `session_key` 对齐**：用户在对话中无法通过 MCP 一句话声明“本会话不沉淀（`opt_out`）”或“撤回排队”，且模型在未透传真实 `session_key` 时调用 MCP 易与 Hook 产生分裂记录。
4. **超大上下文截尾与毒丸重试缺失**：当前读取会话直接截取前 24000 字符，超大上下文后半段根因与结论会被静默丢弃；单条异常会话失败时缺少独立重试计数与死信隔离（DLQ）。

本次变更基于已归档的 v4 数据底座，实现完整的沉淀队列状态机、Codex 严格归档制 + Claude Code exit 触发制 + MCP 显式声明、超大上下文瘦身/分片、P00–P07 队列消费衔接，以及 `Workflows` 页的「沉淀队列（库存）+ 执行流水线」双 Tab 控制台。

## What Changes

- **核心队列状态机与超大上下文处理**（`backend/ai_flywheel/core/queue_service.py`、`config.py`、`flywheel.yaml.example`）：
  - 新增 `DistillQueueService`，统一收口 4 类入队事件：`EXPLICIT_ENQUEUE`、`ARCHIVE`、`WITHDRAW`、`OPT_OUT`。
  - 实现三级入队闸门：① `opt_out` 或显式不沉淀标记（`#no-flywheel` / `本会话不沉淀`）一票否决 → `excluded(USER_OPT_OUT)`；② 手工/MCP 声明直通高优 `queued`；③ 可配置规则过滤（复用 `core/triage.py`，支持 `re_triage(dry_run)` 预览与重筛）。
  - 实现撤回再输入与增量水位线：未消费 `queued` 撤回为 `withdrawn`；再次归档时未消费旧项标记 `superseded`，已消费会话比对 `last_consumed_hash / last_consumed_turn` 仅对增量区间生成 `rev + 1`。
  - 实现超大上下文 0-token 结构化瘦身 + 按轮次边界自动分片（`chunk 1/N .. N/N`、`is_oversized=1`），并在读取文本时按分片区间精准加载，避免硬截断丢尾。
  - 实现队列项级失败重试与死信隔离（超过 `max_retries` 转 `dead_letter`），所有状态变迁统一记录到 `audit_events`（`entity_type="queue_item"`）。
  - 新增 `SessionsConfig` 扩展配置：`codex_archive_sources`、`queue_max_item_chars`（默认 24000）、`queue_max_retries`（默认 3）、`codex_archive_sync_interval_seconds`（默认 60），同步写入 `flywheel.yaml.example`。
- **双端精准触发与 MCP 显式声明**（`core/hooks.py`、`core/connect_templates.py`、`mcp_server.py`、`core/publication.py`、`app.py`、`runtime.py`）：
  - **Codex 严格归档制**：新增 `reconcile_codex_archives()` 扫描配置的 `codex_archive_sources`（如 `~/.codex/archived_sessions`）：新文件出现 → `ARCHIVE` 入队；已入队（`queued`）文件消失（取消归档）→ 自动 `WITHDRAW`；文件增大后重现 → 增量 `rev + 1`。`serve` 角色后台守护线程定期执行轻量同步并在停机时干净退出。
  - **Claude Code exit 触发制**：`connect_templates.py` 与 `hooks.py` 改为 `SessionEnd` 触发 `ARCHIVE`、`SessionStart(source="resume")` 与同 `session_id` 再次 `UserPromptSubmit` 触发 `WITHDRAW`；`Stop` 不再触发入队。
  - **`session_key` 对齐**：`UserPromptSubmit` 注入上下文头增加 `session=<session_key>`，使模型调用 MCP 时精准命中同一会话。
  - **MCP 工具**：新增 `flywheel_session_intent(action="enqueue"|"opt_out"|"withdraw", ...)` 并同步 `MCP_TOOL_CATALOG`（7 Tools + 4 Resources）。
- **P00–P07 流水线与队列无缝衔接**（`core/pipeline/coordinator.py`、`core/pipeline/recovery.py`）：
  - `P00_PREPARE` 对账归档源与队列库存（同时向后兼容既有直接 `register_session` 调用方与旧测试）；
  - `P01_BEGIN` 按优先级认领 `queued` 项（支持 `queue_ids` 或 `session_keys` 指定插队），标记 `processing` 并绑定 `run_id`；
  - `P05_SETTLE` 结算已审查项为 `processed` 并推进会话主档消费水位线，未审查项退回 `queued`；
  - 失败时递增 `retry_count`（超限转 `dead_letter`），审批取消（`CANCELLED`）不计失败重试安全退回 `queued`。
- **队列控制面 API 与 Workflows 双 Tab 看板**（`routes/runs.py`、`frontend/src/pages/WorkflowsPage.tsx`、`frontend/src/components/workflows/QueueInventoryPanel.tsx`、`frontend/src/api.ts`、`frontend/src/types.ts`）：
  - 新增 `GET /api/site/queue`、`GET /api/site/queue/{queue_id}/events`、`POST /api/site/queue/sync`、`POST /api/site/queue/{queue_id}/action`、`POST /api/site/queue/bulk-action`、`POST /api/site/queue/re-triage`，并扩展 `POST /api/site/runs` 支持 `queue_ids`。
  - `WorkflowsPage` 新增顶部常驻库存条（6 态计数、按工具分片、预计清仓批次数、一键同步归档）与「沉淀队列（库存）」/「执行流水线（P00–P07）」双 Tab；队列 Tab 消费 `/api/site/dicts`，复用 `ListToolbar` + `PaginationBar`，支持筛选、轨迹展开、单条/批量操作（插队立即沉淀 / 捞回入队 / 排除 / 标不沉淀）、规则重筛预览及一键跳转关联批次。

## Capabilities

### New Capabilities
- `distill-queue-pipeline`：显式沉淀队列状态机、三级入队闸门、撤回与增量 revision、超大上下文瘦身与分片、死信隔离、Codex 严格归档同步、Claude Code exit 触发、MCP `flywheel_session_intent` 以及 Workflows 库存与流水线双 Tab 看板。

### Modified Capabilities
- `workflow-orchestration`：扩展 P00/P01/P05 与失败/取消收尾契约，使其优先认领与结算 `distill_queue` 工作项并支持 `queue_ids` 指定插队，同时保持既有 `session_keys` 与单测向后兼容。

## Impact

- 后端新增：`backend/ai_flywheel/core/queue_service.py`。
- 后端修改：`config.py`、`flywheel.yaml.example`、`core/triage.py`、`core/hooks.py`、`core/connect_templates.py`、`core/publication.py`、`mcp_server.py`、`runtime.py`、`app.py`、`core/pipeline/coordinator.py`、`core/pipeline/recovery.py`、`routes/runs.py`。
- 前端新增：`frontend/src/components/workflows/QueueInventoryPanel.tsx`。
- 前端修改：`frontend/src/pages/WorkflowsPage.tsx`、`frontend/src/api.ts`、`frontend/src/types.ts`。
- 测试新增/修改：`tests/test_distill_queue.py`、`tests/test_approval_channel_abstraction.py`、`tests/test_flywheel_core.py`。
- 向后兼容：既有 `flywheel_register_session`、`flywheel_ingest_session`、`session_keys` 启动批次及全部既有 P00–P07 副作用不变量继续成立。
