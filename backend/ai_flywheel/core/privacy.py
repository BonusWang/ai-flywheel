from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

_PRIVATE_TAG_RE = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI/Anthropic API Key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("GitHub Personal Access Token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("GitHub Fine-Grained Token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{24,}\b")),
    ("AWS Access Key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{14,}\b")),
    ("Private Key Block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
]

FRESHNESS_DAYS: dict[str, int] = {
    "volatile": 30,
    "evolving": 90,
    "evergreen": 180,
}


class SecretLeakError(ValueError):
    """Raised when write-time privacy gate detects an unredacted secret/token."""


def sanitize_and_check_secrets(
    text: str,
    *,
    context: str = "",
    refuse_secrets: bool = True,
) -> str:
    """Strip <private>...</private> spans and block obvious credential/secret leaks before writing."""
    cleaned = _PRIVATE_TAG_RE.sub("[REDACTED_PRIVATE]", text or "")
    if refuse_secrets:
        for label, pat in _SECRET_PATTERNS:
            if pat.search(cleaned):
                ctx = f" ({context})" if context else ""
                raise SecretLeakError(
                    f"Write blocked by privacy gate{ctx}: detected possible {label}. "
                    "Wrap private spans in <private>...</private> or remove credentials."
                )
    return cleaned


def shanghai_today_ymd(now_utc: datetime | None = None) -> str:
    dt = now_utc or datetime.now(timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def compute_valid_until(tier: str = "evolving", verified_ymd: str | None = None) -> str:
    days = FRESHNESS_DAYS.get((tier or "evolving").strip().lower(), 90)
    try:
        base = date.fromisoformat((verified_ymd or "")[:10])
    except ValueError:
        base = date.fromisoformat(shanghai_today_ymd())
    return (base + timedelta(days=days)).isoformat()
