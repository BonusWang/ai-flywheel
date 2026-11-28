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
| B00 | 2026-11-28 | *(pending)* | Established isolated governance worktree, full AST/frontend audit, architecture map, ADR log, pending decisions, and backlog | Hygiene + OpenSpec + baseline verification |
