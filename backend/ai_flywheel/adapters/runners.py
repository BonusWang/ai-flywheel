from __future__ import annotations

import json
import re
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import httpx

from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import PlannedWrite, RetroItem, RuleRecord, SessionRecord
from ai_flywheel.storage.fs_store import ImmutableFileStore, canonical_hash


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", text).strip("-").lower()
    return cleaned[:42] or "session-lesson"


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
    """Abstract AI execution runner (supports local CLI subprocess, dry-run, and future API adapters)."""

    @abstractmethod
    def review_sessions(
        self,
        sessions: list[tuple[SessionRecord, str]],
        open_retros: list[RetroItem],
    ) -> dict[str, Any]:
        """P02: Extract structured facts, incidents, and candidate lessons from admitted sessions."""

    @abstractmethod
    def plan_writes(
        self,
        review_findings: dict[str, Any],
        existing_rules: list[RuleRecord],
        max_write_set: int,
        default_doc_prefix: str = "lessons/",
    ) -> list[PlannedWrite]:
        """P03: Deduplicate against existing rules and generate controlled write plan."""


class DryRunHeuristicRunner(BaseWorkflowRunner):
    """Zero-cost deterministic local runner for zero-config operation and testing."""

    def review_sessions(
        self,
        sessions: list[tuple[SessionRecord, str]],
        open_retros: list[RetroItem],
    ) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        retro_by_session: dict[str, list[RetroItem]] = {}
        for r in open_retros:
            retro_by_session.setdefault(r.session_key, []).append(r)

        for sess, text in sessions:
            s_retros = retro_by_session.get(sess.session_key, [])
            title = sess.title or f"{sess.client} 会话沉淀 ({sess.session_id[:12]})"
            summary = sess.summary or (text[:220] + ("..." if len(text) > 220 else "")) or "无正文摘要"
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
                    "suggest_rule": bool(s_retros or any(k in text.lower() for k in ["失败", "报错", "重试", "fix", "error", "规则"])),
                }
            )
        return {
            "runner": "dry_run",
            "sessions_reviewed": len(findings),
            "findings": findings,
        }

    def plan_writes(
        self,
        review_findings: dict[str, Any],
        existing_rules: list[RuleRecord],
        max_write_set: int,
        default_doc_prefix: str = "lessons/",
    ) -> list[PlannedWrite]:
        existing_titles = {r.title.lower() for r in existing_rules}
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
                f"- 下次同类任务先通过 `flywheel_preflight_activate` 激活上下文并执行真实验证。\n"
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
                triggers = list(dict.fromkeys([t for t in item.get("tags", []) if len(t) >= 2] + [item["project"]]))[:5]
                plans.append(
                    PlannedWrite(
                        target_kind="flywheel_rule",
                        relative_path=None,
                        action="register_rule",
                        title=f"先例防线：{title[:40]}",
                        summary="基于复盘线索注册新活跃规则（初始状态 unverified_active，待后续自然任务再验证）",
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
                )
        return plans[:max_write_set]


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

        # Receipt cache key to avoid re-running identical expensive CLI prompts
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
    ) -> dict[str, Any]:
        payload = [
            {"session_key": s.session_key, "client": s.client, "project": s.project, "title": s.title, "text": t[:6000]}
            for s, t in sessions
        ]
        prompt = (
            "You are the P02 Review Worker of AI Flywheel. Analyze the following sessions and return STRICT JSON: "
            '{"findings":[{"session_key":"...","client":"...","project":"...","title":"...",'
            '"facts":["..."],"anomalies":["..."],"tags":["..."],"suggest_rule":true}]}.\n\n'
            + json.dumps(payload, ensure_ascii=False)
        )
        parsed = self._invoke_cli("p02_review", prompt)
        if parsed and isinstance(parsed.get("findings"), list):
            parsed["runner"] = f"cli:{self.cfg.runner.cli.command}"
            parsed["sessions_reviewed"] = len(parsed["findings"])
            return parsed
        res = self.fallback.review_sessions(sessions, open_retros)
        res["runner"] = f"cli_fallback_dry_run:{self.cfg.runner.cli.command or 'unconfigured'}"
        return res

    def plan_writes(
        self,
        review_findings: dict[str, Any],
        existing_rules: list[RuleRecord],
        max_write_set: int,
        default_doc_prefix: str = "lessons/",
    ) -> list[PlannedWrite]:
        existing_summary = [{"rule_id": r.rule_id, "title": r.title, "status": r.status.value} for r in existing_rules]
        prompt = (
            "You are the P03 Route & Write-Plan Worker of AI Flywheel. Deduplicate against existing_rules and return STRICT JSON: "
            f'{{"planned_writes":[{{"target_kind":"knowledge_doc|flywheel_rule","relative_path":"{default_doc_prefix}....md or null",'
            '"action":"create|update|register_rule","title":"...","summary":"...","content_preview":"...",'
            '"rule_payload":null,"source_session_keys":["..."]}]}. '
            f"Max items: {max_write_set}.\n\n"
            + json.dumps({"findings": review_findings.get("findings", []), "existing_rules": existing_summary}, ensure_ascii=False)
        )
        parsed = self._invoke_cli("p03_plan", prompt)
        if parsed and isinstance(parsed.get("planned_writes"), list):
            out: list[PlannedWrite] = []
            for item in parsed["planned_writes"][:max_write_set]:
                try:
                    out.append(PlannedWrite.model_validate(item))
                except Exception:
                    continue
            if out:
                return out
        return self.fallback.plan_writes(
            review_findings,
            existing_rules,
            max_write_set,
            default_doc_prefix=default_doc_prefix,
        )


class OpenAICompatibleRunner(BaseWorkflowRunner):
    """OpenAI-compatible HTTP API runner (works with local Magpie gateway, OpenAI, DeepSeek, Ollama, etc.)."""

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
    ) -> dict[str, Any]:
        payload = [
            {"session_key": s.session_key, "client": s.client, "project": s.project, "title": s.title, "text": t[:6000]}
            for s, t in sessions
        ]
        prompt = (
            "You are the P02 Review Worker of AI Flywheel. Analyze the sessions and return STRICT JSON: "
            '{"findings":[{"session_key":"...","client":"...","project":"...","title":"...",'
            '"facts":["..."],"anomalies":["..."],"tags":["..."],"suggest_rule":true}]}.\n\n'
            + json.dumps(payload, ensure_ascii=False)
        )
        parsed = self._invoke_api("p02_review", prompt)
        if parsed and isinstance(parsed.get("findings"), list):
            parsed["runner"] = f"api:{self.cfg.runner.api.model}"
            parsed["sessions_reviewed"] = len(parsed["findings"])
            return parsed
        res = self.fallback.review_sessions(sessions, open_retros)
        res["runner"] = f"api_fallback_dry_run:{self.cfg.runner.api.model or 'unconfigured'}"
        return res

    def plan_writes(
        self,
        review_findings: dict[str, Any],
        existing_rules: list[RuleRecord],
        max_write_set: int,
        default_doc_prefix: str = "lessons/",
    ) -> list[PlannedWrite]:
        existing_summary = [{"rule_id": r.rule_id, "title": r.title, "status": r.status.value} for r in existing_rules]
        prompt = (
            "You are the P03 Route & Write-Plan Worker of AI Flywheel. Deduplicate against existing_rules and return STRICT JSON: "
            f'{{"planned_writes":[{{"target_kind":"knowledge_doc|flywheel_rule","relative_path":"{default_doc_prefix}....md or null",'
            '"action":"create|update|register_rule","title":"...","summary":"...","content_preview":"...",'
            '"rule_payload":null,"source_session_keys":["..."]}]}. '
            f"Max items: {max_write_set}.\n\n"
            + json.dumps({"findings": review_findings.get("findings", []), "existing_rules": existing_summary}, ensure_ascii=False)
        )
        parsed = self._invoke_api("p03_plan", prompt)
        if parsed and isinstance(parsed.get("planned_writes"), list):
            out: list[PlannedWrite] = []
            for item in parsed["planned_writes"][:max_write_set]:
                try:
                    out.append(PlannedWrite.model_validate(item))
                except Exception:
                    continue
            if out:
                return out
        return self.fallback.plan_writes(
            review_findings,
            existing_rules,
            max_write_set,
            default_doc_prefix=default_doc_prefix,
        )


def build_runner(cfg: FlywheelConfig, fs_store: ImmutableFileStore) -> BaseWorkflowRunner:
    if cfg.runner.mode == "api" and cfg.runner.api.base_url and cfg.runner.api.model and cfg.safety_valves.cli_execution_enabled:
        return OpenAICompatibleRunner(cfg, fs_store)
    if cfg.runner.mode == "cli" and cfg.runner.cli.command and cfg.safety_valves.cli_execution_enabled:
        return CliSubprocessRunner(cfg, fs_store)
    return DryRunHeuristicRunner()
