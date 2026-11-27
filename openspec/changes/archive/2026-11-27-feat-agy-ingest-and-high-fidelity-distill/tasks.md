# Tasks: feat-agy-ingest-and-high-fidelity-distill

- [x] 1. 在 `adapters/sessions.py`、`core/triage.py` 与 `core/queue_service.py` 中实现 Antigravity `transcript.jsonl` 原生解析（映射 `USER_INPUT`/`PLANNER_RESPONSE`、跳过 `GENERIC`/`CHECKPOINT`/`SYSTEM`、剥离包装标签提取标题、归一化 `antigravity:<uuid>`）及活跃会话静默窗口保护，并新增 `tests/test_agy_sessions.py` 回归测试【test】
- [x] 2. 在 `frontend/src/types.ts` 与 `frontend/src/pages/AutomationsPage.tsx` 中补齐 P0 新增调度可观测字段（`started_at`、`misfire_grace_seconds`、`catchup_missed_schedule`、`missed_today`）并展示漏跑提醒与启动时间，确保前端 `npm run build` 通过【test】
- [x] 3. 在 `config.py`、`flywheel.yaml.example` 与 `adapters/runners/clients.py` 中新增 `p02_session_char_limit`（默认 `16000`）、阶段模型覆盖（`review_model`、`review_reasoning_effort`、`plan_model`、`plan_reasoning_effort`）及 `workflow.max_domain_writes_per_run`（默认 `8`），确保阶段模型参数进入 `api_cache` 缓存键且保持 `_call_chat_completion(prompt, system_prompt)` 签名兼容【test】
- [x] 4. 在 `adapters/runners/prompts.py`、`normalizers.py`、`outcomes.py` 与 `core/pipeline/coordinator.py` 中实现 P02 高保真输入（可配置预算 + `structured_brief` 置顶注入 + 可选 `events` 拆解）、解除 P03 `220-450 chars` 浅摘要限制、注入多事件分卡约束并落实领域页独立预算计数【test】
- [x] 5. 在 `adapters/wiki/router.py`、`gate.py`、`t5_adapter.py`、`models.py` 与 `core/pipeline/helpers.py` 中实现 Schema 治理 Wiki 的受控 `incident` 路由（仅当 `.wiki-schema.md` 声明且 `allowed_write_prefixes` 放行时启用），并在 `is_sensitive_write()` 与飞书卡片中统一强制判为敏感待审批【test】
- [x] 6. 运行 `python scripts/verify_repo.py --frontend` 全门禁验收，并使用真实长排查会话在沙箱中完成 AGY 解析与高保真 P02 输入召回率对比验证及 `serve` 冒烟【real】
