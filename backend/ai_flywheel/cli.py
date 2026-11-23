from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

import uvicorn

from ai_flywheel.app import create_app
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.seed import seed_demo_data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flywheel", description="AI Collaboration Flywheel CLI")
    parser.add_argument("--config", default=None, help="Path to flywheel.yaml")
    parser.add_argument("--base-dir", default=None, help="Repository / working root directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Start Web Portal, REST/SSE API, and MCP Streamable HTTP server")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--seed", action="store_true", help="Seed built-in demo data if database is empty")

    sub.add_parser("mcp-stdio", help="Run MCP Server over stdio (for Claude Desktop / stdio clients)")

    p_run = sub.add_parser("run", help="Trigger one P00-P07 autonomous workflow run")
    p_run.add_argument("--phase", default="daily", choices=["daily", "weekly_exception"])
    p_run.add_argument("--auto-approve", action="store_true", help="Skip interactive approval gate for this run")

    sub.add_parser("seed", help="Populate clean built-in demo data")

    p_export = sub.add_parser("export-public", help="Export desensitized public showcase JSON snapshot")
    p_export.add_argument("--output", default="public-snapshot.json")

    p_hook = sub.add_parser("hook-forward", help="Thin fail-open hook forwarder reading stdin JSON")
    p_hook.add_argument("client", help="Client name, e.g. codex or claude-code")
    p_hook.add_argument("--url", default="http://127.0.0.1:8765", help="Base URL of running ai-flywheel server")

    args = parser.parse_args(argv)

    if args.command == "hook-forward":
        raw = sys.stdin.read()
        endpoint = f"{args.url.rstrip('/')}/api/hooks/{args.client}"
        try:
            req = urllib.request.Request(
                endpoint,
                data=raw.encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                sys.stdout.write(resp.read().decode("utf-8"))
        except Exception:
            sys.stdout.write('{"continue": true}')
        return 0

    if args.command == "serve":
        rt = FlywheelRuntime.create(
            config_path=args.config,
            base_dir=args.base_dir,
            auto_seed=args.seed,
        )
        app = create_app(runtime=rt)
        host = args.host or rt.cfg.server.host
        port = args.port or rt.cfg.server.port
        print(f"AI Flywheel running at http://{host}:{port}  (MCP HTTP: http://{host}:{port}/mcp)")
        uvicorn.run(app, host=host, port=port)
        return 0

    if args.command == "mcp-stdio":
        rt = FlywheelRuntime.create(
            config_path=args.config,
            base_dir=args.base_dir,
            skip_fts_sync=True,
        )
        rt.mcp.run(transport="stdio")
        return 0

    if args.command == "seed":
        rt = FlywheelRuntime.create(config_path=args.config, base_dir=args.base_dir)
        res = seed_demo_data(rt.cfg, rt.db, rt.activation, rt.receipts)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.command == "run":
        rt = FlywheelRuntime.create(config_path=args.config, base_dir=args.base_dir)
        run = rt.orchestrator.start_run(
            trigger="cli",
            phase=args.phase,
            auto_approve_override=True if args.auto_approve else None,
        )
        print(json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0 if run.status.value in {"PASS", "AWAITING_APPROVAL", "PARTIAL"} else 1

    if args.command == "export-public":
        rt = FlywheelRuntime.create(config_path=args.config, base_dir=args.base_dir)
        snap = rt.publication.export_public_snapshot()
        out_path = Path(args.output).resolve()
        out_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Exported sanitized public snapshot to {out_path}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
