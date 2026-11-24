from __future__ import annotations

import re
from dataclasses import dataclass

from ai_flywheel.core.time_utils import (
    FRESHNESS_DAYS,
    compute_valid_until,
    shanghai_iso_week_key,
    shanghai_today_ymd,
)

# The date helpers live in `ai_flywheel.core.time_utils`; they are re-exported here so
# that every existing `from ai_flywheel.core.privacy import ...` keeps working.
__all__ = [
    "FRESHNESS_DAYS",
    "SECRET_PATTERNS",
    "SecretLeakError",
    "SecretPattern",
    "compute_valid_until",
    "redact_secrets",
    "sanitize_and_check_secrets",
    "shanghai_iso_week_key",
    "shanghai_today_ymd",
]

_PRIVATE_TAG_RE = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class SecretPattern:
    """One credential shape the product knows how to mask.

    ``label`` names the credential kind (it is quoted when a write is refused).
    ``credential`` marks the kinds a deployment would have to rotate: those are refused
    at write time. The remaining patterns are masked wherever they appear but never
    refuse a write on their own — a document that merely mentions ``token=`` is not a leak.
    """

    label: str
    pattern: re.Pattern[str]
    credential: bool = True


# The canonical secret patterns. One set, shared by every redaction surface: the
# write-time privacy gate, the receipt store and the Feishu card renderer.
# A pattern whose group 1 holds a readable prefix keeps that prefix when the value is
# masked ("Bearer [REDACTED]"), so a redacted line still says what it referred to.
SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    # Credential kinds: a write carrying one of these is refused, not merely masked.
    SecretPattern("OpenAI/Anthropic API Key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b")),
    SecretPattern("GitHub Personal Access Token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    SecretPattern("GitHub Fine-Grained Token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{24,}\b")),
    SecretPattern("AWS Access Key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    SecretPattern("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{14,}\b")),
    SecretPattern("Private Key Block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    # Masked wherever they appear, never a reason to refuse a write on their own.
    SecretPattern("API Key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{8,}\b"), credential=False),
    SecretPattern("GitHub Legacy Token", re.compile(r"\b(?:gho|ghp|ghu|ghs|ghr)-[A-Za-z0-9_\-]{12,}\b"), credential=False),
    SecretPattern("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), credential=False),
    SecretPattern("Bearer Token", re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9\-._~+/]+=*"), credential=False),
    SecretPattern("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), credential=False),
    SecretPattern(
        "Credential Assignment",
        re.compile(r"(?i)\b((?:api[_-]?key|token|secret|password)\s*[=:]\s*['\"]?)([^\s'\",;]+)"),
        credential=False,
    ),
)


class SecretLeakError(ValueError):
    """Raised when write-time privacy gate detects an unredacted secret/token."""


def redact_secrets(text: str, *, mask: str = "[REDACTED]", keep_prefix: bool = True) -> str:
    """Mask every credential the canonical patterns match.

    ``keep_prefix`` keeps the readable prefix a pattern captured (group 1), so
    ``Bearer [REDACTED]`` still says what was redacted; without it the whole match
    is replaced by the mask.
    """
    redacted = text or ""
    for secret in SECRET_PATTERNS:
        pattern = secret.pattern
        if keep_prefix and pattern.groups:
            redacted = pattern.sub(lambda match, m=mask: (match.group(1) or "") + m, redacted)
        else:
            redacted = pattern.sub(lambda _match, m=mask: m, redacted)
    return redacted


def sanitize_and_check_secrets(
    text: str,
    *,
    context: str = "",
    refuse_secrets: bool = True,
) -> str:
    """Strip <private>...</private> spans and block obvious credential/secret leaks before writing."""
    cleaned = _PRIVATE_TAG_RE.sub("[REDACTED_PRIVATE]", text or "")
    if refuse_secrets:
        for secret in SECRET_PATTERNS:
            if not secret.credential:
                continue
            if secret.pattern.search(cleaned):
                ctx = f" ({context})" if context else ""
                raise SecretLeakError(
                    f"Write blocked by privacy gate{ctx}: detected possible {secret.label}. "
                    "Wrap private spans in <private>...</private> or remove credentials."
                )
    return cleaned
