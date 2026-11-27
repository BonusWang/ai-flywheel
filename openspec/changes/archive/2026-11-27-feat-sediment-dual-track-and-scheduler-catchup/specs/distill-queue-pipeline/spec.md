# distill-queue-pipeline 变更规格

## ADDED Requirements

### Requirement: 手工已沉淀会话的双轨防重意图（hand_distilled）
系统 SHALL 在 MCP 工具 `flywheel_session_intent` 与 `DistillQueueService.handle_session_intent` 中支持 `action = "hand_distilled"`（可选参数 `wiki_paths: list[str]`、`governance_only: bool = True`），以打通“客户端即时手工写文档”与“飞轮后台治理提炼”双轨协同：
- 调用 `hand_distilled` 且 `governance_only = true` 时，系统 MUST 将该会话以 `P0_CORE` 优先级入队（若已有同会话未消费 `queued` 项则幂等替换为最新修订），并在队列项 `payload` 与会话 `metadata` 中持久化 `distill_mode = "governance_only"` 及脱敏后的 `hand_distilled_paths`；
- 调用 `hand_distilled` 且 `governance_only = false` 时，系统 MUST 将未消费的同会话排队项置为 `excluded`（`exclude_reason_code = "HAND_DISTILLED_SKIP"`）并留审计轨迹，不再触发后续任何分析；
- 已被标记 `distill_mode = "governance_only"` 的会话，后续若再收到相同内容哈希或未新增实质轮次的被动归档事件（`ARCHIVE`），系统 MUST 保留 `governance_only` 标记，不得将其降级回普通写卡模式或生成重复队列项；
- 所有 `hand_distilled` 状态变更 MUST 写入 `audit_events`（`entity_type = "queue_item"`），载荷包含 `distill_mode` 与 `hand_distilled_paths`。

#### Scenario: 手工沉淀后回调 hand_distilled 进入治理专属队列
- **WHEN** 客户端在手工完成知识文档写入后，调用 `flywheel_session_intent(action="hand_distilled", session_key="codex:s-101", wiki_paths=["projects/demo/note.md"], governance_only=True)`
- **THEN** 系统必须生成（或替换）一条 `P0_CORE`、`status = "queued"` 的队列项，其 `payload.distill_mode == "governance_only"` 且 `payload.hand_distilled_paths == ["projects/demo/note.md"]`
- **AND** `audit_events` 必须记录该次状态变迁

#### Scenario: 被动归档同步不覆盖已登记的 hand_distilled 模式
- **WHEN** 会话 `codex:s-101` 已通过 `hand_distilled` 登记 `governance_only = true`，随后本地归档目录对该同一会话触发 `ARCHIVE` 对账且内容未发生实质增量
- **THEN** 系统不得生成第二条普通模式 `queued` 项，原有 `governance_only` 标记必须保持不变

### Requirement: 主动入队携带可选结构化证据摘要
系统 SHALL 在 `flywheel_ingest_session` 与 `DistillQueueService.on_explicit_enqueue` 中支持可选参数 `structured_brief: dict | None`（可包含 `facts`、`domain_entities`、`decisions`、`incidents` 等结构化字段），经隐私脱敏后持久化到队列项 `payload.structured_brief`，并计入内容哈希以支持幂等更新；未传该参数时行为与现状完全一致。

#### Scenario: 通过 MCP 传入结构化摘要随队列项持久化
- **WHEN** 客户端调用 `flywheel_ingest_session` 并传入非空 `structured_brief`
- **THEN** 队列项 `payload.structured_brief` 必须保存脱敏后的结构化字典
- **AND** 若其中含命中密钥规则的片段，入队前必须经过 `redact_text` 脱敏

### Requirement: 拦截受控写入子代理碎片并支持存量清洗
系统 SHALL 在会话初筛与队列重筛中识别并拦截执行受控知识库写入的子代理派单会话（包括标记 `is_subagent = true` 或首轮指令命中受控写入派单特征且无独立业务问题上下文的会话），默认归入 `excluded`（`exclude_reason_code = "SUBAGENT_FRAGMENT"`），避免自动批次对“写文档过程”产生元递归沉淀。

#### Scenario: 存量受控写入子代理排队项在重筛后移出队列
- **WHEN** 队列中存在 `is_subagent = true` 且首轮内容为受控写入派单的 `queued` 条目，调用 `re_triage_queue(dry_run=False)`
- **THEN** 该条目必须转为 `excluded` 且 `exclude_reason_code = "SUBAGENT_FRAGMENT"`，不再进入自动批次认领
