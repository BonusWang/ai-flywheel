"""Generic Webhook notification channel.

The webhook is the outbound channel that is not tied to one vendor: one JSON
POST per event. Two properties matter more than the transport.

* **Nothing leaves unmasked.** Every outbound string passes the shared privacy
  gate (``core/privacy.py``): ``<private>`` spans are stripped and every
  credential shape in ``SECRET_PATTERNS`` is masked *before* the body is
  serialized. The channel never re-implements the pattern set.
* **A notification never takes a batch down.** A refused connection, a timeout,
  an invalid URL or an HTTP 5xx is reported in the structured result, never
  raised. The run record and the ask card stay the source of truth; the webhook
  is a courtesy.

When a signing secret is configured, the request carries
``X-Flywheel-Signature: sha256=<hex>`` computed over the exact UTF-8 JSON body.
The secret itself is never logged, echoed back or returned in the result.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx

from ai_flywheel import __version__
from ai_flywheel.config import WebhookDeliveryConfig
from ai_flywheel.core.privacy import redact_secrets, sanitize_and_check_secrets
from ai_flywheel.models import PlannedWrite, WorkflowRunRecord, is_sensitive_write, utc_now_iso

#: The event kinds a webhook payload may carry.
EVENT_TYPES = frozenset({"awaiting_approval", "run_completed", "run_failed", "fyi"})

RESULT_CHANNEL = "webhook"


def sanitize_outbound(text: str) -> str:
    """Strip ``<private>`` spans, then mask every credential shape.

    ``sanitize_and_check_secrets(refuse_secrets=False)`` is the same gate the
    write path uses, minus the refusal: an outbound notice masks rather than
    blocks, because a batch must not fail over a webhook string.
    """
    cleaned = sanitize_and_check_secrets(text or "", context="webhook", refuse_secrets=False)
    return redact_secrets(cleaned)


def _read(write: PlannedWrite | dict[str, Any], key: str) -> Any:
    if isinstance(write, PlannedWrite):
        return getattr(write, key)
    return write.get(key)


def _compact_write(write: PlannedWrite | dict[str, Any]) -> dict[str, Any]:
    """One planned write, reduced to what a webhook receiver needs, fully sanitized."""
    relative_path = _read(write, "relative_path")
    return {
        "action": str(_read(write, "action") or ""),
        "target_kind": str(_read(write, "target_kind") or ""),
        "title": sanitize_outbound(str(_read(write, "title") or "")),
        "relative_path": sanitize_outbound(str(relative_path)) if relative_path else None,
        "sensitive": is_sensitive_write(write),
        "approval_reason": sanitize_outbound(str(_read(write, "approval_reason") or "")),
    }


def build_webhook_payload(
    *,
    event_type: str,
    run_id: str = "",
    status: str = "",
    title: str = "",
    summary: str = "",
    portal_url: str = "",
    reasons: list[str] | None = None,
    planned_writes: list[PlannedWrite | dict[str, Any]] | None = None,
    sent_at: str | None = None,
) -> dict[str, Any]:
    """The wire payload: every string sanitized, every planned write compacted."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown webhook event_type: {event_type!r}")
    return {
        "event_type": event_type,
        "run_id": run_id,
        "status": status,
        "title": sanitize_outbound(title),
        "summary": sanitize_outbound(summary),
        "portal_url": sanitize_outbound(portal_url),
        "reasons": [sanitize_outbound(reason) for reason in (reasons or [])],
        "planned_writes": [_compact_write(write) for write in (planned_writes or [])],
        "sent_at": sent_at or utc_now_iso(),
    }


def sign_body(secret: str, body: bytes) -> str:
    """The ``X-Flywheel-Signature`` value for one raw body."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _result(sent: bool, status_code: int | None, reason: str) -> dict[str, Any]:
    return {"channel": RESULT_CHANNEL, "sent": sent, "status_code": status_code, "reason": reason}


def send_webhook_notification(
    cfg: WebhookDeliveryConfig,
    payload: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """POST one payload, returning ``{channel, sent, status_code, reason}``.

    ``client`` lets a caller (and the tests) supply a transport; when omitted a
    short-lived client with the configured timeout is created and closed here.
    A transport failure is reported, never raised.
    """
    if not cfg.enabled:
        return _result(False, None, "webhook disabled")
    url = (cfg.url or "").strip()
    if not url:
        return _result(False, None, "webhook url is empty")

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"ai-flywheel/{__version__}",
    }
    if cfg.secret:
        headers["X-Flywheel-Signature"] = sign_body(cfg.secret, body)

    owns_client = client is None
    http = client or httpx.Client(timeout=float(cfg.timeout_seconds))
    try:
        response = http.post(url, content=body, headers=headers)
    except Exception as exc:  # a notification must never take a batch down
        return _result(False, None, sanitize_outbound(f"{type(exc).__name__}: {exc}"))
    finally:
        if owns_client:
            http.close()

    if response.is_success:
        return _result(True, response.status_code, "")
    return _result(False, response.status_code, f"HTTP {response.status_code}")


class WebhookChannel:
    """A webhook bound to one configuration; the gate on routine notices lives here."""

    def __init__(self, cfg: WebhookDeliveryConfig, *, client: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self._client = client

    def notify(
        self,
        *,
        event_type: str,
        run: WorkflowRunRecord | None = None,
        run_id: str = "",
        status: str = "",
        title: str = "",
        summary: str = "",
        portal_url: str = "",
        reasons: list[str] | None = None,
        planned_writes: list[PlannedWrite | dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Send one event; routine ``fyi`` notices are opt-in via ``notify_on_fyi``."""
        if not self.cfg.enabled:
            return _result(False, None, "webhook disabled")
        if event_type == "fyi" and not self.cfg.notify_on_fyi:
            return _result(False, None, "notify_on_fyi disabled")
        payload = build_webhook_payload(
            event_type=event_type,
            run_id=run_id or (run.run_id if run is not None else ""),
            status=status or (run.status.value if run is not None else ""),
            title=title,
            summary=summary,
            portal_url=portal_url,
            reasons=reasons,
            planned_writes=planned_writes,
        )
        return send_webhook_notification(self.cfg, payload, client=self._client)
