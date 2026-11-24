## MODIFIED Requirements

### Requirement: P03 路由与写入计划
P03 SHALL 调用执行器生成写入计划：与既有规则和技能查重，写入项数量不超过 `max_writes_per_run` 与知识落点 `max_write_set` 的较小值；计划文档的默认前缀取第一个允许写入前缀。挂载 Schema 治理的 Wiki 时，MUST 对每项 `knowledge_doc` 计划执行 T5 归一化。

P03 MUST 在节点摘要与节点载荷里写明计划来源（模型、本地 CLI 或本地启发式计划器）。

执行器报告有计划项被丢弃或未列入时，P03 MUST 在节点摘要里给出数量，在节点载荷里列出明细，并追加 `plan_items_dropped` 审计事件；节点不得因此失败。

执行器报告计划不可用时，P03 MUST 按 fail-run 契约失败：节点为 `FAILED`，节点载荷保留逐项原因，有被丢弃项时先追加 `plan_items_dropped` 审计事件；不得改用其他计划器，不得产生任何写入。

执行器报告有重复流程候选未生成 Skill 计划时，P03 MUST 在节点摘要里给出数量，在节点载荷里列出候选，并追加 `skill_candidates_deferred` 审计事件。

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
