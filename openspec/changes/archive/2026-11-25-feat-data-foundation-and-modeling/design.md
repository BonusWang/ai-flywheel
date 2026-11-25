# Design: 数据底座与统一建模契约

## 上下文与设计决策

### 1. 为什么独立建 `distill_queue` 而不扩展 `sessions.status`？
- **备选方案 A**：直接把 `sessions.status` 扩展为 6 个状态，把分片和重试放进 `metadata_json`。
  - *否决原因*：一个 `session_key` 只能有一行，已沉淀后再次续聊归档会覆盖历史沉淀状态；超大上下文拆片无法成为独立排队单元；SQL 统计库存与失败重试无法建高效索引。
- **采用方案 B**：`sessions` 作为**实体主档**（存身份、`opt_out`、最新内容指纹与已消费水位线），`distill_queue` 作为**队列库存表**（以 `(session_key, revision, chunk_index)` 唯一约束支持增量重入队与分片）。同时完全保留 `sessions.status` 原有 4 态以维持既有单测与旧路径兼容。

### 2. 为什么统一字典表与 Python Enum 并存？
- **备选方案 A**：彻底删除 Python Enum，全部改为字符串查字典表。
  - *否决原因*：失去静态类型检查与状态机合法性边界。
- **采用方案 B**：核心状态机枚举（`QueueStatus`、`IngressChannel`、`RunStatus`、`RuleStatus`、`SignalKind`）继续作为代码层执行契约；`sys_dict_items` 集中托管全部展示标签（中英文）、分组、排序、颜色/图标元数据以及可动态扩展的分类项（如新 AI 客户端、新排除原因）。内置项带 `is_builtin = 1`，启动时 `INSERT OR IGNORE` / 仅刷新内置元数据，绝不覆盖 `is_builtin = 0` 的用户自定义项。

### 3. 为什么复用 `audit_events` 而不新建 `queue_history` / `entity_states`？
- `audit_events` 已有 `(entity_type, entity_id, seq DESC)` 索引与 JSON 载荷，补齐 `from_status`、`to_status`、`reason_code` 三列即可原生承接所有实体的状态变迁轨迹，避免出现多张结构相似的流水表。
- 当前状态保留在各自业务主表（如 `distill_queue.status`），确保列表筛选与认领查询走单表联合索引，不引入通用状态表的额外 JOIN 开销。

### 4. 如何防止 `docs/schema.sql` 与运行时代码漂移？
- `docs/schema.sql` 维护完整的最终表结构（v4 完整状态，不含历史兼容补丁脚本），在 `tests/test_arch_invariants.py` 中分别用 `sqlite3.executescript(docs/schema.sql)` 和 `FlywheelDB(tmp_db)` 建库，通过 `sqlite_master` 与 `PRAGMA table_info` 逐表逐列比对（排除 SQLite 内置 `sqlite_*` 与 FTS5 内部影子表 `knowledge_fts_*`）。任何改了 `schema.py`/`migrations.py` 却忘了同步 `docs/schema.sql` 的改动都会立即让不变量测试失败。

## 迁移与回滚策略
- v4 迁移 `_migrate_to_v4` 在 `schema_migrations` 中记录 `version = 4`；若检测到存量数据库，先备份为 `<db>.bak-v3`。
- 全部采用幂等 `CREATE TABLE IF NOT EXISTS`、`CREATE INDEX IF NOT EXISTS`、`_ensure_column` 与 `INSERT OR IGNORE` 种子写入，可重复运行零副作用。

## 真实验证方式
1. 在临时目录创建模拟 v3 数据库（写入存量 `sessions`、`rules`、`audit_events`），由新代码初始化验证无损升级至 v4 且备份生成。
2. 以 `process_role="serve"` 启动真实 FastAPI 服务，实测 `GET /health`、`GET /api/site/dicts`、`GET /api/site/dicts?dict_type=queue_status` 返回正确 JSON 与状态码。
3. 执行 `python scripts/verify_repo.py --frontend`（卫生含 `.sql` → openspec → pytest → 前端构建）全绿。
