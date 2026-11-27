# Design: 双轨沉淀互通意图、子代理防污染与调度漏跑补跑（P0）

## 1. 设计目标与边界

本 P0 变更聚焦“打通入口双轨防重、阻止子代理污染队列、根治定时休眠静默漏跑”三项高优止血能力，为 P1（跨客户端采集与 P02/P03 高保真质量追平）铺路：

- **零破坏性升级**：保持既有 SQLite v4 表结构、7 个 MCP 工具名、既有 REST 路由数量与 `rt.register_session` / `orchestrator.start_run` 兼容路径不变。
- **热文件串行纪律**：`core/pipeline/coordinator.py` 与 `core/pipeline/helpers.py` 刚由 `feat-project-domain-incremental-deposit` 修改并提交；本提案先冻结规格与设计，待维护者确认归档前序变更后再进入 Apply 实施阶段。

## 2. 关键设计决策与备选取舍

### 决策 A：`distill_mode` 与 `hand_distilled_paths` 存储位置

- **备选 1（新增 SQLite v5 迁移列）**：在 `distill_queue` 与 `sessions` 新增 `distill_mode`、`hand_distilled_paths_json` 列。需要升级 schema 版本、同步修改 `docs/schema.sql`、`docs/data-model.md` 与 `tests/test_arch_invariants.py` 的逐列断言，变更面偏重。
- **备选 2（采用：复用 `distill_queue.payload_json` 与 `sessions.metadata_json`）**：在 `payload` 中写入 `distill_mode`（默认 `"full"`，手工报备时为 `"governance_only"`）与 `hand_distilled_paths`（脱敏后的相对路径列表），在 `sessions.metadata` 同步记录末次手工沉淀摘要与路径。
- **取舍理由**：`distill_mode` 不参与 SQL 优先级排序，只在入队幂等判定与 P03 计划过滤时按行读取；复用已有 JSON 载荷实现零迁移、零回滚风险，且前端与审计日志均可直接透出。

### 决策 B：`hand_distilled` 在 P02–P04 中的作用点

- **备选 1（仅在 P03 Prompt 中口头嘱咐模型不产出 `knowledge_doc`）**：LLM 属不可信输出，仍可能误产 `knowledge_doc` 导致重复落盘。
- **备选 2（采用：Prompt 提示 + P03 确定性硬过滤双保险）**：
  1. 在 P03 输入元数据中告知规划器某会话已手工落盘（减少无效 Token 生成）；
  2. 在 `core/pipeline/coordinator.py` 的 P03 后处理中执行**确定性硬过滤**：若某 `knowledge_doc` 的 `source_session_keys` 非空且全部属于本批 `governance_only` 会话，则强制移除该项并写 `governance_only_wiki_skipped` 审计事件；当 `source_session_keys` 为空且本批所有准入会话均为 `governance_only` 时，同样过滤全部 `knowledge_doc`。
- **取舍理由**：遵守本仓“LLM 输出不可信，确定性门禁在 P03/P04 兜底”的架构护栏，确保 100% 不会重复写 Wiki，同时不影响 `flywheel_rule`、`skill_improvement` 与错题/分歧入库。

### 决策 C：定时任务防休眠漏跑的检测方式

- **备选 1（仅调大 APScheduler `misfire_grace_time`）**：进程一直在跑且仅宿主机短暂休眠时有效；但如果服务在 20:00 时恰好未启动或刚在 20:30 重启，APScheduler 内存作业不会知道 20:00 被漏掉了。
- **备选 2（采用：调大 `misfire_grace_seconds` + 巡检/启动态漏跑补跑）**：
  - APScheduler 侧将 `misfire_grace_time` 绑定到配置项 `workflow.misfire_grace_seconds`（默认 `14400` 秒 = 4 小时）；
  - `AutomationManager` 新增 `check_missed_schedule_catchup(now=None)`（可注入时钟便于单测）：计算当日（上海时区）应触发时间 `scheduled_today`，当 `scheduled_today <= now <= scheduled_today + misfire_grace_seconds`、`today_scheduled_ran == False` 且存在待处理队列/待审会话时，每日仅一次写入 `schedule_misfire_detected` 审计事件；若 `catchup_missed_schedule == True` 且 `role_active == True`，尝试触发一次 `start_run(trigger="scheduled", phase="auto")`（遇锁忙则跳过并留日志）。
  - 在 `app.py` 的 `recovery` 巡检循环内低频调用（复用已有守护线程，不新增额外线程）。
- **取舍理由**：同时覆盖“进程存活但机器休眠”和“错峰重启进程”两种真实桌面场景，且完全在 `serve` 角色约束内运行。

### 决策 D：受控写入子代理碎片识别边界

- 公开仓严禁硬编码私有项目路径或个人 Agent 名称，因此识别规则保持通用化：
  1. 既有 `is_subagent == True` 且带 `parent_session_key` 的记录继续由自动批次认领跳过；
  2. 在 `triage.py` 增加通用受控写入子代理特征检测（例如无交互单轮写入派单或命中用户配置的 `exclude_content_prefixes`），在入队与 `re_triage_queue` 时统一标记 `excluded (SUBAGENT_FRAGMENT)`。

## 3. 跨层契约核对

- 新增配置键 `workflow.misfire_grace_seconds`、`workflow.catchup_missed_schedule` → 同步更新 `config.py` 与 `flywheel.yaml.example`。
- MCP 工具总数维持 7 个不变（扩展现有 `flywheel_session_intent` 与 `flywheel_ingest_session` 可选参数，不破坏既有客户端与单测工具清单断言）。
- 审计事件新增 `governance_only_wiki_skipped`、`schedule_misfire_detected`，遵循通用 `audit_events` 表结构，不破坏既有枚举。
- 健康检查 `/health` 与 `/api/site/automations` 仅增补只读字段 `started_at` / `missed_today` / `misfire_grace_seconds` / `catchup_missed_schedule`，向后兼容前端现有消费逻辑。

## 4. 真实验证方式

1. **自动化回归（`test`）**：
   - `tests/test_distill_queue.py`：覆盖 `hand_distilled`（`governance_only=True/False`）、归档同步不覆盖 `governance_only`、`structured_brief` 脱敏透传、受控写入子代理重筛排除；
   - `tests/test_workflow_orchestration.py`：覆盖纯 `governance_only` 批次与混合批次的 P03 硬过滤、审计事件与错题/规则正常落库；
   - `tests/test_automation_console.py`：通过可注入时钟覆盖休眠漏跑检测、自动补跑一次、关闭补跑仅告警与 `/health` `started_at` 字段；
   - `python scripts/verify_repo.py --frontend` 全绿（卫生扫描、OpenSpec 严格校验、pytest、前端构建）。
2. **真实环境验证（`real`）**：
   - 以 `serve` 角色重启本地服务，核验 `/health` 返回新 `started_at`、`GET /api/site/automations` 返回 `misfire_grace_seconds` 与 `missed_today`；
   - 通过真实 MCP 调用 `flywheel_session_intent(action="hand_distilled", ...)`，在沙箱触发一次 P00–P07 跑批，确认知识库零重复写入且错题/规则通道正常工作。
