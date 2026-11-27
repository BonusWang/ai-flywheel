# Tasks: feat-sediment-dual-track-and-scheduler-catchup

- [x] 1. 在 `config.py` 与 `flywheel.yaml.example` 中新增 `workflow.misfire_grace_seconds`（默认 `14400`）与 `workflow.catchup_missed_schedule`（默认 `true`），并补齐配置默认值校验单测【test】
- [x] 2. 在 `core/queue_service.py` 与 `mcp_server.py` 中为 `flywheel_session_intent` 新增 `action="hand_distilled"`（支持 `wiki_paths` 与 `governance_only`，持久化至 `payload_json`/`metadata_json` 且不被后续同哈希归档覆盖），并为 `flywheel_ingest_session` 增补经 `redact_text` 脱敏的可选 `structured_brief` 载荷【test】
- [x] 3. 在 `core/triage.py` 与 `core/queue_service.py` 中加固受控写入子代理碎片识别规则，确保无独立业务上下文的写入派单会话在入队与 `re_triage_queue` 时归入 `excluded (SUBAGENT_FRAGMENT)`【test】
- [x] 4. 在 `core/pipeline/coordinator.py` 与 `core/pipeline/helpers.py` 中实现 P03 `governance_only` 确定性硬过滤：移除纯 `governance_only` 来源的 `knowledge_doc` 并写入 `governance_only_wiki_skipped` 审计事件，同时保留 `flywheel_rule`、`skill_improvement` 与错题/分歧落库（执行前确认前序热文件变更 `feat-project-domain-incremental-deposit` 已归档）【test】
- [x] 5. 在 `core/automation.py`、`app.py` 与 `routes/overview.py` 中将 `daily_flywheel` 绑定 `misfire_grace_seconds`，实现 `serve` 角色启动/巡检漏跑检测与每日一次幂等补跑，并在 `/health` 与 `/api/site/automations` 暴露 `started_at`、`missed_today` 及补跑配置状态【test】
- [x] 6. 运行 `python scripts/verify_repo.py --frontend` 全门禁验收，并在真实本地 `serve` 进程上完成 `/health`、`/api/site/automations`、MCP `hand_distilled` 入队与沙箱 P00–P07 端到端冒烟验证【real】
