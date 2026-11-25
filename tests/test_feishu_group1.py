"""feat-feishu-approval-card-loop 第 1 组测试：配置、lark-cli 调用层、自检、监听分类。

进程级调用用假 runner / 假 PATH 目录；不启动真实 lark-cli（event consume 会创建服务端订阅）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from ai_flywheel.adapters.delivery import DeliveryAdapter
from ai_flywheel.adapters.lark_cli import LarkCliRunner, LarkCliUnavailableError
from ai_flywheel.config import FeishuCliConfig, FlywheelConfig
from ai_flywheel.core.feishu_check import CHECK_SETTING_KEY, FeishuCheckService, event_card_id, render_check_card
from ai_flywheel.core.feishu_listen import classify_failure
from ai_flywheel.storage.db import FlywheelDB


# ---------------- 1.1 配置 ----------------

def test_effective_approver_ids_dedup_and_order():
    cfg = FeishuCliConfig(approver_open_ids=[" ou_b ", "", "ou_a", "ou_a"], user_id="ou_a")
    assert cfg.effective_approver_ids == ["ou_b", "ou_a"]


def test_interaction_ready_conditions():
    assert FeishuCliConfig().interactive is False  # 默认关闭
    ok = FeishuCliConfig(enabled=True, interactive=True, user_id="ou_x", approver_open_ids=["ou_y"])
    assert ok.interaction_ready is True
    assert FeishuCliConfig(enabled=True, interactive=True, user_id="ou_x").interaction_ready is True  # user_id 兜底
    assert FeishuCliConfig(enabled=True, interactive=True).interaction_ready is False  # 无审批人
    assert FeishuCliConfig(enabled=True, interactive=True, user_id="ou_x", identity="user").interaction_ready is False
    assert FeishuCliConfig(enabled=True, interactive=True, approver_open_ids=["ou_y"]).interaction_ready is False  # 群聊


# ---------------- 1.2 调用层解析 ----------------

@pytest.fixture()
def fake_path(tmp_path: Path, monkeypatch):
    (tmp_path / "bin").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.chdir(tmp_path)  # 当前目录里放假文件，验证解析不搜当前目录
    return tmp_path


def test_resolve_direct_exe(fake_path: Path):
    exe = fake_path / "bin" / "lark-cli.exe"
    exe.write_bytes(b"MZ")
    prefix, kind = LarkCliRunner.resolve("lark-cli")
    assert kind == "exe"
    assert prefix == [str(exe)]


def test_resolve_shim_to_native(fake_path: Path):
    pkg = fake_path / "bin" / "node_modules" / "@larksuite" / "cli"
    (pkg / "scripts").mkdir(parents=True)
    (pkg / "scripts" / "run.js").write_text("// entry")
    (pkg / "bin").mkdir(parents=True)
    native = pkg / "bin" / "lark-cli.exe"
    native.write_bytes(b"MZ")
    shim = fake_path / "bin" / "lark-cli.cmd"
    shim.write_text('@echo off\r\n"%~dp0%\\node_modules\\@larksuite\\cli\\scripts\\run.js" %*\r\n', encoding="utf-8")
    prefix, kind = LarkCliRunner.resolve("lark-cli")
    assert kind == "exe"
    assert prefix == [str(native)]


def test_resolve_shim_falls_back_to_node(fake_path: Path):
    pkg = fake_path / "bin" / "node_modules" / "@larksuite" / "cli"
    (pkg / "scripts").mkdir(parents=True)
    entry = pkg / "scripts" / "run.js"
    entry.write_text("// entry")
    (fake_path / "bin" / "lark-cli.cmd").write_text(
        '@echo off\r\n"%~dp0%\\node_modules\\@larksuite\\cli\\scripts\\run.js" %*\r\n', encoding="utf-8"
    )
    node_dir = fake_path / "nodedir"
    node_dir.mkdir()
    node = node_dir / "node.exe"
    node.write_bytes(b"MZ")
    import os

    old = os.environ["PATH"]
    os.environ["PATH"] = f"{fake_path / 'bin'}{os.pathsep}{node_dir}"
    try:
        prefix, kind = LarkCliRunner.resolve("lark-cli")
        assert kind == "node"
        assert prefix == [str(node), str(entry)]
    finally:
        os.environ["PATH"] = old


def test_resolve_never_searches_cwd(fake_path: Path):
    (fake_path / "lark-cli.exe").write_bytes(b"MZ")  # 当前目录里的假 exe，不在 PATH 上
    with pytest.raises(LarkCliUnavailableError):
        LarkCliRunner.resolve("lark-cli")


def test_resolve_shim_entry_escape_rejected(fake_path: Path):
    (fake_path / "bin" / "lark-cli.cmd").write_text(
        '@echo off\r\n"%~dp0%\\..\\evil\\run.js" %*\r\n', encoding="utf-8"
    )
    with pytest.raises(LarkCliUnavailableError):
        LarkCliRunner.resolve("lark-cli")


def test_resolve_unparseable_shim_never_falls_back_to_it(fake_path: Path):
    (fake_path / "bin" / "lark-cli.cmd").write_text("@echo off\r\nweird\r\n", encoding="utf-8")
    with pytest.raises(LarkCliUnavailableError):
        LarkCliRunner.resolve("lark-cli")


# ---------------- 1.2 信封解析与 argv ----------------

class FakeProcRunner(LarkCliRunner):
    """Overrides run() so no subprocess is spawned; records argv and returns canned output."""

    def __init__(self, responses: list[tuple[int, str, str]]):
        super().__init__(command=self._stub_command())
        self.calls: list[tuple[list[str], bytes | None]] = []
        self._responses = list(responses)

    @staticmethod
    def _stub_command() -> str:
        import sys

        return sys.executable  # 任意存在的可执行文件，绕过解析

    def run(self, args, *, input_bytes=None, timeout=20.0):
        self.calls.append((list(args), input_bytes))
        return self._responses.pop(0) if self._responses else (0, "{}", "")


def test_send_interactive_argv_and_envelope():
    r = FakeProcRunner([(0, json.dumps({"ok": True, "data": {"message_id": "om_1"}}), "")])
    res = r.send_interactive(user_id="ou_x", card_json='{"schema":"2.0"}', idempotency_key="k" * 80)
    argv = r.calls[0][0]
    assert argv[argv.index("--msg-type") + 1] == "interactive"
    assert argv[argv.index("--user-id") + 1] == "ou_x"
    assert argv[argv.index("--idempotency-key") + 1] == "k" * 50  # 50 字符截断
    assert res.status == "sent" and res.message_id == "om_1"


def test_envelope_error_and_unknown_and_garbage():
    r = FakeProcRunner([(0, json.dumps({"ok": False, "error": {"type": "x"}}), ""), (-1, "", "timeout"), (0, "not-json", "")])
    assert r.send_interactive(user_id="u", card_json="{}", idempotency_key="k").status == "failed"
    assert r.send_interactive(user_id="u", card_json="{}", idempotency_key="k").status == "unknown"
    assert r.send_interactive(user_id="u", card_json="{}", idempotency_key="k").status == "failed"


def test_patch_card_uses_api_patch_with_stdin_body():
    r = FakeProcRunner([(0, '{"ok": true, "data": {}}', "")])
    res = r.patch_card(message_id="om_9", card_json='{"schema":"2.0"}')
    argv, body = r.calls[0]
    assert argv[:3] == ["api", "PATCH", "/open-apis/im/v1/messages/om_9"]
    assert argv[argv.index("--data") + 1] == "-"
    assert json.loads(body.decode("utf-8")) == {"content": '{"schema":"2.0"}'}
    assert res.status == "sent"


def test_version_check_below_minimum():
    r = FakeProcRunner([(0, "lark-cli version 1.0.58\n", "")])
    with pytest.raises(LarkCliUnavailableError):
        r.check_version()


# ---------------- 1.3 DeliveryAdapter 收口 ----------------

def _cfg(**feishu) -> FlywheelConfig:
    cfg = FlywheelConfig()
    cfg.delivery.feishu_cli = FeishuCliConfig(**feishu)
    return cfg


def test_delivery_skips_and_statuses():
    ad = DeliveryAdapter(_cfg(enabled=False))
    assert ad.send_feishu_card(title="t", markdown="m")["status"] == "SKIPPED"
    ad = DeliveryAdapter(_cfg(enabled=True, user_id="ou_x", notify_on_fyi=False))
    assert ad.send_feishu_card(title="t", markdown="m", kind="fyi")["status"] == "SKIPPED"
    assert "no user_id" in DeliveryAdapter(_cfg(enabled=True)).send_feishu_card(title="t", markdown="m")["reason"]

    runner = FakeProcRunner([(0, json.dumps({"ok": True, "data": {"message_id": "om_2"}}), "")])
    ad = DeliveryAdapter(_cfg(enabled=True, user_id="ou_x"), lark_runner=runner)
    out = ad.send_feishu_card(title="标题", markdown="正文", idempotency_key="flywheel-ask-run-x")
    assert out["status"] == "sent" and out["external_ref"] == "om_2"
    argv = runner.calls[0][0]
    assert argv[argv.index("--msg-type") + 1] == "text"
    content = json.loads(argv[argv.index("--content") + 1])
    assert content["text"].startswith("【标题】")


# ---------------- 1.5 自检 ----------------

class FakeCheckRunner:
    def __init__(self, *, send="sent", patch="sent", version="1.0.68", auth=True, target="C:/fake/lark-cli.exe"):
        self.send_status, self.patch_status = send, patch
        self._version, self._auth, self._target = version, auth, target
        self.patches: list[str] = []
        self.sends: list[str] = []

    @property
    def target_path(self):
        return self._target

    def version(self):
        return self._version

    def check_version(self):
        if tuple(int(x) for x in self._version.split(".")) < (1, 0, 64):
            raise LarkCliUnavailableError("too old")

    def auth_bot_ready(self):
        return self._auth

    def send_interactive(self, *, user_id, card_json, idempotency_key):
        self.sends.append(card_json)
        from ai_flywheel.adapters.lark_cli import LarkResult

        return LarkResult(self.send_status, message_id="om_check" if self.send_status == "sent" else None, error=None if self.send_status == "sent" else "boom")

    def patch_card(self, *, message_id, card_json):
        self.patches.append(card_json)
        from ai_flywheel.adapters.lark_cli import LarkResult

        return LarkResult(self.patch_status, error=None if self.patch_status == "sent" else "patch boom")


def _check_service(tmp_path: Path, runner, *, listener_state=None, approvers=("ou_me",), user_id="ou_me", interactive=True):
    cfg = FlywheelConfig()
    cfg.delivery.feishu_cli = FeishuCliConfig(
        enabled=True, interactive=interactive, user_id=user_id, approver_open_ids=list(approvers)
    )
    db = FlywheelDB(tmp_path / "check.db")
    svc = FeishuCheckService(cfg, runner, listener_state or {"state": "ready", "reason": "", "ready": True}, db=db)
    return svc, db


def test_check_success_flow(tmp_path: Path):
    runner = FakeCheckRunner()
    svc, db = _check_service(tmp_path, runner)
    result: dict = {}

    def _run():
        result.update(svc.run(wait_seconds=10))

    t = threading.Thread(target=_run)
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline and not runner.sends:
        time.sleep(0.05)
    assert runner.sends, "test card was sent"
    clicked = False
    while time.time() < deadline and t.is_alive():
        if svc.handle_click({"event_id": "e1", "operator_id": "ou_me", "action_value": {"card": svc._current_card}}):
            clicked = True
            break
        time.sleep(0.05)
    t.join(timeout=15)
    assert clicked
    assert result["state"] == "passed"
    assert result["operator"] == "ou_me"
    # 终卡不再带按钮，且结果可查询为有效
    assert all('"behaviors"' not in p for p in runner.patches[1:])
    status = svc.status()
    assert status["valid"] is True and status["state"] == "passed"
    with db.connect() as conn:
        row = conn.execute("SELECT kind, state FROM decision_cards WHERE card_id = ?", (result["card_id"],)).fetchone()
    assert row["kind"] == "fcheck" and row["state"] == "closed"


def test_check_non_approver_click(tmp_path: Path):
    runner = FakeCheckRunner()
    svc, _ = _check_service(tmp_path, runner)
    result: dict = {}

    def _run():
        result.update(svc.run(wait_seconds=8))

    t = threading.Thread(target=_run)
    t.start()
    deadline = time.time() + 8
    while time.time() < deadline and not runner.sends:
        time.sleep(0.05)
    while time.time() < deadline and t.is_alive():
        if svc.handle_click({"event_id": "e2", "operator_id": "ou_stranger", "action_value": {"card": svc._current_card}}):
            break
        time.sleep(0.05)
    t.join(timeout=12)
    assert result["state"] == "failed"
    assert "approver" in result["reason"]


def test_check_timeout_hints_subscription(tmp_path: Path):
    runner = FakeCheckRunner()
    svc, _ = _check_service(tmp_path, runner)
    result = svc.run(wait_seconds=0.3)
    assert result["state"] == "failed"
    assert "card-callback subscription" in result["reason"]
    assert any('"自检超时' in p or "自检超时" in p for p in runner.patches)


def test_check_gates(tmp_path: Path):
    # 无审批人
    svc, _ = _check_service(tmp_path, FakeCheckRunner(), approvers=(), user_id="")
    assert "no approvers" in svc.run()["reason"]
    # 版本过低
    svc, _ = _check_service(tmp_path, FakeCheckRunner(version="1.0.58"))
    assert "version" in svc.run()["reason"]
    # bot 身份未就绪
    svc, _ = _check_service(tmp_path, FakeCheckRunner(auth=False))
    assert "bot identity" in svc.run()["reason"]
    # 监听未就绪
    svc, _ = _check_service(tmp_path, FakeCheckRunner(), listener_state={"state": "backoff", "reason": "x", "ready": False})
    assert "listener not ready" in svc.run()["reason"]


def test_check_fingerprint_order_insensitive_and_startup_cleanup(tmp_path: Path):
    runner = FakeCheckRunner()
    svc, db = _check_service(tmp_path, runner, approvers=("ou_a", "ou_b"))
    fp = svc.fingerprint()
    assert fp["approvers"] == ["ou_a", "ou_b", "ou_me"]
    cfg2 = FlywheelConfig()
    cfg2.delivery.feishu_cli = FeishuCliConfig(enabled=True, interactive=True, user_id="ou_me", approver_open_ids=["ou_b", "ou_a", "ou_a"])
    svc2 = FeishuCheckService(cfg2, runner, {"state": "ready", "ready": True}, db=db)
    assert svc2.fingerprint() == fp  # 顺序与重复不影响
    # 遗留未完成自检：启动收口改卡并标记失败
    db.set_system_setting(CHECK_SETTING_KEY, "feishu", {"state": "running", "card_id": "fcheck-old", "message_id": "om_old"})
    svc2.startup_cleanup()
    assert any("自检超时" in p or "失效" in p for p in runner.patches)
    stored = svc2._read_setting()
    assert stored["state"] == "failed" and "interrupted" in stored["reason"]


def test_event_card_id_parsing():
    assert event_card_id({"action_value": {"card": "fcheck-x"}}) == "fcheck-x"
    assert event_card_id({"action_value": json.dumps({"card": "fcheck-y"})}) == "fcheck-y"
    assert event_card_id({"action_value": "junk"}) is None
    assert event_card_id({}) is None


# ---------------- 1.4 监听分类与去重 ----------------

def test_classify_failure():
    assert classify_failure("another consumer (pid 4) is already running", 2) == "occupied"
    assert classify_failure("auth failure: no token", 3) == "permanent"
    assert classify_failure("connection reset", 4) == "transient"


def test_listener_dedups_duplicate_event_lines():
    from ai_flywheel.core.feishu_listen import FeishuClickListener

    seen: list[dict] = []
    listener = FeishuClickListener.__new__(FeishuClickListener)
    listener._on_click = seen.append
    listener._seen_event_ids = set()
    line = json.dumps({"event_id": "ev-1", "operator_id": "ou_me"})
    listener._handle_line(line)
    listener._handle_line(line)
    listener._handle_line(json.dumps({"event_id": "ev-2", "operator_id": "ou_me"}))
    assert len(seen) == 2
    listener._handle_line("not json")
    assert len(seen) == 2


# ---------------- 监听随交互模式启停 ----------------

import subprocess as _sp


class _FakePipe:
    def __init__(self, proc=None):
        self._proc = proc
        self.closed = False
        self.lines: list[bytes] = []

    def readline(self, size=-1):
        while not self.closed:
            if self.lines:
                return self.lines.pop(0)
            time.sleep(0.01)
        return b""

    def close(self):
        self.closed = True
        if self._proc is not None:
            self._proc.returncode = 0  # stdin EOF = graceful exit


class _FakeProc:
    def __init__(self):
        self.pid = 4242
        self.returncode = None
        self.stdin = _FakePipe(self)
        self.stdout = _FakePipe()
        self.stderr = _FakePipe()
        self.stderr.lines.append(b"[event] ready event_key=card.action.trigger\n")

    def wait(self, timeout=None):
        deadline = time.time() + (timeout if timeout else 1e9)
        while self.returncode is None:
            if time.time() > deadline:
                raise _sp.TimeoutExpired("fake", timeout)
            time.sleep(0.01)
        return self.returncode


class _FakeListenerRunner:
    def __init__(self):
        self.spawned = 0

    def popen(self, args):
        self.spawned += 1
        return _FakeProc()

    def consume_args(self):
        return ["event", "consume", "card.action.trigger", "--as", "bot"]


def test_listener_idle_when_interactive_off():
    from ai_flywheel.core.feishu_listen import FeishuClickListener

    runner = _FakeListenerRunner()
    listener = FeishuClickListener(runner, on_click=lambda e: None, should_run=lambda: False)
    listener.start()
    time.sleep(0.6)
    assert runner.spawned == 0  # 交互模式关闭：绝不起消费者，不占订阅
    assert listener.state["state"] == "stopped" and "interactive mode off" in listener.state["reason"]
    listener.stop()


def test_listener_stops_consumer_when_interactive_turned_off():
    from ai_flywheel.core.feishu_listen import FeishuClickListener

    flag = {"on": True}
    runner = _FakeListenerRunner()
    listener = FeishuClickListener(
        runner, on_click=lambda e: None, should_run=lambda: flag["on"]
    )
    listener.config_poll_seconds = 0.1
    listener.start()
    deadline = time.time() + 5
    while time.time() < deadline and listener.state["state"] != "ready":
        time.sleep(0.05)
    assert listener.state["state"] == "ready" and runner.spawned == 1
    flag["on"] = False  # 运行中关闭交互模式
    deadline = time.time() + 5
    while time.time() < deadline and listener.state["state"] != "stopped":
        time.sleep(0.05)
    assert listener.state["state"] == "stopped" and "interactive mode off" in listener.state["reason"]
    time.sleep(0.4)
    assert runner.spawned == 1  # 不再重拉
    listener.stop()


def test_listener_startup_audit_records_exe_version_hash(tmp_path: Path):
    import shutil
    import sys

    from ai_flywheel.config import load_config
    from ai_flywheel.runtime import FlywheelRuntime

    shutil.copytree(Path(__file__).resolve().parent.parent / "profile", tmp_path / "profile")
    cfg = load_config(base_dir=tmp_path)
    # interactive off: the listener stays idle (no consumer is spawned), but the startup audit is still written
    cfg.delivery.feishu_cli = FeishuCliConfig(enabled=True, user_id="ou_x", command=sys.executable)
    rt = FlywheelRuntime(cfg, process_role="serve")
    try:
        rt.start_feishu()
    finally:
        rt.stop_feishu()
    events = rt.db.list_audit_events(entity_type="decision_card", entity_id="feishu_listener")
    started = [e for e in events if e["event_type"] == "listener_started"]
    assert started, events
    assert sys.executable in started[0]["summary"] and "sha256=" in started[0]["summary"]
    assert len(started[0]["payload"].get("sha256", "")) == 64


# ---------------- W0.4：永久失败不再空转、自检指纹缓存与 running 状态 ----------------

class _DrainPipe:
    """A pipe that hands over its lines and then reports EOF (no blocking read)."""

    def __init__(self, lines=()):
        self.lines = list(lines)
        self.closed = False

    def readline(self, size=-1):
        if self.lines:
            return self.lines.pop(0)
        return b""

    def close(self):
        self.closed = True


class _AuthFailProc:
    """A consumer that dies with an auth error and never prints the ready marker."""

    def __init__(self):
        self.pid = 4243
        self.returncode = 3
        self.stdin = _DrainPipe()
        self.stdout = _DrainPipe()
        self.stderr = _DrainPipe([b"auth failure: no token\n"])

    def wait(self, timeout=None):
        return self.returncode


class _AuthFailRunner:
    def __init__(self):
        self.spawned = 0

    def popen(self, args):
        self.spawned += 1
        return _AuthFailProc()

    def consume_args(self):
        return ["event", "consume", "card.action.trigger", "--as", "bot"]


def test_listener_does_not_spin_after_the_permanent_failure_limit():
    """达到永久失败上限后不再空转重拉消费者，而是停下来等交互模式重新打开。"""
    from ai_flywheel.core.feishu_listen import FeishuClickListener

    runner = _AuthFailRunner()
    listener = FeishuClickListener(
        runner,
        on_click=lambda e: None,
        should_run=lambda: True,
        max_permanent_failures=2,
        permanent_backoff=0.05,
        transient_backoff=0.05,
        ready_timeout=0.2,
    )
    listener.start()
    deadline = time.time() + 10
    while time.time() < deadline and runner.spawned < 2:
        time.sleep(0.05)
    time.sleep(1.0)
    try:
        assert runner.spawned == 2, "达到上限后不得继续重拉消费者"
        assert listener.state["state"] == "stopped"
        assert "permanent failure" in listener.state["reason"]
    finally:
        listener.stop()


def test_listener_resumes_after_interactive_mode_is_toggled():
    """交互模式关闭再打开后，监听器恢复并重置永久失败计数。"""
    from ai_flywheel.core.feishu_listen import FeishuClickListener

    flag = {"on": True}
    runner = _AuthFailRunner()
    listener = FeishuClickListener(
        runner,
        on_click=lambda e: None,
        should_run=lambda: flag["on"],
        max_permanent_failures=1,
        permanent_backoff=0.05,
        transient_backoff=0.05,
        ready_timeout=0.2,
    )
    listener.start()
    deadline = time.time() + 10
    while time.time() < deadline and runner.spawned < 1:
        time.sleep(0.05)
    time.sleep(0.5)
    assert listener.state["state"] == "stopped"
    assert listener._consecutive >= 1
    try:
        flag["on"] = False
        time.sleep(1.2)
        flag["on"] = True
        deadline = time.time() + 10
        while time.time() < deadline and runner.spawned < 2:
            time.sleep(0.05)
        assert runner.spawned >= 2
        assert listener._consecutive == 0
    finally:
        listener.stop()


def test_check_fingerprint_is_cached_until_the_setup_changes(tmp_path: Path):
    runner = FakeCheckRunner()
    svc, _ = _check_service(tmp_path, runner)
    calls = {"version": 0}
    original = runner.version

    def _counting():
        calls["version"] += 1
        return original()

    runner.version = _counting  # type: ignore[method-assign]

    first = svc.fingerprint()
    second = svc.fingerprint()
    assert first == second
    assert calls["version"] == 1  # 第二次命中缓存

    # 审批人集合变化后指纹必须重算
    svc.cfg.delivery.feishu_cli.approver_open_ids = ["ou_other"]
    third = svc.fingerprint()
    assert third != first
    assert calls["version"] == 2


def test_check_persists_the_running_state_for_startup_cleanup(tmp_path: Path):
    runner = FakeCheckRunner()
    svc, db = _check_service(tmp_path, runner)
    result: dict = {}

    def _run():
        result.update(svc.run(wait_seconds=8))

    t = threading.Thread(target=_run)
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline and not runner.sends:
        time.sleep(0.05)
    try:
        stored = svc._read_setting()
        assert stored["state"] == "running"
        assert stored.get("message_id") == "om_check"
        # 中断的自检在下次启动时能被认出来并收口
        svc.startup_cleanup()
        cleaned = svc._read_setting()
        assert cleaned["state"] == "failed" and "interrupted" in cleaned["reason"]
    finally:
        t.join(timeout=15)


def test_check_source_has_no_dead_branch():
    source = (Path(__file__).resolve().parent.parent / "backend" / "ai_flywheel" / "adapters" / "channels" / "feishu" / "check.py").read_text(
        encoding="utf-8"
    )
    assert "if False" not in source
