# Tasks: fix-queue-session-identity-and-grouping

- [x] 1. 在 `distill_queue` 增加 `parent_session_key` 与 `is_subagent` 列、幂等迁移、索引及权威 DDL 同步（`schema.py`、`migrations.py`、`queue_repo.py`、`models.py`、`docs/schema.sql`、`docs/data-model.md`）【test】
- [x] 2. 在 `config.py` 与 `flywheel.yaml.example` 的 `sessions.triage` 增加 `exclude_content_prefixes` 配置项【static】
- [x] 3. 实现 JSONL 元数据提取（Codex `session_meta` / Claude Code `custom-title` 与 `isSidechain`）、可读标题生成及无头脚本过滤逻辑（`core/triage.py`、`adapters/sessions.py`）【test】
- [x] 4. 更新 `DistillQueueService`（`core/queue_service.py`）与 `queue_repo.py`：Codex UUID 归一化去重、父子归属持久化、自动批次默认跳过子代理（显式 `queue_ids` 可认领）、`re_triage_queue` 干跑零写入与存量标题/父子/重复 `SUPERSEDED` 清洗【test】
- [x] 5. 更新前端类型与队列看板（`frontend/src/types.ts`、`frontend/src/components/workflows/QueueInventoryPanel.tsx`）：展示工作区短名（悬停全路径）、按根会话树形折叠分片与子代理、支持展开查看与单选插队/整组操作及重筛预览计数【test】
- [x] 6. 补充单元测试与跨层回归（`tests/test_distill_queue.py`、`tests/test_data_foundation.py`）并跑通 `python scripts/verify_repo.py --frontend` 全门禁及真实 API 回放验收【real】
