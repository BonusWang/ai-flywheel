# Tasks: fix-retro-signal-closure-semantics

- [x] 1. 在 `storage/db/queue_repo.py` 新增 `list_active_queue_session_keys()`，并在 `routes/governance.py` 的 `GET /api/site/retro` 附带 `has_queued_session` 与 `settlement_mode`【test】
- [x] 2. 更新 `core/retro_advisory.py`，将 `verdict="keep"` 的 `recommended_action` 改为 `"promote"` 并修正建议说明文案【test】
- [x] 3. 在 `routes/governance.py` 新增 `POST /api/site/retro/promote`（写入 `mistake_observations`、将线索置 `resolved`、写审计并刷新核心索引卡），同步 `tests/test_approval_channel_abstraction.py` 路由断言【test】
- [x] 4. 更新前端 `types.ts`、`api.ts` 与 `CluesObservationsTab.tsx`：展示「随下次沉淀自动结算 / 无关联待审会话」徽标，明确区分「转入错题观察池」「已人工处理（不再送入下次沉淀）」「忽略」三类操作与批量转错题【test】
- [x] 5. 补充自动化测试（`tests/test_console_governance_ux.py` 等）并跑通 `python scripts/verify_repo.py --frontend` 全门禁及真实 API 回放验收【real】
