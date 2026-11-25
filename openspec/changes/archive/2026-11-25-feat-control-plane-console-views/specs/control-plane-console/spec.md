# 治理与进化控制面控制台规格

## ADDED Requirements

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
