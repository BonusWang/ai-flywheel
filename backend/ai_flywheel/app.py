from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings

from ai_flywheel import __version__
from ai_flywheel.core.automation import CATCHUP_CHECK_INTERVAL_SECONDS, recovery_watchdog_settings
from ai_flywheel.mcp_server import build_mcp_server
from ai_flywheel.routes import (
    build_automations_router,
    build_governance_router,
    build_overview_router,
    build_runs_router,
)
from ai_flywheel.runtime import FlywheelRuntime


def create_app(
    runtime: FlywheelRuntime | None = None,
    *,
    config_path: str | Path | None = None,
    base_dir: str | Path | None = None,
    auto_seed: bool = False,
    process_role: str | None = None,
) -> FastAPI:
    # The role decides what this process does (the recovery loop, the Feishu
    # listener). An explicitly requested role wins; without one the runtime's
    # own role decides, because the runtime was built for this process.
    effective_role = process_role or (runtime.process_role if runtime is not None else "serve")
    rt = runtime or FlywheelRuntime.create(
        config_path=config_path,
        base_dir=base_dir,
        auto_seed=auto_seed,
        process_role=effective_role,
    )
    mcp_server = build_mcp_server(rt)
    mcp_http_app = mcp_server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost", "testserver"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        ),
    )

    recovery_stop: threading.Event | None = None
    recovery_thread: threading.Thread | None = None
    archive_stop: threading.Event | None = None
    archive_thread: threading.Thread | None = None

    # ---------------- Feishu interactive layer (built in FlywheelRuntime) ----------------
    # The Feishu pieces are read from the runtime at call time, not captured
    # here: the runtime may build (or rebuild) them after the app was created.

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal recovery_stop, recovery_thread, archive_stop, archive_thread
        if effective_role == "serve":
            rt.automation.start(process_role=effective_role)
            # Recovery loop: reap runs whose executor is gone and reconcile ask cards.
            # First execution happens inside the thread so lifespan startup never blocks.
            recovery_stop = threading.Event()

            def _recovery_loop() -> None:
                interval, stale_after = recovery_watchdog_settings()
                # The lost-fire check runs on the first pass — a service started after the
                # cron time finds out at once — and then a few times an hour: the window
                # in which a fire may still be caught up is hours wide. It follows the
                # recovery pass on purpose, so a lock held by a dead run is free by then.
                last_catchup_check: float | None = None
                while True:
                    try:
                        reaped = rt.orchestrator.recover_interrupted_runs(stale_after_seconds=stale_after)
                        rt.automation.record_recovery_pass(list(reaped), error=None)
                    except Exception as exc:
                        rt.automation.record_recovery_pass([], error=str(exc))
                        logging.getLogger("ai_flywheel.recovery").exception("recovery pass failed")
                    tick = time.monotonic()
                    if (
                        last_catchup_check is None
                        or tick - last_catchup_check >= CATCHUP_CHECK_INTERVAL_SECONDS
                    ):
                        last_catchup_check = tick
                        try:
                            rt.automation.check_missed_schedule_catchup()
                        except Exception:
                            logging.getLogger("ai_flywheel.recovery").exception(
                                "schedule catch-up check failed"
                            )
                    if recovery_stop.wait(interval):
                        break

            recovery_thread = threading.Thread(target=_recovery_loop, name="flywheel-recovery", daemon=True)
            recovery_thread.start()

            # Codex strict-archive sync: a lightweight poll. It only touches the
            # disk when archive sources are configured; the stop event makes the
            # thread leave immediately on shutdown.
            archive_stop = threading.Event()

            def _archive_sync_loop() -> None:
                raw_interval = os.environ.get(
                    "FLYWHEEL_ARCHIVE_SYNC_INTERVAL",
                    str(rt.cfg.sessions.codex_archive_sync_interval_seconds),
                )
                try:
                    interval = max(1.0, float(raw_interval))
                except (TypeError, ValueError):
                    interval = 60.0
                while True:
                    try:
                        if rt.cfg.sessions.codex_archive_sources:
                            report = rt.queue.reconcile_codex_archives()
                            if any(
                                int(report.get(key) or 0) > 0
                                for key in ("enqueued", "excluded", "withdrawn")
                            ):
                                rt.broker.publish(
                                    "queue_updated",
                                    {"source": "archive_sync", **report},
                                )
                    except Exception:
                        logging.getLogger("ai_flywheel.archive_sync").exception(
                            "archive sync pass failed"
                        )
                    if archive_stop.wait(interval):
                        break

            archive_thread = threading.Thread(
                target=_archive_sync_loop, name="flywheel-archive-sync", daemon=True
            )
            archive_thread.start()
            rt.start_feishu()
        async with mcp_server.session_manager.run():
            yield
        rt.stop_feishu()
        if recovery_stop is not None:
            recovery_stop.set()
        if archive_stop is not None:
            archive_stop.set()
        if archive_thread is not None:
            archive_thread.join(timeout=10)
        rt.automation.stop()

    app = FastAPI(
        title="AI Flywheel",
        version=__version__,
        description="Evidence-driven AI collaboration flywheel portal, API, and MCP server.",
        lifespan=lifespan,
    )
    app.state.runtime = rt
    app.state.mcp = mcp_server

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Normalize /mcp -> /mcp/ so MCP HTTP clients posting directly to /mcp never hit a 307 redirect
    @app.middleware("http")
    async def normalize_mcp_path(request: Request, call_next):
        if request.scope.get("path") == "/mcp":
            request.scope["path"] = "/mcp/"
        return await call_next(request)

    # Mount official MCP Streamable HTTP at /mcp
    app.mount("/mcp", mcp_http_app)

    # ---------------- Live SSE Stream ----------------
    @app.get("/api/events")
    async def sse_events(request: Request) -> StreamingResponse:
        entry = rt.broker.subscribe()
        _, queue = entry

        async def event_stream():
            try:
                yield "event: connected\ndata: {\"status\":\"connected\"}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield msg
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                rt.broker.unsubscribe(entry)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # ---------------- Domain routers ----------------
    app.include_router(build_overview_router(rt))
    app.include_router(build_governance_router(rt))
    app.include_router(build_runs_router(rt))
    app.include_router(build_automations_router(rt))

    # ---- Built React Frontend SPA Hosting ----
    frontend_dist = (rt.cfg.base_dir / "frontend" / "dist").resolve()
    if not frontend_dist.exists():
        # Fall back to the checkout's built dist so an external --config still serves the console.
        bundled_dist = (Path(__file__).resolve().parents[2] / "frontend" / "dist").resolve()
        if bundled_dist.exists():
            frontend_dist = bundled_dist
    if frontend_dist.exists():
        assets_dir = frontend_dist / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/{full_path:path}")
        def serve_spa(full_path: str):
            if full_path.startswith(("api/", "mcp")):
                raise HTTPException(status_code=404, detail="Not Found")
            candidate = frontend_dist / full_path
            if full_path and candidate.exists() and candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(frontend_dist / "index.html"))

    return app
