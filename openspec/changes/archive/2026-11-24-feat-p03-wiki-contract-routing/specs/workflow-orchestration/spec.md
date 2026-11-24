## MODIFIED Requirements

### Requirement: P03 路由与写入计划
P03 SHALL 调用执行器生成写入计划：与既有规则和技能查重，写入项数量不超过 `max_writes_per_run` 与知识落点 `max_write_set` 的较小值；计划文档的默认前缀取第一个允许写入前缀。

P03 MUST 在节点摘要与节点载荷里写明计划来源（模型、本地 CLI 或本地启发式计划器）。

执行器报告有计划项被丢弃或未列入时，P03 MUST 在节点摘要里给出数量，在节点载荷里列出明细，并追加 `plan_items_dropped` 审计事件；节点不得因此失败。

执行器报告计划不可用时，P03 MUST 按 fail-run 契约失败：节点为 `FAILED`，节点载荷保留逐项原因，有被丢弃项时先追加 `plan_items_dropped` 审计事件；不得改用其他计划器，不得产生任何写入。

执行器报告有重复流程候选未生成 Skill 计划时，P03 MUST 在节点摘要里给出数量，在节点载荷里列出候选，并追加 `skill_candidates_deferred` 审计事件。

挂载 Schema 治理的 Wiki 时，P03 还 MUST：

- 发现路由候选集，并随计划请求交给执行器；
- 执行器为模型执行器（`api` / `cli`）时，提示词 MUST 包含候选集，并要求每项 `knowledge_doc` 声明页面类型、项目（或显式"无归属"）与理由；执行器 MUST 把声明原样带到计划项上，校验前不改写；
- 对每项 `knowledge_doc` 先做路由声明校验，结论为写入的再执行 T5 归一化；
- 留痕：已路由与未路由两张清单进节点载荷，分别写 `planned_doc_routed` 与 `planned_doc_unrouted` 审计事件，节点摘要给出未写入的数量与原因分布。

#### Scenario: 写入项数量受限
- **WHEN** 执行器返回的写入项超过单批上限
- **THEN** 只保留上限内的写入项，P03 内部检查记录计划数与上限

#### Scenario: 部分丢弃留痕
- **WHEN** `api` 模式下模型返回 5 项，其中 1 项不合规
- **THEN** 运行继续，P03 为 `PASS`；节点摘要写明计划来源为模型、1 项未列入；节点载荷列出该项的序号、类型与原因；存在 `plan_items_dropped` 审计事件

#### Scenario: 全部无效时整轮失败
- **WHEN** `api` 模式下模型的计划项全部不合规
- **THEN** P03 为 `FAILED`，P04–P07 为 `SKIPPED`，运行为 `FAILED`，准入会话回到 `pending`，独占锁已释放；存在 `plan_items_dropped` 与 `run_failed` 审计事件；写入计划为空，知识库与技能目录没有任何新增文件

#### Scenario: 演练模式标明来源与未成计划的 Skill 候选
- **WHEN** `runner.mode` 为 `dry_run`，审查结果含重复流程候选
- **THEN** P03 摘要写明计划来自本地启发式计划器并给出未生成计划的候选数量，写入计划里没有 Skill 项，存在 `skill_candidates_deferred` 审计事件

#### Scenario: 候选集进入提示词
- **WHEN** 门禁适用且执行器为模型执行器
- **THEN** P03 提示词包含可路由页面类型，以及每个候选项目的目录名、`project_id`、别名与描述

#### Scenario: 按声明归属的笔记被写入并留痕
- **WHEN** 模型为某篇知识文档声明的项目与证据不冲突
- **THEN** 该笔记归一化到声明项目的 `实战笔记/` 下，存在 `planned_doc_routed` 审计事件，节点载荷中它的路由来源为 `declared`

#### Scenario: 草稿无项目标识但声明正确
- **WHEN** 草稿全文没有声明项目的标识，工作区也没有命中
- **THEN** 该笔记仍按声明写入

#### Scenario: 演练执行器的计划走证据判定
- **WHEN** 门禁适用且执行器为零配置演练执行器
- **THEN** 知识文档按证据判定归属，节点载荷中它的路由来源为 `evidence`

### Requirement: 无法归属项目的知识文档处理
当某项 `knowledge_doc` 计划的路由结论为不写入（含 T5 归一化返回"无法归属"）时，系统 SHALL 不为它生成写入：

- P03 MUST 把该项移出写入计划，在节点摘要中给出数量与原因分布，并在节点载荷与 `planned_doc_unrouted` 审计事件中列出标题、原路径、来源会话、原因码、模型声明与证据命中；
- 运行不得因此失败或挂起；移除后计划为空时按"无写入计划"继续；
- P04 再次遇到时 MUST 写入状态为 `SKIPPED_UNROUTED_PROJECT` 的回执（带原因码）并继续处理其余写入项，节点摘要给出跳过数量。

#### Scenario: 无法归属的笔记不落库也不阻断
- **WHEN** 一批计划中有两项知识文档无法归属项目，其余计划正常
- **THEN** 运行状态为 `PASS`，这两项未写入，存在 `planned_doc_unrouted` 审计事件，且 P03 摘要给出数量 2

#### Scenario: 计划全部无法归属
- **WHEN** 本批全部知识文档都无法归属项目，且没有其他写入项
- **THEN** 写入计划为空，运行照常完成后续节点

#### Scenario: 声明与证据冲突留痕
- **WHEN** 模型为某篇知识文档声明项目 beta，而来源会话工作区命中项目 alpha
- **THEN** 该笔记不写入，`planned_doc_unrouted` 审计事件中它的原因码为 `workspace_mismatch`，并列出声明与工作区命中

#### Scenario: 无归属声明保留理由
- **WHEN** 模型声明某篇知识文档无归属并给出理由
- **THEN** 该笔记不写入，审计事件中它的原因码为 `declared_none` 并保留该理由

#### Scenario: 模型计划缺声明不写入
- **WHEN** 模型执行器的某项知识文档计划没有路由声明
- **THEN** 该笔记不写入，原因码为 `declaration_missing`
