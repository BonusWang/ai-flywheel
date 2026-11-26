# data-modeling-foundation Specification

## Purpose
TBD - created by archiving change feat-data-foundation-and-modeling. Update Purpose after archive.
## Requirements
### Requirement: 统一码值字典表与内置系统码值种子
系统 SHALL 在 SQLite 中维护统一码值字典表 `sys_dict_items`（联合主键 `(dict_type, code)`），用于集中托管可扩展分类码值与前后端展示元数据，并在数据库初始化与迁移时幂等植入全部内置系统字典项。

#### Scenario: 空库初始化自动植入完整内置字典
- **WHEN** 系统在空数据库上首次初始化 `FlywheelDB`
- **THEN** `sys_dict_items` 表必须存在，且包含 `queue_status`、`ingress_channel`、`exclude_reason`、`client_type`、`priority_level`、`run_status`、`rule_status`、`signal_kind`、`entity_type` 九类内置字典项
- **AND** `QueueStatus`、`IngressChannel`、`RunStatus`、`RuleStatus`、`SignalKind` 的每一个枚举值都必须在 `sys_dict_items` 中有对应启用记录

#### Scenario: 重复初始化保持自定义扩展字典不被覆盖
- **WHEN** 维护者或运行时向 `sys_dict_items` 写入一条 `is_builtin = 0` 的自定义字典项（如新增一种客户端类型或排除原因），随后重启并再次执行 `_init_schema()`
- **THEN** 该自定义字典项的 `label_zh`、`enabled` 与 `meta_json` 必须完整保留，不得被删除或覆盖

### Requirement: 通用轨迹表 `audit_events` 支持结构化状态变迁复用
系统 SHALL 复用现有 `audit_events` 表作为全系统唯一通用状态轨迹表，新增 `from_status`、`to_status`、`reason_code` 三个默认为空字符串的列及事件类型查询索引，且保持既有调用完全向后兼容。

#### Scenario: 写入与查询带状态变迁的队列轨迹
- **WHEN** 调用 `db.append_audit_event(entity_type="queue_item", entity_id="q-1", event_type="status_changed", from_status="queued", to_status="withdrawn", reason_code="USER_WITHDRAW", actor="mcp")`
- **THEN** `db.list_audit_events(entity_type="queue_item", entity_id="q-1")` 必须返回包含 `from_status == "queued"`、`to_status == "withdrawn"`、`reason_code == "USER_WITHDRAW"` 的记录

#### Scenario: 既有未传状态参数的调用零兼容破坏
- **WHEN** 既有代码以旧签名调用 `db.append_audit_event(entity_type="workflow_run", entity_id="run-1", event_type="run_started", summary="...")`
- **THEN** 写入必须成功，且 `from_status`、`to_status`、`reason_code` 自动落为空字符串 `""`

### Requirement: 沉淀队列库存表与会话主档扩展列
系统 SHALL 建立独立的 `distill_queue` 表记录每一次入队沉淀工作项，并在 `sessions` 主档表扩展 `opt_out`、内容指纹与消费水位线列，使会话主档与多 revision / 分片队列任务解耦。

#### Scenario: 同一会话支持多 revision 增量项与超大上下文分片项
- **WHEN** 同一 `session_key` 先写入 `revision=1, chunk_index=1, chunk_total=1` 且状态为 `processed`，随后写入 `revision=2, chunk_index=1, chunk_total=2` 与 `revision=2, chunk_index=2, chunk_total=2` 且状态为 `queued`
- **THEN** 三条记录必须均可共存于 `distill_queue`，按 `(session_key, revision, chunk_index)` 唯一约束去重
- **AND** 库存聚合查询 `summarize_queue()` 必须准确统计各状态计数及按 `client` 分组计数

#### Scenario: v3 存量数据库无损迁移至 v4
- **WHEN** 已有 v3 结构的 SQLite 数据库在升级后首次由 `FlywheelDB` 打开
- **THEN** 迁移必须自动创建 v3 备份、幂等建立 `sys_dict_items` 与 `distill_queue`、补齐 `sessions` 与 `audit_events` 新增列，并在 `schema_migrations` 记录版本 `4`
- **AND** 原有 `sessions`、`rules`、`workflow_runs`、`audit_events` 中的存量数据必须完整保留

### Requirement: 统一字典只读查询接口与前后端类型对齐
系统 SHALL 提供 `GET /api/site/dicts` 接口向控制台与调用方暴露启用中的统一码值字典，并保证前端 `types.ts` 中的 `QueueStatus` 联合与后端枚举逐值一致。

#### Scenario: 按类型筛选与全量拉取统一字典
- **WHEN** 客户端请求 `GET /api/site/dicts` 或 `GET /api/site/dicts?dict_type=queue_status`
- **THEN** 接口必须返回 HTTP 200，包含按 `dict_type` 分组的 `dicts` 映射与扁平 `items` 列表，并按 `sort_order ASC` 排序

### Requirement: 权威建模脚本落仓与运行时 Schema 一致性不变量
系统 SHALL 在仓库中维护权威 DDL 脚本 `docs/schema.sql` 与数据模型说明 `docs/data-model.md`，并将其纳入公开仓卫生扫描与自动化架构不变量测试。

#### Scenario: 权威 DDL 与运行时 SQLite 表结构严格一致
- **WHEN** 运行 `tests/test_arch_invariants.py` 中的 Schema 一致性测试
- **THEN** 将 `docs/schema.sql` 执行到独立内存/临时 SQLite 数据库后得到的业务表集合及每张表的列集合，必须与运行时 `FlywheelDB` 初始化得到的表集合及列集合完全一致
- **AND** `scripts/verify_repo.py` 的卫生扫描必须覆盖 `.sql` 文件后缀

### Requirement: 沉淀队列父子会话归属列与增量迁移
系统 SHALL 在 `distill_queue` 表维护 `parent_session_key`（默认空字符串）与 `is_subagent`（默认 `0`）列及父会话索引 `idx_distill_queue_parent`，并通过无条件幂等迁移覆盖已有 v4 存量数据库，保持运行时表结构与权威 DDL `docs/schema.sql` 1:1 一致。

#### Scenario: 全新库与既有 v4 存量库均自动补齐父子归属列并与权威 DDL 一致
- **WHEN** 在全新数据库或已有 v4 旧库（`schema_migrations` 已含版本 `4` 但缺少新列）上初始化 `FlywheelDB`
- **THEN** `distill_queue` 必须包含 `parent_session_key` 与 `is_subagent` 列及索引 `idx_distill_queue_parent`，且逐表列集合与 `docs/schema.sql` 完全一致

