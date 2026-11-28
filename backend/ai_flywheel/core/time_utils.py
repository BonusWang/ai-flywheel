"""Shanghai-time helpers shared by the knowledge, skill and Wiki surfaces.

Every date the product writes (freshness stamps, record ids, weekly distillation keys)
is a Shanghai date, not a host date: a machine running in UTC would otherwise write
``2026-01-01`` while the user is already on ``2026-01-02``, and a UTC host would
reject a card dated today as being "in the future".

This module is the single home of that rule; ``ai_flywheel.core.privacy`` re-exports
the names for backwards compatibility.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# The business timezone every product-facing date is computed in.
SHANGHAI_TZ = timezone(timedelta(hours=8))

# How long a knowledge card stays valid per freshness tier (days).
FRESHNESS_DAYS: dict[str, int] = {
    "volatile": 30,
    "evolving": 90,
    "evergreen": 180,
}

# The tier assumed when none is declared.
DEFAULT_FRESHNESS_TIER = "evolving"


def shanghai_now_iso(now_utc: datetime | None = None) -> str:
    """Current instant in Shanghai time as ISO 8601."""
    dt = now_utc or datetime.now(timezone.utc)
    return dt.astimezone(SHANGHAI_TZ).isoformat()


def shanghai_today_ymd(now_utc: datetime | None = None) -> str:
    """Today in Shanghai as ``YYYY-MM-DD``."""
    dt = now_utc or datetime.now(timezone.utc)
    return dt.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d")


def compute_valid_until(tier: str = DEFAULT_FRESHNESS_TIER, verified_ymd: str | None = None) -> str:
    """The date a card verified on ``verified_ymd`` expires, per its freshness tier."""
    days = FRESHNESS_DAYS.get((tier or DEFAULT_FRESHNESS_TIER).strip().lower(), FRESHNESS_DAYS[DEFAULT_FRESHNESS_TIER])
    try:
        base = date.fromisoformat((verified_ymd or "")[:10])
    except ValueError:
        base = date.fromisoformat(shanghai_today_ymd())
    return (base + timedelta(days=days)).isoformat()


def shanghai_iso_week_key(now_utc: datetime | None = None) -> str:
    """The ISO week of the Shanghai date as ``YYYY-Www`` (the weekly distillation key)."""
    dt = now_utc or datetime.now(timezone.utc)
    iso_year, iso_week, _ = dt.astimezone(SHANGHAI_TZ).isocalendar()
    return f"{iso_year}-W{iso_week:02d}"
