# Architecture Governance Progress Log

> Tracks measurable entropy reduction, verification outcomes, and commit trail across batches.

## Baseline Metrics (`620bba8`, `v0.9.0`)

- **Backend**: 89 files, 33,498 LOC
  - Largest file: `core/pipeline/coordinator.py` (2,487 LOC)
  - Largest function: `WorkflowOrchestrator.start_run()` (925 LOC)
  - Second largest function: `WorkflowOrchestrator._execute_post_approval()` (771 LOC)
  - Duplicate class/mixin methods: `2` (`SessionRepoMixin.count_retro_items`, `KnowledgeRepoMixin.get_preflight_knowledge_pointers`)
  - Ad-hoc frontmatter parsers: `5`
  - Production imports of legacy shims (`adapters.wiki_gate` / `core.feishu_*`): `2`
  - Upward runtime layer violation edges (excluding leaf primitives): `4` across 3 modules (`adapters.sessions -> core.triage`, `storage.seed -> core.{activation,receipts}`, `runtime -> mcp_server`)
  - Runtime circular imports: `0`
- **Frontend**: 42 files, 14,319 LOC
  - Largest component: `components/rules/CluesObservationsTab.tsx` (1,334 LOC)
  - `any` occurrences across `frontend/src/`: `65`
- **Tests**: 1,110 collected pytest cases

---

## Completed Batches

| Batch | Date | Commit | Summary | Verification |
|---|---|---|---|---|
| B00 | 2026-11-28 | `d5546b2` | Established isolated governance worktree, full AST/frontend audit, architecture map, ADR log, pending decisions, and backlog | Hygiene (378 files clean) + OpenSpec |
| B01 | 2026-11-28 | `dde57a8` | Removed duplicate `SessionRepoMixin.count_retro_items` and shadowed `KnowledgeRepoMixin.get_preflight_knowledge_pointers`; unified `SHANGHAI_TZ` in `storage/db/schema.py`; added AST invariant banning duplicate class methods & DB mixin collisions | `test_arch_invariants` + `test_v2_schema_and_skills` + `test_v3_true_flywheel` (45 passed) |
| B02 | 2026-11-28 | `31eb253` | Unified 5 frontmatter parsers & `_trim_blank` into `core/markdown_utils.py`; added `shanghai_now_iso` to `core/time_utils.py` & removed duplicates; unified `recovery_watchdog_settings()`; shared `WikiSchemaLoader` via `WikiGate` (fixing cache block check); migrated all production `adapters.wiki_gate` imports to `adapters.wiki`; added AST invariants for DAG cycles, legacy shims, leaf primitives, and layer direction | Hygiene (379 files clean) + 345 targeted tests passed |
| B03 | 2026-11-28 | `fa0f715` | Extracted `adapters/transcripts.py` from `core/triage.py` (1,140→519 LOC), deleted duplicate `_flatten_content`/boilerplate helpers in `adapters/sessions.py`, injected `triage_fn` to remove `[adapters->core]`; moved `storage/seed.py` core imports under `TYPE_CHECKING` to remove `[storage->core]`; extracted `GovernanceService` (`core/governance.py`) shrinking `routes/governance.py` (669→309 LOC); shrank `_KNOWN_LAYER_EXCEPTIONS` from 4→1 | Hygiene (381 files clean) + 206 targeted tests passed (incl. 63-route parity & layer invariants) |
| B04 | 2026-11-28 | `4e9b514` | Decomposed `core/pipeline/coordinator.py` (2,487→787 LOC; `start_run` 925→95 LOC, `_execute_post_approval` 771→18 LOC) into `stage_pre_approval.py` (P00–P03 + gate) and `stage_post_approval.py` (P04–P07) via `StageTracker` & `_enter_stage`; renamed `helpers.promote_observation` → `link_rule_to_observation` | Hygiene (383 files clean) + 223 pipeline/recovery/git-commit/invariant tests passed |
| B05 | 2026-11-28 | `b08e8d9` | Modularized `core/queue_service.py` (2,399→1,542 LOC) into `core/queue_compaction.py` (369 LOC, unifying `read_full_transcript` onto `adapters.sessions.read_transcript_excerpt`) and `core/queue_reconcile.py` (`QueueReconcileMixin`, 546 LOC) | Hygiene (385 files clean) + 294 distill-queue/agy/invariant tests passed |
| B06 | 2026-11-28 | `ed55cf2` | Unified `CliSubprocessRunner` & `OpenAICompatibleRunner` onto `_CachedModelRunner` in `adapters/runners/clients.py`; replaced both remaining ad-hoc frontmatter parses & duplicate `_mapping` in `adapters/wiki/router.py` | Hygiene (385 files clean) + 244 runner/reliability/P02/P03/invariant tests passed |
| B07 | 2026-11-28 | `f255e7b` | Decomposed `CluesObservationsTab.tsx` (1,334→494 LOC) into 3 self-contained sub-views + `cluesUtils.ts`; decomposed `QueueInventoryPanel.tsx` (1,266→684 LOC) into `QueueRow.tsx` + `QueueGroupRow.tsx`; decomposed `AutomationsPage.tsx` (1,183→876 LOC) into `cronVisual.ts` + `AutomationsWatchdogsSection.tsx` + `AutomationsHistorySection.tsx`; added `CallTelemetry`/`DreamNodeSummary`/`NodeExecutionPayload` removing all 10 `as any` casts in `NodeDetailPanel.tsx` | `tsc -b && vite build` passed (3.57s) + hygiene (394 files clean) + `test_arch_invariants` (36 passed) |
| B08 | 2026-11-28 | `7f79008` | Added frontmatter-split & frontend HTTP/SSE centralization invariants (`test_arch_invariants.py`); resolved all independent review findings (`skills.py` frontmatter validation, `markdown_utils` BOM stripping, Clues sub-view failure selection preservation, `from pkg import shim` AST check, `runtime.py` lazy core-card hook); synced `AGENTS.md` package map & metrics | `python scripts/verify_repo.py --frontend`: 4/4 PASS (hygiene 394 files clean, openspec strict, 1,117 pytest passed, frontend built) |
| B09 | 2026-11-28 | `00d3d70` | [PD-001] Deleted all 5 deprecated legacy shims (`core/feishu_{card,channel,listen,check}.py`, `adapters/wiki_gate.py`), migrated all test callers to canonical modules, and locked both file non-existence and zero imports across `backend/` + `tests/` | Hygiene (389 files clean) + 481 targeted tests passed |
| B10 | 2026-11-28 | `a59b8a5` | [PD-002/003] Moved `build_mcp_server(rt)` from `runtime.py` to entry layer (`app.py`, `cli.py`), migrated all test callers, cleared `_KNOWN_LAYER_EXCEPTIONS` to **0** upward layer violations, and documented PD-003 retention | Hygiene (389 files clean) + 190 MCP/core/distill-queue/invariant tests passed |
| B11 | 2026-11-28 | `4ce96af` | Extracted `core/queue_hand_distilled.py` (shrinking `queue_service.py` 1,542→1,278 LOC) and `adapters/wiki/routing_models.py` (shrinking `wiki/router.py` 1,297→878 LOC); synced `AGENTS.md` package map | AST import check + hygiene (391 files clean) + 362 queue/wiki/invariant tests passed |
| B12 | 2026-11-28 | `37555ca` | Created `frontend/src/lib/errors.ts` (`errorMessage`), replaced all `catch (e: any)` across 10 frontend files with `unknown`, typed `TriageReportSummary`/`ReviewFindingSummary` removing all remaining `as any` casts, extracted `ConnectClientsTab.tsx` & `ConnectSandboxTab.tsx` from `ConnectPolicyPage.tsx` (930→746 LOC), added `test_frontend_has_no_catch_any_or_as_any` invariant | `tsc -b && vite build` passed (2.54s) + hygiene (394 files clean) + 39/39 `test_arch_invariants` passed |
| B13 | 2026-11-28 | *(current)* | Resolved all Phase 2 independent review findings (P1-1: keep `ConnectClientsTab`/`ConnectSandboxTab` mounted across tab switches; P1-2: sync `openspec/specs/approval-card-channels/spec.md` baseline to PD-001; P2-1/2/3: sync `openspec/config.yaml`, remove stale shim docstrings, unify `CluesObservationsTab`/Feishu check `errorMessage()`, align ADR/PROGRESS metrics) | `python scripts/verify_repo.py --frontend`: 4/4 PASS (hygiene 394 files clean, openspec strict, 1,118 pytest passed, frontend built) |

---

## Before vs After Architecture Metrics

| Metric | Baseline (`620bba8`) | Post-Governance | Delta |
|---|---:|---:|---|
| Largest backend file | 2,487 LOC (`coordinator.py`) | 1,278 LOC (`queue_service.py`) | -49% (`coordinator.py`: -68%, `queue_service.py`: -47%, `wiki/router.py`: -32%) |
| Largest backend function | 925 LOC (`start_run`) | 462 LOC (`_run_p04_write_verify`) | -50% (`start_run`: 925 → 95 LOC; `_execute_post_approval`: 771 → 18 LOC) |
| Largest frontend component | 1,334 LOC (`CluesObservationsTab.tsx`) | 923 LOC (`WorkflowsPage.tsx`) | -31% (`CluesObservationsTab`: -63%, `QueueInventoryPanel`: -46%, `AutomationsPage`: -26%, `ConnectPolicyPage`: -20%) |
| Frontend `any` count | 65 | 16 (`0` `as any`, `0` `catch (e: any)`) | -75% + static invariant |
| Duplicate class / DB mixin methods | 2 | 0 | Eliminated + AST invariant |
| Ad-hoc frontmatter parsers | 5 | 0 | Eliminated + static invariant |
| Legacy forwarding shim files | 5 (2 prod imports) | 0 | All 5 deleted + AST non-existence invariant (PD-001) |
| Upward runtime layer violations | 4 | 0 | **Zero** upward exceptions + AST invariant (PD-002) |
| Runtime circular imports | 0 | 0 | Locked via AST Tarjan SCC invariant |
| Pending maintainer decisions | 3 (`PD-001`–`PD-003`) | 0 | All 3 resolved & recorded in ADR-009 / ADR-010 |
| Enforceable architecture invariants (`test_arch_invariants.py`) | 31 | 39 | +8 new automated invariants |
