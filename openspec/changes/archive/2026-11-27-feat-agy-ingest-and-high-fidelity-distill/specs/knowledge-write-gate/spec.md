# knowledge-write-gate 变更规格

## ADDED Requirements

### Requirement: Schema 治理 Wiki 的受控事故台账（incident）路由与强制敏感审批
当挂载 Wiki 的 `.wiki-schema.md` 声明了 `incident` 页面类型（且 `knowledge_sink.allowed_write_prefixes` 明确包含对应事故目录前缀）时，系统 SHALL 支持在 P03–P04 链路中受控起草与写入协作事故卡，并强制经过人工审批门：
- `discover_routing_candidates` MUST 识别 `.wiki-schema.md` 中声明的 `incident` 页面类型及其允许分区路径，仅当 `allowed_write_prefixes` 放行该分区前缀时将其加入可路由候选；
- `evaluate_route` 校验 `wiki_route.page_type == "incident"` 时，MUST 校验目标路径位于声明的事故分区内、符合日期/编号文件名模式并通过写前门禁与密钥拦截；
- `models.is_sensitive_write()` 与飞书审批卡片 `is_sensitive()` MUST 将所有 `incident` 页面类型的写入项统一判定为**敏感写入（强制挂起等待人工审批）**，即使在 `approval_mode = "smart"` 且单批只有 1 项写入时也不得自动放行。

#### Scenario: 允许前缀包含事故目录时 incident 路由通过并强制触发人工审批
- **WHEN** 挂载 Wiki 的 `.wiki-schema.md` 声明了 `incident` 类型、`allowed_write_prefixes` 包含对应事故分区前缀，P03 返回一条合规的 `incident` `knowledge_doc` 计划项且当前为 `approval_mode = "smart"`
- **THEN** 路由校验必须通过且 `is_sensitive_write()` 必须返回 `True`
- **AND** 流水线必须在 P03 之后挂起为 `AWAITING_APPROVAL` 等待维护者审批

#### Scenario: allowed_write_prefixes 未开放事故前缀时拒绝路由
- **WHEN** 挂载 Wiki 声明了 `incident` 类型，但 `allowed_write_prefixes` 未包含事故目录前缀
- **THEN** `incident` 不得进入可路由候选，模型强行声明 `incident` 时必须被拦截为未路由并留痕
