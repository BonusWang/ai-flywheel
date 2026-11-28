# Architecture Map & Entropy Baseline

> Living architectural map for AI Flywheel (baseline commit `620bba8`, `v0.9.0`).
> Maintained as part of the systematic architecture governance program.

---

## 1. Codebase Scale (Baseline)

| Surface | Files | LOC | Largest Modules |
|---|---:|---:|---|
| Backend (`backend/ai_flywheel/`) | 89 `.py` | 33,498 | `core/pipeline/coordinator.py` (2,487), `core/queue_service.py` (2,403), `adapters/wiki/router.py` (1,313), `core/triage.py` (1,140), `adapters/knowledge.py` (970), `core/automation.py` (917) |
| Frontend (`frontend/src/`) | 42 `.ts/.tsx/.css` | 14,319 | `components/rules/CluesObservationsTab.tsx` (1,334), `components/workflows/QueueInventoryPanel.tsx` (1,266), `pages/AutomationsPage.tsx` (1,183), `pages/ConnectPolicyPage.tsx` (930), `pages/WorkflowsPage.tsx` (922) |
| Tests (`tests/`) | 1,110 test cases | — | Including `tests/test_arch_invariants.py` (860 LOC) and `tests/test_approval_channel_abstraction.py` (245 LOC) |

---

## 2. Layered Dependency Architecture

Intended dependency direction:

```text
Entry Layer       (app.py, cli.py, mcp_server.py, routes/*)
      │
      ▼
Runtime Wiring    (runtime.py)
      │
      ▼
Domain Core       (core/*)
      │
      ▼
Outbound Adapters (adapters/*)
      │
      ▼
Storage Layer     (storage/*)
      │
      ▼
Shared Leaf Primitives (models.py, config.py, core/time_utils.py, core/privacy.py, core/approval_channel.py)
```

### Observed Baseline Layer & Cycle Findings (AST Audit)

- **Runtime circular imports**: `0` (two `TYPE_CHECKING`-only annotation cycles exist: `adapters.wiki.router <-> adapters.wiki.t5_adapter` and `core.hooks / mcp_server / runtime`).
- **True upward runtime dependencies**:
  1. `adapters/sessions.py -> core/triage.py` (`[adapters -> core]`): low-level JSONL/Antigravity transcript parsing and active-window checks live inside `core/triage.py`, forcing `SessionAdapter` to import 10 helpers upward.
  2. `storage/seed.py -> core/activation.py, core/receipts.py` (`[storage -> core]`): runtime imports used solely in type annotations (already guarded by `from __future__ import annotations`, can move under `TYPE_CHECKING`).
  3. `runtime.py -> mcp_server.py` (`[runtime -> entry]`): `FlywheelRuntime.__init__` builds and exposes `rt.mcp = build_mcp_server(self)`. Recorded in `DECISIONS_PENDING.md` because tests directly read `rt.mcp`.
- **Shared leaf primitives misplaced under `core/`**:
  - `core/time_utils.py`, `core/privacy.py`, and `core/approval_channel.py` have **zero** dependencies on `core/*` engines, `adapters/*`, or `storage/*`. They are leaf contracts shared across `storage/`, `adapters/`, and `core/`.

---

## 3. State Ownership Map

| State Domain | Authoritative Owner | Persistence / Projection |
|---|---|---|
| Schema & relational state (sessions, runs, nodes, planned writes, queue, rules, observations, dissents, retro queue, outcomes, audits, dicts, locks, outbox) | `storage/db/` (`FlywheelDB` composed of 8 repository mixins + migrations) | SQLite v4 (`docs/schema.sql`) |
| Immutable execution receipts & validated LLM reply caches | `storage/fs_store.py` + `adapters/runners/clients.py` | `data/receipts/` (`api_cache/`, `cli_cache/`) |
| Knowledge documents & Wiki schema contract | `adapters/knowledge.py` + `adapters/wiki/` | Local Markdown / mounted Wiki + FTS5 projection in SQLite |
| Skill files (append-only updates) | `adapters/skills.py` | User-configured `skills_dirs` |
| Derived core index card | `core/core_card.py` | `data/FLYWHEEL-CORE.md` (derived projection, refreshed on state changes) |
| Configuration & safety valves | `config.py` (`FlywheelConfig`) | `flywheel.yaml` + env overrides (`FLYWHEEL_*`) |
| Live workflow / queue SSE pub-sub | `core/pipeline/events.py` (`EventBroker`) | In-memory subscriber queues |
| Scheduler & daemon telemetry | `core/automation.py` (`AutomationManager`) + `app.py` lifespan loops | In-memory + fire-claim rows in `system_settings` |
| Frontend UI state | `frontend/src/App.tsx` (URL routing, theme, SSE refresh token) + page-local state | Browser URL + `localStorage` (theme / last manual client) |

---

## 4. Entropy & Specification Deviation Inventory (Baseline)

### A. Duplicate Implementations & Shadowed Methods
1. **Shadowed / duplicate SQLite mixin methods** (`storage/db/`):
   - `SessionRepoMixin.count_retro_items` is defined **twice** in `session_repo.py` (L362 and L519).
   - `get_preflight_knowledge_pointers` is duplicated identically in both `SessionRepoMixin` (L608) and `KnowledgeRepoMixin` (L455); `KnowledgeRepoMixin`'s copy is dead shadowed code in `FlywheelDB` MRO.
2. **Five separate Markdown frontmatter parsers**:
   - Canonical: `adapters/wiki/schema.py:split_frontmatter` (BOM-safe, line-accurate).
   - Ad-hoc `text.split("---", 2)` copies in `core/activation.py:_parse_frontmatter`, `adapters/skills.py:_parse_frontmatter`, `adapters/runners/normalizers.py:_draft_frontmatter` / `_draft_body`, plus local wrappers in `adapters/wiki/t5_adapter.py:_load_draft_frontmatter` and `adapters/wiki/domain_pages.py:_draft_frontmatter`.
3. **Duplicate Shanghai time helpers**:
   - `_shanghai_now_iso()` duplicated in `core/automation.py:131` and `core/queue_service.py:132`.
   - `storage/db/schema.py:583` (`to_shanghai_date`) manually constructs `timezone(timedelta(hours=8))` instead of reusing `SHANGHAI_TZ`.
4. **Duplicate recovery watchdog environment parsing**:
   - `FLYWHEEL_RECOVERY_INTERVAL` and `FLYWHEEL_RECOVERY_STALE_SECONDS` parsed independently in `app.py:76-77` and `core/automation.py:238-239`.
5. **Duplicate `WikiSchemaLoader` instance**:
   - `adapters/knowledge.py:118-124` maintains its own `self._schema_loader` alongside `self.wiki_gate.schema_loader` (two independent caches for the same `.wiki-schema.md`).
6. **Duplicate helper `_trim_blank`**:
   - Defined identically in `adapters/wiki/domain_pages.py:270` and `adapters/wiki/t5_adapter.py:254`.
7. **Semantically colliding function name `promote_observation`**:
   - `ActivationEngine.promote_observation` creates a new trial rule from an observation (portal action), whereas `core/pipeline/helpers.py:promote_observation` links an already-registered P04 rule back to its source observation.

### B. Legacy Shims & Stale Internal Callers
- `core/pipeline/coordinator.py:37` and `core/pipeline/helpers.py:18` still import from the compatibility shim `ai_flywheel.adapters.wiki_gate` instead of the canonical package `ai_flywheel.adapters.wiki`.
- `core/feishu_{card,channel,listen,check}.py` have zero production callers in `backend/`, but remain explicitly pinned by backward-compatibility tests in `tests/test_approval_channel_abstraction.py` (tracked in `DECISIONS_PENDING.md` for deprecation sign-off).

### C. Business Logic Leaking into HTTP Transport Layer
- `routes/governance.py` (669 LOC) embeds substantial domain orchestration inline in route handlers:
  - `site_promote_retro` (~93 LOC: root-cause hashing, severity grading, observation upsert, audit, core-card refresh)
  - `site_list_knowledge` (~74 LOC: freshness enrichment, orphan card merging, expiry evaluation, sorting)
  - `site_update_knowledge_freshness` (~63 LOC: action/tier validation, audit event, core-card refresh)
  - `site_list_skills` (~52 LOC: DB skill asset + local probe merging & health sorting)

### D. Oversized Monoliths Hurting Locality & Parallel Agent Development
- **Backend**:
  - `core/pipeline/coordinator.py` (2,487 LOC): `start_run()` is 925 LOC; `_execute_post_approval()` is 771 LOC.
  - `core/queue_service.py` (2,403 LOC): mixes transcript compaction/chunking, hand-distilled tracking, archive reconciliation, re-triage sweeps, and queue state machine.
  - `adapters/wiki/router.py` (1,313 LOC) and `core/triage.py` (1,140 LOC).
  - `adapters/runners/clients.py` (777 LOC): duplicated cache persistence and review/plan execution scaffolding across CLI and API runners.
- **Frontend**:
  - `components/rules/CluesObservationsTab.tsx` (1,334 LOC): 4 sub-views + retro grouping logic in one file.
  - `components/workflows/QueueInventoryPanel.tsx` (1,266 LOC): local `Badge`, `QueueRow` (300L), `QueueGroupRow` (225L), and panel shell (640L) in one file.
  - `pages/AutomationsPage.tsx` (1,183 LOC): cron visual builder/parser + 4 independent sections in one file.
  - `pages/ConnectPolicyPage.tsx` (930 LOC) and `pages/WorkflowsPage.tsx` (922 LOC).
  - 65 `any` occurrences concentrated in `NodeDetailPanel.tsx`, `QueueInventoryPanel.tsx`, `AutomationsPage.tsx`, `types.ts`, and `api.ts`.

### E. Missing Enforceable Architecture Invariants
Baseline `tests/test_arch_invariants.py` checks hygiene, prompt schemas, cross-layer enum/version contracts, serve role, Windows contracts, pipeline cache/side-effects, and DDL parity — **but does not yet enforce**:
1. Zero duplicate methods inside classes or colliding methods across `FlywheelDB` repository mixins.
2. Zero runtime circular imports across `backend/ai_flywheel/`.
3. Zero production imports of deprecated compatibility shims (`core.feishu_*`, `adapters.wiki_gate`).
4. Layer dependency direction enforcement (preventing any new upward imports across `storage -> adapters -> core -> runtime -> entry`).
