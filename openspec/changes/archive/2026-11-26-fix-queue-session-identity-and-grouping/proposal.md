# Change: 修复待提炼对话身份归一化、父子会话归组与无头脚本过滤

## Why

在控制面「沉淀与审批 → 待提炼对话」队列库存中，当前存在 4 类叠加问题导致列表难以辨识与筛选：

1. **来源与主题不可见**：`QueueInventoryPanel` 未展示队列记录已持有的 `workspace` / `project` 信息；同时磁盘扫描（`adapters/sessions.py`）、归档对账与 Hook 兜底逻辑统一使用 `sid[:16]` 生成占位标题，导致 Codex `rollout-YYYY-MM-...` 文件名被截断为完全相同的 `codex session rollout-YYYY-MM-` 前缀。
2. **长对话分片平铺展示**：超长会话按轮次边界拆分为 2–4 个分片后在队列中平铺为多行同名条目，视觉上与重复入队难以区分。
3. **Codex 主会话跨通道重复入队**：MCP / Hook 使用 `codex:<uuid>` 作为 `session_key`，而磁盘扫描与归档同步直接使用 `Path.stem`（`codex:rollout-<ts>-<uuid>`），导致同一主会话重复入队。
4. **子 Agent 与无头批处理污染队列**：Codex 派生的子 Agent（JSONL 第 1 行 `session_meta` 原生携带 `parent_thread_id` 与 `thread_source="subagent"`）被当成独立主会话排进自动批次；外部无头批处理脚本调用（无父会话 ID，但在受管工作区运行）绕过仅基于文件名前缀的过滤进入 `queued`。

## What Changes

- **JSONL 元数据提取与可读标题生成**：从 Codex `session_meta` 与 Claude Code JSONL 头部提取规范 `canonical_session_id`、`parent_session_id`、`is_subagent`、`subagent_nickname` / `subagent_role`、`workspace` 与 `custom-title`；按「显式/自定义标题 → 首条有效用户消息摘录 → `[工作区目录] 客户端 · 尾段 ID`」生成可读标题，消除前缀截断撞名。
- **Codex 会话身份归一化与父子归属**：磁盘扫描与归档同步将 Codex `session_id` 归一化为 `session_meta.payload.id`（或文件名中提取的 UUID），与 MCP/Hook 共享同一 `codex:<uuid>` 去重口径；在 `distill_queue` 增加 `parent_session_key` 与 `is_subagent` 列（同步 `schema.py`、`migrations.py`、`docs/schema.sql`、`docs/data-model.md`），使自动批次认领默认跳过 `is_subagent = 1` 的子代理记录，仅在显式指定 `queue_ids` 时放行。
- **可配置无头脚本过滤与存量一键重筛清洗**：在 `SessionTriageConfig` 新增 `exclude_content_prefixes`（公开仓默认空，同步 `flywheel.yaml.example`）并叠加通用无交互/禁用工具单轮特征，将孤立无头批处理归入 `excluded (SUBAGENT_FRAGMENT)`；扩展 `re_triage_queue` 支持干跑预览与实际应用时顺带刷新未消费条目的标题/工作区/父子关系，并将与规范主会话重复的旧 `codex:rollout-...` 条目标记为 `SUPERSEDED`。
- **前端工作区展示与主会话树形折叠**：`QueueInventoryPanel` 展示工作区短名（悬停见完整路径），并按根会话将同会话多分片与所属子代理折叠归组到主会话行下，支持展开查看与按需单选插队。
- **既有队列瘦身加固一并纳入基线**：保留并纳入 JSONL 提取跳过 `tool_result`/`thinking`/`reasoning` 块、单会话分片上限保头保尾（`MAX_CHUNKS_PER_SESSION = 4`）及 `POST /api/site/queue/sync` 默认不泛洪旧 `pending` 存根的回归行为。

## Capabilities

### Modified Capabilities
- `distill-queue-pipeline`: 新增会话身份归一化与可读标题提取、父子会话归属与自动认领隔离、可配置无头脚本过滤、重筛存量清洗，以及前端按根会话树形折叠与工作区展示要求。
- `data-modeling-foundation`: 在 `distill_queue` 表增加 `parent_session_key` 与 `is_subagent` 列及父会话索引，并保持权威 DDL `docs/schema.sql` 与运行时迁移 1:1 一致。

## Impact

- 后端：`backend/ai_flywheel/config.py`、`models.py`、`adapters/sessions.py`、`core/triage.py`、`core/queue_service.py`、`storage/db/schema.py`、`storage/db/migrations.py`、`storage/db/queue_repo.py`、`routes/runs.py`
- 配置与文档：`flywheel.yaml.example`、`docs/schema.sql`、`docs/data-model.md`
- 前端：`frontend/src/types.ts`、`frontend/src/components/workflows/QueueInventoryPanel.tsx`
- 测试：`tests/test_distill_queue.py`、`tests/test_data_foundation.py`、`tests/test_arch_invariants.py`
