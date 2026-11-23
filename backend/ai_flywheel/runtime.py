from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_flywheel.adapters.delivery import DeliveryAdapter
from ai_flywheel.adapters.knowledge import KnowledgeSinkAdapter
from ai_flywheel.adapters.sessions import SessionAdapter
from ai_flywheel.config import FlywheelConfig, load_config, save_config
from ai_flywheel.core.activation import ActivationEngine
from ai_flywheel.core.hooks import ThinHookProcessor
from ai_flywheel.core.orchestrator import EventBroker, WorkflowOrchestrator
from ai_flywheel.core.policies import PolicyEngine
from ai_flywheel.core.publication import PublicationLayer
from ai_flywheel.core.receipts import ReceiptService
from ai_flywheel.mcp_server import build_mcp_server
from ai_flywheel.models import RegisterSessionRequest, RetroStatus, SessionRecord, SessionStatus
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.fs_store import ImmutableFileStore
from ai_flywheel.storage.seed import seed_demo_data


class FlywheelRuntime:
    """Single dependency container wiring all decoupled engines, adapters, MCP, and APIs."""

    def __init__(self, cfg: FlywheelConfig) -> None:
        self.cfg = cfg
        self.db = FlywheelDB(cfg.db_path)
        self.fs_store = ImmutableFileStore(cfg.receipts_dir)
        self.policies = PolicyEngine(cfg, self.db)
        self.activation = ActivationEngine(cfg, self.db, self.fs_store, self.policies)
        self.receipts = ReceiptService(self.db, self.fs_store, self.policies, self.activation)
        self.sessions = SessionAdapter(cfg, self.db)
        self.knowledge = KnowledgeSinkAdapter(cfg, self.db)
        self.delivery = DeliveryAdapter(cfg)
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
        )
        self.hooks = ThinHookProcessor(self)
        self.publication = PublicationLayer(cfg, self.db, self.policies, self.activation)
        self.mcp = build_mcp_server(self)
        # Ensure initial FTS index is synced with knowledge directory
        self.knowledge.sync_fts_index()

    @classmethod
    def create(
        cls,
        config_path: str | Path | None = None,
        base_dir: str | Path | None = None,
        auto_seed: bool = False,
    ) -> FlywheelRuntime:
        cfg = load_config(config_path=config_path, base_dir=base_dir)
        rt = cls(cfg)
        if auto_seed:
            seed_demo_data(rt.cfg, rt.db, rt.activation, rt.receipts)
        return rt

    def register_session(self, req: RegisterSessionRequest) -> SessionRecord:
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
        return self.db.upsert_session(rec)

    def update_runtime_config(self, patch: dict[str, Any]) -> FlywheelConfig:
        current = self.cfg.model_dump(mode="json")
        for section, values in patch.items():
            if section in current and isinstance(current[section], dict) and isinstance(values, dict):
                current[section].update(values)
        updated = FlywheelConfig.model_validate(current)
        updated.base_dir = self.cfg.base_dir
        updated.config_path = self.cfg.config_path
        save_config(updated)
        # Apply in-place to all holders sharing self.cfg reference
        for field_name in FlywheelConfig.model_fields:
            if field_name in {"base_dir", "config_path"}:
                continue
            setattr(self.cfg, field_name, getattr(updated, field_name))
        self.knowledge.sync_fts_index()
        return self.cfg
