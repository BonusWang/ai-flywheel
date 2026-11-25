# Design: 沉淀队列机制、双端归档触发与 Workflows 库存看板

## 核心架构决策

### 1. 统一入队状态机（`core/queue_service.py`）
所有来源（Codex 归档目录扫描、Claude Code `SessionEnd`/`SessionStart` Hook、MCP `flywheel_session_intent`/`flywheel_ingest_session`、控制台手工补录）全部收敛到 `DistillQueueService`，保证单一行为真源：
- `on_explicit_enqueue(...)` → 补齐/更新 `sessions` 主档，跳过噪音过滤，写入 `priority="P0_CORE"`、`status="queued"`。
- `on_archive(...)` → 刷新主档，依次过三级闸门：
  1. `opt_out`（主档已封禁或文本/标签命中 `#no-flywheel` / `本会话不沉淀`）→ `excluded(USER_OPT_OUT)`；
  2. 内容指纹比对 `last_consumed_hash` → 若完全无增量且无未消费项，跳过不制造重复库存（或记录无增量）；
  3. 确定性规则筛查（复用 `core/triage.py` `triage_session`，将原字符串 reason 映射为标准 `exclude_reason` 码值）→ 不通过则 `excluded(<CODE>)`；通过则先执行 **0-token 瘦身**，再按 `queue_max_item_chars` 判断单条或拆分为 `chunk 1/N .. N/N` 进入 `queued`。
- `on_withdraw(...)` → 将该 `session_key` 当前处于 `queued` 的所有项置为 `withdrawn`（不动 `processing` / `processed` 项）。
- `on_opt_out(...)` → 主档 `opt_out = 1`，并将当前所有 `queued` / `withdrawn` 项置为 `excluded(USER_OPT_OUT)`。

### 2. 超大上下文瘦身与分片算法（0-token 确定性）
- **解析轮次**：将 JSONL 或纯文本按 `[user]` / `[assistant]`（或双换行段落）拆分为带序号的 turns 列表，记录 `total_turns` 与 `total_chars`。
- **增量切片**：若会话主档 `last_consumed_turn > 0` 且 `total_turns > last_consumed_turn`，仅取 `[last_consumed_turn:]` 增量区间参与本次入队。
- **0-token 结构化瘦身**：对每条 turn，保留完整 `[user]` 文本与关键报错/结论行；对超长工具输出/代码块/日志行（单条 assistant/tool 块 > 2500 字符），保留头部 1200 字符 + 中间折叠摘要标记 + 尾部 800 字符。
- **按轮次分片**：瘦身后若总长仍 `> queue_max_item_chars`（默认 24000），按轮次边界顺序切分，每片不超过阈值且相邻片保留最后 1 轮重叠上下文，写入 `chunk_index = 1..N, chunk_total = N, is_oversized = 1`，并在 `payload` 中存 `turn_start / turn_end`（或压缩后文本切片）以便 P01/P02 精准加载不丢尾。

### 3. P00–P07 流水线与既有测试的无缝兼容
- 保持 `WorkflowOrchestrator.start_run(..., session_keys=..., queue_ids=...)` 向后兼容：
  - 若显式传 `queue_ids`：直接按 `queue_ids` 认领 `distill_queue` 中的对应项（允许插队）；
  - 若显式传 `session_keys`：保持既有行为直接取对应会话，并同步更新关联的 `queued` 项（若存在）；
  - 若均未传（常规自动/手动批次）：`P00_PREPARE` 先调用 `reconcile_codex_archives()` 同步归档目录，并对既有 `sessions(status='pending')` 中尚未建立队列项的历史/手工会话做幂等桥接补入 `distill_queue`，随后 `P01_BEGIN` 按优先级从 `distill_queue` 认领 `queued` 项（上限 `max_sessions_per_run` 与 `max_chars_per_run`）。
- 结算契约：
  - `P05_SETTLE`：已审查项 → `processed`（更新 `completed_at`、`sessions.last_consumed_turn / last_consumed_hash`、`sessions.status = processed`）；未审查项 → 退回 `queued`（清空 `run_id`）。
  - `_fail_run`：认领项 `retry_count + 1`，若 `>= max_retries` 转 `dead_letter`，否则退回 `queued`。
  - `reject_run`（`CANCELLED`）：认领项安全退回 `queued`，**不递增 `retry_count`**。

### 4. `serve` 角色后台归档对账守护
- 严格遵守 `AGENTS.md` Guardrail #7：仅在 `effective_role == "serve"` 时于 `app.py` lifespan 启动轻量后台轮询（可并入独立 daemon 线程 `flywheel-archive-sync`，使用 `threading.Event.wait(interval)`，停机时 `set()` 立即退出零残留），默认每 60s 调用一次 `rt.queue.reconcile_codex_archives()`，并发布 `queue_updated` SSE 事件。

### 5. 前端 Workflows 页设计（保持 5+1 导航精简）
- 顶部常驻 **库存与进度汇总条**（待处理 Lag、处理中、已沉淀、已排除、已撤回、死信 + 各接入工具分片计数 + 预计清仓批次数 + 「同步归档」按钮）。
- 导航下方提供清晰双 Tab：**「沉淀队列（库存）」**与**「执行流水线（P00–P07）」**（有待审批批次时默认停在流水线或高亮提示；点击已沉淀队列项上的 `run_id` 徽章可一键切到流水线 Tab 并展开该批次）。
- 队列 Tab 消费 `/api/site/dicts` 映射全部状态、通道与排除原因中文标签（带 `labels.ts` 本地降级兜底）。

## 真实验证方式（冒烟流程）
1. 启动真实 `flywheel serve`（`process_role="serve"`）临时实例，配置临时 Codex `archived_sessions` 目录。
2. 实测 **Codex 严格归档闭环**：放入活跃目录不入队 → 移入 `archived_sessions` 后同步入 `queued` → 移出后同步自动 `withdrawn` → 扩写内容再移入生成 `rev 2`。
3. 实测 **超大上下文**：归档一个 > 60k 字符的多轮对话，确认自动拆分为 `chunk 1/N..N/N` 且末尾片完整保留最终结论。
4. 实测 **Claude Code Hook + MCP 对齐**：发 `UserPromptSubmit` 验证响应头含 `session=claude-code:...` → 发 `Stop` 验证不入队 → 发 `SessionEnd` 验证入 `queued` → 发 `SessionStart(source="resume")` 验证转 `withdrawn` → 调 MCP `flywheel_session_intent(action="opt_out")` 验证永久排除。
5. 实测 **队列驱动流水线消费**：通过 `POST /api/site/runs`（指定 `queue_ids`）跑完 P00–P07（`dry_run` + `auto_approve=true`），核验 `GET /api/site/queue` 库存清零、`processed` 绑定真实 `run_id`、`GET /api/site/queue/{queue_id}/events` 返回完整状态变迁轨迹。
6. 执行 `python scripts/verify_repo.py --frontend` 全绿。
