# Proposal: 控制面治理交互重构、全站统一查询分页、Skill 与保鲜修复及关于页精简

## Why

在真实使用 `5+1` 控制台管理规则、错题、分歧、运行批次与知识资产时，暴露出五类直接影响可用性与可信度的前后端缺陷：

1. **「错题与分歧」概念错位与噪音过载**：Tab 角标实为 `观察中错题 + 待查看实时异常线索`，真正的规则分歧（`dissent_records`）未在该 Tab 展示；普通观察错题与运行时 Hook 异常线索全量平铺并挂载高存在感按钮，且缺少同类聚合、历史隐藏与批量清理，导致长屏密排、难以分辨“需人工拍板 vs 系统自动处理”。
2. **全站轨迹与长列表缺少统一分页、查询与排序保障**：`/workflows`（沉淀与审批批次）、`/automations`（执行历史）、`/rules`（避坑规则、错题与分歧、Skill 与保鲜、证据台账）均直接全量平铺，批次或台账增多后页面无限拉长，缺少默认创建时间倒序、关键词检索、状态筛选与每页 10 条分页。
3. **「Skill 与保鲜」数据断层与口径矛盾**：`GET /api/site/skills` 未合并 `capabilities.skills_dirs` 探测到的本地 Skill，已配置本地目录仍显示空表；`GET /api/site/knowledge` 仅截断 mtime 最新 100 篇文档，导致过期经验卡计数与顶部健康卡（`expired_card_count`）严重矛盾。
4. **「关于」页存在外部产品点名与冗余协议预览**：Hero 与页脚硬编码对比外部竞品名，页面中部包含对人类读者意义不明的 `/llms.txt` 折叠原文面板。
5. **缺少低危超期错题自动归档与批量收口接口**：观察池与异常线索队列缺少后端批量操作与周五蒸馏自动减法，长期运行必然熵增。

## What Changes

- **后端治理接口与蒸馏增强**：
  - 扩充 `GET /api/site/runs`、`GET /api/site/outcomes`、`GET /api/site/observations`、`GET /api/site/dissents`、`GET /api/site/retro`、`GET /api/site/knowledge`、`GET /api/site/skills`：默认按创建时间倒序，返回完整/足额数据及总数元数据（保持既有 JSON 字段 100% 向后兼容）。
  - `GET /api/site/dissents` 补充关联规则标题与状态（`target_title`、`target_status`），并新增批量更新接口 `POST /api/site/dissents/bulk-status`。
  - 新增错题批量归档 `POST /api/site/observations/bulk-archive` 与实时异常线索批量更新 `POST /api/site/retro/bulk-status`；`GET /api/site/retro` 返回同类重复信号的聚合视图元数据或支持按 `(kind, client, summary)` 聚合收口。
  - `core/dream.py` 周五蒸馏 Tier 1 新增自动归档超过 30 天且仅出现 1 次的低危（`green_minor` / `yellow_caught`）观察中错题（同时在 `preview_dream_consolidation()` 中只读透出）。
  - `GET /api/site/skills` 合并 `db.list_skill_assets()` 与 `activation.probe_local_skills()`，使配置了 `skills_dirs` 的本地技能立即可见并标注实测状态。
  - `GET /api/site/knowledge` 合并 `db.get_knowledge_freshness_map()` 中的全部受管/过期卡与文档列表，保证过期计数与 `growth-metrics.live.expired_card_count` 严格一致。
- **前端统一组件与页面重构**：
  - 新增通用组件 `PaginationBar.tsx`（默认 10 条/页，支持 10/20/50 切换）与 `ListToolbar.tsx`（关键词搜索 + 筛选 + 默认创建时间倒序标识）。
  - 重构 `RulesRetroPage.tsx` 与 `CluesObservationsTab.tsx`：二级拆分为「待我处理（默认，Tab 角标仅计此数）/ 错题观察池 / 规则分歧 / 实时异常线索」，每条默认 1–2 行紧凑摘要 + 点击展开详情 + 10 条分页 + 批量处理 + 重复异常同类折叠。
  - 在 `WorkflowsPage.tsx`、`AutomationsPage.tsx`、`RulesListTab.tsx`、`AssetsFreshnessTab.tsx`、`OutcomesLedgerTab.tsx` 全面接入查询、筛选、创建时间倒序与默认 10 条分页。
  - 精简 `AboutPage.tsx`：移除外部产品点名，移除页内 `/llms.txt` 入口与折叠原文面板（保留底部小链接与侧边栏链接）。

## Capabilities

### Modified Capabilities
- `control-plane-console`: 升级规则治理子视图、全站列表分页检索、Skill/保鲜真源对齐与关于页精简。

## Impact

- 后端：`backend/ai_flywheel/storage/db/rule_repo.py`、`session_repo.py`、`workflow_repo.py`、`routes/governance.py`、`routes/runs.py`、`core/dream.py`。
- 前端：`frontend/src/components/ListControls.tsx`、`frontend/src/components/rules/*`、`frontend/src/pages/RulesRetroPage.tsx`、`WorkflowsPage.tsx`、`AutomationsPage.tsx`、`AboutPage.tsx`、`api.ts`、`types.ts`。
- 测试：`tests/test_console_governance_ux.py`。
