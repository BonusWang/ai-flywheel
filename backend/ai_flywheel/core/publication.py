from __future__ import annotations

from typing import Any

from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.activation import ActivationEngine
from ai_flywheel.core.policies import PolicyEngine
from ai_flywheel.models import OutcomeOrigin, RuleStatus, SessionStatus, VerificationKind
from ai_flywheel.storage.db import FlywheelDB


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
        sessions = self.db.list_sessions(limit=200)
        outcomes = self.db.list_outcomes(limit=200)
        retro_items = self.db.list_retro_items(limit=200)
        runs = self.db.list_workflow_runs(limit=20)
        preflight_count = self.db.count_preflights()

        rule_counts = {
            RuleStatus.UNVERIFIED_ACTIVE.value: 0,
            RuleStatus.VERIFIED_ACTIVE.value: 0,
            RuleStatus.NEEDS_REVISION.value: 0,
            RuleStatus.RETIRED.value: 0,
        }
        for r in rules:
            rule_counts[r.status.value] = rule_counts.get(r.status.value, 0) + 1

        natural_outcomes = [o for o in outcomes if o.origin == OutcomeOrigin.NATURAL]
        real_verified_outcomes = [
            o
            for o in natural_outcomes
            if any(c.kind == VerificationKind.REAL and c.result.value == "pass" for c in o.checks)
        ]
        real_verification_rate = (
            round(len(real_verified_outcomes) / len(natural_outcomes) * 100, 1)
            if natural_outcomes
            else None
        )

        active_total = rule_counts["unverified_active"] + rule_counts["verified_active"]
        verified_ratio = (
            round(rule_counts["verified_active"] / active_total * 100, 1)
            if active_total > 0
            else None
        )

        open_retro = [x for x in retro_items if x.status.value in {"open", "in_review"}]
        pending_sessions = [s for s in sessions if s.status == SessionStatus.PENDING]
        latest_run = runs[0] if runs else None

        skills = [] if public_mode else self.activation.probe_local_skills()
        return {
            "site": site,
            "metrics": {
                "preflight_activations": preflight_count,
                "sessions_total": len(sessions),
                "sessions_pending": len(pending_sessions),
                "rules_total": len(rules),
                "rules_by_status": rule_counts,
                "verified_rule_ratio": verified_ratio,
                "natural_outcomes_total": len(natural_outcomes),
                "real_verification_rate": real_verification_rate,
                "open_retro_count": len(open_retro),
                "workflow_runs_total": len(runs),
                "probed_skills_count": len(skills),
            },
            "latest_run": self._sanitize_run(latest_run.model_dump(mode="json"), public_mode)
            if latest_run
            else None,
            "recent_rules": [
                self._sanitize_rule(r.model_dump(mode="json"), public_mode) for r in rules[:6]
            ],
            "recent_retro": [r.model_dump(mode="json") for r in open_retro[:5]],
            "safety_valves": self.cfg.safety_valves.model_dump(mode="json"),
            "workflow_lock": self.db.get_workflow_lock(),
        }

    def _sanitize_rule(self, rule: dict[str, Any], public_mode: bool) -> dict[str, Any]:
        if public_mode and self.cfg.safety_valves.public_export_strip_paths:
            rule["source_pointers"] = [p.split("/")[-1].split("\\")[-1] for p in rule.get("source_pointers", [])]
            rule["origin_session_key"] = None
        return rule

    def _sanitize_run(self, run: dict[str, Any], public_mode: bool) -> dict[str, Any]:
        if public_mode and self.cfg.safety_valves.public_export_strip_paths:
            for node in run.get("nodes", []):
                node["artifacts"] = [a.split("/")[-1].split("\\")[-1] for a in node.get("artifacts", [])]
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
            "",
            "## MCP Endpoint",
            f"- Streamable HTTP: {mcp_http_url}",
            "- Stdio CLI: `flywheel mcp-stdio`",
            "",
            "## Available MCP Tools",
            *[f"- `{tool['name']}`: {tool['description']}" for tool in MCP_TOOL_CATALOG],
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
        for r in rules[:12]:
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
