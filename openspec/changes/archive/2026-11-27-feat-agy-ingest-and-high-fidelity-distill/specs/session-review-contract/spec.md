# session-review-contract 变更规格

## ADDED Requirements

### Requirement: P02 高保真输入预算、结构化摘要置顶与阶段模型分层
系统 SHALL 消除固定 3000 字符截断对长排查会话造成的中段事实丢失，并支持为 P02 审查独立配置模型档位：
- `ApiRunnerConfig` MUST 提供 `p02_session_char_limit`（默认 `16000`，范围 `2000..64000`），P02 组装单会话正文时 MUST 使用该配置预算，不得再硬编码 3000 字符上限；
- 当队列项载荷携带 `structured_brief` 时，P02 提示词 MUST 将该结构化摘要置顶置于该会话文本之前，确保客户端主动提炼的关键表名、配置事实与结论不被截断；
- `ApiRunnerConfig` MUST 支持可选 `review_model` 与 `review_reasoning_effort`（为空时回退至 `model` / `reasoning_effort`）；`OpenAICompatibleRunner` 执行 P02 时 MUST 使用阶段生效模型与推理强度，并将两者纳入 `api_cache` 缓存键，避免切换阶段模型误命中旧缓存；
- P02 审查输出契约 SHALL 支持可选 `events` 数组（每项含 `event_id`、`kind` ∈ `delivery|incident|decision|domain_fact`、`title`、`summary`、`session_key`），用于将同一会话内独立成立的业务交付与协作事故拆分为不同事件；缺失时向后兼容为空数组。

#### Scenario: 长会话按配置的 16000 字符预算保留中段核心表名与根因
- **WHEN** `p02_session_char_limit = 16000` 且送审一条 12000 字符的排查对话（关键表名与报错根因位于对话中段）
- **THEN** P02 提示词必须完整携带该会话内容，不得丢失中段关键表名与根因

#### Scenario: P02 使用独立 review_model 时缓存键与调用参数隔离
- **WHEN** 配置 `runner.api.model = "cheap-model"` 且 `runner.api.review_model = "strong-review-model"`、`review_reasoning_effort = "high"`
- **THEN** P02 请求体必须发送 `model = "strong-review-model"` 与 `reasoning_effort = "high"`，且生成的 `api_cache` 键必须与默认模型区分
