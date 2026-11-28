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
