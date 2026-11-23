# Changelog

All notable changes to **AI Flywheel** are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/).

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
