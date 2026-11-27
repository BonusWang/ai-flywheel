# 实施证据：feat-sediment-dual-track-and-scheduler-catchup

> 等级：**test** 自动化测试 · **static** 读代码 · **real** 真实运行 · **unknown** 未验证。
> 任务 1–5 与任务 6 的全门禁为 **test** 级；任务 6 的 real 项尚未执行。
> 第 5 节是不可复现的本机只读核对，只记计数。

## 1. 状态

| 任务 | 状态 | 等级 | 支撑测试 |
|---|---|---|---|
| 1 配置默认值 | 完成 | test | `tests/test_automation_console.py::test_misfire_grace_and_catch_up_come_with_safe_defaults_and_a_lower_bound`、`tests/test_arch_invariants.py::test_the_example_config_matches_the_workflow_schedule_defaults` |
| 2 `hand_distilled` 与 `structured_brief` | 完成 | test | `tests/test_distill_queue.py` 第 9.1–9.3 节，7 条 |
| 3 受控写入派单拦截 | 完成 | test | `tests/test_distill_queue.py` 第 9.4–9.6 节，10 条 |
| 4 P03 `governance_only` 确定性过滤 | 完成 | test | `tests/test_workflow_orchestration.py`，10 条 |
| 5 调度漏跑补跑与可观测 | 完成 | test | `tests/test_automation_console.py` 新增 21 条（30 个用例）、`tests/test_arch_invariants.py::test_only_the_serve_role_checks_for_a_missed_schedule_and_health_says_when_it_started` |
| 6 全门禁 `scripts/verify_repo.py --frontend` | 通过 | test | 见下表 |
| 6 真实 `serve` 冒烟 | **未完成** | unknown | 见第 3 节 |

全门禁在勾选任务 1–5 之后的工作树上运行（2026-11-28），四项检查全过：

| 检查 | 结果 |
|---|---|
| hygiene | PASS |
| openspec | PASS |
| pytest | PASS，690 passed（改动前 630） |
| frontend | PASS |

## 2. 规格场景 → 测试

三个能力规格共 9 个场景，每个场景有一条以它命名的测试；下表以外的测试覆盖各自的反例与边界。

| 规格 | 场景 | 测试 |
|---|---|---|
| distill-queue-pipeline | 手工沉淀后回调 hand_distilled 进入治理专属队列 | `test_hand_distilled_queues_a_governance_only_item_with_its_paths_and_an_audit_trail` |
| distill-queue-pipeline | 被动归档同步不覆盖已登记的 hand_distilled 模式 | `test_archive_sync_never_demotes_a_hand_distilled_session_back_to_a_plain_item` |
| distill-queue-pipeline | 通过 MCP 传入结构化摘要随队列项持久化 | `test_ingest_session_persists_a_redacted_structured_brief_and_counts_it_into_the_hash` |
| distill-queue-pipeline | 存量受控写入子代理排队项在重筛后移出队列 | `test_re_triage_moves_a_legacy_controlled_write_subagent_item_out_of_the_queue` |
| workflow-orchestration | governance_only 会话不重复写 Wiki 但正常产出规则与错题 | `test_a_hand_distilled_session_yields_rules_and_observations_but_no_second_document` |
| workflow-orchestration | 混合批次中普通会话的知识文档照常生成 | `test_in_a_mixed_batch_the_ordinary_sessions_document_is_still_written` |
| automation-console | 宿主机休眠跨过 20:00 后在宽限窗口内自动检测并补跑 | `test_a_lost_fire_is_recorded_and_caught_up_once_a_day` |
| automation-console | 关闭自动补跑时仅记录漏跑告警不自动触发 | `test_a_lost_fire_is_only_recorded_when_catch_up_is_off` |
| automation-console | 健康检查与自动化快照返回服务启动时间 | `test_health_and_the_automation_snapshot_say_when_the_process_started` |

前四条在 `tests/test_distill_queue.py`，中间两条在 `tests/test_workflow_orchestration.py`，后三条在 `tests/test_automation_console.py`。

测试规模：新增 60 个用例（`test_automation_console.py` +30、`test_workflow_orchestration.py` +10、`test_distill_queue.py` +17、`test_arch_invariants.py` +3）。

## 3. 尚未验证（任务 6，real）

- 真实 `serve` 进程上的 `/health.started_at` 与 `/api/site/automations` 的 `scheduler` 新字段。
- 真实 MCP 客户端调用 `flywheel_session_intent(action="hand_distilled")`。
- 沙箱 P00–P07 端到端（真实模型）。
- 真实休眠唤醒后的 `misfire_grace_seconds` 生效。

## 4. 实现中的取舍（static，需在 Verify 时知悉）

- **补跑批次跑在独立的一次性守护线程上**，不占用恢复巡检线程；规格原文是「不新增额外线程」，指恢复循环本身复用。
- **漏跑检查的节奏**是恢复循环的第一轮及其后每 300 秒（`CATCHUP_CHECK_INTERVAL_SECONDS`）。
- **漏跑审计只在宽限窗口内且有积压时落盘**；服务整晚不在的情况只会留下「今天没跑」的事实。
- **补跑批次的阶段按补跑当天解析**（`phase="auto"`）。示例配置（20:00 触发、宽限 4 小时）下窗口几乎整段落在触发当天；触发时刻加宽限期跨过午夜时，周五漏掉的批次在次日补跑只含日常沉淀，不含周复盘。
- **派单规则只读用户自己的那一轮指令**：编排器转达的消息不算对话，也不打断「只有一条派单」。只有转达消息、没有派单正文的会话不会被这条规则识别，交给其余初筛规则。
- **`controlled_write_markers` 默认为空**：不配置时规则完全依赖结构特征（批准用语、`write-set`、契约标记、简报篇幅）。
- **找不到父会话的子代理记录一律按碎片排除**，不看内容是不是派单；此前只有文件名命中的才算。规格括注里的「标记 `is_subagent = true` 且无独立业务问题上下文」按「没有父会话」判定。
- **治理专属声明的路径只增不减**：第二次声明并入第一次的路径；要重新开始只能显式 `enqueue`。
- **P04 有一道与 P03 相同的过滤**，覆盖「批次等待审批期间会话被声明为 hand_distilled」的情况，超出规格文字。

## 5. 派单规则与本机队列的只读核对（real，仓库不可复现）

规则定稿前后对本机数据做过两次只读核对。素材是私有会话与本地数据库，这里只记计数，不含任何内容。

历史会话上的规则校准：

| 项 | 计数 |
|---|---|
| 已渲染的会话 | 95（主会话 87，带子代理标记 8） |
| 规则命中 | 8，全部是带子代理标记的会话 |
| 主会话误报 | 0 |
| 含编排器转达消息的会话 | 3，均在命中之内；把转达算作对话会丢掉这 3 条 |
| 只有转达、没有派单正文的会话 | 0 |

按新规则对真实队列做一次「如果现在重筛」的估算（2026-11-28）：

| 项 | 计数 |
|---|---|
| 排队中的队列项 | 157（普通 81，子代理 76；子代理项全部有父会话） |
| 重筛会按派单移出的子代理项 | 5 |
| 标记为子代理但找不到父会话的记录 | 0（队列项与会话都是 0） |

队列里另有 6 条普通项的源文件已不存在，按队列保存的文本判断，均不命中派单规则。这是估算，不是重筛本身：真正的结果以控制台「重筛」的预览为准。
