from __future__ import annotations

import json
import re
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import httpx

from ai_flywheel.adapters.skills import SkillWriterAdapter
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.privacy import compute_valid_until, shanghai_today_ymd
from ai_flywheel.models import CreateRuleRequest, PlannedWrite, RetroItem, RuleRecord, SessionRecord
from ai_flywheel.storage.fs_store import ImmutableFileStore, canonical_hash

_RAW_ID_TITLE_RE = re.compile(
    r"^(?:codex|claude|claude-code|antigravity|cursor|session|chat)\s*[·:：\-]\s*[0-9a-f\-]{6,}$",
    re.IGNORECASE,
)


def _clean_title(raw_title: str, fallback_text: str = "", fallback_label: str = "协作经验沉淀") -> str:
    """Replace raw client+UUID titles (e.g. 'codex · 01a2072f-9a5d') with a readable concise Chinese title."""
    title = (raw_title or "").strip()
    if title and not _RAW_ID_TITLE_RE.match(title):
        return title[:48]
    candidate = re.sub(r"[\r\n#>*`]+", " ", (fallback_text or "")).strip()
    candidate = re.sub(r"\s+", " ", candidate)
    if candidate:
        # Take first meaningful sentence/clause
        clause = re.split(r"[。！？!?；;\n]", candidate)[0].strip()
        if len(clause) >= 4:
            return clause[:36]
    return fallback_label


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", text).strip("-").lower()
    return cleaned[:42] or "session-lesson"


def _ascii_slug(text: str, fallback: str = "custom-workflow") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(cleaned) < 2:
        return fallback
    return cleaned[:48]


def _coerce_str_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str) and val.strip():
        return [s.strip() for s in re.split(r"[\n;；]+", val) if s.strip()]
    return []


def normalize_rule_payload(item: PlannedWrite) -> dict[str, Any]:
    """Normalize LLM or heuristic rule_payload so CreateRuleRequest validation never crashes in P04."""
    raw = dict(item.rule_payload) if isinstance(item.rule_payload, dict) else {}

    title = str(raw.get("title") or raw.get("name") or item.title or "").strip()
    if not title:
        title = "协作避坑规则"

    domain = str(raw.get("domain") or raw.get("category") or "engineering").strip() or "engineering"

    projects = _coerce_str_list(raw.get("projects"))
    if not projects:
        projects = ["generic"]

    triggers = _coerce_str_list(raw.get("triggers") or raw.get("keywords"))
    if not triggers:
        # Derive simple triggers from title tokens
        tokens = re.findall(r"[a-zA-Z0-9_-]{3,}|[\u4e00-\u9fff]{2,6}", title)
        triggers = list(dict.fromkeys(tokens))[:4] or ["review"]

    checklist = _coerce_str_list(raw.get("checklist") or raw.get("steps") or raw.get("checks"))
    for alt_key in ("rule", "requirement", "action", "must_do", "description", "scope"):
        extra = raw.get(alt_key)
        if isinstance(extra, str) and extra.strip():
            prefix = "适用范围：" if alt_key == "scope" else ""
            entry = f"{prefix}{extra.strip()}"
            if entry not in checklist:
                checklist.append(entry)
    if not checklist and item.summary:
        checklist.append(item.summary.strip())

    forbidden = _coerce_str_list(
        raw.get("forbidden_routes") or raw.get("forbidden") or raw.get("avoid") or raw.get("donts")
    )
    source_pointers = _coerce_str_list(raw.get("source_pointers"))
    if item.relative_path and item.relative_path not in source_pointers:
        source_pointers.append(item.relative_path)

    origin_session = raw.get("origin_session_key")
    if not origin_session and item.source_session_keys:
        origin_session = item.source_session_keys[0]

    normalized = {
        "rule_id": raw.get("rule_id") or None,
        "title": title,
        "domain": domain,
        "projects": projects,
        "triggers": triggers[:8],
        "checklist": checklist[:10] or ["执行前复核相关先例并做真实验证"],
        "forbidden_routes": forbidden[:8],
        "applicable_skills": _coerce_str_list(raw.get("applicable_skills")),
        "source_pointers": source_pointers,
        "origin_session_key": str(origin_session) if origin_session else None,
    }
    # Validate early via Pydantic model so downstream P04 is guaranteed clean
    validated = CreateRuleRequest.model_validate(normalized)
    out = validated.model_dump(mode="json")
    # Preserve optional observation linkage metadata for P04 promotion bookkeeping
    if raw.get("root_cause_key"):
        out["root_cause_key"] = str(raw["root_cause_key"])
    if raw.get("obs_id"):
        out["obs_id"] = str(raw["obs_id"])
    return out


def _normalize_planned_write(raw_item: dict[str, Any], default_doc_prefix: str) -> PlannedWrite | None:
    try:
        pw = PlannedWrite.model_validate(raw_item)
    except Exception:
        return None

    if pw.target_kind == "flywheel_rule":
        pw.action = "register_rule"
        pw.rule_payload = normalize_rule_payload(pw)
        if not pw.content_preview:
            chk = pw.rule_payload.get("checklist", [])
            pw.content_preview = "\n".join(f"- {c}" for c in chk)
    elif pw.target_kind == "skill_improvement":
        slug = SkillWriterAdapter.extract_skill_name(pw.relative_path, fallback_title=pw.title)
        if not slug or len(slug) < 2:
            slug = _ascii_slug(pw.title, fallback="workflow-helper")
        pw.relative_path = f"{slug}/SKILL.md"
        if pw.action not in {"create", "update"}:
            pw.action = "create"
        # Ensure content_preview has valid SKILL.md YAML frontmatter
        if not pw.content_preview.strip().startswith("---"):
            body = pw.content_preview.strip() or f"# {pw.title}\n\n## 步骤\n- {pw.summary}\n"
            desc = (pw.summary or pw.title).replace("\n", " ").strip()[:180]
            pw.content_preview = f"---\nname: {slug}\ndescription: {desc}\n---\n\n{body}\n"
        pw.requires_approval = True
        if not pw.approval_reason:
            pw.approval_reason = (
                "本批建议新建可复用 Skill，需确认后写入技能目录"
                if pw.action == "create"
                else "本批建议优化已有 Skill，需确认后更新技能文件"
            )
    elif pw.target_kind == "knowledge_doc":
        pw.title = _clean_title(pw.title, pw.summary or pw.content_preview)
        if not pw.relative_path:
            prefix = default_doc_prefix.replace("\\", "/").strip("/")
            today = shanghai_today_ymd()
            pw.relative_path = f"{prefix}/{today}-{_slugify(pw.title)}.md" if prefix else f"lessons/{today}-{_slugify(pw.title)}.md"
        if pw.action in {"delete", "overwrite_core"}:
            pw.requires_approval = True
            pw.approval_reason = pw.approval_reason or "涉及高风险文档修改，需人工确认"
    return pw


def _extract_json_block(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


class BaseWorkflowRunner(ABC):
    """Abstract AI execution runner (supports OpenAI-compatible API, local CLI subprocess, and dry-run)."""

    @abstractmethod
    def review_sessions(
        self,
        sessions: list[tuple[SessionRecord, str]],
        open_retros: list[RetroItem],
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
    ) -> dict[str, Any]:
        """P02: Extract facts, incidents, candidate rules, and repeated workflow candidates for Skill routing."""

    @abstractmethod
    def plan_writes(
        self,
        review_findings: dict[str, Any],
        existing_rules: list[RuleRecord],
        max_write_set: int,
        default_doc_prefix: str = "lessons/",
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
    ) -> list[PlannedWrite]:
        """P03: Deduplicate against existing rules & skills and generate controlled write plan."""


class DryRunHeuristicRunner(BaseWorkflowRunner):
    """Zero-cost deterministic local runner for zero-config operation and testing."""

    _WORKFLOW_KEYWORDS = ("重复流程", "每次都要", "固定步骤", "封装skill", "封装 skill", "skill", "自动化流程", "checklist")
    _RED_KEYWORDS = ("静默失败", "线上故障", "用户发现", "沉默错误", "漏检", "silent", "production outage", "红灯")
    _DISSENT_KEYWORDS = ("不适用", "例外", "反例", "规则冲突", "dissent", "exception")
    _TEMP_CHAT_KEYWORDS = ("翻译一下", "帮我翻译", "今天天气", "闲聊", "随便聊聊", "查个单词", "hello world", "打个招呼")

    def review_sessions(
        self,
        sessions: list[tuple[SessionRecord, str]],
        open_retros: list[RetroItem],
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
    ) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        repeated_workflows: list[dict[str, Any]] = []
        mistakes: list[dict[str, Any]] = []
        dissents: list[dict[str, Any]] = []
        retro_by_session: dict[str, list[RetroItem]] = {}
        for r in open_retros:
            retro_by_session.setdefault(r.session_key, []).append(r)

        skill_names = {str(sk.get("name", "")).lower(): sk for sk in (existing_skills or [])}

        for sess, text in sessions:
            s_retros = retro_by_session.get(sess.session_key, [])
            summary = sess.summary or (text[:220] + ("..." if len(text) > 220 else "")) or "无正文摘要"
            title = _clean_title(sess.title, summary, fallback_label=f"{sess.project} 协作沉淀")
            lower_all = f"{title} {summary} {text[:2000]}".lower()

            suggest_skill = any(k in lower_all for k in self._WORKFLOW_KEYWORDS)
            matched_skill = next((name for name in skill_names if name and name in lower_all), None)
            if suggest_skill:
                repeated_workflows.append(
                    {
                        "session_key": sess.session_key,
                        "name_hint": matched_skill or _ascii_slug(title, fallback=f"{_ascii_slug(sess.project, 'ops')}-workflow"),
                        "title": title,
                        "summary": summary,
                        "existing_skill_match": matched_skill,
                        "action": "update" if matched_skill else "create",
                        "reason": "检测到可重复执行的多步流程特征，适合封装或补充到 Skill",
                    }
                )

            has_error_signal = bool(
                s_retros or any(k in lower_all for k in ["失败", "报错", "重试", "fix", "error", "规则", "故障"])
            )
            if has_error_signal:
                is_red = any(r.severity == "high" for r in s_retros) or any(
                    k in lower_all for k in self._RED_KEYWORDS
                )
                grade = "red_silent" if is_red else "yellow_caught"
                caught_by = "user" if is_red else "self"
                rc_seed = str(sess.metadata.get("root_cause_key") or "").strip() if isinstance(sess.metadata, dict) else ""
                rc_key = rc_seed or _ascii_slug(f"{sess.project}-{title}", fallback=f"issue-{_slugify(title)[:24]}")
                mistakes.append(
                    {
                        "root_cause_key": rc_key,
                        "severity_grade": grade,
                        "title": title,
                        "phenomenon": summary[:180],
                        "truth": "需核对前置条件并用真实命令验证",
                        "thinking_root_cause": "未先核查边界条件或沿用未经验证的假设",
                        "caught_by": caught_by,
                        "session_key": sess.session_key,
                    }
                )

            if any(k in lower_all for k in self._DISSENT_KEYWORDS):
                ref_rule = next((r.rule_id for r in s_retros if r.rule_id), None)
                if ref_rule:
                    dissents.append(
                        {
                            "target_type": "rule",
                            "target_id": ref_rule,
                            "counter_claim": summary[:200],
                            "condition_scope": f"project={sess.project}",
                            "evidence_pointer": sess.session_key,
                            "session_key": sess.session_key,
                        }
                    )

            is_temp_chat = (
                any(k in lower_all for k in self._TEMP_CHAT_KEYWORDS)
                and not s_retros
                and not suggest_skill
                and not has_error_signal
                and sess.project in {"", "generic", "general"}
            )

            findings.append(
                {
                    "session_key": sess.session_key,
                    "client": sess.client,
                    "project": sess.project,
                    "title": title,
                    "worthy": not is_temp_chat,
                    "category": "temp_chat" if is_temp_chat else ("workflow" if suggest_skill else "project"),
                    "skip_reason": "临时一次性问答/翻译，无需沉淀为长期经验" if is_temp_chat else "",
                    "facts": [summary],
                    "anomalies": [r.summary for r in s_retros],
                    "tags": sess.tags or [sess.project, sess.client],
                    "char_count": len(text),
                    "suggest_rule": has_error_signal and not is_temp_chat,
                    "skill_route": {
                        "hit": suggest_skill,
                        "action": ("update" if matched_skill else "create") if suggest_skill else "none",
                        "target_skill": matched_skill or "",
                        "reason": "命中重复流程关键词" if suggest_skill else "常规单次任务，暂无需封装 Skill",
                    },
                }
            )
        return {
            "runner": "dry_run",
            "phase": phase,
            "sessions_reviewed": len(findings),
            "findings": findings,
            "repeated_workflows": repeated_workflows,
            "mistakes": mistakes,
            "dissents": dissents,
        }

    def plan_writes(
        self,
        review_findings: dict[str, Any],
        existing_rules: list[RuleRecord],
        max_write_set: int,
        default_doc_prefix: str = "lessons/",
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
    ) -> list[PlannedWrite]:
        existing_titles = {r.title.lower() for r in existing_rules}
        existing_rc_keys = {r.root_cause_key for r in existing_rules if r.root_cause_key}
        used_paths: set[str] = set()
        today = shanghai_today_ymd()
        valid_until = compute_valid_until("evolving", today)
        prefix = default_doc_prefix.replace("\\", "/").strip("/")
        prefix = f"{prefix}/" if prefix else "lessons/"
        plans: list[PlannedWrite] = []
        use_two_track_gate = "eligible_promotions" in review_findings
        eligible_promotions = review_findings.get("eligible_promotions") or []

        for item in review_findings.get("findings", []):
            if len(plans) >= max_write_set:
                break
            if item.get("worthy") is False:
                continue
            title = _clean_title(item.get("title", ""), " ".join(item.get("facts", [])))
            slug = _slugify(title)
            rel_path = f"{prefix}{today}-{slug}.md"
            if rel_path in used_paths:
                short_id = _ascii_slug(item["session_key"].split(":")[-1], "s")[:8]
                rel_path = f"{prefix}{today}-{slug}-{short_id}.md"
            used_paths.add(rel_path)
            facts_md = "\n".join(f"- {f}" for f in item.get("facts", [])) or "- 待补充事实"
            anomalies_md = (
                "\n".join(f"- {a}" for a in item.get("anomalies", []))
                if item.get("anomalies")
                else "- 本会话无记录异常"
            )
            tags_str = ", ".join(item.get("tags", []))
            content = (
                f"---\n"
                f"title: {title}\n"
                f"source_session: {item['session_key']}\n"
                f"project: {item['project']}\n"
                f"tags: [{tags_str}]\n"
                f"freshness_tier: evolving\n"
                f"last_verified: {today}\n"
                f"valid_until: {valid_until}\n"
                f"---\n\n"
                f"# {title}\n\n"
                f"## 事实回顾\n{facts_md}\n\n"
                f"## 异常与根因线索\n{anomalies_md}\n\n"
                f"## 适用前提与失效条件\n"
                f"- 适用于当前项目环境与同类工作流；若接口版本、配置结构或环境前提发生变化，需重新核验。\n\n"
                f"## 分歧与反例\n"
                f"- 暂未记录反例；后续遇到例外场景可追加记录，勿直接覆盖原结论。\n\n"
                f"## 复用建议\n"
                f"- 下次同类任务先核对先例规则并做真实运行验证。\n"
            )
            plans.append(
                PlannedWrite(
                    target_kind="knowledge_doc",
                    relative_path=rel_path,
                    action="create",
                    title=title,
                    summary=f"沉淀来自 {item['session_key']} 的协作经验卡",
                    content_preview=content,
                    source_session_keys=[item["session_key"]],
                    approved=True,
                )
            )

            # Backward-compatible path when called directly outside orchestrator's two-track observation gate
            if (
                not use_two_track_gate
                and item.get("suggest_rule")
                and title.lower() not in existing_titles
                and len(plans) < max_write_set
            ):
                triggers = list(
                    dict.fromkeys([t for t in item.get("tags", []) if len(t) >= 2] + [item["project"]])
                )[:5]
                rule_pw = PlannedWrite(
                    target_kind="flywheel_rule",
                    relative_path=None,
                    action="register_rule",
                    title=f"先例防线：{title[:40]}",
                    summary="基于复盘线索新增试运行避坑规则（后续自然任务实测后自动晋升已生效）",
                    content_preview=f"触发词: {', '.join(triggers)}\n必查项: 复核同类根因并保留真实运行证据",
                    rule_payload={
                        "title": f"先例防线：{title[:40]}",
                        "domain": "engineering",
                        "projects": [item["project"], "generic"],
                        "triggers": triggers or ["debug"],
                        "checklist": [
                            f"回顾经验卡 {rel_path}，核对根因与前置条件",
                            "执行真实验证并提交结构化回执",
                        ],
                        "forbidden_routes": ["跳过验证或盲目重试超过 2 次"],
                        "source_pointers": [rel_path],
                        "origin_session_key": item["session_key"],
                    },
                    source_session_keys=[item["session_key"]],
                    approved=True,
                )
                rule_pw.rule_payload = normalize_rule_payload(rule_pw)
                plans.append(rule_pw)

        # Two-track mistake promotion: only red_silent (count>=1) or yellow_caught (count>=3) promote to rules
        if use_two_track_gate:
            for obs in eligible_promotions:
                if len(plans) >= max_write_set:
                    break
                rc_key = str(obs.get("root_cause_key") or "")
                obs_title = _clean_title(str(obs.get("title") or ""), str(obs.get("phenomenon") or ""))
                rule_title = f"避坑规则：{obs_title[:36]}"
                if rc_key in existing_rc_keys or rule_title.lower() in existing_titles:
                    continue
                is_red = obs.get("severity_grade") == "red_silent"
                reason_tag = (
                    "🔴 高危沉默错误直通试运行规则"
                    if is_red
                    else f"🟡 同根因累计出现 {obs.get('occurrence_count', 3)} 次，满足事不过三晋升条件"
                )
                tokens = re.findall(r"[a-zA-Z0-9_-]{3,}|[\u4e00-\u9fff]{2,6}", obs_title)
                triggers = list(dict.fromkeys(tokens))[:5] or ["debug"]
                sess_list = obs.get("session_keys") or []
                rule_pw = PlannedWrite(
                    target_kind="flywheel_rule",
                    relative_path=None,
                    action="register_rule",
                    title=rule_title,
                    summary=reason_tag,
                    content_preview=(
                        f"晋升依据: {reason_tag}\n"
                        f"思维根因: {obs.get('thinking_root_cause') or '待补充'}\n"
                        f"检查项: {obs.get('truth') or '执行前核对前置条件并真实验证'}"
                    ),
                    rule_payload={
                        "title": rule_title,
                        "domain": "engineering",
                        "projects": ["generic"],
                        "triggers": triggers,
                        "checklist": [
                            obs.get("truth") or "执行前核对前置条件与边界假设",
                            "保留真实运行证据并提交结构化回执",
                        ],
                        "forbidden_routes": [obs.get("thinking_root_cause") or "跳过前置核查直接盲改"],
                        "source_pointers": sess_list[:3],
                        "origin_session_key": sess_list[0] if sess_list else None,
                        "root_cause_key": rc_key,
                        "obs_id": obs.get("obs_id"),
                    },
                    source_session_keys=sess_list[:3],
                    approved=True,
                )
                rule_pw.rule_payload = normalize_rule_payload(rule_pw)
                plans.append(rule_pw)

        # Emit Skill creation/improvement proposals for detected repeated workflows
        for wf in review_findings.get("repeated_workflows", []):
            if len(plans) >= max_write_set:
                break
            skill_slug = _ascii_slug(wf.get("name_hint") or wf.get("title") or "reusable-workflow")
            action = "update" if wf.get("action") == "update" else "create"
            verb = "优化已有 Skill" if action == "update" else "封装新 Skill"
            desc = (wf.get("summary") or wf.get("title") or "可复用多步工作流").replace("\n", " ").strip()[:160]
            skill_md = (
                f"---\n"
                f"name: {skill_slug}\n"
                f"description: {desc}\n"
                f"---\n\n"
                f"# {wf.get('title') or skill_slug}\n\n"
                f"## 适用场景\n"
                f"- {desc}\n\n"
                f"## 执行步骤\n"
                f"1. 确认前置条件与目标范围。\n"
                f"2. 按已验证的标准步骤执行，避免重复踩坑。\n"
                f"3. 完成后执行真实运行校验并留存结果。\n"
            )
            plans.append(
                PlannedWrite(
                    target_kind="skill_improvement",
                    relative_path=f"{skill_slug}/SKILL.md",
                    action=action,
                    title=f"{verb}：{skill_slug}",
                    summary=wf.get("reason") or "从重复出现的操作流程中提炼出可复用 Skill",
                    content_preview=skill_md,
                    skill_route={
                        "hit": True,
                        "action": action,
                        "target_skill": skill_slug,
                        "reason": wf.get("reason") or "",
                    },
                    requires_approval=True,
                    approval_reason=f"涉及{verb}（{skill_slug}/SKILL.md），属于能力资产变更，需人工确认",
                    source_session_keys=[wf.get("session_key", "")],
                    approved=True,
                )
            )
        return plans[:max_write_set]


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
            "text": t[:3000],
        }
        for s, t in sessions[:6]
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


def _build_p03_prompt(
    review_findings: dict[str, Any],
    existing_rules: list[RuleRecord],
    existing_skills: list[dict[str, Any]] | None,
    max_write_set: int,
    default_doc_prefix: str,
    phase: str,
) -> str:
    existing_summary = [{"rule_id": r.rule_id, "title": r.title, "status": r.status.value} for r in existing_rules]
    skill_names = [sk.get("name", "") for sk in (existing_skills or [])[:60] if sk.get("name")]
    capped_items = min(max_write_set, 5)
    today = shanghai_today_ymd()
    return (
        "You are the P03 Route & Write-Plan Worker of AI Flywheel.\n"
        "Deduplicate against existing_rules and existing_skills, then return the top highest-value writes in Chinese.\n"
        "CRITICAL TITLE RULE: Every item's title MUST be clean, concise Chinese (6-24 chars). NEVER output raw IDs like 'codex · 01a2072f'.\n"
        "Allowed target_kind values:\n"
        f'1. "knowledge_doc": relative_path under "{default_doc_prefix}{today}-slug.md", action="create|update", '
        f'content_preview=concise structured markdown (220-450 chars) with frontmatter including '
        f'freshness_tier (volatile|evolving|evergreen), last_verified: {today}, valid_until, '
        'and sections "## 适用前提与失效条件" and "## 分歧与反例".\n'
        '2. "flywheel_rule": ONLY emit for items in eligible_promotions (either red_silent or occurrence_count>=3). '
        'relative_path=null, action="register_rule", MUST include rule_payload with EXACT keys: '
        '{"title":"简短中文标题","domain":"engineering","projects":["generic"],"triggers":["kw1","kw2"],'
        '"checklist":["检查项1","检查项2"],"forbidden_routes":["禁忌路线"],"root_cause_key":"optional-key"}.\n'
        '3. "skill_improvement" (ONLY for high-value repeated multi-step workflows from repeated_workflows): '
        'relative_path="<kebab-case-slug>/SKILL.md", action="create|update", requires_approval=true, '
        'approval_reason="简述为何值得封装或优化该 Skill", '
        'skill_route={"hit":true,"action":"create|update","target_skill":"<slug>","reason":"..."}, '
        'content_preview MUST be concise valid SKILL.md (250-500 chars) starting with YAML frontmatter "---\\nname: <slug>\\ndescription: ...\\n---\\n\\n# ...".\n'
        f"IMPORTANT: Output at most {capped_items} total items. Keep content_preview concise so JSON completes cleanly. Return STRICT JSON ONLY:\n"
        '{"planned_writes":[...]}\n\n'
        + json.dumps(
            {
                "phase": phase,
                "findings": [f for f in review_findings.get("findings", []) if f.get("worthy", True) is not False],
                "eligible_promotions": review_findings.get("eligible_promotions", []),
                "repeated_workflows": review_findings.get("repeated_workflows", []),
                "existing_rules": existing_summary,
                "existing_skill_names": skill_names,
            },
            ensure_ascii=False,
        )
    )


class CliSubprocessRunner(BaseWorkflowRunner):
    """Configurable local CLI subprocess runner (e.g. codex / claude CLI) with receipt caching."""

    def __init__(self, cfg: FlywheelConfig, fs_store: ImmutableFileStore) -> None:
        self.cfg = cfg
        self.fs_store = fs_store
        self.fallback = DryRunHeuristicRunner()

    def _invoke_cli(self, stage: str, prompt: str) -> dict[str, Any] | None:
        cli_cfg = self.cfg.runner.cli
        if not self.cfg.safety_valves.cli_execution_enabled or not cli_cfg.command:
            return None

        prompt_hash = canonical_hash({"stage": stage, "cmd": cli_cfg.command, "prompt": prompt})
        cache_dir = self.fs_store.root / "cli_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{stage}-{prompt_hash[:16]}.json"
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

        cmd = [cli_cfg.command]
        used_placeholder = False
        for arg in cli_cfg.args:
            if "{prompt}" in arg:
                cmd.append(arg.replace("{prompt}", prompt))
                used_placeholder = True
            else:
                cmd.append(arg)

        proc = subprocess.run(
            cmd,
            input=None if used_placeholder else prompt,
            text=True,
            capture_output=True,
            timeout=cli_cfg.timeout_seconds,
            cwd=cli_cfg.working_dir or str(self.cfg.base_dir),
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Local CLI '{cli_cfg.command}' exited with code {proc.returncode}: {(proc.stderr or proc.stdout)[:400]}"
            )
        parsed = _extract_json_block(proc.stdout)
        if parsed is not None:
            cache_file.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
        return parsed

    def review_sessions(
        self,
        sessions: list[tuple[SessionRecord, str]],
        open_retros: list[RetroItem],
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
    ) -> dict[str, Any]:
        prompt = _build_p02_prompt(sessions, open_retros, existing_skills, phase)
        parsed = self._invoke_cli("p02_review", prompt)
        if parsed and isinstance(parsed.get("findings"), list):
            parsed["runner"] = f"cli:{self.cfg.runner.cli.command}"
            parsed["phase"] = phase
            parsed["sessions_reviewed"] = len(parsed["findings"])
            parsed.setdefault("repeated_workflows", [])
            return parsed
        res = self.fallback.review_sessions(sessions, open_retros, existing_skills=existing_skills, phase=phase)
        res["runner"] = f"cli_fallback_dry_run:{self.cfg.runner.cli.command or 'unconfigured'}"
        return res

    def plan_writes(
        self,
        review_findings: dict[str, Any],
        existing_rules: list[RuleRecord],
        max_write_set: int,
        default_doc_prefix: str = "lessons/",
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
    ) -> list[PlannedWrite]:
        prompt = _build_p03_prompt(
            review_findings, existing_rules, existing_skills, max_write_set, default_doc_prefix, phase
        )
        parsed = self._invoke_cli("p03_plan", prompt)
        if parsed and isinstance(parsed.get("planned_writes"), list):
            out: list[PlannedWrite] = []
            for raw_item in parsed["planned_writes"][:max_write_set]:
                if isinstance(raw_item, dict):
                    pw = _normalize_planned_write(raw_item, default_doc_prefix)
                    if pw is not None:
                        out.append(pw)
            if out:
                return out
        return self.fallback.plan_writes(
            review_findings,
            existing_rules,
            max_write_set,
            default_doc_prefix=default_doc_prefix,
            existing_skills=existing_skills,
            phase=phase,
        )


class OpenAICompatibleRunner(BaseWorkflowRunner):
    """OpenAI-compatible HTTP API runner (works with local routing gateways, OpenAI, DeepSeek, Ollama)."""

    def __init__(self, cfg: FlywheelConfig, fs_store: ImmutableFileStore) -> None:
        self.cfg = cfg
        self.fs_store = fs_store
        self.fallback = DryRunHeuristicRunner()

    def _call_chat_completion(self, prompt: str, system_prompt: str) -> str:
        api_cfg = self.cfg.runner.api
        endpoint = api_cfg.base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"

        headers = {"Content-Type": "application/json", **api_cfg.extra_headers}
        if api_cfg.api_key:
            headers.setdefault("Authorization", f"Bearer {api_cfg.api_key}")

        resp = httpx.post(
            endpoint,
            headers=headers,
            json={
                "model": api_cfg.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
            },
            timeout=api_cfg.timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)

    def _invoke_api(
        self,
        stage: str,
        prompt: str,
        *,
        repair_schema_hint: str | None = None,
    ) -> dict[str, Any] | None:
        api_cfg = self.cfg.runner.api
        if not self.cfg.safety_valves.cli_execution_enabled or not api_cfg.base_url or not api_cfg.model:
            return None

        prompt_hash = canonical_hash(
            {"stage": stage, "base_url": api_cfg.base_url, "model": api_cfg.model, "prompt": prompt}
        )
        cache_dir = self.fs_store.root / "api_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{stage}-{prompt_hash[:16]}.json"
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

        sys_prompt = "You are an AI Flywheel workflow worker. Return strictly valid JSON with no markdown prose outside JSON."
        raw_text = self._call_chat_completion(prompt, sys_prompt)
        parsed = _extract_json_block(raw_text)

        # Lightweight 1-retry JSON format repair (without re-running earlier stages)
        if parsed is None and repair_schema_hint and raw_text.strip():
            repair_prompt = (
                "The previous output was malformed JSON. Fix ONLY the JSON syntax/structure and return STRICT JSON ONLY.\n"
                f"Expected top-level schema: {repair_schema_hint}\n\n"
                f"Malformed output:\n{raw_text[:8000]}"
            )
            try:
                repaired_text = self._call_chat_completion(
                    repair_prompt,
                    "You are a strict JSON repair utility. Return ONLY one valid JSON object.",
                )
                parsed = _extract_json_block(repaired_text)
            except Exception:
                parsed = None

        if parsed is not None:
            cache_file.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
        return parsed

    def review_sessions(
        self,
        sessions: list[tuple[SessionRecord, str]],
        open_retros: list[RetroItem],
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
    ) -> dict[str, Any]:
        prompt = _build_p02_prompt(sessions, open_retros, existing_skills, phase)
        parsed = self._invoke_api(
            "p02_review",
            prompt,
            repair_schema_hint='{"findings":[...],"mistakes":[...],"dissents":[...],"repeated_workflows":[...]}',
        )
        if parsed and isinstance(parsed.get("findings"), list):
            parsed["runner"] = f"api:{self.cfg.runner.api.model}"
            parsed["phase"] = phase
            parsed["sessions_reviewed"] = len(parsed["findings"])
            parsed.setdefault("repeated_workflows", [])
            parsed.setdefault("mistakes", [])
            parsed.setdefault("dissents", [])
            return parsed
        res = self.fallback.review_sessions(sessions, open_retros, existing_skills=existing_skills, phase=phase)
        res["runner"] = f"api_fallback_dry_run:{self.cfg.runner.api.model or 'unconfigured'}"
        return res

    def plan_writes(
        self,
        review_findings: dict[str, Any],
        existing_rules: list[RuleRecord],
        max_write_set: int,
        default_doc_prefix: str = "lessons/",
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
    ) -> list[PlannedWrite]:
        prompt = _build_p03_prompt(
            review_findings, existing_rules, existing_skills, max_write_set, default_doc_prefix, phase
        )
        parsed = self._invoke_api(
            "p03_plan",
            prompt,
            repair_schema_hint='{"planned_writes":[{"target_kind":"knowledge_doc|flywheel_rule|skill_improvement","action":"create|update|register_rule","title":"简洁中文标题","summary":"...","content_preview":"..."}]}',
        )
        if parsed and isinstance(parsed.get("planned_writes"), list):
            out: list[PlannedWrite] = []
            for raw_item in parsed["planned_writes"][:max_write_set]:
                if isinstance(raw_item, dict):
                    pw = _normalize_planned_write(raw_item, default_doc_prefix)
                    if pw is not None:
                        out.append(pw)
            if out:
                return out
        return self.fallback.plan_writes(
            review_findings,
            existing_rules,
            max_write_set,
            default_doc_prefix=default_doc_prefix,
            existing_skills=existing_skills,
            phase=phase,
        )


def build_runner(cfg: FlywheelConfig, fs_store: ImmutableFileStore) -> BaseWorkflowRunner:
    if cfg.runner.mode == "api" and cfg.runner.api.base_url and cfg.runner.api.model and cfg.safety_valves.cli_execution_enabled:
        return OpenAICompatibleRunner(cfg, fs_store)
    if cfg.runner.mode == "cli" and cfg.runner.cli.command and cfg.safety_valves.cli_execution_enabled:
        return CliSubprocessRunner(cfg, fs_store)
    return DryRunHeuristicRunner()
