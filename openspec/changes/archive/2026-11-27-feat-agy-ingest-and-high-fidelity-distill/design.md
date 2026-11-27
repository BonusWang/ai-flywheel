# Design: Antigravity 会话原生采集与高保真沉淀流水线（P1）

## 1. 设计目标与并行拆解边界

P1 围绕“补齐 Antigravity 客户端采集”与“将飞轮自动沉淀质量追平手工四域+多事件标准”两条互不重叠的主线展开，可直接拆分为两个文件集完全正交的并行实现单元：

- **单元 2A（AGY 原生解析与前端可观测）**：`adapters/sessions.py`、`core/triage.py`、`core/queue_service.py`、`frontend/src/types.ts`、`frontend/src/pages/AutomationsPage.tsx`、`tests/test_agy_sessions.py`。
- **单元 2B（P02/P03 高保真输入、阶段模型分层、多事件与 `incident` 受控路由）**：`config.py`、`flywheel.yaml.example`、`models.py`、`adapters/runners/*`、`adapters/wiki/router.py`、`adapters/wiki/gate.py`、`adapters/wiki/t5_adapter.py`、`core/pipeline/coordinator.py`、`core/pipeline/helpers.py`、以及对应回归测试。

## 2. 关键设计决策与取舍

### 决策 A：Antigravity `transcript.jsonl` 解析策略

- **结构特征**：Antigravity 将每次对话持久化在 `<brain>/<uuid>/.system_generated/logs/transcript.jsonl`，每行 JSON 含 `type`（`USER_INPUT` / `PLANNER_RESPONSE` / `GENERIC` / `CHECKPOINT` 等）、`source`、`content`、`created_at`。
- **取舍**：在 `adapters/sessions.py` 的 `_extract_jsonl_text` 与 `core/triage.py` 的 `extract_jsonl_metadata` 中新增精确识别分支：仅当行含 `step_index` 或 `type` 属于 Antigravity 枚举集时启用该分支，严格跳过 `GENERIC` 工具输出，剥离 `<USER_REQUEST>` 与 `<ADDITIONAL_METADATA>` 包装后再产出 `[user]` / `[assistant]` 文本，绝不影响 Codex / Claude Code 的既有解析路径。
- **静默窗口保护**：Antigravity 没有类似 Codex `archived_sessions/` 的归档目录，因此扫盘时通过文件 `mtime` 与当前时间之差判断是否超过 `scan_quiet_window_seconds`（默认 `900` 秒）；仅针对 `antigravity` 客户端生效（或在配置源上显式配置），避免半截活跃对话被提前入队。

### 决策 B：P02 高保真输入预算与 `structured_brief` 置顶

- **现状缺陷**：硬编码 `P02_SESSION_TEXT_LIMIT = 3000` 在 16k–20k 字符的真实排查会话中丢掉 50%–100% 的核心表名和报错根因。
- **方案**：
  - 将 `excerpt_session_text(text, limit=...)` 的默认上限提高并绑定 `cfg.runner.api.p02_session_char_limit`（默认 `16000`，仍保留头尾拼接防超长溢出）；
  - 当队列项带有 `payload["structured_brief"]` 时，在送审文本顶部前置 `[structured_brief]` JSON 块，保证主 Agent 主动归拢的表名、配置、根因与决策零截断进入 P02；
  - 保留向后兼容：未传 `session_char_limit` 时函数默认值保持安全且可通过参数覆盖，不破坏现有单测。

### 决策 C：按阶段模型分层（P02 Review / P03 Plan）

- **方案**：在 `ApiRunnerConfig` 中增加四个可选字段：`review_model`、`review_reasoning_effort`、`plan_model`、`plan_reasoning_effort`（全部默认空字符串 `""`）。
- **解析逻辑**：`OpenAICompatibleRunner._resolve_stage_api_params(stage)`：
  - `stage == "p02_review"` → 取 `review_model or model`、`review_reasoning_effort or reasoning_effort`；
  - `stage == "p03_plan"` → 取 `plan_model or model`、`plan_reasoning_effort or reasoning_effort`；
  - `_cache_file(stage, prompt)` 将阶段实际生效的 `(effective_model, effective_effort)` 纳入 `canonical_hash`；
  - `_call_chat_completion(prompt, system_prompt)` 保持既有双参签名（供沙箱 stub 与既有 monkeypatch 零改动兼容），通过实例级当前调用上下文属性 `_active_stage_model` / `_active_stage_effort` 传递阶段覆盖并在 `finally` 中重置。

### 决策 D：`incident` 受控路由与强制敏感审批

- **安全边界**：事故台账属于高敏感资产，遵循“默认关闭、配置显式开放前缀 + 必须人工审批”双闸门：
  1. 只有挂载 Wiki 的 `.wiki-schema.md` 声明了 `incident` 类型 **且** `knowledge_sink.allowed_write_prefixes` 显式包含该分区前缀时，`incident` 才出现在 `routing_candidates.page_types`；
  2. `models.is_sensitive_write()` 检查 `planned_write` 的 `wiki_route.page_type == "incident"` 或路径命中事故目录或 `payload.get("page_type") == "incident"`，强制返回 `True`；同步保持 `core/feishu_card.py` 调用同一判定源（单点权威）。

### 决策 E：领域页与主卡片名额分离

- `WorkflowConfig` 新增 `max_domain_writes_per_run: int = Field(default=8, ge=0, le=32)`。
- 在 `outcomes.py` 对模型返回的计划项截断时：非领域页（主经验卡、规则、Skill、事故卡）计数上限取 `max_writes_per_run`，领域增量页（`deposit_routing` 命中的表/配置/架构/算法页）单独计入 `max_domain_writes_per_run`，两者合并后的总物理路径仍严格受 `knowledge_sink.max_write_set` 硬上限与 `high_write_count_threshold` 审批阈值约束。

## 3. 真实验证方式

1. **自动化回归（`test`）**：
   - `tests/test_agy_sessions.py`：构造含 `GENERIC` 噪声与 `<USER_REQUEST>` 标签的 Antigravity `transcript.jsonl`，验证解析、身份归一化、标题提取、静默窗口与入队；
   - `tests/test_p02_review_contract.py` & `tests/test_p03_plan_contract.py`：验证 16000 字高保真预算、`structured_brief` 置顶、阶段模型/推理强度切换与缓存键隔离、`events` 归一化、领域页独立预算；
   - `tests/test_wiki_route_declaration.py` & `tests/test_arch_invariants.py`：验证 `incident` 路由正反例、`is_sensitive_write` 强制敏感与飞书卡片一致性、示例配置与前端类型构建一致；
   - `python scripts/verify_repo.py --frontend` 全绿。
2. **真实样本回放验证（`real`）**：
   - 使用脱敏/沙箱隔离目录对真实长排查会话（>16k 字符）执行 AGY 解析与 P02 输入构建回放，量化验证关键表名与根因召回率从 50% 提升至 100%，并验证重启 `serve` 后所有接口与前端正常。
