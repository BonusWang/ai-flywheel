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
  - Upward runtime layer violations (excluding leaf primitives): `3` (`adapters.sessions -> core.triage`, `storage.seed -> core.{activation,receipts}`, `runtime -> mcp_server`)
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
| B06 | 2026-11-28 | *(current)* | Unified `CliSubprocessRunner` & `OpenAICompatibleRunner` onto `_CachedModelRunner` in `adapters/runners/clients.py`; replaced both remaining ad-hoc frontmatter parses & duplicate `_mapping` in `adapters/wiki/router.py` | Hygiene (385 files clean) + 244 runner/reliability/P02/P03/invariant tests passed |
