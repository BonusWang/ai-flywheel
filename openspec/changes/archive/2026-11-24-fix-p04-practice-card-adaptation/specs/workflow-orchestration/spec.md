## ADDED Requirements

### Requirement: 无法归属项目的知识文档处理
当 T5 归一化对某项 `knowledge_doc` 计划返回"无法归属"时，系统 SHALL 不为它生成写入：

- P03 MUST 把该项移出写入计划，在节点摘要中给出数量，并在节点载荷与 `planned_doc_unrouted` 审计事件中列出标题、原路径与来源会话；
- 运行不得因此失败或挂起；移除后计划为空时按"无写入计划"继续；
- P04 再次遇到无法归属的知识文档时 MUST 写入状态为 `SKIPPED_UNROUTED_PROJECT` 的回执并继续处理其余写入项，节点摘要给出跳过数量。

#### Scenario: 无法归属的笔记不落库也不阻断
- **WHEN** 一批计划中有两项知识文档无法归属项目，其余计划正常
- **THEN** 运行状态为 `PASS`，这两项未写入，存在 `planned_doc_unrouted` 审计事件，且 P03 摘要给出数量 2

#### Scenario: 计划全部无法归属
- **WHEN** 本批全部知识文档都无法归属项目，且没有其他写入项
- **THEN** 写入计划为空，运行照常完成后续节点
