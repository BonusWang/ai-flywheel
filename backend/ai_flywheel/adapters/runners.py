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
from ai_flywheel.models import CreateRuleRequest, PlannedWrite, RetroItem, RuleRecord, SessionRecord
from ai_flywheel.storage.fs_store import ImmutableFileStore, canonical_hash


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
    return validated.model_dump(mode="json")


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
        if not pw.relative_path:
            prefix = default_doc_prefix.replace("\\", "/").strip("/")
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
        retro_by_session: dict[str, list[RetroItem]] = {}
        for r in open_retros:
            retro_by_session.setdefault(r.session_key, []).append(r)

        skill_names = {str(sk.get("name", "")).lower(): sk for sk in (existing_skills or [])}

        for sess, text in sessions:
            s_retros = retro_by_session.get(sess.session_key, [])
            title = sess.title or f"{sess.client} 会话沉淀 ({sess.session_id[:12]})"
            summary = sess.summary or (text[:220] + ("..." if len(text) > 220 else "")) or "无正文摘要"
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

            findings.append(
                {
                    "session_key": sess.session_key,
                    "client": sess.client,
                    "project": sess.project,
                    "title": title,
                    "facts": [summary],
                    "anomalies": [r.summary for r in s_retros],
                    "tags": sess.tags or [sess.project, sess.client],
                    "char_count": len(text),
                    "suggest_rule": bool(
                        s_retros or any(k in lower_all for k in ["失败", "报错", "重试", "fix", "error", "规则"])
                    ),
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
        used_paths: set[str] = set()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prefix = default_doc_prefix.replace("\\", "/").strip("/")
        prefix = f"{prefix}/" if prefix else "lessons/"
        plans: list[PlannedWrite] = []

        for item in review_findings.get("findings", []):
            if len(plans) >= max_write_set:
                break
            title = item["title"]
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
                f"---\n\n"
                f"# {title}\n\n"
                f"## 事实回顾\n{facts_md}\n\n"
                f"## 异常与根因线索\n{anomalies_md}\n\n"
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

            if item.get("suggest_rule") and title.lower() not in existing_titles and len(plans) < max_write_set:
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
        "Analyze the sessions for: (1) factual lessons, (2) anomalies/pitfalls worth a rule, AND "
        "(3) repeated multi-step workflows worth encapsulating into a new Skill or updating an existing Skill "
        "(compare against existing_skills to avoid duplicates).\n"
        "Return STRICT JSON ONLY:\n"
        '{"findings":[{"session_key":"...","client":"...","project":"...","title":"...",'
        '"facts":["..."],"anomalies":["..."],"tags":["..."],"suggest_rule":true,'
        '"skill_route":{"hit":false,"action":"none|create|update","target_skill":"slug-or-empty","reason":"..."}}],'
        '"repeated_workflows":[{"session_key":"...","name_hint":"kebab-case-skill-slug","title":"...",'
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
    return (
        "You are the P03 Route & Write-Plan Worker of AI Flywheel.\n"
        "Deduplicate against existing_rules and existing_skills, then return the top highest-value writes in Chinese.\n"
        "Allowed target_kind values:\n"
        f'1. "knowledge_doc": relative_path under "{default_doc_prefix}YYYY-MM-DD-slug.md", action="create|update", content_preview=concise structured markdown (200-400 chars).\n'
        '2. "flywheel_rule": relative_path=null, action="register_rule", MUST include rule_payload with EXACT keys: '
        '{"title":"简短中文标题","domain":"engineering","projects":["generic"],"triggers":["kw1","kw2"],'
        '"checklist":["检查项1","检查项2"],"forbidden_routes":["禁忌路线"]}.\n'
        '3. "skill_improvement" (ONLY for high-value repeated multi-step workflows from repeated_workflows): '
        'relative_path="<kebab-case-slug>/SKILL.md", action="create|update", requires_approval=true, '
        'approval_reason="简述为何值得封装或优化该 Skill", '
        'skill_route={"hit":true,"action":"create|update","target_skill":"<slug>","reason":"..."}, '
        'content_preview MUST be concise valid SKILL.md (250-500 chars) starting with YAML frontmatter "---\\nname: <slug>\\ndescription: ...\\n---\\n\\n# ...".\n'
        f"IMPORTANT: Output at most {capped_items} total items (prefer 1-2 knowledge_doc, 1-2 flywheel_rule, 1-2 skill_improvement). Keep content_preview concise so JSON completes quickly. Return STRICT JSON ONLY:\n"
        '{"planned_writes":[...]}\n\n'
        + json.dumps(
            {
                "phase": phase,
                "findings": review_findings.get("findings", []),
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

    def _invoke_api(self, stage: str, prompt: str) -> dict[str, Any] | None:
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
                    {
                        "role": "system",
                        "content": "You are an AI Flywheel workflow worker. Return strictly valid JSON with no markdown prose outside JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
            },
            timeout=api_cfg.timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = _extract_json_block(content if isinstance(content, str) else json.dumps(content))
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
        parsed = self._invoke_api("p02_review", prompt)
        if parsed and isinstance(parsed.get("findings"), list):
            parsed["runner"] = f"api:{self.cfg.runner.api.model}"
            parsed["phase"] = phase
            parsed["sessions_reviewed"] = len(parsed["findings"])
            parsed.setdefault("repeated_workflows", [])
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
        parsed = self._invoke_api("p03_plan", prompt)
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
