# distill-queue-pipeline Specification Delta

## ADDED Requirements

### Requirement: 会话身份归一化、可读标题提取与 JSONL 噪音剥离
系统 SHALL 在读取本地 JSONL 会话文件时提取结构化元数据与人类可读标题，并剥离工具输出与推理块噪音：
- Codex 扫描与归档同步 MUST 从 JSONL `session_meta.payload.id`（或文件名中的规范 UUID）归一化 `session_id` 为 `codex:<uuid>`，使其与 MCP / Hook 共享同一 `session_key` 去重边界；
- 标题生成优先级 MUST 为：`显式标题 / Claude Code custom-title` → `首条有效用户对话摘录（剥离系统样板与角色前缀）` → `工作区目录名 + 客户端 + 会话尾段标识`，不得把 `rollout-YYYY-MM-...` 截断为同名碰撞前缀；
- JSONL 文本提取 MUST 跳过 `tool_result`、`tool_use`、`function_call`、`function_call_output`、`thinking` 与 `reasoning` 记录或内容块，且超大上下文分片 MUST 在达到单会话分片上限（`MAX_CHUNKS_PER_SESSION`）时同时保留开篇上下文与结尾结论。

#### Scenario: Codex 磁盘扫描与 MCP 入队同一主会话时自动去重
- **WHEN** 某 Codex 主会话先通过 MCP 以 `codex:<uuid>` 入队，随后包含相同 `session_meta.payload.id = <uuid>` 的 `rollout-<timestamp>-<uuid>.jsonl` 进入归档同步或扫描
- **THEN** 系统必须将其识别为同一 `codex:<uuid>` 会话，不得再生成第二条 `codex:rollout-...` 重复队列项

#### Scenario: 从 JSONL 自动提取可读标题且不发生前缀截断撞名
- **WHEN** 归档或扫描读取到未显式传标题的 Claude Code（含 `custom-title`）或 Codex `rollout-2026-11-...` 文件
- **THEN** Claude Code 会话优先使用 `customTitle`；无自定义标题的会话优先使用首条有效用户消息精简摘录；即使正文为空，兜底标题也不得为多个文件共享的 `rollout-YYYY-MM-` 截断前缀

### Requirement: 父子会话归属模型与子代理自动认领隔离
系统 SHALL 识别并持久化子代理会话与其主会话的父子归属关系：
- 当 Codex `session_meta` 携带 `parent_thread_id` / `thread_source = "subagent"`（或 `source.subagent.thread_spawn.parent_thread_id`），或 Claude Code JSONL 标记 `isSidechain = true` 时，入队记录 MUST 写入 `parent_session_key` 与 `is_subagent = true`，并在 `payload` 中保留子代理角色或昵称（如有）；
- 带有效 `parent_session_key` 的子代理记录在自动批次认领（`list_queued_items_prioritized`）时 MUST 默认被跳过，仅认领主会话及其分片；
- 当维护者显式传入 `queue_ids` 触发整理时，系统 SHALL 允许认领被显式选中的子代理记录。

#### Scenario: 子代理记录归属主会话且不进入默认自动批次
- **WHEN** 一个主会话 `codex:<parent-uuid>` 与其派生的子代理 `codex:<child-uuid>`（`parent_thread_id = <parent-uuid>`）均处于 `queued` 状态，随后触发常规自动批次
- **THEN** 子代理队列项必须记录 `parent_session_key = "codex:<parent-uuid>"` 与 `is_subagent = true`
- **AND** 自动批次只认领主会话队列项，子代理项保持 `queued` 等待按需手动勾选
- **AND** 若维护者以 `queue_ids = [<child-queue-id>]` 显式触发批次，系统必须正常认领该子代理项

### Requirement: 可配置无头脚本过滤与重筛存量清洗
系统 SHALL 支持在 `SessionTriageConfig` 中配置 `exclude_content_prefixes`（默认空列表，与 `flywheel.yaml.example` 对齐）拦截无父会话 ID 的外部无头批处理调用，并结合通用无交互/禁用工具单轮特征将其归入 `excluded (SUBAGENT_FRAGMENT)`；`re_triage_queue` SHALL 在干跑预览保持零写入的前提下，于实际应用时对未消费队列项回读原 JSONL 刷新可读标题、工作区与父子归属，并将与规范主会话重复的旧 `codex:rollout-...` 条目标记为 `SUPERSEDED`。

#### Scenario: 命中内容排除前缀或无交互禁用工具特征的无头脚本被移出队列
- **WHEN** 一个无父会话 ID 的会话正文以配置的 `exclude_content_prefixes` 开头，或满足单轮无交互禁用工具批处理特征
- **THEN** 入队或重筛必须将其置为 `excluded` 且 `exclude_reason_code = "SUBAGENT_FRAGMENT"`
- **AND** 维护者仍可在控制台看到该移出原因并按需执行 `requeue` 恢复

#### Scenario: 重筛预览零写入，确认应用后清洗存量旧标题与重复 Codex rollout 项
- **WHEN** 队列中存在旧占位标题（如 `codex session rollout-2026-11-`）或与规范 `codex:<uuid>` 重复的旧 `codex:rollout-...` 排队项，先调用 `re_triage_queue(dry_run=True)` 再调用 `re_triage_queue(dry_run=False)`
- **THEN** `dry_run=True` 必须返回预计移出/刷新摘要且不修改任何数据库行
- **AND** `dry_run=False` 必须刷新未消费条目的可读标题、工作区与父子归属，并将重复的旧 `codex:rollout-...` 项置为 `excluded (SUPERSEDED)`

### Requirement: 队列看板展示工作区来源并按主会话树形折叠分片与子代理
Web 控制台「沉淀与审批 → 待提炼对话」队列看板 SHALL 在每条主记录上直接展示精简工作区来源（悬停显示完整路径），并将同一根会话（`parent_session_key || session_key`）的多个分片与所属子代理折叠归组在主会话行下，支持展开查看与单独勾选/插队。

#### Scenario: 同一主会话的多分片与子代理在队列列表折叠为一组
- **WHEN** 队列数据包含同一主会话的多个分片（`chunk_total > 1`）或挂载在同一 `parent_session_key` 下的多个子代理记录
- **THEN** 列表主视图必须将其折叠为一条根会话组，展示工作区短名、分片数与子代理数徽标；展开后可逐条查看分片序号、子代理角色/昵称并按需单选插队或移出
