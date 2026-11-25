# 工作流编排增量契约

## ADDED Requirements

### Requirement: 流水线认领、结算与失败重试衔接沉淀队列
工作流编排器 SHALL 在 P00–P07 阶段中与 `distill_queue` 协同工作，支持显式 `queue_ids` 插队认领与自动按优先级认领，并在结算、失败与审批取消时精确维护队列状态、会话水位线与重试计数。

#### Scenario: P01 认领队列项并在 P05 结算推进水位线
- **WHEN** 启动一次工作流批次（未指定 `session_keys` 或指定 `queue_ids`）
- **THEN** P01 必须将认领的 `distill_queue` 条目标记为 `processing` 并绑定当前 `run_id`
- **AND** P05 必须将已审查条目标记为 `processed` 且更新对应 `sessions` 主档的 `last_consumed_turn` 与 `last_consumed_hash`，将未审查条目安全退回 `queued`

#### Scenario: 批次失败递增重试与审批取消零惩罚退回
- **WHEN** 已认领队列项的批次执行失败（`FAILED`）
- **THEN** 对应队列项必须递增 `retry_count`；当 `retry_count >= max_retries` 时转入 `dead_letter`，否则退回 `queued`
- **AND** 当批次在审批门被拒绝（`CANCELLED`）时，对应队列项必须直接退回 `queued` 且不得递增 `retry_count`
