# Change: 修复实时异常线索处理语义冲突与转错题观察池闭环

## Why

在控制面「规则与进化 → 错题与分歧 → 实时异常线索」视图中，当前存在两处容易导致维护者误操作的设计冲突：

1. **文案与主按钮行为相反**：`retro_advisory.py` 对高价值实证/复发线索给出提示「高价值 · 建议留存：建议保留进入沉淀或提炼为避坑规则」，但 `recommended_action` 仍指向 `resolved`，导致前端把「已处理」高亮为主按钮。而后端 `POST /api/site/retro/bulk-status` 将状态改为 `resolved` 只会改状态字段、既不写入错题观察池也不触发沉淀，反而把线索移出 `open` 集合，使下一次 P02 审查无法再读到它。
2. **孤立线索与自动结算状态不透明**：P05 仅自动结算 `session_key` 位于本批次已审会话中的 `open` 线索；对于上报时 `session_key` 不在待提炼队列中的孤立线索（如 `generic:*` 或测试/归档会话），页面顶部仍统一宣称“每日定时整理时会自动结算”，维护者无法分辨哪些线索会自动结案、哪些必须人工处置。

## What Changes

- **新增「转入错题观察池」真实闭环接口**：新增 `POST /api/site/retro/promote`，将单条或同组实时异常线索真正写入 `mistake_observations`（按 `root_cause_key` 幂等去重与累计出现会话），同步将对应 `retro_queue` 记录置为 `resolved`，写入 `audit_events` 并刷新核心索引卡。
- **透出队列关联与结算模式**：`GET /api/site/retro` 查询当前 `distill_queue` 中处于 `queued`/`processing` 的会话集合，为每条线索附带 `has_queued_session` 与 `settlement_mode`（`auto_next_batch` / `manual_only`）。
- **对齐建议分类与前端三种清晰去向**：`retro_advisory.py` 对 `keep` 类高价值线索返回 `recommended_action = "promote"`；前端 `CluesObservationsTab` 明确区分「转入错题观察池」「已人工处理（不再送入下次沉淀）」「忽略（噪音/误报）」三类动作，并展示「随下次沉淀自动结算」或「无关联待审会话」徽标。

## Capabilities

### Modified Capabilities
- `control-plane-console`: 新增实时异常线索关联会话结算状态透出、转入错题观察池接口及三种明确去向的交互契约。

## Impact

- 后端：`backend/ai_flywheel/core/retro_advisory.py`、`storage/db/queue_repo.py`、`routes/governance.py`
- 前端：`frontend/src/types.ts`、`frontend/src/api.ts`、`frontend/src/components/rules/CluesObservationsTab.tsx`
- 测试：`tests/test_console_governance_ux.py`、`tests/test_retro_advisory.py`（如存在或新增测试）、`tests/test_approval_channel_abstraction.py`
