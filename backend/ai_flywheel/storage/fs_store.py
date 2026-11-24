from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ai_flywheel.core.privacy import SECRET_PATTERNS as CANONICAL_SECRET_PATTERNS
from ai_flywheel.core.privacy import redact_secrets

# The canonical secret patterns, kept here as the raw compiled regexes so that the
# module's historical surface stays intact. `core.privacy` owns the definitions.
SECRET_PATTERNS: list[re.Pattern[str]] = [secret.pattern for secret in CANONICAL_SECRET_PATTERNS]


def redact_text(text: str) -> str:
    """Mask credentials in stored text, keeping the readable prefix of the match."""
    return redact_secrets(text)


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
