"""feat-agy-ingest-and-high-fidelity-distill：Antigravity 原生 transcript 的解析与静默窗口采集。

覆盖：
1. 步态级解析：`USER_INPUT` 只留请求正文、`PLANNER_RESPONSE` 只留回答，工具块与系统步态严格剥离；
2. 身份与标题：会话身份归一化为 `antigravity:<uuid>`，标题取第一条有效请求；
3. 工作区：`Active Document` / 工作区声明与请求里写下的本地路径，未命中留空；
4. 静默窗口：仍在写入的 transcript 不登记、不入队，静默之后由后续扫描补上；显式入队不受限；
5. 兼容联调：Codex / Claude Code 解析不变，`read_full_transcript` → `prepare_queue_chunks` 可用；
6. 容错：合法却不是一条记录的行（`null` / `[]` / 数字 / 截断的半行）被三个读取器一致跳过；
7. 续聊：同一个 UUID 的 transcript 在消费之后继续增长，再次静默后只把新增的轮次补进队列，
   会话既有的 opt_out / hand_distilled 意图不变。

会话、项目、技能与模型名全部是虚构示例；路径全部由 pytest 的临时目录派生。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest

from ai_flywheel.adapters.sessions import SessionAdapter, _extract_jsonl_text, read_transcript_excerpt
from ai_flywheel.config import FlywheelConfig, ScanSourceConfig, SessionTriageConfig
from ai_flywheel.core.queue_service import (
    HAND_DISTILLED_SKIP,
    MAX_CHUNKS_PER_SESSION,
    QUIET_WINDOW_ACTIVE,
    prepare_queue_chunks,
    read_full_transcript,
    split_dialogue_turns,
)
from ai_flywheel.core.triage import (
    DEFAULT_SCAN_QUIET_WINDOW_SECONDS,
    antigravity_session_id,
    antigravity_workspace,
    derive_readable_title,
    extract_jsonl_metadata,
    is_antigravity_client,
    is_antigravity_record,
    scan_quiet_window_seconds,
    transcript_is_active,
    triage_session,
)
from ai_flywheel.models import (
    IngressChannel,
    QueueStatus,
    RunStatus,
    SessionRecord,
    SessionStatus,
    WorkflowRunRecord,
)
from ai_flywheel.runtime import FlywheelRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]

AGY_UUID = "3c9f5a12-6b4e-4d7a-8f21-9e0c7d5b4a01"
AGY_UUID_QUIET = "3c9f5a12-6b4e-4d7a-8f21-9e0c7d5b4a02"
AGY_UUID_ACTIVE = "3c9f5a12-6b4e-4d7a-8f21-9e0c7d5b4a03"
CODEX_UUID = "7a1c2e90-4b3d-4f6a-9c8e-1d2f3a4b5c01"
CLAUDE_UUID = "9e8d7c6b-5a4f-4e3d-2c1b-0a9f8e7d6c02"
CLAUDE_TITLE = "部署报错排查"

#: Antigravity 把每个会话记在 ``<brain>/<uuid>/.system_generated/logs/transcript.jsonl``。
TRANSCRIPT_GLOB = "*/.system_generated/logs/transcript.jsonl"

#: 扫盘采集一段 transcript 时在会话 metadata 里记下的标记：文件当时的修改时间与大小。
SCANNED_MTIME = "scanned_mtime"
SCANNED_SIZE = "scanned_size"

ASK = "排查部署报错：连接池超时，请定位根因并给出修复方案。"
ANSWER = "先看日志，再核对连接池上限；修复后重新部署，测试验收通过。"
FOLLOW_UP = "补充：回滚后异常消失，请总结这次排查的修复经验。"
CONCLUSION = "结论：连接池上限需要按并发量配置，部署前先做压测。"
#: 四轮对话，`read_full_transcript` 逐行读出的样子。
DIALOGUE = f"[user] {ASK}\n[assistant] {ANSWER}\n[user] {FOLLOW_UP}\n[assistant] {CONCLUSION}"
#: 同一段对话进入队列分片后的样子：标题行与每一轮各成一个轮次，轮次之间空一行。
QUEUED_TEXT = "\n\n".join([f"Title: {ASK}", *DIALOGUE.split("\n")])

#: 用户步态里由应用写入的元数据：当前时间与打开的浏览器标签页，都不是对话。
BROWSER_TAB_METADATA = (
    "\n- The current local time is 2026-01-01T09:00:00.000Z\n"
    "- Active Document: 某页面 (https://example.test/a) (HTML)\n"
)


# ---------------------------------------------------------------- Antigravity fixtures


def _step(step_type: str, index: int, **fields: Any) -> dict[str, Any]:
    """一步记录：真实的键集合与取值形态，内容是虚构的。"""
    source = {"USER_INPUT": "USER_EXPLICIT", "SYSTEM_MESSAGE": "SYSTEM", "CHECKPOINT": "SYSTEM"}
    return {"step_index": index, "type": step_type, "source": source.get(step_type, "MODEL"), "status": "DONE", **fields}


def _user_step(text: str, index: int, *, metadata: str = "", settings: str = "") -> dict[str, Any]:
    content = f"<USER_REQUEST>{text}</USER_REQUEST>"
    if metadata:
        content += f"<ADDITIONAL_METADATA>{metadata}</ADDITIONAL_METADATA>"
    if settings:
        content += f"<USER_SETTINGS_CHANGE>{settings}</USER_SETTINGS_CHANGE>"
    return _step("USER_INPUT", index, content=content)


def _planner_step(index: int, content: str | None = None, *, thinking: str = "内部推理：先看日志。") -> dict[str, Any]:
    """一步模型回复：多数只有思考与工具调用，``content`` 才是回答。"""
    step = _step("PLANNER_RESPONSE", index, model="fictional-model", input_tokens=1200, output_tokens=300)
    if thinking:
        step["thinking"] = thinking
    step["tool_calls"] = [{"name": "read_file", "arguments": {"path": "src/app.py"}}]
    if content is not None:
        step["content"] = content
    return step


def _dump(index: int, lines: int = 200) -> dict[str, Any]:
    """一步工具输出：一个文件里体积最大的部分。"""
    body = "\n".join(f"TOOL-DUMP-LINE {index}-{n:04d} file contents and stack trace" for n in range(lines))
    return _step("GENERIC", index, content=body)


def _conversation(index: int = 0, *, first: str = ASK, metadata: str = "") -> list[dict[str, Any]]:
    """一段两轮的对话，中间夹着系统步态、工具调用与工具输出。"""
    return [
        _step("SYSTEM_MESSAGE", index, content="<system_message>SYSTEM-NOTICE</system_message>"),
        _user_step(first, index + 1, metadata=metadata),
        _planner_step(index + 2),
        _dump(index + 3),
        _planner_step(index + 4, ANSWER),
        _step("CHECKPOINT", index + 5, content="checkpoint-id: fictional"),
        _user_step(FOLLOW_UP, index + 6),
        _planner_step(index + 7),
        _dump(index + 8),
        _planner_step(index + 9, CONCLUSION),
    ]


def _write_jsonl_lines(path: Path, lines: Sequence[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8", newline="\n")
    return path


def _write_transcript(root: Path, session_uuid: str, steps: Sequence[dict[str, Any]]) -> Path:
    path = root / session_uuid / ".system_generated" / "logs" / "transcript.jsonl"
    return _write_jsonl_lines(path, steps)


def _codex_lines() -> list[dict[str, Any]]:
    return [
        {"timestamp": "2026-01-01T09:00:00Z", "type": "session_meta", "payload": {"id": CODEX_UUID}},
        {
            "timestamp": "2026-01-01T09:00:01Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": ASK}]},
        },
        {
            "timestamp": "2026-01-01T09:00:02Z",
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "TOOL-DUMP-LINE reasoning"}]},
        },
        {
            "timestamp": "2026-01-01T09:00:03Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": "TOOL-DUMP-LINE tool output"},
        },
        {
            "timestamp": "2026-01-01T09:00:04Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ANSWER}]},
        },
    ]


def _claude_code_lines() -> list[dict[str, Any]]:
    return [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": ASK}, {"type": "tool_result", "content": "TOOL-DUMP-LINE tool result"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "TOOL-DUMP-LINE thinking"}, {"type": "text", "text": ANSWER}],
            },
        },
        {"type": "system", "subtype": "turn_duration", "content": "TOOL-DUMP-LINE system notice"},
        {"type": "custom-title", "customTitle": CLAUDE_TITLE, "leafUuid": CLAUDE_UUID},
    ]


def _claude_code_turns(cwd: Path) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for index, (role, text) in enumerate(
        [("user", ASK), ("assistant", ANSWER), ("user", FOLLOW_UP), ("assistant", CONCLUSION)]
    ):
        line: dict[str, Any] = {
            "type": role,
            "sessionId": CLAUDE_UUID,
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
        }
        if index == 0:
            line["cwd"] = str(cwd)
        lines.append(line)
    return lines


def _repo(tmp_path: Path, name: str = "demo-service") -> tuple[Path, Path]:
    """一个带 ``.git`` 的虚构项目与其中的 ``docs/runbook.md``。"""
    repo = tmp_path / "projects" / name
    (repo / ".git").mkdir(parents=True)
    (repo / "docs").mkdir()
    doc = repo / "docs" / "runbook.md"
    doc.write_text("# runbook\n", encoding="utf-8")
    return repo, doc


def _age(path: Path, seconds: int) -> float:
    """把文件的最后修改时间设为 ``seconds`` 秒之前；返回读回的修改时间。"""
    mtime = int(time.time()) - seconds
    os.utime(path, (mtime, mtime))
    return path.stat().st_mtime


def _runtime(tmp_path: Path) -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    skills = tmp_path / "skills"
    skills.mkdir()
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.capabilities.skills_dirs = [str(skills)]
    # 这里验证的是采集与静默窗口，不是噪声阈值：阈值放低，让对话容易通过初筛。
    cfg.sessions.triage.min_chars = 20
    cfg.sessions.triage.gray_workspace_min_chars = 40
    return FlywheelRuntime(cfg, skip_fts_sync=True)


def _agy_source(root: Path) -> ScanSourceConfig:
    return ScanSourceConfig(client="antigravity", path=str(root), glob=TRANSCRIPT_GLOB)


# ---------------------------------------------------------------- 1. 步态级解析


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param("<USER_REQUEST>排查部署报错</USER_REQUEST>", "[user] 排查部署报错", id="plain"),
        pytest.param(
            "<USER_REQUEST>排查部署报错</USER_REQUEST><ADDITIONAL_METADATA>"
            "The current local time is 2026-01-01T09:00:00.000Z</ADDITIONAL_METADATA>",
            "[user] 排查部署报错",
            id="with-metadata",
        ),
        pytest.param(
            "<USER_REQUEST>排查部署报错</USER_REQUEST>"
            "<USER_SETTINGS_CHANGE>Accepted model: fictional-model</USER_SETTINGS_CHANGE>",
            "[user] 排查部署报错",
            id="with-settings-change",
        ),
        pytest.param(
            "<USER_REQUEST>按技能执行</USER_REQUEST><ADDITIONAL_METADATA><SKILL>"
            "<name>demo-skill</name><path>skills/demo-skill/SKILL.md</path></SKILL></ADDITIONAL_METADATA>",
            "[user] 按技能执行",
            id="with-skill-inside-metadata",
        ),
        pytest.param(
            "<ADDITIONAL_METADATA>The current local time is 2026-01-01T09:00:00.000Z</ADDITIONAL_METADATA>"
            "<USER_REQUEST>元数据在前</USER_REQUEST>",
            "[user] 元数据在前",
            id="metadata-first",
        ),
        # 写入途中被截断：请求未闭合时读到行尾，未闭合的元数据读到行尾都被剥离。
        pytest.param("<USER_REQUEST>写入被截断的请求", "[user] 写入被截断的请求", id="truncated-request"),
        pytest.param(
            "<USER_REQUEST>元数据未闭合</USER_REQUEST><ADDITIONAL_METADATA>Active Document: a.md",
            "[user] 元数据未闭合",
            id="unclosed-metadata",
        ),
        pytest.param("没有包装标签的裸请求", "[user] 没有包装标签的裸请求", id="no-wrappers"),
        # 文档审批：请求为空，包装之外也没有正文，这一步不产生对话。
        pytest.param(
            "<USER_REQUEST></USER_REQUEST><ADDITIONAL_METADATA>说明</ADDITIONAL_METADATA>", "", id="empty-request"
        ),
        pytest.param("<USER_REQUEST>   </USER_REQUEST>", "", id="blank-request"),
        # 请求为空但包装之外写了字：这些字才是这个人说的。
        pytest.param(
            "<USER_REQUEST></USER_REQUEST><ADDITIONAL_METADATA>说明</ADDITIONAL_METADATA>审批通过",
            "[user] 审批通过",
            id="typed-outside-request",
        ),
    ],
)
def test_a_user_step_keeps_only_what_the_person_typed(content: str, expected: str):
    """`USER_INPUT` 只留 `<USER_REQUEST>` 的正文，元数据与设置变更一律剥离。"""
    assert _extract_jsonl_text(_step("USER_INPUT", 1, content=content)) == expected


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        pytest.param(_planner_step(2, "修复方案：调大连接池上限。"), "[assistant] 修复方案：调大连接池上限。", id="answer"),
        pytest.param(
            _planner_step(2, "带思考的回复", thinking="内部推理：不该出现"), "[assistant] 带思考的回复", id="answer-with-thinking"
        ),
        pytest.param(_planner_step(2, None), "", id="thinking-and-tool-calls-only"),
        pytest.param(_planner_step(2, None, thinking=""), "", id="tool-calls-only"),
        pytest.param(_step("PLANNER_RESPONSE", 2, thinking="只有思考", tool_calls=[{"name": "run_command"}]), "", id="no-model-key"),
        pytest.param(_step("PLANNER_RESPONSE", 2, tool_calls=[{"name": "run_command"}]), "", id="no-thinking-key"),
    ],
)
def test_a_planner_step_is_its_answer_only(step: dict[str, Any], expected: str):
    """`PLANNER_RESPONSE` 只取 `content`；`thinking` 与 `tool_calls` 不是回答。"""
    assert _extract_jsonl_text(step) == expected


def test_tool_output_checkpoints_and_system_steps_are_not_dialogue():
    """工具输出、检查点、系统与错误步态都不是对话，体积再大也一行不进。"""
    steps = [
        _dump(3, lines=400),
        _step("CHECKPOINT", 5, content="checkpoint-id: fictional"),
        _step("SYSTEM_MESSAGE", 0, content="<system_message>SYSTEM-NOTICE</system_message>"),
        _step("SYSTEM", 0, content="SYSTEM-NOTICE"),
        _step("ERROR_MESSAGE", 7, error="TOOL-DUMP-LINE error detail"),
        _step("SOME_FUTURE_STEP", 8, content="TOOL-DUMP-LINE from a step type not named here"),
    ]
    for step in steps:
        assert is_antigravity_record(step) is True
        assert _extract_jsonl_text(step) == ""


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param(_codex_lines()[0], "", id="codex-session-meta"),
        pytest.param(_codex_lines()[1], f"[user] {ASK}", id="codex-user-message"),
        pytest.param(_codex_lines()[2], "", id="codex-reasoning"),
        pytest.param(_codex_lines()[3], "", id="codex-function-call-output"),
        pytest.param(_codex_lines()[4], f"[assistant] {ANSWER}", id="codex-assistant-message"),
        pytest.param(_claude_code_lines()[0], f"[user] {ASK}", id="claude-code-user-with-tool-result"),
        pytest.param(_claude_code_lines()[1], f"[assistant] {ANSWER}", id="claude-code-assistant-with-thinking"),
        pytest.param(_claude_code_lines()[2], "", id="claude-code-system"),
        pytest.param({"type": "summary", "summary": "会话摘要", "leafUuid": CLAUDE_UUID}, "", id="claude-code-summary"),
        pytest.param({"role": "user", "content": "旧式扁平记录"}, "[user] 旧式扁平记录", id="flat-record"),
    ],
)
def test_codex_and_claude_code_lines_read_exactly_as_before(line: dict[str, Any], expected: str):
    """Codex / Claude Code 的每一类记录都不被认作 Antigravity 步态，读出的文本与改动前一致。"""
    assert is_antigravity_record(line) is False
    assert _extract_jsonl_text(line) == expected


def test_a_codex_or_claude_code_envelope_is_never_read_as_an_antigravity_step():
    """`payload` / `message` 信封说明记录属于谁：即便多带一个 `step_index` 也按原路解析。"""
    codex_line = {
        "type": "response_item",
        "step_index": 4,
        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "带 step_index 的 Codex 记录"}]},
    }
    claude_line = {
        "type": "user",
        "step_index": 4,
        "sessionId": CLAUDE_UUID,
        "message": {"role": "user", "content": "带 step_index 的 Claude Code 记录"},
    }
    assert is_antigravity_record(codex_line) is False
    assert is_antigravity_record(claude_line) is False
    assert _extract_jsonl_text(codex_line) == "[user] 带 step_index 的 Codex 记录"
    assert _extract_jsonl_text(claude_line) == "[user] 带 step_index 的 Claude Code 记录"
    for not_a_step in (None, "USER_INPUT", ["USER_INPUT"], {}, {"type": "user"}, {"role": "assistant"}):
        assert is_antigravity_record(not_a_step) is False
    assert is_antigravity_record({"type": "USER_INPUT", "content": "x"}) is True


def test_a_transcript_reads_as_dialogue_only(tmp_path: Path):
    """整份 transcript 只读出对话：工具输出、系统步态与思考都不出现。"""
    path = _write_transcript(tmp_path, AGY_UUID, _conversation())
    assert read_full_transcript(path) == DIALOGUE
    assert read_transcript_excerpt(path) == DIALOGUE
    assert path.stat().st_size > 20 * len(DIALOGUE)
    for leak in ("TOOL-DUMP-LINE", "SYSTEM-NOTICE", "内部推理", "checkpoint-id", "src/app.py"):
        assert leak not in read_full_transcript(path, max_chars=10_000_000)


# ---------------------------------------------------------------- 2. 身份与标题


@pytest.mark.parametrize("upper", [False, True], ids=["lowercase", "uppercase"])
def test_identity_is_the_conversation_directory(tmp_path: Path, upper: bool):
    """身份是 transcript 所在的 uuid 目录，原样保留；同目录下的文件都叫 transcript.jsonl。"""
    session_uuid = AGY_UUID.upper() if upper else AGY_UUID
    path = _write_transcript(tmp_path, session_uuid, _conversation())
    meta = extract_jsonl_metadata(path)
    assert meta["canonical_session_id"] == session_uuid
    assert meta["is_subagent"] is False
    assert meta["parent_session_id"] == ""
    # 相对路径同样归一化；布局本身已经说明身份，不需要知道客户端。
    relative = Path(session_uuid) / ".system_generated" / "logs" / "transcript.jsonl"
    assert antigravity_session_id(relative) == session_uuid
    assert antigravity_session_id(path, client="claude-code") == session_uuid


def test_a_transcript_outside_the_layout_keeps_its_identity_when_the_client_is_named(tmp_path: Path):
    """被移出布局的 transcript：客户端是 antigravity 时仍按 uuid 目录归一化。"""
    inbox = tmp_path / "inbox"
    moved = inbox / AGY_UUID / "transcript.jsonl"
    moved.parent.mkdir(parents=True)
    shutil.copyfile(_write_transcript(tmp_path, AGY_UUID, _conversation()), moved)
    assert extract_jsonl_metadata(moved, client="antigravity")["canonical_session_id"] == AGY_UUID
    assert extract_jsonl_metadata(moved, client="antigravity")["first_user_message"] == ASK

    # 以会话命名的导出文件：文件名已经带了身份。
    export = inbox / f"{AGY_UUID}.jsonl"
    shutil.copyfile(moved, export)
    assert extract_jsonl_metadata(export)["canonical_session_id"] == AGY_UUID
    assert extract_jsonl_metadata(export, client="antigravity")["canonical_session_id"] == AGY_UUID


def test_the_title_is_the_first_request_that_says_something(tmp_path: Path):
    """标题取第一条有效请求的正文：不带包装标签，空请求与样板文本被跳过。"""
    steps = _conversation(metadata=BROWSER_TAB_METADATA)

    def title_of(first_step: dict[str, Any]) -> str:
        steps[1] = first_step
        meta = extract_jsonl_metadata(_write_transcript(tmp_path, AGY_UUID, steps))
        return derive_readable_title(
            "antigravity", meta["canonical_session_id"], text=meta["first_user_message"], workspace=meta["workspace"]
        )

    assert title_of(_user_step(ASK, 1, metadata=BROWSER_TAB_METADATA)) == ASK
    assert title_of(_user_step("", 1, metadata=BROWSER_TAB_METADATA)) == FOLLOW_UP
    boilerplate = "<system-reminder>SYSTEM-NOTICE</system-reminder>"
    assert title_of(_user_step(boilerplate, 1, metadata=BROWSER_TAB_METADATA)) == FOLLOW_UP
    long_ask = "请排查部署报错并在修复后补充回归测试用例" * 5
    title = title_of(_user_step(long_ask, 1, metadata=BROWSER_TAB_METADATA))
    assert len(title) == 64
    assert long_ask.startswith(title)


def test_a_conversation_without_a_user_step_gets_a_fallback_title_and_its_own_identity(tmp_path: Path):
    """没有用户步态的会话（由系统触发）：标题回退为 `antigravity · <尾 8 位>`，身份仍是自己的 uuid。"""
    path = _write_transcript(
        tmp_path,
        AGY_UUID,
        [
            _step("SYSTEM_MESSAGE", 0, content="<system_message>SYSTEM-NOTICE</system_message>"),
            _planner_step(1),
            _dump(2),
            _planner_step(3, ANSWER),
        ],
    )
    meta = extract_jsonl_metadata(path)
    assert meta["canonical_session_id"] == AGY_UUID
    assert meta["first_user_message"] == ""
    assert derive_readable_title("antigravity", meta["canonical_session_id"], workspace="") == f"antigravity · {AGY_UUID[-8:]}"
    assert read_full_transcript(path) == f"[assistant] {ANSWER}"


def test_a_user_step_many_steps_in_is_still_read(tmp_path: Path):
    """用户步态排在很后面时，Antigravity transcript 继续往下读用户步态，其他行不解析。"""
    steps = [_step("SYSTEM_MESSAGE", 0, content="<system_message>SYSTEM-NOTICE</system_message>")]
    steps += [_dump(index, lines=5) for index in range(1, 120)]
    steps += _conversation(120, metadata=BROWSER_TAB_METADATA)
    meta = extract_jsonl_metadata(_write_transcript(tmp_path, AGY_UUID, steps))
    assert meta["canonical_session_id"] == AGY_UUID
    assert meta["first_user_message"] == ASK


def test_other_clients_are_still_read_only_in_their_opening_lines(tmp_path: Path):
    """`max_lines` 对其他客户端不变：开头 50 行之外的内容不读。"""
    filler = [{"type": "response_item", "payload": {"type": "reasoning", "summary": []}} for _ in range(80)]
    late = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "第 81 行"}]}}
    path = _write_jsonl_lines(tmp_path / f"rollout-{CODEX_UUID}.jsonl", [*filler, late])
    assert extract_jsonl_metadata(path)["first_user_message"] == ""
    assert extract_jsonl_metadata(path, max_lines=81)["first_user_message"] == "第 81 行"


# ---------------------------------------------------------------- 3. 工作区


def _workspace_cases(tmp_path: Path) -> dict[str, tuple[str, str]]:
    """一个用户步态的内容 → 它应当给出的工作区（``""`` 表示留空）。"""
    repo, doc = _repo(tmp_path)
    notes = tmp_path / "work area" / "release notes" / "v1.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("v1\n", encoding="utf-8")
    doc_posix, doc_native = doc.as_posix(), str(doc)
    notes_posix = notes.as_posix()

    def step(request: str = "请补全这份文档的部署步骤。", line: str = "") -> str:
        return _user_step(request, 0, metadata=f"\n{line}\n" if line else "")["content"]

    return {
        "open document, forward slashes": (
            step(line=f"- Active Document: runbook.md tab 1 ({doc_posix}) (MARKDOWN)"),
            str(repo),
        ),
        "open document, backslashes and bold label": (
            step(line=f"- **Active Document:** runbook.md tab 1 ({doc_native}) (MARKDOWN)"),
            str(repo),
        ),
        "workspace declaration": (step(line=f"* Workspace: {repo.as_posix()}"), str(repo)),
        "typed path, forward slashes": (step(request=f"请修订 {doc_posix} 里的部署步骤。"), str(repo)),
        "typed path, backslashes": (step(request=f"请修订 {doc_native} 里的部署步骤。"), str(repo)),
        "typed path with a word written straight after it": (step(request=f"请修订{doc_posix}里的部署步骤。"), str(repo)),
        "typed path with spaces in backticks": (step(request=f"请整理 `{notes_posix}` 的内容。"), str(notes.parent)),
        "typed path with spaces in double quotes": (step(request=f'请整理 "{notes_posix}" 的内容。'), str(notes.parent)),
        "typed path with spaces in two folders, unquoted": (step(request=f"请看 {notes.parent.as_posix()} 的说明。"), str(notes.parent)),
        "typed file that does not exist yet": (step(request=f"请新建 {(repo / 'docs' / 'rollback.md').as_posix()}"), str(repo)),
        # 以下一律留空：浏览器标签页不是工作区，URL、相对路径与没有线索都不是。
        "browser tab": (step(line="- Active Document: 某页面 (https://example.test/a) (HTML)"), ""),
        "only a url in the request": (step(request="请参考 https://example.test/docs/a 的说明。"), ""),
        "a relative path only": (step(request="请看 docs/runbook.md 里的部署步骤。"), ""),
        "a path inside a quoted command": (step(request="请运行 `git -C /srv/demo status` 并解释输出。"), ""),
        "a typed path cut by a blank that is on no disk": (
            step(request=f"请看 {tmp_path / 'no such folder' / 'v1.md'} 的说明。"),
            "",
        ),
        "no clue at all": (step(), ""),
    }


@pytest.mark.parametrize(
    "case",
    [
        "open document, forward slashes",
        "open document, backslashes and bold label",
        "workspace declaration",
        "typed path, forward slashes",
        "typed path, backslashes",
        "typed path with a word written straight after it",
        "typed path with spaces in backticks",
        "typed path with spaces in double quotes",
        "typed path with spaces in two folders, unquoted",
        "typed file that does not exist yet",
    ],
)
def test_the_workspace_comes_from_the_open_document_or_the_typed_path(tmp_path: Path, case: str):
    """`Active Document` / 工作区声明，或请求里写下的本地路径：落在磁盘上才成为工作区。"""
    content, expected = _workspace_cases(tmp_path)[case]
    assert antigravity_workspace(content, home=tmp_path / "unused-home") == expected


@pytest.mark.parametrize(
    "case",
    [
        "browser tab",
        "only a url in the request",
        "a relative path only",
        "a path inside a quoted command",
        "a typed path cut by a blank that is on no disk",
        "no clue at all",
    ],
)
def test_a_workspace_is_never_guessed(tmp_path: Path, case: str):
    """没有命中就留空：浏览器标签页、URL、相对路径、被空格截断的路径都不算工作区。"""
    content, expected = _workspace_cases(tmp_path)[case]
    assert expected == ""
    assert antigravity_workspace(content, home=tmp_path / "unused-home") == ""
    assert antigravity_workspace(None, home=tmp_path) == ""
    assert antigravity_workspace("", home=tmp_path) == ""


def test_a_tool_s_own_folder_the_home_directory_and_a_drive_root_are_never_workspaces(tmp_path: Path):
    """技能、设置与会话存储所在的目录，家目录本身与盘符根目录都不是工作区。"""
    home = tmp_path / "home"
    skill = home / ".gemini" / "antigravity" / "skills" / "demo-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# demo-skill\n", encoding="utf-8")
    project = home / "projects" / "demo-service" / "README.md"
    project.parent.mkdir(parents=True)
    project.write_text("# demo\n", encoding="utf-8")

    def workspace_of(request: str) -> str:
        return antigravity_workspace(_user_step(request, 0)["content"], home=home)

    assert workspace_of(f"请按 {skill.as_posix()} 的步骤执行。") == ""
    assert workspace_of(f"工作目录就是 {home.as_posix()} 本身。") == ""
    assert workspace_of(f"盘符根目录 {tmp_path.anchor}. 下面还有别的吗") == ""
    assert workspace_of(f"盘符根目录 {tmp_path.anchor}not-there，下面呢") == ""
    # 家目录下普通的项目目录仍然算工作区。
    assert workspace_of(f"请看 {project.as_posix()} 的说明。") == str(project.parent)


def test_the_workspace_is_looked_for_in_later_user_steps_in_order(tmp_path: Path):
    """工作区线索可能在中途才出现：按用户步态顺序找，先命中的算数；标题仍取首条请求。"""
    repo, doc = _repo(tmp_path)
    opened_later = f"\n- Active Document: runbook.md tab 1 ({doc.as_posix()}) (MARKDOWN)\n"
    steps = [*_conversation()]
    steps.append(_user_step("继续：请补全运行手册的回滚步骤。", 10, metadata=opened_later))
    steps.append(_planner_step(11, "已补全回滚步骤。"))
    meta = extract_jsonl_metadata(_write_transcript(tmp_path, AGY_UUID, steps))
    assert meta["first_user_message"] == ASK
    assert meta["workspace"] == str(repo)

    # 首条请求已经指向另一个项目：后面打开的文档不覆盖它。
    other_repo, other_doc = _repo(tmp_path, "other-service")
    steps[1] = _user_step(f"请先看 {other_doc.as_posix()} 的说明。", 1)
    meta = extract_jsonl_metadata(_write_transcript(tmp_path, AGY_UUID, steps))
    assert meta["workspace"] == str(other_repo)


# ---------------------------------------------------------------- 4. 兼容联调


def test_codex_and_claude_code_transcripts_read_and_chunk_as_before(tmp_path: Path):
    """Codex / Claude Code transcript 的元数据、对话文本、标题与分片结果都不变。"""
    cwd = tmp_path / "demo"
    expected = f"[user] {ASK}\n[assistant] {ANSWER}"
    for path, client, session_uuid, expected_title in (
        (tmp_path / f"rollout-2026-01-01T09-00-00-{CODEX_UUID}.jsonl", "codex", CODEX_UUID, ASK),
        (tmp_path / "projects" / "demo" / f"{CLAUDE_UUID}.jsonl", "claude-code", CLAUDE_UUID, CLAUDE_TITLE),
    ):
        lines = _codex_lines() if client == "codex" else _claude_code_lines()
        lines[0]["cwd"] = str(cwd)
        _write_jsonl_lines(path, lines)
        assert read_full_transcript(path) == expected
        assert read_transcript_excerpt(path) == expected
        meta = extract_jsonl_metadata(path)
        assert meta["canonical_session_id"] == session_uuid
        assert meta["workspace"] == str(cwd)
        # Codex 的标题来自首条用户消息；Claude Code 的来自它自己的 custom-title。
        if client == "codex":
            assert meta["first_user_message"] == ASK
        assert meta["custom_title"] == (CLAUDE_TITLE if client == "claude-code" else "")
        assert derive_readable_title(
            client,
            session_uuid,
            custom_title=meta["custom_title"],
            text=meta["first_user_message"],
            workspace=meta["workspace"],
        ) == expected_title
        prepared = prepare_queue_chunks(read_full_transcript(path))
        assert prepared["total_turns"] == 2
        assert prepared["is_oversized"] is False
        assert [chunk["chunk_text"] for chunk in prepared["chunks"]] == [expected.replace("\n", "\n\n")]


def test_an_oversized_conversation_keeps_its_head_and_tail_without_tool_output(tmp_path: Path):
    """大体积会话：工具输出不计入分片，开头与最终结论都在，每片只含对话轮次。"""
    rounds = 80
    steps: list[dict[str, Any]] = [_step("SYSTEM_MESSAGE", 0, content="<system_message>SYSTEM-NOTICE</system_message>")]
    for index in range(rounds):
        base = 1 + index * 4
        ask = f"第 {index + 1} 轮：请排查部署报错并给出修复方案。" + "补充上下文与日志说明。" * 130
        steps.append(_user_step(ask, base))
        steps.append(_planner_step(base + 1))
        steps.append(_dump(base + 2, lines=40))
        steps.append(_planner_step(base + 3, f"第 {index + 1} 轮结论：已定位根因并修复，测试通过。"))
    steps.append(_user_step("最后请给出最终结论。", steps[-1]["step_index"] + 1))
    steps.append(_planner_step(steps[-1]["step_index"] + 1, CONCLUSION))
    path = _write_transcript(tmp_path, AGY_UUID, steps)

    text = read_full_transcript(path)
    assert "TOOL-DUMP-LINE" not in text
    prepared = prepare_queue_chunks(text, max_item_chars=24000)
    chunks = prepared["chunks"]
    assert prepared["total_turns"] == rounds * 2 + 2
    assert prepared["is_oversized"] is True
    assert len(chunks) == MAX_CHUNKS_PER_SESSION
    assert prepared["omitted_middle_chunks"] >= 1
    assert chunks[0]["chunk_text"].startswith("[user] 第 1 轮")
    assert chunks[-1]["chunk_text"].rstrip().endswith(f"[assistant] {CONCLUSION}")
    for chunk in chunks:
        assert len(chunk["chunk_text"]) <= 24000
        assert "TOOL-DUMP-LINE" not in chunk["chunk_text"]
        assert all(turn.startswith(("[user]", "[assistant]")) for turn in split_dialogue_turns(chunk["chunk_text"]))


def test_tool_output_no_longer_inflates_what_the_triage_rules_measure(tmp_path: Path):
    """初筛量的是对话，不是文件：几百 KB 的工具输出不改变「太短」的判定。"""
    path = _write_transcript(tmp_path, AGY_UUID, [_user_step("你好", 0), _dump(1, lines=3000), _planner_step(2, "你好，有什么可以帮你？")])
    session = SessionRecord(
        session_key=f"antigravity:{AGY_UUID}",
        client="antigravity",
        session_id=AGY_UUID,
        title="你好",
        transcript_path=str(path),
    )
    text = SessionAdapter(FlywheelConfig(base_dir=tmp_path), SimpleNamespace()).load_session_text(session, max_chars=8000)
    assert text == "Title: 你好\n\n[user] 你好\n[assistant] 你好，有什么可以帮你？"
    assert path.stat().st_size > 100_000

    result = triage_session(session, text, SessionTriageConfig())
    assert result["admit"] is False
    assert result["reason_code"] == "TOO_SHORT"
    # 量到的只有这 33 个字符的对话，不是文件里几百 KB 的工具输出。
    measured = int(result["reason"].split("(")[1].split("<")[0])
    assert measured == len("[user] 你好\n[assistant] 你好，有什么可以帮你？")


# ---------------------------------------------------------------- 5. 静默窗口


@pytest.mark.parametrize(
    ("now_ts", "active"),
    [
        pytest.param(1000.0, True, id="just-written"),
        pytest.param(1899.999, True, id="half-a-second-inside"),
        pytest.param(1900.0, False, id="exactly-900-seconds"),
        pytest.param(5000.0, False, id="long-quiet"),
    ],
)
def test_the_quiet_window_is_half_open(now_ts: float, active: bool):
    """刚好满 900 秒即视为静默：窗口是左开右闭的 900 秒。"""
    assert transcript_is_active(1000.0, quiet_seconds=900, now_ts=now_ts) is active


def test_a_window_of_zero_never_holds_anything_back():
    assert transcript_is_active(1000.0, quiet_seconds=0, now_ts=1000.0) is False
    assert transcript_is_active(1000.0, quiet_seconds=-5, now_ts=1000.0) is False


@pytest.mark.parametrize(
    ("sessions", "expected"),
    [
        pytest.param(SimpleNamespace(), DEFAULT_SCAN_QUIET_WINDOW_SECONDS, id="setting-absent"),
        pytest.param(SimpleNamespace(scan_quiet_window_seconds=1200), 1200, id="wider-window"),
        pytest.param(SimpleNamespace(scan_quiet_window_seconds="600"), 600, id="numeric-string"),
        pytest.param(SimpleNamespace(scan_quiet_window_seconds=0), 0, id="zero-switches-off"),
        pytest.param(SimpleNamespace(scan_quiet_window_seconds=None), 0, id="none-switches-off"),
        pytest.param(SimpleNamespace(scan_quiet_window_seconds=-30), 0, id="negative-switches-off"),
        pytest.param(None, DEFAULT_SCAN_QUIET_WINDOW_SECONDS, id="no-sessions-section"),
    ],
)
def test_the_quiet_window_setting_is_read_with_a_default(sessions: Any, expected: int):
    """`sessions.scan_quiet_window_seconds`：缺省 900，0 / None 关闭，负数按关闭。"""
    assert DEFAULT_SCAN_QUIET_WINDOW_SECONDS == 900
    assert scan_quiet_window_seconds(SimpleNamespace(sessions=sessions)) == expected
    assert scan_quiet_window_seconds(object()) == DEFAULT_SCAN_QUIET_WINDOW_SECONDS


@pytest.mark.parametrize(
    ("client", "expected"),
    [
        pytest.param("antigravity", True, id="antigravity"),
        pytest.param(" Antigravity ", True, id="padded-and-capitalised"),
        pytest.param("ANTIGRAVITY", True, id="upper-case"),
        pytest.param("antigravity-desktop", False, id="a-different-client-with-the-same-prefix"),
        pytest.param("claude-code", False, id="claude-code"),
        pytest.param("codex", False, id="codex"),
        pytest.param("", False, id="empty"),
        pytest.param(None, False, id="none"),
    ],
)
def test_only_the_antigravity_client_has_a_quiet_window(client: Any, expected: bool):
    assert is_antigravity_client(client) is expected


def test_the_scan_holds_back_a_transcript_that_is_still_being_written(tmp_path: Path):
    """刚修改 60 秒的会话不登记、不入队；超过 900 秒的会话正常扫盘，静默后由后续扫描补上。"""
    rt = _runtime(tmp_path)
    brain = tmp_path / "brain"
    active = _write_transcript(brain, AGY_UUID_ACTIVE, _conversation())
    quiet = _write_transcript(brain, AGY_UUID_QUIET, _conversation())
    _age(active, 60)
    _age(quiet, 901)
    rt.cfg.sessions.scan_sources = [_agy_source(brain)]

    report = rt.sessions.reconcile_scan_sources()
    assert (report["scanned"], report["newly_registered"], report["skipped_active"]) == (1, 1, 1)
    assert report["filtered_fragments"] == 0
    assert report["gaps"] == []

    assert rt.db.get_session(f"antigravity:{AGY_UUID_ACTIVE}") is None
    session = rt.db.get_session(f"antigravity:{AGY_UUID_QUIET}")
    assert session.session_key == f"antigravity:{AGY_UUID_QUIET}"
    assert session.client == "antigravity"
    assert session.session_id == AGY_UUID_QUIET
    assert session.title == ASK
    assert session.status == SessionStatus.PENDING
    assert session.transcript_path == str(quiet)

    # 刚才被挡下的会话静默之后被下一次扫描补上；已登记的不会重复登记。
    _age(active, 901)
    report = rt.sessions.reconcile_scan_sources()
    assert (report["scanned"], report["newly_registered"], report["skipped_active"]) == (2, 1, 0)
    assert rt.db.get_session(f"antigravity:{AGY_UUID_ACTIVE}").title == ASK


def test_the_scan_reads_other_clients_without_a_quiet_window(tmp_path: Path):
    """静默窗口只属于 antigravity：其他客户端刚写入的会话照常扫盘。"""
    rt = _runtime(tmp_path)
    projects = tmp_path / "projects"
    path = _write_jsonl_lines(projects / "demo" / f"{CLAUDE_UUID}.jsonl", _claude_code_turns(tmp_path / "demo"))
    _age(path, 5)
    rt.cfg.sessions.scan_sources = [ScanSourceConfig(client="claude-code", path=str(projects))]

    report = rt.sessions.reconcile_scan_sources()
    assert (report["scanned"], report["newly_registered"], report["skipped_active"]) == (1, 1, 0)
    assert rt.db.get_session(f"claude-code:{CLAUDE_UUID}").session_key == f"claude-code:{CLAUDE_UUID}"


def test_the_scan_window_follows_the_setting_even_when_the_setting_is_absent(tmp_path: Path):
    """配置里没有这个键时按 900 秒处理；设为 0 时窗口关闭。"""
    rt = _runtime(tmp_path)
    brain = tmp_path / "brain"
    active = _write_transcript(brain, AGY_UUID_ACTIVE, _conversation())
    _age(active, 60)
    sessions = SimpleNamespace(scan_sources=[_agy_source(brain)], triage=rt.cfg.sessions.triage)
    adapter = SessionAdapter(SimpleNamespace(sessions=sessions, resolve_path=rt.cfg.resolve_path), rt.db)

    report = adapter.reconcile_scan_sources()
    assert (report["scanned"], report["newly_registered"], report["skipped_active"]) == (0, 0, 1)

    sessions.scan_quiet_window_seconds = 0
    report = adapter.reconcile_scan_sources()
    assert (report["scanned"], report["newly_registered"], report["skipped_active"]) == (1, 1, 0)


def test_the_archive_gate_leaves_a_session_it_held_back_untouched(tmp_path: Path):
    """归档事件在静默窗口内到达：不登记会话、不建队列版本，静默之后才入队。"""
    rt = _runtime(tmp_path)
    path = _write_transcript(tmp_path, AGY_UUID_ACTIVE, _conversation())
    key = f"antigravity:{AGY_UUID_ACTIVE}"
    mtime = _age(path, 60)

    held = rt.queue.on_archive("antigravity", AGY_UUID_ACTIVE, transcript_path=str(path), now_ts=mtime + 60)
    assert held == {"action": "skipped", "reason_code": QUIET_WINDOW_ACTIVE, "items": []}
    assert rt.db.get_session(key) is None
    assert rt.db.list_session_queue_items(key) == []
    assert rt.db.get_latest_queue_revision(key) == 0

    released = rt.queue.on_archive("antigravity", AGY_UUID_ACTIVE, transcript_path=str(path), now_ts=mtime + 900.5)
    assert released["action"] == "enqueued"
    assert [item.session_key for item in released["items"]] == [key]


def test_the_archive_gate_leaves_an_already_queued_session_as_it_is(tmp_path: Path):
    """已入队的会话继续写入：窗口内的归档事件不生成新版本、不动已入队的条目。"""
    rt = _runtime(tmp_path)
    brain = tmp_path / "brain"
    path = _write_transcript(brain, AGY_UUID, _conversation())
    key = f"antigravity:{AGY_UUID}"
    quiet = _age(path, 3600)
    first = rt.queue.on_archive("antigravity", "transcript", transcript_path=str(path), now_ts=quiet + 3600)
    assert first["action"] == "enqueued"
    assert [item.revision for item in rt.db.list_session_queue_items(key)] == [1]

    grown = [*_conversation(), _user_step("追加：再补一个回归测试用例。", 10), _planner_step(11, "已补充。")]
    active = _age(_write_transcript(brain, AGY_UUID, grown), 60)
    result = rt.queue.on_archive("antigravity", "transcript", transcript_path=str(path), now_ts=active + 60)
    assert result == {"action": "skipped", "reason_code": QUIET_WINDOW_ACTIVE, "items": []}
    assert [(item.revision, item.status) for item in rt.db.list_session_queue_items(key)] == [(1, QueueStatus.QUEUED)]
    assert rt.db.get_latest_queue_revision(key) == 1

    settled = _age(path, 901)
    result = rt.queue.on_archive("antigravity", "transcript", transcript_path=str(path), now_ts=settled + 901)
    assert result["action"] == "enqueued"
    assert result["items"][0].revision == 2
    assert [item.revision for item in rt.db.list_session_queue_items(key)] == [2, 1]


def test_the_archive_gate_is_exactly_as_wide_as_the_window(tmp_path: Path):
    """差半秒满 900 秒仍被挡下；超过 900 秒即放行。"""
    rt = _runtime(tmp_path)
    path = _write_transcript(tmp_path, AGY_UUID, _conversation())
    mtime = _age(path, 3600)
    key = f"antigravity:{AGY_UUID}"

    held = rt.queue.on_archive("antigravity", AGY_UUID, transcript_path=str(path), now_ts=mtime + 899.5)
    assert held == {"action": "skipped", "reason_code": QUIET_WINDOW_ACTIVE, "items": []}
    released = rt.queue.on_archive("antigravity", AGY_UUID, transcript_path=str(path), now_ts=mtime + 900.5)
    assert [item.session_key for item in released["items"]] == [key]
    assert rt.db.get_latest_queue_revision(key) == 1


def test_the_archive_gate_follows_the_setting(tmp_path: Path):
    """窗口关闭时正在写入的 transcript 直接入队；窗口调大时更早静默的 transcript 仍被挡下。"""
    rt = _runtime(tmp_path)
    path = _write_transcript(tmp_path, AGY_UUID, _conversation())
    key = f"antigravity:{AGY_UUID}"
    mtime = _age(path, 1000)

    rt.cfg.sessions.scan_quiet_window_seconds = 3600
    held = rt.queue.on_archive("antigravity", AGY_UUID, transcript_path=str(path), now_ts=mtime + 1000)
    assert held["reason_code"] == QUIET_WINDOW_ACTIVE

    rt.cfg.sessions.scan_quiet_window_seconds = 0
    released = rt.queue.on_archive("antigravity", AGY_UUID, transcript_path=str(path), now_ts=mtime + 1)
    assert released["action"] == "enqueued"
    assert [item.session_key for item in released["items"]] == [key]


@pytest.mark.parametrize("client", ["codex", "claude-code", "generic"])
def test_the_archive_gate_applies_to_antigravity_only(tmp_path: Path, client: str):
    """其他客户端的归档事件没有静默窗口：刚写入 5 秒也照常入队。"""
    rt = _runtime(tmp_path)
    path = _write_jsonl_lines(tmp_path / "session.jsonl", _claude_code_turns(tmp_path / "demo"))
    _age(path, 5)

    result = rt.queue.on_archive(client, CLAUDE_UUID, transcript_path=str(path), now_ts=time.time())
    assert result["action"] == "enqueued"
    assert [item.session_key for item in result["items"]] == [f"{client}:{CLAUDE_UUID}"]


def test_an_explicit_enqueue_is_never_held_back(tmp_path: Path):
    """显式入队是这个人的决定：transcript 正在写入也照常入队，且只带对话。"""
    rt = _runtime(tmp_path)
    path = _write_transcript(tmp_path, AGY_UUID_ACTIVE, [*_conversation(), _dump(10, lines=50)])
    _age(path, 10)

    items = rt.queue.on_explicit_enqueue(
        client="antigravity", session_id=AGY_UUID_ACTIVE, transcript_path=str(path), title=ASK, project="demo"
    )
    assert len(items) == 1
    assert items[0].status is QueueStatus.QUEUED
    assert items[0].session_key == f"antigravity:{AGY_UUID_ACTIVE}"
    text = rt.queue.load_queue_item_text(items[0])
    assert text.startswith(QUEUED_TEXT)
    assert "TOOL-DUMP-LINE" not in text
    assert "USER_REQUEST" not in text


def test_the_archive_sync_counts_what_it_held_back_and_picks_it_up_later(tmp_path: Path):
    """归档同步把被挡下的 transcript 记在 `skipped_active` 下，静默后由下一轮补上。"""
    rt = _runtime(tmp_path)
    brain = tmp_path / "brain"
    active = _write_transcript(brain, AGY_UUID_ACTIVE, _conversation())
    quiet = _write_transcript(brain, AGY_UUID_QUIET, _conversation())
    _age(active, 60)
    _age(quiet, 3600)
    rt.cfg.sessions.codex_archive_sources = [
        ScanSourceConfig(client="antigravity", path=str(brain), glob="**/transcript.jsonl")
    ]

    report = rt.queue.reconcile_codex_archives()
    assert (report["scanned"], report["skipped_active"], report["enqueued"], report["excluded"], report["unchanged"]) == (
        2,
        1,
        1,
        0,
        0,
    )
    assert [(source["scanned"], source["skipped_active"], source["enqueued"]) for source in report["sources"]] == [(2, 1, 1)]
    assert rt.db.get_session(f"antigravity:{AGY_UUID_ACTIVE}") is None

    _age(active, 3600)
    report = rt.queue.reconcile_codex_archives()
    assert (report["scanned"], report["skipped_active"], report["enqueued"], report["unchanged"]) == (2, 0, 1, 1)
    queued = rt.db.list_session_queue_items(f"antigravity:{AGY_UUID_ACTIVE}", statuses=[QueueStatus.QUEUED])
    assert len(queued) == 1
    assert queued[0].ingress_channel is IngressChannel.CODEX_ARCHIVE


def test_the_passive_flow_queues_the_quiet_conversation_and_never_sees_the_active_one(tmp_path: Path):
    """扫盘 → 初筛 → 入队：静默的会话以 `antigravity:<uuid>` 入队且只带对话，正在写入的那个全程不可见。"""
    rt = _runtime(tmp_path)
    brain = tmp_path / "brain"
    repo, doc = _repo(tmp_path)
    opened = f"\n- Active Document: runbook.md tab 1 ({doc.as_posix()}) (MARKDOWN)\n"
    active = _write_transcript(brain, AGY_UUID_ACTIVE, _conversation())
    quiet = _write_transcript(brain, AGY_UUID_QUIET, _conversation(metadata=opened))
    _age(active, 60)
    _age(quiet, 3600)
    rt.cfg.sessions.scan_sources = [_agy_source(brain)]
    quiet_key, active_key = f"antigravity:{AGY_UUID_QUIET}", f"antigravity:{AGY_UUID_ACTIVE}"

    scan = rt.sessions.reconcile_scan_sources()
    assert (scan["newly_registered"], scan["skipped_active"]) == (1, 1)
    session = rt.db.get_session(quiet_key)
    assert session.title == ASK
    assert session.workspace == str(repo)

    sweep = rt.sessions.sweep_pending_triage()
    assert (sweep["checked"], sweep["admitted"], sweep["skipped"]) == (1, 1, 0)
    assert rt.db.get_session(quiet_key).metadata["triage_status"] == "admitted"

    assert rt.queue.bridge_pending_sessions_to_queue() == 1
    items = rt.db.list_session_queue_items(quiet_key)
    assert len(items) == 1
    assert items[0].status is QueueStatus.QUEUED
    assert items[0].client == "antigravity"
    assert items[0].ingress_channel is IngressChannel.LEGACY_SCAN
    item_text = rt.queue.load_queue_item_text(items[0])
    assert item_text.startswith(QUEUED_TEXT)
    assert "TOOL-DUMP-LINE" not in item_text

    assert rt.db.get_session(active_key) is None
    assert rt.db.list_session_queue_items(active_key) == []
    assert rt.db.get_latest_queue_revision(active_key) == 0


# ---------------------------------------------------------------- 6. 合法却不是记录的行

#: 合法的 JSON，却不是一条对话记录：写到一半被截断的行、别的进程写进来的内容，或者就是一个空值。
JUNK_LINES: tuple[tuple[str, str], ...] = (
    ("truncated", '{"timestamp": "2026-01-01T09:00:00Z", "type": "message", "message": {"role": "user"'),
    ("null", "null"),
    ("empty-list", "[]"),
    ("number-list", "[1, 2, 3]"),
    ("number", "123"),
    ("float", "4.5"),
    ("true", "true"),
    ("numeric-string", '"456"'),
    ("empty-object", "{}"),
    ("blank", "   "),
)

#: 三种 transcript：各自的干净记录、所在的目录与会话标识。
TRANSCRIPTS = {
    "antigravity": (_conversation(), Path("brain") / AGY_UUID_QUIET / ".system_generated" / "logs", AGY_UUID_QUIET),
    "claude-code": (_claude_code_turns(Path("projects") / "demo"), Path("projects") / "demo", CLAUDE_UUID),
    "codex": (_codex_lines(), Path("codex") / "archived_sessions", CODEX_UUID),
}


def _write_lines(path: Path, lines: Sequence[dict[str, Any] | str]) -> Path:
    """Write ``lines`` as JSONL; a line that is already text is written as it is."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            (line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)) + "\n"
            for line in lines
        ),
        encoding="utf-8",
        newline="\n",
    )
    return path


def _with_junk(lines: Sequence[dict[str, Any]], junk: str) -> list[dict[str, Any] | str]:
    """``lines`` with one junk line first, one in the middle and one last."""
    middle = len(lines) // 2
    return [junk, *lines[:middle], junk, *lines[middle:], junk]


@pytest.mark.parametrize(
    "value",
    [None, [], [1, 2, 3], 123, 4.5, True, "456", {}, {"message": None}, {"message": []}],
    ids=["none", "list", "list-of-numbers", "int", "float", "bool", "string", "object", "null-message", "list-message"],
)
def test_a_json_value_that_is_not_a_record_reads_as_no_text(value: Any):
    """`json.loads` 认这些值，它们却不是一条记录：读出来是空文本，不是异常。"""
    assert _extract_jsonl_text(value) == ""


@pytest.mark.parametrize("client", sorted(TRANSCRIPTS))
@pytest.mark.parametrize("junk", [line for _, line in JUNK_LINES], ids=[name for name, _ in JUNK_LINES])
def test_a_valid_json_line_that_is_not_a_record_changes_no_reading(tmp_path: Path, client: str, junk: str):
    """夹着这种行的 transcript，三个读取器读出来与干净的逐字相同，入队的也是同一份对话。"""
    rt = _runtime(tmp_path)
    lines, folder, session_id = TRANSCRIPTS[client]
    clean = _write_lines(tmp_path / folder / "clean.jsonl", lines)
    mixed = _write_lines(tmp_path / folder / "mixed.jsonl", _with_junk(lines, junk))
    _age(clean, 901)
    _age(mixed, 901)

    expected = read_full_transcript(clean)
    assert ASK in expected and ANSWER in expected
    assert "TOOL-DUMP-LINE" not in expected

    assert read_full_transcript(mixed) == expected
    assert read_transcript_excerpt(mixed) == read_transcript_excerpt(clean)
    metadata = extract_jsonl_metadata(mixed, client=client)
    assert metadata == extract_jsonl_metadata(clean, client=client)
    assert metadata["canonical_session_id"] == session_id

    result = rt.queue.on_archive(client, session_id, transcript_path=str(mixed))
    assert result["action"] == "enqueued", result
    (item,) = result["items"]
    # The queue reads the dialogue through the same excerpt reader: one turn per line, under the title.
    session = rt.db.get_session(item.session_key)
    assert rt.queue.load_queue_item_text(item) == "\n\n".join([f"Title: {session.title}", *expected.split("\n")])


# ---------------------------------------------------------------- 7. 续聊后的再次采集

APPENDED_ASK = "追加：回滚之后异常没有再出现，请把回滚预案也补进经验卡。"
APPENDED_ANSWER = "已补充：回滚预案写进经验卡，并补一条回归用例。"
HAND_WRITTEN_NOTE = "projects/demo/deploy-rollback.md"


def _grown_conversation() -> list[dict[str, Any]]:
    """同一段对话在第一轮消费之后又多了两轮。"""
    return [*_conversation(), _user_step(APPENDED_ASK, 10), _planner_step(11, APPENDED_ANSWER)]


def _consume(rt: FlywheelRuntime, queue_id: str) -> WorkflowRunRecord:
    """Run one batch over the given queue item; the batch must settle it."""
    run = rt.orchestrator.start_run(trigger="test", queue_ids=[queue_id], auto_approve_override=True)
    assert run.status == RunStatus.PASS, run.error
    return run


def _first_pass(tmp_path: Path) -> tuple[FlywheelRuntime, Path, str, float]:
    """扫盘 → 入队 → 消费完：返回运行时、transcript、会话键，以及采集时它的修改时间。"""
    rt = _runtime(tmp_path)
    brain = tmp_path / "brain"
    path = _write_transcript(brain, AGY_UUID_QUIET, _conversation())
    scanned = _age(path, 7200)
    rt.cfg.sessions.scan_sources = [_agy_source(brain)]
    key = f"antigravity:{AGY_UUID_QUIET}"

    scan = rt.sessions.reconcile_scan_sources()
    assert (scan["scanned"], scan["newly_registered"], scan["regrown"]) == (1, 1, 0)
    assert _marks(rt, key) == (scanned, path.stat().st_size)
    rt.sessions.sweep_pending_triage()
    assert rt.queue.bridge_pending_sessions_to_queue() == 1
    (first,) = rt.db.list_session_queue_items(key)
    assert (first.revision, first.consumed_from_turn, first.consumed_to_turn) == (1, 0, 5)
    _consume(rt, first.queue_id)

    session = rt.db.get_session(key)
    assert session.status is SessionStatus.PROCESSED
    assert (session.last_consumed_turn, session.last_consumed_hash) == (5, first.content_hash)
    assert _marks(rt, key) == (scanned, path.stat().st_size)
    return rt, path, key, scanned


def _marks(rt: FlywheelRuntime, key: str) -> tuple[float, int]:
    """会话上记下的采集标记：``(修改时间, 大小)``。"""
    meta = rt.db.get_session(key).metadata
    return (meta[SCANNED_MTIME], meta[SCANNED_SIZE])


def test_a_conversation_that_continues_after_it_was_consumed_is_picked_up_again(tmp_path: Path):
    """首次静默入队并消费完 → 同一个 UUID 的 transcript 又多了两轮 → 再次静默：只有新增的轮次以修订 2 入队。"""
    rt, path, key, _ = _first_pass(tmp_path)

    # 还在写入：这一轮扫描不碰它，队列也不多出任何东西。
    _write_jsonl_lines(path, _grown_conversation())
    _age(path, 60)
    scan = rt.sessions.reconcile_scan_sources()
    assert (scan["scanned"], scan["skipped_active"], scan["regrown"]) == (0, 1, 0)
    assert rt.queue.bridge_pending_sessions_to_queue() == 0
    assert rt.sessions.take_grown_transcripts() == []

    # 再次静默：扫盘认出它比上次采集时长了，队列只取消费水位线之后的部分。
    grown = _age(path, 3600)
    scan = rt.sessions.reconcile_scan_sources()
    assert (scan["scanned"], scan["skipped_active"], scan["regrown"]) == (1, 0, 1)
    assert rt.queue.bridge_pending_sessions_to_queue() == 1
    assert rt.sessions.take_grown_transcripts() == []

    revisions = rt.db.list_session_queue_items(key)
    assert [(item.revision, item.status) for item in revisions] == [
        (2, QueueStatus.QUEUED),
        (1, QueueStatus.PROCESSED),
    ]
    second, first = revisions
    assert second.ingress_channel is IngressChannel.LEGACY_SCAN
    assert second.client == "antigravity"
    assert (second.consumed_from_turn, second.consumed_to_turn) == (5, 7)
    assert second.content_hash != first.content_hash
    text = rt.queue.load_queue_item_text(second)
    assert f"[user] {APPENDED_ASK}" in text
    assert f"[assistant] {APPENDED_ANSWER}" in text
    assert ASK not in text and FOLLOW_UP not in text
    assert "TOOL-DUMP-LINE" not in text

    # The marks moved with what was taken; the watermark waits for the batch that settles it.
    session = rt.db.get_session(key)
    assert _marks(rt, key) == (grown, path.stat().st_size)
    assert (session.last_consumed_turn, session.status) == (5, SessionStatus.PROCESSED)

    # The new revision settles like any other, on its own.
    _consume(rt, second.queue_id)
    assert rt.db.get_session(key).last_consumed_turn == 7

    # Nothing grew since: a third pass has nothing to take and makes no third revision.
    scan = rt.sessions.reconcile_scan_sources()
    assert (scan["scanned"], scan["regrown"]) == (1, 0)
    assert rt.queue.bridge_pending_sessions_to_queue() == 0
    assert [item.revision for item in rt.db.list_session_queue_items(key)] == [2, 1]
    assert _marks(rt, key) == (grown, path.stat().st_size)


def test_a_normal_batch_takes_what_was_appended_on_its_own(tmp_path: Path):
    """不靠人工触发：一个普通批次的 P00 自己扫出增长并桥接入队，随后审查、结算新增的轮次。"""
    rt, path, key, _ = _first_pass(tmp_path)
    _age(_write_jsonl_lines(path, _grown_conversation()), 3600)

    run = rt.orchestrator.start_run(trigger="test", auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    assert run.admitted_sessions == [key]
    p00 = next(node for node in run.nodes if node.node_id == "P00_PREPARE")
    assert "桥接入队 1 条" in p00.summary
    assert [(item.revision, item.status) for item in rt.db.list_session_queue_items(key)] == [
        (2, QueueStatus.PROCESSED),
        (1, QueueStatus.PROCESSED),
    ]
    assert rt.db.get_session(key).last_consumed_turn == 7
    assert rt.sessions.take_grown_transcripts() == []


def test_a_transcript_touched_without_growing_makes_no_revision(tmp_path: Path):
    """修改时间变了、对话没变：队列判定内容未变，不出版本；采集标记跟着前移，下次不再重复送。"""
    rt, path, key, _ = _first_pass(tmp_path)
    touched = _age(path, 3600)

    scan = rt.sessions.reconcile_scan_sources()
    assert scan["regrown"] == 1
    assert rt.queue.bridge_pending_sessions_to_queue() == 0
    assert [item.revision for item in rt.db.list_session_queue_items(key)] == [1]
    assert _marks(rt, key) == (touched, path.stat().st_size)

    scan = rt.sessions.reconcile_scan_sources()
    assert (scan["scanned"], scan["regrown"]) == (1, 0)
    assert rt.queue.bridge_pending_sessions_to_queue() == 0


def test_a_transcript_that_kept_its_modification_time_but_not_its_size_is_taken_too(tmp_path: Path):
    """修改时间没变、文件变长了（时钟回拨，或保留时间的同步）：同样算增长。"""
    rt, path, key, scanned = _first_pass(tmp_path)
    _write_jsonl_lines(path, _grown_conversation())
    os.utime(path, (scanned, scanned))
    assert path.stat().st_mtime == scanned

    scan = rt.sessions.reconcile_scan_sources()
    assert (scan["scanned"], scan["regrown"]) == (1, 1)
    assert rt.queue.bridge_pending_sessions_to_queue() == 1
    (second, first) = rt.db.list_session_queue_items(key)
    assert (second.revision, second.status, first.revision) == (2, QueueStatus.QUEUED, 1)
    assert (second.consumed_from_turn, second.consumed_to_turn) == (5, 7)
    assert _marks(rt, key) == (scanned, path.stat().st_size)


def test_a_session_registered_before_the_marks_existed_counts_from_now(tmp_path: Path):
    """升级前登记的会话没有采集标记：第一次扫盘只补上标记，此后的增长照常被采到。"""
    rt, path, key, _ = _first_pass(tmp_path)
    session = rt.db.get_session(key)
    # What a registration of an older build left behind: the session, without any mark.
    session.metadata = {
        name: value for name, value in session.metadata.items() if name not in (SCANNED_MTIME, SCANNED_SIZE)
    }
    rt.db.upsert_session(session)
    assert SCANNED_MTIME not in rt.db.get_session(key).metadata

    # It differs from nothing on record: the scan only starts counting from what it sees now.
    _age(path, 3600)
    scan = rt.sessions.reconcile_scan_sources()
    assert (scan["scanned"], scan["regrown"]) == (1, 0)
    assert _marks(rt, key) == (path.stat().st_mtime, path.stat().st_size)
    assert rt.queue.bridge_pending_sessions_to_queue() == 0

    _age(_write_jsonl_lines(path, _grown_conversation()), 1800)
    scan = rt.sessions.reconcile_scan_sources()
    assert scan["regrown"] == 1
    assert rt.queue.bridge_pending_sessions_to_queue() == 1
    assert [item.revision for item in rt.db.list_session_queue_items(key)] == [2, 1]


@pytest.mark.parametrize("intent", ["none", "governance_only", "opt_out", "skipped"])
def test_a_grown_transcript_keeps_the_intent_the_session_is_under(tmp_path: Path, intent: str):
    """续聊只是对话变长了：它不改变这个会话被冻结、被整体跳过，或只做治理的决定。"""
    rt, path, key, scanned = _first_pass(tmp_path)
    if intent == "governance_only":
        declared = rt.queue.handle_session_intent(
            "hand_distilled", session_key=key, governance_only=True, wiki_paths=[HAND_WRITTEN_NOTE]
        )
        assert declared["distill_mode"] == "governance_only"
        _consume(rt, declared["queue_ids"][0])
    elif intent == "opt_out":
        vetoed = rt.queue.handle_session_intent("opt_out", session_key=key, reason="会话内容不再参与沉淀。")
        assert vetoed["reason_code"] == "USER_OPT_OUT"
    elif intent == "skipped":
        skipped = rt.queue.handle_session_intent(
            "hand_distilled", session_key=key, governance_only=False, wiki_paths=[HAND_WRITTEN_NOTE]
        )
        assert skipped["reason_code"] == HAND_DISTILLED_SKIP
    before = [
        (item.revision, item.status, item.exclude_reason_code)
        for item in rt.db.list_session_queue_items(key)
    ]
    if intent == "opt_out":
        assert before == [(2, QueueStatus.EXCLUDED, "USER_OPT_OUT"), (1, QueueStatus.PROCESSED, "")]
    elif intent == "skipped":
        assert before == [(2, QueueStatus.EXCLUDED, HAND_DISTILLED_SKIP), (1, QueueStatus.PROCESSED, "")]

    grown = _age(_write_jsonl_lines(path, _grown_conversation()), 3600)
    scan = rt.sessions.reconcile_scan_sources()
    bridged = rt.queue.bridge_pending_sessions_to_queue()
    session = rt.db.get_session(key)
    items = rt.db.list_session_queue_items(key)
    shape = [(item.revision, item.status, item.exclude_reason_code) for item in items]
    queued = [item for item in items if item.status is QueueStatus.QUEUED]

    if intent == "opt_out":
        # 冻结的会话连看都不看：没有增长可报，没有新的队列项，标记停在原地。
        assert (scan["regrown"], bridged) == (0, 0)
        assert session.opt_out is True
        assert shape == before
        assert session.metadata[SCANNED_MTIME] == scanned
        return

    assert scan["regrown"] == 1
    assert session.metadata[SCANNED_MTIME] == grown
    if intent == "none":
        assert bridged == 1
        assert shape == [(2, QueueStatus.QUEUED, ""), *before]
        assert "distill_mode" not in queued[0].payload
    elif intent == "governance_only":
        # 新增的轮次沿用治理专属模式与已登记的路径，不会再排一次知识文档的写入。
        assert bridged == 1
        assert shape == [(3, QueueStatus.QUEUED, ""), *before]
        assert queued[0].payload["distill_mode"] == "governance_only"
        assert queued[0].payload["hand_distilled_paths"] == [HAND_WRITTEN_NOTE]
        assert rt.queue.distill_mode_of(session) == "governance_only"
    else:
        # 整体跳过：对话再长也不重新入队，已有的可见记录原样保留。
        assert (bridged, queued) == (0, [])
        assert shape == before
        assert rt.queue.distill_mode_of(session) == "skipped"
        assert session.metadata["hand_distilled_skip"] is True
