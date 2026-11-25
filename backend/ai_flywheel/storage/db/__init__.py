"""The flywheel store.

``ai_flywheel.storage.db`` stays the single import surface: the repositories live
in the modules next to this file, and everything they used to export from the
old monolith is re-exported here.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ai_flywheel.models import utc_now_iso
from ai_flywheel.storage.clock import Clock
from ai_flywheel.storage.db.asset_repo import AssetRepoMixin
from ai_flywheel.storage.db.audit_repo import AuditRepoMixin
from ai_flywheel.storage.db.dict_repo import BUILTIN_DICT_SEEDS, DictRepoMixin
from ai_flywheel.storage.db.knowledge_repo import KnowledgeRepoMixin
from ai_flywheel.storage.db.migrations import MigrationsMixin
from ai_flywheel.storage.db.queue_repo import QueueRepoMixin
from ai_flywheel.storage.db.rule_repo import RuleRepoMixin
from ai_flywheel.storage.db.schema import (
    INTERNAL_RUN_TRIGGER,
    KNOWLEDGE_FTS_COLUMNS,
    SCHEMA_SQL,
    SEVERITY_RANK,
    TERMINAL_RUN_STATUSES,
    KnowledgeQueryTerms,
    RunAlreadyFinalizedError,
    _ASCII_TERM,
    _CJK_RUN,
    _KNOWLEDGE_FTS_SQL,
    _knowledge_query_terms,
    _knowledge_snippet,
    _load_json_list,
    _load_json_map,
    build_knowledge_search_text,
    cjk_bigrams,
    normalize_root_cause_key,
    to_shanghai_date,
)
from ai_flywheel.storage.db.session_repo import SessionRepoMixin
from ai_flywheel.storage.db.workflow_repo import WorkflowRepoMixin

# Everything the old monolith exported stays importable from here.
__all__ = [
    "BUILTIN_DICT_SEEDS",
    "FlywheelDB",
    "INTERNAL_RUN_TRIGGER",
    "KNOWLEDGE_FTS_COLUMNS",
    "SCHEMA_SQL",
    "SEVERITY_RANK",
    "TERMINAL_RUN_STATUSES",
    "KnowledgeQueryTerms",
    "RunAlreadyFinalizedError",
    "_ASCII_TERM",
    "_CJK_RUN",
    "_KNOWLEDGE_FTS_SQL",
    "_knowledge_query_terms",
    "_knowledge_snippet",
    "_load_json_list",
    "_load_json_map",
    "build_knowledge_search_text",
    "cjk_bigrams",
    "normalize_root_cause_key",
    "to_shanghai_date",
]


class FlywheelDB(
    WorkflowRepoMixin,
    SessionRepoMixin,
    RuleRepoMixin,
    AssetRepoMixin,
    KnowledgeRepoMixin,
    AuditRepoMixin,
    DictRepoMixin,
    QueueRepoMixin,
    MigrationsMixin,
):
    """The flywheel store: one SQLite database, one class.

    The behaviour lives in the repository mixins next to this module; this class
    owns the connection handling (open, transaction, timestamps) and composes the
    repositories.
    """

    _lock_ttl_seconds = 3600

    def __init__(self, db_path: Path, clock: "Clock | None" = None) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._open_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _open_connection(self, *, isolation_level: str | None = "") -> sqlite3.Connection:
        """Open a connection with the pragmas every caller relies on.

        ``busy_timeout`` makes a contended writer wait for the lock instead of failing
        with "database is locked"; ``foreign_keys`` turns the declared references into
        real constraints on every connection, not only on the schema-init one.

        ``isolation_level`` defaults to ``""`` (deferred transactions): the driver opens a
        transaction before the first DML statement, so ``with self.connect() as conn:``
        rolls the block's writes back when it raises. ``None`` would put the connection
        into SQLite autocommit mode and silently disable that rollback, so only
        ``write_tx`` — which drives BEGIN IMMEDIATE / COMMIT / ROLLBACK by hand — asks
        for it explicitly.
        """
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=isolation_level,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    @contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that takes SQLite's write lock up front.

        ``BEGIN IMMEDIATE`` acquires the write lock before the first statement, so
        concurrent writers queue on ``busy_timeout`` instead of failing halfway through
        with "database is locked". The transaction is committed when the block ends and
        rolled back when it raises; a block that rolls back itself (``ROLLBACK``) is
        committed as a no-op.
        """
        conn = self._open_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            else:
                if conn.in_transaction:
                    conn.execute("COMMIT")
        finally:
            conn.close()

    @staticmethod
    def _now_or_system(now: str | None, clock: "Clock | None" = None) -> str:
        if now:
            return now
        if clock is not None:
            return clock.now_iso()
        return utc_now_iso()

    def _now_iso(self, now: str | None = None) -> str:
        return self._now_or_system(now, self._clock)

    def now_datetime(self) -> datetime:
        """The current UTC time, from the injected clock when there is one."""
        if self._clock is not None:
            return self._clock.now()
        return datetime.now(timezone.utc)
