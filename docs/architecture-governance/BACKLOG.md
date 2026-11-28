# Architecture Governance Backlog

> Prioritized, closed-loop refactoring batches. Each batch establishes baseline tests,
> migrates all callers, deletes replaced code, runs full verification, and adds anti-regression checks.

| ID | Batch | Scope | Status | Commit |
|---|---|---|---|---|
| B00 | Governance baseline docs | `docs/architecture-governance/*` | `done` | `d5546b2` |
| B01 | Eliminate duplicate DB repository methods & unify `SHANGHAI_TZ` in `storage/db` | `storage/db/session_repo.py`, `knowledge_repo.py`, `schema.py`, `tests/test_arch_invariants.py` | `done` | `dde57a8` |
| B02 | Canonical frontmatter & Shanghai time helpers, single recovery env parser, migrate internal `wiki_gate` imports, share `WikiSchemaLoader`, add AST layer/cycle/shim invariants | `core/markdown_utils.py`, `core/time_utils.py`, `core/automation.py`, `core/queue_service.py`, `core/activation.py`, `core/pipeline/*`, `adapters/skills.py`, `adapters/knowledge.py`, `adapters/runners/normalizers.py`, `adapters/wiki/*`, `app.py`, `tests/test_arch_invariants.py` | `done` | `31eb253` |
| B03 | Fix upward layer violations (`storage/seed.py`, `adapters/sessions.py` -> transcript parsing) & extract governance domain services out of `routes/governance.py` | `storage/seed.py`, `adapters/transcripts.py`, `adapters/sessions.py`, `core/triage.py`, `core/governance.py`, `routes/governance.py`, `tests/` | `done` | `fa0f715` |
| B04 | Decompose `core/pipeline/coordinator.py` (2,487 LOC) into cohesive P00–P07 stage modules & disambiguate `promote_observation` | `core/pipeline/*`, `tests/` | `done` | `4e9b514` |
| B05 | Modularize `core/queue_service.py` (2,403 LOC) into cohesive `core/queue_*` modules (compaction, reconcile, service) | `core/queue_*`, `core/queue_service.py`, `tests/` | `done` | `b08e8d9` |
| B06 | Unify shared review/plan cache & execution scaffolding in `adapters/runners/clients.py` & unify `_frontmatter_field` in `adapters/wiki/router.py` | `adapters/runners/clients.py`, `adapters/wiki/router.py`, `tests/` | `done` | `ed55cf2` |
| B07 | Decompose frontend monoliths (`QueueInventoryPanel`, `CluesObservationsTab`, `AutomationsPage`) & tighten `any` types | `frontend/src/components/workflows/*`, `frontend/src/components/rules/*`, `frontend/src/pages/AutomationsPage.tsx`, `frontend/src/lib/*` | `done` | `f255e7b` |
| B08 | Final pass: add frontend/frontmatter invariants, sync `AGENTS.md` package map, update architecture metrics, and run full `verify_repo.py --frontend` + independent review | Cross-repo | `done` | `7f79008` |
| B09 | [PD-001] Delete 5 deprecated shims (`core/feishu_*`, `adapters/wiki_gate.py`), migrate all test imports, lock non-existence invariant | `core/feishu_*.py`, `adapters/wiki_gate.py`, `tests/*`, `AGENTS.md`, `openspec/config.yaml` | `done` | `00d3d70` |
| B10 | [PD-002/003] Decouple `FlywheelRuntime` from `mcp_server` so backend has 0 upward layer exceptions; document PD-003 retention | `runtime.py`, `app.py`, `cli.py`, `tests/*` | `done` | *(current)* |
| B11 | Decompose `core/queue_service.py` hand-distilled ledger (`core/queue_hand_distilled.py`) and `adapters/wiki/router.py` models (`adapters/wiki/routing_models.py`) | `core/queue_*`, `adapters/wiki/*`, `AGENTS.md` | `pending` | — |
| B12 | Decompose `ConnectPolicyPage.tsx`, unify frontend `errorMessage()` replacing `catch (e: any)`, type `NoiseFunnelStrip`/`HomePage` node payloads, add frontend invariant, final independent review + full gate | `frontend/src/*`, `tests/test_arch_invariants.py` | `pending` | — |

---

## Deletion Test Criteria Applied to Every Batch

Before adding or keeping any abstraction, module, or indirection layer:
1. **If we delete this layer, does complexity vanish or spill into N callers?**
2. **Are there >= 2 real callers or a real varying seam?**
3. **Did every old caller migrate and did the obsolete implementation get deleted in the same commit?**
