# Tasks

- [x] 1.1 后端 DB 仓储扩展：支持错题批量归档、超期低危单次错题自动归档、分歧批量更新、异常线索批量更新，以及放宽列表默认查询上限与 `total` 统计【test】
- [x] 1.2 后端路由增强（`routes/governance.py`、`routes/runs.py`）：`GET /api/site/dissents` 附带规则标题与状态；新增 `POST /api/site/observations/bulk-archive`、`POST /api/site/dissents/bulk-status`、`POST /api/site/retro/bulk-status`；`GET /api/site/skills` 合并本地探测 Skill；`GET /api/site/knowledge` 完整纳入 `freshness_map` 过期卡；`GET /api/site/runs` / `outcomes` / `observations` / `retro` 返回 `total` 与默认倒序【test】
- [x] 1.3 周五蒸馏增强（`core/dream.py`）：Tier 1 自动归档超 30 天低危单次观察错题并在 `preview_dream_consolidation()` 透出候选【test】
- [x] 2.1 前端新增通用列表控件 `frontend/src/components/ListControls.tsx`（`ListToolbar` 搜索/筛选 + `PaginationBar` 默认 10 条/页支持 10/20/50 切换）【static】
- [x] 2.2 前端重构 `RulesRetroPage.tsx` 与 `CluesObservationsTab.tsx`：四子视图（待我处理/错题观察池/规则分歧/实时异常线索）、角标仅计待我处理、1–2 行紧凑卡片点击展开详情、重复线索聚合与批量处理【real】
- [x] 2.3 前端全站列表接入查询、筛选、默认创建时间倒序与 10 条分页：`WorkflowsPage.tsx`、`AutomationsPage.tsx`、`RulesListTab.tsx`、`AssetsFreshnessTab.tsx`、`OutcomesLedgerTab.tsx`【real】
- [x] 2.4 前端精简 `AboutPage.tsx`：移除外部产品点名与页内 `/llms.txt` 入口及折叠原文面板（仅保留底部小链接）【real】
- [x] 3.1 补齐自动化测试 `tests/test_console_governance_ux.py` 并运行 `python scripts/verify_repo.py --frontend` 全绿【test】
- [x] 3.2 本地服务重启与端到端浏览器仿真验证（实测 `/rules`、`/workflows`、`/automations`、`/about` 页面渲染、交互与 API 数据一致性）【real】
