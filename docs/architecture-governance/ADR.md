# Architecture Decision Records (ADR)

> Durable architectural decisions established during the architecture governance program.

---

## ADR-001: Leaf Shared Primitives and Enforceable Layer Boundaries

- **Status**: Accepted
- **Context**: `AGENTS.md` defines the dependency direction `entry -> runtime -> core -> adapters -> storage`, while `models.py` and `config.py` serve as shared data/config contracts. In practice, `core/time_utils.py`, `core/privacy.py`, and `core/approval_channel.py` are pure leaf modules (zero upward or sideways dependencies on engines, adapters, or storage) that are imported by `storage/`, `adapters/`, and `core/`.
- **Decision**:
  1. Treat `models`, `config`, `core.time_utils`, `core.privacy`, `core.markdown_utils`, and `core.approval_channel` as **Shared Leaf Primitives**: they must NEVER import `storage/*`, `adapters/*`, `runtime`, `routes/*`, `app`, `cli`, `mcp_server`, or non-leaf `core/*` modules.
  2. Enforce strict layer direction via AST tests in `tests/test_arch_invariants.py`:
     - `storage` may only depend on `storage` and Shared Leaf Primitives.
     - `adapters` may only depend on `adapters`, `storage`, and Shared Leaf Primitives.
     - `core` may only depend on `core`, `adapters`, `storage`, and Shared Leaf Primitives.
     - `runtime` may only depend on `core`, `adapters`, `storage`, and Shared Leaf Primitives (with a tracked exception for `mcp_server` pending ADR/maintainer decision).
  3. Enforce zero runtime circular imports across the entire `ai_flywheel` package via AST Tarjan SCC analysis.
- **Consequences**: Prevents accidental upward coupling and circular imports while keeping existing public import paths stable.

---

## ADR-002: Single Canonical Markdown Frontmatter & Shanghai Time Primitives

- **Status**: Accepted
- **Context**: Five different modules independently parsed YAML frontmatter (`split("---")` vs line-accurate scanning), two modules duplicated `_shanghai_now_iso()`, and `storage/db/schema.py` hardcoded `timezone(timedelta(hours=8))`. Naive `text.split("---", 2)` mishandles BOMs and inline `---` horizontal rules.
- **Decision**:
  1. Introduce `ai_flywheel.core.markdown_utils` (a Shared Leaf Primitive) owning canonical `split_frontmatter`, `parse_frontmatter`, and `trim_blank_lines`.
  2. Re-export `split_frontmatter` from `adapters/wiki/schema.py` for full contract continuity, and migrate all 5 backend callers (`core/activation.py`, `adapters/skills.py`, `adapters/runners/normalizers.py`, `adapters/wiki/t5_adapter.py`, `adapters/wiki/domain_pages.py`) to the canonical implementation.
  3. Add `shanghai_now_iso()` to `ai_flywheel.core.time_utils` and delete all local `_shanghai_now_iso` / raw `timezone(timedelta(hours=8))` duplicates.
- **Consequences**: One BOM-safe, line-accurate frontmatter parser and one Shanghai clock module across the entire backend.

---

## ADR-003: Keep Documented Compatibility Shims Out of Production Code

- **Status**: Accepted
- **Context**: `core/feishu_{card,channel,listen,check}.py` and `adapters/wiki_gate.py` exist as historical import shims verified by compatibility tests. However, `core/pipeline/coordinator.py` and `core/pipeline/helpers.py` still imported `adapters.wiki_gate` in production code.
- **Decision**:
  1. Migrate all internal production code under `backend/` to import canonical modules (`adapters.wiki`, `adapters.channels.feishu.*`) directly.
  2. Add an AST invariant forbidding any module under `backend/ai_flywheel/` from importing deprecated compatibility shims (`core.feishu_*`, `adapters.wiki_gate`).
  3. Retain the shim files themselves until maintainer sign-off on breaking deprecation (tracked in `DECISIONS_PENDING.md`).
- **Consequences**: Zero new internal coupling to shims while preserving external/test compatibility.

---

## ADR-004: Transcript Parsing in Adapters & Governance Domain Service Extraction

- **Status**: Accepted
- **Context**:
  1. `adapters/sessions.py` imported 10 helpers upward from `core/triage.py`, and duplicated `_flatten_content` + boilerplate detection, because client JSONL/Antigravity transcript parsing lived inside the 1,140 LOC `core/triage.py` monolith.
  2. `storage/seed.py` imported `ActivationEngine` and `ReceiptService` at runtime purely for type hints.
  3. `routes/governance.py` (669 LOC) embedded observation lifecycle, retro promotion, knowledge freshness enrichment, and skill ledger merging inline inside FastAPI route handlers.
- **Decision**:
  1. Extract pure client transcript parsing into `adapters/transcripts.py` (~659 LOC) and re-export from `core/triage.py` (~519 LOC); inject `triage_fn` into `SessionAdapter` from `FlywheelRuntime`.
  2. Move `storage/seed.py` core engine imports under `TYPE_CHECKING`.
  3. Extract `GovernanceService` (`core/governance.py`, wired as `rt.governance`), shrinking `routes/governance.py` to thin HTTP mapping (~310 LOC).
  4. Shrink `_KNOWN_LAYER_EXCEPTIONS` in `tests/test_arch_invariants.py` from 4 entries to 1 (`runtime -> mcp_server`).
- **Consequences**: `storage/*` and `adapters/*` now have zero upward runtime dependencies; governance operations are directly testable without HTTP transport.

---

## ADR-005: Decompose Pipeline Coordinator into P00–P03 and P04–P07 Stage Modules

- **Status**: Accepted
- **Context**: `core/pipeline/coordinator.py` had grown to 2,487 LOC with two monolithic methods (`start_run` at 925 LOC and `_execute_post_approval` at 771 LOC), forcing every pipeline stage change to contend on the same file. Meanwhile, `core/pipeline/helpers.py:promote_observation` collided in name with `ActivationEngine.promote_observation` despite having distinct semantics (linking an already-created P04 rule vs creating a new trial rule).
- **Decision**:
  1. Split stage implementations cleanly along the P00–P07 approval boundary:
     - `core/pipeline/stage_pre_approval.py` (`PreApprovalStagesMixin`: P00–P03 + approval gate)
     - `core/pipeline/stage_post_approval.py` (`PostApprovalStagesMixin`: P04–P07 + `P04Outcome` / `P05Outcome`)
     - `core/pipeline/coordinator.py` (~670 LOC): coordinator shell, approval lifecycle, and linear stage dispatch via `StageTracker` and `_enter_stage`.
  2. Rename `core/pipeline/helpers.py:promote_observation` to `link_rule_to_observation`.
  3. Preserve `core/orchestrator.py` and `WorkflowOrchestrator`'s public interface and runtime monkeypatch seam untouched.
- **Consequences**: `start_run` drops from 925 LOC to ~95 LOC and `_execute_post_approval` from 771 LOC to ~18 LOC, making the P00–P07 state machine scannable at a glance and isolating pre- vs post-approval edits.

---

## ADR-006: Modularize Distill Queue Service & Unify Transcript Reader

- **Status**: Accepted
- **Context**: `core/queue_service.py` had grown to 2,399 LOC mixing pure transcript compaction/chunking, batch archive reconciliation/re-triage sweeps, and real-time ingress state transitions. Its `read_full_transcript` function also duplicated the JSONL reader in `adapters/sessions.py:read_transcript_excerpt`.
- **Decision**:
  1. Extract deterministic compaction, turn splitting, chunking, content hashing, and structured brief rendering into `core/queue_compaction.py` (~355 LOC), and delegate `read_full_transcript` directly to `adapters/sessions.py:read_transcript_excerpt`.
  2. Extract batch reconciliation (`reconcile_codex_archives`), `re_triage_queue`, and pending-session/grown-transcript bridging into `core/queue_reconcile.py` (`QueueReconcileMixin`, ~540 LOC).
  3. Keep `core/queue_service.py` (~1,530 LOC) as the canonical ingress state machine and public import surface re-exporting all queue helpers.
- **Consequences**: Compaction is pure and DB-free; one canonical JSONL transcript reader serves both session scanning and queue ingestion; maintenance sweeps evolve independently of real-time ingress.

---

## ADR-007: Unify Real Runner Orchestration via `_CachedModelRunner`

- **Status**: Accepted
- **Context**: `CliSubprocessRunner` and `OpenAICompatibleRunner` in `adapters/runners/clients.py` duplicated `review_sessions`, `plan_writes_detailed`, `plan_writes`, and `_persist_cache` across ~200 lines (including a redundant `cache_hit` computation). Separately, `adapters/wiki/router.py` still had two ad-hoc `yaml.safe_load` frontmatter blocks and a duplicate `_mapping` helper.
- **Decision**:
  1. Introduce `_CachedModelRunner(BaseWorkflowRunner)` owning canonical prompt building, validation, no-fallback failure handling, and post-validation caching for both real runners, while keeping `_invoke_cli`, `_invoke_api`, `_call_chat_completion`, and `_persist_cache` seams intact.
  2. Replace both remaining ad-hoc frontmatter parses in `adapters/wiki/router.py` with `core.markdown_utils.parse_frontmatter` / `parse_frontmatter_file` and reuse `adapters.wiki.schema._mapping`.
- **Consequences**: Single implementation of real-runner validation/cache lifecycle; zero remaining ad-hoc `yaml.safe_load` frontmatter parsers in `router.py`.

---

## ADR-008: Frontend Monolith Decomposition & Typed Node Payloads

- **Status**: Accepted
- **Context**: Three frontend components exceeded 1,180 LOC (`CluesObservationsTab.tsx` 1,334 LOC, `QueueInventoryPanel.tsx` 1,266 LOC, `AutomationsPage.tsx` 1,183 LOC), mixing multiple independent sub-views, filters, pagers, and helper functions in single files. Meanwhile, `NodeDetailPanel.tsx` used 10 `(activeNode.payload as any)` casts.
- **Decision**:
  1. Decompose `QueueInventoryPanel.tsx` into `QueueRow.tsx`, `QueueGroupRow.tsx`, and the panel shell (`1,266 -> 682 LOC`).
  2. Decompose `CluesObservationsTab.tsx` into `cluesUtils.ts`, `CluesObservationsSubView.tsx`, `CluesDissentsSubView.tsx`, `CluesRetrosSubView.tsx`, and the tab shell (`1,334 -> 495 LOC`), giving each sub-view isolated local filter/selection/pagination state.
  3. Decompose `AutomationsPage.tsx` by extracting `lib/cronVisual.ts`, `AutomationsWatchdogsSection.tsx`, and `AutomationsHistorySection.tsx` (`1,183 -> 875 LOC`).
  4. Add `CallTelemetry`, `DreamNodeSummary`, and `NodeExecutionPayload` to `types.ts`, eliminating all 10 `as any` casts in `NodeDetailPanel.tsx` and tightening `api.ts` observation return types.
- **Consequences**: Smaller, independently editable UI modules with local state ownership and stronger TypeScript coverage.

---

## ADR-009: Remove Deprecated `core/feishu_*` and `adapters/wiki_gate.py` Shims (PD-001)

- **Status**: Accepted (Maintainer Approved)
- **Context**: After B02 migrated all internal production callers, the 5 forwarding shim modules (`core/feishu_{card,channel,listen,check}.py` and `adapters/wiki_gate.py`) served only historical test imports and added parallel import paths for the same symbols.
- **Decision**:
  1. Delete all 5 deprecated shim modules.
  2. Migrate all remaining test imports directly to `ai_flywheel.adapters.channels.feishu.*` and `ai_flywheel.adapters.wiki`.
  3. Strengthen `test_no_production_code_imports_legacy_shims` in `tests/test_arch_invariants.py` to assert both non-existence of the 5 shim files and zero imports across `backend/` and `tests/`.
- **Consequences**: Canonical import paths everywhere (`Deletion over Accumulation`), zero dead forwarding shells.

---

## ADR-010: MCP Server Owned by Entry Layer & Intentional Retention of `core/orchestrator.py` (PD-002 / PD-003)

- **Status**: Accepted (Maintainer Approved)
- **Context**:
  1. `runtime.py` imported `build_mcp_server` from `mcp_server.py` (an entry-layer transport adapter) and exposed `rt.mcp`, creating the single remaining `[runtime -> entry]` layer exception in `_KNOWN_LAYER_EXCEPTIONS`.
  2. `core/orchestrator.py` (48 LOC) is the documented runtime monkeypatch facade over `core/pipeline/`.
- **Decision**:
  1. Remove `self.mcp` and the `mcp_server` import from `FlywheelRuntime`; construct `build_mcp_server(rt)` exclusively in the entry layer (`app.py`, exposing `app.state.mcp`, and `cli.py` for `mcp-stdio`).
  2. Migrate all test callers to `build_mcp_server(rt)` and empty `_KNOWN_LAYER_EXCEPTIONS` in `tests/test_arch_invariants.py`, locking **zero** upward layer violations across the entire backend.
  3. Explicitly retain `core/orchestrator.py` as an intentional monkeypatch facade seam (PD-003), matching `AGENTS.md`.
- **Consequences**: Strict acyclic `entry -> runtime -> core -> adapters -> storage -> leaf` dependency enforcement with zero exceptions.

---

## ADR-011: Extract `queue_hand_distilled.py` and `wiki/routing_models.py`

- **Status**: Accepted
- **Context**: `core/queue_service.py` (1,542 LOC) and `adapters/wiki/router.py` (1,297 LOC) remained the two largest backend files, each combining a cohesive sub-domain/model block with its core state-machine/evaluation logic.
- **Decision**:
  1. Extract `core/queue_hand_distilled.py` (`QueueHandDistilledMixin`, `_declaration_meta`, ~275 LOC) from `core/queue_service.py` (shrinking it to ~1,275 LOC), preserving `DistillQueueService`'s public API intact.
  2. Extract `adapters/wiki/routing_models.py` (`IncidentRouting`, `DepositRouting`, `RoutingCandidates`, `RouteDecision`, `RouteContext`, reason maps, ~455 LOC) from `adapters/wiki/router.py` (shrinking it to ~855 LOC), re-exporting all symbols from `router.py`.
- **Consequences**: Largest backend file drops below 1,300 LOC; routing models and hand-distilled policy can evolve independently.
