# Change: 双轨沉淀互通意图、子代理防污染与调度漏跑补跑（P0）

## Why

真实多客户端协作运行暴露出四处入口与调度链路断点，导致日常沉淀绕过飞轮队列、双轨状态割裂且晚间定时批处理静默漏跑：

1. **双轨沉淀缺少状态互通与防重契约**：当高复杂度会话（如多表结构核对、协作事故登记或契约修订）在客户端完成即时手工落盘后，现有 `flywheel_session_intent` 仅有 `enqueue` / `opt_out` / `withdraw` 三种动作——选择 `opt_out` 会让该会话彻底退出飞轮、丢失错题观察池/规则晋升/分歧反例与 Skill 提炼；不登记则后台批处理会对其重复生成 `knowledge_doc`，产生文档重复与审核噪音。
2. **主动入队缺少结构化证据载荷**：客户端通过 `flywheel_ingest_session` 提交沉淀意图时只能传纯文本 `summary` 与 `content`，缺少可选的结构化事实摘要字段，导致关键查证结论在后续审查中容易被长对话噪音淹没。
3. **受控写入子代理碎片混入队列**：部分经历史扫盘或无父会话 ID 派单进入的受控写入子代理会话以 `queued` 滞留队列，若被自动批次认领会产生“对写文档过程再次沉淀”的元递归噪音。
4. **定时任务受短宽限期影响静默漏跑且缺少可观测信号**：`AutomationManager` 将 APScheduler 的 `misfire_grace_time` 硬编码为 `300` 秒（5 分钟）；宿主机在计划触发点短暂休眠超过 5 分钟后，当日定时任务被静默丢弃、无审计留痕也无补跑机制；同时 `/health` 与自动化快照缺少进程启动时间，运维难以察觉后台 `serve` 进程滞后于最新代码。

## What Changes

- **MCP 双轨互通意图与结构化入队（`mcp_server.py` + `core/queue_service.py`）**：
  - `flywheel_session_intent` 新增动作 `hand_distilled`（可选 `wiki_paths: list[str]` 与 `governance_only: bool = True`），在 `distill_queue.payload_json` 与会话 `metadata` 中记录 `distill_mode = "governance_only"` 及已手工落盘路径，以 `P0_CORE` 入队（或幂等替换同会话未消费项），并防止后续归档同步把已手工沉淀会话覆盖回普通写卡模式；
  - `flywheel_ingest_session` 新增可选 `structured_brief: dict | None` 参数，将客户端提炼的结构化事实/领域线索写入队列载荷供 P02/P03 消费。
- **P03 治理专属规划过滤（`core/pipeline/coordinator.py` + `core/pipeline/helpers.py`）**：
  - 当批次中包含 `distill_mode == "governance_only"` 的队列项时，P02 仍照常提取错题候选、分歧候选与重复流程候选；P03 必须过滤掉来源会话全部为 `governance_only` 的 `knowledge_doc` 计划项，写入 `governance_only_wiki_skipped` 审计事件，但完整保留 `flywheel_rule`、`skill_improvement` 与错题/分歧候选的后续落库。
- **受控写入子代理防污染（`core/triage.py` + `core/queue_service.py`）**：
  - 扩展子代理/无头写入任务识别规则，拦截受控写入子代理派单会话进入默认自动批次，并支持通过 `re_triage_queue` 清洗存量混入队列项。
- **定时调度防休眠静默漏跑与可观测补跑（`config.py` + `flywheel.yaml.example` + `core/automation.py` + `app.py`）**：
  - `WorkflowConfig` 新增 `misfire_grace_seconds`（默认 `14400` 秒）与 `catchup_missed_schedule`（默认 `true`），替换硬编码 300 秒；
  - `serve` 角色启动与 `recovery` 巡检循环检测当日已过计划时间、当日定时未运行且存在待处理队列项时，记录 `schedule_misfire_detected` 审计事件并在开启补跑时最多触发一次当日补跑（持锁互斥、幂等防重复）；
  - `/health` 与 `GET /api/site/automations` 暴露服务 `started_at` 及当日漏跑状态 `missed_today`。

## Capabilities

### Modified Capabilities
- `distill-queue-pipeline`: 新增 `hand_distilled`（`governance_only`）双轨防重意图、可选 `structured_brief` 入队载荷，以及受控写入子代理碎片识别与重筛清洗。
- `workflow-orchestration`: 约束 P03 对 `governance_only` 会话跳过 `knowledge_doc` 计划生成并留痕，同时保留错题/规则/分歧/Skill 治理产出。
- `automation-console`: 支持可配置 `misfire_grace_seconds` 与当日漏跑检测/补跑机制，并在健康检查与自动化快照中暴露进程 `started_at` 与 `missed_today` 状态。

## Impact

- 后端：`backend/ai_flywheel/mcp_server.py`、`backend/ai_flywheel/core/queue_service.py`、`backend/ai_flywheel/core/triage.py`、`backend/ai_flywheel/core/pipeline/coordinator.py`、`backend/ai_flywheel/core/pipeline/helpers.py`、`backend/ai_flywheel/core/automation.py`、`backend/ai_flywheel/config.py`、`backend/ai_flywheel/app.py`、`backend/ai_flywheel/routes/overview.py`
- 配置模板：`flywheel.yaml.example`
- 测试：`tests/test_distill_queue.py`、`tests/test_automation_console.py`、`tests/test_workflow_orchestration.py`、`tests/test_arch_invariants.py`
- 热文件串行依赖：本变更触及 `core/pipeline/coordinator.py` 与 `core/pipeline/helpers.py`，进入 Apply 实施代码前须由维护者先归档已完成提交的 `feat-project-domain-incremental-deposit`。
- 兼容性：复用既有 SQLite v4 的 `distill_queue.payload_json` 与 `sessions.metadata_json`，无需数据库结构迁移，对存量 MCP 调用方与配置文件完全向后兼容。
