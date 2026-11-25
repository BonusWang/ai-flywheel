# Design: 控制面治理交互、统一分页与数据对齐

## 关键设计决策与取舍

1. **向后兼容的后端列表扩展**
   - 备选 A：把 `/api/site/runs`、`/api/site/outcomes` 等响应结构改造成纯 `{items, total, page, page_size}`。会破坏既有前端与全部后端测试。
   - **选定方案 B**：保留全部现有键名（`runs`、`outcomes`、`observations`、`dissents`、`items`、`documents`、`skills`），同时放宽默认上限并支持可选查询参数（`limit` / `offset` / `q` / `status`），附加 `total` 元数据；前端使用统一 `ListControls`（`ListToolbar` + `PaginationBar`，默认 10 条/页）进行丝滑即时筛选与分页。
2. **「错题与分歧」四子视图架构**
   - `action`（待我处理，默认）：`observations` 中 `status=='observing'` 且（`severity_grade=='red_silent'` 或 `occurrence_count>=3`）+ `dissents` 中 `status=='open'`。Tab 角标精确等于此集合数量。
   - `observations`（错题观察池）：默认仅展示 `observing`（可切换全部/已升级/已归档），顶部提示系统自动“事不过三”，支持批量归档。
   - `dissents`（规则分歧）：全量展示 `GET /api/site/dissents`，带关联规则标题与状态，支持单条与批量标记“已吸收（merged）/ 忽略（dismissed）”。
   - `retros`（实时异常线索）：默认仅展示 `open`/`in_review`（可切换含历史已处理），前端对 `(kind, client, summary)` 相同的信号自动分组聚合为单行并展示 `×N`，点击“已处理/忽略”或“批量忽略”直接通过 `POST /api/site/retro/bulk-status` 一次性处理整组 `retro_ids`。
3. **Skill 合并与 Knowledge 过期卡补齐**
   - `GET /api/site/skills` 以 `db.list_skill_assets()` 为基础，按 `skill_name` 合并 `activation.probe_local_skills()` 中尚未入库的本地 Skill（`origin="local_probed"`, `invoke_count=0`, `health_state="untested"`），零数据库写副作用。
   - `GET /api/site/knowledge` 将 `freshness_map` 中未出现在最新 100 篇文档里的所有 `is_expired=True` 卡片（以及已有曝光/助益记录的卡片）一并补全并返回 `expired_total` 与 `total`，保证与 `growth-metrics.live.expired_card_count` 完全一致。
4. **真实验证策略**
   - 单元测试覆盖所有新 API、批量接口、蒸馏自动归档、Skill 探测合并、过期卡补全。
   - `verify_repo.py --frontend` 全量验收。
   - 重启本地 `flywheel serve`（加载最新代码与 `frontend/dist` 及 `ai-home/flywheel-profile/site.yaml`），使用真实浏览器访问 `/rules`、`/workflows`、`/automations`、`/about` 并截图人工核验分页、搜索、子视图切换、Skill 清单、过期卡计数与品牌名称。
