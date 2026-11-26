# data-modeling-foundation Specification Delta

## ADDED Requirements

### Requirement: 沉淀队列父子会话归属列与增量迁移
系统 SHALL 在 `distill_queue` 表维护 `parent_session_key`（默认空字符串）与 `is_subagent`（默认 `0`）列及父会话索引 `idx_distill_queue_parent`，并通过无条件幂等迁移覆盖已有 v4 存量数据库，保持运行时表结构与权威 DDL `docs/schema.sql` 1:1 一致。

#### Scenario: 全新库与既有 v4 存量库均自动补齐父子归属列并与权威 DDL 一致
- **WHEN** 在全新数据库或已有 v4 旧库（`schema_migrations` 已含版本 `4` 但缺少新列）上初始化 `FlywheelDB`
- **THEN** `distill_queue` 必须包含 `parent_session_key` 与 `is_subagent` 列及索引 `idx_distill_queue_parent`，且逐表列集合与 `docs/schema.sql` 完全一致
