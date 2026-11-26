# control-plane-console Specification

## Purpose
TBD - created by archiving change feat-control-plane-console-views. Update Purpose after archive.
## Requirements
### Requirement: Skill 实效账本只读查询接口
系统 SHALL 提供 `GET /api/site/skills` 接口，返回已登记 Skill 资产的实效账本（`skills` 列表，包含 `skill_name`、`origin`、`invoke_count`、`verified_pass_count`、`fail_count`、`health_state`、`last_used_at`、`version`）与当前本地探测到的 Skill 总数（`probed_count`），供控制面识别高实效 Skill、连续失败 Skill 与长期零调用的闲置 Skill。

#### Scenario: 查询 Skill 实效账本
- **WHEN** 客户端请求 `GET /api/site/skills`
- **THEN** 返回 HTTP 200 及 `skills` 数组与非负整数 `probed_count`，且不修改任何数据库记录或磁盘文件

### Requirement: 知识文档列表融合新鲜度与助益指标
系统 SHALL 在 `GET /api/site/knowledge` 返回的每篇文档摘要中保留原有字段（`doc_path`、`title`、`size_bytes`、`excerpt`），并合并对应的新鲜度与助益字段（`freshness_tier`、`valid_until`、`freshness_state`、`is_expired`、`surface_count`、`helped_count`），使控制面可直接识别过期待复核的经验卡。

#### Scenario: 知识列表返回过期与保鲜状态
- **WHEN** 某知识文档在 `knowledge_card_metrics` 中已超过 `valid_until` 或状态为 `needs_recheck`，客户端请求 `GET /api/site/knowledge`
- **THEN** 返回 HTTP 200，该文档条目包含 `is_expired: true` 与对应 `freshness_tier`、`valid_until`、`helped_count`

### Requirement: 分层蒸馏只读预览与手动执行接口
系统 SHALL 提供 `GET /api/site/dream` 与 `POST /api/site/dream/run` 接口：
- `GET /api/site/dream` MUST 严格只读，返回最近一次 `dream_completed` 审计记录（如有）与当前只读预览结果（候选休眠规则、超期经验卡、规则合并候选、Skill 实效告警、未决分歧数与 `proposals` 建议清单），不得修改任何规则状态、经验卡状态或写入审计/快照；
- `POST /api/site/dream/run` 接受 `{ "dry_run": boolean }`：当 `dry_run` 为 `true` 时 MUST 执行只读预览且零副作用；当 `dry_run` 为 `false` 时 SHALL 执行 `run_dream_consolidation` 并刷新派生核心索引卡，返回本次蒸馏结果与审计摘要。

#### Scenario: 只读预览蒸馏不产生数据库写入
- **WHEN** 存在满足休眠条件的零命中试运行规则时，客户端调用 `GET /api/site/dream` 或 `POST /api/site/dream/run`（`dry_run: true`）
- **THEN** 返回 HTTP 200 与 `dry_run: true` 及待休眠规则候选清单，但该规则在数据库中的状态仍保持 `unverified_active` 且不新增 `dream_completed` 审计事件

#### Scenario: 手动执行蒸馏落盘并刷新核心索引卡
- **WHEN** 客户端调用 `POST /api/site/dream/run`（`dry_run: false`）
- **THEN** 返回 HTTP 200 与 `dry_run: false`，执行 Tier 1 休眠与过期标记、保存当周指标快照、记录 `dream_completed` 审计事件并刷新核心索引卡

### Requirement: 前端控制面首页与治理资产视图
Web 控制台 SHALL 保持 `5+1` 极简导航骨架并将核心视图对齐治理与进化控制面心智：
- 一级导航名称为「今日态势 / 沉淀与审批 / 自动化调度 / 规则与进化 / 接入与设置」+「关于」；
- 首页「今日态势」MUST 展示真实证据通过率、预检激活数、规则健康度、错题观察池状态及由 `/api/site/automations` 提供的实时调度与安全阀状态，不得硬编码调度时间文案；
- 「规则与进化」页面 SHALL 提供避坑规则、错题与分歧、Skill 与经验保鲜、证据台账（Outcomes）、执行规矩五个视图以及核心索引卡（`FLYWHEEL-CORE.md`）预览与一键刷新入口；
- 「沉淀与审批」页面展开批次时 SHALL 展示 P00–P03 噪音过滤漏斗（存在初筛数据时）及 P02/P03 模型调用遥测摘要（存在 `call_telemetry` 时）；
- 「自动化调度」页面 SHALL 提供周五分层蒸馏控制卡，支持只读预览建议与手动触发执行；「接入与设置」页面移除与「自动化调度」重复的调度开关，改为状态提示与跳转入口。

#### Scenario: 控制台展示动态调度与治理资产
- **WHEN** 用户访问控制面首页与各治理页面
- **THEN** 首页调度信息与 `/api/site/automations` 实时一致；「规则与进化」可查看 Skill 实效账本、过期经验卡、证据台账与核心索引卡；「自动化调度」可预览并执行蒸馏

### Requirement: 前端页面模块化拆分与行为保持
Web 控制台 SHALL 将「沉淀与审批」与「规则与进化」两个页面拆分为职责单一的展示组件，页面容器只承担数据加载、状态编排与事件处理：
- 「沉淀与审批」页面的审批抽屉、节点详情、手工补录表单、批次状态徽标、噪音过滤漏斗与批次流水线行 MUST 各自成独立组件；
- 「规则与进化」页面的避坑规则、错题与分歧、Skill 与保鲜、证据台账、执行规矩五个视图以及核心索引卡弹窗 MUST 各自成独立组件；
- 核心索引卡弹窗 MUST 由「规则与进化」与「今日态势」两个页面共用同一实现，不得各维护一份；
- 拆分 MUST 保持既有行为不变：噪音过滤漏斗、审批选择按计划写入数量重建（`initCount`）、SSE 事件触发刷新、飞书审批状态文案、`stages`/`digest`/`sessions`/`events` 四个详情页签、五个治理页签、规则筛选与状态迁移、分歧新增/吸收/忽略、错题升级/归档、实时线索处理、策略保存/恢复默认、核心索引卡预览与重建 MUST 全部可用；
- 拆分后页面容器 MUST 保持在可读规模内（「沉淀与审批」< 400 行、「规则与进化」< 300 行），且不改变任何后端接口、请求参数或数据契约。

#### Scenario: 拆分后批次详情与审批行为保持
- **WHEN** 用户在「沉淀与审批」页展开一个待审批批次，依次查看噪音过滤漏斗、八个阶段节点与模型调用遥测，并勾选/取消计划写入后点击「同意并写入」或「取消本次写入」
- **THEN** 页面展示与拆分前一致；审批请求携带的条目索引与拆分前一致；批次状态按原契约刷新

#### Scenario: 治理页签与操作保持
- **WHEN** 用户在「规则与进化」页依次切换五个页签，并执行规则状态迁移、错题升级、分歧记录、策略保存与核心索引卡重建
- **THEN** 每个页签渲染对应内容，操作照常调用既有后端接口、刷新数据并给出与拆分前一致的提示文案

#### Scenario: 核心索引卡弹窗两页共用
- **WHEN** 用户在「今日态势」或「规则与进化」页打开核心索引卡弹窗并点击「立即重建」
- **THEN** 两个入口打开同一实现的弹窗，重建成功后弹窗内容与所在页面持有的索引卡数据同步更新

### Requirement: 关于页产品定位与导航文案一致性
「关于」页 SHALL 与仓库 README 的产品定位保持一致：说明本产品是面向多 AI 编码工具的本地优先「治理与进化控制面」，由执行纪律与实证门禁、抗熵增进化引擎与受控审批流水线构成，并与 OpenViking / Mem0 等上下文数据库、记忆引擎形成正交互补关系。
「关于」页及其他控制台页面中指向批次页面的跳转按钮与说明文案 MUST 统一使用页面名称「沉淀与审批」，不得再出现旧名称「每日沉淀」。

#### Scenario: 关于页展示治理与进化控制面定位
- **WHEN** 用户访问「关于」页
- **THEN** 首屏文案、阶段说明或页脚架构说明中包含「治理与进化控制面」定位，以及「与 OpenViking / Mem0 等上下文数据库正交互补」的边界表述

#### Scenario: 批次页面名称统一
- **WHEN** 用户查看「关于」页的跳转按钮，或查看「自动化调度」页中指向批次页面的说明文案
- **THEN** 文案使用「沉淀与审批」，不出现「每日沉淀」

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

### Requirement: 知识保鲜台账同步 Wiki 归档状态并尊重人工/自动归档边界
系统 SHALL 在 `sync_fts_index` 扫描挂载知识库时同步文档 frontmatter 中的 `status` 与 `last_verified` 到 `knowledge_card_metrics` 台账：
- 当源文档 frontmatter 的 `status` 为 `archived` 或 `superseded` 时，系统 MUST 将对应台账记录的 `freshness_state` 同步为相同终态，使其自动退出「待复核」清单；
- 当某篇文档在飞轮台账中已处于 `archived` 或 `superseded` 时，常规 FTS 同步 MUST 保持其归档状态不变，仅当源文档 frontmatter 出现严格更新的 `last_verified` 日期（且 `status` 非归档态）时才自动重新激活为 `fresh`。

#### Scenario: Wiki 源文件标记 status archived 后同步退出待复核
- **WHEN** 某篇过期笔记的 Markdown frontmatter 被更新为 `status: archived` 并触发 `sync_fts_index()`
- **THEN** `knowledge_card_metrics` 中该文档的 `freshness_state` 必须更新为 `archived`
- **AND** `GET /api/site/knowledge` 中该文档的 `is_expired` 必须为 `false`，不再计入 `expired_total`

#### Scenario: 已在飞轮台账归档的笔记在源文件未更新时保持归档，源文件更新后自动重新激活
- **WHEN** 某篇笔记在飞轮台账被标为 `archived`，其源文件 `last_verified` 未变化时执行 `sync_fts_index()`
- **THEN** 该笔记在台账中必须保持 `archived` 不被覆盖回 `fresh`
- **AND** 当源文件 `last_verified` 更新为更晚日期后再次同步，该笔记必须重新激活为 `fresh` 并重算 `valid_until`

### Requirement: 周五蒸馏 Tier 1 自动退场超期零助益知识笔记（仅改台账）
`core/dream.py` 的周五蒸馏 Tier 1 SHALL 自动将超过 `valid_until` 满 `stale_card_grace_days`（默认 30 天）且 `helped_count == 0` 的知识卡片在飞轮台账 `knowledge_card_metrics` 中标记为 `archived` 并写入审计事件，同时绝不修改或删除知识库磁盘文件；只读预览 `preview_dream_consolidation` MUST 透出 `auto_archived_cards` 候选清单且零写入。

#### Scenario: 超期满 30 天且零助益的卡片在蒸馏时自动退场，有助益卡片保留待复核
- **WHEN** 存在卡片 A（`valid_until` 早于今天 40 天且 `helped_count = 0`）与卡片 B（`valid_until` 早于今天 40 天但 `helped_count = 2`），先调用 `preview_dream_consolidation` 再调用 `run_dream_consolidation`
- **THEN** 只读预览必须在 `auto_archived_cards` 中列出卡片 A、不包含卡片 B，且不修改数据库
- **AND** 执行蒸馏后，卡片 A 的台账 `freshness_state` 变为 `archived` 并写入审计事件，卡片 B 保持 `needs_recheck`，且两篇磁盘源文件字节内容零变化

### Requirement: 控制台知识保鲜纯台账兜底复核接口与弹窗交互
系统 SHALL 提供 `POST /api/site/knowledge/freshness` 接口与阅读弹窗操作栏，支持维护者在控制台对知识笔记保鲜台账执行兜底操作（`verify` 顺延有效、`archive` 移出待复核、`restore` 恢复跟踪、`set_tier` 调整 30/90/180 天档位），所有操作 MUST 仅更新 SQLite 台账与审计事件并刷新核心索引卡，绝不修改知识库源文件。

#### Scenario: 维护者在控制台弹窗确认有效或归档仅更新台账
- **WHEN** 客户端调用 `POST /api/site/knowledge/freshness` 对一篇过期卡片执行 `action="verify"` 或 `action="archive"`
- **THEN** 接口返回 HTTP 200 及更新后的新鲜度记录与 `expired_total`，对应审计事件入库；`verify` 刷新 `last_verified_at`/`valid_until` 并置 `fresh`，`archive` 置 `archived` 退出待复核
- **AND** 挂载知识库中对应的 `.md` 源文件保持零写入

