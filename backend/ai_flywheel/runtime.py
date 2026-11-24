from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from ai_flywheel.adapters.delivery import DeliveryAdapter
from ai_flywheel.adapters.knowledge import KnowledgeSinkAdapter
from ai_flywheel.adapters.sessions import SessionAdapter
from ai_flywheel.adapters.skills import SkillWriterAdapter
from ai_flywheel.config import FlywheelConfig, deep_merge, load_config, save_config
from ai_flywheel.core.activation import ActivationEngine
from ai_flywheel.core.core_card import read_core_card, rebuild_core_card
from ai_flywheel.core.hooks import ThinHookProcessor
from ai_flywheel.core.orchestrator import EventBroker, WorkflowOrchestrator
from ai_flywheel.core.policies import PolicyEngine
from ai_flywheel.core.publication import PublicationLayer
from ai_flywheel.core.receipts import ReceiptService
from ai_flywheel.mcp_server import build_mcp_server
from ai_flywheel.models import RegisterSessionRequest, RetroStatus, SessionRecord, SessionStatus
from ai_flywheel.storage.clock import SystemClock
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.fs_store import ImmutableFileStore
from ai_flywheel.storage.seed import seed_demo_data


class FlywheelRuntime:
    """Single dependency container wiring all decoupled engines, adapters, MCP, and APIs."""

    def __init__(self, cfg: FlywheelConfig, *, skip_fts_sync: bool = False, clock: SystemClock | None = None, process_role: str = "cli") -> None:
        self.cfg = cfg
        self.clock = clock or SystemClock()
        self.process_role = process_role
        self.db = FlywheelDB(cfg.db_path, clock=self.clock)
        self.fs_store = ImmutableFileStore(cfg.receipts_dir)
        self.policies = PolicyEngine(cfg, self.db)
        self.activation = ActivationEngine(cfg, self.db, self.fs_store, self.policies)
        self.receipts = ReceiptService(self.db, self.fs_store, self.policies, self.activation)
        self.sessions = SessionAdapter(cfg, self.db)
        self.knowledge = KnowledgeSinkAdapter(cfg, self.db)
        self.delivery = DeliveryAdapter(cfg)
        self.skill_writer = SkillWriterAdapter(cfg)
        self.broker = EventBroker()
        self.orchestrator = WorkflowOrchestrator(
            cfg=cfg,
            db=self.db,
            fs_store=self.fs_store,
            policies=self.policies,
            activation=self.activation,
            sessions=self.sessions,
            knowledge=self.knowledge,
            delivery=self.delivery,
            broker=self.broker,
            skill_writer=self.skill_writer,
        )
        self.hooks = ThinHookProcessor(self)
        self.publication = PublicationLayer(cfg, self.db, self.policies, self.activation)
        self.mcp = build_mcp_server(self)
        self.outbox = None
        self.lark_runner = None
        self.feishu_listener = None
        self.feishu_check = None
        self.feishu_channel = None
        self._build_feishu()
        # An outcome or a signal changes rules, dissents and freshness: the
        # derived core card is a projection of those, so it is refreshed with them.
        self.receipts.set_state_change_hook(self.refresh_core_card)

        if not skip_fts_sync:
            self.knowledge.sync_fts_index()
            for sk in self.activation.probe_local_skills():
                self.db.upsert_skill_asset(
                    skill_name=sk["name"],
                    source_dir=str(Path(sk["path"]).parent.parent),
                    description=sk.get("description", ""),
                    origin="manual",
                )
            self.refresh_core_card()

    def _build_feishu(self) -> None:
        fcfg = self.cfg.delivery.feishu_cli
        if not (fcfg.enabled and fcfg.identity == "bot" and fcfg.user_id):
            return
        from ai_flywheel.adapters.lark_cli import LarkCliRunner
        from ai_flywheel.core.feishu_check import FeishuCheckService
        from ai_flywheel.core.feishu_channel import FeishuChannel
        from ai_flywheel.core.feishu_listen import FeishuClickListener
        from ai_flywheel.storage.outbox import OutboxDB

        runner = LarkCliRunner(fcfg.command, fcfg.command_args, identity=fcfg.identity)
        # One resolved invocation for the whole process: the delivery adapter and
        # the Feishu channel must not each freeze their own copy of the exe.
        self.lark_runner = runner
        self.delivery.set_lark_runner(runner)
        self.outbox = OutboxDB(self.db)
        # The click consumer only runs while interactive mode is on: the app's
        # card.action.trigger subscription is single-consumer, so an idle listener
        # in a non-interactive serve would block whoever needs it.
        listener = FeishuClickListener(
            runner,
            on_click=lambda ev: self._route_feishu_click(ev),
            should_run=lambda: bool(self.cfg.delivery.feishu_cli.enabled and self.cfg.delivery.feishu_cli.interactive),
            audit=self._listener_audit,
        )
        check = FeishuCheckService(self.cfg, runner, lambda: listener.state, db=self.db)
        channel = FeishuChannel(
            self.cfg, self.db, self.outbox, runner,
            listener=listener, check=check,
            approve_fn=self.orchestrator.approve_and_resume,
            reject_fn=self.orchestrator.reject_run,
            process_role=self.process_role,
        )
        self.feishu_listener, self.feishu_check, self.feishu_channel = listener, check, channel
        self.orchestrator.feishu_channel = channel

    def _route_feishu_click(self, event) -> None:
        if self.feishu_channel is not None:
            self.feishu_channel.handle_click(event)

    def start_feishu(self) -> None:
        if self.feishu_channel is None:
            return
        if self.feishu_check is not None:
            try:
                self.feishu_check.startup_cleanup()
            except Exception:
                import logging

                logging.getLogger("ai_flywheel.feishu_check").exception("startup cleanup failed")
        if self.feishu_listener is not None:
            # Startup audit: which lark-cli was resolved (path, version, file hash).
            lark = self.feishu_channel.runner
            try:
                version = lark.version()
            except Exception:
                version = "unknown"
            digest = ""
            try:
                digest = hashlib.sha256(Path(lark.target_path).read_bytes()).hexdigest()
            except OSError:
                pass
            self._listener_audit(
                "listener_started",
                f"点击监听器启动：exe={lark.target_path} version={version} sha256={digest}",
                {"exe": lark.target_path, "version": version, "sha256": digest, "kind": lark.kind},
            )
            self.feishu_listener.start()
        self.feishu_channel.start()

    def _listener_audit(self, kind: str, detail: str, payload: dict | None = None) -> None:
        try:
            self.db.append_audit_event(
                entity_type="decision_card",
                entity_id="feishu_listener",
                event_type=kind,
                actor="feishu_listener",
                summary=detail[:300],
                payload=payload or {"detail": detail[:300]},
            )
        except Exception:
            pass

    def stop_feishu(self) -> None:
        if self.feishu_channel is not None:
            self.feishu_channel.stop()
        if self.feishu_listener is not None:
            self.feishu_listener.stop()

    def refresh_core_card(self) -> dict[str, Any]:
        return rebuild_core_card(self.cfg, self.db)

    def read_core_card(self) -> dict[str, Any]:
        """The derived core card as it stands, without rewriting it on disk."""
        return read_core_card(self.cfg, self.db)

    @classmethod
    def create(
        cls,
        config_path: str | Path | None = None,
        base_dir: str | Path | None = None,
        auto_seed: bool = False,
        skip_fts_sync: bool = False,
        process_role: str = "cli",
    ) -> FlywheelRuntime:
        cfg = load_config(config_path=config_path, base_dir=base_dir)
        rt = cls(cfg, skip_fts_sync=skip_fts_sync, process_role=process_role)
        if auto_seed:
            seed_demo_data(rt.cfg, rt.db, rt.activation, rt.receipts)
            # The seed writes rules and observations: the derived core card has
            # to follow, or a plain read of it would hand out a stale card.
            rt.refresh_core_card()
        return rt

    def register_session(
        self, req: RegisterSessionRequest, *, preserve_existing_lifecycle: bool = False
    ) -> SessionRecord:
        """Register or refresh a session.

        ``preserve_existing_lifecycle`` is what a lifecycle hook (Stop / SessionEnd /
        SessionStart) uses: it must never re-open a session the flywheel already owns.
        An explicit ingestion (MCP / manual) leaves it off, so new content still re-queues
        a processed session.
        """
        skey = f"{req.client}:{req.session_id}"
        open_retros = [
            r for r in self.db.list_retro_items(status=RetroStatus.OPEN.value) if r.session_key == skey
        ]
        priority = self.sessions.classify_priority(req, has_open_retro=bool(open_retros))
        rec = SessionRecord(
            session_key=skey,
            client=req.client,
            session_id=req.session_id,
            title=req.title or f"{req.client} · {req.session_id[:16]}",
            project=req.project or "generic",
            workspace=req.workspace,
            transcript_path=req.transcript_path,
            summary=req.summary,
            content=req.content,
            tags=req.tags,
            priority=priority,
            status=SessionStatus.PENDING,
            metadata=req.metadata,
        )
        return self.db.upsert_session(rec, preserve_existing_lifecycle=preserve_existing_lifecycle)

    # The settings that are read once, at startup: changing them only takes
    # effect after the process is restarted.
    STARTUP_ONLY_PATHS: tuple[tuple[str, ...], ...] = (
        ("workflow", "schedule_enabled"),
        ("workflow", "daily_cron"),
        ("delivery", "feishu_cli", "command"),
        ("delivery", "feishu_cli", "command_args"),
        ("delivery", "feishu_cli", "identity"),
    )

    @staticmethod
    def _without_empty_api_key(patch: dict[str, Any]) -> dict[str, Any]:
        """Drop an empty or redacted api key: the configured one must survive.

        The settings form sends the key back as ``[REDACTED]``; writing that
        over the real key would lock the user out of their own endpoint.
        """
        runner = patch.get("runner")
        if not isinstance(runner, dict):
            return patch
        api = runner.get("api")
        if not isinstance(api, dict):
            return patch
        value = api.get("api_key")
        if value and value != "[REDACTED]":
            return patch
        cleaned = copy.deepcopy(patch)
        cleaned["runner"]["api"].pop("api_key", None)
        return cleaned

    @staticmethod
    def _dig(data: dict[str, Any], path: tuple[str, ...]) -> Any:
        node: Any = data
        for key in path:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node

    @classmethod
    def _restart_reasons(cls, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
        """The startup-only settings this patch changed."""
        return [
            ".".join(path)
            for path in cls.STARTUP_ONLY_PATHS
            if cls._dig(before, path) != cls._dig(after, path)
        ]

    def update_runtime_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge a patch into the live configuration.

        The merge is a deep merge, so a nested patch (``sessions.triage``) keeps
        the sibling keys of its section instead of replacing the whole section.

        Returns the new configuration together with whether a restart is needed
        for the settings that are only read at startup.
        """
        current = self.cfg.model_dump(mode="json")
        scoped = {
            section: values
            for section, values in patch.items()
            if isinstance(values, dict) and isinstance(current.get(section), dict)
        }
        merged = deep_merge(current, self._without_empty_api_key(scoped))
        updated = FlywheelConfig.model_validate(merged)
        updated.base_dir = self.cfg.base_dir
        updated.config_path = self.cfg.config_path
        save_config(updated)
        # Apply in-place to all holders sharing self.cfg reference
        for field_name in FlywheelConfig.model_fields:
            if field_name in {"base_dir", "config_path"}:
                continue
            setattr(self.cfg, field_name, getattr(updated, field_name))
        for section, values in patch.items():
            if isinstance(values, dict):
                for k, v in values.items():
                    if k == "api" and isinstance(v, dict) and "api_key" in v:
                        v = {ak: ("***" if ak == "api_key" else av) for ak, av in v.items()}
                    self.db.set_system_setting(f"{section}.{k}", section, v, updated_by="user")
        self.db.append_audit_event(
            entity_type="system_settings",
            entity_id="flywheel.yaml",
            event_type="config_updated",
            actor="user",
            summary=f"更新系统配置分组：{', '.join(patch.keys())}",
        )
        self.knowledge.sync_fts_index()
        restart_reasons = self._restart_reasons(current, merged)
        return {
            "config": self.cfg,
            "restart_required": bool(restart_reasons),
            "restart_reasons": restart_reasons,
        }
