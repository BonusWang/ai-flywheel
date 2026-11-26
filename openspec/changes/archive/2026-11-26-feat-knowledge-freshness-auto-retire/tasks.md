# Tasks: feat-knowledge-freshness-auto-retire

- [x] 1. 在 `adapters/knowledge.py` 的 `sync_fts_index()` 同步 Wiki frontmatter 的 `status: archived / superseded`，并保护已归档台账不被未更新的源文件覆写回 `fresh`【test】
- [x] 2. 在 `storage/db/knowledge_repo.py` 新增 `list_stale_zero_help_cards`、`auto_archive_stale_zero_help_cards` 与 `update_knowledge_freshness_action`【test】
- [x] 3. 在 `core/dream.py` 将「超期 ≥30 天且零助益卡片自动归档（仅改台账）」纳入 `preview_dream_consolidation` 与 `run_dream_consolidation` Tier 1 并写审计【test】
- [x] 4. 在 `routes/governance.py` 新增 `POST /api/site/knowledge/freshness` 纯台账复核接口（支持 `verify` / `archive` / `restore` / `set_tier` + 审计 + 核心索引卡刷新）【test】
- [x] 5. 更新前端 `types.ts`、`api.ts`、`DocumentReaderModal.tsx`、`AssetsFreshnessTab.tsx`、`RulesRetroPage.tsx` 与 `AutomationsPage.tsx`，补齐「已归档」筛选、弹窗纯台账复核工具条（含不改源文件说明）与蒸馏退场展示【test】
- [x] 6. 补充自动化测试（`tests/test_console_governance_ux.py`、`tests/test_wiki_gate_and_metrics.py`）并通过 `python scripts/verify_repo.py --frontend` 全门禁与真实 API 回放验收【real】
