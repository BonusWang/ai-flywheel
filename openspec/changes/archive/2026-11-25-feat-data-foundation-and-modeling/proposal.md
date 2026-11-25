# Proposal: 数据底座整理、统一码值字典、通用轨迹增强与沉淀队列建表

## Why

当前 AI Flywheel 的 SQLite 存储层经过 v1 → v2 → v3 演进后已累积 20 张业务/系统表与 1 张 FTS5 虚表，但存在四个基础设施层面的结构缺口，制约后续沉淀队列与多端归档能力的演进：

1. **缺少权威的建模脚本与 ER 资产**：全部 DDL 仅内嵌在 `storage/db/schema.py` 的字符串与 `migrations.py` 的代码迁移中，仓库内没有独立可读的 `docs/schema.sql` 与 `docs/data-model.md`，也缺少测试验证文档与运行时表结构一致。
2. **码值分散在前后端与硬编码字符串中**：客户端类型、入队通道、队列状态、排除原因、优先级、信号种类等码值散落在后端 `models.py` 枚举、`triage.py` 拼接文本与前端 `labels.ts`，每新增一种接入端或排除原因都需多处同步修改。
3. **通用轨迹表缺少结构化状态变迁字段**：现有 `audit_events` 已具备 `(entity_type, entity_id, seq)` 多态轨迹骨架，但缺少 `from_status`、`to_status`、`reason_code` 列，难以直接复用于队列、规则与批次的统一状态变迁查询，否则容易被迫为各业务另建重复的 `*_history` 表。
4. **会话主档与流水线队列库存混用**：`sessions` 表以 `session_key` 为主键同时承担主档与排队状态，无法 cleanly 表达同一会话“撤回后再归档（多 revision 增量）”、“超大上下文分段（chunk）”以及“独立重试计数与死信隔离（DLQ）”。

根因是**缺少显式的数据建模基线、统一码值字典表、结构化轨迹复用列以及独立的队列库存表**。本次变更先完成数据底座升级与跨层契约对齐，为下一阶段的沉淀队列状态机、双端归档触发与 Workflows 库存看板提供稳定地基（遵循 `storage/db/*` 热文件串行纪律）。

## What Changes

- **v4 幂等 SQLite 迁移与仓储层**（`storage/db/schema.py`、`storage/db/migrations.py`、`storage/db/dict_repo.py`、`storage/db/queue_repo.py`、`storage/db/audit_repo.py`、`storage/db/__init__.py`）：
  - 新增统一码值字典表 `sys_dict_items`（联合主键 `(dict_type, code)`），并在初始化/迁移时幂等植入标准系统字典（`queue_status`、`ingress_channel`、`exclude_reason`、`client_type`、`priority_level`、`run_status`、`rule_status`、`signal_kind`、`entity_type`），保留自定义非内置项不被覆盖。
  - 新增沉淀队列库存表 `distill_queue`（主键 `queue_id`，唯一约束 `(session_key, revision, chunk_index)`，带状态/优先级/客户端/会话/批次联合索引）。
  - 为 `sessions` 表幂等新增主档扩展列：`opt_out`、`opt_out_reason`、`latest_content_hash`、`latest_turn_count`、`last_consumed_turn`、`last_consumed_hash`。
  - 为通用轨迹表 `audit_events` 幂等新增结构化状态变迁列：`from_status`、`to_status`、`reason_code` 及事件类型索引；`append_audit_event` 向后兼容新增可选参数。
  - 新增 `DictRepoMixin`（字典读写与分组查询）和 `QueueRepoMixin`（队列项 CRUD、库存聚合统计、状态更新与轨迹联写），经 `FlywheelDB` 统一导出。
- **领域模型与只读字典 API**（`models.py`、`routes/governance.py`）：
  - 新增 `QueueStatus`、`IngressChannel`、`DictItem`、`DistillQueueItem` Pydantic 模型与枚举。
  - 新增 `GET /api/site/dicts`（支持可选 `dict_type` 筛选，返回分组与扁平字典项），作为前后端统一码值展示元数据来源。
- **前端类型与标签契约对齐**（`frontend/src/types.ts`、`frontend/src/api.ts`、`frontend/src/lib/labels.ts`）：
  - 对齐 `QueueStatus`、`IngressChannel`、`DistillQueueItem`、`DictItem` 与 `AuditEventRecord` 扩展字段；新增 `api.listDicts()`；在 `labels.ts` 补充 `QUEUE_STATUS_LABELS`、`INGRESS_CHANNEL_LABELS` 作为降级兜底。
- **权威建模文档落仓与不变量守护**（`docs/schema.sql`、`docs/data-model.md`、`scripts/verify_repo.py`、`tests/test_data_foundation.py`、`tests/test_arch_invariants.py`）：
  - 落仓完整标准 DDL 脚本 `docs/schema.sql` 与全库数据模型说明 `docs/data-model.md`（含 Mermaid ER 图、字典种子表与轨迹复用规范）。
  - 将 `.sql` 加入 `scripts/verify_repo.py` 的 `SCAN_SUFFIXES`，使 DDL 文件同样接受公开仓卫生扫描。
  - 新增 `tests/test_data_foundation.py` 覆盖空库建表、v3→v4 升级迁移、字典幂等植入、队列 CRUD/库存聚合与 `audit_events` 状态轨迹；在 `tests/test_arch_invariants.py` 新增不变量测试，校验 `docs/schema.sql` 与运行时 SQLite schema 表/列一致，以及前后端 `QueueStatus` 枚举对齐。

## Capabilities

### New Capabilities
- `data-modeling-foundation`：提供统一码值字典表、通用状态变迁轨迹增强、`distill_queue` 队列存储模型、`GET /api/site/dicts` 契约以及 `docs/schema.sql` 与运行时表结构的一致性守护。

### Modified Capabilities
- （无：本次变更不修改既有 P00–P07 流水线运行行为、审批门或既有 API 语义。）

## Impact

- 后端新增：`storage/db/dict_repo.py`、`storage/db/queue_repo.py`。
- 后端修改：`storage/db/schema.py`、`storage/db/migrations.py`、`storage/db/audit_repo.py`、`storage/db/session_repo.py`、`storage/db/__init__.py`、`models.py`、`routes/governance.py`。
- 前端修改：`frontend/src/types.ts`、`frontend/src/api.ts`、`frontend/src/lib/labels.ts`。
- 文档与脚本新增/修改：`docs/schema.sql`、`docs/data-model.md`、`scripts/verify_repo.py`。
- 测试新增/修改：`tests/test_data_foundation.py`、`tests/test_arch_invariants.py`。
- 向后兼容：所有新增表与列均通过幂等 v4 迁移添加，现有 v3 数据库无损升级，既有测试与 P00–P07 流程零破坏。
