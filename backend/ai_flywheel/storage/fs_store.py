from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


SECRET_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"((?:api[_-]?key|token|secret|password)\s*[=:]\s*['\"]?)([^\s'\",;]+)", re.IGNORECASE),
    re.compile(r"\b(gho|ghp|sk)-[A-Za-z0-9_\-]{12,}\b"),
]


def redact_text(text: str) -> str:
    redacted = text
    redacted = SECRET_PATTERNS[0].sub(r"\1[REDACTED]", redacted)
    redacted = SECRET_PATTERNS[1].sub(r"\1[REDACTED]", redacted)
    redacted = SECRET_PATTERNS[2].sub("[REDACTED_TOKEN]", redacted)
    return redacted


def canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class ImmutableFileStore:
    """Windows-safe immutable receipt and artifact snapshot store."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def write_receipt(self, category: str, receipt_id: str, payload: dict[str, Any]) -> tuple[Path, str]:
        digest = canonical_hash(payload)
        short = digest[:12]
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", receipt_id)[:64]
        target_dir = self.root / category
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{safe_id}-{short}.json"
        if not target.exists():
            envelope = {
                "receipt_id": receipt_id,
                "receipt_hash": digest,
                "payload": payload,
            }
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
        return target, digest
