# Architecture Governance Backlog

> Prioritized, closed-loop refactoring batches. Each batch establishes baseline tests,
> migrates all callers, deletes replaced code, runs full verification, and adds anti-regression checks.

| ID | Batch | Scope | Status | Commit |
|---|---|---|---|---|
| B00 | Governance baseline docs | `docs/architecture-governance/*` | `in_progress` | — |
| B01 | Eliminate duplicate DB repository methods & unify `SHANGHAI_TZ` in `storage/db` | `storage/db/session_repo.py`, `knowledge_repo.py`, `schema.py`, `tests/test_arch_invariants.py` | `pending` | — |
| B02 | Canonical frontmatter & Shanghai time helpers, single recovery env parser, migrate internal `wiki_gate` imports, share `WikiSchemaLoader`, add AST layer/cycle/shim invariants | `core/markdown_utils.py`, `core/time_utils.py`, `core/automation.py`, `core/queue_service.py`, `core/activation.py`, `core/pipeline/*`, `adapters/skills.py`, `adapters/knowledge.py`, `adapters/runners/normalizers.py`, `adapters/wiki/*`, `app.py`, `tests/test_arch_invariants.py` | `pending` | — |
| B03 | Fix upward layer violations (`storage/seed.py`, `adapters/sessions.py` -> transcript parsing) & extract governance domain services out of `routes/governance.py` | `storage/seed.py`, `adapters/transcripts.py`, `adapters/sessions.py`, `core/triage.py`, `core/governance.py`, `routes/governance.py`, `tests/` | `pending` | — |
| B04 | Decompose `core/pipeline/coordinator.py` (2,487 LOC) into cohesive P00–P07 stage modules & disambiguate `promote_observation` | `core/pipeline/*`, `tests/` | `pending` | — |
| B05 | Modularize `core/queue_service.py` (2,403 LOC) into cohesive `core/queue/` modules (compaction, archive sync, re-triage, service) | `core/queue/*`, `core/queue_service.py`, `tests/` | `pending` | — |
| B06 | Unify shared review/plan cache & execution scaffolding in `adapters/runners/clients.py` | `adapters/runners/clients.py`, `tests/` | `pending` | — |
| B07 | Decompose frontend monoliths (`QueueInventoryPanel`, `CluesObservationsTab`, `AutomationsPage`) & tighten `any` types | `frontend/src/components/workflows/*`, `frontend/src/components/rules/*`, `frontend/src/pages/AutomationsPage.tsx`, `frontend/src/lib/*` | `pending` | — |
| B08 | Final pass: evaluate remaining hotspots (`adapters/wiki/router.py`, `routes/runs.py`, `app.py` daemon runner), tighten invariants, sync `AGENTS.md` | Cross-repo | `pending` | — |

---

## Deletion Test Criteria Applied to Every Batch

Before adding or keeping any abstraction, module, or indirection layer:
1. **If we delete this layer, does complexity vanish or spill into N callers?**
2. **Are there >= 2 real callers or a real varying seam?**
3. **Did every old caller migrate and did the obsolete implementation get deleted in the same commit?**
