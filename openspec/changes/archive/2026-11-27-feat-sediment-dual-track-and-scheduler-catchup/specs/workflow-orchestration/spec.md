# workflow-orchestration 变更规格

## ADDED Requirements

### Requirement: P03 对 governance_only 会话跳过知识文档写入并保留治理产出
当批次准入的队列项携带 `distill_mode = "governance_only"`（已由客户端即时手工完成知识文档落盘）时，流水线 SHALL 保持 P02 审查不变（正常抽取错题候选、分歧候选与重复流程候选），并在 P03 强制执行以下过滤契约：
- 若某条 `knowledge_doc` 计划项的所有 `source_session_keys` 均为本批中的 `governance_only` 会话，P03 MUST 将其从最终写入计划中移除，不得写入任何知识文档；
- 被过滤的 `knowledge_doc` 项 MUST 记录到 P03 节点载荷（`governance_only_skipped_docs`）并追加一条 `governance_only_wiki_skipped` 审计事件，节点不得因此失败；
- 来自 `governance_only` 会话的 `flywheel_rule`、`skill_improvement` 以及 P02 产出的错题候选与分歧候选 MUST 完整保留，按既有审批门与 P04 契约正常推进；
- 若同一批次混合了普通会话与 `governance_only` 会话，普通会话产出的 `knowledge_doc` MUST 照常路由与写入，不受影响。

#### Scenario: governance_only 会话不重复写 Wiki 但正常产出规则与错题
- **WHEN** 本批次准入一条 `distill_mode = "governance_only"` 的会话，P02 产出 1 条高危沉默错题候选与 1 条发现，P03 执行器返回 1 条 `knowledge_doc` 和 1 条 `flywheel_rule`（`source_session_keys` 均指向该会话）
- **THEN** P03 最终写入计划必须移除该 `knowledge_doc`、保留该 `flywheel_rule`
- **AND** P03 节点载荷与 `audit_events` 必须记录 `governance_only_wiki_skipped`
- **AND** 审批放行进入 P04 后，知识库不得新增文件，规则表与错题观察池必须正常落库

#### Scenario: 混合批次中普通会话的知识文档照常生成
- **WHEN** 本批次同时准入普通会话 `s-normal` 与 `governance_only` 会话 `s-hand`，P03 返回分别归属两者的 2 条 `knowledge_doc`
- **THEN** 仅移除归属 `s-hand` 的 `knowledge_doc`，归属 `s-normal` 的 `knowledge_doc` 必须照常进入路由校验与写入计划
