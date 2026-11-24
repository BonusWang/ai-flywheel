from __future__ import annotations

import re
from typing import Any

from ai_flywheel import __version__
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.activation import ActivationEngine
from ai_flywheel.core.policies import PolicyEngine
from ai_flywheel.models import OutcomeOrigin, RetroStatus, RuleStatus, SessionStatus
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.fs_store import redact_text


FLYWHEEL_VERSION = __version__

# The rule states that count as "in force". A retired, dormant or
# needs-revision rule is not one of them and must not be shown as such.
ACTIVE_RULE_STATUSES = frozenset(
    {RuleStatus.VERIFIED_ACTIVE.value, RuleStatus.UNVERIFIED_ACTIVE.value}
)

# A path-looking token: an absolute Windows or POSIX path. The look-behind
# keeps a relative path (`lessons/a.md`) and a URL (`https://host/a.md`) out of
# the match — only a real absolute path is stripped.
_ABS_PATH_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9:/])((?:[A-Za-z]:[\\/]|/)[^\s\"'`<>|]*)")


MCP_TOOL_CATALOG: list[dict[str, str]] = [
    {
        "name": "flywheel_register_session",
        "title_zh": "会话登记",
        "phase": "登记",
        "description_zh": "登记 AI 客户端会话元数据与本地日志路径，纳入后台飞轮待审队列。",
        "description": "Register an AI client session (local transcript path or summary/content).",
    },
    {
        "name": "flywheel_preflight_activate",
        "title_zh": "启程预检",
        "phase": "启程",
        "description_zh": "任务开局注入反偷懒策略、协作红线与命中先例，返回不可变哈希回执。",
        "description": "Retrieve anti-laziness policy card, user collaboration preferences, matched rules, and immutable receipt ID.",
    },
    {
        "name": "flywheel_report_signal",
        "title_zh": "异常上报",
        "phase": "监控",
        "description_zh": "上报重复报错死循环、高风险操作、复发或阻塞信号，推入复盘队列。",
        "description": "Report anomaly signals (retry loop, high-risk tool, recurrence, blocker) to retro queue.",
    },
    {
        "name": "flywheel_record_outcome",
        "title_zh": "证据回执",
        "phase": "验证",
        "description_zh": "提交分级验证回执（真实/测试/静态/未知）与硬门禁检查，驱动规则晋升或修订。",
        "description": "Submit evidence-backed verification outcome (real/test/static/unknown) with hard gate checks.",
    },
    {
        "name": "flywheel_ingest_session",
        "title_zh": "会话入队",
        "phase": "采集",
        "description_zh": "供纯 MCP 客户端（如 Claude Desktop）直接推送会话摘要与事实事件。",
        "description": "Push conversation digest or events directly from MCP-only clients (e.g. Claude Desktop).",
    },
    {
        "name": "flywheel_status",
        "title_zh": "健康态势",
        "phase": "观测",
        "description_zh": "查看飞轮全局健康指标、活跃规则分布与最近一次后台工作流状态。",
        "description": "Inspect flywheel health metrics and latest P00-P07 run status.",
    },
]


MCP_RESOURCE_CATALOG: list[dict[str, str]] = [
    {
        "uri": "flywheel://core-card",
        "title_zh": "核心索引卡",
        "description_zh": "自动派生的 ≤200 行跨智能体索引卡：生效规则（含未决分歧）、错题观察池聚类、活跃 Skill 实效账本、过期经验卡提醒。",
        "description": "Derived <=200-line cross-agent index card: active rules with open dissents, mistake-pool clusters, skill effectiveness ledger, expired card warnings.",
    },
    {
        "uri": "flywheel://policies/active",
        "title_zh": "现行策略",
        "description_zh": "全部现行飞轮方法论策略（反偷懒、闭环契约、硬门禁、个人偏好）。",
        "description": "All active flywheel methodology policies (anti-laziness, loop contract, hard gates, user preferences).",
    },
    {
        "uri": "flywheel://rules/active",
        "title_zh": "现行规则",
        "description_zh": "当前生效的先例避坑规则（试运行与已验证）。",
        "description": "Currently active precedent rules (unverified_active and verified_active).",
    },
    {
        "uri": "flywheel://runs/latest",
        "title_zh": "最新批次",
        "description_zh": "最近一次 P00–P07 自主工作流运行报告。",
        "description": "Latest P00-P07 autonomous workflow run report.",
    },
]


class PublicationLayer:
    """Single read model shared by Web Portal, REST API v1, MCP Resources, and /llms.txt.

    Inspired by AIHOT's publication/ invariant: all consumers read identical numbers,
    and public_mode strips local filesystem paths for future public showcase deployment.
    """

    def __init__(
        self,
        cfg: FlywheelConfig,
        db: FlywheelDB,
        policies: PolicyEngine,
        activation: ActivationEngine,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.policies = policies
        self.activation = activation

    def get_overview(self, public_mode: bool = False) -> dict[str, Any]:
        site = self.policies.load_site_profile()
        rules = self.db.list_rules()
        runs = self.db.list_workflow_runs(limit=20)
        preflight_count = self.db.count_preflights()

        # The counts are exact: they come from COUNT(*), not from a page of rows.
        # A page would cap the number at its size and the portal would say
        # "20 sessions" on a store that holds 200.
        sessions_total = self.db.count_sessions()
        sessions_pending = self.db.count_sessions(status=SessionStatus.PENDING.value)
        natural_outcomes_total = self.db.count_outcomes(origin=OutcomeOrigin.NATURAL.value)
        real_verified_outcomes = self.db.count_real_verified_natural_outcomes()
        open_retro_count = self.db.count_retro_items(
            status=RetroStatus.OPEN.value
        ) + self.db.count_retro_items(status=RetroStatus.IN_REVIEW.value)
        workflow_runs_total = self.db.count_workflow_runs()

        rule_counts = {
            RuleStatus.UNVERIFIED_ACTIVE.value: 0,
            RuleStatus.VERIFIED_ACTIVE.value: 0,
            RuleStatus.NEEDS_REVISION.value: 0,
            RuleStatus.DORMANT.value: 0,
            RuleStatus.RETIRED.value: 0,
        }
        for r in rules:
            rule_counts[r.status.value] = rule_counts.get(r.status.value, 0) + 1

        real_verification_rate = (
            round(real_verified_outcomes / natural_outcomes_total * 100, 1)
            if natural_outcomes_total
            else None
        )

        active_total = rule_counts["unverified_active"] + rule_counts["verified_active"]
        verified_ratio = (
            round(rule_counts["verified_active"] / active_total * 100, 1)
            if active_total > 0
            else None
        )

        latest_run = runs[0] if runs else None
        # Only the rules that are in force are shown as active ones; a retired
        # or dormant rule would otherwise be counted and labelled as if it were.
        active_rules = [r for r in rules if r.status.value in ACTIVE_RULE_STATUSES]
        open_retro = sorted(
            [
                *self.db.list_retro_items(status=RetroStatus.OPEN.value, limit=5),
                *self.db.list_retro_items(status=RetroStatus.IN_REVIEW.value, limit=5),
            ],
            key=lambda item: item.created_at,
            reverse=True,
        )[:5]

        skills = [] if public_mode else self.activation.probe_local_skills()
        return {
            "site": site,
            "metrics": {
                "preflight_activations": preflight_count,
                "sessions_total": sessions_total,
                "sessions_pending": sessions_pending,
                "rules_total": len(rules),
                "rules_by_status": rule_counts,
                "verified_rule_ratio": verified_ratio,
                "natural_outcomes_total": natural_outcomes_total,
                "real_verification_rate": real_verification_rate,
                "open_retro_count": open_retro_count,
                "workflow_runs_total": workflow_runs_total,
                "probed_skills_count": len(skills),
            },
            "latest_run": self._sanitize_run(latest_run.model_dump(mode="json"), public_mode)
            if latest_run
            else None,
            "recent_rules": [
                self._sanitize_rule(r.model_dump(mode="json"), public_mode) for r in active_rules[:6]
            ],
            "recent_retro": [self._sanitize_retro(r.model_dump(mode="json"), public_mode) for r in open_retro[:5]],
            "safety_valves": self.cfg.safety_valves.model_dump(mode="json"),
            "workflow_lock": self.db.get_workflow_lock(),
        }

    def _sanitize_rule(self, rule: dict[str, Any], public_mode: bool) -> dict[str, Any]:
        if public_mode and self.cfg.safety_valves.public_export_strip_paths:
            rule["source_pointers"] = [p.split("/")[-1].split("\\")[-1] for p in rule.get("source_pointers", [])]
            rule["origin_session_key"] = None
        return rule

    def _sanitize_retro(self, item: dict[str, Any], public_mode: bool) -> dict[str, Any]:
        if public_mode and self.cfg.safety_valves.public_export_strip_paths:
            if item.get("summary"):
                item["summary"] = redact_text(str(item["summary"]))
            if item.get("session_key"):
                item["session_key"] = redact_text(str(item["session_key"]))
            if isinstance(item.get("detail"), dict):
                item["detail"] = self._redact_structure(item["detail"])
        return item

    @staticmethod
    def _strip_abs_paths(text: str) -> str:
        """Take the local layout out of a path.

        An absolute path is reduced to its file name: ``C:\\Users\\<name>\\kb\\lessons\\a.md``
        becomes ``a.md`` and ``/home/<name>/kb/lessons/a.md`` becomes ``a.md`` —
        the local layout and the account name both go. A relative path stays as
        it is: it carries the knowledge partition (``lessons/``, ``projects/``)
        and no private prefix.
        """

        def _one(token: str) -> str:
            parts = [p for p in token.replace("\\", "/").split("/") if p]
            return parts[-1] if parts else token

        return _ABS_PATH_TOKEN_RE.sub(lambda m: _one(m.group(1)), text)

    def _redact_structure(self, value: Any) -> Any:
        """Walk a JSON-ish structure and take paths and credentials out of it."""
        if isinstance(value, str):
            return redact_text(self._strip_abs_paths(value))
        if isinstance(value, dict):
            return {k: self._redact_structure(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_structure(v) for v in value]
        return value

    def _sanitize_run(self, run: dict[str, Any], public_mode: bool) -> dict[str, Any]:
        if not (public_mode and self.cfg.safety_valves.public_export_strip_paths):
            return run
        for node in run.get("nodes", []):
            node["artifacts"] = [self._strip_abs_paths(a) for a in node.get("artifacts", [])]
            if node.get("payload"):
                node["payload"] = self._redact_structure(node["payload"])
        for write in run.get("planned_writes", []):
            if write.get("relative_path"):
                write["relative_path"] = redact_text(self._strip_abs_paths(str(write["relative_path"])))
            if write.get("content_preview"):
                write["content_preview"] = redact_text(str(write["content_preview"]))
            if write.get("summary"):
                write["summary"] = redact_text(str(write["summary"]))
        if run.get("digest_markdown"):
            run["digest_markdown"] = redact_text(str(run["digest_markdown"]))
        if run.get("admitted_sessions"):
            run["admitted_sessions"] = [redact_text(str(s)) for s in run["admitted_sessions"]]
        if run.get("error"):
            run["error"] = redact_text(self._strip_abs_paths(str(run["error"])))
        return run

    def build_llms_txt(self) -> str:
        overview = self.get_overview(public_mode=True)
        brand = overview["site"].get("brand", {})
        metrics = overview["metrics"]
        mcp_http_url = f"http://{self.cfg.server.host}:{self.cfg.server.port}/mcp"
        lines = [
            f"# {brand.get('name', 'AI Flywheel')}",
            "",
            f"> {brand.get('motto', '')}",
            f"> {brand.get('subtitle', '')}",
            f"> Version: {FLYWHEEL_VERSION}",
            "",
            "## MCP Endpoint",
            f"- Streamable HTTP: {mcp_http_url}",
            "- Stdio CLI: `flywheel mcp-stdio`",
            "",
            "## Available MCP Tools",
            *[f"- `{tool['name']}`: {tool['description']}" for tool in MCP_TOOL_CATALOG],
            "",
            "## Available MCP Resources (read-only)",
            *[f"- `{res['uri']}`: {res['description']}" for res in MCP_RESOURCE_CATALOG],
            "",
            "## Current Metrics",
            f"- Preflight Activations: {metrics['preflight_activations']}",
            f"- Active Rules: {metrics['rules_by_status']['verified_active']} verified / {metrics['rules_by_status']['unverified_active']} unverified",
            f"- Open Retro Items: {metrics['open_retro_count']}",
            f"- Workflow Runs: {metrics['workflow_runs_total']}",
        ]
        return "\n".join(lines) + "\n"

    def get_about_payload(self, public_mode: bool = False) -> dict[str, Any]:
        overview = self.get_overview(public_mode=public_mode)
        base_url = f"http://{self.cfg.server.host}:{self.cfg.server.port}"
        sessions = self.db.list_sessions(limit=40)
        rules = self.db.list_rules()
        retro = self.db.list_retro_items(limit=20)

        river_sources: list[dict[str, Any]] = []
        for s in sessions[:18]:
            river_sources.append(
                {
                    "name": s.title if not public_mode else f"{s.client} 会话信号",
                    "kind": s.client,
                    "signal_only": s.status == SessionStatus.PENDING,
                }
            )
        for r in [rule for rule in rules if rule.status.value in ACTIVE_RULE_STATUSES][:12]:
            river_sources.append(
                {
                    "name": r.title,
                    "kind": "rule",
                    "signal_only": r.status != RuleStatus.VERIFIED_ACTIVE,
                }
            )
        for item in retro[:10]:
            river_sources.append(
                {
                    "name": item.summary[:42],
                    "kind": "retro",
                    "signal_only": True,
                }
            )

        return {
            "site": overview["site"],
            "metrics": overview["metrics"],
            "endpoints": {
                "portal_url": base_url,
                "mcp_http_url": f"{base_url}/mcp",
                "mcp_stdio_cmd": "flywheel mcp-stdio",
                "llms_txt_url": f"{base_url}/llms.txt",
                "overview_api_url": f"{base_url}/api/v1/overview",
            },
            "mcp_tools": MCP_TOOL_CATALOG,
            "llms_txt": self.build_llms_txt(),
            "river_sources": river_sources,
            "recent_rules": overview["recent_rules"],
        }

    def export_public_snapshot(self) -> dict[str, Any]:
        """Desensitized JSON snapshot ready for future static/public portal publishing."""
        overview = self.get_overview(public_mode=True)
        rules = [
            self._sanitize_rule(r.model_dump(mode="json"), public_mode=True)
            for r in self.db.list_rules()
        ]
        runs = [
            self._sanitize_run(r.model_dump(mode="json"), public_mode=True)
            for r in self.db.list_workflow_runs(limit=10)
        ]
        policies = {
            pid: {k: v for k, v in pol.items() if not k.startswith("_")}
            for pid, pol in self.policies.list_policies().items()
            if pid != "user_preferences"
        }
        return {
            "schema_version": 1,
            "overview": overview,
            "policies": policies,
            "rules": rules,
            "runs": runs,
        }
