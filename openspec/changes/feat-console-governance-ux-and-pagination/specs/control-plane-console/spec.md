# 控制面控制台治理交互、分页查询与数据对齐

## ADDED Requirements

### Requirement: 错题与分歧页按需人工拍板与系统自动处理分层展示
控制面 `/rules` 的「错题与分歧」视图 SHALL 拆分为四个独立子视图：`待我处理`、`错题观察池`、`规则分歧`、`实时异常线索`，并且 Tab 角标 SHALL 仅统计真正需要维护者拍板的 `待我处理` 条目数（`red_silent` 观察中错题 + `occurrence_count >= 3` 待晋升错题 + `open` 规则分歧）。

#### Scenario: 打开错题与分歧默认聚焦待我处理
- **WHEN** 维护者进入 `/rules` 并切换到「错题与分歧」Tab
- **THEN** 页面默认展示 `待我处理` 子视图，Tab 角标仅等于待我处理条目总数，且每条默认展示 1–2 行关键摘要，详细根因与原文可通过点击展开。

#### Scenario: 规则分歧集中展示并可批量收敛
- **WHEN** 后端存在未处理的 `dissent_records` 并请求 `GET /api/site/dissents`
- **THEN** 响应 MUST 附带关联规则标题与状态（`target_title`、`target_status`），前端 `规则分歧` 子视图可直接按单条或批量将其标记为已吸收或忽略。

#### Scenario: 实时异常线索同类聚合与批量清理
- **WHEN** `retro_queue` 中存在多条同类型、同客户端、同摘要的重复未处理信号
- **THEN** 前端 `实时异常线索` 默认隐藏历史已处理项、将重复信号折叠展示重复次数，并支持调用 `POST /api/site/retro/bulk-status` 批量标记处理或忽略。

### Requirement: 全站轨迹与治理列表统一支持创建时间倒序、检索筛选与分页
控制面所有轨迹与治理列表（`/workflows` 批次、`/automations` 执行历史、`/rules` 避坑规则/错题与分歧/Skill 与保鲜/证据台账）SHALL 默认按创建时间倒序排列，提供关键词检索与状态筛选，并默认按每页 10 条分页（支持 10/20/50 切换）。

#### Scenario: 批次轨迹超过 10 条时按每页 10 条分页并支持筛选
- **WHEN** `/workflows` 加载超过 10 个批次记录
- **THEN** 列表默认按创建时间倒序仅展示第 1 页的 10 条记录，并提供翻页控件、状态/触发方式筛选与关键词查询；后端 `GET /api/site/runs` MUST 返回足额历史与 `total` 总数。

### Requirement: Skill 实效账本与经验卡保鲜口径与探测/健康卡一致
`GET /api/site/skills` SHALL 合并数据库实效账本与 `capabilities.skills_dirs` 本地探测到的现役 Skill；`GET /api/site/knowledge` SHALL 合并 `freshness_map` 中的全部过期/受管卡片，使其待复核数与 `growth-metrics.live.expired_card_count` 一致。

#### Scenario: 配置本地 skills_dirs 后即使尚无回执也展示本地 Skill 清单
- **WHEN** `capabilities.skills_dirs` 包含本地 Skill 目录且 `skill_assets` 表为空
- **THEN** `GET /api/site/skills` MUST 返回所有本地探测到的 Skill（标注未实测/本地探测来源与 0 次调用），不再返回空列表。

#### Scenario: 过期经验卡不在最新 100 篇文档内时仍完整计入并展示
- **WHEN** 挂载知识库超过 100 篇文档且存在位于 100 篇之后的过期经验卡
- **THEN** `GET /api/site/knowledge` MUST 包含所有 `is_expired=True` 的卡片，前端「经验卡新鲜度保鲜」待复核计数与顶部健康卡一致。

### Requirement: 周五蒸馏自动归档超期低危单次观察错题
`core/dream.py` 的周五蒸馏 Tier 1 SHALL 自动归档超过 30 天且仅出现 1 次的非高危（`yellow_caught` / `green_minor`）观察中错题，并在只读预览中提前透出候选清单。

#### Scenario: 低危单次错题超 30 天未复发被蒸馏自动归档
- **WHEN** 一条 `yellow_caught` 且 `occurrence_count=1` 的观察中错题更新时间早于 30 天前，执行 `run_dream_consolidation`
- **THEN** 该错题状态变为 `archived` 并写入审计事件，而 `red_silent` 错题 MUST 保持 `observing` 不被自动归档。

### Requirement: 关于页保持中立定位并精简页内 llms.txt 预览区块
控制面 `/about` 页面 SHALL 不在 Hero 或页脚点名外部竞品上下文数据库名称，且 SHALL 移除页内 `/llms.txt` 协议介绍与折叠原文面板（仅保留页脚与侧边栏底部的小链接）。

#### Scenario: 访问关于页不出现外部竞品点名与页内 llms.txt 原文面板
- **WHEN** 用户打开 `/about` 页面
- **THEN** 页面仅展示 AI Flywheel 自身定位、核心指标、MCP 接入端点与工具清单，不出现页内 `/llms.txt` 折叠原文区或外部产品点名，页脚仍保留 `/llms.txt` 轻量链接。
