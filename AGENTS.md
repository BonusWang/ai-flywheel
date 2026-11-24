# AGENTS.md — AI Flywheel

> The map, the rules and the gate. Read this before you change anything.
> Everything here is a contract, not a suggestion: each rule below has a test that
> fails when it is broken.

---

## 1. What this repository is

A **local-first, evidence-driven AI collaboration flywheel**. AI tools (Codex, Claude Code,
Claude Desktop, Antigravity, …) talk to it over standard **MCP**; an optional ultra-thin
forwarding hook is available for clients that cannot speak MCP. In the background a fixed
**P00–P07** workflow turns conversations into knowledge, and a minimal **4+1** web console
shows the batches, the rules and the audit trail.

The methodology is deliberately decoupled from the knowledge base: `profile/` holds the
policies, the knowledge sink is pluggable, and the wiki gate is contract-driven.

- Python 3.11+ · FastAPI · Pydantic v2 · SQLite (FTS5) · MCP SDK · httpx · APScheduler
- Frontend: React 19 + TypeScript + Vite (`frontend/`)
- Version: **`ai_flywheel.__version__`** — it is the only place the version is written down.

---

## 2. Architecture & package map

| Path | Owns |
|---|---|
| `backend/ai_flywheel/core/` | The domain engine: `orchestrator` (the P00–P07 state machine), `activation` (preflight & rules), `triage` (session triage), `dream` (weekly distillation), `core_card` (the derived index card), `privacy`, `receipts`, `heartbeat`, `feishu_*` (card / channel / listener / self-check), `time_utils` |
| `backend/ai_flywheel/adapters/runners/` | The execution layer: `clients` (CLI + OpenAI-compatible API), `heuristic` (dry-run), `contracts` (the wire contract + diagnostics), `prompts`, `normalizers`, `outcomes` |
| `backend/ai_flywheel/adapters/wiki/` | The mounted-wiki contract: `schema` (`.wiki-schema.md`), `router` (routing candidates & evidence), `gate` (pre-write validation), `validator`, `t5_adapter`, `postflight` |
| `backend/ai_flywheel/adapters/knowledge.py` | The knowledge sink: privacy gate → schema gate → atomic LF write → read-back verification → FTS refresh |
| `backend/ai_flywheel/adapters/skills.py` | Skill writes. An update **appends**; it never rewrites a file. |
| `backend/ai_flywheel/adapters/lark_cli.py`, `adapters/delivery.py` | Outbound delivery: the native `lark-cli` invocation and the delivery adapter |
| `backend/ai_flywheel/storage/db/` | SQLite, split per repository (`workflow_repo`, `session_repo`, `rule_repo`, `knowledge_repo`, `asset_repo`, `audit_repo`) + `schema.py` / `migrations.py` |
| `backend/ai_flywheel/storage/` | `fs_store` (immutable receipts), `outbox` (managed delivery outbox), `clock` (injectable clock), `seed` |
| `backend/ai_flywheel/` | `app.py` (REST + SSE + lifespan), `mcp_server.py`, `cli.py`, `config.py`, `models.py`, `runtime.py` (the dependency container) |
| `frontend/src/` | The console: `pages/` (Home, Workflows, Rules, Connect, About), `components/`, `api.ts`, `types.ts`, `lib/labels.ts` |
| `profile/` | Methodology policies and the portal branding — decoupled from the knowledge base |
| `openspec/` | Spec-driven development: `config.yaml` (injected into every generated artifact), `specs/` (the baseline), `changes/` (in flight and archived) |
| `tests/` | pytest. `tests/test_arch_invariants.py` holds the cross-layer invariants. |
| `scripts/verify_repo.py` | The one-shot verification gate (hygiene → openspec → pytest → frontend build) |

**Dependency direction:** `app.py` / `cli.py` / `mcp_server.py` → `runtime.py` → engines
(`core/`) → adapters (`adapters/`) → storage (`storage/`). Adapters never import the app
layer; the core never imports an adapter's transport.

---

## 3. The P00–P07 pipeline and what each stage may touch

A run is one batch. The stage boundaries are a **side-effect contract**: a stage that writes
something the contract does not list is a bug, even when the batch still ends `PASS`.

| Stage | Name | May read | May write | Notes |
|---|---|---|---|---|
| `P00_PREPARE` | 准备与对账 | config, sessions, receipts | run record, audit events | Reconciles the batch, never touches the knowledge base |
| `P01_BEGIN` | 独占启动 | sessions, budget | the workflow lock | One exclusive lock per run; the admission budget is decided here |
| `P02_REVIEW` | 全文审查 | the admitted sessions | **the run record only** | **Read-only**: mistakes and dissents are *candidates* and stay in the node payload until the batch is approved |
| `P03_ROUTE_PLAN` | 路由与写入计划 | rules, skills, the mounted wiki contract | **the run record only** | **Read-only** for knowledge / observations / dissents / skills: it produces a *plan*, never a write |
| `P04_WRITE_VERIFY` | 受控写入与验收 | the plan | **the one controlled write stage** | The only stage that writes knowledge documents, rules and Skills — and the first stage that may persist mistake observations and dissent records (after approval) |
| `P05_SETTLE` | 台账与队列结算 | the run record | session status, the `retro_queue`, the core card | Idempotent settlement: reviewed sessions become `processed`, unreviewed ones go back to `pending`; one core-card refresh; the FTS index is rebuilt **once for the whole batch** (not once per document) |
| `P06_REPORT` | 机器报告与摘要 | everything so far | the run digest | Read-only for the knowledge base |
| `P07_DELIVER` | 交付与终态释锁 | the digest | delivery records, the ask card, the lock | The terminal state: the exclusive lock is released here, never earlier |

**Fail-run contract.** When a stage fails: the downstream stages are explicitly `SKIPPED`,
the admitted sessions go back to `pending`, the exclusive lock is released, and the run ends
`FAILED`. A rejected approval ends the run `CANCELLED` — not `PARTIAL`: "nothing was
written" and "half of it was written" must never look the same.

---

## 4. Single sources of truth

| Question | The one place to look |
|---|---|
| What version is this? | `ai_flywheel.__version__` — the portal, `/health`, `/llms.txt` and the MCP server all read it from there |
| Which runner will a batch use? | `adapters/runners/clients.py` → `resolve_runner_mode(cfg)` (`api:<model>` / `cli` / `dry_run`) |
| Is this write sensitive? | `models.is_sensitive_write()` — the portal, the approval gate and the Feishu card all call it |
| What does the mounted wiki allow? | `adapters/wiki/schema.py` → `WikiSchemaLoader` (the `.wiki-schema.md` contract, cached per file revision) |
| What date is "today"? | `core/time_utils.py` — every product-facing date is a **Shanghai** date, never a host date |
| What counts as a secret? | `core/privacy.py` → `SECRET_PATTERNS` — one set shared by the write gate, the receipt store and the card renderer |
| What does the run look like? | The `workflow_runs` row plus its normalized child tables (`workflow_nodes`, `planned_writes`, `run_sessions`, `decision_cards`) — the portal, the API and MCP read the same projection |
| Which node ids exist? | `core/orchestrator.py` → `NODE_TEMPLATES` (the frontend labels cover exactly these ids) |

If two layers disagree about one of these, one of them is wrong — and the invariant test
in `tests/test_arch_invariants.py` says which.

---

## 5. Non-negotiable guardrails

1. **No silent fallbacks.** When the configured planner or reviewer cannot produce a
   usable result, the run **fails** with a reason code and structured diagnostics. It
   never swaps in the zero-cost heuristic and still reports `api:<model>`. A silent
   fallback is the difference between "the model said something" and "we made something up".
2. **Cache only after validation.** A reply is written to `api_cache/` *after* it passed
   semantic validation. An unusable output must never poison the next run.
3. **Windows is first-class.** Backslash workspace paths split into clean segments; every
   written document uses **LF** line endings; shell scripts run under **Git Bash**, not WSL.
4. **No production sleep hooks.** No environment variable may pause, hold or park a real
   batch mid-pipeline. Debug switches do not belong in the shipped backend.
5. **The safety valve has the last word.** `safety_valves.auto_approve_writes = False`
   vetoes `workflow.approval_mode = "auto"` — the effective mode is downgraded to `smart`,
   never to a bypass. `safety_valves.model_execution_enabled` gates every runner that
   calls a model or a local CLI.
6. **This repository is public.** No personal paths, no nicknames, no credentials, no
   references to private projects or local checkouts — not in code, not in comments, not
   in docs. Personal configuration belongs in the git-ignored `flywheel.yaml`.
   `scripts/verify_repo.py` fails the build when a leak appears.
7. **Background daemons need the `serve` role.** The recovery loop, the outbox tick and
   the Feishu listener only run when the process role is `serve`. A daemon started
   anywhere else is a bug: two consumers of the same card subscription corrupt each other.
8. **A write is atomic and verified.** Write to a temporary file, swap it in, read it back
   and compare. A Skill update **appends**: every byte of the old file is the prefix of the
   new one.

---

## 6. Verification — before you say "done"

```bash
# The whole gate: hygiene → openspec → pytest (add --frontend for the build)
python scripts/verify_repo.py --frontend

# Or the pieces
pytest tests -q
openspec validate --all --strict
cd frontend && npm run build
```

`python scripts/verify_repo.py` is the one command a reviewer runs. It exits non-zero as
soon as one check fails, and it never repairs anything: a failing check is a report.

Evidence grades used across this repository: **real** (a real run) / **test** (an automated
test) / **static** (a reading of the code) / **unknown** (not verified). Claiming "fixed"
needs at least `test`; anything that touches the write path, the API or a script needs
`real`.

---

## 7. Working agreement

This repository is spec-driven. The flow is **Explore → Propose → Apply → Verify → Archive**:

- `openspec/config.yaml` holds the project context and is injected into every generated
  artifact. Read it before writing a proposal.
- A change lives in `openspec/changes/<kebab-case-name>/` with `proposal.md`, `specs/`,
  `design.md` and `tasks.md`. New capability → `feat-`, fix → `fix-`, governance → `docs-`
  or `chore-`.
- `openspec/specs/` is the current baseline. Touching a capability that has no baseline
  yet? Add its baseline with `ADDED Requirements` in the same change, then the delta.
- Every task ends with its acceptance method: 【test】 / 【real】 / 【static】.
- **Archiving is a human decision.** A change is archived after Verify passed *and* the
  real-run evidence exists — not before, and not automatically.

### Hot-file discipline

`core/orchestrator.py`, `core/feishu_*.py`, `adapters/runners/*` and `storage/db/*` are hot
files: several people and agents edit them. **Serialize the edits** — do not stack
concurrent changes on the same pipeline module before the previous one is archived. When
two changes must touch the same hot file, land one, archive it, then start the next.

### Cross-layer checklist

A change is done only when every layer that knows about it agrees:

- [ ] The backend model / enum changed → the frontend `types.ts` and `lib/labels.ts` changed with it
- [ ] A new config key → `config.py` **and** `flywheel.yaml.example`
- [ ] A new node / stage → `NODE_TEMPLATES`, the stage matrix above, and the frontend labels
- [ ] A new write target → `is_sensitive_write`, the approval gate and the card agree
- [ ] A new secret shape → `core/privacy.py` `SECRET_PATTERNS` (one set, every surface)
- [ ] A new stage side effect → the matrix above, and a test that pins it
