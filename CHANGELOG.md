# Changelog

All notable changes to **AI Flywheel** are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/).

## [0.6.2] - 2026-11-25

### Added
- **Unified list toolbar & pagination across the console** (`frontend/src/components/ListControls.tsx`, `WorkflowsPage.tsx`, `AutomationsPage.tsx`, `RulesListTab.tsx`, `CluesObservationsTab.tsx`, `AssetsFreshnessTab.tsx`, `OutcomesLedgerTab.tsx`):
  - Added reusable `ListToolbar` (keyword search, status/client/trigger filters, default `created_at DESC` ordering) and `PaginationBar` (default 10 items per page, switchable 10 / 20 / 50) across all trajectory and governance lists.
- **Bulk governance APIs & Friday Dream low-severity auto-archive** (`routes/governance.py`, `storage/db/rule_repo.py`, `storage/db/session_repo.py`, `core/dream.py`, `tests/test_console_governance_ux.py`):
  - Added `POST /api/site/observations/bulk-archive`, `POST /api/site/dissents/bulk-status`, and `POST /api/site/retro/bulk-status`, and raised default list limits with `total` metadata on `/api/site/runs`, `/api/site/outcomes`, `/api/site/observations`, `/api/site/dissents`, and `/api/site/retro`.
  - Extended Friday Dream Tier 1 (`run_dream_consolidation` and read-only `preview_dream_consolidation`) to auto-archive single-occurrence `yellow_caught` / `green_minor` observing mistakes older than 30 days while never auto-archiving `red_silent` observations.

### Changed
- **Four-subview governance redesign for "错题与分歧"** (`frontend/src/pages/RulesRetroPage.tsx`, `frontend/src/components/rules/CluesObservationsTab.tsx`):
  - Split the tab into **待我处理** (default: high-risk `red_silent` / ready-to-promote observations + open dissents), **错题观察池** (system auto-tracking banner + batch archive), **规则分歧** (dedicated view for `dissent_records` with enriched `target_title` / `target_status` and batch convergence), and **实时异常线索** (default hides resolved history, groups repeated identical signals with `×N` badges, and supports batch resolution/dismissal). Tab badge counts only actionable items in **待我处理**.
  - Compacted list rows to 1–2 line summaries with click-to-expand details.
- **Cleaner About page** (`frontend/src/pages/AboutPage.tsx`):
  - Removed third-party memory product name-drops from the hero and footer, and removed the inline `/llms.txt` collapsible raw-text panel while retaining MCP endpoint cards, the 6 MCP tool descriptions, and the footer `/llms.txt` link.

### Fixed
- **Local Skills ledger & knowledge freshness count parity** (`routes/governance.py`, `frontend/src/components/rules/AssetsFreshnessTab.tsx`):
  - `GET /api/site/skills` now merges locally probed skills from `capabilities.skills_dirs` with database effectiveness records so configured local skills appear immediately rather than rendering an empty table.
  - `GET /api/site/knowledge` now merges all expired and tracked cards from `freshness_map` beyond the newest filesystem slice, aligning `expired_total` 1:1 with `growth-metrics.live.expired_card_count`.

## [0.6.1] - 2026-11-25

### Added
- **3+1 ecosystem architecture document** (`docs/architecture.svg`):
  - Added a sanitized, vendor-neutral architecture view built around a **Non-SSOT** Client Gateway & Distribution Layer (Model Routing `/v1`, MCP Hub, Static Asset Sync) and three kernel roles: the AI Flywheel governance & evolution engine (MCP 6 Tools + 4 Resources, execution evidence gate, P00–P07 controlled pipeline, SQLite FTS5, 5+1 control plane, approval channels), the optional External Profile & Equipment Repo (`profile.override_dir` policies/triggers, `skills/`, `agents/`; read-only mount with P04 append-only skill updates), and the Mounted Knowledge Base (human-readable Git-tracked Markdown / Obsidian Wiki, `.wiki-schema.md` write gate, FTS5 read-back and approved P04 writes).
- **Unified six-section `AGENTS.md` constitution** (`AGENTS.md`):
  - Reorganized the repository contract into six sections — 3+1 ecosystem boundaries, architecture / package map / single sources of truth, the P00–P07 stage matrix and the eight non-negotiable guardrails, working agreement and hot-file discipline, verification and evidence grades, and Git collaboration rules. Every existing hard contract is preserved; the hot-file list now names `adapters/channels/feishu/*` as the implementation and `core/feishu_*.py` as thin compatibility shims.

### Changed
- **Version `0.6.1`** (`backend/ai_flywheel/__init__.py`, `pyproject.toml`, `frontend/package.json`, `profile/site.yaml`, `README.md`):
  - Bumped the single version source and its parity layers to `0.6.1`, embedded the 3+1 ecosystem diagram in `README.md`, and added a concise release guide for this version.
- **Public-repo hygiene now scans SVG** (`scripts/verify_repo.py`):
  - Added `.svg` to `SCAN_SUFFIXES`, so personal paths, nicknames, private repository names and credentials are rejected in diagrams as well as in code, configuration and prose.
- **OpenSpec baseline closure** (`openspec/`):
  - Archived eight completed v0.5 / v0.6 changes — automation console, control-plane console views, onboarding CLI & neutral wiki gate, webhook approval & rule lineage, Feishu approval-loop UX & config, approval-channel abstraction, frontend page split, and orchestrator pipeline package — and synced their delta specs into `openspec/specs/`; `openspec/changes/` now contains only `archive/`.

### Fixed
- **Stale console wording and package map in `openspec/config.yaml`**:
  - Corrected `4+1 Web 控制台` to `5+1 Web 控制台` and aligned the directory / layer description with the v0.6.1 source tree (`core/pipeline/`, `core/automation.py`, `core/approval_channel.py`, `core/doctor.py`, `core/onboarding.py`, `adapters/channels/`, `routes/`, plus the `core/feishu_*.py` compatibility-shim note).

## [0.6.0] - 2026-11-25

### Added
- **Governance & Evolution Control Plane Console (P0-1 & P0-2)** (`core/dream.py`, `routes/`, `frontend/src/App.tsx`, `frontend/src/pages/HomePage.tsx`, `frontend/src/pages/RulesRetroPage.tsx`, `frontend/src/pages/WorkflowsPage.tsx`, `frontend/src/pages/AutomationsPage.tsx`, `frontend/src/pages/ConnectPolicyPage.tsx`, `README.md`):
  - Added zero-side-effect `preview_dream_consolidation()` in `core/dream.py` and exposed `GET /api/site/skills` (Skill effectiveness ledger), `GET /api/site/dream` (latest audit + read-only preview), and `POST /api/site/dream/run` (`dry_run` preview or explicit execution + core-card refresh); enriched `GET /api/site/knowledge` with card freshness and helpfulness metrics (`freshness_tier`, `valid_until`, `is_expired`, `surface_count`, `helped_count`).
  - Rebuilt the `5+1` console around the **Governance & Evolution Control Plane** positioning: renamed primary nav to 今日态势 / 沉淀与审批 / 自动化调度 / 规则与进化 / 接入与设置 + 关于; replaced hardcoded schedule copy on `HomePage` with live `/api/site/automations`, real verification rate, rule health, mistake pool, safety valves, and derived core-card preview; deduplicated schedule toggles in `ConnectPolicyPage` and elevated safety valves.
  - Added **Skill 与保鲜**, **证据台账 (Outcomes)**, and **FLYWHEEL-CORE.md** live preview/rebuild modal to `RulesRetroPage`; added **噪音过滤漏斗** and **模型调用遥测** badges to `WorkflowsPage`; added a **周五分层蒸馏控制卡** (preview & run) to `AutomationsPage`.
  - Updated `README.md` and `AboutPage.tsx` with the clarified control-plane positioning, explicit comparison with context databases / memory engines (e.g., OpenViking, Mem0), MCP execution loop, and non-goals.
- **Optional P04 Local Git Audit Trail (`knowledge_sink.auto_git_commit`)** (`core/pipeline/git_commit.py`, `config.py`, `flywheel.yaml.example`, `tests/test_pipeline_git_commit.py`):
  - Added opt-in `knowledge_sink.auto_git_commit` (default `false`): when enabled and P04 writes files inside a Git repository, stages and commits **only** the exact P04-written paths (`git commit --only`) with `run_id` and `approval_source` (`portal` / `feishu` / `auto`) in the commit message, leaving unrelated dirty/untracked files untouched and never running `git push`. Git errors are audited (`p04_git_commit_failed` / `p04_git_commit_skipped`) without breaking verified writes.
- **Onboarding & Diagnostics CLI (`flywheel doctor` & `flywheel init-client`)** (`core/doctor.py`, `core/onboarding.py`, `cli.py`, `tests/test_onboarding_cli.py`):
  - Added read-only `flywheel doctor [--json]` covering 7 check groups (`config`, `storage`, `knowledge_sink`, `skills`, `runner`, `safety_valves`, `channels`) with exit code `0` on healthy/warn and `1` on failures.
  - Added `flywheel init-client <claude-code|codex|claude-desktop|antigravity> [--transport http|stdio] [--apply] [--target <path>] [--json]` defaulting to safe dry-run; `--apply` backs up existing config, atomically merges only the `ai-flywheel` MCP entry (JSON or TOML) with LF line endings, preserves sibling entries, and is idempotent.
  - Added neutral `knowledge_sink.postflight_hook_script` config key (env `FLYWHEEL_POSTFLIGHT_HOOK_SCRIPT`) with 100% backward-compatible fallback to `wiki_d2_postflight_script`.
- **Generic Webhook Notification Channel & Bi-Temporal Rule Lineage (P2-1)** (`adapters/channels/webhook.py`, `adapters/delivery.py`, `storage/db/schema.py`, `storage/db/migrations.py`, `storage/db/rule_repo.py`, `core/activation.py`, `frontend/src/components/rules/RulesListTab.tsx`, `tests/test_webhook_and_rule_lineage.py`):
  - Added vendor-neutral `delivery.webhook` channel (`enabled`, `url`, `secret`, `timeout_seconds`, `notify_on_fyi`; env `FLYWHEEL_WEBHOOK_URL` / `FLYWHEEL_WEBHOOK_SECRET`) with `core/privacy.py` outbound redaction, optional `X-Flywheel-Signature: sha256=<hex>` HMAC signing, non-fatal delivery on network/HTTP errors, and config secret masking.
  - Added lightweight bi-temporal rule lineage columns (`supersedes_rule_id`, `effective_from`, `effective_to`) with idempotent SQLite migration: superseding an active rule retires it with `effective_to` and `rule_superseded` audit trail; dormant/retired transitions stamp `effective_to` and reactivation clears it; rendered lineage badges on rule cards.

### Changed
- **Modular Frontend & Backend Architecture (P1-1, P1-2, P1-3)** (`frontend/src/components/workflows/`, `frontend/src/components/rules/`, `core/approval_channel.py`, `adapters/channels/feishu/`, `routes/`, `core/pipeline/`, `core/orchestrator.py`, `app.py`):
  - Decomposed `WorkflowsPage.tsx` (1,103 → 346 lines) into 8 sub-components under `components/workflows/` and `RulesRetroPage.tsx` (1,084 → 298 lines) into 6 sub-components under `components/rules/` (with shared `CoreCardModal`).
  - Introduced `core/approval_channel.py` (`ApprovalChannel` Protocol & `ApprovalBusyError`), moved Feishu implementation into `adapters/channels/feishu/` (keeping explicit re-export shims in `core/feishu_*.py`), and split `app.py` (798 → 168 lines) into 4 domain `APIRouter` modules under `routes/` preserving all 53 application routes.
  - Refactored `core/orchestrator.py` (1,845 → 48 lines compatibility facade) into `core/pipeline/` (`constants`, `events`, `helpers`, `recovery`, `git_commit`, `coordinator`), preserving the P00–P07 side-effect contract and monkeypatch seam.

### Fixed
- **Feishu interactive approval configuration, self-check UX, and text fallback clarity** (`adapters/channels/feishu/`, `adapters/runners/normalizers.py`, `runtime.py`, `routes/`, `frontend/src/pages/ConnectPolicyPage.tsx`, `frontend/src/pages/WorkflowsPage.tsx`):
  - Sanitized approval `reasons` in plain-text Feishu fallbacks, normalized trailing punctuation so `。；` never appears, rendered Chinese target-kind labels (`KIND_LABEL`), and distinguished interactive-off / self-check-unverified / link-unavailable footers instead of always claiming buttons are unavailable.
  - Added `FeishuClickListener.wake()`, hot-initialization of the Feishu interactive layer on `/api/site/config` updates, fast-fail when running `feishu-check` with `interactive=false`, and enriched `GET /api/site/feishu-check` with `enabled`, `interactive`, `interaction_ready`, and `availability`.
  - Exposed interactive approval card toggle, extra `approver_open_ids`, live link/check status, and a one-click self-check trigger with polling guidance in the Web Console's Connect & Settings page, and aligned the Workflows approval drawer status text.
- **Strip `<private>` blocks before generating document/skill filename slugs** (`adapters/runners/contracts.py`): prevented `<private>...</private>` content inside session titles from leaking into heuristic-generated `relative_path` filenames and outbound notifications.

## [0.5.0] - 2026-11-25

### Added
- **Automation Console & Hot-Reloadable Scheduler** (`core/automation.py`, `runtime.py`, `app.py`, `frontend/src/pages/AutomationsPage.tsx`):
  - Promoted APScheduler from a local `create_app` variable into `AutomationManager` owned by `FlywheelRuntime`, strictly pinned to `Asia/Shanghai` timezone (`core/time_utils.SHANGHAI_TZ`) and gated on the `serve` process role.
  - Added REST control plane: `GET /api/site/automations` (live scheduler state, `next_run_at`, daily/cumulative KPI metrics, recovery watchdog & Feishu daemon status, and recent runs), `PUT /api/site/automations/schedule` (strict 5-field cron validation + live hot-reload without restarting `serve`), and `POST /api/site/automations/recover` (one-click stale-run recovery pass).
  - Removed `workflow.schedule_enabled` and `workflow.daily_cron` from `STARTUP_ONLY_PATHS`: editing schedule settings via `/api/site/automations/schedule` or `/api/site/config` now reloads the live job immediately (`restart_required: false`) and broadcasts `automation_updated` over SSE.
  - Added the **自动化任务** (`/automations`) page in the Web Console sidebar (`5+1` console): master auto-execution toggle + live status banner, visual Cron schedule editor (preset frequency + Shanghai hour/minute pickers synced with 5-field Cron input + Friday weekly Dream checkbox), today/cumulative KPI tiles, background daemon watchdog cards (Recovery Watchdog & Feishu channel), and filterable scheduled/manual execution history.

## [0.4.1] - 2026-11-25

### Fixed
- **External `--config` / `FLYWHEEL_CONFIG` console & profile resolution** (`config.py`, `app.py`):
  - `load_config(config_path, base_dir)` no longer overwrites an explicit `--base-dir` with `config_path.parent`.
  - Launching `flywheel --config <external-path> serve` (where `base_dir` is outside the repository checkout) now falls back to the checkout's built `frontend/dist` and bundled `profile/` instead of returning `404` on `/` or missing default policies.

## [0.4.0] - 2026-11-25

### Added
- **Feishu interactive approval loop** (`adapters/lark_cli.py`, `core/feishu_listen.py`, `core/feishu_check.py`, `core/feishu_card.py`, `core/feishu_channel.py`, `storage/outbox.py`):
  - Awaiting batches can be approved / routine-approved / cancelled directly on a Feishu interactive card; the execution result is written back to the same card. Clicks are validated (approver set, delivered message ref, offered options, first-answer-wins with `event_id` dedup) and resume the run through the atomic claim primitive with `source=feishu` audit trails.
  - Managed outbox: deliveries are registered before sending (crash-safe), lease-guarded, retried on a 0–55 min backoff with a 57-minute give-up window, requeued at startup when the ask card is still open; unknown send outcomes keep retrying the same form and key instead of switching shape. Interactive cards fall back to plain text whenever the link is not trusted (`serve` down, listener reconnecting, self-check invalid, 3 explicit failures), recording why.
  - Self-check gate (`flywheel feishu-check`): runs inside `serve` reusing the single click listener; sends a test card, patches it, waits for an approver's click and rewrites it. Interactive cards are only sent after it passes; any fingerprint change (lark-cli file/version, approver set, recipient, identity) invalidates it. Feishu apps must subscribe to `card.action.trigger` (long connection) — there is no platform preflight, this check is the only reliable probe.
  - lark-cli invocation layer: resolves the native executable (PATH-only, never the current directory; npm `.cmd` shims resolved to their package's native exe or node entry; no shim fallback), sends interactive cards, patches by `message_id` via `api PATCH ... --data -` (there is no `im messages patch` on lark-cli 1.0.68), and consumes `card.action.trigger` as flat NDJSON. All outbound text (ask cards, text fallbacks, alerts, completion notices) is sanitized: control/format characters stripped, whitespace folded, `& | < > ^ % !` fullwidthed, links deactivated, `<private>` sections removed and secret-shaped strings masked before it reaches Feishu.
  - Approval cards list every write (batch cap) with action / path / title / summary / reason; sensitive items add a body excerpt and a content hash with an explicit "excerpt ≠ full text" note; cards stay under a 24 KB UTF-8 budget by shrinking excerpts first — items are never dropped. Terminal cards carry no buttons.
  - Card layout follows Feishu's card style guide: header icon, batch / approval-mode subtitle and a status tag; one outlined block per write (a bold type line, the title, small grey detail lines — empty fields are omitted); the main action is a full-width filled button with secondary / danger actions beneath it, always at the bottom.
  - Card state sync never rewrites an open card while the link is healthy, so its buttons stay clickable; when the listener becomes unavailable the open card is switched to a "handle it on the computer" notice and the buttons are restored once it is back. The outcome of an answered card is written back right away instead of waiting for the next outbox tick, and a busy hint (another batch executing) is a one-off notice above the actions that leaves them clickable. Legacy one-shot delivery rows are migrated to text deliveries exactly once.
  - `flywheel serve` starts the 30-second outbox/recovery loop only with the `serve` process role; non-serve processes register deliveries and send their first attempt (text) inline. The click listener runs only while interactive mode is on (the app's `card.action.trigger` subscription is single-consumer, so an idle listener would block whoever needs it): it stays idle when `interactive` is off and stops its consumer within seconds when interactive mode or the channel is turned off at runtime, restarting when they come back. The console shows the Feishu link status on the approval drawer. Requires lark-cli >= 1.0.64; `command` / `command_args` / `identity` changes need a restart.
- **OpenSpec spec-driven development flow** (`openspec/`):
  - Project context and repository conventions live in `openspec/config.yaml` and are injected whenever a change artifact is generated.
  - First baseline specs: `knowledge-write-gate` (controlled knowledge writes + schema-aware Wiki gate) and `workflow-orchestration` (the P00–P07 run lifecycle). Other capabilities get their baseline when first touched.
  - Claude Code skills / commands for the OpenSpec workflow under `.claude/`.
- **Model call telemetry and transport-failure retries** (`adapters/runners/clients.py`, `core/orchestrator.py`, `config.py`, `flywheel.yaml.example`):
  - `runner.api.reasoning_effort` is optional: empty (the default) keeps the parameter out of the request JSON, so the runner stays portable to endpoints that do not know it; a non-empty value is sent and enters the cache key, so switching effort never reuses a reply produced with another one.
  - `runner.api.transport_retries` (default 1) retries a timeout or a refused connection immediately, without backoff — a timeout is not a rate limit, so waiting does not make the next attempt faster. HTTP status errors are never retried and stay sanitized as `Model API returned HTTP <status>`.
  - Every call leaves a trace: attempts, wall-clock duration, the model that actually served it, prompt / completion / reasoning tokens (read from `usage.completion_tokens_details.reasoning_tokens` or from the flat `usage.reasoning_tokens`). The trace rides on the P02 and P03 node payloads. A gateway that reports no usage, or a call replaced by a stub, leaves the trace absent instead of breaking the run.
  - A transport failure leaves a sanitized summary on the failed node — stage, timeout threshold, attempts and exception type — and never the gateway address, the request or the reply.
- **The safety valve vetoes auto-approval** (`config.py`):
  - `safety_valves.auto_approve_writes = False` now vetoes `workflow.approval_mode = "auto"`. The valve has the last word: the effective approval mode is downgraded to `smart` — routine writes pass, sensitive ones still wait for a human — instead of bypassing the gate.
  - `effective_auto_approve` requires the valve as well, so `workflow.auto_approve_writes` no longer approves on its own.
- **A truthful name for the model-execution valve** (`config.py`, `adapters/runners/clients.py`):
  - `safety_valves.model_execution_enabled` (env `FLYWHEEL_MODEL_EXECUTION_ENABLED`) gates every runner that calls a model or a local CLI. Unset means the older `cli_execution_enabled` decides, so an existing configuration keeps its meaning.
- **Exact counts in the publication layer** (`storage/db/`):
  - `count_sessions`, `count_workflow_runs`, `count_outcomes`, `count_retro_items` and `count_real_verified_natural_outcomes` answer with `COUNT(*)`. The portal no longer caps "20 sessions" at the size of one page.
- **The settings page says when a restart is needed** (`frontend/src/pages/ConnectPolicyPage.tsx`): the response of `PUT /api/site/config` carries `restart_required` and `restart_reasons`; the page lists the settings that only take effect after a restart.
- **`AGENTS.md` — the onboarding map for engineers and agents** (`AGENTS.md`):
  - The architecture and package map, the P00–P07 stage side-effect matrix (who may read what, who may write what), the single-source-of-truth table (version, runner mode, write sensitivity, the mounted Wiki contract, the business timezone, the secret patterns, the run projection) and the non-negotiable guardrails.
  - The guardrails are the ones that broke before: no silent fallbacks, caching only after validation, Windows as a first-class platform, no production sleep hooks, the safety valve has the last word, the public repo carries no personal data, and background daemons run only in the `serve` role.
  - A cross-layer checklist (backend ↔ frontend ↔ example config ↔ stage matrix) and a hot-file discipline: serialize the edits on the pipeline modules instead of stacking concurrent changes.
- **`scripts/verify_repo.py` — the one-shot verification gate** (`scripts/verify_repo.py`):
  - One command runs the public-repo hygiene rules, the CHANGELOG heading check, `openspec validate --all --strict` and the whole test suite; `--frontend` adds the production build.
  - It exits non-zero as soon as a check fails and never repairs anything: a failing check is a report, not a fix.
- **`tests/test_arch_invariants.py` — the architecture invariants, as tests** (`tests/test_arch_invariants.py`):
  - Public-repo hygiene: no personal identifier, local path or credential in any non-ignored file; no UTF-8 BOM in a Python source; no `_unused_` method; no `if False:` branch; no production hold/park hook in the backend; no duplicated CHANGELOG heading inside a release section.
  - The P03 repair skeletons parse as JSON and carry every required item key; the routed variant adds `wiki_route` to the item itself.
  - Cross-layer parity: the version is the same in all four layers, the example config matches the runner defaults, the frontend status union matches the backend enum, the frontend labels cover every node id and signal kind, and the Feishu card and the backend agree on what a sensitive write is.
  - A real `serve`-role lifespan smoke test: the health endpoint answers, the derived core card exists, the recovery thread is alive and really ran, and the app shuts down without an error.
  - The Windows contract: a backslash workspace path splits into clean segments, and a written document is LF.
  - The pipeline contract: a rejected batch leaves the mistake pool and the dissent table empty, and an unparsable model reply creates no cache file.

### Changed
- **The pipeline side-effect contract is now explicit** (`core/orchestrator.py`):
  - P00 and P01 only read; P02 and P03 are read-only with respect to knowledge, observations, dissents and Skills; P04 is the single controlled write stage; P05 settles the queues and refreshes the core card once; P06 and P07 report, deliver and release the exclusive lock.
  - The FTS index is rebuilt **once for the whole batch**, not once per document, and the rebuild never masks the write failure that is already on its way out.
- **Sessions are conserved across P01 → P02 → P05** (`core/orchestrator.py`, `adapters/runners/prompts.py`):
  - In model mode the batch admits at most as many sessions as the reviewer will actually read. A session that was admitted but never sent to the reviewer is settled back to `pending` — it still owes the flywheel a card, and a later batch picks it up again.
  - A long session reaches the reviewer as a head + tail excerpt with the middle explicitly marked as omitted, so the tail of a conversation is never lost to a character budget.
- **A reply is cached only after it passed validation** (`adapters/runners/clients.py`): the model reply is written to `api_cache/` after the semantic check, never before. An unusable or unparsable output can no longer poison the next identical batch.
- **P03 drops a credential before it reaches the plan** (`adapters/runners/normalizers.py`): an item whose body carries a secret is sanitized or dropped before anything is written, and the batch quota counts the writes that actually survive.
- **A cancelled run is not a partial run** (`models.py`, `storage/db/workflow_repo.py`): `RunStatus.CANCELLED` marks a batch the maintainer refused at the approval gate — nothing was written. It is deliberately distinct from `PARTIAL`, which means "half of it was done".
- **Approval claim, execution heartbeat and crash recovery** (`storage/db/workflow_repo.py`, `core/orchestrator.py`, `core/heartbeat.py`, `app.py`, `cli.py`):
  - Approve and cancel now share one atomic claim primitive: the lock upsert, the `AWAITING_APPROVAL → RUNNING` (or `→ CANCELLED`) transition, the approved items, the ask-card state and the audit event land in a single write transaction. Concurrent approvals or an approve/cancel race can only produce one winner; a run whose lock is held by another run is rejected with HTTP 409 (plus the lock's expiry) instead of executing without the lock.
  - Lock acquisition is a single conditional upsert (free / expired / no-expiry / own-run refresh) shared by `start_run` and the claim; a lock row with no expiry is deleted by migration.
  - Executing runs renew the lock and a heartbeat: a background daemon thread every 15 s plus synchronous checkpoints before each P04 write, each P05 settlement and the P07 delivery. Losing the lock mid-run fails the run under the fail-run contract ("执行期间失去独占锁"). The heartbeat thread stops when a run suspends, so a suspended run's lock still expires after its TTL.
  - All terminal states (PASS / PARTIAL / CANCELLED / FAILED) go through one conditional terminal entry (status first, then ask card, audit and own-lock removal); `save_workflow_run` refuses to overwrite a terminal status with a different one, so a reaped run cannot be resurrected by a zombie executor.
  - `serve` reaps runs whose heartbeat stopped (startup + every 30 s): failed node, downstream SKIPPED, sessions back to `pending`, ask card set to `failed` with a result summary. Ask-card state is reconciled against run status (a card stuck at `decided` for a `FAILED` run is corrected). `flywheel serve` passes a 10 s graceful-shutdown window so these threads actually stop.
  - `FlywheelRuntime` / `FlywheelDB` accept an injectable clock; lock/claim/recovery functions take explicit `now` parameters.
- **Practice cards written into a schema-governed Wiki are attributed on evidence, never guessed** (`adapters/wiki/router.py`, `core/orchestrator.py`):
  - Project routing checks the source session's workspace path first, then the project id / directory name found in the draft (alphanumeric-boundary match; fragments shorter than 3 characters and generic words such as `core` or `web` are ignored).
  - A planned document with no project evidence is no longer written into an arbitrary project. P03 drops it and surfaces it in the node summary, the node payload and a `planned_doc_unrouted` audit event; P04 records a `SKIPPED_UNROUTED_PROJECT` receipt instead of failing the batch.
  - New optional `knowledge_sink.practice_fallback_project` names an explicit catch-all project directory.
- **Knowledge documents are routed by the mounted Wiki contract: the planner declares, evidence validates** (`adapters/wiki/router.py`, `adapters/wiki/schema.py`, `adapters/runners/`, `core/orchestrator.py`, `models.py`):
  - P03 reads the routing candidates off the mounted Wiki on every run: the page types declared in `.wiki-schema.md` that the landing adapter can materialise (currently `project-practice-note`), and every project directory under the projects partition that hosts `实战笔记/`. No project name is hardcoded; an empty candidate set writes nothing.
  - The P03 prompt hands these candidates to the model and requires every `knowledge_doc` to carry `wiki_route` (`page_type`, `project`, `reason`), or an explicit `null` project for notes that merely mention a project or span projects.
  - Workspace and draft evidence no longer select the project; they only validate the declaration. A declaration outside the candidate set, a note declared as unattributed, or a declaration contradicted by the evidence is not written and is traced (`planned_doc_unrouted` with a reason code, the declaration and the evidence hits). A note whose draft names no project is still written on a consistent declaration. Routed notes are traced in `planned_doc_routed`.
  - In `api` / `cli` mode a knowledge document without a routing declaration is not written. Only `dry_run` plans still use evidence-based routing, and `knowledge_sink.practice_fallback_project` applies to them alone.
  - A mounted Wiki whose `.wiki-schema.md` cannot be read fails the run at P03, where the candidates are discovered, instead of being treated as "nothing routable" (the failure used to surface at the first write in P04).
- **`api` / `cli` runs fail at P03 when the model's write plan is unusable** (BREAKING) (`adapters/runners/`, `core/orchestrator.py`):
  - A reply that cannot be parsed, or whose items are all invalid after field completion, now fails P03 under the fail-run contract (sessions back to `pending`, lock released, alert). Previously the zero-cost heuristic planner silently took over while the run still reported `api:<model>`.
  - An explicit empty plan from the model is honoured as "nothing to write". Switch `runner.mode` to `dry_run` when the heuristic plan is what you want.
- **`api` / `cli` runs fail at P02 when the model's review is unusable** (BREAKING) (`adapters/runners/`, `core/orchestrator.py`):
  - A reply that is not a JSON object, has no `findings` list, or reviews none of the sessions it was given now fails P02 under the fail-run contract (sessions back to `pending`, lock released, alert). Previously the zero-cost heuristic reviewer silently took over while the run still reported `api:<model>` and the node showed `PASS`; its template mistake observations entered the pool before the approval gate, where no rejection could remove them.
  - The reason is traced in the log, the P02 node summary and payload, and a `review_unavailable` audit event: a reason code, the reply length, why the JSON decoder rejected it, and whether the format repair ran or its call failed (exception type only). The reply itself is never stored. When the reply was cached, the failure names the file to delete.
  - Every P02 node now states where its review came from (API model, local CLI or the local heuristic reviewer) in the summary and the payload. A runner that does not say is reported as undeclared, never guessed.
- **The heuristic planner no longer authors Skills** (BREAKING) (`adapters/runners/heuristic.py`):
  - Zero-config `dry_run` runs plan knowledge docs and rules only. Repeated-workflow candidates are reported in the P03 node summary, the node payload and a `skill_candidates_deferred` audit event instead of becoming template-body `SKILL.md` files.
- **A Skill update appends a section, it never rewrites the file** (BREAKING) (`adapters/skills.py`, `adapters/runners/`, `app.py`, `frontend/src/`):
  - `action="update"` means "append one section at the end of the existing `SKILL.md`". Every byte of the existing file is kept as the prefix of the new file (a constructive invariant — a violation raises instead of writing), and updating a target that does not exist raises `Cannot update missing skill` rather than quietly turning the update into a create.
  - The appended text is normalized deterministically before it lands: an accidental YAML frontmatter and a leading H1 are stripped, a body without an H2 is wrapped in `## {title}（{today} 补充）`, and normalizing twice yields the same text. What is written is exactly what the approval card previews.
  - The append keeps the original file's line ending (CRLF stays CRLF), backs the file up before writing, replaces atomically through a temporary file and reads back to compare SHA-256. The receipt reports the original line count, the added line count and the total.
  - The P03 prompt now distinguishes creating from updating: an update item carries only the incremental section to append, and the prompt hands the model the matched Skills' existing H2 outline (`existing_skill_sections`) so it does not repeat a heading that is already there. A plan item is dropped and traced when the target is missing (`UPDATE_TARGET_MISSING`), when the target's frontmatter is invalid (`UPDATE_TARGET_INVALID`), or when the addition says nothing that is not already in the file (`UPDATE_NO_NEW_CONTENT`).
  - New read-only endpoint `GET /api/site/skills/{skill_name}/preview` returns the existing file's size, H2 outline and full text. On an update item the approval card shows that outline and offers an expandable "compare with the current file" panel next to the new section.
- **The monoliths became packages** (`adapters/runners/`, `adapters/wiki/`, `storage/db/`):
  - `adapters/runners.py` is now a package: `clients` (CLI + API), `heuristic` (dry-run), `contracts` (the wire contract, the diagnostics and the text helpers), `prompts`, `normalizers`, `outcomes`.
  - `adapters/wiki_gate.py` is now `adapters/wiki/`: `schema` (the `.wiki-schema.md` contract, cached per file revision), `router` (the candidates and the evidence), `gate` (pre-write validation), `validator`, `t5_adapter`, `postflight`. The old import path keeps working as a re-export.
  - `storage/db.py` is now `storage/db/`, split per repository (`workflow_repo`, `session_repo`, `rule_repo`, `knowledge_repo`, `asset_repo`, `audit_repo`) with `schema.py` and `migrations.py` beside them.
  - `core/time_utils.py` is the single home of the business timezone: every product-facing date (freshness stamps, record ids, weekly distillation keys) is a **Shanghai** date, never a host date.
- **Background runs and approvals answer immediately** (`app.py`):
  - `POST /api/site/runs` and `POST /api/site/runs/{run_id}/approve` accept `?background=true`: the lock is taken in the request thread (so a busy lock is a clean 409 instead of a failure nobody sees) and the pipeline runs on a daemon thread that reports through the SSE stream.
  - The SSE stream keeps the connection alive with a periodic comment, so a proxy does not close an idle console.
- **One version, one place** (`__init__.py`, `pyproject.toml`, `profile/site.yaml`, `frontend/package.json`): the version is `0.4.0` and lives in `ai_flywheel.__version__`. The portal, the MCP server and `/llms.txt` read it from there instead of repeating the string.
- **Only the rules that are in force are shown as such** (`core/publication.py`): `recent_rules` on the Home and About pages holds the verified and the trial rules; retired, dormant and needs-revision rules keep their own counters and are no longer counted as active.
- **The approval selection follows the write set** (`frontend/src/pages/WorkflowsPage.tsx`): a batch that reaches the approval gate with N planned writes is approved as a whole, instead of keeping the empty selection it had while it was still running — 「同意并写入」 no longer approves nothing.
- **The runner mode names its model** (`frontend/src/lib/labels.ts`): `api:<model>` reads as 「API 模型 · <model>」 instead of falling back to the raw string.
- **Git closure publishes to the configured remote** (`adapters/delivery.py`): `delivery.git_closure.remote` / `.branch` are wired. The push is skipped — never fatal — when the clone has no such remote or is on another branch; the commit stays the deliverable.
- **The approval claim names the lock state** (`storage/db/workflow_repo.py`): the result and the audit event carry `lock_continuously_held`. The historical `reacquired` key is kept with the same value, so nothing downstream breaks.
- **The Feishu notice says what it sends** (`adapters/delivery.py`): the plain-text notification is `send_feishu_text_notice`; `send_feishu_card` stays as a backward-compatible alias.

### Fixed
- **Startup bootstrap, SQLite concurrency and the wiki gate** (`runtime.py`, `storage/db/`, `storage/outbox.py`, `adapters/wiki/`, `adapters/knowledge.py`):
  - `FlywheelRuntime` builds the startup bootstrap again: the FTS index is synced, the probed local Skills are registered in `skill_assets` and `data/FLYWHEEL-CORE.md` is generated (`skip_fts_sync=True` still skips all three).
  - SQLite runs in WAL mode; every connection sets `busy_timeout` and `foreign_keys`, and the lock / claim / terminal / outbox transactions take the write lock up front (`BEGIN IMMEDIATE`). Concurrent approvals or an approve/cancel race now produce exactly one winner instead of "database is locked".
  - The Wiki D2 script runs under Git Bash on Windows instead of the WSL launcher, and the D2 work-root environment variable name is configurable instead of hardcoded in the public source.
- **Windows is a first-class platform** (`adapters/wiki/router.py`, `adapters/knowledge.py`):
  - Wiki routing splits Windows workspace paths on backslashes, so a folder name matches exactly instead of only as a substring; the UTF-8 BOM is detected through an explicit escape instead of an invisible literal character.
  - Knowledge documents are written with LF line endings on every platform, and the read-back check plus the "update changed nothing" check both compare the normalized content.
- **Feishu approval loop defects** (`core/feishu_channel.py`, `core/feishu_listen.py`, `core/feishu_check.py`, `core/orchestrator.py`, `storage/outbox.py`):
  - The ask card carries each item's `requires_approval` flag, so a knowledge document or rule that needs approval is treated as sensitive and 「仅同意常规项」 never approves it; the pressed option is recorded on the decision card.
  - A text fallback that failed to go out is retried and ends in `given_up` instead of being stranded; abandoned deliveries are requeued once at startup, so the 57-minute give-up window is respected instead of restarting every 30 seconds.
  - The click listener waits for interactive mode to be switched off and on again after repeated permanent failures, instead of respawning the consumer in a tight loop.
  - The interactive self-check caches its fingerprint (no full-binary hash and no `lark-cli --version` spawn on every status tick) and records the running state, so an interrupted check is detected and cleaned up at startup.
  - The legacy one-shot delivery records the delivery id the outbox registers (`del-<card>-feishu_cli`).
- **Gate, hook and public-repo hygiene** (`adapters/wiki/`, `adapters/knowledge.py`, `adapters/runners/`, `core/hooks.py`):
  - The document read surface refuses the excluded paths, the schema file itself and — on a schema-governed Wiki — the pages the schema keeps out of the default surface.
  - A model API failure reports only the HTTP status code; the request URL is no longer embedded in the error.
  - `apply_patch` is no longer treated as a read-only tool, and the read-only command check reads the command out of a structured tool input.
- **Normalized practice cards stay faithful to the draft** (`adapters/wiki/t5_adapter.py`):
  - Body sections are mapped from the draft's own sections: every fact is kept, and the recorded anomaly becomes 「错误示范」 instead of boilerplate; empty sections say so instead of inventing content.
  - `description` and 「背景与结论」 use a real draft statement rather than the raw session key; `record_id` stays unique for CJK-only titles.
- **Model plan items are no longer dropped silently in P03** (`adapters/runners/contracts.py`, `adapters/runners/normalizers.py`):
  - The P03 output contract lists the required per-item fields (`title`, `summary`, `relative_path`, `source_session_keys`, …). The v0.2 prompt rewrite had dropped them from the JSON skeleton, so every model item since then failed validation.
  - A missing `title` or `summary` is completed from fixed sources (rule payload, frontmatter, heading, first sentence) instead of dropping the item; completions are counted in the plan diagnostics.
  - Every dropped or over-limit item is traced in the log, the P03 node summary and payload, and a `plan_items_dropped` audit event. The P03 summary and payload state which planner produced the plan.
  - A Skill plan item's action must match its target directory: `update` on a missing directory is dropped (never a new directory labelled as an update), `create` on an existing one is relabelled `update`. The approval reason of an `update` states that the whole `SKILL.md` is replaced (the original is backed up first).
- **`flywheel hook-forward` never reached the server** (`cli.py`): an inner `import urllib.error` shadowed the module-level `urllib.request`, so every call raised `UnboundLocalError`, fell into the fail-open branch and answered `{"continue": true}` without contacting the service.
- **The public export leaks no local path** (`core/publication.py`): the exported snapshot strips absolute paths from the write paths, the node artifacts and the node payloads, and masks credentials in the digest, the write summaries and the retro items. The private portal view is unchanged.

### Removed
- **Dead code, confirmed without callers first**: `PolicyEngine.load_taxonomy()` and `profile/taxonomy.yaml`, `FlywheelDB.update_decision_card_state()`, `feishu_card.FIELD_LIMITS`, `feishu_listen._PERMANENT_MARKERS`, `triage._RAW_ID_TITLE_RE`, the unused `FakeClock._monotonic`, the unused `FeishuChannel._last_avail` and the unused `avail` argument of `_sync_cards`, and the frontend components `PipelineDagCanvas` / `FlywheelLoopDiagram` together with the types and labels only they used.

## [0.3.0] - 2026-11-23

### Added
- **Two-Level Session Triage Filter** (`core/triage.py`, `adapters/sessions.py`, `core/orchestrator.py`):
  - Level-1 0-token rule filter skips subagent/system fragments (`agent-*`, `subagent-*`, `warmup-*`), short/empty sessions (`< 600` chars), and low-signal temporary chat workspaces (`*Chat` gray-list requires `>= 1500` chars + `>= 2` project/knowledge signals + `>= 2` turns), while always admitting formal project/wiki workspaces and explicit user/MCP submissions.
  - Extracts true workspace `cwd` from Codex/Claude Code JSONL headers and strips injected `<app-context>` / `developer` / `system` boilerplate so both triage and P02 review operate on real dialogue.
  - Level-2 P02 semantic worthiness gate (`worthy: true|false`) explicitly authorizes skipping one-off Q&A/translations/chitchat instead of fabricating low-value notes.
- **Two-Track Mistake Observation Pool** (`mistake_observations` table):
  - Grades extracted mistakes as `red_silent` (fast-track promotion on occurrence #1), `yellow_caught` ("Rule of Three": promotes to `flywheel_rule` only when `occurrence_count >= 3`), or `green_minor` (observing only), preventing rule bloat.
- **Dissent & Counter-Evidence Preservation** (`dissent_records` table):
  - Automatically captures traceable counter-evidence whenever a rule check fails verification or P02 extracts an exception, without overwriting the original rule.
  - Surfaces open dissents in `preflight_activate` context and inline on rule cards (`+ 记例外` / `已吸收` / `忽略`).
- **Write-Time Privacy & Secrets Gate** (`core/privacy.py`):
  - Strips `<private>...</private>` blocks to `[REDACTED_PRIVATE]` and raises `SecretLeakError` to block API keys, tokens, and private keys before writing knowledge docs, Skills, or rules.
- **Experience Card Freshness Governance**:
  - Tracks `freshness_tier` (`volatile`=30d, `evolving`=90d, `evergreen`=180d), `last_verified_at`, `valid_until`, and `freshness_state`.
  - Demotes expired cards on recall with `⚠️ [已过复核期，执行前请先核实]` warnings and auto-renews freshness when `record_outcome` passes a `real` verification check with `used_knowledge_paths`.
- **Derived Core Index Card & MCP Resource** (`core/core_card.py`):
  - Deterministically rebuilds `data/FLYWHEEL-CORE.md` (strictly capped at `<= 200` lines and `<= 24 KB`, isolated inside `data/`) and exposes it as MCP Resource `flywheel://core-card`.
- **Friday Tiered Dream Consolidation & Anti-Skill-Theater** (`core/dream.py`):
  - Auto-demotes zero-hit `unverified_active` rules older than 14 days to `dormant` (reversible with 1-click wake-up, audit-logged).
  - Sweeps expired knowledge cards, flags overlapping rules (trigger Jaccard >= 0.7), tracks `used_skills` (`invoke_count` / `verified_pass_count` / `fail_count`), flags unused (>= 21d) or failing (>= 3 fails) Skills, and saves weekly `flywheel_metrics_snapshots` with small-sample honesty indicators (`sample_sufficient`).
- **P03 Quality & Resilience**:
  - Adds 1-retry lightweight JSON format repair in `OpenAICompatibleRunner` without re-running P02, plus clean Chinese title normalization removing raw `client · <uuid>` titles.
- **SQLite Schema v3 Auto-Migration**:
  - Automatically backs up existing v2 database to `flywheel.db.bak-v2` before applying v3 schema tables, columns, and indexes (`schema_migrations` version `3`).
- **Portal API & 4+1 UI Seamless Upgrade**:
  - New endpoints: `/api/site/observations`, `/api/site/dissents`, `/api/site/growth-metrics`, `/api/site/core-card`.
  - Preserves 4+1 sidebar navigation; upgrades `规则与规矩` with compact 3-card growth bar, `已休眠` filter & wake-up button, inline rule dissent box, and `错题观察池` tab; adds Friday Dream summary in `每日沉淀` batch drawer.

## [0.2.0] - 2026-11-23

### Added
- **Simplified 4+1 Plain-Chinese Portal** (`每日成果`, `每日沉淀`, `规则与规矩`, `接入与设置` + `关于` About page with animated canvas & `/llms.txt` parity).
- **GitLab-CI-Style Batch Pipeline View** in `WorkflowsPage` with inline batch approval drawer, `MarkdownView` renderer, and `audit_events` timeline.
- **SQLite Schema v2 Normalized Tables** (`schema_migrations`, `system_settings`, `audit_events`, `workflow_nodes`, `run_sessions`, `planned_writes`, `decision_cards`, `card_deliveries`, `skill_assets`) with universal `created_at` / `updated_at` audit columns, optimistic locking `version`, and automatic `flywheel.db.bak-v1` backup.
- **Skill Writer & Smart Approval**: Controlled `SKILL.md` creation/update with frontmatter validation, `.bak` backup, LF SHA-256 read-back verification, `smart | always | auto` approval modes, Friday `auto -> both` weekly review phase, and Feishu CLI (`lark-cli`) notification delivery.

### Fixed
- Defensive `normalize_rule_payload()` in `runners.py` and `orchestrator.py` preventing P04 Pydantic validation failures when an LLM omits `title` or uses alias keys in `rule_payload`.
- Stranded `in_progress` sessions on run failure now cleanly return to `pending`.

## [0.1.0] - 2026-11-22

### Added
- Initial release of AI Flywheel: decoupled `profile/policies/`, P00–P07 workflow state machine, two-tier rule activation lifecycle, outcome hard gates, SQLite FTS5 Chinese-capable knowledge search, thin fail-open hooks (`hook-forward`), MCP Streamable HTTP & stdio server, and React web portal.
