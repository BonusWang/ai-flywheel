"""The prompts the runners send.

Two prompts, one per node: P02 reviews a batch of sessions, P03 turns a review into a
write plan. Both are deterministic functions of their input — the same batch always
produces the same prompt, so a cached reply is always the reply to that batch.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_flywheel.adapters.runners.contracts import P02_MAX_SESSIONS
from ai_flywheel.adapters.skills import inspect_skill_at
from ai_flywheel.adapters.wiki.router import RoutingCandidates
from ai_flywheel.core.time_utils import shanghai_today_ymd
from ai_flywheel.models import RetroItem, RuleRecord, SessionRecord

# How much of one session the P02 prompt carries. A long session is excerpted head + tail:
# the conclusions, the fixes and the verifications live at its end, and a plain prefix
# would drop exactly the part the reviewer needs.
P02_SESSION_TEXT_LIMIT = 3000
P02_SESSION_HEAD_CHARS = 1450
P02_SESSION_TAIL_CHARS = 1450
P02_MIDDLE_OMITTED = "\n...[middle omitted]...\n"


def excerpt_session_text(text: str, limit: int = P02_SESSION_TEXT_LIMIT) -> str:
    """Head + tail excerpt of a session text, kept within ``limit`` characters.

    Short texts are passed through unchanged; longer ones keep their opening and their
    closing, with the omitted middle marked so the reviewer knows it is not the whole text.
    """
    body = text or ""
    if len(body) <= limit:
        return body
    head_chars = min(P02_SESSION_HEAD_CHARS, limit // 2)
    tail_chars = min(P02_SESSION_TAIL_CHARS, limit - head_chars)
    return f"{body[:head_chars]}{P02_MIDDLE_OMITTED}{body[-tail_chars:]}"


def _build_p02_prompt(
    sessions: list[tuple[SessionRecord, str]],
    open_retros: list[RetroItem],
    existing_skills: list[dict[str, Any]] | None,
    phase: str,
) -> str:
    payload = [
        {
            "session_key": s.session_key,
            "client": s.client,
            "project": s.project,
            "title": s.title,
            "text": excerpt_session_text(t),
        }
        for s, t in sessions[:P02_MAX_SESSIONS]
    ]
    skills_brief = [
        {"name": sk.get("name", ""), "description": str(sk.get("description", ""))[:120]}
        for sk in (existing_skills or [])[:60]
    ]
    weekly_note = (
        "This is a FRIDAY WEEKLY REVIEW (phase=both): pay extra attention to cross-session recurring workflows, "
        "overlapping skills, and recurring issues that should be encapsulated into a reusable Skill or rule.\n"
        if phase in {"weekly", "both"}
        else ""
    )
    return (
        "You are the P02 Review Worker of AI Flywheel.\n"
        f"{weekly_note}"
        "CRITICAL WORTHINESS GATE (Anti-Noise): Only project engineering sessions, reusable knowledge/research sessions, "
        "repeated workflows, or real mistake/dissent cases should be distilled! "
        "If a session is a temporary one-off Q&A, translation, weather/command lookup, chitchat, or exploratory test "
        "with NO reusable takeaway, you MUST set worthy=false, category='temp_chat', and skip_reason (do NOT fabricate lessons).\n"
        "Analyze the sessions for:\n"
        "(1) worthiness + factual lessons with CLEAN concise Chinese titles (NEVER use raw IDs like 'codex · 01a2072f'),\n"
        "(2) mistakes/pitfalls for the Mistake Observation Pool graded as 'red_silent' (silent failure or user-caught outage), "
        "'yellow_caught' (self/tool caught), or 'green_minor', with root_cause_key (kebab-case) and thinking_root_cause,\n"
        "(3) dissents/exceptions where a session shows an existing rule or experience card does NOT apply (preserve counter-evidence),\n"
        "(4) repeated multi-step workflows worth encapsulating into a new Skill or updating an existing Skill "
        "(compare against existing_skills to avoid duplicates).\n"
        "Return STRICT JSON ONLY:\n"
        '{"findings":[{"session_key":"...","client":"...","project":"...","title":"简洁中文标题",'
        '"worthy":true,"category":"project|knowledge|workflow|temp_chat","skip_reason":"",'
        '"facts":["..."],"anomalies":["..."],"tags":["..."],"suggest_rule":true,'
        '"skill_route":{"hit":false,"action":"none|create|update","target_skill":"slug-or-empty","reason":"..."}}],'
        '"mistakes":[{"root_cause_key":"kebab-case-root-cause","severity_grade":"red_silent|yellow_caught|green_minor",'
        '"title":"简短错题标题","phenomenon":"发生了什么","truth":"正确做法","thinking_root_cause":"思维盲点",'
        '"caught_by":"user|tool|self","session_key":"..."}],'
        '"dissents":[{"target_type":"rule|knowledge_doc|skill","target_id":"rule-id-or-path",'
        '"counter_claim":"反例或例外说明","condition_scope":"适用例外条件","evidence_pointer":"...","session_key":"..."}],'
        '"repeated_workflows":[{"session_key":"...","name_hint":"kebab-case-skill-slug","title":"简洁中文标题",'
        '"summary":"...","existing_skill_match":"existing-slug-or-null","action":"create|update","reason":"..."}]}\n\n'
        + json.dumps(
            {
                "phase": phase,
                "sessions": payload,
                "open_retros": [r.summary for r in open_retros[:15]],
                "existing_skills": skills_brief,
            },
            ensure_ascii=False,
        )
    )


def _p03_routing_text(routing_candidates: RoutingCandidates | None) -> tuple[str, str]:
    """Routing instructions for the P03 prompt: (paragraph, extra requirement inside the knowledge_doc item).

    No candidates (not a contract-governed Wiki) leaves the prompt untouched.
    """
    if routing_candidates is None:
        return "", ""
    if not routing_candidates.routable:
        return (
            "WIKI ROUTING: the knowledge base is a contract-governed Wiki that currently offers no routable page "
            'type or project (see "routing_candidates"). Do not output any "knowledge_doc" item.\n',
            "",
        )
    return (
        "WIKI ROUTING: the knowledge base is a contract-governed Wiki. "
        '"routing_candidates" in the input lists the ONLY page type and projects a "knowledge_doc" may be '
        "written as or into. "
        'EVERY "knowledge_doc" item MUST also carry "wiki_route": '
        '{"page_type": <one value copied from routing_candidates.page_types>, '
        '"project": <the "project" or "project_id" of ONE entry of routing_candidates.projects, copied exactly, or null>, '
        '"reason": "<= 40 Chinese characters saying why this note is about that project\'s own work>"}. '
        "Judge by the note's main subject, not by which project names appear in the text: declare a project only "
        "when the note is about that project's own work (its code, configuration, delivery or project-specific "
        "practice). When the note merely mentions a project in passing, compares several projects, audits or "
        'maintains the knowledge base itself, or states a lesson that spans projects, set both "page_type" and '
        '"project" to null; such a note is held back, not written. Never invent a page type or project outside '
        "routing_candidates, and never assign a project just to get a note written.\n",
        ' MUST also carry "wiki_route" (see WIKI ROUTING above)',
    )


def _existing_skill_sections(
    review_findings: dict[str, Any], skills_root: Path | None
) -> dict[str, list[str]]:
    """The level-2 headings of the Skills this batch wants to update.

    Only the Skills the review named are inspected, so the prompt grows by a few hundred
    characters at most and never by the size of the Skill files themselves.
    """
    if skills_root is None:
        return {}
    targets: list[str] = []
    for workflow in review_findings.get("repeated_workflows", []) or []:
        if not isinstance(workflow, dict):
            continue
        matched = str(workflow.get("existing_skill_match") or "").strip()
        if matched and matched not in targets:
            targets.append(matched)
    sections: dict[str, list[str]] = {}
    for name in targets[:10]:
        info = inspect_skill_at(skills_root, name)
        if info.get("exists"):
            sections[name] = list(info.get("headings") or [])
    return sections


def _build_p03_prompt(
    review_findings: dict[str, Any],
    existing_rules: list[RuleRecord],
    existing_skills: list[dict[str, Any]] | None,
    max_write_set: int,
    default_doc_prefix: str,
    phase: str,
    *,
    routing_candidates: RoutingCandidates | None = None,
    existing_skill_sections: dict[str, list[str]] | None = None,
) -> str:
    existing_summary = [{"rule_id": r.rule_id, "title": r.title, "status": r.status.value} for r in existing_rules]
    skill_names = [sk.get("name", "") for sk in (existing_skills or [])[:60] if sk.get("name")]
    # The prompt's cap is the batch's own cap: a configured max_writes_per_run must not be
    # silently narrowed to a hard-coded number, or the high-write threshold never fires.
    capped_items = max(1, max_write_set)
    today = shanghai_today_ymd()
    routing_paragraph, knowledge_doc_routing = _p03_routing_text(routing_candidates)
    routable = routing_candidates is not None and routing_candidates.routable
    skill_sections = {str(name): list(headings or []) for name, headings in (existing_skill_sections or {}).items()}
    payload: dict[str, Any] = {
        "phase": phase,
        "findings": [f for f in review_findings.get("findings", []) if f.get("worthy", True) is not False],
        "eligible_promotions": review_findings.get("eligible_promotions", []),
        "repeated_workflows": review_findings.get("repeated_workflows", []),
        "existing_rules": existing_summary,
        "existing_skill_names": skill_names,
        "existing_skill_sections": skill_sections,
    }
    if routing_candidates is not None:
        payload["routing_candidates"] = routing_candidates.to_dict(description_limit=120)
    skill_sections_note = (
        '"existing_skill_sections" in the input lists the level-2 headings each Skill to update already has. '
        "Write only what those Skills do not cover yet, and never repeat a heading that is already listed there.\n"
        if skill_sections
        else ""
    )
    return (
        "You are the P03 Route & Write-Plan Worker of AI Flywheel.\n"
        "Deduplicate against existing_rules and existing_skills, then return the top highest-value writes in Chinese.\n"
        "CRITICAL TITLE RULE: Every item's title MUST be clean, concise Chinese (6-24 chars). NEVER output raw IDs like 'codex · 01a2072f'.\n"
        "OUTPUT CONTRACT: EVERY item MUST contain ALL of these keys, whatever its target_kind: "
        '"target_kind", "action", "title", "summary" (one Chinese sentence, <= 60 chars, stating what this write captures), '
        '"relative_path" (path or null), "source_session_keys" (array of session_key values copied verbatim from '
        "findings / repeated_workflows / eligible_promotions; empty only if the write comes from none of them), "
        "plus the kind-specific keys listed below. "
        'If nothing is worth writing, return {"planned_writes": []}.\n'
        f"{routing_paragraph}"
        f"{skill_sections_note}"
        "Allowed target_kind values:\n"
        f'1. "knowledge_doc": relative_path under "{default_doc_prefix}{today}-slug.md", action="create|update", '
        f'content_preview=concise structured markdown (220-450 chars) with frontmatter including '
        f'freshness_tier (volatile|evolving|evergreen), last_verified: {today}, valid_until, '
        f'and sections "## 适用前提与失效条件" and "## 分歧与反例"{knowledge_doc_routing}.\n'
        '2. "flywheel_rule": ONLY emit for items in eligible_promotions (either red_silent or occurrence_count>=3). '
        'relative_path=null, action="register_rule", MUST include rule_payload with EXACT keys: '
        '{"title":"简短中文标题","domain":"engineering","projects":["generic"],"triggers":["kw1","kw2"],'
        '"checklist":["检查项1","检查项2"],"forbidden_routes":["禁忌路线"],"root_cause_key":"optional-key"}.\n'
        '3. "skill_improvement" (ONLY for high-value repeated multi-step workflows from repeated_workflows): '
        'relative_path="<skill-dir>/SKILL.md", action="create|update". For action="update" the directory MUST be the name of '
        "an existing skill from existing_skill_names (for example repeated_workflows[].existing_skill_match), never a new "
        'directory; for action="create" it MUST be a name that does not exist yet. requires_approval=true, '
        'approval_reason="简述为何值得封装或优化该 Skill", '
        'skill_route={"hit":true,"action":"create|update","target_skill":"<slug>","reason":"..."}. '
        'content_preview depends on the action: for action="create" it MUST be a complete valid SKILL.md starting with '
        'YAML frontmatter "---\\nname: <slug>\\ndescription: ...\\n---\\n\\n# ..." (250-500 chars); for action="update" it MUST be '
        'ONLY the incremental Markdown section to append at the END of the existing SKILL.md, starting with a "## " heading '
        "(200-500 chars), with NO frontmatter, NO \"# \" title and NO re-writing of the existing sections.\n"
        f"IMPORTANT: Output at most {capped_items} total items. Keep content_preview concise so JSON completes cleanly. "
        "Reserve at least one slot for a worthy knowledge_doc item: a rule or a Skill must not crowd out the "
        "experience card of a session that deserves one. "
        "Return STRICT JSON ONLY:\n"
        '{"planned_writes":[{"target_kind":"...","action":"...","title":"...","summary":"...","relative_path":"... or null",'
        '"source_session_keys":["..."],"content_preview":"...","rule_payload":{...},"skill_route":{...}'
        + (',"wiki_route":{"page_type":"...","project":"... or null","reason":"..."}' if routable else "")
        + "}]}\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
