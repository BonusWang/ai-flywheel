# Design: 知识笔记保鲜期偏自动化退场与台账兜底复核

## 1. 架构边界与取舍

- **唯一写入边界**：自动退场与控制台兜底操作**严格只修改飞轮 SQLite 台账（`knowledge_card_metrics`）并写 `audit_events`**，绝不写回或删除挂载知识库（`E:/wiki` 或 `builtin_kb`）的 `.md` 源文件。
- **备选方案与取舍**：
  - 备选：手动点「确认有效」时通过 Wiki 门禁回写 `.md` 的 frontmatter `last_verified`。
  - 放弃原因：维护者已确认偏自动化与引擎/知识库解耦边界——飞轮台账归引擎管，知识库原文归人/P04 管；不回写源文件避免了意外 Git diff 与门禁阻塞，且源文件未来若真的更新了 `last_verified`，`sync_fts_index` 会自动重新激活台账。

## 2. `adapters/knowledge.py` FTS 同步规则

在 `sync_fts_index()` 中读取 `doc.get("status")`（已在 `_search_documents` 解析）：
1. `doc_status = str(doc.get("status") or "").strip().lower()`
2. 若 `doc_status in {"archived", "superseded"}`：
   - 无论 `last_verified` 是否存在或变化，只要 `prev` 缺失或 `prev["freshness_state"] != doc_status`，即调用 `upsert_knowledge_freshness(..., freshness_state=doc_status)`。
3. 若 `doc_status` 非归档态：
   - 若 `prev` 存在且 `prev["freshness_state"] in {"archived", "superseded"}`：只有当 `last_v` 非空且 `last_v > str(prev.get("last_verified_at") or "")` 时才重新激活为 `"fresh"`；否则跳过，保留台账归档决定。
   - 其他情况沿用现有规则（`prev` 不存在或 `last_v` 更新时刷新为 `"fresh"`）。

## 3. `knowledge_repo.py` 与 `core/dream.py` 自动退场

- 在 `KnowledgeRepoMixin` 新增：
  - `list_stale_zero_help_cards(today_ymd: str, *, grace_days: int = 30) -> list[dict[str, Any]]`（严格只读）：计算 `cutoff_ymd = (date.fromisoformat(today_ymd) - timedelta(days=grace_days)).isoformat()`，查询 `valid_until != '' AND valid_until < cutoff_ymd AND COALESCE(helped_count, 0) = 0 AND freshness_state NOT IN ('archived', 'superseded')`。
  - `auto_archive_stale_zero_help_cards(today_ymd: str, *, grace_days: int = 30) -> list[dict[str, Any]]`：更新上述卡片为 `freshness_state = 'archived'` 并返回被退场清单。
  - `update_knowledge_freshness_action(doc_path: str, action: str, *, today_ymd: str, freshness_tier: str | None = None) -> dict[str, Any]`：统一封装 `verify` / `archive` / `restore` / `set_tier` 原子更新，返回更新后的完整新鲜度记录。
- 在 `core/dream.py`：
  - `preview_dream_consolidation(..., stale_card_grace_days: int = 30)` 新增返回 `auto_archived_cards`（调用只读查询）；
  - `run_dream_consolidation(..., stale_card_grace_days: int = 30)` 在 Tier 1 先执行 `auto_archive_stale_zero_help_cards`（若数量 > 0 写入 `audit_events` `event_type="knowledge_freshness_auto_archived"`），再执行 `mark_expired_knowledge_cards(today)`，并返回 `auto_archived_cards` 及更新审计摘要。

## 4. API 与前端交互

- 后端路由 `routes/governance.py` 新增：
  - `POST /api/site/knowledge/freshness`：请求体 `{ doc_path: str, action: "verify" | "archive" | "restore" | "set_tier", freshness_tier?: "volatile" | "evolving" | "evergreen" }`；校验 `doc_path` 非空及合法 `action`/`tier`，调用 `rt.db.update_knowledge_freshness_action(...)`，写入 `audit_events`（`entity_type="knowledge_card"`、`actor="portal"`），调用 `rt.refresh_core_card()`，返回 `{ card, expired_total }`。
- 前端：
  - `types.ts` & `api.ts`：新增 `KnowledgeFreshnessAction`、`api.updateKnowledgeFreshness(...)`、`DreamReport.auto_archived_cards?: Array<...>`。
  - `DocumentReaderModal.tsx`：新增可选 `freshness`（当前文档的 `freshness_tier` / `valid_until` / `freshness_state` / `is_expired`）与 `onFreshnessUpdated` 属性；传入时在正文上方展示一行紧凑台账工具条（当前状态徽标 + 「确认仍有效（顺延）」「移出待复核（归档）」/「恢复保鲜跟踪」+ 档位切换下拉 + 小字注记「仅更新飞轮台账，不改动知识库源文件」）。
  - `AssetsFreshnessTab.tsx`：`docFilter` 增加 `'archived'`（「已归档」），条目区分展示「待复核」「有效」「已归档」徽标，并将 `freshness` 与刷新回调传给 `DocumentReaderModal`（由 `RulesRetroPage` 传入 `onChanged`）。
  - `AutomationsPage.tsx`：周五蒸馏卡片预览/结果若包含 `auto_archived_cards` 时一并展示自动退场卡片计数。

## 5. 真实验证方式

1. 单元测试覆盖 `sync_fts_index` 归档同步与防误唤醒、Dream 干跑/实跑超期零助益自动退场与有助益保护、`POST /api/site/knowledge/freshness` 全部 4 种动作与源文件零字节变化。
2. 真实运行验证：在临时拷贝的本地真实 `flywheel.db` 上跑 `preview_dream_consolidation` 核实超期 30 天零助益卡片识别结果，并通过 FastAPI `TestClient` 验证端到端 `GET /api/site/knowledge → POST /api/site/knowledge/freshness → GET /api/site/knowledge` 计数变化与 `verify_repo.py --frontend` 全门禁。
