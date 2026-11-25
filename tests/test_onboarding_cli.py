"""`flywheel doctor`, `flywheel init-client` and the neutral postflight hook alias.

The tests pin what the change promises:

- `doctor` is read-only, reports `name/status/summary/detail` per check, and its exit
  code follows the FAIL checks;
- `init-client` is a dry-run unless `--apply`, merges only the `ai-flywheel` entry,
  keeps a backup, writes LF, fails closed on an unreadable target and is idempotent;
- `postflight_hook_script` and the legacy `wiki_d2_postflight_script` both drive the
  post-write hook, with the neutral name winning and the env var working.
"""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from ai_flywheel.adapters.wiki.postflight import resolve_bash_executable, run_postflight
from ai_flywheel.adapters.wiki.validator import WikiGateResult
from ai_flywheel.cli import main
from ai_flywheel.config import FlywheelConfig, KnowledgeSinkConfig, load_config
from ai_flywheel.core.doctor import run_doctor
from ai_flywheel.core.onboarding import apply_init_client


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config loading must not pick up the developer's own FLYWHEEL_* overrides."""
    for name in list(os.environ):
        if name.startswith("FLYWHEEL_"):
            monkeypatch.delenv(name, raising=False)


def _snapshot(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def _cli(*args: str, base_dir: Path) -> int:
    return main(["--base-dir", str(base_dir), *args])


def _schema_wiki(tmp_path: Path) -> tuple[FlywheelConfig, Path]:
    root = tmp_path / "wiki"
    root.mkdir()
    (root / ".wiki-schema.md").write_text("---\ntype: protocol\n---\n\n# test schema\n", encoding="utf-8")
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.knowledge_sink.type = "local_markdown_wiki"
    cfg.knowledge_sink.root = str(root)
    cfg.knowledge_sink.wiki_gate_enabled = True
    cfg.knowledge_sink.wiki_gate_require_scripts = True
    return cfg, root


# ---------------------------------------------------------------- doctor


def test_doctor_reports_a_healthy_default_config_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    before = _snapshot(tmp_path)
    report = run_doctor(FlywheelConfig(base_dir=tmp_path))

    assert report["ok"] is True
    assert report["counts"]["FAIL"] == 0
    assert [check["name"] for check in report["checks"]] == [
        "config",
        "storage",
        "knowledge_sink",
        "skills",
        "runner",
        "safety_valves",
        "channels",
    ]
    for check in report["checks"]:
        assert set(check) == {"name", "status", "summary", "detail"}
        assert check["status"] in {"PASS", "WARN", "FAIL"}
        assert check["summary"]
    assert _snapshot(tmp_path) == before, "doctor must not create a data directory or a database"

    assert _cli("doctor", base_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert "OK:" in out
    assert "PASS" in out
    assert _snapshot(tmp_path) == before


def test_doctor_json_output_is_valid_json_and_follows_the_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    assert _cli("doctor", "--json", base_dir=tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert isinstance(payload["version"], str)
    assert payload["counts"]["FAIL"] == 0
    assert all("status" in check for check in payload["checks"])


def test_doctor_fails_an_api_runner_without_a_base_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    config_file = tmp_path / "flywheel.yaml"
    config_file.write_text("runner:\n  mode: api\n  api:\n    model: mock-model\n", encoding="utf-8")
    before = config_file.read_bytes()

    report = run_doctor(load_config(base_dir=tmp_path))
    runner = next(check for check in report["checks"] if check["name"] == "runner")
    assert runner["status"] == "FAIL"
    assert "base_url" in runner["summary"]
    assert report["ok"] is False

    assert _cli("doctor", "--json", base_dir=tmp_path) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert config_file.read_bytes() == before


def test_doctor_requires_a_postflight_hook_on_a_schema_governed_root(tmp_path: Path):
    cfg, _root = _schema_wiki(tmp_path)

    report = run_doctor(cfg)
    sink = next(check for check in report["checks"] if check["name"] == "knowledge_sink")
    assert sink["status"] == "FAIL"
    assert "postflight" in sink["detail"]
    assert report["ok"] is False

    hook = tmp_path / "hook.sh"
    hook.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")

    # The neutral name satisfies the requirement...
    cfg.knowledge_sink.postflight_hook_script = str(hook)
    assert run_doctor(cfg)["ok"] is True

    # ...and so does the legacy alias, unchanged.
    cfg.knowledge_sink.postflight_hook_script = ""
    cfg.knowledge_sink.wiki_d2_postflight_script = str(hook)
    assert run_doctor(cfg)["ok"] is True

    # A configured hook that does not exist is still a failure.
    cfg.knowledge_sink.postflight_hook_script = str(tmp_path / "missing-hook.sh")
    assert run_doctor(cfg)["ok"] is False


def test_doctor_notes_the_auto_approval_downgrade(tmp_path: Path):
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.workflow.approval_mode = "auto"
    cfg.safety_valves.auto_approve_writes = False

    valves = next(check for check in run_doctor(cfg)["checks"] if check["name"] == "safety_valves")
    assert valves["status"] == "WARN"
    assert "smart" in valves["summary"]
    assert "auto_approve_writes=False" in valves["detail"]

    cfg.safety_valves.auto_approve_writes = True
    valves = next(check for check in run_doctor(cfg)["checks"] if check["name"] == "safety_valves")
    assert valves["status"] == "PASS"
    assert "approval_mode=auto" in valves["summary"]


def test_doctor_flags_incomplete_interactive_feishu(tmp_path: Path):
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.delivery.feishu_cli.enabled = True
    cfg.delivery.feishu_cli.interactive = True
    cfg.delivery.feishu_cli.user_id = ""
    cfg.delivery.feishu_cli.approver_open_ids = []

    channels = next(check for check in run_doctor(cfg)["checks"] if check["name"] == "channels")
    assert channels["status"] == "FAIL"

    cfg.delivery.feishu_cli.interactive = False
    channels = next(check for check in run_doctor(cfg)["checks"] if check["name"] == "channels")
    assert channels["status"] == "PASS"
    assert "text-only" in channels["summary"]


# ---------------------------------------------------------------- init-client


def test_init_client_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target = tmp_path / "client" / "mcp.json"
    before = _snapshot(tmp_path)

    assert _cli("init-client", "claude-code", "--target", str(target), "--json", base_dir=tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["action"] == "create"
    assert payload["changed"] is False
    assert payload["backup"] is None
    assert payload["entry"] == {"type": "http", "url": "http://127.0.0.1:8765/mcp"}
    assert "ai-flywheel" in payload["entry_preview"]
    assert payload["merge"]["target_exists"] is False
    assert payload["merge"]["existing_servers"] == []
    assert not target.exists()
    assert not target.parent.exists(), "a dry-run must not create the parent directory either"
    assert _snapshot(tmp_path) == before


def test_init_client_apply_merges_json_keeps_a_backup_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    target = tmp_path / "mcp.json"
    original = {
        "theme": "dark",
        "mcpServers": {"other-server": {"command": "other", "args": ["--x"]}},
        "projects": {"/work/alpha": {"allowedTools": []}},
    }
    target.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
    command = ("init-client", "claude-code", "--target", str(target), "--apply", "--json")

    assert _cli(*command, base_dir=tmp_path) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["mode"] == "apply"
    assert first["action"] == "update"
    assert first["changed"] is True
    assert first["merge"]["target_exists"] is True
    assert first["merge"]["existing_servers"] == ["other-server"]
    assert first["merge"]["preserved_top_level_keys"] == ["projects", "theme"]
    assert first["merge"]["replaces_existing_entry"] is False

    backup = Path(first["backup"])
    assert backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8")) == original

    merged = json.loads(target.read_text(encoding="utf-8"))
    assert merged["theme"] == "dark"
    assert merged["projects"] == original["projects"]
    assert merged["mcpServers"]["other-server"] == original["mcpServers"]["other-server"]
    assert merged["mcpServers"]["ai-flywheel"] == {"type": "http", "url": "http://127.0.0.1:8765/mcp"}
    text = target.read_text(encoding="utf-8")
    assert text.count('"ai-flywheel"') == 1
    assert "\r\n" not in text

    # A second apply is a no-op: same bytes, no new backup, no duplicate entry.
    before_second = target.read_bytes()
    assert _cli(*command, base_dir=tmp_path) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["action"] == "unchanged"
    assert second["changed"] is False
    assert second["backup"] is None
    assert target.read_bytes() == before_second
    assert len(list(tmp_path.glob("mcp.json*.bak"))) == 1


def test_init_client_apply_merges_toml_revalidates_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    target = tmp_path / "config.toml"
    target.write_text(
        'model = "mock-model"\n\n'
        "[mcp_servers.other]\n"
        'command = "other"\n\n'
        "[profiles.fast]\n"
        'model = "fast"\n',
        encoding="utf-8",
    )
    command = ("init-client", "codex", "--target", str(target), "--apply", "--json")

    assert _cli(*command, base_dir=tmp_path) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["action"] == "update"
    assert first["merge"]["existing_servers"] == ["other"]
    assert first["merge"]["preserved_top_level_keys"] == ["model", "profiles"]
    assert Path(first["backup"]).is_file()

    text = target.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert parsed["model"] == "mock-model"
    assert parsed["profiles"]["fast"]["model"] == "fast"
    assert parsed["mcp_servers"]["other"]["command"] == "other"
    assert parsed["mcp_servers"]["ai-flywheel"]["url"] == "http://127.0.0.1:8765/mcp"
    assert text.count("[mcp_servers.ai-flywheel]") == 1
    assert "\r\n" not in text

    assert _cli(*command, base_dir=tmp_path) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["action"] == "unchanged"
    assert second["changed"] is False
    assert target.read_text(encoding="utf-8") == text


def test_init_client_updates_an_existing_quoted_toml_section_in_place(tmp_path: Path):
    target = tmp_path / "config.toml"
    target.write_text(
        '[mcp_servers."ai-flywheel"]\n'
        'url = "http://old-endpoint/mcp"\n\n'
        "[mcp_servers.other]\n"
        'command = "other"\n',
        encoding="utf-8",
    )
    result = apply_init_client(FlywheelConfig(base_dir=tmp_path), "codex", target=target)

    assert result["ok"] is True
    assert result["action"] == "update"
    text = target.read_text(encoding="utf-8")
    assert text.count("ai-flywheel") == 1, "the quoted section must be updated, not duplicated"
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["ai-flywheel"]["url"] == "http://127.0.0.1:8765/mcp"
    assert parsed["mcp_servers"]["other"]["command"] == "other"


@pytest.mark.parametrize(
    ("client", "filename", "content"),
    [
        ("claude-code", "broken.json", '{"mcpServers": '),
        ("codex", "broken.toml", "not = [valid\n"),
    ],
)
def test_init_client_fails_closed_on_a_malformed_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    client: str,
    filename: str,
    content: str,
):
    target = tmp_path / filename
    target.write_text(content, encoding="utf-8")
    before = target.read_bytes()

    assert _cli("init-client", client, "--target", str(target), "--apply", "--json", base_dir=tmp_path) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]
    assert target.read_bytes() == before, "a malformed target must never be rewritten"
    assert not Path(str(target) + ".bak").exists()


@pytest.mark.parametrize(
    ("client", "filename", "content"),
    [
        ("claude-code", "servers.json", '{"mcpServers": "not-an-object"}'),
        ("codex", "servers.toml", 'mcp_servers = "not-a-table"\n'),
    ],
)
def test_init_client_fails_closed_when_the_server_table_has_the_wrong_shape(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    client: str,
    filename: str,
    content: str,
):
    target = tmp_path / filename
    target.write_text(content, encoding="utf-8")
    before = target.read_bytes()

    assert _cli("init-client", client, "--target", str(target), "--apply", "--json", base_dir=tmp_path) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]
    assert target.read_bytes() == before
    assert not Path(str(target) + ".bak").exists()


def test_init_client_refuses_http_for_claude_desktop_and_defaults_to_stdio(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    target = tmp_path / "claude_desktop_config.json"

    assert _cli(
        "init-client", "claude-desktop", "--transport", "http", "--target", str(target), "--json",
        base_dir=tmp_path,
    ) == 1
    refused = json.loads(capsys.readouterr().out)
    assert refused["ok"] is False
    assert "stdio" in refused["error"]
    assert not target.exists()

    assert _cli("init-client", "claude-desktop", "--target", str(target), "--json", base_dir=tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["transport"] == "stdio"
    assert payload["entry"] == {"command": "flywheel", "args": ["mcp-stdio"]}


# ---------------------------------------------------------------- postflight hook alias


def test_effective_postflight_script_prefers_the_neutral_name():
    sink = KnowledgeSinkConfig(
        postflight_hook_script=" neutral-hook.sh ",
        wiki_d2_postflight_script="legacy-hook.sh",
    )
    assert sink.effective_postflight_script == "neutral-hook.sh"


def test_effective_postflight_script_falls_back_to_the_legacy_name():
    sink = KnowledgeSinkConfig(postflight_hook_script="   ", wiki_d2_postflight_script=" legacy-hook.sh ")
    assert sink.effective_postflight_script == "legacy-hook.sh"
    assert KnowledgeSinkConfig().effective_postflight_script == ""


def test_postflight_env_var_sets_the_neutral_hook_and_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FLYWHEEL_WIKI_D2_POSTFLIGHT", "legacy-env.sh")
    monkeypatch.setenv("FLYWHEEL_POSTFLIGHT_HOOK_SCRIPT", "neutral-env.sh")
    cfg = load_config(base_dir=tmp_path)
    assert cfg.knowledge_sink.postflight_hook_script == "neutral-env.sh"
    assert cfg.knowledge_sink.wiki_d2_postflight_script == "legacy-env.sh"
    assert cfg.knowledge_sink.effective_postflight_script == "neutral-env.sh"

    monkeypatch.delenv("FLYWHEEL_POSTFLIGHT_HOOK_SCRIPT")
    cfg = load_config(base_dir=tmp_path)
    assert cfg.knowledge_sink.effective_postflight_script == "legacy-env.sh"


def test_run_postflight_runs_the_neutral_hook_without_the_legacy_field(tmp_path: Path):
    bash = resolve_bash_executable("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("bash is required for the postflight hook test")
    root = tmp_path / "wiki"
    root.mkdir()
    hook = tmp_path / "postflight.sh"
    hook.write_text('#!/usr/bin/env bash\necho \'{"status": "ok"}\'\nexit 0\n', encoding="utf-8")

    neutral = KnowledgeSinkConfig(
        postflight_hook_script=str(hook),
        wiki_bash=bash,
        wiki_gate_require_scripts=True,
    )
    result = WikiGateResult(applicable=True, page_type="project-practice-note")
    run_postflight(result, "lessons/2026-11-25-card.md", sink=neutral, root=root, evidence_dir=tmp_path / "evidence")
    assert result.ok is True
    assert result.postflight is not None and result.postflight["status"] == "pass"

    legacy = KnowledgeSinkConfig(
        wiki_d2_postflight_script=str(hook),
        wiki_bash=bash,
        wiki_gate_require_scripts=True,
    )
    result = WikiGateResult(applicable=True, page_type="project-practice-note")
    run_postflight(result, "lessons/2026-11-25-card.md", sink=legacy, root=root, evidence_dir=tmp_path / "evidence")
    assert result.ok is True
    assert result.postflight is not None and result.postflight["status"] == "pass"


def test_run_postflight_reports_a_missing_hook_through_the_neutral_name(tmp_path: Path):
    sink = KnowledgeSinkConfig(wiki_gate_require_scripts=True)
    result = WikiGateResult(applicable=True, page_type="project-practice-note")
    run_postflight(result, "lessons/2026-11-25-card.md", sink=sink, root=tmp_path, evidence_dir=tmp_path / "evidence")

    assert result.ok is False
    assert [issue["code"] for issue in result.issues] == ["D2_SCRIPT_MISSING"]
    assert "postflight_hook_script" in result.issues[0]["message"]
