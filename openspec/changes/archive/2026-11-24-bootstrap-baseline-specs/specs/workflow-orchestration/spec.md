# workflow-orchestration —— P00–P07 工作流编排

> 现状基线（2026-11-24，对应 v0.3.0 代码）。只描述系统已经具备的行为。

## ADDED Requirements

### Requirement: 独占运行锁
系统 SHALL 保证同一时刻只有一个运行持有独占锁。已有运行持锁时再次启动 MUST 抛出 `WorkflowLockError`，且不创建新运行。安全阀 `WORKFLOW_ENABLED` 关闭时 MUST 拒绝启动。

#### Scenario: 重复启动被拒绝
- **WHEN** 已有运行持有独占锁时再次启动运行
- **THEN** 抛出 `WorkflowLockError` 并指明当前持锁运行，不产生新的运行记录

### Requirement: 固定 8 节点状态机与节点回执
每次运行 SHALL 依次经过 P00 准备、P01 启动、P02 审查、P03 路由与计划、P04 写入与验收、P05 结算、P06 报告、P07 交付八个节点。每个节点结束时 MUST 把节点快照写入不可变回执存储并记录回执哈希前缀，同时向事件总线发布节点状态更新。

#### Scenario: 节点结束落回执
- **WHEN** 任一节点进入终态
- **THEN** 在回执存储生成 `runs/{run_id}/{node_id}` 回执，运行记录带该回执的哈希前缀

### Requirement: P00 准备与对账
P00 SHALL 校验内置策略完整性、按配置的扫描源对账登记新会话，并在未显式指定会话时执行规则初筛；初筛跳过数大于 0 时 MUST 追加 `triage_skipped` 审计事件。待审会话取显式指定的会话，否则取 `pending` 会话并按 P0 核心、P1 信号、P1 一般、P2 元信息的优先级排序。

#### Scenario: 初筛跳过留痕
- **WHEN** 规则初筛跳过了临时或碎片会话
- **THEN** P00 摘要给出跳过数量，并写入 `triage_skipped` 审计事件

### Requirement: P01 独占准入
P01 SHALL 在单次运行会话数上限与总字符预算内依次准入候选会话（单会话正文截取上限 24000 字符），把准入会话置为 `in_progress`。继续准入将超预算且已有准入会话时 MUST 停止准入。

#### Scenario: 字符预算截断准入
- **WHEN** 下一个候选会话会使总字符数超过 `max_chars_per_run`，且已有至少一个准入会话
- **THEN** 停止准入，P01 摘要给出准入数量与已用字符数

### Requirement: P02 全文审查与错题入池
P02 SHALL 调用执行器审查准入会话。执行器按配置选择：`api` 模式且已配置地址与模型时用 OpenAI 兼容 API，`cli` 模式且已配置命令时用本地 CLI，否则用零成本的启发式演练执行器。审查产出的错题 MUST 按根因键写入错题观察池；仅当观察记录仍在观察中，且为高危沉默错误，或自查拦截类累计出现不少于 3 次时，才进入可晋升清单；分歧与反例 MUST 单独留存，不覆盖既有规则。

#### Scenario: 自查拦截类满三次进入可晋升
- **WHEN** 同一根因键的自查拦截类错题累计出现 3 次
- **THEN** 该观察记录进入本轮可晋升清单

#### Scenario: 高危沉默错误首次即可晋升
- **WHEN** 审查产出一条高危沉默错误
- **THEN** 该观察记录首次出现即进入本轮可晋升清单

### Requirement: P03 路由与写入计划
P03 SHALL 调用执行器生成写入计划：与既有规则和技能查重，写入项数量不超过 `max_writes_per_run` 与知识落点 `max_write_set` 的较小值；计划文档的默认前缀取第一个允许写入前缀。挂载 Schema 治理的 Wiki 时，MUST 对每项 `knowledge_doc` 计划执行 T5 归一化。

#### Scenario: 写入项数量受限
- **WHEN** 执行器返回的写入项超过单批上限
- **THEN** 只保留上限内的写入项，P03 内部检查记录计划数与上限

### Requirement: 审批门
P03 之后系统 SHALL 按生效审批模式决定是否挂起：`auto` 不挂起；`always` 只要有写入计划就挂起；`smart` 仅在出现 Skill 封装 / 优化、标记需审批、删除或覆盖核心文件，或写入项数超过高写入阈值时挂起。调用方显式传入的自动放行 / 强制审批覆盖优先于配置。

挂起时 P04 置为等待审批、运行置为 `AWAITING_APPROVAL`，系统 MUST 追加 `awaiting_approval` 审计事件，创建选项为「同意并写入」「取消本次写入」的决策卡并登记门户投递；飞书通道启用时一并推送。

#### Scenario: smart 模式遇 Skill 变更挂起
- **WHEN** 审批模式为 `smart`，且计划含 Skill 封装或优化项
- **THEN** 运行在 P03 后挂起等待审批，P04 置为等待审批

#### Scenario: smart 模式常规笔记直接放行
- **WHEN** 审批模式为 `smart`，计划只含常规笔记与规则且数量不超过高写入阈值
- **THEN** 运行不挂起，追加 `auto_approved` 审计事件后直接进入 P04

#### Scenario: 人工批准后续跑
- **WHEN** 对等待审批的运行批准了部分写入项
- **THEN** 仅被批准的项进入 P04，运行状态回到运行中并继续执行

### Requirement: 拒绝放行
对等待审批的运行执行拒绝时，系统 SHALL 把准入会话恢复为 `pending`，把 P04–P07 标记为跳过，运行置为 `PARTIAL` 并记录原因，释放独占锁，关闭决策卡，追加 `rejected` 审计事件。

#### Scenario: 取消本次写入
- **WHEN** 用户对等待审批的运行选择取消
- **THEN** 没有任何写入发生，准入会话回到 `pending`，独占锁被释放

### Requirement: fail-run 契约
任一节点抛出异常时，系统 SHALL 把该节点置为 `FAILED` 并记录原因，把其后所有未执行节点显式置为 `SKIPPED`，运行置为 `FAILED`，把准入会话全部恢复为 `pending`，释放独占锁，追加 `run_failed` 审计事件并落失败回执；飞书通道启用时 MUST 推送异常告警。

#### Scenario: P04 首项写入失败
- **WHEN** P04 中某项写入被门禁拒绝并抛出异常
- **THEN** P04 为 `FAILED`，P05–P07 为 `SKIPPED`，运行为 `FAILED`，准入会话回到 `pending`，锁已释放

### Requirement: P04 受控写入
P04 SHALL 只处理被批准的写入项：知识文档经写入链路落盘；规则经确定性归一化后注册，带观察记录标识的同步把该观察置为已晋升；Skill 项在 `SKILL_WRITE_ENABLED` 关闭时记为跳过，否则经 Skill 写入校验并登记技能资产。P04 MUST 汇总写入回执作为节点载荷。

#### Scenario: Skill 写入安全阀关闭
- **WHEN** `SKILL_WRITE_ENABLED` 关闭且计划含 Skill 项
- **THEN** 该项回执为 SKIPPED_BY_SAFETY_VALVE，不写任何技能文件

### Requirement: P05 结算
P05 SHALL 把准入会话结算为 `processed`，其中 P02 判定为不值得沉淀（`worthy` 为假）的会话结算为 `archived`；阶段含周复盘时执行周五蒸馏；每次结算后 MUST 重建核心索引卡。

#### Scenario: 不值得沉淀的会话归档
- **WHEN** 某准入会话在 P02 被判定 `worthy` 为假
- **THEN** P05 把该会话置为 `archived`，其余准入会话置为 `processed`

### Requirement: P06 报告与 P07 交付
P06 SHALL 生成运行摘要（处理对话数、沉淀笔记、新增规则、Skill 变更、核心索引卡状态及各节点结果）。P07 SHALL 按配置执行交付，随后把运行置为 `PASS`、释放独占锁、把决策卡置为完成、追加 `run_completed` 审计事件并落最终回执。

#### Scenario: 交付完成后释锁
- **WHEN** P07 交付执行完毕
- **THEN** 运行状态为 `PASS`，`lock_released` 为真，并写入 `run_completed` 审计事件

### Requirement: 阶段解析
触发阶段为 `auto` 时，系统 SHALL 按东八区时间判定：周五解析为 `both`（每日 + 周复盘），其余日期解析为 `daily`；`friday_weekly_enabled` 关闭时 `both` MUST 降为 `daily`。

#### Scenario: 非周五的自动阶段
- **WHEN** 东八区时间为非周五且触发阶段为 `auto`
- **THEN** 运行阶段解析为 `daily`
