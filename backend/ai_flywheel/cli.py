from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from ai_flywheel.app import create_app
from ai_flywheel.config import load_config
from ai_flywheel.core.doctor import PASS, run_doctor
from ai_flywheel.core.onboarding import (
    SUPPORTED_CLIENTS,
    apply_init_client,
    plan_init_client,
)
from ai_flywheel.mcp_server import build_mcp_server
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
    p_run.add_argument(
        "--phase",
        default="daily",
        choices=["auto", "daily", "weekly", "both"],
        help="daily | weekly | both, or auto (daily, plus the weekly review on Friday)",
    )
    p_run.add_argument("--auto-approve", action="store_true", help="Skip interactive approval gate for this run")

    sub.add_parser("seed", help="Populate clean built-in demo data")

    p_export = sub.add_parser("export-public", help="Export desensitized public showcase JSON snapshot")
    p_export.add_argument("--output", default="public-snapshot.json")

    p_hook = sub.add_parser("hook-forward", help="Thin fail-open hook forwarder reading stdin JSON")
    p_hook.add_argument("client", help="Client name, e.g. codex or claude-code")
    p_hook.add_argument("--url", default="http://127.0.0.1:8765", help="Base URL of running ai-flywheel server")

    p_fcheck = sub.add_parser("feishu-check", help="Verify the Feishu interactive approval link via a running serve")
    p_fcheck.add_argument("--url", default="http://127.0.0.1:8765", help="Base URL of the running ai-flywheel serve")
    p_fcheck.add_argument("--wait", type=int, default=120, help="Seconds the server waits for your click (long mode: 600)")

    p_doctor = sub.add_parser(
        "doctor",
        help="Read-only diagnostics: is this installation able to run as configured?",
    )
    p_doctor.add_argument("--json", action="store_true", help="Print the full report as JSON")

    p_init = sub.add_parser(
        "init-client",
        help="Render (dry-run) or merge the ai-flywheel MCP entry into a client configuration",
    )
    p_init.add_argument("client", choices=list(SUPPORTED_CLIENTS), help="Which MCP client to configure")
    p_init.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default=None,
        help="MCP transport (default: the client's native one)",
    )
    p_init.add_argument(
        "--apply",
        action="store_true",
        help="Write the merged configuration (a .bak backup is kept); default is a dry-run",
    )
    p_init.add_argument("--target", default=None, help="Explicit configuration file path")
    p_init.add_argument("--json", action="store_true", help="Print the result as JSON")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        try:
            cfg = load_config(config_path=args.config, base_dir=args.base_dir)
        except Exception as exc:
            print(f"cannot load configuration: {exc}", file=sys.stderr)
            return 1
        report = run_doctor(cfg)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"AI Flywheel doctor · v{report['version']}")
            print(f"{'STATUS':<7} {'CHECK':<15} SUMMARY")
            for check in report["checks"]:
                print(f"{check['status']:<7} {check['name']:<15} {check['summary']}")
                if check["status"] != PASS:
                    print(f"{'':<7} {'':<15} {check['detail']}")
            counts = report["counts"]
            verdict = "OK" if report["ok"] else "FAILED"
            print(
                f"{verdict}: {counts['PASS']} passed, {counts['WARN']} warning(s), "
                f"{counts['FAIL']} failure(s)"
            )
        return 0 if report["ok"] else 1

    if args.command == "init-client":
        try:
            cfg = load_config(config_path=args.config, base_dir=args.base_dir)
        except Exception as exc:
            print(f"cannot load configuration: {exc}", file=sys.stderr)
            return 2
        run = apply_init_client if args.apply else plan_init_client
        result = run(cfg, args.client, transport=args.transport, target=args.target)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif not result["ok"]:
            print(f"init-client failed: {result['error']}", file=sys.stderr)
        else:
            print(f"client:    {result['client']} ({result['label']})")
            print(f"transport: {result['transport']}")
            print(f"target:    {result['target']}")
            print(f"format:    {result['format']}")
            print(f"mode:      {result['mode']}")
            print(f"action:    {result['action']}")
            print(f"entry:     {json.dumps(result['entry'], ensure_ascii=False)}")
            merge = result["merge"]
            print(
                f"merge:     target_exists={merge['target_exists']} "
                f"existing_servers={len(merge['existing_servers'])} "
                f"replaces_entry={merge['replaces_existing_entry']}"
            )
            if result["backup"]:
                print(f"backup:    {result['backup']}")
            print("entry preview:")
            print(result["entry_preview"], end="" if result["entry_preview"].endswith("\n") else "\n")
        return 0 if result["ok"] else 1

    if args.command == "feishu-check":
        base = args.url.rstrip("/")
        req = urllib.request.Request(
            f"{base}/api/site/feishu-check",
            data=json.dumps({"wait_seconds": args.wait}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                started = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            print(f"feishu-check failed to start: HTTP {exc.code} {detail[:300]}")
            return 1
        print(f"self-check started (wait {started.get('wait_seconds')}s); a test card is on its way — please tap it.")
        deadline = time.time() + args.wait + 90
        last: dict = {}
        while time.time() < deadline:
            time.sleep(2)
            try:
                with urllib.request.urlopen(f"{base}/api/site/feishu-check", timeout=10) as resp:
                    last = json.loads(resp.read().decode("utf-8"))
            except Exception:
                continue
            if last.get("running"):
                continue
            break
        state = last.get("state", "unknown")
        print(json.dumps(last, ensure_ascii=False, indent=2)[:1200])
        if state == "passed" and last.get("valid"):
            print("Feishu interactive link verified.")
            return 0
        print(f"Feishu self-check did not pass (state={state}).")
        return 1

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
            process_role="serve",
        )
        app = create_app(runtime=rt)
        host = args.host or rt.cfg.server.host
        port = args.port or rt.cfg.server.port
        print(f"AI Flywheel running at http://{host}:{port}  (MCP HTTP: http://{host}:{port}/mcp)")
        # Grace window lets the lifespan shutdown (listener/recovery threads) actually run
        # instead of waiting forever on open SSE connections; a Ctrl+C does not interrupt
        # a synchronous approve request — that still needs a hard kill.
        uvicorn.run(app, host=host, port=port, timeout_graceful_shutdown=10)
        return 0

    if args.command == "mcp-stdio":
        rt = FlywheelRuntime.create(
            config_path=args.config,
            base_dir=args.base_dir,
            skip_fts_sync=True,
        )
        build_mcp_server(rt).run(transport="stdio")
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
        return 0 if run.status.value in {"PASS", "AWAITING_APPROVAL", "PARTIAL", "CANCELLED"} else 1

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
