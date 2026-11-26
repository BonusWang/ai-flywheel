# control-plane-console Specification Delta

## ADDED Requirements

### Requirement: 实时异常线索透出队列结算归属与转入错题观察池闭环
系统 SHALL 消除「实时异常线索」视图中“建议留存”与“已处理”的语义冲突，并提供真实的线索转错题沉淀闭环：
- `GET /api/site/retro` MUST 为每条线索附带 `has_queued_session`（布尔值，指示其 `session_key` 当前是否存在 `queued` 或 `processing` 的队列项）与 `settlement_mode`（`auto_next_batch` 或 `manual_only`）；
- `advise_retro_item` 对 `verdict = "keep"` 的高价值实证/复发线索 MUST 返回 `recommended_action = "promote"`，不得引导维护者误点仅结案的 `resolved`；
- 系统 MUST 提供 `POST /api/site/retro/promote` 接口，接受 `retro_ids` 列表，将其聚合转换为 `mistake_observations` 观察错题记录（按 `root_cause_key` 幂等去重）、将对应线索状态更新为 `resolved`、写入 `audit_events` 并刷新核心索引卡；
- 前端 `CluesObservationsTab` 的「实时异常线索」视图 SHALL 展示「随下次沉淀自动结算」或「无关联待审会话」徽标，并清晰区分「转入错题观察池」「已人工处理（不再送入下次沉淀）」「忽略」三类操作。

#### Scenario: 将高价值实时异常线索一键转入错题观察池并结案
- **WHEN** 存在一条 `status = "open"` 的 `missing_evidence` 或 `rule_recurrence` 实时异常线索，客户端调用 `POST /api/site/retro/promote`
- **THEN** 后端必须在 `mistake_observations` 中创建或更新对应观察错题记录，将该线索状态更新为 `resolved`，写入 `retro_promoted_to_observation` 审计事件并返回 HTTP 200 与错题对象

#### Scenario: 线索列表准确区分随下次沉淀自动结算与无关联待审会话
- **WHEN** 线索 A 的 `session_key` 当前在 `distill_queue` 中处于 `queued` 状态，线索 B 的 `session_key` 不在活跃队列中，客户端请求 `GET /api/site/retro`
- **THEN** 线索 A 必须返回 `has_queued_session: true` 与 `settlement_mode: "auto_next_batch"`，线索 B 必须返回 `has_queued_session: false` 与 `settlement_mode: "manual_only"`
