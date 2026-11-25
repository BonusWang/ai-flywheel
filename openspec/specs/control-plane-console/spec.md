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

