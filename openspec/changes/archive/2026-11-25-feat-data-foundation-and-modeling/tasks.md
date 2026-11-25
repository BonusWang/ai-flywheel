# Tasks: feat-data-foundation-and-modeling

- [x] 1. 在 `backend/ai_flywheel/models.py` 新增 `QueueStatus`、`IngressChannel` 枚举及 `DictItem`、`DistillQueueItem` 模型，并在 `SessionRecord` 中补充主档扩展字段（含安全默认值以保持向后兼容）【test】
- [x] 2. 在 `backend/ai_flywheel/storage/db/schema.py` 与 `migrations.py` 实现 v4 幂等迁移：建立 `sys_dict_items` 与 `distill_queue` 表及索引、扩展 `sessions` 与 `audit_events` 新增列、自动备份 v3 库并植入全部内置系统字典种子【test】
- [x] 3. 新增 `backend/ai_flywheel/storage/db/dict_repo.py`（`DictRepoMixin`）与 `backend/ai_flywheel/storage/db/queue_repo.py`（`QueueRepoMixin`），增强 `audit_repo.py` 与 `session_repo.py` 支持新增列，并在 `storage/db/__init__.py` 组合导出【test】
- [x] 4. 在 `backend/ai_flywheel/routes/governance.py` 新增 `GET /api/site/dicts` 接口（支持 `dict_type` 筛选与分组返回），同步更新 `frontend/src/types.ts`、`frontend/src/api.ts`、`frontend/src/lib/labels.ts`【test】
- [x] 5. 编写并落仓权威建模资产 `docs/schema.sql` 与 `docs/data-model.md`（含完整表清单、Mermaid ER 图、内置字典清册与通用轨迹复用规范），并将 `.sql` 加入 `scripts/verify_repo.py` 的 `SCAN_SUFFIXES`【static】
- [x] 6. 新增 `tests/test_data_foundation.py`（覆盖空库初始化、v3→v4 升级迁移、字典幂等与自定义项保留、`distill_queue` 多 revision/分片与库存统计、`audit_events` 状态变迁兼容、`GET /api/site/dicts`）并在 `tests/test_arch_invariants.py` 新增 `docs/schema.sql` 与运行时表/列一致性及 `QueueStatus` 前后端对齐不变量测试【test】
- [x] 7. 执行真实冒烟验收（v3→v4 升级实测、真实 `serve` 启动核验 `/health` 与 `/api/site/dicts`、退出无报错）与 `python scripts/verify_repo.py --frontend` 全绿验证【real】
