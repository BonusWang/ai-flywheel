"""Atomic registration of project practice cards in their project notes index."""
from __future__ import annotations

import hashlib
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import yaml

from ai_flywheel.adapters.wiki.schema import split_frontmatter
from ai_flywheel.core.time_utils import compute_valid_until, shanghai_today_ymd

PRACTICE_INDEX_MARKER = "## P04 自动登记"
PRACTICE_INDEX_FILE = "笔记索引.md"
PRACTICE_INDEX_TYPE = "project-practice-index"
PRACTICE_TYPES = {
    "project-practice-note": "重卡",
    "project-practice-lite": "轻记卡",
}

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_INDEX_SECTION_BOUNDARY_RE = re.compile(r"^##\s+(?:受控词表|加载规则|已归档)", re.MULTILINE)
_TABLE_SEPARATOR_RE = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|)+\s*$", re.MULTILINE)


class PracticeIndexError(ValueError):
    """Raised when a practice card cannot be registered in its project index."""


@dataclass(frozen=True)
class PracticeIndexRegistration:
    target_relative_path: str
    status: str
    entry_markdown: str
    new_content: str
    preimage_sha256: str
    card_relative_path: str

    def side_effect(self) -> dict[str, str]:
        return {
            "kind": "practice_index_entry",
            "target_path": self.target_relative_path,
            "action": "append_entry",
            "status": self.status,
            "entry_preview": self.entry_markdown,
        }

    def receipt(self) -> dict[str, str]:
        return {
            "target_path": self.target_relative_path,
            "status": self.status,
            "entry_markdown": self.entry_markdown,
            "preimage_sha256": self.preimage_sha256,
            "postimage_sha256": _sha256_text(self.new_content),
        }


def plan_practice_index_registration(
    root: Path,
    relative_path: str,
    content: str,
    *,
    today_ymd: str | None = None,
) -> PracticeIndexRegistration | None:
    """Return the exact index mutation a practice-card write needs.

    ``None`` means the document is not a project practice card. A practice card
    without a usable project index is an error: silently writing the card would
    recreate the orphan-page defect this registration exists to prevent.
    """
    rel = _normalize_path(relative_path)
    fm_text, _body = split_frontmatter(content)
    if fm_text is None:
        return None
    try:
        frontmatter = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise PracticeIndexError(f"practice card frontmatter is invalid: {exc}") from None
    if not isinstance(frontmatter, dict):
        return None

    page_type = str(frontmatter.get("type") or "").strip()
    if page_type not in PRACTICE_TYPES:
        return None
    segments = rel.split("/")
    try:
        practice_segment = segments.index("实战笔记")
    except ValueError:
        return None
    if practice_segment < 1:
        return None

    index_rel = "/".join([*segments[: practice_segment + 1], PRACTICE_INDEX_FILE])
    index_path = (root / index_rel).resolve()
    root_resolved = root.resolve()
    if root_resolved not in index_path.parents:
        raise PracticeIndexError(f"practice index escapes the knowledge root: {index_rel}")
    if not index_path.is_file():
        raise PracticeIndexError(f"practice card has no project notes index: {index_rel}")

    with index_path.open("r", encoding="utf-8", newline="") as handle:
        index_text = handle.read()
    index_fm_text, _index_body = split_frontmatter(index_text)
    if index_fm_text is None:
        raise PracticeIndexError(f"project notes index has no frontmatter: {index_rel}")
    try:
        index_fm = yaml.safe_load(index_fm_text)
    except yaml.YAMLError as exc:
        raise PracticeIndexError(f"project notes index frontmatter is invalid: {index_rel}: {exc}") from None
    if not isinstance(index_fm, dict) or str(index_fm.get("type") or "").strip() != PRACTICE_INDEX_TYPE:
        raise PracticeIndexError(
            f"project notes index must declare type={PRACTICE_INDEX_TYPE}: {index_rel}"
        )

    card_target = rel[:-3] if rel.endswith(".md") else rel
    preimage_sha = _sha256_text(index_text)
    if _index_contains_card(index_text, card_target):
        return PracticeIndexRegistration(
            target_relative_path=index_rel,
            status="already_present",
            entry_markdown="",
            new_content=index_text,
            preimage_sha256=preimage_sha,
            card_relative_path=rel,
        )

    verification_ymd = today_ymd or shanghai_today_ymd()
    entry_date = _entry_date(frontmatter, today_ymd=verification_ymd)
    title = _safe_title(str(frontmatter.get("title") or _first_h1(content) or Path(rel).stem))
    entry = (
        f"| {entry_date} | {PRACTICE_TYPES[page_type]} | "
        f"[[{card_target}|{title}]] |"
    )
    new_content = _insert_entry(
        index_text,
        entry,
        today_ymd=verification_ymd,
        freshness_tier=str(index_fm.get("freshness_tier") or "evolving"),
    )
    if new_content == index_text:
        raise PracticeIndexError(f"practice index registration produced no change: {index_rel}")
    return PracticeIndexRegistration(
        target_relative_path=index_rel,
        status="update",
        entry_markdown=entry,
        new_content=new_content,
        preimage_sha256=preimage_sha,
        card_relative_path=rel,
    )


def approved_registration_matches(
    registration: PracticeIndexRegistration,
    approved_side_effects: list[dict[str, object]] | None,
) -> bool:
    if registration.status != "update":
        return True
    for effect in approved_side_effects or []:
        if not isinstance(effect, dict):
            continue
        if (
            effect.get("kind") == "practice_index_entry"
            and effect.get("target_path") == registration.target_relative_path
            and effect.get("action") == "append_entry"
            and effect.get("status") == "update"
            and effect.get("entry_preview") == registration.entry_markdown
        ):
            return True
    return False


@contextmanager
def practice_index_write_lock(index_path: Path, *, timeout_seconds: float = 5.0) -> Iterator[None]:
    """Serialize index read-modify-write across threads and Flywheel processes.

    The lock is a transient sibling file created with ``O_EXCL``. A crashed writer
    cannot block later runs forever: a lock older than ten minutes is treated as
    stale. The data write remains an atomic temp-file replacement.
    """
    lock_path = index_path.with_name(f".{index_path.name}.p04.lock")
    deadline = time.monotonic() + timeout_seconds
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > 600
            except FileNotFoundError:
                continue
            if stale:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise PracticeIndexError(f"timed out waiting for practice index lock: {index_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _insert_entry(index_text: str, entry: str, *, today_ymd: str, freshness_tier: str) -> str:
    fm_text, body = split_frontmatter(index_text)
    if fm_text is None:
        raise PracticeIndexError("project notes index has no frontmatter")
    fm_text = _replace_scalar(fm_text, "updated", today_ymd)
    fm_text = _replace_scalar(fm_text, "last_verified", today_ymd)
    fm_text = _replace_scalar(
        fm_text,
        "valid_until",
        compute_valid_until(freshness_tier, today_ymd),
    )

    if PRACTICE_INDEX_MARKER in body:
        marker_at = body.index(PRACTICE_INDEX_MARKER)
        separator = _TABLE_SEPARATOR_RE.search(body, marker_at)
        if separator is None:
            raise PracticeIndexError(
                f"{PRACTICE_INDEX_MARKER} section exists without a Markdown table"
            )
        insert_at = separator.end()
        body = body[:insert_at] + "\n" + entry + body[insert_at:]
    else:
        block = (
            f"{PRACTICE_INDEX_MARKER}\n\n"
            "| 日期 | 类型 | 经验卡 |\n"
            "|---|---|---|\n"
            f"{entry}\n"
        )
        boundary = _INDEX_SECTION_BOUNDARY_RE.search(body)
        if boundary:
            prefix = body[: boundary.start()]
            suffix = body[boundary.start() :]
            body = prefix.rstrip() + "\n\n" + block + "\n" + suffix.lstrip("\n")
        else:
            body = body.rstrip() + "\n\n" + block

    normalized_fm = fm_text.replace("\r\n", "\n").rstrip()
    normalized_body = body.replace("\r\n", "\n").lstrip("\n")
    return "---\n" + normalized_fm + "\n---\n" + normalized_body


def _replace_scalar(frontmatter_text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}:\s*.*$", re.MULTILINE)
    replacement = f"{key}: {value}"
    if pattern.search(frontmatter_text):
        return pattern.sub(replacement, frontmatter_text, count=1)
    return frontmatter_text.rstrip() + f"\n{replacement}\n"


def _index_contains_card(index_text: str, card_target: str) -> bool:
    target_norm = card_target.replace("\\", "/").removesuffix(".md")
    stem = target_norm.rsplit("/", 1)[-1]
    for match in _WIKILINK_RE.finditer(index_text):
        target = match.group(1).strip().replace("\\", "/").removesuffix(".md")
        if target == target_norm or target.rsplit("/", 1)[-1] == stem:
            return True
    return False


def _entry_date(frontmatter: dict, *, today_ymd: str | None) -> str:
    for key in ("event_date", "created", "updated"):
        value = str(frontmatter.get(key) or "").strip()[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return value
    return today_ymd or shanghai_today_ymd()


def _first_h1(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _safe_title(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", title).strip() or "未命名经验卡"
    return (
        cleaned.replace("|", "｜")
        .replace("[", "［")
        .replace("]", "］")
        .replace("\n", " ")
    )


def _normalize_path(relative_path: str) -> str:
    rel = (relative_path or "").replace("\\", "/").lstrip("/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
