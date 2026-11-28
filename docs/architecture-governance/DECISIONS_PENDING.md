# Pending Maintainer Decisions

> Items requiring product sign-off, external contract changes, or breaking deprecations.
> Governance execution records these here and proceeds with independent non-breaking batches.

---

## PD-001: Deprecation & Removal of Legacy Import Shims (`core/feishu_*`, `adapters/wiki_gate.py`)

- **Status**: **Resolved (Executed in B09)** — Option A approved by maintainer. All 5 shims deleted, all test callers migrated to canonical modules (`adapters.channels.feishu.*` and `adapters.wiki`), and enforced by `tests/test_arch_invariants.py` (see ADR-009).

---

## PD-002: `FlywheelRuntime -> mcp_server` Upward Wiring (`rt.mcp`)

- **Status**: **Resolved (Executed in B10)** — Option B approved by maintainer. Removed `rt.mcp` and `mcp_server` import from `runtime.py`, moved `build_mcp_server(rt)` to entry layer (`app.py` / `cli.py`), migrated all test callers, and cleared `_KNOWN_LAYER_EXCEPTIONS` to `frozenset()` (see ADR-010).

---

## PD-003: `core/orchestrator.py` Monkeypatch Facade Seam

- **Status**: **Resolved (Documented in B10)** — Explicitly retained per `AGENTS.md` as the intentional monkeypatch facade seam over `core/pipeline/` (see ADR-010).
