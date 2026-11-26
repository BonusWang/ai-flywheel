# Change: 知识笔记保鲜期偏自动化退场与台账兜底复核

## Why

当前「规则与进化 → Skill 与保鲜 → 知识笔记保鲜期」只实现了“到期标记 `needs_recheck`”和“任务实测引用后自动顺延”，存在三处闭环断点，导致大量历史对话结晶或旧笔记永久滞留在「待复核」清单中：

1. **Wiki 原文归档状态未同步**：`KnowledgeSinkAdapter.sync_fts_index()` 同步 frontmatter 时硬编码写入 `freshness_state="fresh"`，忽略了 Wiki 源文档中的 `status: archived` / `superseded`。
2. **每周蒸馏缺少自动退场**：`core/dream.py` 对零命中试运行规则（14 天）和低危单次错题（30 天）都已有 Tier 1 自动退场，但对超期已久且从未产生助益（`helped_count == 0`）的知识笔记仅标记 `needs_recheck`，从不自动归档。
3. **控制台无兜底复核入口**：`DocumentReaderModal` 为纯只读弹窗，后端 `routes/governance.py` 未提供人工更新保鲜台账的接口，维护者阅读完过期笔记后无法确认有效、调档或移出待复核。

## What Changes

- **Wiki 归档状态自动同步**：`sync_fts_index()` 识别文档 frontmatter 的 `status: archived / superseded` 并同步至 `knowledge_card_metrics.freshness_state`；对于已在飞轮台账归档的卡片，仅当 Wiki 源文件出现更新的 `last_verified` 日期时才自动重新激活为 `fresh`。
- **每周蒸馏 Tier 1 自动退场**：在 `knowledge_repo.py` 与 `core/dream.py` 新增超期零助益卡片自动归档能力（默认超过 `valid_until` 满 30 天且 `helped_count == 0`），仅更新飞轮 SQLite 台账并记录 `audit_events`，绝不修改或删除知识库源文件；只读预览 `preview_dream_consolidation` 同步透出 `auto_archived_cards` 候选清单。
- **控制台轻量台账兜底复核**：新增 `POST /api/site/knowledge/freshness`（支持 `verify` / `archive` / `restore` / `set_tier`，只改 SQLite 台账并记审计、刷新核心索引卡，绝不触碰知识库文件）；在 `AssetsFreshnessTab` 新增「已归档」筛选，在 `DocumentReaderModal` 增加台账操作栏并明示“仅更新飞轮台账，不改动知识库源文件”。

## Capabilities

### Modified Capabilities
- `control-plane-console`: 新增知识保鲜期自动同步 Wiki 归档状态、每周蒸馏自动归档超期零助益卡片，以及控制台纯台账兜底复核接口与弹窗交互契约。

## Impact

- 后端：`backend/ai_flywheel/adapters/knowledge.py`、`storage/db/knowledge_repo.py`、`core/dream.py`、`routes/governance.py`
- 前端：`frontend/src/types.ts`、`frontend/src/api.ts`、`frontend/src/components/ui/DocumentReaderModal.tsx`、`frontend/src/components/rules/AssetsFreshnessTab.tsx`、`frontend/src/pages/RulesRetroPage.tsx`、`frontend/src/pages/AutomationsPage.tsx`
- 测试：`tests/test_console_governance_ux.py`、`tests/test_wiki_gate_and_metrics.py`
