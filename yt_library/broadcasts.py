"""Broadcast availability and deterministic adaptive recheck scheduling."""

from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any
from zoneinfo import ZoneInfo

from .config import configured_broadcast_polling, effective_display_timezone


def effective_broadcast_status(
    status: str | None, availability: str, is_playable: Any
) -> str | None:
    availability = (availability or "").strip().lower()
    unavailable = (
        is_playable == 0
        and availability not in {"public", "unlisted", "subscriber_only"}
    ) or (
        availability
        in {
            "deleted",
            "removed",
            "unavailable",
            "needs_auth",
            "premium_only",
            "private",
        }
        and not (availability == "private" and is_playable == 1)
    )
    return None if status in {"live", "upcoming"} and unavailable else status


def effective_broadcast_status_sql(alias: str = "v") -> str:
    prefix = f"{alias}." if alias else ""
    return f"""CASE WHEN {prefix}broadcast_status IN ('live', 'upcoming') AND (
      ({prefix}is_playable = 0 AND lower(COALESCE({prefix}availability, '')) NOT IN ('public', 'unlisted', 'subscriber_only')) OR (
        lower(COALESCE({prefix}availability, '')) IN
          ('deleted', 'removed', 'unavailable', 'needs_auth', 'premium_only', 'private')
        AND NOT (lower(COALESCE({prefix}availability, '')) = 'private'
                 AND COALESCE({prefix}is_playable, 0) = 1)
      )) THEN NULL ELSE {prefix}broadcast_status END"""


def _instant(value: str | None) -> datetime | None:
    if not value or len(value) <= 10:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def broadcast_recheck_interval_hours(
    status: str,
    started_at: str | None,
    latest_watch: str | None,
    *,
    now: datetime,
    config: dict[str, Any],
) -> int:
    policy = configured_broadcast_polling(config)
    recent = _instant(latest_watch)
    if recent is not None:
        recently_watched = (
            now - timedelta(days=policy["recent_watch_days"]) <= recent <= now
        )
    else:
        local_date = now.astimezone(ZoneInfo(effective_display_timezone(config))).date()
        try:
            watch_date = datetime.strptime(latest_watch or "", "%Y-%m-%d").date()
            recently_watched = (
                local_date - timedelta(days=policy["recent_watch_days"])
                <= watch_date
                <= local_date
            )
        except ValueError:
            recently_watched = False
    if recently_watched:
        return policy["frequent_hours"]
    start = _instant(started_at)
    if status == "upcoming":
        return (
            policy["frequent_hours"]
            if start is None
            or start <= now + timedelta(hours=policy["upcoming_near_hours"])
            else policy["normal_hours"]
        )
    if start is None or start > now:
        return policy["frequent_hours"]
    age = now - start
    if age < timedelta(hours=policy["initial_live_hours"]):
        return policy["frequent_hours"]
    if age <= timedelta(days=policy["established_live_days"]):
        return policy["normal_hours"]
    return policy["long_running_hours"]


def broadcast_recheck_due(
    video_id: str,
    status: str | None,
    started_at: str | None,
    checked_at: str | None,
    latest_watch: str | None,
    *,
    now: datetime,
    config: dict[str, Any],
) -> bool:
    if status not in {"live", "upcoming"}:
        return False
    checked = _instant(checked_at)
    if checked is None:
        return True
    interval = broadcast_recheck_interval_hours(
        status, started_at, latest_watch, now=now, config=config
    )
    # Fixed UTC slots distribute streams across hourly Updates and survive restarts.
    phase = (
        int.from_bytes(hashlib.sha256(video_id.encode()).digest()[:4], "big") % interval
    )
    current_hour = int(now.timestamp() // 3600)
    slot_hour = current_hour - (current_hour - phase) % interval
    return checked.timestamp() < slot_hour * 3600
