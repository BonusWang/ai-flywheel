# Design: 待提炼对话身份归一化、父子会话归组与无头脚本过滤

## 1. 数据模型与 DDL 一致性

在 `distill_queue` 增加两列（并在 `migrations.py` 用 `_ensure_column` 幂等迁移，同步 `docs/schema.sql` 与 `docs/data-model.md`）：

- `parent_session_key TEXT NOT NULL DEFAULT ''`：指向父会话的规范 `session_key`（例如 `codex:<parent-uuid>`）；主会话为空字符串。
- `is_subagent INTEGER NOT NULL DEFAULT 0`：`1` 表示子代理会话，`0` 表示主会话。
- 索引：`CREATE INDEX IF NOT EXISTS idx_distill_queue_parent ON distill_queue(parent_session_key, status);`

**备选方案与取舍**：
- 备选：把 `parent_session_key` / `is_subagent` 只放进 `payload_json`。
- 放弃原因：`list_queued_items_prioritized` 是在 SQL 层按优先级 + FIFO 做 `LIMIT` 的，如果把 `is_subagent` 放进 JSON，就无法在 SQL `WHERE status = 'queued' AND is_subagent = 0` 高效过滤，可能导致一页 `LIMIT` 全被子代理占满而饿死主会话。

## 2. JSONL 元数据提取与可读标题规则

在 `backend/ai_flywheel/core/triage.py`（或供 `adapters/sessions.py` 与 `core/queue_service.py` 共用的 helper）新增 `extract_jsonl_metadata(path: Path)` 与 `derive_readable_title(...)`：

1. **Codex `session_meta` 解析**（读取前 25 行）：
   - `canonical_session_id`：`payload.id` → 正则提取 `rollout-...-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})` 最后一个 UUID → 退化 `path.stem`。
   - `parent_session_id`：`payload.parent_thread_id` → `payload.source.subagent.thread_spawn.parent_thread_id` → 若 `payload.session_id` 非空且不等于 `payload.id` 则取 `payload.session_id`。
   - `is_subagent`：`bool(parent_session_id)` 或 `payload.thread_source == "subagent"` 或 `isinstance(payload.source, dict) and "subagent" in payload.source`。
   - `subagent_nickname` / `subagent_role`：优先取 `payload.agent_nickname` / `payload.agent_role`，其次取 `thread_spawn` 内同名字段。
   - `workspace`：沿用 `cwd` / `workspace` / `project_root`（顶层或 `payload`）。
2. **Claude Code JSONL 解析**：
   - `custom_title`：捕获 `type == "custom-title"` 的 `customTitle` 字段（若有多条取最新非空）。
   - `canonical_session_id`：`sessionId` 字段（退化 `path.stem`）。
   - `is_subagent`：`isSidechain is True` 或文件名前缀命中 `exclude_filename_prefixes`。
3. **可读标题优先级（`derive_readable_title`）**：
   - 判定是否为旧占位标题（空串，或匹配 `^<client>\s+(?:session|archived session|·)\s+`）；
   - 优先级：① 有效显式标题 → ② `custom_title` → ③ 文本中首条真实用户消息（剔除 `[user]`/`[queue-operation]` 前缀、BOM、`<system-reminder>`/`<task-notification>`/`<app-context>` 等包裹块以及 `[$skill](...)` 路径引用）单行压缩到 ≤64 字符 → ④ 兜底 `[目录短名] <client> · <id尾8位>`（无目录则 `<client> · <id尾8位>`）。

## 3. 自动批次认领与子代理归属策略

- `on_archive` 与 `reconcile_codex_archives`、`reconcile_scan_sources` 统一调用 `extract_jsonl_metadata`：
  - 若提取出 `parent_session_id`，计算 `parent_session_key = f"{client}:{parent_session_id}"`，并标记 `is_subagent = True`；
  - 若 `is_subagent` 且具备明确 `parent_session_key`，**允许入队**（挂载在主会话下展示，而非直接扔进 `excluded`），但在 `list_queued_items_prioritized(limit, include_subagents=False)` 默认加 `AND is_subagent = 0`，使其**不进入默认自动批次**；
  - 若维护者通过 `POST /api/site/runs` 显式传入 `queue_ids`（前端勾选或点击「插队整理」），`coordinator.py` 按 `queue_id` 精确认领，子代理可被按需放行。
- **孤立无头脚本拦截**（无 `parent_session_key`）：
  - `SessionTriageConfig` 增加 `exclude_content_prefixes: list[str] = Field(default_factory=list)`，在 `flywheel.yaml.example` 中记录示例说明（公开仓不放个人提示词）。
  - `triage_session` 检查首条用户正文是否以 `exclude_content_prefixes` 中任意前缀开头，或同时满足「≤2 轮交互 + 含通用无工具批处理指令特征（如 `all tools disabled` / `所有工具已关闭` / `严禁调用任何工具`）」，命中则返回 `admit=False, reason_code="SUBAGENT_FRAGMENT"`。

## 4. `re_triage_queue` 存量清洗

- `re_triage_queue(dry_run=True/False)` 扫描 `QUEUED` 与可重筛 `EXCLUDED` 项：
  - 回读存在的 `source_path` / `session.transcript_path`，计算规范 `canonical_key`、可读标题、`workspace`、`parent_session_key`、`is_subagent` 与子代理元信息；
  - 若发现某 `QUEUED` 的旧 `codex:rollout-...` 项对应的规范 `codex:<uuid>` 已存在其他队列记录（`queued`/`processing`/`processed`），将其纳入 `would_exclude`（`reason_code="SUPERSEDED"`）；
  - 当 `dry_run=True` 时仅统计 `metadata_refresh_count` 与 `changes`（`would_exclude`/`would_admit`），**零数据库写入**；
  - 当 `dry_run=False` 时先原地更新标题/工作区/父子列与 `payload`（同时回写 `sessions` 主档的 `title` 与 `workspace`），再执行状态迁移与重复 `SUPERSEDED` 移出。

## 5. 前端树形折叠与工作区徽标

- `frontend/src/types.ts`：`DistillQueueItem` 增加 `parent_session_key?: string`、`is_subagent?: boolean`、`payload?: Record<string, any>`；`QueueReTriageResult` 增加可选 `metadata_refresh_count?: number`。
- `frontend/src/components/workflows/QueueInventoryPanel.tsx`：
  - 将过滤后的 `items` 按 `rootKey = item.parent_session_key || item.session_key` 聚合为组，分页按“根会话组”分页；
  - 单条根会话直接渲染精简行，带工作区目录短名徽标（`title` 悬停完整路径）；
  - 多分片或带子代理的根会话渲染为可展开分组行：主行显示主会话标题、工作区短名、`N 分片` / `M 个子代理` 徽标与整组主会话操作；展开后列出每个分片与子代理（标注 `子代理 · <nickname> (<role>)` 与「默认不进自动批次」提示），支持逐条勾选、插队整理、移出或看轨迹。

## 6. 真实验证方式

1. 自动化测试：`tests/test_distill_queue.py`、`tests/test_data_foundation.py`、`tests/test_arch_invariants.py` 全覆盖（Codex UUID 归一化去重、父子归属与自动认领隔离、显式 `queue_ids` 放行子代理、标题提取与无撞名、`exclude_content_prefixes` 与无头脚本拦截、`re_triage_queue` 干跑/应用、DDL 一致性）。
2. 真实运行验证：通过 FastAPI `TestClient` 对接临时真实 JSONL 文件回放 `sync → queue list → re-triage dry_run → re-triage apply → run claim` 完整链路，并运行 `python scripts/verify_repo.py --frontend`。
