# Design: 实时异常线索处理语义冲突修复与转错题观察池闭环

## 1. 队列关联判定（零 N+1 查询）

- 在 `storage/db/queue_repo.py` 新增 `list_active_queue_session_keys(self) -> set[str]`：
  - 执行 `SELECT DISTINCT session_key FROM distill_queue WHERE status IN ('queued', 'processing')`。
- 在 `routes/governance.py` 的 `GET /api/site/retro` 中调用一次 `active_queue_keys = rt.db.list_active_queue_session_keys()`，为每条线索写入：
  - `has_queued_session = bool(x.session_key and x.session_key in active_queue_keys)`
  - `settlement_mode = "auto_next_batch" if has_queued_session else "manual_only"`

## 2. 线索建议语义对齐（`core/retro_advisory.py`）

- 对 `verdict == "keep"`（`missing_evidence` / `rule_recurrence` / 命中实证或纠偏关键词）：
  - `recommended_action = "promote"`
  - `reason = "命中实证/权威源纠偏信号，可点击「转入错题观察池」直接沉淀，或保持待处理随下次批次自动带入。"`
- 对 `verdict == "review"`（`high_risk_tool` / `retry_loop` / `blocker` / 其他）：
  - 保留 `verdict = "review"`，说明确认无复用价值可直接标「已人工处理」或「忽略」，有复用价值可「转入错题观察池」。

## 3. 转入错题观察池接口（`POST /api/site/retro/promote`）

- 请求体 `RetroPromoteBody`：`retro_ids: list[str]`、`severity_grade: str | None = None`。
- 处理逻辑：
  1. 过滤有效 `retro_ids` 并读取对应 `RetroItem`（若全部不存在返回 HTTP 404）；
  2. 取最新一条作为主线索 `primary`，构造：
     - `root_cause_key`：若 `primary.rule_id` 存在取 `f"retro-rule-{primary.rule_id}"`，否则基于 `build_retro_group_key(primary)` 的稳定短哈希 `f"retro-{kind}-{sha[:10]}"`；
     - `title`：`normalize_retro_summary(primary.summary)[:80]`；
     - `severity_grade`：若入参合法则用入参，否则当 `primary.kind in {RULE_RECURRENCE, MISSING_EVIDENCE}` 或 `primary.severity == "high"` 时取 `"red_silent"`，其余取 `"yellow_caught"`；
     - `phenomenon`：`primary.summary`；
     - `truth`：`f"来源实时异常线索（{primary.kind.value}），需核查根因并固化验证/边界规则"`；
     - `thinking_root_cause`：复用 `advise_retro_item(primary)["reason"]`；
     - `caught_by`：`"tool"`；
     - `session_key`：`primary.session_key`。
  3. 调用 `rt.db.upsert_mistake_observation(...)` 落库观察错题；
  4. 调用 `rt.db.bulk_update_retro_status(found_ids, RetroStatus.RESOLVED)` 将对应线索全部结案；
  5. 写入 `audit_events`（`entity_type="retro_queue"`、`entity_id=primary.retro_id`、`event_type="retro_promoted_to_observation"`、`actor="portal"`）并调用 `rt.refresh_core_card()`；
  6. 返回 `{"observation": obs, "created": created, "resolved_count": updated, "retro_ids": found_ids}`。

## 4. 前端交互更新（`CluesObservationsTab.tsx`）

- `types.ts`：`RetroItem` 增加 `has_queued_session?: boolean` 与 `settlement_mode?: 'auto_next_batch' | 'manual_only'`；`RetroAdvisory.recommended_action` 扩展为 `'resolved' | 'dismissed' | 'promote'`。
- `api.ts`：新增 `promoteRetros(retroIds: string[])`。
- `CluesObservationsTab.tsx`：
  - 顶部说明明确解释自动结算条件与三个操作去向；
  - 每组待处理线索增加结算状态徽标：`随下次沉淀自动结算`（组内任一条 `has_queued_session === true`）或 `无关联待审会话`；
  - 高价值（`keep`）线索主按钮为「转入错题观察池」，次按钮为「已人工处理」（hover 提示「仅标记已处理并结案，不再送入下次沉淀」）和「忽略」；
  - 普通 `review` 线索同时提供「转入错题观察池」「已人工处理」「忽略」；`ignore` 噪音线索主按钮为「忽略 / 整组忽略」；
  - 多选操作条增加「批量转入错题池」。
  - `RulesRetroPage.tsx` 保持行数 < 300 约束（无需改动或零增行）。

## 5. 备选方案与取舍

- 备选：把 `resolved` 状态本身改成“自动喂给 P02”。
- 放弃原因：`resolved` 在 P05 结算与既有契约里代表“已结案退出待处理队列”，改变它的语义会破坏 P05 幂等结算与历史视图；新增显式「转入错题观察池」动作既保持 `open/resolved/dismissed` 状态机纯洁，又给高价值和孤立线索提供真正的沉淀通道。

## 6. 真实验证方式

1. 自动化测试覆盖 `GET /api/site/retro` 的 `has_queued_session` / `settlement_mode` 区分、`advise_retro_item` 的 `recommended_action == "promote"`、`POST /api/site/retro/promote` 写入 `mistake_observations` + 结案线索 + 审计记录，以及 `EXPECTED_ROUTES` 路由清单更新（67）。
2. 真实运行验证：在临时拷贝的本地真实 `flywheel.db` 上通过 FastAPI `TestClient` 校验现有 23 条真实 `open` 线索的 `auto_next_batch` vs `manual_only` 分类，并对一条孤立高价值线索实测 `POST /api/site/retro/promote` 完整闭环，再跑通 `python scripts/verify_repo.py --frontend`。
