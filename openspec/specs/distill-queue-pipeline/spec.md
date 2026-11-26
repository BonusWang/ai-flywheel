# distill-queue-pipeline Specification

## Purpose
TBD - created by archiving change feat-distill-queue-and-inventory. Update Purpose after archive.
## Requirements
### Requirement: 统一队列入队状态机与三级闸门
系统 SHALL 通过 `DistillQueueService` 统一处理 `EXPLICIT_ENQUEUE`、`ARCHIVE`、`WITHDRAW`、`OPT_OUT` 四类队列事件，并在入队时按严格优先级执行三级闸门，所有状态变迁 MUST 写入通用轨迹表 `audit_events`。

#### Scenario: 显式不沉淀标记一票否决
- **WHEN** 会话主档已标记 `opt_out = 1`，或归档文本/标签包含显式不沉淀标记（如 `#no-flywheel` 或 `本会话不沉淀`），随后触发 `ARCHIVE` 入队事件
- **THEN** 系统必须将该次入队记录置为 `excluded` 且 `exclude_reason_code = "USER_OPT_OUT"`，不得进入 `queued`
- **AND** `audit_events` 必须记录一条 `entity_type = "queue_item"`、`to_status = "excluded"`、`reason_code = "USER_OPT_OUT"` 的轨迹

#### Scenario: 手工与 MCP 显式声明直通高优排队
- **WHEN** 用户在控制台手工补录或通过 MCP `flywheel_session_intent(action="enqueue")` 提交沉淀
- **THEN** 即使对话较短或位于灰名单工作区，系统也必须绕过噪音过滤，将其以 `P0_CORE` 优先级直接写入 `queued`

### Requirement: 撤回再输入与增量水位线
系统 SHALL 支持对话在归档后被撤回、继续输入并再次归档的完整闭环，不得丢失已沉淀历史或重复生成冗余排队项。

#### Scenario: 未消费项撤回后再次归档不产生重复排队
- **WHEN** 某会话归档进入 `queued`（`rev 1`）后触发 `WITHDRAW`，随后追加新对话再次触发 `ARCHIVE`
- **THEN** 原 `rev 1` 必须保持 `withdrawn` 状态不被消费，新归档必须以 `rev 2` 进入 `queued`

#### Scenario: 已沉淀会话续聊后再归档仅针对增量入队
- **WHEN** 某会话 `rev 1` 已完成沉淀（`status = "processed"`）并记录消费水位线，随后会话新增多轮对话再次归档
- **THEN** 系统必须保留 `rev 1` 的 `processed` 记录与批次归属，并为新增区间生成 `rev 2`（`consumed_from_turn` 接续上次水位线）进入 `queued`
- **AND** 若内容哈希相比已消费水位线无任何变化，则不得重复入队

### Requirement: 超大上下文结构化瘦身与分片（不静默丢尾）
系统 SHALL 在入队前对超大上下文执行 0-token 确定性瘦身，瘦身后仍超过 `queue_max_item_chars` 阈值时按轮次边界自动拆分为有序分片 `chunk 1/N .. N/N` 入队，确保后半段结论与根因不被静默截断。

#### Scenario: 超大上下文自动拆分为多片并可逐片读取
- **WHEN** 一条超过单条字数上限的长对话触发 `ARCHIVE` 入队
- **THEN** 系统必须生成 `chunk_total > 1` 且 `is_oversized = 1` 的连续分片队列项（`chunk_index = 1..N`）
- **AND** 按分片加载文本时，尾部分片必须包含原对话末尾的关键结论内容

### Requirement: Codex 严格归档制与 Claude Code exit 触发制
系统 SHALL 对 Codex 执行严格归档制（仅同步 `codex_archive_sources` 归档目录与 MCP 声明，不自动入队活跃会话），并对 Claude Code 执行 `SessionEnd` 退出触发制（`Stop` 不再触发入队，`SessionStart(resume)` 或同会话新提示自动撤回未消费排队项）。

#### Scenario: Codex 归档目录出现、移除与增长同步
- **WHEN** 一个 `.jsonl` 文件进入配置的 Codex `archived_sessions` 目录并运行对账
- **THEN** 系统必须按规则闸门生成对应的 `codex_archive` 队列项
- **AND** 当该文件在消费前从 `archived_sessions` 移除后再次对账，其 `queued` 项必须自动转为 `withdrawn`

#### Scenario: Claude Code 退出触发入队与 resume 自动撤回
- **WHEN** Claude Code Hook 依次发送 `Stop`、`SessionEnd`、`SessionStart(source="resume")`
- **THEN** `Stop` 不得创建队列项；`SessionEnd` 必须触发 `claude_exit` 入队；随后的 `SessionStart(source="resume")` 必须将尚未消费的 `queued` 项转为 `withdrawn`

#### Scenario: UserPromptSubmit 透传真实 session_key 并支持 MCP 对齐控制
- **WHEN** `UserPromptSubmit` Hook 收到带 `session_id` 的请求
- **THEN** 返回的 `additionalContext` 头部必须包含规范 `session=<client>:<session_id>` 标识
- **AND** 模型随后调用 MCP `flywheel_session_intent(action="opt_out", session_key=...)` 必须精准命中该同一会话

### Requirement: 队列失败重试与死信隔离（DLQ）
系统 SHALL 为每条队列项独立维护 `retry_count` 与 `max_retries`，防止毒丸会话无限阻断后续批次。

#### Scenario: 连续失败达到重试上限后自动隔离进死信
- **WHEN** 某条 `queued` 队列项被批次认领后连续执行失败达到 `max_retries` 次
- **THEN** 该队列项状态必须转为 `dead_letter` 并记录 `last_error`，后续自动批次不再认领它
- **AND** 维护者可通过队列接口执行 `requeue` 重置 `retry_count` 并重新入队

### Requirement: 队列控制面 API 与 Workflows 库存双 Tab 看板
系统 SHALL 暴露完整的队列库存查询、单条/批量干预、归档同步与规则重筛预览接口，并在 Web 控制台 `Workflows` 页面展示顶部常驻库存条及「沉淀队列（库存）」与「执行流水线（P00–P07）」双 Tab。

#### Scenario: 查询队列库存汇总与按条件筛选分页
- **WHEN** 前端或客户端调用 `GET /api/site/queue`
- **THEN** 必须返回包含 6 态计数、按 `client` 分片计数及 `estimated_batches` 的 `summary`，以及支持 `status / client / ingress_channel / q` 筛选与分页的 `items`

#### Scenario: 规则重筛支持只读预览与实际应用
- **WHEN** 调用 `POST /api/site/queue/re-triage` 且 `dry_run = true`
- **THEN** 接口必须返回预计受影响的条目清单与变化计数，且不得修改数据库中的任何队列状态

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

