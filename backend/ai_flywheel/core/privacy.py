from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ai_flywheel.core.time_utils import (
    FRESHNESS_DAYS,
    compute_valid_until,
    shanghai_iso_week_key,
    shanghai_today_ymd,
)

# The date helpers live in `ai_flywheel.core.time_utils`; they are re-exported here so
# that every existing `from ai_flywheel.core.privacy import ...` keeps working.
__all__ = [
    "BRIEF_REDACTED",
    "BRIEF_TRUNCATED",
    "CJK_CREDENTIAL_ASSIGNMENT",
    "FRESHNESS_DAYS",
    "MAX_BRIEF_DEPTH",
    "MAX_BRIEF_ITEMS",
    "MAX_BRIEF_VALUE_CHARS",
    "SECRET_PATTERNS",
    "SecretLeakError",
    "SecretPattern",
    "compute_valid_until",
    "find_secret_shape",
    "has_private_tags",
    "key_names_credential",
    "mask_credential_shapes",
    "redact_secrets",
    "redact_structured_brief",
    "refuse_credential_shapes",
    "sanitize_and_check_secrets",
    "shanghai_iso_week_key",
    "shanghai_today_ymd",
    "strip_private_tags",
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

# The same credential assignment written with the Chinese keywords a Chinese-language Wiki
# uses, read only by the strict check below. It stays out of SECRET_PATTERNS on purpose: as a
# masking rule it would also mask ordinary sentences such as 「密钥：统一走环境变量注入」 on
# every outbound surface. Its value holds no Han character, so a phrase is never matched.
CJK_CREDENTIAL_ASSIGNMENT = SecretPattern(
    "Credential Assignment",
    re.compile(r"((?:密码|口令|私钥|密钥|令牌)\s*[=:：]\s*['\"“]?)([^\s'\"“”<>{},，;；|$一-鿿]{4,})"),
    credential=False,
)
# A value that says of itself that it is not the credential: masked, redacted, a run of stars,
# or a plain yes/no. A value that is a phrase in Chinese is not a credential either.
_NOT_A_SECRET_RE = re.compile(r"(?i)redact|masked|\*{3,}|x{4,}|^(?:true|false|null|none|n/a|na|no|yes)[.!]?$")
# A value that names where the credential lives instead of holding it: a variable reference.
_REFERENCE_VALUE_RE = re.compile(r"^[`'\"“]?(?:\$|%|\{\{|<)")
# The credential itself: from the start of the value up to the first space or CJK character.
# Prose may follow a credential with nothing but a full-width comma between them.
_VALUE_TOKEN_RE = re.compile(r"[^\s'\"“”<>{},，;；|$一-鿿]+")
_HAN_RE = re.compile(r"[一-鿿]")
# Shorter than this, a value is a flag or an abbreviation, not something that opens a door.
_MIN_TOKEN_LEN = 4


class SecretLeakError(ValueError):
    """Raised when write-time privacy gate detects an unredacted secret/token."""


def _is_credential_value(secret: SecretPattern, match: re.Match[str]) -> bool:
    """Whether a match is a credential rather than a word that merely looks like one.

    The strict check reads kinds that are masked-only in the canonical set, so it tells a
    value from a mention: ``Bearer token 鉴权`` and ``密码：统一走环境变量注入`` describe a
    credential, ``Bearer eyJhbGciOi…`` and ``密码：Hk9-Qp31`` carry one.
    """
    if secret.credential:
        return True
    if secret.label == "Bearer Token":
        token = match.group(0)[len(match.group(1) or ""):]
        return len(token) >= 20
    if secret.pattern.groups < 2:
        return True  # a fixed shape (a key or a token prefix): there is no value to read as a phrase
    token = _VALUE_TOKEN_RE.match(match.group(2) or "")
    value = token.group(0) if token else ""
    return (
        len(value) >= _MIN_TOKEN_LEN
        and not _HAN_RE.search(value)
        and not _NOT_A_SECRET_RE.search(value)
        and not _REFERENCE_VALUE_RE.match(value)
    )


def find_secret_shape(text: str, *, strict: bool = False) -> str | None:
    """The label of the first credential shape in ``text``, or None when there is none.

    The default reads the canonical set as the write gate does. ``strict`` adds the shapes
    a draft may merely mention (a masked-only kind, a Chinese credential assignment): text
    that is about to enter a configuration page is held to that, because a password, a
    token or a key has no business being there in any spelling.
    """
    scanned = text or ""
    for secret in (*SECRET_PATTERNS, CJK_CREDENTIAL_ASSIGNMENT) if strict else SECRET_PATTERNS:
        if not strict and not secret.credential:
            continue
        # Every match is read: a line that only mentions a credential must not hide the one
        # after it that carries it.
        for match in secret.pattern.finditer(scanned):
            if _is_credential_value(secret, match):
                return secret.label
    return None


def refuse_credential_shapes(text: str, *, context: str = "") -> None:
    """Raise ``SecretLeakError`` when ``text`` carries a credential in any shape the set knows."""
    label = find_secret_shape(text, strict=True)
    if label:
        ctx = f" ({context})" if context else ""
        raise SecretLeakError(
            f"Write blocked by privacy gate{ctx}: detected possible {label}. "
            "A configuration page records where a credential is kept, never the credential itself."
        )


def mask_credential_shapes(text: str) -> str:
    """``redact_secrets``, over the Chinese credential assignment as well."""
    redacted = redact_secrets(text)
    return CJK_CREDENTIAL_ASSIGNMENT.pattern.sub(
        lambda match: (match.group(1) or "") + "[REDACTED]"
        if _is_credential_value(CJK_CREDENTIAL_ASSIGNMENT, match)
        else match.group(0),
        redacted,
    )


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


def has_private_tags(text: str) -> bool:
    """Whether ``text`` contains a ``<private>...</private>`` block."""
    return bool(_PRIVATE_TAG_RE.search(text or ""))


def strip_private_tags(text: str) -> str:
    """Replace every ``<private>...</private>`` block with ``[REDACTED_PRIVATE]``."""
    return _PRIVATE_TAG_RE.sub("[REDACTED_PRIVATE]", text or "")


def sanitize_and_check_secrets(
    text: str,
    *,
    context: str = "",
    refuse_secrets: bool = True,
) -> str:
    """Strip <private>...</private> spans and block obvious credential/secret leaks before writing."""
    cleaned = strip_private_tags(text)
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


#: A structured brief is evidence for the review, not a second transcript: every
#: string is capped, and so is each list.
MAX_BRIEF_VALUE_CHARS = 2000
MAX_BRIEF_ITEMS = 50
MAX_BRIEF_DEPTH = 6

#: What a value under a key that names a credential becomes, and what a value nested deeper
#: than a brief is read becomes.
BRIEF_REDACTED = "[REDACTED]"
BRIEF_TRUNCATED = "[TRUNCATED]"

#: The words by which a key of a structured brief names a credential. A key is read word by
#: word, so ``db_password``, ``DB-PASSWORD``, ``dbPassword`` and ``passwords`` all say password.
#: Redacted one string at a time, ``{"db_password": "…"}`` would keep its value: the pair is
#: not the assignment ``password=…`` that the text patterns read.
_CREDENTIAL_KEY_WORDS = frozenset(
    {
        "password",
        "passwords",
        "passwd",
        "passphrase",
        "passcode",
        "pwd",
        "secret",
        "secrets",
        "token",
        "tokens",
        "credential",
        "credentials",
        "authorization",
        "bearer",
        "jwt",
        "apikey",
        "privatekey",
        "accesskey",
    }
)
#: …and the same, spelled with a separator: ``api_key``, ``private-key``, ``access key``,
#: ``signing key``. A primary key and a sort key are not among them.
_CREDENTIAL_KEY_PREFIXES = frozenset(
    {"api", "private", "access", "ssh", "signing", "encryption", "license", "master"}
)
#: ``token`` is also how a model's usage is spelled: these keys are figures, not credentials.
_TOKEN_FIGURE_KEYS = frozenset(
    {
        "max tokens",
        "min tokens",
        "maximum tokens",
        "minimum tokens",
        "input tokens",
        "output tokens",
        "total tokens",
        "prompt tokens",
        "completion tokens",
        "context tokens",
        "cached tokens",
        "reasoning tokens",
        "token count",
        "token usage",
        "token limit",
        "token budget",
        "tokens used",
    }
)
#: The same words in Chinese. Chinese does not separate its words, so they are read anywhere
#: in the key: ``数据库密码`` and ``登录口令`` name a credential as clearly as ``password``.
_CJK_CREDENTIAL_KEY_WORDS = ("密码", "口令", "私钥", "密钥", "秘钥", "令牌", "凭据", "凭证")

#: A key as words: separators split, and so does the edge where a word starts with a capital.
_KEY_WORDS_SPLIT_RE = re.compile(
    r"[^0-9A-Za-z一-鿿]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)


def key_names_credential(key: Any) -> bool:
    """Whether a dictionary key names a credential: ``password``, ``api_key``, ``数据库密码`` …"""
    text = str(key)
    if any(word in text for word in _CJK_CREDENTIAL_KEY_WORDS):
        return True
    words = [word.lower() for word in _KEY_WORDS_SPLIT_RE.split(text) if word]
    phrase = " ".join(words)
    for index, word in enumerate(words):
        if word in _CREDENTIAL_KEY_WORDS:
            if word in {"token", "tokens"} and phrase in _TOKEN_FIGURE_KEYS:
                continue
            return True
        if word in {"key", "keys"} and index and words[index - 1] in _CREDENTIAL_KEY_PREFIXES:
            return True
    return False


def _mask_brief_text(text: str, limit: int = MAX_BRIEF_VALUE_CHARS) -> str:
    """A string of a brief, capped: stripped and masked over the whole string first, so that
    the cap can never cut a credential into a shape no pattern knows."""
    return mask_credential_shapes(strip_private_tags(text))[:limit]


def _redact_brief_value(value: Any, *, depth: int, credential: bool = False) -> Any:
    """One value of a structured brief, redacted and reduced to JSON-safe data."""
    if credential:
        return None if value is None else BRIEF_REDACTED
    if depth > MAX_BRIEF_DEPTH:
        return BRIEF_TRUNCATED
    if isinstance(value, str):
        return _mask_brief_text(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # NaN and infinity are not JSON: a number the transports cannot carry is dropped.
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, (list, tuple)):
        return [_redact_brief_value(entry, depth=depth + 1) for entry in list(value)[:MAX_BRIEF_ITEMS]]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, entry in list(value.items())[:MAX_BRIEF_ITEMS]:
            key = _mask_brief_text(str(raw_key), limit=120)
            cleaned[key] = _redact_brief_value(
                entry, depth=depth + 1, credential=key_names_credential(raw_key)
            )
        return cleaned
    return _mask_brief_text(str(value))


def redact_structured_brief(brief: Any) -> dict[str, Any]:
    """A client's structured brief as redacted, JSON-safe data (``{}`` when there is none).

    Single source of truth shared by queue ingress and P02 prompt rendering: every string
    goes through ``strip_private_tags`` + ``mask_credential_shapes`` (keys too), and
    whatever sits under a key that names a credential is masked as a whole — scalars,
    numbers, lists and nested dicts alike.
    """
    if not isinstance(brief, dict):
        return {}
    cleaned = _redact_brief_value(brief, depth=1)
    return cleaned if isinstance(cleaned, dict) else {}
