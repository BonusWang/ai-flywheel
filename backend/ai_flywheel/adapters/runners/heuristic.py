"""The zero-cost local planner.

`DryRunHeuristicRunner` reads the review with keyword rules instead of a model: it
proposes experience cards for every worthy session, promotes the mistakes the review
already graded as promotable, and reports repeated workflows instead of writing them —
a keyword-triggered planner cannot author a Skill.

It is the fallback configuration (`runner.mode: dry_run`), never a silent fallback: when
an api/cli runner cannot use its reply, the node fails instead of calling this planner.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ai_flywheel.adapters.runners.contracts import (
    PLAN_SOURCE_HEURISTIC,
    REVIEW_SOURCE_HEURISTIC,
    BaseWorkflowRunner,
    PlanDiagnostics,
    PlanOutcome,
    ReviewDiagnostics,
    _ascii_slug,
    _clean_title,
    _slugify,
    normalize_rule_payload,
)
from ai_flywheel.adapters.runners.normalizers import empty_domain_signals
from ai_flywheel.adapters.wiki.router import RoutingCandidates
from ai_flywheel.core.privacy import SecretLeakError, sanitize_and_check_secrets
from ai_flywheel.core.time_utils import compute_valid_until, shanghai_today_ymd
from ai_flywheel.models import PlannedWrite, RetroItem, RuleRecord, SessionRecord

logger = logging.getLogger(__name__)


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
                    # The local reviewer reads no table, configuration or architecture: no domain clue.
                    "domain_signals": empty_domain_signals(),
                }
            )
        return {
            "runner": "dry_run",
            "review_source": REVIEW_SOURCE_HEURISTIC,
            "review_diagnostics": ReviewDiagnostics(
                source=REVIEW_SOURCE_HEURISTIC, sessions_sent=len(sessions), finding_count=len(findings)
            ).to_dict(),
            "phase": phase,
            "sessions_reviewed": len(findings),
            "findings": findings,
            "repeated_workflows": repeated_workflows,
            "mistakes": mistakes,
            "dissents": dissents,
            # The local reviewer reads every admitted session, so every one of them was reviewed.
            "sent_session_keys": [s.session_key for s, _ in sessions],
        }

    def _plan(
        self,
        review_findings: dict[str, Any],
        existing_rules: list[RuleRecord],
        max_write_set: int,
        default_doc_prefix: str,
        phase: str,
    ) -> tuple[list[PlannedWrite], list[dict[str, Any]]]:
        """Local plan (knowledge docs and rules) plus the repeated-workflow candidates it does not turn into Skills."""
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
            try:
                # Same privacy pre-gate as the model path: a `<private>` span never reaches
                # SQLite or the approval preview, and a credential drops the whole draft.
                content = sanitize_and_check_secrets(content, context="p03:knowledge_doc")
            except SecretLeakError:
                logger.warning(
                    "P03 heuristic plan item dropped: session=%s reasons=['SECRET_LEAK_BLOCKED']",
                    item.get("session_key"),
                )
                used_paths.discard(rel_path)
                continue
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
                tokens = re.findall(r"[a-zA-Z0-9_-]{3,}|[一-鿿]{2,6}", obs_title)
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

        # Repeated workflows are reported, never written. A keyword-triggered planner cannot author a
        # Skill: its SKILL.md would be template prose, and an "update" would overwrite a real Skill.
        deferred: list[dict[str, Any]] = []
        for wf in review_findings.get("repeated_workflows", []):
            if not isinstance(wf, dict):
                continue
            action = "update" if wf.get("action") == "update" else "create"
            existing = str(wf.get("existing_skill_match") or "").strip()
            deferred.append(
                {
                    "session_key": str(wf.get("session_key") or ""),
                    "title": str(wf.get("title") or "")[:80],
                    "action": action,
                    "target_skill": existing or str(wf.get("name_hint") or wf.get("title") or "reusable-workflow"),
                    "reason": str(wf.get("reason") or "")[:200],
                }
            )
        return plans[:max_write_set], deferred

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
        return self._plan(review_findings, existing_rules, max_write_set, default_doc_prefix, phase)[0]

    def plan_writes_detailed(
        self,
        review_findings: dict[str, Any],
        existing_rules: list[RuleRecord],
        max_write_set: int,
        default_doc_prefix: str = "lessons/",
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
        routing_candidates: RoutingCandidates | None = None,
    ) -> PlanOutcome:
        """P03 plan plus diagnostics. The heuristic planner has no model to declare a route, so candidates are ignored."""
        plans, deferred = self._plan(review_findings, existing_rules, max_write_set, default_doc_prefix, phase)
        return PlanOutcome(
            plans,
            PlanDiagnostics(
                source=PLAN_SOURCE_HEURISTIC,
                returned_count=len(plans),
                kept_count=len(plans),
                deferred_skill_candidates=deferred,
            ),
        )
