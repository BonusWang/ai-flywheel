# Pending Maintainer Decisions

> Items requiring product sign-off, external contract changes, or breaking deprecations.
> Governance execution records these here and proceeds with independent non-breaking batches.

---

## PD-001: Deprecation & Removal of Legacy Import Shims (`core/feishu_*`, `adapters/wiki_gate.py`)

- **Status**: **Resolved (Executed in B09)** — Option A approved by maintainer. All 5 shims deleted, all test callers migrated to canonical modules (`adapters.channels.feishu.*` and `adapters.wiki`), and enforced by `tests/test_arch_invariants.py` (see ADR-009).

---

## PD-002: `FlywheelRuntime -> mcp_server` Upward Wiring (`rt.mcp`)

- **Current state**:
  - `backend/ai_flywheel/runtime.py` imports `build_mcp_server` from `ai_flywheel.mcp_server` and stores `self.mcp = build_mcp_server(self)`.
  - `tests/test_arch_invariants.py`, `app.py`, and `cli.py` access `rt.mcp` directly.
- **Why pending**:
  - Moving `build_mcp_server(rt)` up to the entry layer (`app.py` / `cli.py`) would eliminate the last `[runtime -> entry]` dependency, but would remove `rt.mcp` from `FlywheelRuntime` (breaking callers and tests that expect `rt.mcp`).
- **Options**:
  - **Option A**: Keep `rt.mcp` as an explicit runtime wiring convenience (or lazy property) and document `mcp_server` as a runtime-mounted transport surface.
  - **Option B**: Remove `rt.mcp`, construct `build_mcp_server(rt)` exclusively in `app.py` and `cli.py`, and update test callers.

---

## PD-003: `core/orchestrator.py` Monkeypatch Facade Seam

- **Current state**:
  - `backend/ai_flywheel/core/orchestrator.py` (48 LOC) is explicitly documented in `AGENTS.md` and tested in `tests/test_pipeline_git_commit.py` as the runtime monkeypatch seam (`build_runner`, `rebuild_core_card`, `run_dream_consolidation`).
- **Why pending**:
  - Deleting this facade would break documented monkeypatch seams and replace them with explicit constructor/method dependency injection across `WorkflowOrchestrator`.
- **Recommendation**: Keep `core/orchestrator.py` as an intentional seam unless the maintainer opts to migrate test injection fully to constructor dependencies.
