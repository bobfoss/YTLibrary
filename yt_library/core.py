#!/usr/bin/env python3
"""Import YouTube library data and browse it locally."""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import html
import http.cookiejar
import http.server
import importlib.metadata
import io
import json
import mimetypes
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import (
    configured_proxy,
    effective_display_timezone,
)
from .network import (
    proxy_unavailable_error,
    socks5_proxy_handlers,
    ytdlp_proxy_options,
)
from .database import (
    CHANNEL_NOTIFICATION_CAPTURE_START as CHANNEL_NOTIFICATION_CAPTURE_START,
    CHANNEL_SUBSCRIPTION_CAPTURE_START as CHANNEL_SUBSCRIPTION_CAPTURE_START,
    PLAYLIST_METADATA_CAPTURE_START as PLAYLIST_METADATA_CAPTURE_START,
    SCHEMA as SCHEMA,
    SCHEMA_VERSION as SCHEMA_VERSION,
    VIDEO_VISIBILITY_CAPTURE_START as VIDEO_VISIBILITY_CAPTURE_START,
    connect,
    delete_playlist_and_orphaned_unwatched_videos,
    migrate_database as migrate_database,
    worker_queue_order_sql,
)
from .history import history_match_type_for_identity, history_source_type_for_identity
from .request_pacing import (
    RequestPacer as RequestPacer,
    configure_request_pacing as configure_request_pacing,
    is_archivarix_request_url as is_archivarix_request_url,
    is_youtube_request_url as is_youtube_request_url,
    open_with_request_pacing as open_with_request_pacing,
    pace_outbound_request as pace_outbound_request,
    request_paced_youtube_dl as request_paced_youtube_dl,
    request_url_matches_hosts as request_url_matches_hosts,
)
from .time_utils import utc_days_ago, utc_now
from .worker_runs import WorkerRunRecorder

if TYPE_CHECKING:
    from .workers import (
        LiveHistoryWorker,
        MetadataWorker,
        PlaceholderRecoveryWorker,
        PlaylistScanWorker,
        WorkerQueueDispatcher,
    )

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_THUMB_DIR = ROOT / "thumbs"
DEFAULT_ARCHIVARIX_THUMB_DIR = ROOT / "archivarix_thumbs"
DEFAULT_VIDEO_THUMB_DIR = ROOT / "video_thumbs"
ARCHIVARIX_COOKIE_FILE = ROOT / "archivarix_cookies.txt"
HISTORY_BATCH_SIZE = 1000
RECENT_HISTORY_BATCH_SIZE = 200
HISTORY_BATCH_DELAY_SECONDS = 10.0
RECENT_HISTORY_OVERLAP_DAYS = 2
DEFAULT_DISPLAY_TIMEZONE = "UTC"


PLAYLIST_MATCH_TYPE_NOTES = {
    "ambiguous_hidden_candidate": "missing from current playable scan; hidden slot mapping is ambiguous",
    "ambiguous_hidden_slot": "current hidden slot has no exposed video ID",
    "inferred_hidden_slot": "matched hidden current slot to missing Takeout video by ordered equal counts",
}
PLAYLIST_MATCH_TYPE_LABELS = {
    "ambiguous_hidden_candidate": "Takeout candidate",
    "ambiguous_hidden_slot": "hidden slot",
    "inferred_hidden_slot": "restored from Takeout",
}

HISTORY_SOURCE_TYPE_LABELS = {
    "takeout_my_activity_youtube": "Takeout + My Activity + YouTube",
    "takeout_my_activity": "Takeout + My Activity",
    "takeout_youtube": "Takeout + YouTube",
    "my_activity_youtube": "My Activity + YouTube",
    "my_activity": "My Activity",
    "takeout": "Takeout",
    "youtube": "YouTube",
}
HISTORY_MATCH_TYPE_LABELS = {
    "video_id_time_date": "matched by video/time and video/date",
    "video_id_time": "matched by video/time",
    "video_id_date": "matched by video/date",
    "my_activity_only": "My Activity only",
    "takeout_only": "Takeout only",
    "youtube_only": "YouTube only",
}
HISTORY_TIME_QUALITY_LABELS = {
    "exact": "exact time",
    "date_only": "date only",
    "unknown": "time unknown",
}
HISTORY_TIME_QUALITY_NOTES = {
    "unknown": "YouTube history entry had no watch date; observed_at is fetch time",
}

VIDEO_SOURCE_PRIORITY = {
    "": 0,
    "archivarix": 10,
    "takeout": 20,
    "my_activity": 25,
    "youtube_history": 30,
    "playlist": 40,
    "youtube_data_api": 45,
    "youtube": 50,
    "metadata": 50,
}

LIKED_VIDEOS_PLAYLIST_ID = "LL"
@dataclass(frozen=True)
class GroupNode:
    key: str
    name: str
    parent_key: str | None
    position: int
    icon: str


class ArchivarixQuotaExceeded(RuntimeError):
    """Raised when Archivarix reports the account has exhausted its daily search quota."""


def normalize_playlist_visibility(value: str) -> str:
    normalized = re.sub(r"\s+", " ", (value or "").strip()).lower()
    if normalized.endswith(" playlist"):
        normalized = normalized[: -len(" playlist")].strip()
    return normalized if normalized in {"private", "public", "unlisted"} else ""


def split_playlist_owner_visibility(value: str) -> tuple[str, str]:
    visibility = normalize_playlist_visibility(value)
    return ("", visibility) if visibility else ((value or "").strip(), "")


def playlist_match_type_note(match_type: str) -> str:
    return PLAYLIST_MATCH_TYPE_NOTES.get(match_type or "", "")


def playlist_match_type_label(match_type: str) -> str:
    return PLAYLIST_MATCH_TYPE_LABELS.get(match_type or "", "")


def history_source_type_label(source_type: str) -> str:
    return HISTORY_SOURCE_TYPE_LABELS.get(source_type or "", source_type or "")


def history_match_type_label(match_type: str) -> str:
    return HISTORY_MATCH_TYPE_LABELS.get(match_type or "", match_type or "")


def history_time_quality_label(time_quality: str) -> str:
    return HISTORY_TIME_QUALITY_LABELS.get(time_quality or "", time_quality or "")


def history_time_quality_note(time_quality: str) -> str:
    return HISTORY_TIME_QUALITY_NOTES.get(time_quality or "", "")


def video_availability_from_youtube_status(status: str) -> str:
    status = (status or "").strip()
    status_upper = status.upper()
    if status_upper == "LIVE":
        return "public"
    if status_upper == "NOT_FOUND" or status_upper.startswith("DELETED_"):
        return "unavailable"
    return ""


def normalize_video_availability(
    video_id: str,
    availability: str = "",
    is_playable: bool | int | None = None,
    youtube_status: str = "",
) -> str:
    if not video_id:
        return "unknown"
    availability = (availability or "").strip()
    legacy_note_values = {
        "Takeout candidate; current hidden slot match is ambiguous",
        *PLAYLIST_MATCH_TYPE_NOTES.values(),
    }
    if availability in legacy_note_values:
        availability = ""
    lowered = availability.lower()
    if lowered == "unavailable video is hidden":
        return "unavailable"
    if lowered == "live":
        return "public"
    if availability:
        return lowered
    status_availability = video_availability_from_youtube_status(youtube_status)
    if status_availability:
        return status_availability
    if is_playable:
        return "public"
    if is_playable is False or is_playable == 0:
        return "unavailable"
    return "unknown"


def video_availability_category(item: Mapping[str, Any]) -> str:
    if not item.get("video_id"):
        return "unavailable"
    availability = str(item.get("availability") or "").strip().lower()
    if availability == "subscriber_only":
        return "members_only"
    if availability in {"public", "unlisted"}:
        return availability
    if availability == "private":
        return (
            "private"
            if item.get("is_playable") is True or item.get("is_playable") == 1
            else "unavailable"
        )
    if availability in {
        "deleted",
        "removed",
        "unavailable",
        "needs_auth",
        "premium_only",
    }:
        return "unavailable"
    if item.get("is_playable") is True or item.get("is_playable") == 1:
        return "public"
    if item.get("is_playable") is False or item.get("is_playable") == 0:
        return "unavailable"
    return "unknown"


def video_availability_category_sql(
    *,
    video_id: str = "video_id",
    availability: str = "availability",
    is_playable: str = "is_playable",
) -> str:
    return f"""
        CASE
          WHEN COALESCE({video_id}, '') = '' THEN 'unavailable'
          WHEN lower(COALESCE({availability}, '')) = 'subscriber_only'
            THEN 'members_only'
          WHEN lower(COALESCE({availability}, '')) IN ('public', 'unlisted')
            THEN lower({availability})
          WHEN lower(COALESCE({availability}, '')) = 'private'
           AND {is_playable} = 1
            THEN 'private'
          WHEN lower(COALESCE({availability}, '')) IN (
            'private', 'deleted', 'removed', 'unavailable', 'needs_auth', 'premium_only'
          ) THEN 'unavailable'
          WHEN {is_playable} = 1 THEN 'public'
          WHEN {is_playable} = 0 THEN 'unavailable'
          ELSE 'unknown'
        END
    """


def playlist_entry_is_unavailable(title: str, availability: str = "") -> bool:
    normalized_title = (title or "").strip().lower().strip("[]() ")
    if normalized_title in {"deleted video", "private video", "unavailable video"}:
        return True
    return (availability or "").strip().lower() in {
        "private",
        "needs_auth",
        "premium_only",
        "subscriber_only",
        "unavailable",
        "deleted",
    }


def watch_playability_value(metadata: dict[str, Any]) -> int | None:
    status = str(metadata.get("playability_status") or metadata.get("yt_status") or "").strip().upper()
    if not status:
        return None
    status = status.split(":", 1)[0].strip()
    if status == "OK":
        return 1
    if status in {"ERROR", "UNPLAYABLE", "LOGIN_REQUIRED", "LIVE_STREAM_OFFLINE"}:
        return 0
    return None


def storable_watch_playability_value(metadata: dict[str, Any]) -> int | None:
    playability = watch_playability_value(metadata)
    if playability != 0:
        return playability
    if (metadata.get("availability") or "").strip():
        return playability
    return None


def watch_playability_availability(
    playability: dict[str, Any],
    microformat: dict[str, Any] | None = None,
    video_details: dict[str, Any] | None = None,
) -> str:
    if not isinstance(playability, dict):
        return ""
    microformat = microformat if isinstance(microformat, dict) else {}
    video_details = video_details if isinstance(video_details, dict) else {}
    error_screen = playability.get("errorScreen")
    offer = (
        error_screen.get("playerLegacyDesktopYpcOfferRenderer", {})
        if isinstance(error_screen, dict)
        else {}
    )
    if not isinstance(offer, dict):
        offer = {}
    offer_id = str(offer.get("offerId") or "").strip().lower()
    text = " ".join(
        text_from_runs(value).strip()
        for value in (
            playability.get("reason"),
            offer.get("itemTitle"),
            offer.get("offerDescription"),
        )
    ).lower()
    if offer_id == "sponsors_only_video" or any(
        marker in text
        for marker in (
            "members-only",
            "members only",
            "join this channel",
        )
    ):
        return "subscriber_only"
    status = str(playability.get("status") or "").strip().upper()
    if status == "OK":
        if video_details.get("isPrivate") is True:
            return "private"
        return "unlisted" if microformat.get("isUnlisted") is True else "public"
    if status == "LIVE_STREAM_OFFLINE" and video_details.get("isUpcoming") is True:
        if video_details.get("isPrivate") is True:
            return "private"
        return "unlisted" if microformat.get("isUnlisted") is True else "public"
    if any(
        marker in text
        for marker in (
            "confirm you're not a bot",
            "confirm you’re not a bot",
            "protect our community",
            "unusual traffic",
            "captcha",
        )
    ):
        return ""
    if status in {"ERROR", "UNPLAYABLE", "LOGIN_REQUIRED", "LIVE_STREAM_OFFLINE"} and text:
        return "unavailable"
    return ""


def watch_primary_info_availability(initial_data: dict[str, Any]) -> str:
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        primary_info = node.get("videoPrimaryInfoRenderer")
        if not isinstance(primary_info, dict):
            continue
        for badge_node in walk(primary_info.get("badges", [])):
            if not isinstance(badge_node, dict):
                continue
            badge = badge_node.get("metadataBadgeRenderer")
            if not isinstance(badge, dict):
                badge = badge_node.get("badgeViewModel")
            if not isinstance(badge, dict):
                continue
            style = str(badge.get("style") or badge.get("badgeStyle") or "").strip().upper()
            label = str(badge.get("label") or badge.get("badgeText") or "").strip().lower()
            if style in {"BADGE_STYLE_TYPE_MEMBERS_ONLY", "BADGE_MEMBERS_ONLY"} or label in {
                "members only",
                "members-only",
            }:
                return "subscriber_only"
        return ""
    return ""


def playlist_zero_result_is_suspicious(
    parsed_count: int,
    ytdlp_error: str,
    previous_scan_count: int,
) -> bool:
    """Avoid replacing a known playlist with an empty web fallback after yt-dlp is denied."""
    return parsed_count == 0 and bool(ytdlp_error.strip()) and previous_scan_count > 0


def playlist_scan_is_incomplete(parsed_count: int, expected_count: int) -> bool:
    """Return whether a source result is shorter than the current expected playlist size."""
    return expected_count > 0 and parsed_count < expected_count


def playlist_scan_requires_exact_count(
    metadata: dict[str, Any],
    *,
    known_owner_channel_id: str = "",
    known_visibility: str = "",
) -> bool:
    """Owned or ambiguous playlist headers must match the displayed YouTube count."""
    owner_channel_id = str(metadata.get("owner_channel_id") or "").strip()
    visibility = str(metadata.get("visibility") or "").strip()
    if not owner_channel_id and not visibility:
        owner_channel_id = (known_owner_channel_id or "").strip()
        visibility = (known_visibility or "").strip()
    return not (owner_channel_id and not visibility)


def youtube_channel_id_from_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    found = re.search(r"(?:youtube\.com|youtu\.be)/(?:channel/)?(UC[A-Za-z0-9_-]{20,})", value)
    if found:
        return found.group(1)
    found = re.search(r"(?:^|[/?&=])(UC[A-Za-z0-9_-]{20,})(?:$|[/?&#])", value)
    return found.group(1) if found else ""


def is_canonical_youtube_channel_id(value: str) -> bool:
    return bool(
        re.fullmatch(r"(?:UC|HC)[A-Za-z0-9_-]{20,}", (value or "").strip())
    )


def youtube_channel_url(channel_id: str) -> str:
    channel_id = (channel_id or "").strip()
    if not channel_id:
        return ""
    if channel_id.startswith(("http://", "https://")):
        return channel_id
    if channel_id.startswith("@"):
        return f"https://www.youtube.com/{channel_id}"
    if channel_id.startswith(("c/", "user/")):
        return f"https://www.youtube.com/{channel_id}"
    return f"https://www.youtube.com/channel/{channel_id}"


def first_channel_alias(aliases: str) -> str:
    alias = next(
        (value.strip() for value in (aliases or "").split(",") if value.strip()),
        "",
    )
    lower_alias = alias.casefold()
    for prefix in (
        "https://www.youtube.com/",
        "http://www.youtube.com/",
        "https://youtube.com/",
        "http://youtube.com/",
        "www.youtube.com/",
        "youtube.com/",
    ):
        if lower_alias.startswith(prefix):
            alias = alias[len(prefix) :]
            break
    return alias


def preferred_youtube_channel_reference(channel_id: str, aliases: str = "") -> str:
    return first_channel_alias(aliases) or (channel_id or "").strip()


def preferred_youtube_channel_url(channel_id: str, aliases: str = "") -> str:
    return youtube_channel_url(preferred_youtube_channel_reference(channel_id, aliases))


def youtube_video_url(video_id: str, playlist_id: str = "") -> str:
    video_id = (video_id or "").strip()
    if not video_id:
        return ""
    url = f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}"
    if playlist_id:
        url += f"&list={urllib.parse.quote(playlist_id)}"
    return url


def youtube_playlist_url(playlist_id: str) -> str:
    playlist_id = (playlist_id or "").strip()
    return f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}" if playlist_id else ""


def archivarix_media_url(video_id: str) -> str:
    return (
        f"https://web.archive.org/web/2oe_/http://wayback-fakeurl.archive.org/yt/{video_id}"
        if video_id
        else ""
    )


def wayback_video_url(video_id: str, capture_at: str | None) -> str:
    if not video_id or not capture_at:
        return ""
    stamp = re.sub(r"[^0-9]", "", capture_at)[:14]
    return f"https://web.archive.org/web/{stamp}/{youtube_video_url(video_id)}" if len(stamp) == 14 else ""


def youtube_channel_ref_from_url(value: str) -> str:
    value = html.unescape((value or "").strip())
    channel_id = youtube_channel_id_from_url(value)
    if channel_id:
        return channel_id
    parsed = urllib.parse.urlparse(value)
    path = parsed.path if parsed.scheme or parsed.netloc else value
    parts = [part for part in path.strip("/").split("/") if part]
    if not parts:
        return ""
    if parts[0].startswith("@"):
        return parts[0]
    if parts[0] in {"c", "user"} and len(parts) > 1:
        return f"{parts[0]}/{parts[1]}"
    return ""


def merge_channel_value(existing: str, incoming: str) -> str:
    return incoming if incoming else existing


def earliest_timestamp(*values: str | None) -> str:
    return min((str(value).strip() for value in values if str(value or "").strip()), default="")


def upsert_channel(
    conn: sqlite3.Connection,
    channel_id: str,
    *,
    title: str = "",
    url: str = "",
    description: str = "",
    aliases: str = "",
    thumbnail_url: str = "",
    thumbnail_path: str = "",
    archivarix_channel_id: str = "",
    status: str = "",
    status_reason: str = "",
    replace_aliases: bool = False,
    replace_status: bool = False,
    fetch_status: str = "",
    fetch_error: str = "",
    first_seen_at: str | None = None,
    fetched_at: str | None = None,
    source: str = "",
    updated_at: str | None = None,
) -> str:
    channel_id = (channel_id or "").strip()
    if not channel_id:
        channel_id = youtube_channel_id_from_url(url)
    if not channel_id:
        return ""
    now = updated_at or utc_now()
    existing = conn.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
    if existing:
        merged_aliases = aliases if replace_aliases else merge_channel_value(existing["aliases"], aliases)
        merged_status = status if replace_status else existing["status"]
        merged_status_reason = status_reason if replace_status else existing["status_reason"]
        conn.execute(
            """
            UPDATE channels
            SET title = ?,
                description = ?,
                aliases = ?,
                thumbnail_url = ?,
                thumbnail_path = ?,
                archivarix_channel_id = ?,
                status = ?,
                status_reason = ?,
                fetch_status = ?,
                fetch_error = ?,
                first_seen_at = ?,
                fetched_at = ?,
                metadata_source = ?,
                updated_at = ?
            WHERE channel_id = ?
            """,
            (
                merge_channel_value(existing["title"], title),
                merge_channel_value(existing["description"], description),
                merged_aliases,
                merge_channel_value(existing["thumbnail_url"], thumbnail_url),
                merge_channel_value(existing["thumbnail_path"], thumbnail_path),
                merge_channel_value(existing["archivarix_channel_id"], archivarix_channel_id),
                merged_status,
                merged_status_reason,
                merge_channel_value(existing["fetch_status"], fetch_status),
                fetch_error if fetch_status else existing["fetch_error"],
                earliest_timestamp(existing["first_seen_at"], first_seen_at) or None,
                fetched_at or existing["fetched_at"],
                merge_channel_value(existing["metadata_source"], source),
                now,
                channel_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO channels(
              channel_id, title, description, aliases, thumbnail_url, thumbnail_path,
              archivarix_channel_id, status, status_reason, fetch_status, fetch_error,
              first_seen_at, fetched_at, metadata_source, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_id,
                title,
                description,
                aliases,
                thumbnail_url,
                thumbnail_path,
                archivarix_channel_id,
                status,
                status_reason,
                fetch_status,
                fetch_error,
                first_seen_at,
                fetched_at,
                source,
                now,
            ),
        )
    return channel_id


def useful_video_title(value: str, video_id: str = "") -> bool:
    title = (value or "").strip()
    if title == (video_id or "").strip():
        return False
    if re.match(
        r"^(?:https?://)?(?:www\.)?(?:youtube\.com/watch(?:\?|$)|youtu\.be/)",
        title,
        re.IGNORECASE,
    ):
        return False
    return title.lower().strip("[]() ") not in {
        "",
        "youtube",
        "- youtube",
        "deleted video",
        "private video",
        "unavailable video",
    }


def video_title_or_blank(value: Any, video_id: str = "") -> str:
    title = str(value or "").strip()
    return title if useful_video_title(title, video_id) else ""


def is_youtube_clip_id(value: str) -> bool:
    candidate = (value or "").strip().rstrip(",")
    return len(candidate) != 11 and bool(
        re.fullmatch(r"Ugk[A-Za-z0-9_-]+", candidate)
    )


def reject_clip_id_as_video(video_id: str) -> None:
    if is_youtube_clip_id(video_id):
        raise ValueError("YouTube clip IDs cannot be stored or queued as video IDs")


def upsert_video(
    conn: sqlite3.Connection,
    video_id: str,
    *,
    title: str = "",
    description: str = "",
    channel_id: str = "",
    channel_title: str = "",
    channel_url: str = "",
    duration_text: str = "",
    view_count: str = "",
    upload_date: str = "",
    uploader_category: str = "",
    video_type: str = "",
    broadcast_status: str | None = None,
    broadcast_started_at: str | None = None,
    broadcast_ended_at: str | None = None,
    broadcast_status_checked_at: str | None = None,
    movie_rating: str = "",
    movie_release_date: str = "",
    movie_offer: str = "",
    max_video_height: int | None = None,
    spatial_format: str | None = None,
    stereo_layout: str | None = None,
    dynamic_range: str | None = None,
    license: str | None = None,
    location_name: str | None = None,
    content_check_required: bool | int | None = None,
    content_check_reason: str | None = None,
    ai_disclosure: bool | int | None = None,
    ai_disclosure_text: str | None = None,
    auto_dubbed: bool | int | None = None,
    thumbnail_url: str = "",
    thumbnail_path: str = "",
    reaction: str = "",
    is_playable: bool | int | None = None,
    availability: str = "",
    source: str = "",
    fetch_status: str = "",
    fetch_error: str = "",
    fetched_at: str | None = None,
    checked_at: str | None = None,
    updated_at: str | None = None,
) -> str:
    video_id = (video_id or "").strip()
    if not video_id:
        return ""
    reject_clip_id_as_video(video_id)
    now = updated_at or utc_now()
    channel_id = upsert_channel(
        conn,
        channel_id,
        title=channel_title,
        url=channel_url,
        source=source,
        updated_at=now,
    ) or None
    existing = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    incoming_priority = VIDEO_SOURCE_PRIORITY.get(source, 0)
    existing_priority = VIDEO_SOURCE_PRIORITY.get(existing["metadata_source"], 0) if existing else 0
    authoritative = not existing or incoming_priority >= existing_priority

    def current(name: str, incoming: str, *, useful: bool = True) -> str:
        incoming = (incoming or "").strip()
        if useful and not incoming:
            return existing[name] if existing else ""
        if authoritative and incoming:
            return incoming
        return (existing[name] if existing else "") or incoming

    def current_movie_field(name: str, incoming: str) -> str:
        if authoritative and video_type and video_type != "movie":
            return ""
        return current(name, incoming)

    def current_observation(name: str, incoming: Any) -> Any:
        if incoming is None:
            return existing[name] if existing else None
        if authoritative:
            return incoming.strip() if isinstance(incoming, str) else incoming
        existing_value = existing[name] if existing else None
        return existing_value if existing_value is not None else incoming

    def current_broadcast_field(name: str, incoming: str | None) -> str | None:
        if broadcast_status is None:
            return existing[name] if existing else None
        if authoritative and broadcast_status == "":
            return ""
        if incoming is None:
            return existing[name] if existing else None
        incoming = incoming.strip()
        if authoritative and incoming:
            return incoming
        existing_value = existing[name] if existing else None
        return existing_value if existing_value not in {None, ""} else incoming

    def current_content_check_reason() -> str | None:
        if content_check_required is None:
            return existing["content_check_reason"] if existing else None
        if not content_check_required:
            return ""
        incoming = str(content_check_reason or "").strip()
        if authoritative:
            return incoming
        existing_value = existing["content_check_reason"] if existing else None
        return existing_value if existing_value is not None else incoming

    def current_ai_disclosure_text() -> str | None:
        if ai_disclosure is None:
            return existing["ai_disclosure_text"] if existing else None
        if not ai_disclosure:
            return ""
        incoming = str(ai_disclosure_text or "").strip()
        if authoritative:
            return incoming
        existing_value = existing["ai_disclosure_text"] if existing else None
        return existing_value if existing_value is not None else incoming

    incoming_title = video_title_or_blank(title, video_id)
    existing_title = str(existing["title"] or "").strip() if existing else ""
    if incoming_title and (authoritative or not useful_video_title(existing_title, video_id)):
        canonical_title = incoming_title
    elif useful_video_title(existing_title, video_id):
        canonical_title = existing_title
    else:
        canonical_title = ""
    canonical_channel = channel_id if authoritative and channel_id else ((existing["channel_id"] if existing else None) or channel_id)
    youtube_owned_state = source != "archivarix"
    incoming_playability = (
        None
        if is_playable is None or not youtube_owned_state
        else int(bool(is_playable))
    )
    incoming_availability = availability if youtube_owned_state else ""
    canonical_playability = incoming_playability if incoming_playability is not None else (existing["is_playable"] if existing else None)
    canonical_availability = normalize_video_availability(
        video_id,
        incoming_availability,
        canonical_playability,
    )
    if not incoming_availability and existing and incoming_playability is None:
        canonical_availability = existing["availability"]
    last_seen = now if incoming_playability == 1 else (existing["last_seen_available_at"] if existing else None)
    metadata_source = source if authoritative and source else (existing["metadata_source"] if existing else source)
    values = (
        canonical_title,
        current("description", description),
        canonical_channel,
        current("duration_text", duration_text),
        current("view_count", str(view_count or "")),
        current("upload_date", upload_date),
        current("uploader_category", uploader_category),
        current("video_type", video_type),
        current_observation("broadcast_status", broadcast_status),
        current_broadcast_field("broadcast_started_at", broadcast_started_at),
        current_broadcast_field("broadcast_ended_at", broadcast_ended_at),
        current_observation(
            "broadcast_status_checked_at",
            broadcast_status_checked_at if broadcast_status is not None else None,
        ),
        current_movie_field("movie_rating", movie_rating),
        current_movie_field("movie_release_date", movie_release_date),
        current_movie_field("movie_offer", movie_offer),
        current_observation("max_video_height", max_video_height),
        current_observation("spatial_format", spatial_format),
        current_observation("stereo_layout", stereo_layout),
        current_observation("dynamic_range", dynamic_range),
        current_observation("license", license),
        current_observation("location_name", location_name),
        current_observation(
            "content_check_required",
            int(bool(content_check_required)) if content_check_required is not None else None,
        ),
        current_content_check_reason(),
        current_observation(
            "ai_disclosure",
            int(bool(ai_disclosure)) if ai_disclosure is not None else None,
        ),
        current_ai_disclosure_text(),
        current_observation(
            "auto_dubbed",
            int(bool(auto_dubbed)) if auto_dubbed is not None else None,
        ),
        current("thumbnail_url", thumbnail_url),
        current("thumbnail_path", thumbnail_path),
        current("reaction", reaction),
        canonical_playability,
        canonical_availability,
        metadata_source,
        fetch_status or (existing["fetch_status"] if existing else ""),
        fetch_error if fetch_status else (existing["fetch_error"] if existing else ""),
        fetched_at or (existing["fetched_at"] if existing else None),
        last_seen,
        checked_at or (existing["last_checked_at"] if existing else None),
        now,
    )
    if existing:
        conn.execute(
            """
            UPDATE videos SET
              title=?, description=?, channel_id=?, duration_text=?, view_count=?, upload_date=?,
              uploader_category=?, video_type=?, broadcast_status=?, broadcast_started_at=?,
              broadcast_ended_at=?, broadcast_status_checked_at=?,
              movie_rating=?, movie_release_date=?, movie_offer=?,
              max_video_height=?, spatial_format=?, stereo_layout=?, dynamic_range=?, license=?, location_name=?,
              content_check_required=?, content_check_reason=?, ai_disclosure=?, ai_disclosure_text=?, auto_dubbed=?,
              thumbnail_url=?, thumbnail_path=?, reaction=?, is_playable=?,
              availability=?, metadata_source=?,
              fetch_status=?, fetch_error=?, fetched_at=?, last_seen_available_at=?,
              last_checked_at=?, updated_at=?
            WHERE video_id=?
            """,
            (*values, video_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO videos(
              video_id, title, description, channel_id, duration_text, view_count, upload_date,
              uploader_category, video_type, broadcast_status, broadcast_started_at,
              broadcast_ended_at, broadcast_status_checked_at,
              movie_rating, movie_release_date, movie_offer,
              max_video_height, spatial_format, stereo_layout, dynamic_range, license, location_name,
              content_check_required, content_check_reason, ai_disclosure, ai_disclosure_text, auto_dubbed,
              thumbnail_url, thumbnail_path, reaction, is_playable, availability, metadata_source,
              fetch_status, fetch_error, fetched_at, last_seen_available_at,
              last_checked_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (video_id, *values),
        )
    return video_id


def normalize_utc_timestamp(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def valid_timezone_name(value: str) -> bool:
    try:
        ZoneInfo((value or "").strip())
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def refresh_exact_history_dates(conn: sqlite3.Connection, timezone_name: str) -> None:
    rows = conn.execute(
        "SELECT event_id, watched_at FROM history_events WHERE time_precision = 'exact'"
    ).fetchall()
    conn.executemany(
        "UPDATE history_events SET watch_date = ? WHERE event_id = ?",
        [
            (local_date_for_utc_instant(row["watched_at"], timezone_name), row["event_id"])
            for row in rows
        ],
    )

def load_cookie_jar(cookie_file: Path) -> http.cookiejar.MozillaCookieJar:
    jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
    if cookie_file.exists():
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except http.cookiejar.LoadError:
            text = cookie_file.read_text(encoding="utf-8", errors="replace")
            header = text.find("# Netscape HTTP Cookie File")
            if header < 0:
                raise
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
                handle.write(text[header:])
                temp_name = handle.name
            try:
                jar = http.cookiejar.MozillaCookieJar(temp_name)
                jar.load(ignore_discard=True, ignore_expires=True)
            finally:
                try:
                    Path(temp_name).unlink()
                except OSError:
                    pass
    return jar


def archivarix_session_status(cookie_file: Path, now: float | None = None) -> tuple[bool, str]:
    """Return whether the local Archivarix login session is still usable."""
    now = time.time() if now is None else now
    try:
        jar = load_cookie_jar(cookie_file)
    except (OSError, http.cookiejar.LoadError):
        return False, "Archivarix cookie file could not be read"
    sessions = [
        cookie
        for cookie in jar
        if cookie.name == "__Secure-better-auth.session_token"
        and cookie.domain.lstrip(".").endswith("archivarix.net")
    ]
    if not sessions:
        return False, "Archivarix login session cookie is missing"
    session = max(sessions, key=lambda cookie: cookie.expires or float("inf"))
    if session.expires is not None and session.expires <= now:
        return False, "Archivarix login session cookie has expired"
    return True, ""


def archivarix_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            return True
        return "timed out" in str(reason).lower()
    return "timed out" in str(exc).lower()


_YOUTUBE_AUTH_COOKIE_NAMES = {
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "LOGIN_INFO",
    "__Secure-1PSID",
    "__Secure-3PSID",
}


def youtube_auth_cookies(cookie_file: Path) -> list[http.cookiejar.Cookie]:
    return [
        cookie
        for cookie in load_cookie_jar(cookie_file)
        if cookie.name in _YOUTUBE_AUTH_COOKIE_NAMES
        and cookie.domain.lstrip(".").endswith(("youtube.com", "google.com"))
    ]


def youtube_cookie_diagnostics(cookie_file: Path, now: float | None = None) -> str:
    now = time.time() if now is None else now
    try:
        sessions = youtube_auth_cookies(cookie_file)
    except (OSError, http.cookiejar.LoadError) as exc:
        return f"cookie_file=unreadable; error={type(exc).__name__}"
    unexpired = [cookie for cookie in sessions if cookie.expires is None or cookie.expires > now]
    non_expiring_auth_cookies = sum(cookie.expires is None for cookie in sessions)
    expirations = [int(cookie.expires) for cookie in unexpired if cookie.expires is not None]
    earliest_expiry = (
        datetime.fromtimestamp(min(expirations), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if expirations
        else "none"
    )
    return (
        f"auth_cookies={len(sessions)}; unexpired={len(unexpired)}; "
        f"non_expiring_auth_cookies={non_expiring_auth_cookies}; earliest_expiry={earliest_expiry}"
    )


@contextmanager
def temporary_ytdlp_cookie_file(cookie_file: Path) -> Iterator[Path | None]:
    """Give yt-dlp a disposable cookie jar so it cannot rewrite the configured export."""
    if not cookie_file.exists():
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="yt-library-ytdlp-") as temp_dir:
        working_cookie_file = Path(temp_dir) / "cookies.txt"
        shutil.copy2(cookie_file, working_cookie_file)
        yield working_cookie_file


def youtube_ytdlp_probe_diagnostics(
    messages: list[str],
    *,
    succeeded: bool,
    deno_available: bool,
    ejs_available: bool,
) -> str:
    combined = "\n".join(messages)
    lowered = combined.lower()
    if "cookies are no longer valid" in lowered or "likely been rotated" in lowered:
        status = "cookies_rotated"
    elif (
        "confirm you're not a bot" in lowered
        or "confirm you’re not a bot" in lowered
        or "protect our community" in lowered
        or "captcha" in lowered
    ):
        status = "bot_challenge"
    elif (
        "login_required" in lowered
        or "login details are needed" in lowered
        or "sign in" in lowered
    ):
        status = "login_required"
    elif succeeded:
        status = "authenticated"
    else:
        status = "error"
    clients: list[str] = []
    for client in re.findall(
        r"\b([a-z][a-z0-9_]*) player response playability status",
        lowered,
    ):
        if client not in clients:
            clients.append(client)
    parts = [
        f"yt_dlp_probe={status}",
        f"deno={'available' if deno_available else 'missing'}",
        f"ejs={'available' if ejs_available else 'missing'}",
    ]
    if clients:
        parts.append(f"clients={','.join(clients)}")
    return "; ".join(parts)


def probe_youtube_authentication_ytdlp(cookie_file: Path, proxy_url: str = "") -> str:
    """Use yt-dlp's authenticated history extractor as a low-volume independent probe."""
    try:
        import yt_dlp  # type: ignore
    except ImportError:
        return "yt_dlp_probe=unavailable; reason=not_installed"

    messages: list[str] = []

    class YtdlpProbeLogger:
        @staticmethod
        def _append(message: str) -> None:
            if len(messages) < 250:
                messages.append(str(message)[:2000])

        def debug(self, message: str) -> None:
            self._append(message)

        def info(self, message: str) -> None:
            self._append(message)

        def warning(self, message: str) -> None:
            self._append(message)

        def error(self, message: str) -> None:
            self._append(message)

    deno_available = bool(shutil.which("deno"))
    try:
        importlib.metadata.version("yt-dlp-ejs")
        ejs_available = True
    except importlib.metadata.PackageNotFoundError:
        ejs_available = False
    succeeded = False
    try:
        with temporary_ytdlp_cookie_file(cookie_file) as working_cookie_file:
            options = {
                "cookiefile": str(working_cookie_file) if working_cookie_file else None,
                "extract_flat": "in_playlist",
                "extractor_retries": 0,
                "fragment_retries": 0,
                "logger": YtdlpProbeLogger(),
                "noprogress": True,
                "playlistend": 1,
                "quiet": True,
                "retries": 0,
                "skip_download": True,
                "socket_timeout": 15,
                **ytdlp_proxy_options(proxy_url),
            }
            with request_paced_youtube_dl(yt_dlp, options) as ydl:
                info = ydl.extract_info(":ythistory", download=False)
            succeeded = bool(info)
    except Exception as exc:
        messages.append(str(exc)[:2000])
    return youtube_ytdlp_probe_diagnostics(
        messages,
        succeeded=succeeded,
        deno_available=deno_available,
        ejs_available=ejs_available,
    )


def youtube_session_status(
    cookie_file: Path,
    now: float | None = None,
    verify_remote: bool = False,
    proxy_url: str = "",
) -> tuple[bool, str]:
    """Return whether local YouTube cookies are current and, optionally, accepted by YouTube."""
    now = time.time() if now is None else now
    try:
        jar = load_cookie_jar(cookie_file)
    except (OSError, http.cookiejar.LoadError):
        return False, "YouTube cookie file could not be read"
    sessions = [
        cookie
        for cookie in jar
        if cookie.name in _YOUTUBE_AUTH_COOKIE_NAMES
        and cookie.domain.lstrip(".").endswith(("youtube.com", "google.com"))
    ]
    if not sessions:
        return False, "YouTube login cookies are missing"
    if not any(cookie.expires is None or cookie.expires > now for cookie in sessions):
        return False, "YouTube login cookies have expired"
    if verify_remote:
        try:
            page = request_text(
                load_cookie_opener(cookie_file, proxy_url),
                "https://www.youtube.com/feed/history",
            )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            proxy_error = proxy_unavailable_error(exc, proxy_url)
            if proxy_error:
                raise proxy_error from exc
            return False, youtube_request_error_diagnostics(exc, "history authentication check")
        if "Watch history isn't viewable when signed out" in page:
            return False, (
                "YouTube login session is not accepted by YouTube; "
                + youtube_page_diagnostics(page, "history authentication check")
            )
    return True, ""


def youtube_page_login_state(html_text: str) -> bool | None:
    match = re.search(r'"LOGGED_IN"\s*:\s*(true|false)', html_text or "")
    if not match:
        return None
    return match.group(1) == "true"


def youtube_page_requires_login(html_text: str) -> bool:
    login_state = youtube_page_login_state(html_text)
    if login_state is not None:
        return not login_state
    markers = (
        "ServiceLogin",
        "accounts.google.com/ServiceLogin",
        "Watch history isn't viewable when signed out",
        "Keep track of what you watch",
    )
    return any(marker in (html_text or "") for marker in markers)


class YouTubeAuthenticationError(RuntimeError):
    def __init__(self, message: str, diagnostics: str = "") -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


def youtube_page_is_authenticated(html_text: str) -> bool:
    return youtube_page_login_state(html_text) is True


def youtube_playlist_is_missing(
    html_text: str,
    playlist_metadata: dict[str, Any],
    ytdlp_error: str,
) -> bool:
    if youtube_page_login_state(html_text) is not True:
        return False
    if playlist_metadata.get("has_video_count"):
        return False
    normalized_error = (ytdlp_error or "").casefold()
    return (
        "requested entity was not found" in normalized_error
        and "http error 404" in normalized_error
    )


def youtube_page_diagnostics(html_text: str, operation: str) -> str:
    page = html_text or ""
    logged_in_match = re.search(r'"LOGGED_IN"\s*:\s*(true|false)', page)
    logged_in = logged_in_match.group(1) if logged_in_match else "missing"
    marker_patterns = (
        ("service_login", r"ServiceLogin"),
        ("signed_out_history", r"Watch history isn't viewable when signed out"),
        ("consent", r"consent\.youtube\.com|consent\.google\.com"),
        ("bot_check", r"confirm you(?:'|’)re not a bot|protect our community"),
        ("unusual_traffic", r"unusual traffic|/sorry/"),
        ("captcha", r"recaptcha|g-recaptcha"),
    )
    markers = [name for name, pattern in marker_patterns if re.search(pattern, page, re.IGNORECASE)]
    player = extract_json_assignment(page, "ytInitialPlayerResponse")
    playability = player.get("playabilityStatus", {}) if isinstance(player, dict) else {}
    player_status = str(playability.get("status") or "").strip()
    player_reason = re.sub(r"\s+", " ", text_from_runs(playability.get("reason")).strip())
    player_reason = re.sub(r"https?://\S+", "<url>", player_reason)[:160]
    client_name_match = re.search(r'"INNERTUBE_CLIENT_NAME"\s*:\s*"([^"\\]+)"', page)
    client_version_match = re.search(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"\\]+)"', page)
    parts = [
        f"operation={operation}",
        f"response_chars={len(page)}",
        f"logged_in={logged_in}",
        f"markers={','.join(markers) if markers else 'none'}",
        f"player_status={player_status or 'missing'}",
    ]
    if player_reason:
        parts.append(f"player_reason={player_reason}")
    if client_name_match:
        parts.append(f"client={client_name_match.group(1)}")
    if client_version_match:
        parts.append(f"client_version={client_version_match.group(1)}")
    return "; ".join(parts)


def youtube_authentication_error(html_text: str, operation: str) -> YouTubeAuthenticationError:
    return YouTubeAuthenticationError(
        "YouTube login session is not accepted by YouTube",
        youtube_page_diagnostics(html_text, operation),
    )


def youtube_request_error_diagnostics(exc: BaseException, operation: str) -> str:
    parts = [f"YouTube request failed; operation={operation}", f"error={type(exc).__name__}"]
    if isinstance(exc, urllib.error.HTTPError):
        parts.append(f"status={exc.code}")
        if exc.reason:
            reason = re.sub(r"https?://\S+", "<url>", str(exc.reason))
            parts.append(f"reason={reason[:120]}")
        retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
        content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
        if retry_after:
            parts.append(f"retry_after={retry_after[:80]}")
        if content_type:
            parts.append(f"content_type={content_type.split(';', 1)[0][:80]}")
    elif isinstance(exc, urllib.error.URLError):
        reason = re.sub(r"https?://\S+", "<url>", str(exc.reason))
        parts.append(f"reason={reason[:160]}")
    elif isinstance(exc, json.JSONDecodeError):
        parts.append(f"json_line={exc.lineno}")
        parts.append(f"json_column={exc.colno}")
    elif str(exc):
        message = re.sub(r"https?://\S+", "<url>", str(exc))
        parts.append(f"message={message[:160]}")
    return "; ".join(parts)


def network_opener(proxy_url: str = "") -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        *socks5_proxy_handlers(proxy_url),
    )


def load_cookie_opener(
    cookie_file: Path,
    proxy_url: str = "",
) -> urllib.request.OpenerDirector:
    jar = load_cookie_jar(cookie_file)
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        *socks5_proxy_handlers(proxy_url),
    )


def request_bytes(
    opener: urllib.request.OpenerDirector,
    url: str,
    timeout: int = 30,
    referer: str | None = None,
) -> tuple[bytes, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with open_with_request_pacing(opener, req, timeout=timeout) as response:
        return response.read(), response.headers.get_content_type()


def request_text(opener: urllib.request.OpenerDirector, url: str) -> str:
    body, _ = request_bytes(opener, url)
    return body.decode("utf-8", "replace")


def group_sort_tree(export: dict[str, Any]) -> list[GroupNode]:
    meta = export.get("ysc_meta", {})
    sub_groups = export.get("ysc_settings", {}).get("sub_groups", {})
    nodes: list[GroupNode] = []
    child_names = {
        child_name
        for children in sub_groups.values()
        if isinstance(children, dict)
        for child_name in children.keys()
    }

    top_level_names = [
        name
        for name, value in export.items()
        if isinstance(value, list) and name not in child_names
    ]

    seen: set[str] = set()

    def add_node(name: str, parent_key: str | None, position: int) -> None:
        if name in seen:
            return
        seen.add(name)
        nodes.append(
            GroupNode(
                key=name,
                name=name,
                parent_key=parent_key,
                position=position,
                icon=meta.get(name, {}).get("img", ""),
            )
        )
        children = sub_groups.get(name, {})
        if isinstance(children, dict):
            for child_position, child_name in enumerate(children.keys()):
                add_node(child_name, name, child_position)

    for position, name in enumerate(top_level_names):
        add_node(name, None, position)

    return nodes


def extract_playlist_id(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if value.startswith("PL") or value.startswith("OLAK"):
        return value
    parsed = urllib.parse.urlparse(value)
    params = urllib.parse.parse_qs(parsed.query)
    if "list" in params and params["list"]:
        return params["list"][0]
    return None

def extract_clip_id(value: str) -> str:
    value = (value or "").strip().rstrip(",")
    if is_youtube_clip_id(value):
        return value
    parsed = urllib.parse.urlparse(value)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) >= 2 and parts[0] == "clip":
        candidate = parts[1]
        return candidate if re.fullmatch(r"Ugk[A-Za-z0-9_-]+", candidate) else ""
    return ""


def load_pockettube(path: Path) -> tuple[dict[str, Any], list[GroupNode], dict[str, list[str]]]:
    with path.open("r", encoding="utf-8") as handle:
        export = json.load(handle)
    groups = group_sort_tree(export)
    group_keys_by_name = {node.name: node.key for node in groups}
    memberships: dict[str, list[str]] = {}
    for name, values in export.items():
        if not isinstance(values, list):
            continue
        group_key = group_keys_by_name.get(name, name)
        memberships[group_key] = [
            playlist_id
            for value in values
            if isinstance(value, str)
            for playlist_id in [extract_playlist_id(value)]
            if playlist_id
        ]
    return export, groups, memberships


def extract_json_assignment(html_text: str, name: str) -> dict[str, Any]:
    start = -1
    for pattern in (
        rf"var\s+{re.escape(name)}\s*=",
        rf"window\[['\"]{re.escape(name)}['\"]\]\s*=",
        rf"{re.escape(name)}\s*=",
    ):
        found = re.search(pattern, html_text)
        if found:
            start = found.end()
            break
    if start == -1:
        return {}

    index = html_text.find("{", start)
    if index == -1:
        return {}
    depth = 0
    in_string = False
    escape = False
    for pos in range(index, len(html_text)):
        char = html_text[pos]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html_text[index : pos + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


def extract_ytcfg(html_text: str) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for key in (
        "INNERTUBE_API_KEY",
        "INNERTUBE_CLIENT_NAME",
        "INNERTUBE_CLIENT_VERSION",
        "VISITOR_DATA",
        "DELEGATED_SESSION_ID",
    ):
        found = re.search(rf'"{re.escape(key)}":\s*"([^"]*)"', html_text)
        if found:
            config[key] = html.unescape(found.group(1))
    return config


def request_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    payload: dict[str, Any],
    referer: str,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.youtube.com",
        "Referer": referer,
    }
    req = urllib.request.Request(url, data=body, headers=headers)
    with open_with_request_pacing(opener, req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def cookie_value(jar: http.cookiejar.CookieJar, names: tuple[str, ...]) -> str:
    for cookie in jar:
        if cookie.name in names and cookie.value:
            return cookie.value
    return ""


def sapisid_auth_header(jar: http.cookiejar.CookieJar, origin: str) -> str:
    sapisid = cookie_value(jar, ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID"))
    if not sapisid:
        return ""
    timestamp = str(int(time.time()))
    digest = hashlib.sha1(f"{timestamp} {sapisid} {origin}".encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {timestamp}_{digest}"


def request_youtubei_json(
    opener: urllib.request.OpenerDirector,
    jar: http.cookiejar.CookieJar,
    api_key: str,
    payload: dict[str, Any],
    referer: str,
    client_version: str,
    *,
    api_path: str = "browse",
) -> dict[str, Any]:
    if api_path not in {"browse", "get_panel", "player", "next"}:
        raise ValueError(f"Unsupported YouTubei API path: {api_path}")
    origin = "https://www.youtube.com"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": origin,
        "Referer": referer,
        "X-Origin": origin,
        "X-Youtube-Client-Name": "1",
        "X-Youtube-Client-Version": client_version,
    }
    auth = sapisid_auth_header(jar, origin)
    if auth:
        headers["Authorization"] = auth
    url = f"https://www.youtube.com/youtubei/v1/{api_path}?key={urllib.parse.quote(api_key)}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with open_with_request_pacing(opener, req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def text_from_runs(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("simpleText"), str):
        return value["simpleText"]
    runs = value.get("runs")
    if isinstance(runs, list):
        return "".join(run.get("text", "") for run in runs if isinstance(run, dict))
    content = value.get("content")
    if isinstance(content, str):
        return content
    return ""


def pick_thumbnail(thumbnails: list[dict[str, Any]]) -> str:
    candidates = [
        item
        for item in thumbnails
        if isinstance(item, dict) and isinstance(item.get("url"), str)
    ]
    if not candidates:
        return ""
    best = max(candidates, key=lambda item: int(item.get("width") or 0))
    return html.unescape(best["url"])


def absolute_url(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("//"):
        return f"https:{value}"
    return value


def content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("content"), str):
        return value["content"]
    return text_from_runs(value)


def channel_ref_from_endpoint(endpoint: Any) -> str:
    if not isinstance(endpoint, dict):
        return ""
    browse = endpoint.get("browseEndpoint") or {}
    browse_id = str(browse.get("browseId") or "").strip()
    if browse_id.startswith("UC"):
        return browse_id
    canonical_base_url = str(browse.get("canonicalBaseUrl") or "").strip()
    ref = youtube_channel_ref_from_url(canonical_base_url)
    if ref:
        return ref
    command = endpoint.get("commandMetadata", {}).get("webCommandMetadata", {})
    return youtube_channel_ref_from_url(str(command.get("url") or ""))


def rich_text_channel_ref(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for run in value.get("runs") or []:
        if not isinstance(run, dict):
            continue
        ref = channel_ref_from_endpoint(run.get("navigationEndpoint"))
        if ref:
            return ref
    for command_run in value.get("commandRuns") or []:
        if not isinstance(command_run, dict):
            continue
        on_tap = command_run.get("onTap") or {}
        ref = channel_ref_from_endpoint(on_tap.get("innertubeCommand"))
        if ref:
            return ref
    command_context = value.get("rendererContext", {}).get("commandContext", {})
    on_tap = command_context.get("onTap") or {}
    return channel_ref_from_endpoint(on_tap.get("innertubeCommand"))


def playlist_owner_from_metadata_part(part: dict[str, Any]) -> tuple[str, str]:
    text_value = part.get("text")
    owner_text = content_text(text_value).strip()
    if not owner_text.lower().startswith("by "):
        avatar_text = (
            part.get("avatarStack", {})
            .get("avatarStackViewModel", {})
            .get("text")
        )
        owner_text = content_text(avatar_text).strip()
        text_value = avatar_text
    if not owner_text.lower().startswith("by "):
        return "", ""
    owner = owner_text[3:].strip()
    avatar_stack = part.get("avatarStack", {}).get("avatarStackViewModel", {})
    avatars = avatar_stack.get("avatars") or []
    if len(avatars) > 1 or re.search(r"\s+and\s+\d+\s+others?$", owner, re.I):
        return "", ""
    return owner, rich_text_channel_ref(text_value)


def playlist_collaboration_panel_request(initial_data: Any) -> dict[str, str]:
    for node in walk(initial_data):
        endpoint = node.get("showEngagementPanelEndpoint")
        if not isinstance(endpoint, dict):
            continue
        identifier = endpoint.get("identifier") or {}
        panel_id = str(identifier.get("tag") or "").strip()
        configuration = endpoint.get("globalConfiguration") or {}
        params = str(configuration.get("params") or "").strip()
        if panel_id == "PAplaylist_collaborate" and params:
            return {"panel_id": panel_id, "params": params}
    return {}


def _channel_ref_from_value(value: Any) -> str:
    for node in walk(value):
        if not isinstance(node, dict):
            continue
        ref = channel_ref_from_endpoint(node)
        if ref:
            return ref
        on_tap = node.get("onTap") or {}
        ref = channel_ref_from_endpoint(on_tap.get("innertubeCommand"))
        if ref:
            return ref
    return ""


def _playlist_participant_role(participant: dict[str, Any]) -> str:
    for node in walk(participant.get("metadata")):
        if not isinstance(node, dict):
            continue
        for key in ("text", "title", "label"):
            value = content_text(node.get(key)).strip().lower()
            if value == "owner":
                return "owner"
    return "collaborator"


def parse_playlist_collaboration_metadata(response: Any) -> dict[str, Any]:
    collaborators_node: dict[str, Any] | None = None
    for node in walk(response):
        model = node.get("playlistCollaborationViewModel")
        if isinstance(model, dict) and isinstance(model.get("playlistCollaborators"), list):
            collaborators_node = model
            break
    if collaborators_node is None:
        return {}

    participants: list[dict[str, str]] = []
    for participant in collaborators_node.get("playlistCollaborators") or []:
        if not isinstance(participant, dict):
            continue
        item = participant.get("contentListItemViewModel") or participant
        if not isinstance(item, dict):
            continue
        title = content_text(item.get("title")).strip()
        channel_id = _channel_ref_from_value(item)
        if not title or not channel_id:
            continue
        participants.append(
            {
                "title": title,
                "channel_id": channel_id,
                "thumbnail_url": image_sources_thumbnail_url(item),
                "role": _playlist_participant_role(item),
            }
        )

    owner = next((item for item in participants if item["role"] == "owner"), None)
    if owner is None:
        return {}
    collaborators = [
        {key: value for key, value in item.items() if key != "role"}
        for item in participants
        if item["role"] != "owner" and item["channel_id"] != owner["channel_id"]
    ]
    return {
        "owner": owner["title"],
        "owner_channel_id": owner["channel_id"],
        "owner_thumbnail_url": owner["thumbnail_url"],
        "collaborators": collaborators,
        "collaborators_authoritative": True,
    }


def fetch_playlist_collaboration_metadata(
    opener: urllib.request.OpenerDirector,
    cookie_file: Path,
    html_text: str,
    referer: str,
) -> dict[str, Any]:
    initial_data = extract_json_assignment(html_text, "ytInitialData")
    panel_request = playlist_collaboration_panel_request(initial_data)
    if not panel_request:
        return {}
    config = extract_ytcfg(html_text)
    api_key = str(config.get("INNERTUBE_API_KEY") or "")
    client_version = str(config.get("INNERTUBE_CLIENT_VERSION") or "")
    if not api_key or not client_version:
        return {}
    response = request_youtubei_json(
        opener,
        load_cookie_jar(cookie_file),
        api_key,
        {
            "context": youtube_web_context(config),
            "panelId": panel_request["panel_id"],
            "params": panel_request["params"],
        },
        referer,
        client_version,
        api_path="get_panel",
    )
    return parse_playlist_collaboration_metadata(response)


def image_sources_thumbnail_url(value: Any) -> str:
    best_url = ""
    best_width = -1
    for node in walk(value):
        if not isinstance(node, dict):
            continue
        sources = node.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            url = source.get("url")
            if not isinstance(url, str) or not url.startswith(("http", "//")):
                continue
            width = int(source.get("width") or 0)
            if width > best_width:
                best_url = html.unescape(url)
                best_width = width
    return absolute_url(best_url)


def playlist_owner_thumbnail_from_metadata_part(part: dict[str, Any]) -> str:
    avatar_stack = (
        part.get("avatarStack", {})
        .get("avatarStackViewModel", {})
    )
    thumbnail = image_sources_thumbnail_url(avatar_stack)
    if thumbnail:
        return thumbnail
    return image_sources_thumbnail_url(part)


def pick_lockup_thumbnail(lockup: dict[str, Any]) -> str:
    best_url = ""
    best_width = -1
    for node in walk(lockup):
        if not isinstance(node, dict):
            continue
        sources = node.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            url = source.get("url")
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            width = int(source.get("width") or 0)
            if width > best_width:
                best_url = html.unescape(url)
                best_width = width
    return best_url


def lockup_metadata_rows(lockup: dict[str, Any]) -> list[list[str]]:
    metadata = (
        lockup.get("metadata", {})
        .get("lockupMetadataViewModel", {})
        .get("metadata", {})
        .get("contentMetadataViewModel", {})
    )
    rows = []
    for row in metadata.get("metadataRows", []) or []:
        if not isinstance(row, dict):
            continue
        parts = []
        for part in row.get("metadataParts", []) or []:
            if isinstance(part, dict):
                text = content_text(part.get("text")).strip()
                if text:
                    parts.append(text)
        if parts:
            rows.append(parts)
    return rows


def parse_youtube_playlist_video_count(value: str) -> int:
    found = re.search(r"([\d,]+)\s+videos?", value or "", re.I)
    return int(found.group(1).replace(",", "")) if found else 0


def has_youtube_playlist_video_count(value: str) -> bool:
    return bool(re.search(r"([\d,]+)\s+videos?", value or "", re.I))


def parse_youtube_playlist_updated_date(
    value: str,
    *,
    observed_at: str = "",
    timezone_name: str = DEFAULT_DISPLAY_TIMEZONE,
) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if not text:
        return ""

    exact = re.fullmatch(
        r"(?:last\s+)?updated\s+on\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})",
        text,
        re.I,
    )
    if exact:
        month_names = {
            name: number
            for number, names in enumerate(
                (
                    (),
                    ("jan", "january"),
                    ("feb", "february"),
                    ("mar", "march"),
                    ("apr", "april"),
                    ("may",),
                    ("jun", "june"),
                    ("jul", "july"),
                    ("aug", "august"),
                    ("sep", "sept", "september"),
                    ("oct", "october"),
                    ("nov", "november"),
                    ("dec", "december"),
                )
            )
            for name in names
        }
        month = month_names.get(exact.group(1).casefold())
        if month:
            try:
                return date(
                    int(exact.group(3)),
                    month,
                    int(exact.group(2)),
                ).isoformat()
            except ValueError:
                return ""

    relative = re.fullmatch(
        r"(?:last\s+)?updated\s+(today|yesterday|(\d+)\s+days?\s+ago)",
        text,
        re.I,
    )
    if not relative:
        return ""
    try:
        zone = ZoneInfo(timezone_name or DEFAULT_DISPLAY_TIMEZONE)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    try:
        observed = datetime.fromisoformat(
            (observed_at or utc_now()).replace("Z", "+00:00")
        )
    except ValueError:
        return ""
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    observed_date = observed.astimezone(zone).date()
    label = relative.group(1).casefold()
    if label == "today":
        days_ago = 0
    elif label == "yesterday":
        days_ago = 1
    else:
        days_ago = int(relative.group(2))
    return (observed_date - timedelta(days=days_ago)).isoformat()


def merge_youtube_playlist_updated_date(
    existing_date: str | None,
    observed_date: str | None,
    observed_text: str = "",
) -> str:
    existing = str(existing_date or "").strip()
    observed = str(observed_date or "").strip()
    if not observed:
        return existing
    if not existing or observed >= existing:
        return observed

    relative = re.fullmatch(
        r"(?:last\s+)?updated\s+(?:today|yesterday|\d+\s+days?\s+ago)",
        re.sub(r"\s+", " ", observed_text or "").strip(),
        re.I,
    )
    if not relative:
        return existing
    try:
        is_rollover_correction = (
            date.fromisoformat(existing) - date.fromisoformat(observed)
            == timedelta(days=1)
        )
    except ValueError:
        return existing
    return observed if is_rollover_correction else existing


def playlist_updated_text_from_initial_data(initial_data: Any) -> str:
    for node in walk(initial_data):
        renderer = node.get("playlistSidebarPrimaryInfoRenderer")
        if not isinstance(renderer, dict):
            continue
        for stat in renderer.get("stats", []) or []:
            text = text_from_runs(stat).strip()
            if parse_youtube_playlist_updated_date(
                text,
                observed_at="2000-01-02T12:00:00Z",
            ):
                return text
    return ""


def playlist_visibility_from_initial_data(initial_data: Any) -> str:
    for node in walk(initial_data):
        renderer = node.get("playlistSidebarPrimaryInfoRenderer")
        if not isinstance(renderer, dict):
            continue
        for badge in renderer.get("badges", []) or []:
            if not isinstance(badge, dict):
                continue
            badge_renderer = badge.get("metadataBadgeRenderer")
            if not isinstance(badge_renderer, dict):
                continue
            visibility = normalize_playlist_visibility(str(badge_renderer.get("label") or ""))
            if visibility:
                return visibility
            icon_type = str(
                badge_renderer.get("icon", {}).get("iconType") or ""
            ).removeprefix("PRIVACY_")
            visibility = normalize_playlist_visibility(icon_type)
            if visibility:
                return visibility

    for node in walk(initial_data):
        renderer = node.get("microformatDataRenderer")
        if not isinstance(renderer, dict):
            continue
        for key in ("privacyStatus", "visibility"):
            visibility = normalize_playlist_visibility(str(renderer.get(key) or ""))
            if visibility:
                return visibility
        if renderer.get("unlisted") is True:
            return "unlisted"
    return ""


def parse_playlist_lockup(
    lockup: dict[str, Any],
    *,
    observed_at: str = "",
    timezone_name: str = DEFAULT_DISPLAY_TIMEZONE,
) -> dict[str, Any] | None:
    playlist_id = lockup.get("contentId")
    if not isinstance(playlist_id, str) or not playlist_id:
        return None
    if lockup.get("contentType") != "LOCKUP_CONTENT_TYPE_PLAYLIST":
        return None

    title = content_text(
        lockup.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title")
    ).strip()
    rows = lockup_metadata_rows(lockup)
    visibility = ""
    video_count = 0
    has_video_count = False
    updated_text = ""
    for parts in rows:
        joined = " • ".join(parts)
        if "Playlist" in parts and parts:
            visibility = normalize_playlist_visibility(parts[0])
        if not has_video_count and has_youtube_playlist_video_count(joined):
            video_count = parse_youtube_playlist_video_count(joined)
            has_video_count = True
        if parse_youtube_playlist_updated_date(
            joined,
            observed_at=observed_at,
            timezone_name=timezone_name,
        ):
            updated_text = joined
    if not has_video_count:
        for node in walk(lockup):
            if not isinstance(node, dict):
                continue
            badge = node.get("thumbnailBadgeViewModel")
            if isinstance(badge, dict):
                text = badge.get("text")
                if isinstance(text, str) and has_youtube_playlist_video_count(text):
                    video_count = parse_youtube_playlist_video_count(text)
                    has_video_count = True
                if has_video_count:
                    break

    return {
        "playlist_id": playlist_id,
        "title": title or playlist_id,
        "description": "",
        "youtube_updated_text": updated_text,
        "youtube_updated_date": parse_youtube_playlist_updated_date(
            updated_text,
            observed_at=observed_at,
            timezone_name=timezone_name,
        ),
        "owner": "",
        "visibility": visibility,
        "video_count": video_count,
        "has_video_count": has_video_count,
        "thumbnail_url": pick_lockup_thumbnail(lockup),
        "url": f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}",
    }


def extract_playlist_metadata(
    html_text: str,
    playlist_id: str,
    *,
    observed_at: str = "",
    timezone_name: str = DEFAULT_DISPLAY_TIMEZONE,
) -> dict[str, Any]:
    initial_data = extract_json_assignment(html_text, "ytInitialData")
    updated_text = playlist_updated_text_from_initial_data(initial_data)
    metadata = {
        "playlist_id": playlist_id,
        "title": "",
        "description": "",
        "owner": "",
        "owner_channel_id": "",
        "owner_thumbnail_url": "",
        "youtube_updated_text": updated_text,
        "youtube_updated_date": parse_youtube_playlist_updated_date(
            updated_text,
            observed_at=observed_at,
            timezone_name=timezone_name,
        ),
        "visibility": "",
        "video_count": 0,
        "has_video_count": False,
        "thumbnail_url": "",
        "url": f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}",
    }
    metadata["visibility"] = playlist_visibility_from_initial_data(initial_data)

    for prop in ("og:title", "twitter:title"):
        found = re.search(
            rf'<meta\s+(?:property|name)="{re.escape(prop)}"\s+content="([^"]*)"',
            html_text,
        )
        if found and not metadata["title"]:
            metadata["title"] = html.unescape(found.group(1))
    found = re.search(r'<meta\s+property="og:image"\s+content="([^"]*)"', html_text)
    if found:
        metadata["thumbnail_url"] = html.unescape(found.group(1))
    found = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', html_text)
    if found:
        metadata["description"] = html.unescape(found.group(1))

    for node in walk(initial_data):
        renderer = node.get("playlistHeaderRenderer")
        if not isinstance(renderer, dict):
            renderer = node.get("pageHeaderRenderer")
        if not isinstance(renderer, dict):
            continue
        title = text_from_runs(renderer.get("title"))
        if title and not metadata["title"]:
            metadata["title"] = title
        description = text_from_runs(renderer.get("description"))
        if description and not metadata["description"]:
            metadata["description"] = description
        owner_rich_text = renderer.get("ownerText") or renderer.get("subtitle")
        owner_text = text_from_runs(owner_rich_text)
        owner, visibility = split_playlist_owner_visibility(owner_text)
        if owner and not metadata["owner"]:
            metadata["owner"] = owner
        owner_channel_id = rich_text_channel_ref(owner_rich_text)
        if owner_channel_id and not metadata["owner_channel_id"]:
            metadata["owner_channel_id"] = owner_channel_id
        if visibility and not metadata["visibility"]:
            metadata["visibility"] = visibility
        for key in ("numVideosText", "numVideosTextText", "videoCountText"):
            count_text = text_from_runs(renderer.get(key))
            if count_text and not metadata["has_video_count"]:
                parsed_count = parse_youtube_playlist_video_count(count_text)
                if re.search(r"[\d,]+\s+videos?", count_text, re.I):
                    metadata["video_count"] = parsed_count
                    metadata["has_video_count"] = True
        thumbnail = renderer.get("playlistHeaderBanner")
        if isinstance(thumbnail, dict):
            thumbs = thumbnail.get("heroPlaylistThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [])
            if thumbs and not metadata["thumbnail_url"]:
                metadata["thumbnail_url"] = pick_thumbnail(thumbs)

        for header_node in walk(renderer):
            if not isinstance(header_node, dict):
                continue
            metadata_rows = header_node.get("metadataRows")
            if not isinstance(metadata_rows, list):
                continue
            for row in metadata_rows:
                if not isinstance(row, dict):
                    continue
                for part in row.get("metadataParts", []) or []:
                    if not isinstance(part, dict):
                        continue
                    owner, owner_channel_id = playlist_owner_from_metadata_part(part)
                    if owner and not metadata["owner"]:
                        metadata["owner"] = owner
                    if owner_channel_id and not metadata["owner_channel_id"]:
                        metadata["owner_channel_id"] = owner_channel_id
                    if owner:
                        owner_thumbnail_url = playlist_owner_thumbnail_from_metadata_part(part)
                        if owner_thumbnail_url and not metadata["owner_thumbnail_url"]:
                            metadata["owner_thumbnail_url"] = owner_thumbnail_url
                parts = [
                    content_text(part.get("text")).strip()
                    for part in row.get("metadataParts", []) or []
                    if isinstance(part, dict) and content_text(part.get("text")).strip()
                ]
                for part in parts:
                    visibility = normalize_playlist_visibility(part)
                    if visibility and not metadata["visibility"]:
                        metadata["visibility"] = visibility
                # This is YouTube's displayed playlist count, not the local scan total.
                if not metadata["has_video_count"]:
                    for part in parts:
                        parsed_count = parse_youtube_playlist_video_count(part)
                        if re.search(r"[\d,]+\s+videos?", part, re.I):
                            metadata["video_count"] = parsed_count
                            metadata["has_video_count"] = True
                            break

    if not metadata["thumbnail_url"]:
        for node in walk(initial_data):
            thumbnail = node.get("thumbnail")
            if isinstance(thumbnail, dict):
                url = pick_thumbnail(thumbnail.get("thumbnails", []))
                if url:
                    metadata["thumbnail_url"] = url
                    break

    if not metadata["title"]:
        found = re.search(r"<title>(.*?)</title>", html_text, flags=re.DOTALL)
        if found:
            title = html.unescape(re.sub(r"\s+", " ", found.group(1))).strip()
            metadata["title"] = re.sub(r"\s+-\s+YouTube$", "", title)
    if not metadata["title"]:
        metadata["title"] = playlist_id
    return metadata


def thumbnail_extension(content_type: str, url: str) -> str:
    guessed = mimetypes.guess_extension(content_type) if content_type else None
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"


def local_asset_path(path: Path) -> str:
    resolved = path.resolve()
    for asset_dir in (
        DEFAULT_THUMB_DIR,
        DEFAULT_VIDEO_THUMB_DIR,
        DEFAULT_ARCHIVARIX_THUMB_DIR,
    ):
        try:
            relative = resolved.relative_to(asset_dir.resolve())
        except ValueError:
            continue
        return str(Path(asset_dir.name) / relative).replace("\\", "/")
    return str(resolved.relative_to(ROOT)).replace("\\", "/")


def cache_thumbnail(
    opener: urllib.request.OpenerDirector,
    playlist_id: str,
    thumbnail_url: str,
    thumb_dir: Path,
) -> str:
    if not thumbnail_url:
        return ""
    thumb_dir.mkdir(parents=True, exist_ok=True)
    try:
        body, content_type = request_bytes(
            opener,
            thumbnail_url,
            timeout=30,
            referer=f"https://www.youtube.com/playlist?list={playlist_id}",
        )
    except Exception:
        return ""
    ext = thumbnail_extension(content_type, thumbnail_url)
    target = thumb_dir / f"{safe_name(playlist_id)}{ext}"
    target.write_bytes(body)
    return local_asset_path(target)


def cache_video_thumbnail(
    opener: urllib.request.OpenerDirector,
    video_id: str,
    thumbnail_url: str,
    thumb_dir: Path,
) -> str:
    if not video_id or not thumbnail_url:
        return ""
    thumb_dir.mkdir(parents=True, exist_ok=True)
    try:
        body, content_type = request_bytes(
            opener,
            thumbnail_url,
            timeout=30,
            referer=f"https://www.youtube.com/watch?v={video_id}",
        )
    except Exception:
        return ""
    ext = thumbnail_extension(content_type, thumbnail_url)
    target = thumb_dir / f"{safe_name(video_id)}{ext}"
    target.write_bytes(body)
    return local_asset_path(target)


def cache_channel_thumbnail(
    opener: urllib.request.OpenerDirector,
    subject_id: str,
    thumbnail_url: str,
    thumb_dir: Path,
    referer_url: str = "",
    timeout: int = 30,
) -> str:
    if not subject_id or not thumbnail_url:
        return ""
    thumb_dir.mkdir(parents=True, exist_ok=True)
    try:
        body, content_type = request_bytes(
            opener,
            thumbnail_url,
            timeout=timeout,
            referer=referer_url or f"https://www.youtube.com/watch?v={subject_id}",
        )
    except Exception:
        return ""
    ext = thumbnail_extension(content_type, thumbnail_url)
    target = thumb_dir / f"{safe_name(subject_id)}_channel{ext}"
    target.write_bytes(body)
    return local_asset_path(target)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def format_duration(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def archivarix_search_deleted(
    query: str,
    page_size: int = 50,
    opener: urllib.request.OpenerDirector | None = None,
) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {"q": query, "page": "1", "pageSize": str(page_size), "status": "deleted"}
    )
    url = f"https://tube.archivarix.net/api/fts?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Accept": "application/json",
            "Referer": f"https://tube.archivarix.net/?q={urllib.parse.quote(query)}",
        },
    )
    opener = opener or network_opener()
    with open_with_request_pacing(opener, req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    data = payload.get("data", {})
    videos = data.get("videos", [])
    return videos if isinstance(videos, list) else []


def archivarix_quota_message_from_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    if "limit reached" in normalized or "searches per day" in normalized:
        return "Archivarix daily search limit reached"
    return ""


def archivarix_http_error_message(exc: urllib.error.HTTPError) -> str:
    if exc.code == 429:
        return "Archivarix daily search limit reached"
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    return archivarix_quota_message_from_text(body)


def archivarix_lookup_video(
    video_id: str,
    opener: urllib.request.OpenerDirector | None = None,
    channel_cache: dict[str, dict[str, Any]] | None = None,
    stop_event: threading.Event | None = None,
    request_timeout: int = 20,
    stream_timeout: int = 25,
) -> dict[str, Any] | None:
    if stop_event and stop_event.is_set():
        return None
    opener = opener or network_opener()
    channel_cache = channel_cache if channel_cache is not None else {}
    youtube_url = f"https://www.youtube.com/watch?v={video_id}"
    request = urllib.request.Request(
        "https://tube.archivarix.net/api/search",
        data=json.dumps({"query": youtube_url}).encode("utf-8"),
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": "https://tube.archivarix.net/",
        },
        method="POST",
    )
    with open_with_request_pacing(opener, request, timeout=request_timeout) as response:
        response_text = response.read().decode("utf-8", "replace")
    quota_message = archivarix_quota_message_from_text(response_text)
    if quota_message:
        raise ArchivarixQuotaExceeded(quota_message)
    session = json.loads(response_text).get("data", {})
    endpoint = session.get("sseEndpointUrl")
    if not isinstance(endpoint, str) or not endpoint:
        return None
    stream_url = urllib.parse.urljoin("https://tube.archivarix.net", endpoint)
    stream_request = urllib.request.Request(
        stream_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Accept": "text/event-stream",
            "Referer": f"https://tube.archivarix.net/?q={urllib.parse.quote(youtube_url)}",
        },
    )
    event = ""
    resolved_channel: dict[str, Any] = {}
    with open_with_request_pacing(opener, stream_request, timeout=stream_timeout) as response:
        for raw_line in response:
            if stop_event and stop_event.is_set():
                return None
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                event = ""
                continue
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload_text = line.partition(":")[2].strip()
            if event == "search:channel_resolved":
                payload = json.loads(payload_text)
                resolved_channel = archivarix_channel_recovery_fields(payload)
                continue
            if event == "search:channel_update":
                payload = json.loads(payload_text)
                resolved_channel.update(archivarix_channel_recovery_fields(payload))
                continue
            if event == "search:video":
                payload = json.loads(payload_text)
                internal_channel_id = str(payload.get("channelId") or "")
                cached_channel = channel_cache.get(internal_channel_id, {}) if internal_channel_id else {}
                channel_fields = dict(cached_channel)
                channel_fields.update({k: v for k, v in resolved_channel.items() if v})
                if internal_channel_id and channel_fields:
                    channel_cache[internal_channel_id] = channel_fields
                apply_archivarix_channel_fields(payload, channel_fields)
                if payload.get("videoId") == video_id:
                    return payload
            if event in {"search:complete", "search:error"}:
                return None
    return None


def archivarix_lookup_channel(
    channel_id: str,
    opener: urllib.request.OpenerDirector | None = None,
) -> dict[str, Any]:
    if not channel_id:
        return {}
    opener = opener or network_opener()
    request = urllib.request.Request(
        "https://tube.archivarix.net/api/search",
        data=json.dumps({"query": channel_id}).encode("utf-8"),
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": "https://tube.archivarix.net/",
        },
        method="POST",
    )
    with open_with_request_pacing(opener, request, timeout=20) as response:
        session = json.loads(response.read().decode("utf-8", "replace")).get("data", {})
    endpoint = session.get("sseEndpointUrl")
    if not isinstance(endpoint, str) or not endpoint:
        return {}
    stream_url = urllib.parse.urljoin("https://tube.archivarix.net", endpoint)
    stream_request = urllib.request.Request(
        stream_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Accept": "text/event-stream",
            "Referer": f"https://tube.archivarix.net/?q={urllib.parse.quote(channel_id)}",
        },
    )
    event = ""
    fields: dict[str, Any] = {}
    with open_with_request_pacing(opener, stream_request, timeout=25) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                event = ""
                continue
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload_text = line.partition(":")[2].strip()
            if event in {"search:channel_resolved", "search:channel_update"}:
                payload = json.loads(payload_text)
                fields.update({k: v for k, v in archivarix_channel_recovery_fields(payload).items() if v})
                status = str(payload.get("channelStatus") or "").strip()
                if status:
                    fields["channelStatus"] = status
                internal_id = str(payload.get("id") or payload.get("internalId") or "").strip()
                if internal_id:
                    fields["archivarixChannelId"] = internal_id
                if fields.get("channelTitle") and fields.get("channelThumbnailUrl"):
                    return fields
                continue
            if event == "search:video":
                payload = json.loads(payload_text)
                internal_id = str(payload.get("channelId") or "").strip()
                if internal_id:
                    fields["archivarixChannelId"] = internal_id
                return fields
            if event in {"search:complete", "search:error"}:
                return fields
    return fields


def archivarix_channel_recovery_fields(payload: dict[str, Any]) -> dict[str, Any]:
    channel_id = str(payload.get("channelId") or "")
    if channel_id and not channel_id.startswith("UC"):
        channel_id = ""
    channel_url = str(payload.get("channelUrl") or "")
    if not channel_url and channel_id:
        channel_url = f"https://www.youtube.com/channel/{channel_id}"
    thumbnail_url = str(
        payload.get("thumbnailUrl")
        or payload.get("avatarLocalUrl")
        or payload.get("channelThumbnailUrl")
        or payload.get("channelAvatarLocalUrl")
        or ""
    )
    if thumbnail_url.startswith("/"):
        thumbnail_url = urllib.parse.urljoin("https://tube.archivarix.net", thumbnail_url)
    return {
        "channelTitle": str(payload.get("channelTitle") or payload.get("channel") or ""),
        "channelExternalId": channel_id,
        "channelUrl": channel_url,
        "channelThumbnailUrl": thumbnail_url,
        "archivarixChannelId": str(payload.get("archivarixChannelId") or payload.get("internalId") or ""),
        "channelDescription": str(payload.get("channelDescription") or payload.get("description") or ""),
        "channelAliases": ", ".join(str(item) for item in payload.get("channelAliases") or payload.get("aliases") or []),
    }


def apply_archivarix_channel_fields(video: dict[str, Any], channel_fields: dict[str, Any]) -> None:
    if not channel_fields:
        return
    if not video.get("channelTitle") and channel_fields.get("channelTitle"):
        video["channelTitle"] = channel_fields["channelTitle"]
    if not video.get("channelExternalId") and channel_fields.get("channelExternalId"):
        video["channelExternalId"] = channel_fields["channelExternalId"]
    if not video.get("channelUrl") and channel_fields.get("channelUrl"):
        video["channelUrl"] = channel_fields["channelUrl"]
    if not video.get("channelThumbnailUrl") and channel_fields.get("channelThumbnailUrl"):
        video["channelThumbnailUrl"] = channel_fields["channelThumbnailUrl"]


def cache_archivarix_thumbnail(
    video_id: str,
    thumbnail_url: str,
    thumb_dir: Path,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: int = 12,
    stop_event: threading.Event | None = None,
) -> str:
    if not video_id:
        return ""
    thumb_dir.mkdir(parents=True, exist_ok=True)
    opener = opener or network_opener()
    sources = [
        f"https://tube.archivarix.net/media/thumbnails/{video_id[:2]}/{video_id[2:4]}/{video_id}.jpg",
        f"https://web.archive.org/web/0im_/https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    ]
    if thumbnail_url:
        sources.append(thumbnail_url)
    body = b""
    content_type = ""
    for source in sources:
        if stop_event and stop_event.is_set():
            return ""
        try:
            body, content_type = request_bytes(
                opener,
                source,
                timeout=timeout,
                referer=f"https://tube.archivarix.net/?q={urllib.parse.quote(video_id)}",
            )
            if body:
                break
        except Exception:
            continue
    if not body:
        return ""
    ext = thumbnail_extension(content_type, thumbnail_url)
    target = thumb_dir / f"{safe_name(video_id)}{ext}"
    target.write_bytes(body)
    return local_asset_path(target)


def continuation_token(data: dict[str, Any]) -> str:
    for node in walk(data):
        if not isinstance(node, dict):
            continue
        renderer = node.get("continuationItemRenderer")
        if not isinstance(renderer, dict):
            continue
        endpoint = renderer.get("continuationEndpoint", {})
        command = endpoint.get("continuationCommand", {})
        token = command.get("token")
        if isinstance(token, str) and token:
            return token
    return ""


def playlist_continuation_token(data: dict[str, Any]) -> str:
    for node in walk(data):
        if not isinstance(node, dict):
            continue
        view_model = node.get("continuationItemViewModel")
        if isinstance(view_model, dict):
            token = (
                view_model.get("continuationCommand", {})
                .get("innertubeCommand", {})
                .get("continuationCommand", {})
                .get("token")
            )
            if isinstance(token, str) and token:
                return token
        renderer = node.get("continuationItemRenderer")
        if not isinstance(renderer, dict):
            continue
        endpoint = renderer.get("continuationEndpoint", {})
        executor = endpoint.get("commandExecutorCommand", {})
        for command in executor.get("commands", []) if isinstance(executor, dict) else []:
            if not isinstance(command, dict):
                continue
            token = command.get("continuationCommand", {}).get("token")
            if isinstance(token, str) and token:
                return token
        token = endpoint.get("continuationCommand", {}).get("token")
        if isinstance(token, str) and token:
            return token
    return ""


def playlist_video_renderers(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        node["playlistVideoRenderer"]
        for node in walk(data)
        if isinstance(node, dict) and isinstance(node.get("playlistVideoRenderer"), dict)
    ]


def playlist_panel_video_renderers(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        node["playlistPanelVideoRenderer"]
        for node in walk(data)
        if isinstance(node, dict) and isinstance(node.get("playlistPanelVideoRenderer"), dict)
    ]


def video_lockup_renderers(data: dict[str, Any]) -> list[dict[str, Any]]:
    lockups = []
    for node in walk(data):
        if not isinstance(node, dict) or not isinstance(node.get("lockupViewModel"), dict):
            continue
        lockup = node["lockupViewModel"]
        if lockup.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
            lockups.append(lockup)
    return lockups


def shorts_lockup_renderers(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        node["shortsLockupViewModel"]
        for node in walk(data)
        if isinstance(node, dict) and isinstance(node.get("shortsLockupViewModel"), dict)
    ]


def hidden_alert_count(data: dict[str, Any]) -> int:
    total = 0
    for node in walk(data):
        if not isinstance(node, dict):
            continue
        renderer = node.get("alertWithButtonRenderer")
        if not isinstance(renderer, dict):
            continue
        text = text_from_runs(renderer.get("text"))
        found = re.search(r"(\d+)\s+unavailable\s+videos?\s+is\s+hidden", text, re.I)
        if not found:
            found = re.search(r"(\d+)\s+unavailable\s+videos?\s+are\s+hidden", text, re.I)
        if found:
            total += int(found.group(1))
    return total


def unavailable_reason(renderer: dict[str, Any], title: str) -> str:
    reason = text_from_runs(renderer.get("unplayableText"))
    if reason:
        return reason
    lower_title = title.strip().lower()
    normalized_title = lower_title.strip("[]() ")
    if normalized_title in {"deleted video", "private video"}:
        return title
    if "unavailable" in lower_title:
        return title
    badges = []
    for badge in renderer.get("badges", []) or []:
        if isinstance(badge, dict):
            label = text_from_runs(badge.get("metadataBadgeRenderer", {}).get("label"))
            if label:
                badges.append(label)
    return ", ".join(badges)


def is_unavailable_video_renderer(renderer: dict[str, Any], title: str, reason: str) -> bool:
    if renderer.get("isPlayable") is False:
        return True
    lower_title = title.strip().lower()
    normalized_title = lower_title.strip("[]() ")
    if normalized_title in {"deleted video", "private video"}:
        return True
    if "unavailable" in lower_title or "deleted" in lower_title or "private" in lower_title:
        return True
    lower_reason = reason.lower()
    return any(word in lower_reason for word in ("unavailable", "deleted", "private"))


def playlist_video_playability(
    renderer: Mapping[str, Any],
    title: str = "",
    reason: str = "",
) -> int | None:
    """Return only playability explicitly supported by a playlist row."""
    for key in ("isPlayable", "is_playable"):
        if key not in renderer:
            continue
        explicit = renderer.get(key)
        if isinstance(explicit, bool) or (
            isinstance(explicit, int) and explicit in {0, 1}
        ):
            return int(explicit)
    if is_unavailable_video_renderer(dict(renderer), title, reason):
        return 0
    if playlist_entry_is_unavailable(title, reason):
        return 0
    return None


def playlist_video_availability(
    video_id: str,
    reason: str,
    playability: int | None,
) -> str:
    if playability == 0:
        return normalize_video_availability(
            video_id,
            reason or "unavailable",
            playability,
        )
    return "public" if video_id else "unknown"


def parse_video_renderer(
    playlist_id: str,
    renderer: dict[str, Any],
    fallback_position: int,
) -> dict[str, Any]:
    index_text = text_from_runs(renderer.get("index"))
    found_index = re.search(r"\d+", index_text)
    position = int(found_index.group(0)) if found_index else fallback_position
    video_id = renderer.get("videoId") if isinstance(renderer.get("videoId"), str) else ""
    title = text_from_runs(renderer.get("title")).strip() or "(untitled)"
    channel = text_from_runs(renderer.get("shortBylineText")).strip()
    duration = text_from_runs(renderer.get("lengthText")).strip()
    reason = unavailable_reason(renderer, title)
    playability = playlist_video_playability(renderer, title, reason)
    return {
        "playlist_id": playlist_id,
        "position": position,
        "video_id": video_id,
        "title": title,
        "channel": channel,
        "duration_text": duration,
        "is_playable": playability,
        "availability": playlist_video_availability(video_id, reason, playability),
        "url": f"https://www.youtube.com/watch?v={video_id}&list={playlist_id}" if video_id else "",
    }


def parse_panel_video_renderer(
    playlist_id: str,
    renderer: dict[str, Any],
    fallback_position: int,
) -> dict[str, Any]:
    index_text = text_from_runs(renderer.get("indexText") or renderer.get("index"))
    found_index = re.search(r"\d+", index_text)
    position = int(found_index.group(0)) if found_index else fallback_position
    video_id = renderer.get("videoId") if isinstance(renderer.get("videoId"), str) else ""
    title = text_from_runs(renderer.get("title")).strip() or "(untitled)"
    channel = text_from_runs(renderer.get("shortBylineText") or renderer.get("longBylineText")).strip()
    duration = text_from_runs(renderer.get("lengthText")).strip()
    reason = unavailable_reason(renderer, title)
    playability = playlist_video_playability(renderer, title, reason)
    return {
        "playlist_id": playlist_id,
        "position": position,
        "video_id": video_id,
        "title": title,
        "channel": channel,
        "duration_text": duration,
        "is_playable": playability,
        "availability": playlist_video_availability(video_id, reason, playability),
        "url": f"https://www.youtube.com/watch?v={video_id}&list={playlist_id}" if video_id else "",
    }


def parse_video_lockup(
    playlist_id: str,
    lockup: dict[str, Any],
    fallback_position: int,
) -> dict[str, Any]:
    video_id = lockup.get("contentId") if isinstance(lockup.get("contentId"), str) else ""
    title = content_text(
        lockup.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title")
    ).strip() or "(untitled)"
    channel = ""
    duration = ""
    rows = lockup_metadata_rows(lockup)
    if rows and rows[0]:
        channel = rows[0][0]
    for node in walk(lockup):
        if not isinstance(node, dict):
            continue
        badge = node.get("thumbnailBadgeViewModel")
        if isinstance(badge, dict):
            text = badge.get("text")
            if isinstance(text, str) and re.search(r"\d+:\d+", text):
                duration = text
                break
    watch_url = ""
    for node in walk(lockup):
        if not isinstance(node, dict):
            continue
        endpoint = node.get("watchEndpoint")
        if isinstance(endpoint, dict) and isinstance(endpoint.get("index"), int):
            fallback_position = int(endpoint["index"]) + 1
            break
    if video_id:
        watch_url = f"https://www.youtube.com/watch?v={video_id}&list={playlist_id}"
    reason = unavailable_reason(lockup, title)
    playability = playlist_video_playability(lockup, title, reason)
    return {
        "playlist_id": playlist_id,
        "position": fallback_position,
        "video_id": video_id,
        "title": title,
        "channel": channel,
        "duration_text": duration,
        "is_playable": playability,
        "availability": playlist_video_availability(video_id, reason, playability),
        "url": watch_url,
    }


def append_playlist_occurrence(
    videos: list[dict[str, Any]],
    seen_positions: set[int],
    seen_entries: set[tuple[int, str]],
    video: dict[str, Any],
) -> bool:
    position = max(1, int(video.get("position") or len(videos) + 1))
    video_id = str(video.get("video_id") or "").strip()
    entry_key = (position, video_id)
    if entry_key in seen_entries:
        return False
    seen_entries.add(entry_key)
    while position in seen_positions:
        position += 1
    cleaned = dict(video)
    cleaned["position"] = position
    seen_positions.add(position)
    videos.append(cleaned)
    return True


def playlist_duplicate_counts(videos: list[dict[str, Any]]) -> tuple[int, int]:
    counts = Counter(
        str(video.get("video_id") or "").strip()
        for video in videos
        if str(video.get("video_id") or "").strip()
    )
    duplicate_counts = [count for count in counts.values() if count > 1]
    return len(duplicate_counts), sum(count - 1 for count in duplicate_counts)


def history_date_from_label(label: str, today: date | None = None) -> str:
    today = today or date.today()
    cleaned = re.sub(r"\s+", " ", label).strip()
    if not cleaned:
        return ""
    lower = cleaned.lower()
    if lower == "today":
        return today.isoformat()
    if lower == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    if lower in weekdays:
        delta = (today.weekday() - weekdays[lower]) % 7
        if delta == 0:
            delta = 7
        return (today - timedelta(days=delta)).isoformat()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d", "%B %d"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        year = parsed.year if "%Y" in fmt else today.year
        candidate = date(year, parsed.month, parsed.day)
        if "%Y" not in fmt and candidate > today:
            candidate = date(year - 1, parsed.month, parsed.day)
        return candidate.isoformat()
    return ""


TZ_OFFSETS = {
    "UTC": "+00:00",
    "GMT": "+00:00",
    "PST": "-08:00",
    "PDT": "-07:00",
    "MST": "-07:00",
    "MDT": "-06:00",
    "CST": "-06:00",
    "CDT": "-05:00",
    "EST": "-05:00",
    "EDT": "-04:00",
}


def local_date_for_utc_instant(value: str, timezone_name: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        return value[:10]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(zone).date().isoformat()


def history_upload_date_conflicts(
    conn: sqlite3.Connection,
    video_id: str,
    timezone_name: str,
) -> list[dict[str, str]]:
    video = conn.execute(
        "SELECT upload_date FROM videos WHERE video_id = ?",
        (video_id,),
    ).fetchone()
    upload_date = str(video["upload_date"] or "").strip() if video else ""
    if not upload_date:
        return []
    published_date = (
        upload_date
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", upload_date)
        else local_date_for_utc_instant(upload_date, timezone_name)
    )
    if not published_date:
        return []
    return [
        {
            "event_id": str(row["event_id"]),
            "video_id": video_id,
            "watch_date": str(row["watch_date"]),
            "published_date": published_date,
        }
        for row in conn.execute(
            """
            SELECT event_id, watch_date
            FROM history_events
            WHERE video_id = ?
              AND watched_at IS NULL
              AND watch_date IS NOT NULL
              AND watch_date <> ''
              AND watch_date < ?
            ORDER BY watch_date, event_id
            """,
            (video_id, published_date),
        )
    ]


def takeout_watch_datetime(watched_at: str) -> str:
    cleaned = re.sub(r"\s+", " ", watched_at).strip()
    iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$", cleaned)
    if iso_match:
        offset = "+00:00" if iso_match.group(3) == "Z" else iso_match.group(3)
        return normalize_utc_timestamp(f"{iso_match.group(1)}T{iso_match.group(2)}{offset}")
    match = re.match(r"^(.*)\s+([A-Z]{2,5})$", cleaned)
    if not match:
        return ""
    datetime_text, tz_name = match.groups()
    offset = TZ_OFFSETS.get(tz_name)
    if not offset:
        return ""
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%B %d, %Y, %I:%M:%S %p"):
        try:
            parsed = datetime.strptime(datetime_text, fmt)
            return normalize_utc_timestamp(parsed.strftime("%Y-%m-%dT%H:%M:%S") + offset)
        except ValueError:
            continue
    return ""


def bounded_int(value: Any, minimum: int = 0, maximum: int = 100) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(minimum, min(maximum, number))


def compact_json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def reaction_from_button_node(node: dict[str, Any]) -> str:
    blob = compact_json_text(node).lower()
    icon_types = {
        str(item.get("iconType") or "").lower()
        for item in walk(node)
        if isinstance(item, dict) and item.get("iconType")
    }
    if "dislike" in icon_types or re.search(r"\bdislike(?:d)?\b", blob):
        return "DISLIKE"
    if "like" in icon_types or re.search(r"\blike(?:d)?\b", blob):
        return "LIKE"
    return ""


def extract_reaction_from_initial_data(initial_data: dict[str, Any]) -> str:
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        like_status = node.get("likeStatusEntity")
        if isinstance(like_status, dict):
            status = str(like_status.get("likeStatus") or "").strip().upper()
            if status in {"LIKE", "DISLIKE", "INDIFFERENT"}:
                return status
        if node.get("isToggled") is True or node.get("isToggledButton") is True:
            reaction = reaction_from_button_node(node)
            if reaction:
                return reaction
        toggle_model = node.get("toggleButtonViewModel")
        if isinstance(toggle_model, dict) and toggle_model.get("isToggled") is True:
            reaction = reaction_from_button_node(node)
            if reaction:
                return reaction
    return ""


def extract_watch_status_from_card(card: dict[str, Any], video_id: str) -> tuple[int, int]:
    progress = 0
    resume_seconds = 0
    for node in walk(card):
        if not isinstance(node, dict):
            continue
        resume = node.get("thumbnailOverlayResumePlaybackRenderer")
        if isinstance(resume, dict) and not progress:
            progress = bounded_int(resume.get("percentDurationWatched"))
        progress_model = node.get("thumbnailOverlayProgressBarViewModel")
        if isinstance(progress_model, dict) and not progress:
            progress = bounded_int(progress_model.get("startPercent"))
        endpoint = node.get("watchEndpoint")
        if isinstance(endpoint, dict) and endpoint.get("videoId") == video_id and not resume_seconds:
            start = endpoint.get("startTimeSeconds")
            if isinstance(start, int) and start > 0:
                resume_seconds = start
    return progress, resume_seconds


def parse_history_lockup(lockup: dict[str, Any], normalized_date: str) -> dict[str, Any] | None:
    video_id = lockup.get("contentId") if isinstance(lockup.get("contentId"), str) else ""
    if not video_id:
        for node in walk(lockup):
            if not isinstance(node, dict):
                continue
            endpoint = node.get("watchEndpoint") or node.get("reelWatchEndpoint")
            if isinstance(endpoint, dict) and isinstance(endpoint.get("videoId"), str):
                video_id = endpoint["videoId"]
                break
    if not video_id:
        return None
    title = content_text(
        lockup.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title")
    ).strip()
    rows = lockup_metadata_rows(lockup)
    channel = rows[0][0] if rows and rows[0] else ""
    channel_url = ""
    url = ""
    watch_progress_percent, watch_resume_seconds = extract_watch_status_from_card(lockup, video_id)
    for node in walk(lockup):
        if not isinstance(node, dict):
            continue
        endpoint = node.get("watchEndpoint")
        if isinstance(endpoint, dict) and endpoint.get("videoId") == video_id:
            start = endpoint.get("startTimeSeconds")
            url = f"https://www.youtube.com/watch?v={video_id}"
            if isinstance(start, int) and start > 0:
                url = f"{url}&t={start}s"
                if not watch_resume_seconds:
                    watch_resume_seconds = start
            break
        endpoint = node.get("reelWatchEndpoint")
        if isinstance(endpoint, dict) and endpoint.get("videoId") == video_id:
            url = f"https://www.youtube.com/shorts/{video_id}"
            break
    if not url:
        url = f"https://www.youtube.com/watch?v={video_id}"
    return {
        "video_id": video_id,
        "title": video_title_or_blank(title, video_id),
        "url": url,
        "channel": channel,
        "channel_url": channel_url,
        "watch_date": normalized_date,
        "watch_progress_percent": watch_progress_percent,
        "watch_resume_seconds": watch_resume_seconds,
    }


def history_sections(data: dict[str, Any], today: date | None = None) -> list[tuple[str, str, list[dict[str, Any]]]]:
    sections: list[tuple[str, str, list[dict[str, Any]]]] = []
    for node in walk(data):
        if not isinstance(node, dict):
            continue
        renderer = node.get("itemSectionRenderer")
        if not isinstance(renderer, dict):
            continue
        title = (
            renderer.get("header", {})
            .get("itemSectionHeaderRenderer", {})
            .get("title", {})
        )
        label = text_from_runs(title).strip()
        if not label:
            continue
        normalized = history_date_from_label(label, today=today)
        rows: list[dict[str, Any]] = []
        for content in renderer.get("contents") or []:
            if not isinstance(content, dict):
                continue
            lockup = content.get("lockupViewModel")
            if isinstance(lockup, dict):
                row = parse_history_lockup(lockup, normalized)
                if row:
                    rows.append(row)
        if rows:
            sections.append((label, normalized, rows))
    return sections


def fetch_youtube_history_web(
    cookie_file: Path,
    limit: int = 100,
    start: int = 1,
    timezone_name: str = DEFAULT_DISPLAY_TIMEZONE,
    proxy_url: str = "",
) -> list[dict[str, Any]]:
    jar = load_cookie_jar(cookie_file)
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        *socks5_proxy_handlers(proxy_url),
    )
    referer = "https://www.youtube.com/feed/history"
    page = request_text(opener, referer)
    if "Watch history isn't viewable when signed out" in page or "Keep track of what you watch" in page:
        raise RuntimeError("YouTube history page is not signed in with the provided cookie file.")
    config = extract_ytcfg(page)
    api_key = config.get("INNERTUBE_API_KEY", "")
    client_version = config.get("INNERTUBE_CLIENT_VERSION", "")
    if not api_key or not client_version:
        raise RuntimeError("Could not find YouTube web API configuration in the history page.")
    context = youtube_web_context(config, timezone_name)
    pages = [
        request_youtubei_json(
            opener,
            jar,
            api_key,
            {"context": context, "browseId": "FEhistory"},
            referer,
            client_version,
        )
    ]
    token = continuation_token(pages[0])
    seen_tokens: set[str] = set()
    max_needed = max(1, start) + max(1, limit) - 1
    all_rows: list[dict[str, Any]] = []
    try:
        today = datetime.now(ZoneInfo(timezone_name)).date()
    except ZoneInfoNotFoundError:
        today = datetime.now(timezone.utc).date()

    while pages:
        page_data = pages.pop(0)
        for _label, _normalized, section_rows in history_sections(page_data, today=today):
            all_rows.extend(section_rows)
        if len(all_rows) >= max_needed:
            break
        if not token or token in seen_tokens or not api_key or not client_version:
            break
        seen_tokens.add(token)
        payload = {
            "context": context,
            "continuation": token,
        }
        next_page = request_youtubei_json(opener, jar, api_key, payload, referer, client_version)
        pages.append(next_page)
        token = continuation_token(next_page)

    offset = max(1, start) - 1
    return all_rows[offset : offset + max(1, limit)]


def parse_shorts_lockup(
    playlist_id: str,
    renderer: dict[str, Any],
    fallback_position: int,
) -> dict[str, Any]:
    endpoint = (
        renderer.get("onTap", {})
        .get("innertubeCommand", {})
        .get("reelWatchEndpoint", {})
    )
    video_id = endpoint.get("videoId") if isinstance(endpoint.get("videoId"), str) else ""
    text = str(renderer.get("accessibilityText") or "").strip()
    title = text
    if "," in title:
        title = title.rsplit(",", 1)[0].strip()
    title = video_title_or_blank(title, video_id)
    reason = unavailable_reason(renderer, title)
    playability = playlist_video_playability(renderer, title, reason)
    return {
        "playlist_id": playlist_id,
        "position": fallback_position,
        "video_id": video_id,
        "title": title,
        "channel": "",
        "duration_text": "Short",
        "is_playable": playability,
        "availability": playlist_video_availability(video_id, reason, playability),
        "url": f"https://www.youtube.com/shorts/{video_id}" if video_id else "",
    }


def extract_channel_thumbnail_url(initial_data: dict[str, Any]) -> str:
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        avatar = node.get("avatarViewModel")
        if isinstance(avatar, dict):
            image = avatar.get("image")
            if isinstance(image, dict):
                url = pick_thumbnail(image.get("sources", []))
                if url:
                    return url
        for key in ("videoOwnerRenderer", "channelThumbnailWithLinkRenderer"):
            renderer = node.get(key)
            if not isinstance(renderer, dict):
                continue
            thumbnail = renderer.get("thumbnail")
            if isinstance(thumbnail, dict):
                url = pick_thumbnail(thumbnail.get("thumbnails", []))
                if url:
                    return url
    return ""


def youtube_path_url(value: str) -> str:
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        if value.startswith("http://www.youtube.com/") or value.startswith("http://youtube.com/"):
            return "https://" + value[len("http://") :]
        return value
    if value.startswith("/"):
        return f"https://www.youtube.com{value}"
    return ""


def endpoint_channel_url(endpoint: dict[str, Any]) -> str:
    metadata = endpoint.get("commandMetadata", {}).get("webCommandMetadata", {})
    if isinstance(metadata, dict):
        url = youtube_path_url(str(metadata.get("url") or ""))
        if url:
            return url
    browse = endpoint.get("browseEndpoint")
    if isinstance(browse, dict):
        canonical = youtube_path_url(str(browse.get("canonicalBaseUrl") or ""))
        if canonical:
            return canonical
        browse_id = str(browse.get("browseId") or "")
        if browse_id:
            return f"https://www.youtube.com/channel/{urllib.parse.quote(browse_id)}"
    return ""


def extract_channel_handle_aliases(
    initial_data: dict[str, Any],
    channel_id: str = "",
) -> str:
    aliases: list[str] = []
    seen: set[str] = set()

    def add_alias(value: str) -> None:
        found = re.search(r"(?:^|/)@([A-Za-z0-9._-]+)(?:$|[/?#])", value)
        if not found:
            return
        alias = f"@{found.group(1)}"
        key = alias.casefold()
        if key not in seen:
            seen.add(key)
            aliases.append(alias)

    resolved_channel_id = (channel_id or "").strip()
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        renderer = node.get("channelMetadataRenderer")
        if not isinstance(renderer, dict):
            continue
        renderer_channel_id = str(renderer.get("externalId") or "").strip()
        if resolved_channel_id and renderer_channel_id and renderer_channel_id != resolved_channel_id:
            continue
        if renderer_channel_id:
            resolved_channel_id = renderer_channel_id
        for value in renderer.get("ownerUrls") or []:
            add_alias(str(value or ""))

    owner_endpoints: list[dict[str, Any]] = []
    channel_ids: set[str] = set()
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        browse = node.get("browseEndpoint")
        if not isinstance(browse, dict):
            continue
        browse_id = str(browse.get("browseId") or "").strip()
        if browse_id.startswith("UC"):
            channel_ids.add(browse_id)
        owner_endpoints.append(node)

    if not resolved_channel_id and len(channel_ids) == 1:
        resolved_channel_id = next(iter(channel_ids))
    if resolved_channel_id:
        for endpoint in owner_endpoints:
            browse = endpoint.get("browseEndpoint") or {}
            if str(browse.get("browseId") or "").strip() != resolved_channel_id:
                continue
            add_alias(str(browse.get("canonicalBaseUrl") or ""))
            metadata = endpoint.get("commandMetadata", {}).get("webCommandMetadata", {})
            if isinstance(metadata, dict):
                add_alias(str(metadata.get("url") or ""))
    return ", ".join(aliases)


def extract_channel_featured_channels(
    initial_data: dict[str, Any],
    owner_channel_id: str = "",
) -> list[dict[str, Any]]:
    featured_channels: list[dict[str, Any]] = []
    seen: set[str] = set()
    owner_channel_id = (owner_channel_id or "").strip()
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        shelf = node.get("shelfRenderer")
        if not isinstance(shelf, dict):
            continue
        shelf_title = text_from_runs(shelf.get("title")).strip().casefold()
        if shelf_title not in {"featured", "featured channels"}:
            continue
        for item in walk(shelf.get("content")):
            if not isinstance(item, dict):
                continue
            renderer = item.get("gridChannelRenderer")
            if not isinstance(renderer, dict):
                continue
            endpoint = renderer.get("navigationEndpoint")
            if not isinstance(endpoint, dict):
                continue
            browse = endpoint.get("browseEndpoint")
            if not isinstance(browse, dict):
                continue
            featured_channel_id = str(browse.get("browseId") or "").strip()
            if (
                not featured_channel_id.startswith("UC")
                or featured_channel_id == owner_channel_id
                or featured_channel_id in seen
            ):
                continue
            seen.add(featured_channel_id)
            channel_url = endpoint_channel_url(endpoint)
            channel_reference = (
                youtube_channel_ref_from_url(channel_url) or featured_channel_id
            )
            featured_channels.append(
                {
                    "channel_id": featured_channel_id,
                    "title": text_from_runs(renderer.get("title")).strip(),
                    "channel_reference": channel_reference,
                    "position": len(featured_channels),
                }
            )
    return featured_channels


def extract_channel_url(initial_data: dict[str, Any]) -> str:
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        for key in ("videoOwnerRenderer", "channelThumbnailWithLinkRenderer"):
            renderer = node.get(key)
            if not isinstance(renderer, dict):
                continue
            endpoint = renderer.get("navigationEndpoint")
            if isinstance(endpoint, dict):
                url = endpoint_channel_url(endpoint)
                if url:
                    return url
    return ""


def extract_channel_id(initial_data: dict[str, Any], channel_url: str = "") -> str:
    channel_id = youtube_channel_id_from_url(channel_url)
    if channel_id:
        return channel_id
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        browse = node.get("browseEndpoint")
        if isinstance(browse, dict):
            browse_id = str(browse.get("browseId") or "")
            if browse_id.startswith("UC"):
                return browse_id
    return ""


def channel_id_from_entity_key(entity: dict[str, Any]) -> str:
    key = urllib.parse.unquote(str(entity.get("key") or "").strip())
    if not key:
        return ""
    try:
        padded_key = key + ("=" * (-len(key) % 4))
        decoded_key = base64.urlsafe_b64decode(padded_key).decode(
            "utf-8",
            errors="ignore",
        )
    except (binascii.Error, ValueError):
        return ""
    found = re.search(r"UC[A-Za-z0-9_-]{20,}", decoded_key)
    return found.group(0) if found else ""


def channel_entity_candidates(
    initial_data: dict[str, Any],
    entity_name: str,
    channel_id: str,
) -> list[dict[str, Any]]:
    matching: list[dict[str, Any]] = []
    unscoped: list[dict[str, Any]] = []
    found_scoped = False
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        entity = node.get(entity_name)
        if not isinstance(entity, dict):
            continue
        entity_channel_id = channel_id_from_entity_key(entity)
        if entity_channel_id:
            found_scoped = True
            if not channel_id or entity_channel_id == channel_id:
                matching.append(entity)
        else:
            unscoped.append(entity)
    if matching:
        return matching
    return [] if channel_id and found_scoped else unscoped


def extract_channel_subscription_state(
    initial_data: dict[str, Any],
    channel_id: str = "",
) -> bool | None:
    for entity in channel_entity_candidates(
        initial_data,
        "subscriptionStateEntity",
        channel_id,
    ):
        subscribed = entity.get("subscribed")
        if isinstance(subscribed, bool):
            return subscribed

    for node in walk(initial_data):
        for key in ("subscriptionButtonRenderer", "subscribeButtonViewModel"):
            renderer = node.get(key)
            if not isinstance(renderer, dict):
                continue
            renderer_channel_id = str(renderer.get("channelId") or "").strip()
            if channel_id and renderer_channel_id and renderer_channel_id != channel_id:
                continue
            subscribed = renderer.get("subscribed")
            if isinstance(subscribed, bool):
                return subscribed
    return None


def normalize_channel_notification_level(value: str) -> str:
    normalized = str(value or "").strip().upper()
    normalized = normalized.removeprefix("SUBSCRIPTION_NOTIFICATION_STATE_")
    return {
        "ALL": "all",
        "OCCASIONAL": "personalized",
        "PERSONALIZED": "personalized",
        "NONE": "none",
        "OFF": "none",
    }.get(normalized, "")


def extract_channel_notification_level(
    initial_data: dict[str, Any],
    channel_id: str = "",
) -> str:
    for entity in channel_entity_candidates(
        initial_data,
        "subscriptionNotificationStateEntity",
        channel_id,
    ):
        level = normalize_channel_notification_level(str(entity.get("state") or ""))
        if level:
            return level
    return ""


def extract_channel_page_metadata(html_text: str, channel_id: str) -> dict[str, Any]:
    initial_data = extract_json_assignment(html_text, "ytInitialData")
    title = ""
    channel_url = youtube_channel_url(channel_id)
    thumbnail_url = ""
    found_channel_id = channel_id
    expected_channel_id = channel_id if is_canonical_youtube_channel_id(channel_id) else ""
    description = ""
    status = ""
    status_reason = ""
    channel_identity_observed = False
    channel_status_observed = False
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        renderer = node.get("channelMetadataRenderer")
        if not isinstance(renderer, dict):
            continue
        renderer_channel_id = str(renderer.get("externalId") or "").strip()
        if not renderer_channel_id:
            renderer_channel_id = youtube_channel_id_from_url(
                str(renderer.get("channelUrl") or "")
            )
        if not renderer_channel_id or (
            expected_channel_id and renderer_channel_id != expected_channel_id
        ):
            continue
        title = str(renderer.get("title") or title or "").strip()
        description = str(renderer.get("description") or description or "").strip()
        found_channel_id = renderer_channel_id
        channel_url = youtube_path_url(str(renderer.get("channelUrl") or "")) or channel_url
        avatar = renderer.get("avatar")
        if isinstance(avatar, dict):
            thumbnail_url = pick_thumbnail(avatar.get("thumbnails", [])) or thumbnail_url
        channel_identity_observed = True
        channel_status_observed = True
        break
    lower_text = html.unescape(re.sub(r"\s+", " ", html_text)).lower()
    if "this account has been terminated" in lower_text:
        status = "terminated"
        channel_status_observed = True
        found = re.search(
            r"(This account has been terminated[^<\"{}]+)",
            html_text,
            flags=re.IGNORECASE,
        )
        if found:
            status_reason = html.unescape(re.sub(r"\s+", " ", found.group(1))).strip()
        if not status_reason:
            status_reason = "This account has been terminated."
    elif "this channel was removed because it violated our community guidelines" in lower_text:
        status = "terminated"
        status_reason = "This channel was removed because it violated YouTube Community Guidelines."
        channel_status_observed = True
    if not thumbnail_url:
        thumbnail_url = extract_channel_thumbnail_url(initial_data)
    if not title:
        found = re.search(r"<title>(.*?)</title>", html_text, flags=re.DOTALL)
        if found:
            title = html.unescape(re.sub(r"\s+", " ", found.group(1))).strip()
            title = re.sub(r"\s+-\s+YouTube$", "", title)
    subscribed = extract_channel_subscription_state(initial_data, found_channel_id)
    notification_level = extract_channel_notification_level(initial_data, found_channel_id)
    return {
        "channel_id": found_channel_id or channel_id,
        "channel": title,
        "channel_url": channel_url,
        "channel_description": description,
        "channel_aliases": extract_channel_handle_aliases(initial_data, found_channel_id),
        "channel_aliases_observed": channel_identity_observed,
        "channel_featured_channels": extract_channel_featured_channels(
            initial_data,
            found_channel_id,
        ),
        "channel_featured_channels_observed": channel_identity_observed,
        "channel_thumbnail_url": absolute_url(thumbnail_url),
        "channel_status": status,
        "channel_status_reason": status_reason,
        "channel_status_observed": channel_status_observed,
        "channel_subscribed": "" if subscribed is None else str(int(subscribed)),
        "channel_notification_level": notification_level,
        "archivarix_channel_id": "",
    }


def merge_channel_metadata(
    primary: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    subscribed = primary.get("channel_subscribed", "")
    if subscribed not in {"0", "1"}:
        subscribed = fallback.get("channel_subscribed", "")
    notification_level = normalize_channel_notification_level(
        primary.get("channel_notification_level", "")
    )
    if not notification_level:
        notification_level = normalize_channel_notification_level(
            fallback.get("channel_notification_level", "")
        )
    primary_status_observed = bool(primary.get("channel_status_observed"))
    channel_status = (
        str(primary.get("channel_status") or "").strip()
        if primary_status_observed
        else str(primary.get("channel_status") or fallback.get("channel_status") or "").strip()
    )
    channel_status_reason = (
        str(primary.get("channel_status_reason") or "").strip()
        if primary_status_observed
        else str(
            primary.get("channel_status_reason")
            or fallback.get("channel_status_reason")
            or ""
        ).strip()
    )
    return {
        "channel_id": primary.get("channel_id", "") or fallback.get("channel_id", ""),
        "channel": primary.get("channel", "") or fallback.get("channel", ""),
        "channel_url": primary.get("channel_url", "") or fallback.get("channel_url", ""),
        "channel_description": primary.get("channel_description", "") or fallback.get("channel_description", ""),
        "channel_aliases": (
            primary.get("channel_aliases", "")
            if primary.get("channel_aliases_observed")
            else primary.get("channel_aliases", "") or fallback.get("channel_aliases", "")
        ),
        "channel_aliases_observed": bool(primary.get("channel_aliases_observed")),
        "channel_featured_channels": (
            primary.get("channel_featured_channels", [])
            if primary.get("channel_featured_channels_observed")
            else primary.get("channel_featured_channels", [])
            or fallback.get("channel_featured_channels", [])
        ),
        "channel_featured_channels_observed": bool(
            primary.get("channel_featured_channels_observed")
        ),
        "channel_thumbnail_url": primary.get("channel_thumbnail_url", "") or fallback.get("channel_thumbnail_url", ""),
        "channel_status": channel_status,
        "channel_status_reason": channel_status_reason,
        "channel_status_observed": primary_status_observed,
        "channel_subscribed": subscribed if subscribed in {"0", "1"} else "",
        "channel_notification_level": notification_level,
        "archivarix_channel_id": primary.get("archivarix_channel_id", "") or fallback.get("archivarix_channel_id", ""),
    }


def archivarix_channel_metadata(fields: dict[str, Any], channel_id: str) -> dict[str, Any]:
    status = str(fields.get("channelStatus") or "").strip()
    status_reason = "Deleted/terminated channel reported by Archivarix." if status == "deleted" else ""
    return {
        "channel_id": str(fields.get("channelExternalId") or channel_id or "").strip(),
        "channel": str(fields.get("channelTitle") or "").strip(),
        "channel_url": str(fields.get("channelUrl") or youtube_channel_url(channel_id) or "").strip(),
        "channel_description": str(fields.get("channelDescription") or "").strip(),
        "channel_aliases": str(fields.get("channelAliases") or "").strip(),
        "channel_featured_channels": [],
        "channel_featured_channels_observed": False,
        "channel_thumbnail_url": absolute_url(str(fields.get("channelThumbnailUrl") or "")),
        "channel_status": status,
        "channel_status_reason": status_reason,
        "channel_subscribed": "",
        "channel_notification_level": "",
        "archivarix_channel_id": str(fields.get("archivarixChannelId") or "").strip(),
    }


def fetch_channel_metadata(
    opener: urllib.request.OpenerDirector,
    channel_id: str,
    thumb_dir: Path,
    require_authenticated: bool = False,
    proxy_url: str = "",
) -> dict[str, Any]:
    channel_url = youtube_channel_url(channel_id)
    page = request_text(opener, channel_url)
    authenticated = youtube_page_is_authenticated(page)
    if require_authenticated and not authenticated:
        raise youtube_authentication_error(page, "channel page")
    metadata = extract_channel_page_metadata(page, channel_id)
    if not authenticated:
        metadata["channel_subscribed"] = ""
        metadata["channel_notification_level"] = ""
    if (
        metadata.get("channel_status") == "terminated"
        or not (metadata.get("channel") and metadata.get("channel_thumbnail_url"))
    ):
        try:
            archivarix_opener = load_cookie_opener(ARCHIVARIX_COOKIE_FILE, proxy_url)
            archivarix_fields = archivarix_lookup_channel(channel_id, archivarix_opener)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            archivarix_fields = {}
        if archivarix_fields:
            metadata = merge_channel_metadata(
                metadata,
                archivarix_channel_metadata(archivarix_fields, channel_id),
            )
    metadata["requested_channel_reference"] = channel_id
    metadata["channel_thumbnail_path"] = cache_channel_thumbnail(
        opener,
        metadata.get("channel_id", "") or channel_id,
        metadata.get("channel_thumbnail_url", ""),
        thumb_dir,
        referer_url=channel_url,
    )
    return metadata


def youtube_video_type(
    details: Mapping[str, Any],
    microformat: Mapping[str, Any],
) -> str:
    live_details = microformat.get("liveBroadcastDetails")
    if not isinstance(live_details, Mapping):
        live_details = {}
    if (
        details.get("isLiveContent") is True
        or details.get("isLive") is True
        or details.get("isUpcoming") is True
        or live_details.get("isLiveNow") is True
    ):
        return "livestream"
    canonical_url = str(microformat.get("canonicalUrl") or "").strip()
    canonical_path = urllib.parse.urlparse(canonical_url).path.rstrip("/")
    if microformat.get("isShortsEligible") is True or canonical_path.startswith(
        "/shorts/"
    ):
        return "short"
    if details.get("isTvfilmVideo") is True:
        return "movie"
    if microformat.get("isShortsEligible") is False or canonical_path == "/watch":
        return "video"
    return ""


def youtube_broadcast_metadata(
    details: Mapping[str, Any],
    microformat: Mapping[str, Any],
    *,
    observed_at: str | None = None,
) -> dict[str, str | None]:
    live_details = microformat.get("liveBroadcastDetails")
    if not isinstance(live_details, Mapping):
        live_details = {}
    is_live = (
        live_details.get("isLiveNow") is True or details.get("isLive") is True
    )
    is_upcoming = details.get("isUpcoming") is True

    if "isLiveContent" not in details and not is_live and not is_upcoming:
        return {
            "broadcast_status": None,
            "broadcast_started_at": None,
            "broadcast_ended_at": None,
        }
    if details.get("isLiveContent") is not True and not is_live and not is_upcoming:
        return {
            "broadcast_status": "",
            "broadcast_started_at": "",
            "broadcast_ended_at": "",
        }

    def normalized_timestamp(name: str) -> str:
        value = str(live_details.get(name) or "").strip()
        if not value:
            return ""
        try:
            return normalize_utc_timestamp(value)
        except ValueError:
            return ""

    started_at = normalized_timestamp("startTimestamp")
    ended_at = normalized_timestamp("endTimestamp")
    if is_live:
        status = "live"
    elif is_upcoming:
        status = "upcoming"
    elif ended_at:
        status = "ended"
    elif started_at:
        comparison_time = observed_at or utc_now()
        try:
            status = (
                "upcoming"
                if normalize_utc_timestamp(started_at) > normalize_utc_timestamp(comparison_time)
                else "ended"
            )
        except ValueError:
            status = None
    else:
        status = None
    return {
        "broadcast_status": status,
        "broadcast_started_at": started_at or None,
        "broadcast_ended_at": ended_at or None,
    }


def youtube_movie_metadata(
    details: Mapping[str, Any],
    initial_data: Mapping[str, Any],
) -> dict[str, str]:
    if details.get("isTvfilmVideo") is not True:
        return {
            "movie_rating": "",
            "movie_release_date": "",
            "movie_offer": "",
        }

    metadata_rows: dict[str, str] = {}
    row_container = _first_youtube_renderer(
        initial_data,
        "metadataRowContainerRenderer",
    )
    for row in row_container.get("rows", []):
        renderer = row.get("metadataRowRenderer", {}) if isinstance(row, dict) else {}
        label = text_from_runs(renderer.get("title") or {}).strip().casefold()
        values = [
            text_from_runs(value).strip()
            for value in renderer.get("contents", [])
            if isinstance(value, dict) and text_from_runs(value).strip()
        ]
        if label and values:
            metadata_rows[label] = ", ".join(values)

    offer = ""
    fallback_rating = ""
    primary_info = _first_youtube_renderer(initial_data, "videoPrimaryInfoRenderer")
    for badge_node in primary_info.get("badges", []):
        badge = (
            badge_node.get("metadataBadgeRenderer", {})
            if isinstance(badge_node, dict)
            else {}
        )
        style = str(badge.get("style") or "").strip().upper()
        label = str(badge.get("label") or "").strip()
        if style == "BADGE_STYLE_TYPE_YPC" and label:
            offer = label
        elif style == "BADGE_STYLE_TYPE_MEDIA" and label:
            fallback_rating = label

    factoids: dict[str, str] = {}
    for node in walk(initial_data):
        renderer = node.get("factoidRenderer") if isinstance(node, dict) else None
        if not isinstance(renderer, dict):
            continue
        label = text_from_runs(renderer.get("label") or {}).strip().casefold()
        value = text_from_runs(renderer.get("value") or {}).strip()
        if label and value:
            factoids[label] = value

    return {
        "movie_rating": metadata_rows.get("rating") or factoids.get("rating") or fallback_rating,
        "movie_release_date": (
            metadata_rows.get("release date")
            or factoids.get("release date")
            or factoids.get("release year")
            or ""
        ),
        "movie_offer": offer,
    }


def youtube_video_feature_metadata(
    player: Mapping[str, Any],
    initial_data: Mapping[str, Any],
) -> dict[str, Any]:
    playability = player.get("playabilityStatus", {})
    details = player.get("videoDetails", {})
    page_observed = (
        str(playability.get("status") or "").strip().upper() == "OK"
        and bool(details)
    )
    streaming_data = player.get("streamingData", {})
    formats = [
        item
        for key in ("formats", "adaptiveFormats")
        for item in streaming_data.get(key, [])
        if isinstance(item, dict)
        and str(item.get("mimeType") or "").lower().startswith("video/")
    ]
    formats_observed = page_observed and bool(formats)
    if not formats_observed:
        max_video_height = None
        spatial_format = None
        stereo_layout = None
        dynamic_range = None
    else:
        heights = []
        for item in formats:
            try:
                height = int(item.get("height") or 0)
            except (TypeError, ValueError):
                height = 0
            if height > 0:
                heights.append(height)
        max_video_height = max(heights) if heights else None

        primary_info = _first_youtube_renderer(initial_data, "videoPrimaryInfoRenderer")
        badge_labels = {
            str(badge.get("label") or "").strip().casefold()
            for node in primary_info.get("badges", [])
            if isinstance(node, dict)
            and isinstance((badge := node.get("metadataBadgeRenderer")), dict)
        }
        vr_config = player.get("playerConfig", {}).get("vrConfig", {})
        partial_spherical = vr_config.get("partialSpherical")
        projections = {
            str(item.get("projectionType") or "").strip().upper()
            for item in formats
            if item.get("projectionType")
        }
        if "vr180" in badge_labels or partial_spherical is True:
            spatial_format = "vr180"
        elif "EQUIRECTANGULAR" in projections or (
            "MESH" in projections and partial_spherical is False
        ):
            spatial_format = "360"
        elif "MESH" in projections:
            spatial_format = None
        else:
            spatial_format = ""

        stereo_values = {
            str(item.get("stereoLayout") or "").strip().upper()
            for item in formats
            if item.get("stereoLayout")
        }
        if "STEREO_LAYOUT_LEFT_RIGHT" in stereo_values:
            stereo_layout = "left_right"
        elif "STEREO_LAYOUT_TOP_BOTTOM" in stereo_values:
            stereo_layout = "top_bottom"
        else:
            stereo_layout = ""

        hdr_transfers = {
            "COLOR_TRANSFER_CHARACTERISTICS_SMPTEST2084",
            "COLOR_TRANSFER_CHARACTERISTICS_ARIB_STD_B67",
        }
        is_hdr = any(
            "HDR" in str(item.get("qualityLabel") or "").upper()
            or str(item.get("transferCharacteristics") or "").upper()
            in hdr_transfers
            for item in formats
        )
        dynamic_range = "hdr" if is_hdr else "sdr"

    license_label = None
    location_name = None
    if page_observed:
        license_label = ""
        row_container = _first_youtube_renderer(
            initial_data,
            "metadataRowContainerRenderer",
        )
        for row in row_container.get("rows", []):
            renderer = row.get("metadataRowRenderer", {}) if isinstance(row, dict) else {}
            label = text_from_runs(renderer.get("title") or {}).strip().casefold()
            if label != "license":
                continue
            values = [
                text_from_runs(value).strip()
                for value in renderer.get("contents", [])
                if isinstance(value, dict) and text_from_runs(value).strip()
            ]
            license_label = ", ".join(values)
            break

        location_name = ""
        primary_info = _first_youtube_renderer(initial_data, "videoPrimaryInfoRenderer")
        super_title = primary_info.get("superTitleLink", {})
        accessibility_labels = [
            str(node.get("label") or "").strip()
            for node in walk(super_title)
            if isinstance(node, dict)
            and node.get("label")
            and (
                "accessibility" in node
                or set(node).issubset({"label"})
            )
        ]
        location_accessibility = " ".join(accessibility_labels).casefold()
        if "location restricted" in location_accessibility or "geo tagged" in location_accessibility:
            location_name = text_from_runs(super_title).strip()

    return {
        "max_video_height": max_video_height,
        "spatial_format": spatial_format,
        "stereo_layout": stereo_layout,
        "dynamic_range": dynamic_range,
        "license": license_label,
        "location_name": location_name,
    }


def youtube_content_check_metadata(
    playability: Mapping[str, Any],
) -> dict[str, bool | str | None]:
    status = str(playability.get("status") or "").strip().upper()
    if not status:
        return {
            "content_check_required": None,
            "content_check_reason": None,
        }
    required = status == "CONTENT_CHECK_REQUIRED"
    reason = text_from_runs(playability.get("reason")).strip() if required else ""
    if required and not reason:
        error_renderer = _first_youtube_renderer(
            playability,
            "playerErrorMessageRenderer",
        )
        reason = text_from_runs(error_renderer.get("reason") or {}).strip()
    return {
        "content_check_required": required,
        "content_check_reason": reason,
    }


def youtube_ai_disclosure_metadata(
    player: Mapping[str, Any],
    initial_data: Mapping[str, Any],
) -> dict[str, bool | str | None]:
    playability = player.get("playabilityStatus", {})
    details = player.get("videoDetails", {})
    page_observed = (
        str(playability.get("status") or "").strip().upper() == "OK"
        and bool(details)
    )
    if not page_observed:
        return {
            "ai_disclosure": None,
            "ai_disclosure_text": None,
        }
    for node in walk(initial_data):
        disclosure = (
            node.get("howThisWasMadeSectionViewModel")
            if isinstance(node, dict)
            else None
        )
        if not isinstance(disclosure, dict):
            continue
        header_text = text_from_runs(disclosure.get("bodyHeader") or {}).strip()
        body_text = text_from_runs(disclosure.get("bodyText") or {}).strip()
        normalized_header = re.sub(r"\s+", " ", header_text).strip().casefold()
        normalized_body = re.sub(r"\s+", " ", body_text).strip().casefold()
        if normalized_header == "made with ai" or (
            "sounds or visuals were altered or fully generated" in normalized_body
        ):
            return {
                "ai_disclosure": True,
                "ai_disclosure_text": body_text,
            }
    return {
        "ai_disclosure": False,
        "ai_disclosure_text": "",
    }


def youtube_auto_dubbed_metadata(
    player: Mapping[str, Any],
    initial_data: Mapping[str, Any],
) -> dict[str, bool | None]:
    if (
        str(player.get("playabilityStatus", {}).get("status") or "").upper() != "OK"
        or not player.get("videoDetails")
    ):
        return {"auto_dubbed": None}
    for node in walk(player.get("streamingData", {})):
        if isinstance(node, dict):
            track = node.get("audioTrack")
            if isinstance(track, dict) and track.get("isAutoDubbed") is True:
                return {"auto_dubbed": True}
    for node in walk(initial_data):
        if not isinstance(node, dict):
            continue
        disclosure = node.get("howThisWasMadeSectionViewModel")
        if isinstance(disclosure, dict):
            header = text_from_runs(disclosure.get("bodyHeader") or {}).strip().casefold()
            body = text_from_runs(disclosure.get("bodyText") or {}).casefold()
            if header == "auto-dubbed" or (
                "audio tracks for some languages were automatically generated" in body
            ):
                return {"auto_dubbed": True}
        primary = node.get("videoPrimaryInfoRenderer")
        if isinstance(primary, dict):
            for badge in primary.get("badges", []) or []:
                label = text_from_runs(
                    badge.get("metadataBadgeRenderer", {}).get("label")
                ).strip().casefold()
                if label == "auto-dubbed":
                    return {"auto_dubbed": True}
    return {"auto_dubbed": False if initial_data else None}


def opener_cookie_jar(
    opener: urllib.request.OpenerDirector,
) -> http.cookiejar.CookieJar | None:
    for handler in getattr(opener, "handlers", ()):
        jar = getattr(handler, "cookiejar", None)
        if isinstance(jar, http.cookiejar.CookieJar):
            return jar
    return None


def resolve_content_checked_player_response(
    opener: urllib.request.OpenerDirector,
    html_text: str,
    video_id: str,
) -> dict[str, Any] | None:
    initial_player = extract_json_assignment(html_text, "ytInitialPlayerResponse")
    playability = initial_player.get("playabilityStatus", {})
    if str(playability.get("status") or "").strip().upper() != "CONTENT_CHECK_REQUIRED":
        return None
    config = extract_ytcfg(html_text)
    api_key = str(config.get("INNERTUBE_API_KEY") or "").strip()
    client_version = str(config.get("INNERTUBE_CLIENT_VERSION") or "").strip()
    jar = opener_cookie_jar(opener)
    if not api_key or not client_version or jar is None:
        return None
    watch_url = f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}"
    return request_youtubei_json(
        opener,
        jar,
        api_key,
        {
            "context": youtube_web_context(config),
            "videoId": video_id,
            "racyCheckOk": True,
            "contentCheckOk": True,
        },
        watch_url,
        client_version,
        api_path="player",
    )


def extract_watch_metadata(
    html_text: str,
    video_id: str,
    *,
    player_response: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    initial_player = extract_json_assignment(html_text, "ytInitialPlayerResponse")
    initial_playability = initial_player.get("playabilityStatus", {})
    player = dict(player_response) if player_response is not None else initial_player
    initial_data = extract_json_assignment(html_text, "ytInitialData")
    details = player.get("videoDetails", {}) if isinstance(player, dict) else {}
    playability = player.get("playabilityStatus", {}) if isinstance(player, dict) else {}
    microformat = (
        player.get("microformat", {}).get("playerMicroformatRenderer", {})
        if isinstance(player, dict)
        else {}
    )
    title = str(details.get("title") or "").strip()
    if not title:
        found = re.search(r"<title>(.*?)</title>", html_text, flags=re.DOTALL)
        if found:
            title = html.unescape(re.sub(r"\s+", " ", found.group(1))).strip()
            title = re.sub(r"\s+-\s+YouTube$", "", title)
    thumbnails = []
    if isinstance(details.get("thumbnail"), dict):
        thumbnails = details.get("thumbnail", {}).get("thumbnails", []) or []
    thumbnail_url = pick_thumbnail(thumbnails)
    if not thumbnail_url:
        thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    channel_thumbnail_url = extract_channel_thumbnail_url(initial_data)
    channel_url = youtube_path_url(str(microformat.get("ownerProfileUrl") or "")) or extract_channel_url(initial_data)
    channel_id = extract_channel_id(initial_data, channel_url)
    reaction = extract_reaction_from_initial_data(initial_data)
    status = str(playability.get("status") or "").strip()
    reason = text_from_runs(playability.get("reason")).strip()
    availability = watch_primary_info_availability(initial_data) or watch_playability_availability(
        playability,
        microformat,
        details,
    )
    playability_status = status
    if reason and status and reason not in status:
        status = f"{status}: {reason}"
    channel = str(details.get("author") or "").strip()
    if status.upper().startswith("ERROR") and title in {"", "YouTube", "- YouTube"}:
        channel_id = ""
        channel = ""
        channel_url = ""
        channel_thumbnail_url = ""
    movie_metadata = youtube_movie_metadata(details, initial_data)
    feature_metadata = youtube_video_feature_metadata(player, initial_data)
    broadcast_metadata = youtube_broadcast_metadata(details, microformat)
    content_check_metadata = youtube_content_check_metadata(initial_playability)
    ai_disclosure_metadata = youtube_ai_disclosure_metadata(player, initial_data)
    return {
        "video_id": video_id,
        "title": title,
        "description": str(details.get("shortDescription") or "").strip(),
        "channel_id": channel_id,
        "channel": channel,
        "channel_url": channel_url,
        "duration_text": format_duration(details.get("lengthSeconds")),
        "view_count": str(details.get("viewCount") or ""),
        "upload_date": str(microformat.get("uploadDate") or microformat.get("publishDate") or ""),
        "uploader_category": str(microformat.get("category") or "").strip(),
        "video_type": youtube_video_type(details, microformat),
        **broadcast_metadata,
        **movie_metadata,
        **feature_metadata,
        **content_check_metadata,
        **ai_disclosure_metadata,
        **youtube_auto_dubbed_metadata(player, initial_data),
        "thumbnail_url": thumbnail_url,
        "channel_thumbnail_url": channel_thumbnail_url,
        "reaction": reaction,
        "availability": availability,
        "playability_status": playability_status,
        "yt_status": status or ("OK" if title else ""),
    }


def fetch_watch_metadata(
    opener: urllib.request.OpenerDirector,
    video_id: str,
    thumb_dir: Path,
    require_authenticated: bool = False,
) -> dict[str, Any]:
    watch_url = f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}"
    page = request_text(opener, watch_url)
    if require_authenticated and not youtube_page_is_authenticated(page):
        raise youtube_authentication_error(page, "watch page")
    resolved_player = resolve_content_checked_player_response(opener, page, video_id)
    metadata = extract_watch_metadata(
        page,
        video_id,
        player_response=resolved_player,
    )
    metadata["thumbnail_path"] = cache_video_thumbnail(
        opener,
        video_id,
        metadata["thumbnail_url"],
        thumb_dir,
    )
    metadata["channel_thumbnail_path"] = cache_channel_thumbnail(
        opener,
        video_id,
        metadata.get("channel_thumbnail_url", ""),
        thumb_dir,
    )
    return metadata


def extract_channel_handle(value: str) -> str:
    value = html.unescape((value or "").strip())
    parsed = urllib.parse.urlparse(value)
    path = parsed.path if parsed.scheme or parsed.netloc else value
    found = re.search(r"(?:^|/)@([A-Za-z0-9._-]+)(?:$|[/?#])", path)
    return f"@{found.group(1)}" if found else ""


def resolve_channel_id(
    opener: urllib.request.OpenerDirector,
    value: str,
) -> str:
    value = html.unescape((value or "").strip())
    channel_id = youtube_channel_id_from_url(value)
    if channel_id:
        return channel_id
    if re.fullmatch(r"UC[A-Za-z0-9_-]{20,}", value):
        return value
    handle = extract_channel_handle(value)
    if not handle:
        return ""
    url = f"https://www.youtube.com/{handle}"
    page = request_text(opener, url)
    metadata = extract_channel_page_metadata(page, "")
    return metadata.get("channel_id", "")


def resolve_metadata_target(
    opener: urllib.request.OpenerDirector,
    value: str,
) -> tuple[str, str]:
    value = html.unescape((value or "").strip())
    if not value:
        return "", ""
    channel_id = resolve_channel_id(opener, value)
    if channel_id:
        return "channel", channel_id
    video_id = extract_video_id(value)
    if not video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        video_id = value
    if video_id:
        return "video", video_id
    return "", ""


def store_video_metadata(
    conn: sqlite3.Connection,
    metadata: dict[str, Any],
    status: str,
    error: str = "",
    updated_at: str | None = None,
) -> str:
    now = updated_at or utc_now()
    channel_id = upsert_channel(
        conn,
        metadata.get("channel_id", ""),
        title=metadata.get("channel", ""),
        url=metadata.get("channel_url", ""),
        description=metadata.get("channel_description", ""),
        aliases=metadata.get("channel_aliases", ""),
        thumbnail_url=metadata.get("channel_thumbnail_url", ""),
        thumbnail_path=metadata.get("channel_thumbnail_path", ""),
        archivarix_channel_id=metadata.get("archivarix_channel_id", ""),
        status=metadata.get("channel_status", ""),
        status_reason=metadata.get("channel_status_reason", ""),
        source="metadata",
        updated_at=now,
    )
    playability = storable_watch_playability_value(metadata)
    status_availability = video_availability_from_youtube_status(metadata.get("yt_status", ""))
    availability = (
        normalize_video_availability(
            metadata.get("video_id", ""),
            metadata.get("availability", ""),
            playability,
            metadata.get("yt_status", ""),
        )
        if playability is not None or (metadata.get("availability") or "").strip() or status_availability
        else ""
    )
    upsert_video(
        conn,
        metadata.get("video_id", ""),
        title=metadata.get("title", ""),
        description=metadata.get("description", ""),
        channel_id=channel_id,
        duration_text=metadata.get("duration_text", ""),
        view_count=metadata.get("view_count", ""),
        upload_date=metadata.get("upload_date", ""),
        uploader_category=metadata.get("uploader_category", ""),
        video_type=metadata.get("video_type", ""),
        broadcast_status=metadata.get("broadcast_status"),
        broadcast_started_at=metadata.get("broadcast_started_at"),
        broadcast_ended_at=metadata.get("broadcast_ended_at"),
        broadcast_status_checked_at=(
            now if metadata.get("broadcast_status") is not None else None
        ),
        movie_rating=metadata.get("movie_rating", ""),
        movie_release_date=metadata.get("movie_release_date", ""),
        movie_offer=metadata.get("movie_offer", ""),
        max_video_height=metadata.get("max_video_height"),
        spatial_format=metadata.get("spatial_format"),
        stereo_layout=metadata.get("stereo_layout"),
        dynamic_range=metadata.get("dynamic_range"),
        license=metadata.get("license"),
        location_name=metadata.get("location_name"),
        content_check_required=metadata.get("content_check_required"),
        content_check_reason=metadata.get("content_check_reason"),
        ai_disclosure=metadata.get("ai_disclosure"),
        ai_disclosure_text=metadata.get("ai_disclosure_text"),
        auto_dubbed=metadata.get("auto_dubbed"),
        thumbnail_url=metadata.get("thumbnail_url", ""),
        thumbnail_path=metadata.get("thumbnail_path", ""),
        reaction=metadata.get("reaction", ""),
        is_playable=playability,
        availability=availability,
        source="metadata",
        fetch_status=status,
        fetch_error=error,
        fetched_at=now,
        checked_at=now,
        updated_at=now,
    )
    if status == "ok":
        conn.execute(
            """
            UPDATE videos
            SET visibility_checked_at = ?
            WHERE video_id = ?
            """,
            (now, metadata.get("video_id", "")),
        )
    return channel_id


def useful_video_metadata(metadata: dict[str, str]) -> bool:
    title = video_title_or_blank(
        metadata.get("title"),
        metadata.get("video_id", ""),
    )
    yt_status = (metadata.get("yt_status") or "").strip().upper()
    has_recovered_fields = bool(
        metadata.get("channel_id")
        or metadata.get("channel")
        or metadata.get("thumbnail_path")
    )
    if title in {"", "YouTube", "- YouTube"}:
        return has_recovered_fields
    if yt_status.startswith("ERROR") and not has_recovered_fields:
        return False
    return True


def metadata_from_archivarix_video(video_id: str, video: dict[str, Any], thumbnail_url: str, thumbnail_path: str) -> dict[str, str]:
    return {
        "video_id": video_id,
        "title": str(video.get("title") or "").strip(),
        "description": str(video.get("description") or "").strip(),
        "channel_id": str(video.get("channelExternalId") or "").strip(),
        "channel": str(video.get("channelTitle") or "").strip(),
        "channel_url": str(video.get("channelUrl") or "").strip(),
        "channel_description": str(video.get("channelDescription") or "").strip(),
        "channel_aliases": str(video.get("channelAliases") or "").strip(),
        "channel_thumbnail_url": str(video.get("channelThumbnailUrl") or "").strip(),
        "channel_thumbnail_path": str(video.get("channelThumbnailPath") or "").strip(),
        "archivarix_channel_id": str(video.get("channelId") or "").strip(),
        "channel_status": str(video.get("channelStatus") or "").strip(),
        "channel_status_reason": str(video.get("channelStatusReason") or "").strip(),
        "duration_text": format_duration(video.get("duration")),
        "view_count": str(video.get("viewCount") or ""),
        "upload_date": str(video.get("uploadDate") or ""),
        "thumbnail_url": thumbnail_url or str(video.get("thumbnailArchiveUrl") or video.get("thumbnailUrl") or ""),
        "thumbnail_path": thumbnail_path,
        "reaction": "",
        "yt_status": str(video.get("status") or ""),
    }


def enrich_archivarix_video_channel(
    video: dict[str, Any],
    channel_id: str,
    archivarix_opener: urllib.request.OpenerDirector,
) -> None:
    if not channel_id:
        return
    try:
        fields = archivarix_lookup_channel(channel_id, archivarix_opener)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        fields = {}
    if not fields:
        return
    channel_metadata = archivarix_channel_metadata(fields, channel_id)
    if channel_metadata.get("channel"):
        video["channelTitle"] = channel_metadata["channel"]
    if channel_metadata.get("channel_id"):
        video["channelExternalId"] = channel_metadata["channel_id"]
    if channel_metadata.get("channel_url"):
        video["channelUrl"] = channel_metadata["channel_url"]
    if channel_metadata.get("channel_description"):
        video["channelDescription"] = channel_metadata["channel_description"]
    if channel_metadata.get("channel_aliases"):
        video["channelAliases"] = channel_metadata["channel_aliases"]
    if channel_metadata.get("channel_thumbnail_url"):
        video["channelThumbnailUrl"] = channel_metadata["channel_thumbnail_url"]
    if channel_metadata.get("archivarix_channel_id"):
        video["channelId"] = channel_metadata["archivarix_channel_id"]
    if channel_metadata.get("channel_status"):
        video["channelStatus"] = channel_metadata["channel_status"]
    if channel_metadata.get("channel_status_reason"):
        video["channelStatusReason"] = channel_metadata["channel_status_reason"]


def merge_channel_reference_row(
    conn: sqlite3.Connection,
    channel_reference: str,
    canonical_channel_id: str,
) -> bool:
    channel_reference = (channel_reference or "").strip()
    canonical_channel_id = (canonical_channel_id or "").strip()
    if (
        not channel_reference
        or channel_reference == canonical_channel_id
        or is_canonical_youtube_channel_id(channel_reference)
        or not is_canonical_youtube_channel_id(canonical_channel_id)
    ):
        return False
    stale = conn.execute(
        "SELECT 1 FROM channels WHERE channel_id = ?",
        (channel_reference,),
    ).fetchone()
    canonical = conn.execute(
        "SELECT 1 FROM channels WHERE channel_id = ?",
        (canonical_channel_id,),
    ).fetchone()
    if stale is None or canonical is None:
        return False

    reference_columns = (
        ("videos", "channel_id"),
        ("playlists", "owner_channel_id"),
        ("clips", "owner_channel_id"),
        ("playlist_collaborators", "channel_id"),
        ("my_activity_subscription_events", "channel_id"),
        ("channel_featured_channels", "owner_channel_id"),
        ("channel_featured_channels", "featured_channel_id"),
    )
    for table, column in reference_columns:
        conn.execute(
            f'UPDATE OR IGNORE "{table}" SET "{column}" = ? WHERE "{column}" = ?',
            (canonical_channel_id, channel_reference),
        )
        conn.execute(
            f'DELETE FROM "{table}" WHERE "{column}" = ?',
            (channel_reference,),
        )
    conn.execute(
        "DELETE FROM channels WHERE channel_id = ?",
        (channel_reference,),
    )
    return True


def store_channel_metadata(
    conn: sqlite3.Connection,
    metadata: dict[str, Any],
    status: str,
    error: str = "",
    updated_at: str | None = None,
) -> str:
    now = updated_at or utc_now()
    channel_id = upsert_channel(
        conn,
        metadata.get("channel_id", ""),
        title=metadata.get("channel", ""),
        url=metadata.get("channel_url", ""),
        description=metadata.get("channel_description", ""),
        aliases=metadata.get("channel_aliases", ""),
        thumbnail_url=metadata.get("channel_thumbnail_url", ""),
        thumbnail_path=metadata.get("channel_thumbnail_path", ""),
        archivarix_channel_id=metadata.get("archivarix_channel_id", ""),
        status=metadata.get("channel_status", ""),
        status_reason=metadata.get("channel_status_reason", ""),
        replace_aliases=status == "ok" and bool(metadata.get("channel_aliases_observed")),
        replace_status=status == "ok" and bool(metadata.get("channel_status_observed")),
        fetch_status=status,
        fetch_error=error,
        fetched_at=now,
        source="metadata",
        updated_at=now,
    )
    requested_channel_reference = str(
        metadata.get("requested_channel_reference") or ""
    ).strip()
    if (
        channel_id
        and requested_channel_reference
        and requested_channel_reference != channel_id
        and not is_canonical_youtube_channel_id(requested_channel_reference)
    ):
        merge_channel_reference_row(
            conn,
            requested_channel_reference,
            channel_id,
        )
    subscribed = str(metadata.get("channel_subscribed") or "").strip()
    notification_level = normalize_channel_notification_level(
        metadata.get("channel_notification_level", "")
    )
    if channel_id and status == "ok" and subscribed == "0":
        conn.execute(
            """
            UPDATE channels
            SET subscribed = 0,
                notification_level = '',
                subscription_checked_at = ?,
                notification_checked_at = ?
            WHERE channel_id = ?
            """,
            (now, now, channel_id),
        )
    elif channel_id and status == "ok" and subscribed == "1":
        conn.execute(
            """
            UPDATE channels
            SET subscribed = 1,
                subscription_checked_at = ?,
                notification_level = CASE
                  WHEN ? <> '' THEN ?
                  ELSE notification_level
                END,
                notification_checked_at = CASE
                  WHEN ? <> '' THEN ?
                  ELSE notification_checked_at
                END
            WHERE channel_id = ?
            """,
            (
                now,
                notification_level,
                notification_level,
                notification_level,
                now,
                channel_id,
            ),
        )
    if (
        channel_id
        and status == "ok"
        and bool(metadata.get("channel_featured_channels_observed"))
    ):
        conn.execute(
            "DELETE FROM channel_featured_channels WHERE owner_channel_id = ?",
            (channel_id,),
        )
        seen_featured_ids: set[str] = set()
        for position, featured in enumerate(
            metadata.get("channel_featured_channels") or []
        ):
            if not isinstance(featured, dict):
                continue
            featured_channel_id = str(featured.get("channel_id") or "").strip()
            if (
                not featured_channel_id.startswith("UC")
                or featured_channel_id == channel_id
                or featured_channel_id in seen_featured_ids
            ):
                continue
            seen_featured_ids.add(featured_channel_id)
            conn.execute(
                """
                INSERT INTO channel_featured_channels(
                  owner_channel_id, featured_channel_id, title,
                  channel_reference, position
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    channel_id,
                    featured_channel_id,
                    str(featured.get("title") or "").strip(),
                    str(featured.get("channel_reference") or "").strip(),
                    position,
                ),
            )
    return channel_id


def video_metadata_channel_id(metadata: dict[str, str]) -> str:
    return (metadata.get("channel_id") or youtube_channel_id_from_url(metadata.get("channel_url", ""))).strip()


def fetch_new_channel_metadata_if_needed(
    conn: sqlite3.Connection,
    opener: urllib.request.OpenerDirector,
    thumb_dir: Path,
    video_metadata: dict[str, str],
    require_authenticated: bool = False,
    proxy_url: str = "",
) -> tuple[dict[str, str], str, str]:
    channel_id = video_metadata_channel_id(video_metadata)
    if not channel_id:
        return {}, "", ""
    if conn.execute("SELECT 1 FROM channels WHERE channel_id = ?", (channel_id,)).fetchone():
        return {}, "", ""
    try:
        metadata = fetch_channel_metadata(
            opener,
            channel_id,
            thumb_dir,
            require_authenticated=require_authenticated,
            proxy_url=proxy_url,
        )
        status = (
            "ok"
            if metadata.get("channel")
            or metadata.get("channel_url")
            or metadata.get("channel_thumbnail_path")
            else "no_metadata"
        )
        return metadata, status, ""
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return (
            {
                "channel_id": channel_id,
                "channel": video_metadata.get("channel", ""),
                "channel_url": video_metadata.get("channel_url", ""),
                "channel_thumbnail_url": video_metadata.get("channel_thumbnail_url", ""),
                "channel_thumbnail_path": video_metadata.get("channel_thumbnail_path", ""),
            },
            "error",
            str(exc),
        )


def playlist_metadata_from_ytdlp_info(info: dict[str, Any], playlist_id: str) -> dict[str, Any]:
    title = str(info.get("title") or info.get("playlist_title") or "").strip()
    description = str(info.get("description") or "").strip()
    owner, inferred_visibility = split_playlist_owner_visibility(
        str(info.get("uploader") or info.get("channel") or "")
    )
    visibility = normalize_playlist_visibility(
        str(info.get("visibility") or info.get("privacy") or info.get("availability") or "")
    ) or inferred_visibility
    playlist_count = info.get("playlist_count") or info.get("n_entries")
    video_count = int(playlist_count or 0)
    thumbnail_url = str(info.get("thumbnail") or "").strip()
    webpage_url = str(info.get("webpage_url") or info.get("original_url") or "").strip()
    if not webpage_url:
        webpage_url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
    owner_channel_id = (
        str(info.get("channel_id") or info.get("uploader_id") or "").strip()
        or youtube_channel_ref_from_url(str(info.get("channel_url") or info.get("uploader_url") or ""))
    )
    return {
        "playlist_id": playlist_id,
        "title": title or playlist_id,
        "description": description,
        "owner": owner,
        "owner_channel_id": owner_channel_id,
        "owner_thumbnail_url": "",
        "visibility": visibility,
        "video_count": video_count,
        "thumbnail_url": thumbnail_url,
        "url": webpage_url,
    }


def scan_playlist_ytdlp(
    playlist_id: str,
    cookie_file: Path,
    proxy_url: str = "",
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed") from exc

    url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
    messages: list[str] = []

    class YtdlpLogger:
        def debug(self, message: str) -> None:
            return

        def warning(self, message: str) -> None:
            messages.append(message)

        def error(self, message: str) -> None:
            messages.append(message)

    try:
        with temporary_ytdlp_cookie_file(cookie_file) as working_cookie_file:
            options = {
                "cookiefile": str(working_cookie_file) if working_cookie_file else None,
                "extract_flat": "in_playlist",
                "ignoreerrors": True,
                "logger": YtdlpLogger(),
                "no_warnings": True,
                "quiet": True,
                "skip_download": True,
                **ytdlp_proxy_options(proxy_url),
            }
            with request_paced_youtube_dl(yt_dlp, options) as ydl:
                info = ydl.extract_info(url, download=False)
    except Exception as exc:
        proxy_error = proxy_unavailable_error(exc, proxy_url)
        if proxy_error:
            raise proxy_error from exc
        raise
    if not info:
        text = "\n".join(messages).strip()
        raise RuntimeError(text or "yt-dlp returned no playlist data")
    metadata = playlist_metadata_from_ytdlp_info(info, playlist_id)
    entries = [entry for entry in info.get("entries") or [] if isinstance(entry, dict)]
    videos: list[dict[str, Any]] = []
    for position, entry in enumerate(entries, start=1):
        video_id = str(entry.get("id") or entry.get("url") or "")
        if not video_id:
            continue
        title = video_title_or_blank(entry.get("title"), video_id)
        channel = str(entry.get("channel") or entry.get("uploader") or "").strip()
        channel_id = str(entry.get("channel_id") or entry.get("uploader_id") or "").strip()
        channel_url = str(entry.get("channel_url") or entry.get("uploader_url") or "").strip()
        if not channel_id:
            channel_id = youtube_channel_id_from_url(channel_url)
        duration = format_duration(entry.get("duration"))
        webpage_url = str(entry.get("webpage_url") or "")
        if not webpage_url:
            webpage_url = f"https://www.youtube.com/watch?v={video_id}&list={playlist_id}"
        reported_availability = str(entry.get("availability") or "").strip()
        playability = playlist_video_playability(
            entry,
            title,
            reported_availability,
        )
        if playability == 0:
            availability = normalize_video_availability(
                video_id,
                reported_availability or "unavailable",
                playability,
            )
        else:
            availability = normalize_video_availability(
                video_id,
                reported_availability or "public",
                None,
            )
        videos.append(
            {
                "playlist_id": playlist_id,
                "position": position,
                "video_id": video_id,
                "title": title,
                "channel_id": channel_id,
                "channel": channel,
                "duration_text": duration,
                "is_playable": playability,
                "availability": availability,
                "url": webpage_url,
            }
        )
    return videos, metadata


def scan_playlist_videos(
    opener: urllib.request.OpenerDirector,
    playlist_id: str,
    cookie_file: Path | None = None,
) -> list[dict[str, Any]]:
    playlist_url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
    page = request_text(opener, playlist_url)
    initial_data = extract_json_assignment(page, "ytInitialData")
    config = extract_ytcfg(page)
    pages = [initial_data]
    token = playlist_continuation_token(initial_data)

    api_key = config.get("INNERTUBE_API_KEY", "")
    client_name = config.get("INNERTUBE_CLIENT_NAME", "WEB")
    client_version = config.get("INNERTUBE_CLIENT_VERSION", "")
    visitor_data = config.get("VISITOR_DATA", "")
    cookie_jar = load_cookie_jar(cookie_file) if cookie_file else None
    seen_tokens: set[str] = set()
    while token and token not in seen_tokens and api_key and client_version:
        seen_tokens.add(token)
        payload = {
            "context": {
                "client": {
                    "clientName": client_name,
                    "clientVersion": client_version,
                    "visitorData": urllib.parse.unquote(visitor_data),
                }
            },
            "continuation": token,
        }
        if cookie_jar:
            data = request_youtubei_json(
                opener,
                cookie_jar,
                api_key,
                payload,
                playlist_url,
                client_version,
            )
        else:
            data = request_json(
                opener,
                f"https://www.youtube.com/youtubei/v1/browse?key={urllib.parse.quote(api_key)}",
                payload,
                playlist_url,
            )
        pages.append(data)
        token = playlist_continuation_token(data)

    videos: list[dict[str, Any]] = []
    seen_positions: set[int] = set()
    seen_entries: set[tuple[int, str]] = set()

    def add_video(video: dict[str, Any]) -> None:
        append_playlist_occurrence(videos, seen_positions, seen_entries, video)

    hidden_alerts = 0
    for page_data in pages:
        hidden_alerts += hidden_alert_count(page_data)
        for renderer in playlist_video_renderers(page_data):
            fallback = len(videos) + 1
            video = parse_video_renderer(playlist_id, renderer, fallback)
            add_video(video)
        for lockup in video_lockup_renderers(page_data):
            fallback = len(videos) + 1
            add_video(parse_video_lockup(playlist_id, lockup, fallback))
        for renderer in shorts_lockup_renderers(page_data):
            fallback = len(videos) + 1
            add_video(parse_shorts_lockup(playlist_id, renderer, fallback))

    first_video_id = next((video["video_id"] for video in videos if video.get("video_id")), "")
    if first_video_id:
        try:
            watch_url = (
                f"https://www.youtube.com/watch?v={urllib.parse.quote(first_video_id)}"
                f"&list={urllib.parse.quote(playlist_id)}"
            )
            watch_page = request_text(opener, watch_url)
            watch_data = extract_json_assignment(watch_page, "ytInitialData")
            panel_videos: list[dict[str, Any]] = []
            panel_positions: set[int] = set()
            panel_entries: set[tuple[int, str]] = set()
            for renderer in playlist_panel_video_renderers(watch_data):
                video = parse_panel_video_renderer(
                    playlist_id,
                    renderer,
                    len(panel_videos) + 1,
                )
                append_playlist_occurrence(
                    panel_videos,
                    panel_positions,
                    panel_entries,
                    video,
                )
            if len(panel_videos) > len(videos):
                videos = panel_videos
                seen_positions = panel_positions
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            pass

    explicit_hidden = sum(1 for video in videos if not video["is_playable"])
    for offset in range(max(0, hidden_alerts - explicit_hidden)):
        position = max(seen_positions, default=0) + 1
        seen_positions.add(position)
        videos.append(
            {
                "playlist_id": playlist_id,
                "position": position,
                "video_id": "",
                "title": "Unavailable video hidden by YouTube",
                "channel": "",
                "duration_text": "",
                "is_playable": 0,
                "availability": "Unavailable video is hidden",
                "url": "",
            }
        )
    return sorted(videos, key=lambda item: item["position"])


def import_playlists(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    thumb_dir = Path(args.thumbs)
    proxy_url = configured_proxy(getattr(args, "config_data", {}))
    timezone_name = effective_display_timezone(getattr(args, "config_data", {}))
    _, groups, memberships = load_pockettube(Path(args.pockettube))
    all_playlist_ids = []
    seen: set[str] = set()
    for playlist_ids in memberships.values():
        for playlist_id in playlist_ids:
            if playlist_id not in seen:
                all_playlist_ids.append(playlist_id)
                seen.add(playlist_id)

    opener = load_cookie_opener(Path(args.cookies), proxy_url)
    conn = connect(db_path)
    with conn:
        conn.execute("DELETE FROM group_playlists")
        conn.execute("DELETE FROM groups")
        for node in groups:
            conn.execute(
                """
                INSERT INTO groups(group_key, name, parent_key, position, icon)
                VALUES (?, ?, ?, ?, ?)
                """,
                (node.key, node.name, node.parent_key, node.position, node.icon),
            )

    print(f"Importing {len(all_playlist_ids)} playlists from {len(groups)} PocketTube groups...")
    for index, playlist_id in enumerate(all_playlist_ids, start=1):
        url = f"https://www.youtube.com/playlist?list={urllib.parse.quote(playlist_id)}"
        status = "ok"
        error = ""
        try:
            page = request_text(opener, url)
            metadata = extract_playlist_metadata(
                page,
                playlist_id,
                timezone_name=timezone_name,
            )
            thumbnail_path = cache_thumbnail(
                opener, playlist_id, metadata["thumbnail_url"], thumb_dir
            )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            metadata = {
                "playlist_id": playlist_id,
                "title": playlist_id,
                "description": "",
                "owner": "",
                "owner_channel_id": "",
                "owner_thumbnail_url": "",
                "visibility": "",
                "video_count": 0,
                "thumbnail_url": "",
                "url": url,
            }
            thumbnail_path = ""
            status = "error"
            error = str(exc)
        with conn:
            owner_channel_id = metadata.get("owner_channel_id", "")
            owner_thumbnail_path = ""
            if owner_channel_id and metadata.get("owner_thumbnail_url"):
                owner_thumbnail_path = cache_channel_thumbnail(
                    opener,
                    owner_channel_id,
                    metadata.get("owner_thumbnail_url", ""),
                    DEFAULT_VIDEO_THUMB_DIR,
                    referer_url=url,
                )
            if owner_channel_id:
                owner_channel_id = upsert_channel(
                    conn,
                    owner_channel_id,
                    title=metadata.get("owner", ""),
                    thumbnail_url=metadata.get("owner_thumbnail_url", ""),
                    thumbnail_path=owner_thumbnail_path,
                    source="playlist_owner",
                    updated_at=utc_now(),
                )
            existing_playlist = conn.execute(
                "SELECT youtube_updated_date FROM playlists WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()
            metadata["youtube_updated_date"] = merge_youtube_playlist_updated_date(
                (
                    existing_playlist["youtube_updated_date"]
                    if existing_playlist
                    else ""
                ),
                metadata.get("youtube_updated_date"),
                str(metadata.get("youtube_updated_text") or ""),
            )
            conn.execute(
                """
                INSERT INTO playlists(
                  playlist_id, title, description, owner_channel_id, visibility,
                  youtube_updated_date, video_count, thumbnail_url, thumbnail_path,
                  fetch_status, fetch_error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                  title=excluded.title,
                  description=excluded.description,
                  youtube_updated_date=COALESCE(
                    NULLIF(excluded.youtube_updated_date, ''),
                    playlists.youtube_updated_date
                  ),
                  owner_channel_id=COALESCE(excluded.owner_channel_id, playlists.owner_channel_id),
                  visibility=COALESCE(NULLIF(excluded.visibility, ''), playlists.visibility),
                  video_count=excluded.video_count,
                  thumbnail_url=excluded.thumbnail_url,
                  thumbnail_path=excluded.thumbnail_path,
                  fetch_status=excluded.fetch_status,
                  fetch_error=excluded.fetch_error,
                  updated_at=excluded.updated_at
                """,
                (
                    playlist_id,
                    metadata["title"],
                    metadata["description"],
                    owner_channel_id or None,
                    metadata["visibility"],
                    metadata.get("youtube_updated_date") or None,
                    metadata["video_count"],
                    metadata["thumbnail_url"],
                    thumbnail_path,
                    status,
                    error,
                    utc_now(),
                ),
            )
        print(f"[{index:03d}/{len(all_playlist_ids):03d}] {status} {metadata['title']}")

    with conn:
        for group_key, playlist_ids in memberships.items():
            for position, playlist_id in enumerate(playlist_ids):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO group_playlists(group_key, playlist_id, position)
                    VALUES (?, ?, ?)
                    """,
                    (group_key, playlist_id, position),
                )
    print(f"Wrote {db_path}")


def youtube_web_context(
    config: dict[str, Any],
    timezone_name: str = "",
) -> dict[str, Any]:
    context = {
        "client": {
            "clientName": config.get("INNERTUBE_CLIENT_NAME", "WEB"),
            "clientVersion": config.get("INNERTUBE_CLIENT_VERSION", ""),
            "visitorData": urllib.parse.unquote(config.get("VISITOR_DATA", "")),
        }
    }
    if timezone_name:
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            zone = timezone.utc
            timezone_name = "UTC"
        offset = datetime.now(zone).utcoffset() or timedelta(0)
        context["client"]["timeZone"] = timezone_name
        context["client"]["utcOffsetMinutes"] = int(offset.total_seconds() // 60)
    return context


def fetch_current_youtube_playlists(
    cookie_file: Path,
    browse_id: str = "FEplaylist_aggregation",
    proxy_url: str = "",
    timezone_name: str = DEFAULT_DISPLAY_TIMEZONE,
) -> tuple[urllib.request.OpenerDirector, list[dict[str, str]]]:
    observed_at = utc_now()
    jar = load_cookie_jar(cookie_file)
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        *socks5_proxy_handlers(proxy_url),
    )
    referer = "https://www.youtube.com/feed/playlists"
    page = request_text(opener, referer)
    if not youtube_page_is_authenticated(page):
        raise youtube_authentication_error(page, "playlists feed")
    config = extract_ytcfg(page)
    api_key = config.get("INNERTUBE_API_KEY", "")
    client_version = config.get("INNERTUBE_CLIENT_VERSION", "")
    if not api_key or not client_version:
        raise RuntimeError("Could not find YouTube web API configuration in the playlists page.")

    payload = {
        "context": youtube_web_context(config, timezone_name),
        "browseId": browse_id,
    }
    pages = [
        request_youtubei_json(opener, jar, api_key, payload, referer, client_version)
    ]
    token = continuation_token(pages[0])
    seen_tokens: set[str] = set()
    while token and token not in seen_tokens:
        seen_tokens.add(token)
        pages.append(
            request_youtubei_json(
                opener,
                jar,
                api_key,
                {
                    "context": youtube_web_context(config, timezone_name),
                    "continuation": token,
                },
                referer,
                client_version,
            )
        )
        token = continuation_token(pages[-1])

    records: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for page_data in pages:
        for node in walk(page_data):
            if not isinstance(node, dict):
                continue
            lockup = node.get("lockupViewModel")
            if not isinstance(lockup, dict):
                continue
            record = parse_playlist_lockup(
                lockup,
                observed_at=observed_at,
                timezone_name=timezone_name,
            )
            if not record:
                continue
            playlist_id = record["playlist_id"]
            if playlist_id in seen_ids:
                continue
            seen_ids.add(playlist_id)
            records.append(record)
    return opener, records


def _clip_config(value: Any) -> dict[str, Any]:
    for node in walk(value):
        if not isinstance(node, dict):
            continue
        config = node.get("clipConfig")
        if isinstance(config, dict) and config.get("postId"):
            return config
    return {}


def _channel_from_runs(value: Any) -> tuple[str, str, str]:
    runs = value.get("runs") if isinstance(value, dict) else None
    if not isinstance(runs, list):
        return text_from_runs(value).strip(), "", ""
    for run in runs:
        if not isinstance(run, dict):
            continue
        title = str(run.get("text") or "").strip()
        endpoint = run.get("navigationEndpoint")
        endpoint = endpoint if isinstance(endpoint, dict) else {}
        browse = endpoint.get("browseEndpoint")
        browse = browse if isinstance(browse, dict) else {}
        channel_id = str(browse.get("browseId") or "").strip()
        canonical = str(browse.get("canonicalBaseUrl") or "").strip()
        if title or channel_id or canonical:
            return title, channel_id, canonical
    return text_from_runs(value).strip(), "", ""


def _clip_view_count(value: str) -> int | None:
    text = (value or "").strip().lower()
    if text.startswith("no view"):
        return 0
    found = re.search(r"([\d,]+)\s+views?", text)
    return int(found.group(1).replace(",", "")) if found else None


def _clip_created_metadata(attribution: dict[str, Any]) -> tuple[str, str]:
    view_count_text = text_from_runs(
        attribution.get("viewCountText") or attribution.get("viewCount") or {}
    ).strip()
    clipped_at_text = text_from_runs(attribution.get("publishedTimeText") or {}).strip()
    created_text = text_from_runs(attribution.get("createdText") or {}).strip()
    if created_text:
        parts = [part.strip() for part in created_text.split("·") if part.strip()]
        if not view_count_text and parts and _clip_view_count(parts[0]) is not None:
            view_count_text = parts.pop(0)
        if not clipped_at_text and parts:
            clipped_at_text = " · ".join(parts)
        elif not clipped_at_text and not view_count_text:
            clipped_at_text = created_text
    if clipped_at_text and not clipped_at_text.casefold().startswith("clipped "):
        clipped_at_text = f"Clipped {clipped_at_text}"
    return view_count_text, clipped_at_text


def _first_youtube_renderer(value: Any, renderer_name: str) -> dict[str, Any]:
    for node in walk(value):
        candidate = node.get(renderer_name) if isinstance(node, dict) else None
        if isinstance(candidate, dict):
            return candidate
    return {}


def _clip_ownership(value: Any) -> str:
    labels = {
        text_from_runs(node).strip().lower()
        for node in walk(value)
        if isinstance(node, dict)
    }
    if "delete clip" in labels:
        return "mine"
    if "report" in labels:
        return "others"
    return "unknown"


def parse_clip_grid_renderer(renderer: dict[str, Any]) -> dict[str, Any] | None:
    config = _clip_config(renderer)
    clip_id = str(config.get("postId") or "").strip()
    source_video_id = str(renderer.get("videoId") or "").strip()
    if not clip_id or not source_video_id:
        return None
    source_channel, source_channel_id, source_channel_url = _channel_from_runs(
        renderer.get("shortBylineText") or renderer.get("longBylineText") or {}
    )
    view_count_text = text_from_runs(renderer.get("viewCountText") or {}).strip()
    clipped_at_text = text_from_runs(renderer.get("publishedTimeText") or {}).strip()
    thumbnail = renderer.get("thumbnail")
    thumbnails = thumbnail.get("thumbnails", []) if isinstance(thumbnail, dict) else []
    return {
        "clip_id": clip_id,
        "title": text_from_runs(renderer.get("title") or {}).strip(),
        "ownership": _clip_ownership(renderer),
        "source_video_id": source_video_id,
        "source_title": "",
        "source_channel": source_channel,
        "source_channel_id": source_channel_id,
        "source_channel_url": source_channel_url,
        "start_ms": int(config.get("startTimeMs") or 0),
        "end_ms": int(config.get("endTimeMs") or 0),
        "view_count": _clip_view_count(view_count_text),
        "view_count_text": view_count_text,
        "clipped_at": "",
        "clipped_at_text": clipped_at_text,
        "thumbnail_url": pick_thumbnail(thumbnails),
        "availability": "active",
        "fetch_status": "discovered",
        "fetch_error": "",
    }


def parse_clip_page(
    initial_data: dict[str, Any],
    player_response: dict[str, Any],
    clip_id: str,
) -> dict[str, Any]:
    attribution = _first_youtube_renderer(initial_data, "clipAttributionRenderer")
    primary_info = _first_youtube_renderer(initial_data, "videoPrimaryInfoRenderer")
    source_owner = _first_youtube_renderer(initial_data, "videoOwnerRenderer")
    config = _clip_config(player_response) or _clip_config(initial_data)
    details = player_response.get("videoDetails")
    details = details if isinstance(details, dict) else {}
    microformat = player_response.get("microformat", {}).get("playerMicroformatRenderer", {})
    microformat = microformat if isinstance(microformat, dict) else {}
    playability = player_response.get("playabilityStatus")
    playability = playability if isinstance(playability, dict) else {}
    source_video_id = str(details.get("videoId") or "").strip()
    owner_title, owner_channel_id, owner_channel_url = _channel_from_runs(
        attribution.get("author")
        or attribution.get("authorText")
        or attribution.get("clipAuthor")
        or {}
    )
    owner_thumbnail = attribution.get("authorThumbnail") or attribution.get("authorAvatar")
    owner_thumbnails = (
        owner_thumbnail.get("thumbnails", []) if isinstance(owner_thumbnail, dict) else []
    )
    source_channel_title, rendered_source_channel_id, source_channel_url = (
        _channel_from_runs(source_owner.get("title") or {})
    )
    source_endpoint = source_owner.get("navigationEndpoint")
    source_endpoint = source_endpoint if isinstance(source_endpoint, dict) else {}
    source_browse = source_endpoint.get("browseEndpoint")
    source_browse = source_browse if isinstance(source_browse, dict) else {}
    rendered_source_channel_id = str(
        source_browse.get("browseId") or rendered_source_channel_id
    ).strip()
    source_channel_url = str(
        source_browse.get("canonicalBaseUrl") or source_channel_url
    ).strip()
    source_owner_thumbnail = source_owner.get("thumbnail")
    source_owner_thumbnails = (
        source_owner_thumbnail.get("thumbnails", [])
        if isinstance(source_owner_thumbnail, dict)
        else []
    )
    source_thumbnail = details.get("thumbnail")
    source_thumbnails = (
        source_thumbnail.get("thumbnails", []) if isinstance(source_thumbnail, dict) else []
    )
    view_count_text, clipped_at_text = _clip_created_metadata(attribution)
    availability = "active" if attribution and source_video_id else "unavailable"
    return {
        "clip_id": clip_id,
        "title": text_from_runs(attribution.get("title") or {}).strip(),
        "owner_title": owner_title,
        "owner_channel_id": owner_channel_id,
        "owner_channel_url": owner_channel_url,
        "owner_thumbnail_url": pick_thumbnail(owner_thumbnails),
        "ownership": _clip_ownership(initial_data),
        "source_video_id": source_video_id,
        "source_title": text_from_runs(primary_info.get("title") or {}).strip(),
        "source_channel": source_channel_title or str(details.get("author") or "").strip(),
        "source_channel_id": rendered_source_channel_id or str(details.get("channelId") or "").strip(),
        "source_channel_url": source_channel_url,
        "source_description": str(details.get("shortDescription") or "").strip(),
        "source_duration_text": format_duration(details.get("lengthSeconds")),
        "source_view_count": str(details.get("viewCount") or "").strip(),
        "source_upload_date": str(microformat.get("uploadDate") or "").strip(),
        "source_uploader_category": str(microformat.get("category") or "").strip(),
        "source_reaction": extract_reaction_from_initial_data(initial_data),
        "source_thumbnail_url": pick_thumbnail(source_thumbnails),
        "source_channel_thumbnail_url": (
            pick_thumbnail(source_owner_thumbnails)
            or extract_channel_thumbnail_url(initial_data)
        ),
        "source_is_playable": 1 if str(playability.get("status") or "").upper() == "OK" else 0,
        "source_availability": watch_playability_availability(
            playability,
            microformat,
            details,
        ),
        "start_ms": int(config.get("startTimeMs") or 0),
        "end_ms": int(config.get("endTimeMs") or 0),
        "view_count": _clip_view_count(view_count_text),
        "view_count_text": view_count_text,
        "clipped_at": "",
        "clipped_at_text": clipped_at_text,
        "thumbnail_url": pick_thumbnail(source_thumbnails),
        "availability": availability,
        "fetch_status": "ok" if availability == "active" else "unavailable",
        "fetch_error": "" if availability == "active" else "Clip metadata unavailable",
    }


def fetch_current_youtube_clips(
    cookie_file: Path,
    proxy_url: str = "",
) -> tuple[urllib.request.OpenerDirector, list[dict[str, Any]]]:
    jar = load_cookie_jar(cookie_file)
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        *socks5_proxy_handlers(proxy_url),
    )
    referer = "https://www.youtube.com/feed/clips"
    page = request_text(opener, referer)
    if not youtube_page_is_authenticated(page):
        raise youtube_authentication_error(page, "clips feed")
    config = extract_ytcfg(page)
    initial_data = extract_json_assignment(page, "ytInitialData")
    pages = [initial_data] if initial_data else []
    api_key = config.get("INNERTUBE_API_KEY", "")
    client_version = config.get("INNERTUBE_CLIENT_VERSION", "")
    if not pages and api_key and client_version:
        pages.append(
            request_youtubei_json(
                opener,
                jar,
                api_key,
                {"context": youtube_web_context(config), "browseId": "FEclips"},
                referer,
                client_version,
            )
        )
    if not pages:
        raise RuntimeError("Could not find YouTube clips feed data.")
    token = continuation_token(pages[0])
    seen_tokens: set[str] = set()
    while token and token not in seen_tokens and api_key and client_version:
        seen_tokens.add(token)
        pages.append(
            request_youtubei_json(
                opener,
                jar,
                api_key,
                {"context": youtube_web_context(config), "continuation": token},
                referer,
                client_version,
            )
        )
        token = continuation_token(pages[-1])
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for page_data in pages:
        for node in walk(page_data):
            renderer = node.get("gridVideoRenderer") if isinstance(node, dict) else None
            if not isinstance(renderer, dict):
                continue
            record = parse_clip_grid_renderer(renderer)
            if not record or record["clip_id"] in seen_ids:
                continue
            seen_ids.add(record["clip_id"])
            records.append(record)
    return opener, records


def fetch_clip_metadata(
    opener: urllib.request.OpenerDirector,
    clip_id: str,
) -> dict[str, Any]:
    clip_id = extract_clip_id(clip_id) or clip_id.strip()
    url = f"https://www.youtube.com/clip/{urllib.parse.quote(clip_id)}"
    page = request_text(opener, url)
    return parse_clip_page(
        extract_json_assignment(page, "ytInitialData"),
        extract_json_assignment(page, "ytInitialPlayerResponse"),
        clip_id,
    )


def _library_owner_identity(conn: sqlite3.Connection) -> dict[str, str]:
    library_channel_id, _library_owner_name = library_playlist_owner_identity(conn)
    row = conn.execute(
        """
        SELECT p.owner_channel_id,
               COALESCE(c.title, '') AS title,
               COALESCE(c.thumbnail_url, '') AS thumbnail_url,
               COALESCE(c.thumbnail_path, '') AS thumbnail_path,
               COUNT(*) AS playlist_count
        FROM playlists p
        LEFT JOIN channels c ON c.channel_id = p.owner_channel_id
        WHERE p.ownership = 'mine'
          AND p.owner_channel_id IS NOT NULL
          AND (? = '' OR p.owner_channel_id = ?)
        GROUP BY p.owner_channel_id
        ORDER BY playlist_count DESC, p.owner_channel_id
        LIMIT 1
        """,
        (library_channel_id, library_channel_id),
    ).fetchone()
    return dict(row) if row else {}


def save_clip_metadata(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    *,
    observed_at: str | None = None,
    fetched: bool = False,
) -> None:
    clip_id = str(record.get("clip_id") or "").strip()
    source_video_id = str(record.get("source_video_id") or "").strip()
    if not clip_id:
        raise ValueError("Clip metadata needs a clip ID")
    now = observed_at or utc_now()
    if source_video_id:
        source_channel_id = upsert_channel(
            conn,
            str(record.get("source_channel_id") or "").strip(),
            title=str(record.get("source_channel") or "").strip(),
            url=str(record.get("source_channel_url") or "").strip(),
            thumbnail_url=str(record.get("source_channel_thumbnail_url") or "").strip(),
            thumbnail_path=str(record.get("source_channel_thumbnail_path") or "").strip(),
            source="youtube",
            updated_at=now,
        )
        upsert_video(
            conn,
            source_video_id,
            title=str(record.get("source_title") or "").strip(),
            description=str(record.get("source_description") or "").strip(),
            channel_id=source_channel_id,
            channel_title=str(record.get("source_channel") or "").strip(),
            duration_text=str(record.get("source_duration_text") or "").strip(),
            view_count=str(record.get("source_view_count") or "").strip(),
            upload_date=str(record.get("source_upload_date") or "").strip(),
            uploader_category=str(record.get("source_uploader_category") or "").strip(),
            thumbnail_url=str(record.get("source_thumbnail_url") or record.get("thumbnail_url") or "").strip(),
            thumbnail_path=str(record.get("source_thumbnail_path") or "").strip(),
            reaction=str(record.get("source_reaction") or "").strip(),
            is_playable=record.get("source_is_playable"),
            availability=str(record.get("source_availability") or "").strip(),
            source="youtube",
            updated_at=now,
        )
    owner = _library_owner_identity(conn) if record.get("ownership") == "mine" else {}
    owner_channel_id = str(record.get("owner_channel_id") or owner.get("owner_channel_id") or "").strip()
    owner_title = str(record.get("owner_title") or owner.get("title") or "").strip()
    if owner_channel_id:
        owner_channel_id = upsert_channel(
            conn,
            owner_channel_id,
            title=owner_title,
            url=str(record.get("owner_channel_url") or "").strip(),
            thumbnail_url=str(record.get("owner_thumbnail_url") or owner.get("thumbnail_url") or "").strip(),
            thumbnail_path=str(record.get("owner_thumbnail_path") or owner.get("thumbnail_path") or "").strip(),
            source="youtube",
            updated_at=now,
        )
    conn.execute(
        """
        INSERT INTO clips(
          clip_id, title, owner_channel_id, owner_title, owner_thumbnail_url,
          owner_thumbnail_path, ownership, source_video_id, start_ms, end_ms,
          view_count, view_count_text, clipped_at, clipped_at_text,
          clipped_at_observed_at, youtube_feed_ordinal, thumbnail_url,
          availability, fetch_status,
          fetch_error, fetched_at, last_seen_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(clip_id) DO UPDATE SET
          title=COALESCE(NULLIF(excluded.title, ''), clips.title),
          owner_channel_id=COALESCE(excluded.owner_channel_id, clips.owner_channel_id),
          owner_title=COALESCE(NULLIF(excluded.owner_title, ''), clips.owner_title),
          owner_thumbnail_url=COALESCE(NULLIF(excluded.owner_thumbnail_url, ''), clips.owner_thumbnail_url),
          owner_thumbnail_path=COALESCE(NULLIF(excluded.owner_thumbnail_path, ''), clips.owner_thumbnail_path),
          ownership=CASE WHEN excluded.ownership <> 'unknown' THEN excluded.ownership ELSE clips.ownership END,
          source_video_id=COALESCE(excluded.source_video_id, clips.source_video_id),
          start_ms=COALESCE(excluded.start_ms, clips.start_ms),
          end_ms=COALESCE(excluded.end_ms, clips.end_ms),
          view_count=COALESCE(excluded.view_count, clips.view_count),
          view_count_text=COALESCE(NULLIF(excluded.view_count_text, ''), clips.view_count_text),
          clipped_at=COALESCE(excluded.clipped_at, clips.clipped_at),
          clipped_at_text=COALESCE(NULLIF(excluded.clipped_at_text, ''), clips.clipped_at_text),
          clipped_at_observed_at=COALESCE(
            clips.clipped_at_observed_at,
            excluded.clipped_at_observed_at
          ),
          youtube_feed_ordinal=COALESCE(
            excluded.youtube_feed_ordinal,
            clips.youtube_feed_ordinal
          ),
          thumbnail_url=COALESCE(NULLIF(excluded.thumbnail_url, ''), clips.thumbnail_url),
          availability=CASE WHEN excluded.availability <> 'unknown' THEN excluded.availability ELSE clips.availability END,
          fetch_status=COALESCE(NULLIF(excluded.fetch_status, ''), clips.fetch_status),
          fetch_error=excluded.fetch_error,
          fetched_at=COALESCE(excluded.fetched_at, clips.fetched_at),
          last_seen_at=COALESCE(excluded.last_seen_at, clips.last_seen_at),
          updated_at=excluded.updated_at
        """,
        (
            clip_id,
            str(record.get("title") or "").strip(),
            owner_channel_id or None,
            owner_title,
            str(record.get("owner_thumbnail_url") or "").strip(),
            str(record.get("owner_thumbnail_path") or "").strip(),
            str(record.get("ownership") or "unknown").strip(),
            source_video_id or None,
            int(record.get("start_ms") or 0) or None,
            int(record.get("end_ms") or 0) or None,
            record.get("view_count"),
            str(record.get("view_count_text") or "").strip(),
            str(record.get("clipped_at") or "").strip() or None,
            str(record.get("clipped_at_text") or "").strip(),
            now if record.get("clipped_at_text") else None,
            record.get("youtube_feed_ordinal"),
            str(record.get("thumbnail_url") or "").strip(),
            str(record.get("availability") or "unknown").strip(),
            str(record.get("fetch_status") or "").strip(),
            str(record.get("fetch_error") or "").strip(),
            now if fetched else None,
            now if record.get("availability") == "active" else None,
            now,
        ),
    )


def save_discovered_clips(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    existing_ids = {row["clip_id"] for row in conn.execute("SELECT clip_id FROM clips")}
    new_ids: list[str] = []
    now = utc_now()
    for feed_ordinal, source_record in enumerate(records, start=1):
        record = dict(source_record)
        record["youtube_feed_ordinal"] = feed_ordinal
        clip_id = str(record.get("clip_id") or "").strip()
        if not clip_id:
            continue
        if clip_id not in existing_ids:
            existing_ids.add(clip_id)
            new_ids.append(clip_id)
        save_clip_metadata(conn, record, observed_at=now, fetched=False)
    return {
        "discovered": len(records),
        "inserted": len(new_ids),
        "updated": max(0, len(records) - len(new_ids)),
        "new_ids": new_ids,
    }


def is_system_playlist(playlist_id: str) -> bool:
    return playlist_id in {"LL", "LM", "WL"} or playlist_id.startswith("RD")


def library_playlist_owner_identity(conn: sqlite3.Connection) -> tuple[str, str]:
    row = conn.execute(
        """
        SELECT COALESCE(p.owner_channel_id, '') AS owner_channel_id,
               lower(trim(COALESCE(c.title, ''))) AS owner_name,
               COUNT(*) AS playlist_count
        FROM playlists p
        LEFT JOIN channels c ON c.channel_id = p.owner_channel_id
        WHERE p.ownership = 'mine'
          AND trim(COALESCE(p.owner_channel_id, '') || COALESCE(c.title, '')) <> ''
        GROUP BY p.owner_channel_id, owner_name
        ORDER BY playlist_count DESC, owner_channel_id, owner_name
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return "", ""
    return str(row["owner_channel_id"] or ""), str(row["owner_name"] or "")


def dominant_playlist_owner_identity(
    records: Sequence[dict[str, Any]],
) -> tuple[str, str]:
    counts: Counter[tuple[str, str]] = Counter()
    for record in records:
        owner_channel_id = str(record.get("owner_channel_id") or "").strip()
        owner_name = str(record.get("owner") or "").strip().casefold()
        if owner_channel_id or owner_name:
            counts[(owner_channel_id, owner_name)] += 1
    if not counts:
        return "", ""
    ordered = counts.most_common(2)
    (identity, top_count) = ordered[0]
    next_count = ordered[1][1] if len(ordered) > 1 else 0
    if top_count < 2 or top_count < max(2, next_count * 2):
        return "", ""
    return identity


def playlist_ownership_for_owner(
    conn: sqlite3.Connection,
    owner_channel_id: str,
    owner_name: str = "",
    *,
    existing: str = "unknown",
    library_owner_identity: tuple[str, str] | None = None,
) -> str:
    owner_channel_id = (owner_channel_id or "").strip()
    owner_name = (owner_name or "").strip().casefold()
    existing = existing if existing in {"mine", "others", "unknown"} else "unknown"
    if not owner_channel_id and not owner_name:
        return existing
    library_channel_id, library_owner_name = (
        library_owner_identity
        if library_owner_identity is not None
        else library_playlist_owner_identity(conn)
    )
    if (library_channel_id and owner_channel_id == library_channel_id) or (
        library_owner_name and owner_name == library_owner_name
    ):
        return "mine"
    return "others"


def playlist_is_tombstoned(conn: sqlite3.Connection, playlist_id: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM playlist_tombstones WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
    )


def clear_playlist_tombstone(conn: sqlite3.Connection, playlist_id: str) -> None:
    conn.execute(
        "DELETE FROM playlist_tombstones WHERE playlist_id = ?",
        (playlist_id,),
    )


def tombstone_playlist(
    conn: sqlite3.Connection,
    playlist_id: str,
    *,
    reason: str,
    observed_at: str | None = None,
) -> int:
    if reason not in {"authenticated_missing", "missing_from_library", "explicit_user"}:
        raise ValueError(f"Unsupported playlist tombstone reason: {reason}")
    observed_at = observed_at or utc_now()
    conn.execute(
        """
        INSERT INTO playlist_tombstones(
          playlist_id, observed_removed_at, reason, last_confirmed_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(playlist_id) DO UPDATE SET
          reason=excluded.reason,
          last_confirmed_at=excluded.last_confirmed_at
        """,
        (playlist_id, observed_at, reason, observed_at),
    )
    return delete_playlist_and_orphaned_unwatched_videos(conn, playlist_id)


def reconcile_missing_library_playlists(
    conn: sqlite3.Connection,
    current_playlist_ids: set[str],
) -> dict[str, Any]:
    missing_rows = conn.execute(
        """
        SELECT playlist_id, ownership
        FROM playlists
        WHERE in_library = 1
        ORDER BY playlist_id
        """
    ).fetchall()
    stats: dict[str, Any] = {
        "tombstoned": 0,
        "pending_verification": 0,
        "unavailable": 0,
        "videos_deleted": 0,
        "verify_ids": [],
    }
    for row in missing_rows:
        playlist_id = str(row["playlist_id"] or "")
        if not playlist_id or playlist_id in current_playlist_ids:
            continue
        ownership = str(row["ownership"] or "unknown")
        if ownership == "mine":
            stats["videos_deleted"] += tombstone_playlist(
                conn,
                playlist_id,
                reason="missing_from_library",
            )
            stats["tombstoned"] += 1
        elif ownership == "others":
            now = utc_now()
            conn.execute(
                """
                UPDATE playlists
                SET in_library=0, library_missing_at=?, updated_at=?
                WHERE playlist_id=?
                """,
                (now, now, playlist_id),
            )
            stats["pending_verification"] += 1
            stats["verify_ids"].append(playlist_id)
        else:
            now = utc_now()
            conn.execute(
                """
                UPDATE playlists
                SET in_library=0, library_missing_at=NULL, fetch_status='unavailable',
                    fetch_error='Playlist is no longer present in the authenticated library',
                    updated_at=?
                WHERE playlist_id=?
                """,
                (now, playlist_id),
            )
            conn.execute(
                """
                UPDATE playlist_scans
                SET scan_status='unavailable',
                    scan_error='Playlist is no longer present in the authenticated library',
                    scanned_at=?
                WHERE playlist_id=?
                """,
                (now, playlist_id),
            )
            stats["unavailable"] += 1
    return stats


def save_discovered_playlists(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    *,
    reconcile_missing: bool = False,
) -> dict[str, Any]:
    existing_ids = {
        row["playlist_id"]
        for row in conn.execute("SELECT playlist_id FROM playlists")
    }
    inserted = 0
    library_owner_identity = library_playlist_owner_identity(conn)
    if not any(library_owner_identity):
        library_owner_identity = dominant_playlist_owner_identity(records)
    current_playlist_ids: set[str] = set()
    change_candidate_ids: list[str] = []
    for record in records:
        playlist_id = (record.get("playlist_id") or "").strip()
        if not playlist_id:
            continue
        now = utc_now()
        current_playlist_ids.add(playlist_id)
        clear_playlist_tombstone(conn, playlist_id)
        if playlist_id not in existing_ids:
            inserted += 1
            existing_ids.add(playlist_id)
        owner_channel_id = (record.get("owner_channel_id") or "").strip()
        if owner_channel_id:
            owner_channel_id = upsert_channel(
                conn,
                owner_channel_id,
                title=record.get("owner", ""),
                thumbnail_url=record.get("owner_thumbnail_url", ""),
                thumbnail_path=record.get("owner_thumbnail_path", ""),
                source="playlist_owner",
                updated_at=now,
            )
        existing = conn.execute(
            """
            SELECT ownership, video_count, youtube_updated_date
            FROM playlists
            WHERE playlist_id = ?
            """,
            (playlist_id,),
        ).fetchone()
        has_video_count = (
            bool(record.get("has_video_count"))
            if "has_video_count" in record
            else record.get("video_count") not in (None, "")
        )
        video_count = max(0, int(record.get("video_count") or 0))
        if (
            existing
            and has_video_count
            and video_count != int(existing["video_count"] or 0)
        ):
            change_candidate_ids.append(playlist_id)
        ownership = playlist_ownership_for_owner(
            conn,
            owner_channel_id,
            record.get("owner", ""),
            existing=str(existing["ownership"] or "unknown") if existing else "unknown",
            library_owner_identity=library_owner_identity,
        )
        youtube_updated_date = merge_youtube_playlist_updated_date(
            existing["youtube_updated_date"] if existing else "",
            record.get("youtube_updated_date"),
            str(record.get("youtube_updated_text") or ""),
        )
        conn.execute(
            """
            INSERT INTO playlists(
              playlist_id, title, description, owner_channel_id, visibility,
              youtube_updated_date, ownership, in_library, video_count,
              thumbnail_url, thumbnail_path,
              fetch_status, fetch_error, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'ok', '', ?)
            ON CONFLICT(playlist_id) DO UPDATE SET
              title=excluded.title,
              description=COALESCE(NULLIF(excluded.description, ''), playlists.description),
              youtube_updated_date=COALESCE(
                NULLIF(excluded.youtube_updated_date, ''),
                playlists.youtube_updated_date
              ),
              ownership=CASE
                WHEN excluded.ownership <> 'unknown' THEN excluded.ownership
                ELSE playlists.ownership
              END,
              in_library=1,
              library_missing_at=NULL,
              owner_channel_id=COALESCE(excluded.owner_channel_id, playlists.owner_channel_id),
              visibility=COALESCE(NULLIF(excluded.visibility, ''), playlists.visibility),
              video_count=CASE
                WHEN ? = 0
                  OR EXISTS (
                    SELECT 1
                    FROM playlist_scans
                    WHERE playlist_id = playlists.playlist_id
                  )
                THEN playlists.video_count
                ELSE excluded.video_count
              END,
              thumbnail_url=COALESCE(NULLIF(excluded.thumbnail_url, ''), playlists.thumbnail_url),
              thumbnail_path=COALESCE(NULLIF(excluded.thumbnail_path, ''), playlists.thumbnail_path),
              fetch_status='ok',
              fetch_error='',
              updated_at=excluded.updated_at
            """,
            (
                playlist_id,
                record.get("title", ""),
                record.get("description", ""),
                owner_channel_id or None,
                record.get("visibility", ""),
                youtube_updated_date or None,
                ownership,
                video_count,
                record.get("thumbnail_url", ""),
                record.get("thumbnail_path", ""),
                now,
                1 if has_video_count else 0,
            ),
        )
    reconciliation: dict[str, Any] = {
        "tombstoned": 0,
        "pending_verification": 0,
        "unavailable": 0,
        "videos_deleted": 0,
        "verify_ids": [],
    }
    if reconcile_missing:
        reconciliation = reconcile_missing_library_playlists(conn, current_playlist_ids)
    return {
        "discovered": len(records),
        "inserted": inserted,
        "updated": max(0, len(records) - inserted),
        "change_candidate_ids": change_candidate_ids,
        **reconciliation,
    }


def discover_current_playlists(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    thumb_dir = Path(args.thumbs)
    proxy_url = configured_proxy(getattr(args, "config_data", {}))
    opener, records = fetch_current_youtube_playlists(
        Path(args.cookies),
        args.browse_id,
        proxy_url,
        effective_display_timezone(getattr(args, "config_data", {})),
    )
    if not args.include_system:
        records = [record for record in records if not is_system_playlist(record["playlist_id"])]

    print(f"Discovered {len(records)} current YouTube playlists.")
    enriched_records: list[dict[str, str]] = []
    for index, record in enumerate(records, start=1):
        record = dict(record)
        playlist_id = record["playlist_id"]
        record["thumbnail_path"] = cache_thumbnail(
            opener,
            playlist_id,
            record["thumbnail_url"],
            thumb_dir,
        )
        owner_channel_id = record.get("owner_channel_id", "")
        if owner_channel_id and record.get("owner_thumbnail_url"):
            record["owner_thumbnail_path"] = cache_channel_thumbnail(
                opener,
                owner_channel_id,
                record.get("owner_thumbnail_url", ""),
                DEFAULT_VIDEO_THUMB_DIR,
                referer_url=record["url"],
            )
        enriched_records.append(record)
        print(f"[{index:03d}/{len(records):03d}] {record['title']}")

    conn = connect(db_path)
    try:
        with conn:
            save_discovered_playlists(conn, enriched_records)
    finally:
        conn.close()
    print(f"Updated {len(records)} playlists.")
    print(f"Wrote {db_path}")


_WORKER_LOG_TARGETS = {
    "metadata": ("metadata_worker_log", "video_id"),
    "playlist": ("playlist_scan_worker_log", "playlist_id"),
    "history": ("live_history_worker_log", "video_id"),
    "placeholder": ("placeholder_recovery_worker_log", "video_id"),
}


def _log_typed_worker_event(
    conn: sqlite3.Connection,
    worker_type: str,
    run_id: str,
    level: str,
    message: str,
    subject_id: str = "",
) -> None:
    table, subject_column = _WORKER_LOG_TARGETS[worker_type]
    conn.execute(
        f"""
        INSERT INTO {table}(run_id, created_at, level, {subject_column}, message)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, utc_now(), level, subject_id, message),
    )


def log_worker_event(
    conn: sqlite3.Connection,
    run_id: str,
    level: str,
    message: str,
    video_id: str = "",
) -> None:
    _log_typed_worker_event(conn, "metadata", run_id, level, message, video_id)


def log_worker_queue_event(
    conn: sqlite3.Connection,
    level: str,
    message: str,
    *,
    run_id: str = "",
) -> None:
    log_worker_event(
        conn,
        run_id,
        f"queue {(level or 'info').strip().lower()}",
        message,
    )


def log_playlist_scan_event(
    conn: sqlite3.Connection,
    run_id: str,
    level: str,
    message: str,
    playlist_id: str = "",
) -> None:
    _log_typed_worker_event(conn, "playlist", run_id, level, message, playlist_id)


def log_live_history_event(
    conn: sqlite3.Connection,
    run_id: str,
    level: str,
    message: str,
    video_id: str = "",
) -> None:
    _log_typed_worker_event(conn, "history", run_id, level, message, video_id)


def log_placeholder_recovery_event(
    conn: sqlite3.Connection,
    run_id: str,
    level: str,
    message: str,
    video_id: str = "",
) -> None:
    _log_typed_worker_event(conn, "placeholder", run_id, level, message, video_id)


def placeholder_recovery_candidate_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
    include_completed: bool = False,
    playlist_id: str = "",
    video_id: str = "",
    only_missing_thumbnails: bool = False,
    likely_unavailable_only: bool = False,
    order_by: str = "playlist",
) -> list[sqlite3.Row]:
    if order_by not in {"playlist", "video"}:
        raise ValueError(f"Unsupported placeholder recovery order: {order_by}")
    availability_category_sql = video_availability_category_sql(
        video_id="v.video_id",
        availability="v.availability",
        is_playable="v.is_playable",
    )
    where = [
        "pi.video_id IS NOT NULL",
        f"({availability_category_sql}) = 'unavailable'",
    ]
    if not include_completed:
        where.append("(r.video_id IS NULL OR r.search_status = 'error')")
    params: list[Any] = []
    if playlist_id:
        where.append("pi.playlist_id = ?")
        params.append(playlist_id)
    if video_id:
        where.append("pi.video_id = ?")
        params.append(video_id)
    if only_missing_thumbnails:
        where.append("(r.video_id IS NULL OR v.thumbnail_path = '')")
    ordering = (
        "pi.video_id"
        if order_by == "video"
        else "MIN(p.title) COLLATE NOCASE, MIN(pi.position), pi.video_id"
    )
    sql = f"""
        SELECT pi.video_id,
               MIN(pi.playlist_id) AS playlist_id,
               MIN(pi.position) AS display_position,
               MIN(p.title) AS playlist_title,
               MIN(v.title) AS title,
               COUNT(DISTINCT pi.playlist_id) AS playlist_count,
               COALESCE(r.search_status, '') AS previous_status
        FROM playlist_items pi
        JOIN playlists p ON p.playlist_id = pi.playlist_id
        JOIN videos v ON v.video_id = pi.video_id
        LEFT JOIN video_recovery r ON r.video_id = pi.video_id
        WHERE {" AND ".join(where)}
        GROUP BY pi.video_id, COALESCE(r.search_status, '')
        ORDER BY {ordering}
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(max(0, offset))
    return conn.execute(sql, params).fetchall()


def playlist_placeholder_recovery_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
    force: bool = False,
    playlist_id: str = "",
) -> list[sqlite3.Row]:
    return placeholder_recovery_candidate_rows(
        conn,
        limit=limit,
        offset=offset,
        include_completed=force,
        playlist_id=playlist_id,
    )


def placeholder_queue_subject_key(video_id: str) -> str:
    return f"placeholder:{(video_id or '').strip()}"


def placeholder_recovery_is_complete(conn: sqlite3.Connection, video_id: str) -> bool:
    video_id = (video_id or "").strip()
    if not video_id:
        return False
    row = conn.execute(
        """
        SELECT search_status, archivarix_status
        FROM video_recovery
        WHERE video_id = ?
        """,
        (video_id,),
    ).fetchone()
    if not row:
        return False
    return (
        str(row["search_status"] or "") in {"found", "thumbnail_only", "not_found"}
        or bool(str(row["archivarix_status"] or "").strip())
    )


def enqueue_placeholder_recovery_targets(
    conn: sqlite3.Connection,
    playlist_id: str,
) -> dict[str, int]:
    rows = playlist_placeholder_recovery_rows(conn, force=False, playlist_id=playlist_id)
    inserted = 0
    existing = 0
    now = utc_now()
    # Recovery should not interrupt the scan that discovered it or the work
    # already queued ahead of it. Keep this batch at the current queue tail.
    tail_priority = int(
        conn.execute("SELECT COALESCE(MAX(priority), 0) + 1 AS priority FROM worker_queue").fetchone()["priority"]
        or 1
    )
    for row in rows:
        video_id = row["video_id"] or ""
        if not video_id:
            continue
        was_inserted = enqueue_placeholder_recovery_item(
            conn,
            video_id=video_id,
            playlist_id=playlist_id,
            current_title=row["title"] or "",
            playlist_count=int(row["playlist_count"] or 0),
            priority=tail_priority,
            updated_at=now,
        )
        if not was_inserted:
            existing += 1
        else:
            inserted += 1
    return {"inserted": inserted, "existing": existing}


def enqueue_placeholder_recovery_item(
    conn: sqlite3.Connection,
    *,
    video_id: str,
    playlist_id: str = "",
    current_title: str = "",
    source_key: str = "",
    playlist_count: int = 0,
    priority: int = 100,
    updated_at: str = "",
    manual: bool = False,
    task_type: str = "recover",
    payload: dict[str, Any] | None = None,
) -> bool:
    video_id = (video_id or "").strip()
    if not video_id:
        return False
    if not manual and placeholder_recovery_is_complete(conn, video_id):
        return False
    subject_key = placeholder_queue_subject_key(video_id)
    task_type = "thumbnail" if task_type == "thumbnail" else "recover"
    payload_json = json.dumps(
        payload or {},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    queued = conn.execute(
        "SELECT 1 FROM worker_queue WHERE subject_key = ?",
        (subject_key,),
    ).fetchone()
    now = updated_at or utc_now()
    conn.execute(
        """
        INSERT INTO worker_queue(
          subject_key, worker_type, task_type, video_id, channel_id, playlist_id,
          channel_title, current_title, source_key, playlist_count, priority, manual,
          payload_json, created_at, updated_at
        )
        VALUES (?, 'placeholder', ?, ?, '', ?, '', ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(subject_key) DO UPDATE SET
          task_type=CASE
            WHEN excluded.manual = 1 THEN excluded.task_type
            ELSE worker_queue.task_type
          END,
          playlist_id=COALESCE(NULLIF(excluded.playlist_id, ''), worker_queue.playlist_id),
          current_title=COALESCE(NULLIF(excluded.current_title, ''), worker_queue.current_title),
          source_key=COALESCE(NULLIF(excluded.source_key, ''), worker_queue.source_key),
          playlist_count=MAX(worker_queue.playlist_count, excluded.playlist_count),
          priority=MIN(worker_queue.priority, excluded.priority),
          manual=MAX(worker_queue.manual, excluded.manual),
          payload_json=CASE
            WHEN excluded.manual = 1 THEN excluded.payload_json
            ELSE worker_queue.payload_json
          END,
          updated_at=excluded.updated_at
        """,
        (
            subject_key,
            task_type,
            video_id,
            playlist_id or "",
            current_title or "",
            source_key or "",
            max(0, int(playlist_count or 0)),
            int(priority),
            1 if manual else 0,
            payload_json,
            now,
            now,
        ),
    )
    return not bool(queued)


def placeholder_worker_queue_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    queue_id: int = 0,
) -> list[sqlite3.Row]:
    sql = f"""
        SELECT queue_id, task_type, video_id, playlist_id, current_title,
               source_key, priority, payload_json
        FROM worker_queue
        WHERE worker_type = 'placeholder'
        ORDER BY {worker_queue_order_sql()}
    """
    params: list[Any] = []
    if queue_id:
        sql = sql.replace(
            "WHERE worker_type = 'placeholder'",
            "WHERE worker_type = 'placeholder' AND queue_id = ?",
        )
        params.append(queue_id)
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def archive_capture_timestamp(url: str) -> str | None:
    match = re.search(r"/web/(\d{14})", url or "")
    if not match:
        return None
    parsed = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def save_video_recovery(
    conn: sqlite3.Connection,
    video_id: str,
    video: dict[str, Any] | None,
    search_status: str,
    search_error: str,
    thumbnail_url: str = "",
    thumbnail_path: str = "",
) -> None:
    now = utc_now()
    recovered_status = (video or {}).get("status") or ""
    if search_status == "not_found":
        recovered_status = "NOT_FOUND"
    media_available: int | None = None
    if search_status == "found":
        media_available = (
            1
            if str((video or {}).get("videoFileUrl") or "").strip()
            else 0
        )
    archivarix_channel_id = str((video or {}).get("channelId") or "")
    channel_id = upsert_channel(
        conn,
        str((video or {}).get("channelExternalId") or ""),
        title=str((video or {}).get("channelTitle") or ""),
        url=str((video or {}).get("channelUrl") or ""),
        description=str((video or {}).get("channelDescription") or ""),
        aliases=str((video or {}).get("channelAliases") or ""),
        thumbnail_url=str((video or {}).get("channelThumbnailUrl") or ""),
        thumbnail_path=str((video or {}).get("channelThumbnailPath") or ""),
        archivarix_channel_id=archivarix_channel_id if not archivarix_channel_id.startswith("UC") else "",
        source="archivarix",
    )
    upsert_video(
        conn,
        video_id,
        title=str((video or {}).get("title") or ""),
        description=str((video or {}).get("description") or ""),
        channel_id=channel_id,
        duration_text=format_duration((video or {}).get("duration")),
        view_count=str((video or {}).get("viewCount") or ""),
        upload_date=str((video or {}).get("uploadDate") or ""),
        thumbnail_url=thumbnail_url,
        thumbnail_path=thumbnail_path,
        source="archivarix",
        updated_at=now,
    )
    conn.execute(
        """
        INSERT INTO video_recovery(
          video_id, archivarix_status, archivarix_channel_id, archive_capture_at,
          media_available, searched_at, search_status, search_error, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
          archivarix_status=COALESCE(
            NULLIF(excluded.archivarix_status, ''),
            video_recovery.archivarix_status
          ),
          archivarix_channel_id=COALESCE(
            NULLIF(excluded.archivarix_channel_id, ''),
            video_recovery.archivarix_channel_id
          ),
          archive_capture_at=COALESCE(excluded.archive_capture_at, video_recovery.archive_capture_at),
          media_available=COALESCE(excluded.media_available, video_recovery.media_available),
          searched_at=excluded.searched_at,
          search_status=excluded.search_status,
          search_error=excluded.search_error,
          updated_at=excluded.updated_at
        """,
        (
            video_id,
            recovered_status,
            archivarix_channel_id if not archivarix_channel_id.startswith("UC") else "",
            archive_capture_timestamp(str((video or {}).get("archiveUrl") or "")),
            media_available,
            now,
            search_status,
            search_error,
            now,
        ),
    )


def recover_archivarix_video(
    video_id: str,
    thumb_dir: Path,
    archivarix_opener: urllib.request.OpenerDirector,
    refresh_metadata: bool = False,
    no_api: bool = False,
    delay: float = 0,
    channel_cache: dict[str, dict[str, Any]] | None = None,
    stop_event: threading.Event | None = None,
    request_timeout: int = 20,
    stream_timeout: int = 25,
    thumbnail_timeout: int = 12,
    channel_thumbnail_timeout: int = 30,
) -> tuple[dict[str, Any] | None, str, str, str, str]:
    if stop_event and stop_event.is_set():
        return None, "", "", "stopped", "Stop requested"
    status = "not_found"
    error = ""
    video: dict[str, Any] | None = None
    thumbnail_url = ""
    thumbnail_path = cache_archivarix_thumbnail(
        video_id,
        "",
        thumb_dir,
        archivarix_opener,
        timeout=thumbnail_timeout,
        stop_event=stop_event,
    )
    if thumbnail_path and not refresh_metadata:
        status = "thumbnail_only"
    elif not no_api:
        try:
            if delay:
                if stop_event:
                    if stop_event.wait(delay):
                        return None, "", thumbnail_path, "stopped", "Stop requested"
                else:
                    time.sleep(delay)
            video = archivarix_lookup_video(
                video_id,
                archivarix_opener,
                channel_cache=channel_cache,
                stop_event=stop_event,
                request_timeout=request_timeout,
                stream_timeout=stream_timeout,
            )
        except ArchivarixQuotaExceeded as exc:
            status = "rate_limited"
            error = str(exc)
        except urllib.error.HTTPError as exc:
            quota_message = archivarix_http_error_message(exc)
            if quota_message:
                status = "rate_limited"
                error = quota_message
            else:
                status = "error"
                error = str(exc)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            status = "timeout" if archivarix_timeout_error(exc) else "error"
            error = str(exc)
    if video:
        if stop_event and stop_event.is_set():
            return None, "", thumbnail_path, "stopped", "Stop requested"
        status = "found"
        thumbnail_url = video.get("thumbnailArchiveUrl") or video.get("thumbnailUrl") or ""
        thumbnail_path = thumbnail_path or cache_archivarix_thumbnail(
            video_id,
            thumbnail_url,
            thumb_dir,
            archivarix_opener,
            timeout=thumbnail_timeout,
            stop_event=stop_event,
        )
        channel_thumbnail_url = video.get("channelThumbnailUrl") or ""
        if channel_thumbnail_url:
            video["channelThumbnailPath"] = cache_channel_thumbnail(
                archivarix_opener,
                video_id,
                channel_thumbnail_url,
                thumb_dir,
                timeout=channel_thumbnail_timeout,
            )
    return video, thumbnail_url, thumbnail_path, status, error


HistoryOccurrenceKey = tuple[str, str]
HistoryOccurrenceSnapshot = dict[HistoryOccurrenceKey, list[tuple[str, int]]]


def history_instant_key(video_id: str, watched_at: str) -> tuple[str, str]:
    return video_id or "", normalize_utc_timestamp(watched_at)


def save_my_activity_events(
    conn: sqlite3.Connection,
    watch_events: Sequence[Any],
    subscription_events: Sequence[Any],
    timezone_name: str = DEFAULT_DISPLAY_TIMEZONE,
) -> dict[str, Any]:
    """Persist normalized My Activity evidence and refresh canonical projections."""

    now = utc_now()
    incoming_occurrences = {
        ("watch", str(event.video_id), str(event.watched_at))
        for event in watch_events
    } | {
        (
            "subscription",
            str(event.channel_id),
            str(event.subscribed_at),
        )
        for event in subscription_events
    }
    known_occurrences = {
        ("watch", str(row["video_id"]), str(row["watched_at"]))
        for row in conn.execute(
            "SELECT video_id, watched_at FROM my_activity_watch_events"
        )
    } | {
        ("subscription", str(row["channel_id"]), str(row["subscribed_at"]))
        for row in conn.execute(
            "SELECT channel_id, subscribed_at FROM my_activity_subscription_events"
        )
    }
    watch_inserted = 0
    watch_existing = 0
    subscription_inserted = 0
    subscription_existing = 0

    for event in watch_events:
        incoming_event_id = str(event.event_id)
        video_id = str(event.video_id)
        watched_at = str(event.watched_at)
        observed_title = str(event.title or "")
        observed_url = str(event.url or "")
        upsert_video(
            conn,
            video_id,
            title=observed_title,
            source="my_activity",
            updated_at=now,
        )
        stored_source = conn.execute(
            """
            SELECT *
            FROM my_activity_watch_events
            WHERE event_id = ? OR (video_id = ? AND watched_at = ?)
            ORDER BY event_id = ? DESC
            LIMIT 1
            """,
            (incoming_event_id, video_id, watched_at, incoming_event_id),
        ).fetchone()
        event_id = str(stored_source["event_id"]) if stored_source else incoming_event_id
        if stored_source and (
            stored_source["video_id"] != video_id
            or stored_source["watched_at"] != watched_at
        ):
            raise RuntimeError(f"Stored My Activity identity conflicts with {event_id}")
        conn.execute(
            """
            INSERT INTO my_activity_watch_events(
              event_id, video_id, watched_at, observed_title, observed_url,
              collected_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
              observed_title=CASE
                WHEN excluded.observed_title <> '' THEN excluded.observed_title
                ELSE my_activity_watch_events.observed_title
              END,
              observed_url=CASE
                WHEN excluded.observed_url <> '' THEN excluded.observed_url
                ELSE my_activity_watch_events.observed_url
              END,
              updated_at=excluded.updated_at
            """,
            (
                event_id,
                video_id,
                watched_at,
                observed_title,
                observed_url,
                now,
                now,
            ),
        )
        canonical = conn.execute(
            """
            SELECT *
            FROM history_events
            WHERE my_activity_event_id = ? OR event_id = ?
            ORDER BY my_activity_event_id IS NOT NULL DESC
            LIMIT 1
            """,
            (event_id, event_id),
        ).fetchone()
        watch_date = local_date_for_utc_instant(watched_at, timezone_name)
        if canonical:
            if (
                canonical["video_id"] != video_id
                or history_instant_key(canonical["video_id"], canonical["watched_at"])
                != history_instant_key(video_id, watched_at)
            ):
                raise RuntimeError(f"Canonical history identity conflicts with {event_id}")
            source_type = history_source_type_for_identity(
                event_id,
                canonical["takeout_history_key"],
                canonical["youtube_ordinal"],
            )
            match_type = history_match_type_for_identity(
                event_id,
                canonical["takeout_history_key"],
                canonical["youtube_ordinal"],
            )
            conn.execute(
                """
                UPDATE history_events
                SET video_id=?, watched_at=?, watch_date=?, time_precision='exact',
                    my_activity_event_id=?, source_type=?, match_type=?, updated_at=?
                WHERE event_id=?
                """,
                (
                    video_id,
                    watched_at,
                    watch_date or None,
                    event_id,
                    source_type,
                    match_type,
                    now,
                    canonical["event_id"],
                ),
            )
            watch_existing += 1
        else:
            conn.execute(
                """
                INSERT INTO history_events(
                  event_id, video_id, watched_at, watch_date, time_precision,
                  source_type, match_type, my_activity_event_id,
                  imported_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'exact', 'my_activity',
                        'my_activity_only', ?, ?, ?)
                """,
                (
                    event_id,
                    video_id,
                    watched_at,
                    watch_date or None,
                    event_id,
                    now,
                    now,
                ),
            )
            watch_inserted += 1

    for event in subscription_events:
        incoming_event_id = str(event.event_id)
        channel_id = str(event.channel_id)
        subscribed_at = str(event.subscribed_at)
        observed_title = str(event.title or "")
        observed_url = str(event.url or "")
        upsert_channel(
            conn,
            channel_id,
            title=observed_title,
            url=observed_url,
            source="my_activity",
            updated_at=now,
        )
        stored_source = conn.execute(
            """
            SELECT *
            FROM my_activity_subscription_events
            WHERE event_id = ? OR (channel_id = ? AND subscribed_at = ?)
            ORDER BY event_id = ? DESC
            LIMIT 1
            """,
            (incoming_event_id, channel_id, subscribed_at, incoming_event_id),
        ).fetchone()
        event_id = str(stored_source["event_id"]) if stored_source else incoming_event_id
        if stored_source and (
            stored_source["channel_id"] != channel_id
            or stored_source["subscribed_at"] != subscribed_at
        ):
            raise RuntimeError(f"Stored My Activity identity conflicts with {event_id}")
        conn.execute(
            """
            INSERT INTO my_activity_subscription_events(
              event_id, channel_id, subscribed_at, observed_title, observed_url,
              collected_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
              observed_title=CASE
                WHEN excluded.observed_title <> '' THEN excluded.observed_title
                ELSE my_activity_subscription_events.observed_title
              END,
              observed_url=CASE
                WHEN excluded.observed_url <> '' THEN excluded.observed_url
                ELSE my_activity_subscription_events.observed_url
              END,
              updated_at=excluded.updated_at
            """,
            (
                event_id,
                channel_id,
                subscribed_at,
                observed_title,
                observed_url,
                now,
                now,
            ),
        )
        channel = conn.execute(
            "SELECT subscribed_at, subscribed_at_source FROM channels WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        if channel:
            selected_at = max(
                channel["subscribed_at"] or ""
                if channel["subscribed_at_source"] == "my_activity"
                else "",
                subscribed_at,
            )
            conn.execute(
                """
                UPDATE channels
                SET subscribed_at=?, subscribed_at_source='my_activity', updated_at=?
                WHERE channel_id=?
                """,
                (selected_at, now, channel_id),
            )
        if stored_source:
            subscription_existing += 1
        else:
            subscription_inserted += 1

    merge_stats = merge_my_activity_takeout_duplicates(conn)
    reconciliation = rebuild_history_reconciliation(conn, timezone_name)
    return {
        "watch_events": len(watch_events),
        "watch_inserted": watch_inserted,
        "watch_existing": watch_existing,
        "subscription_events": len(subscription_events),
        "subscription_inserted": subscription_inserted,
        "subscription_existing": subscription_existing,
        "known_before": len(known_occurrences),
        "overlap_events": len(known_occurrences & incoming_occurrences),
        "first_collection": not known_occurrences,
        "merged_takeout_rows": merge_stats["merged"],
        "ambiguous_takeout_rows": merge_stats["ambiguous"],
        "history_rows": reconciliation["rows"],
        "matched_history_rows": reconciliation["matched"],
    }


def save_youtube_data_api_snapshot(
    conn: sqlite3.Connection,
    snapshot: Any,
) -> dict[str, int]:
    """Apply a complete current YouTube Data API snapshot without keeping scan diffs."""

    now = utc_now()
    subscription_ids: set[str] = set()
    for subscription in snapshot.subscriptions:
        channel_id = str(subscription.channel_id or "").strip()
        if not channel_id:
            continue
        subscription_ids.add(channel_id)
        subscribed_at = normalize_utc_timestamp(str(subscription.published_at or ""))
        upsert_channel(
            conn,
            channel_id,
            title=str(subscription.title or ""),
            source="youtube_data_api",
            updated_at=now,
        )
        conn.execute(
            """
            UPDATE channels
            SET subscribed=1,
                subscribed_at=CASE
                  WHEN subscribed_at_source = 'my_activity' THEN subscribed_at
                  ELSE COALESCE(NULLIF(?, ''), subscribed_at)
                END,
                subscribed_at_source=CASE
                  WHEN subscribed_at_source = 'my_activity' THEN subscribed_at_source
                  WHEN ? <> '' THEN 'youtube_data_api'
                  ELSE subscribed_at_source
                END,
                subscription_checked_at=?, updated_at=?
            WHERE channel_id=?
            """,
            (subscribed_at, subscribed_at, now, now, channel_id),
        )
    if subscription_ids:
        placeholders = ",".join("?" for _ in subscription_ids)
        conn.execute(
            f"""
            UPDATE channels
            SET subscribed=0, notification_level='', subscription_checked_at=?, updated_at=?
            WHERE subscribed=1 AND channel_id NOT IN ({placeholders})
            """,
            (now, now, *sorted(subscription_ids)),
        )
    else:
        conn.execute(
            """
            UPDATE channels
            SET subscribed=0, notification_level='', subscription_checked_at=?, updated_at=?
            WHERE subscribed=1
            """,
            (now, now),
        )

    playlist_items_updated = 0
    playlist_items_inserted = 0
    playlist_items_unmatched = 0
    for playlist in snapshot.playlists:
        playlist_id = str(playlist.playlist_id or "").strip()
        if not playlist_id:
            continue
        previous_signature = _playlist_change_signature(conn, playlist_id)
        owner_channel_id = upsert_channel(
            conn,
            str(playlist.owner_channel_id or ""),
            source="youtube_data_api",
            updated_at=now,
        ) or None
        created_at = normalize_utc_timestamp(str(playlist.published_at or ""))
        clear_playlist_tombstone(conn, playlist_id)
        conn.execute(
            """
            INSERT INTO playlists(
              playlist_id, title, description, owner_channel_id, visibility,
              created_at, metadata_checked_at, ownership, in_library,
              library_missing_at, video_count,
              fetch_status, fetch_error, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'mine', 1, NULL, ?, 'ok', '', ?)
            ON CONFLICT(playlist_id) DO UPDATE SET
              title=CASE WHEN excluded.title <> '' THEN excluded.title ELSE playlists.title END,
              description=CASE
                WHEN excluded.description <> '' THEN excluded.description
                ELSE playlists.description
              END,
              owner_channel_id=COALESCE(excluded.owner_channel_id, playlists.owner_channel_id),
              visibility=CASE
                WHEN excluded.visibility <> '' THEN excluded.visibility
                ELSE playlists.visibility
              END,
              created_at=COALESCE(excluded.created_at, playlists.created_at),
              metadata_checked_at=excluded.metadata_checked_at,
              ownership='mine',
              in_library=1,
              library_missing_at=NULL,
              video_count=excluded.video_count,
              fetch_status='ok', fetch_error='', updated_at=excluded.updated_at
            """,
            (
                playlist_id,
                str(playlist.title or ""),
                str(playlist.description or ""),
                owner_channel_id,
                normalize_playlist_visibility(str(playlist.visibility or "")),
                created_at or None,
                now,
                max(0, int(playlist.video_count or 0)),
                now,
            ),
        )
        for item in playlist.items:
            video_id = str(item.video_id or "").strip()
            if not video_id:
                continue
            added_at = normalize_utc_timestamp(str(item.published_at or ""))
            upsert_video(
                conn,
                video_id,
                title=str(item.title or ""),
                source="youtube_data_api",
                updated_at=now,
            )
            position = max(1, int(item.position or 1))
            exact = conn.execute(
                """
                SELECT 1 FROM playlist_items
                WHERE playlist_id=? AND position=? AND video_id=?
                """,
                (playlist_id, position, video_id),
            ).fetchone()
            if exact:
                conn.execute(
                    """
                    UPDATE playlist_items
                    SET added_at=COALESCE(NULLIF(?, ''), added_at), updated_at=?
                    WHERE playlist_id=? AND position=? AND video_id=?
                    """,
                    (added_at, now, playlist_id, position, video_id),
                )
                playlist_items_updated += 1
                continue
            same_video = conn.execute(
                """
                SELECT position FROM playlist_items
                WHERE playlist_id=? AND video_id=?
                ORDER BY position
                """,
                (playlist_id, video_id),
            ).fetchall()
            if len(same_video) == 1:
                conn.execute(
                    """
                    UPDATE playlist_items
                    SET added_at=COALESCE(NULLIF(?, ''), added_at), updated_at=?
                    WHERE playlist_id=? AND position=?
                    """,
                    (added_at, now, playlist_id, same_video[0]["position"]),
                )
                playlist_items_updated += 1
                continue
            occupied = conn.execute(
                "SELECT 1 FROM playlist_items WHERE playlist_id=? AND position=?",
                (playlist_id, position),
            ).fetchone()
            if occupied:
                playlist_items_unmatched += 1
                continue
            conn.execute(
                """
                INSERT INTO playlist_items(
                  playlist_id, position, video_id, membership_state,
                  source_quality, added_at, updated_at
                )
                VALUES (?, ?, ?, 'current', 'youtube_api', ?, ?)
                """,
                (playlist_id, position, video_id, added_at or None, now),
            )
            playlist_items_inserted += 1
        if (
            previous_signature is not None
            and previous_signature != _playlist_change_signature(conn, playlist_id)
        ):
            _record_playlist_change(conn, playlist_id, now)
        _refresh_playlist_known_change_at(conn, playlist_id)
        enqueue_playlist_scan_item(
            conn,
            playlist_id,
            title=str(playlist.title or ""),
            source_key="youtube_data_api",
            priority=-1,
            manual=False,
        )
    return {
        "subscriptions": len(subscription_ids),
        "playlists": len(snapshot.playlists),
        "playlist_items": sum(len(playlist.items) for playlist in snapshot.playlists),
        "playlist_items_updated": playlist_items_updated,
        "playlist_items_inserted": playlist_items_inserted,
        "playlist_items_unmatched": playlist_items_unmatched,
    }


def history_occurrence_key(row: sqlite3.Row | dict[str, Any]) -> HistoryOccurrenceKey:
    if isinstance(row, dict):
        return str(row.get("video_id") or ""), str(row.get("watch_date") or "")
    return str(row["video_id"] or ""), str(row["watch_date"] or "")


def youtube_history_occurrence_snapshot(conn: sqlite3.Connection) -> HistoryOccurrenceSnapshot:
    snapshot: HistoryOccurrenceSnapshot = {}
    for row in conn.execute(
        """
        SELECT event_id, video_id, watch_date, youtube_ordinal
        FROM history_events
        WHERE youtube_ordinal IS NOT NULL
        ORDER BY youtube_ordinal
        """
    ):
        snapshot.setdefault(history_occurrence_key(row), []).append(
            (row["event_id"], int(row["youtube_ordinal"]))
        )
    return snapshot


def youtube_history_day_counts(
    snapshot: HistoryOccurrenceSnapshot,
) -> dict[str, Counter[str]]:
    days: dict[str, Counter[str]] = {}
    for (video_id, watch_date), occurrences in snapshot.items():
        if not video_id or not watch_date:
            continue
        days.setdefault(watch_date, Counter())[video_id] += len(occurrences)
    return days


@dataclass
class HistoryDayOverlapTracker:
    existing_days: dict[str, Counter[str]]
    required_matching_days: int = RECENT_HISTORY_OVERLAP_DAYS
    current_date: str = ""
    current_counts: Counter[str] = field(default_factory=Counter)
    consecutive_matching_days: int = 0
    confirmed_days: list[str] = field(default_factory=list)
    reached_overlap: bool = False

    def _finish_current_day(self) -> None:
        if not self.current_date or self.reached_overlap:
            return
        matches = bool(self.current_counts) and self.current_counts == self.existing_days.get(self.current_date)
        if matches:
            self.consecutive_matching_days += 1
            self.confirmed_days.append(self.current_date)
            if self.consecutive_matching_days >= self.required_matching_days:
                self.reached_overlap = True
                self.confirmed_days = self.confirmed_days[-self.required_matching_days :]
        else:
            self.consecutive_matching_days = 0
            self.confirmed_days = []

    def add_rows(self, rows: list[dict[str, Any]]) -> bool:
        for row in rows:
            video_id, watch_date = history_occurrence_key(row)
            if not watch_date:
                self._finish_current_day()
                self.current_date = ""
                self.current_counts.clear()
                if not self.reached_overlap:
                    self.consecutive_matching_days = 0
                    self.confirmed_days = []
                continue
            if self.current_date and watch_date != self.current_date:
                self._finish_current_day()
                self.current_counts = Counter()
            self.current_date = watch_date
            if video_id:
                self.current_counts[video_id] += 1
        return self.reached_overlap

    def finish(self) -> bool:
        self._finish_current_day()
        self.current_date = ""
        self.current_counts = Counter()
        return self.reached_overlap


def youtube_history_event_id(
    video_id: str,
    watch_date: str,
    occurrence_number: int,
) -> str:
    payload = f"{video_id}\x1f{watch_date}\x1f{occurrence_number}"
    return f"youtube:{hashlib.sha1(payload.encode('utf-8', 'replace')).hexdigest()}"


def save_youtube_history_events(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    start: int,
    occurrence_snapshot: HistoryOccurrenceSnapshot,
    seen_occurrences: Counter[HistoryOccurrenceKey],
    timezone_name: str = DEFAULT_DISPLAY_TIMEZONE,
) -> dict[str, Any]:
    now = utc_now()
    observed_at = now
    new = 0
    existing = 0
    last_video_id = ""
    assignments: list[dict[str, Any]] = []
    progress_guards: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=start):
        video_id = row.get("video_id") or ""
        if not video_id:
            continue
        key = history_occurrence_key(row)
        seen_occurrences[key] += 1
        occurrence_number = seen_occurrences[key]
        prior_occurrences = occurrence_snapshot.get(key, [])
        if occurrence_number <= len(prior_occurrences):
            event_id, old_ordinal = prior_occurrences[occurrence_number - 1]
            existing += 1
        else:
            event_id = youtube_history_event_id(video_id, key[1], occurrence_number)
            old_ordinal = None
            new += 1
        last_video_id = video_id
        channel_url = row.get("channel_url") or ""
        channel_id = upsert_channel(
            conn,
            row.get("channel_id") or youtube_channel_id_from_url(channel_url),
            title=row.get("channel") or "",
            url=channel_url,
            first_seen_at=row.get("watched_at") or row.get("watch_date") or None,
            source="youtube_history",
            updated_at=now,
        )
        upsert_video(
            conn,
            video_id,
            title=row.get("title") or "",
            channel_id=channel_id,
            channel_title=row.get("channel") or "",
            channel_url=channel_url,
            source="youtube_history",
            updated_at=now,
        )
        reported_progress = bounded_int(row.get("watch_progress_percent"))
        reported_resume = max(0, int(row.get("watch_resume_seconds") or 0))
        stored_event = conn.execute(
            """
            SELECT watch_progress_percent, watch_resume_seconds
            FROM history_events
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        stored_progress = bounded_int(stored_event["watch_progress_percent"]) if stored_event else 0
        stored_resume = max(0, int(stored_event["watch_resume_seconds"] or 0)) if stored_event else 0
        progress = (
            stored_progress
            if reported_progress == 0 and stored_progress > 0
            else reported_progress
        )
        resume = (
            stored_resume
            if reported_resume == 0 and stored_resume > 0
            else reported_resume
        )
        if progress != reported_progress:
            progress_guards.append(
                {
                    "video_id": video_id,
                    "reported": reported_progress,
                    "retained": progress,
                }
            )
        if old_ordinal is not None:
            conn.execute(
                """
                UPDATE history_events
                SET video_id=?,
                    watch_date=CASE WHEN watched_at IS NULL THEN ? ELSE watch_date END,
                    time_precision=CASE WHEN watched_at IS NULL THEN ? ELSE 'exact' END,
                    source_type=CASE
                      WHEN my_activity_event_id IS NOT NULL AND takeout_history_key IS NOT NULL
                        THEN 'takeout_my_activity_youtube'
                      WHEN my_activity_event_id IS NOT NULL THEN 'my_activity_youtube'
                      WHEN takeout_history_key IS NOT NULL THEN 'takeout_youtube'
                      ELSE 'youtube'
                    END,
                    match_type=CASE
                      WHEN my_activity_event_id IS NOT NULL AND takeout_history_key IS NOT NULL
                        THEN 'video_id_time_date'
                      WHEN my_activity_event_id IS NOT NULL OR takeout_history_key IS NOT NULL
                        THEN 'video_id_date'
                      ELSE 'youtube_only'
                    END,
                    youtube_ordinal=?, watch_progress_percent=?, watch_resume_seconds=?,
                    observed_at=?, updated_at=?
                WHERE event_id=?
                """,
                (
                    video_id,
                    key[1] or None,
                    "date_only" if key[1] else "unknown",
                    index,
                    progress,
                    resume,
                    observed_at,
                    now,
                    event_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO history_events(
                  event_id, video_id, watched_at, watch_date, time_precision,
                  source_type, match_type, youtube_ordinal,
                  watch_progress_percent, watch_resume_seconds,
                  observed_at, imported_at, updated_at
                )
                VALUES (?, ?, NULL, ?, ?, 'youtube', 'youtube_only', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                  video_id=excluded.video_id,
                  watch_date=excluded.watch_date,
                  time_precision=excluded.time_precision,
                  source_type='youtube',
                  match_type='youtube_only',
                  youtube_ordinal=excluded.youtube_ordinal,
                  watch_progress_percent=excluded.watch_progress_percent,
                  watch_resume_seconds=excluded.watch_resume_seconds,
                  observed_at=excluded.observed_at,
                  updated_at=excluded.updated_at
                """,
                (
                    event_id,
                    video_id,
                    key[1] or None,
                    "date_only" if key[1] else "unknown",
                    index,
                    progress,
                    resume,
                    observed_at,
                    now,
                    now,
                ),
            )
        assignments.append(
            {
                "event_id": event_id,
                "video_id": video_id,
                "watch_date": key[1],
                "old_ordinal": old_ordinal,
                "new_ordinal": index,
            }
        )
    date_conflicts = [
        conflict
        for video_id in dict.fromkeys(
            str(row.get("video_id") or "").strip() for row in rows
        )
        if video_id
        for conflict in history_upload_date_conflicts(conn, video_id, timezone_name)
    ]
    return {
        "new": new,
        "existing": existing,
        "last_video_id": last_video_id,
        "assignments": assignments,
        "progress_guards": progress_guards,
        "date_conflicts": date_conflicts,
    }


def youtube_history_order_shift(
    assignments: list[dict[str, Any]],
    *,
    matching_days: set[str] | None = None,
    fallback: int = 0,
) -> int:
    deltas = Counter(
        int(assignment["new_ordinal"]) - int(assignment["old_ordinal"])
        for assignment in assignments
        if assignment.get("old_ordinal") is not None
        and (not matching_days or assignment.get("watch_date") in matching_days)
    )
    return deltas.most_common(1)[0][0] if deltas else fallback


def synchronize_youtube_history_order(
    conn: sqlite3.Connection,
    occurrence_snapshot: HistoryOccurrenceSnapshot,
    assigned_event_ids: set[str],
    *,
    processed: int,
    shift: int,
    final: bool = False,
    complete_scan: bool = False,
) -> None:
    now = utc_now()
    removed_event_ids: list[str] = []
    cleared_event_ids: list[str] = []
    shifted_events: list[tuple[int, str]] = []
    for occurrences in occurrence_snapshot.values():
        for event_id, old_ordinal in occurrences:
            if event_id in assigned_event_ids:
                continue
            predicted_ordinal = old_ordinal + shift
            remove_observation = complete_scan or (final and predicted_ordinal <= processed)
            if remove_observation:
                removed_event_ids.append(event_id)
            elif predicted_ordinal <= processed:
                cleared_event_ids.append(event_id)
            else:
                shifted_events.append((predicted_ordinal, event_id))
    conn.executemany(
        """
        UPDATE history_events
        SET youtube_ordinal=NULL,
            source_type=CASE
              WHEN my_activity_event_id IS NOT NULL AND takeout_history_key IS NOT NULL
                THEN 'takeout_my_activity'
              WHEN my_activity_event_id IS NOT NULL THEN 'my_activity'
              ELSE 'takeout'
            END,
            match_type=CASE
              WHEN my_activity_event_id IS NOT NULL AND takeout_history_key IS NOT NULL
                THEN 'video_id_time'
              WHEN my_activity_event_id IS NOT NULL THEN 'my_activity_only'
              ELSE 'takeout_only'
            END,
            updated_at=?
        WHERE event_id=?
          AND (takeout_history_key IS NOT NULL OR my_activity_event_id IS NOT NULL)
        """,
        [(now, event_id) for event_id in removed_event_ids],
    )
    conn.executemany(
        """
        DELETE FROM history_events
        WHERE event_id=?
          AND takeout_history_key IS NULL
          AND my_activity_event_id IS NULL
        """,
        [(event_id,) for event_id in removed_event_ids],
    )
    conn.executemany(
        """
        UPDATE history_events
        SET youtube_ordinal=NULL,
            source_type=CASE
              WHEN my_activity_event_id IS NOT NULL AND takeout_history_key IS NOT NULL
                THEN 'takeout_my_activity'
              WHEN my_activity_event_id IS NOT NULL THEN 'my_activity'
              WHEN takeout_history_key IS NOT NULL THEN 'takeout'
              ELSE 'youtube'
            END,
            match_type=CASE
              WHEN my_activity_event_id IS NOT NULL AND takeout_history_key IS NOT NULL
                THEN 'video_id_time'
              WHEN my_activity_event_id IS NOT NULL THEN 'my_activity_only'
              WHEN takeout_history_key IS NOT NULL THEN 'takeout_only'
              ELSE 'youtube_only'
            END,
            updated_at=?
        WHERE event_id=?
        """,
        [(now, event_id) for event_id in cleared_event_ids],
    )
    conn.executemany(
        "UPDATE history_events SET youtube_ordinal=?, updated_at=? WHERE event_id=?",
        [(ordinal, now, event_id) for ordinal, event_id in shifted_events],
    )
    ordered_rows = conn.execute(
        """
        SELECT event_id, youtube_ordinal
        FROM history_events
        WHERE youtube_ordinal IS NOT NULL
        ORDER BY youtube_ordinal, event_id
        """
    ).fetchall()
    conn.executemany(
        "UPDATE history_events SET youtube_ordinal=?, updated_at=? WHERE event_id=?",
        [
            (ordinal, now, row["event_id"])
            for ordinal, row in enumerate(ordered_rows, start=1)
            if row["youtube_ordinal"] != ordinal
        ],
    )


def youtube_takeout_match_count(
    conn: sqlite3.Connection,
    start: int,
    count: int,
) -> int:
    if count <= 0:
        return 0
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM history_events
            WHERE youtube_ordinal >= ?
              AND youtube_ordinal < ?
              AND takeout_history_key IS NOT NULL
            """,
            (start, start + count),
        ).fetchone()[0]
    )


def history_row_hash(row: dict[str, str]) -> str:
    payload = "\x1f".join(
        (row.get("video_id", ""), takeout_watch_datetime(row.get("watched_at", "")))
    )
    return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()


def merge_my_activity_takeout_duplicates(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    activity_by_instant: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in conn.execute(
        """
        SELECT *
        FROM history_events
        WHERE my_activity_event_id IS NOT NULL
          AND COALESCE(watched_at, '') <> ''
        """
    ):
        activity_by_instant.setdefault(
            history_instant_key(row["video_id"], row["watched_at"]),
            [],
        ).append(row)

    takeout_by_instant: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in conn.execute(
        """
        SELECT *
        FROM history_events
        WHERE takeout_history_key IS NOT NULL
          AND my_activity_event_id IS NULL
          AND COALESCE(watched_at, '') <> ''
        """
    ):
        takeout_by_instant.setdefault(
            history_instant_key(row["video_id"], row["watched_at"]),
            [],
        ).append(row)

    now = utc_now()
    merged = 0
    ambiguous = 0
    for instant_key in activity_by_instant.keys() & takeout_by_instant.keys():
        activity_candidates = activity_by_instant[instant_key]
        takeout_candidates = takeout_by_instant[instant_key]
        if len(activity_candidates) != 1 or len(takeout_candidates) != 1:
            ambiguous += 1
            continue
        activity = activity_candidates[0]
        takeout = takeout_candidates[0]
        if (
            activity["takeout_history_key"]
            and activity["takeout_history_key"] != takeout["takeout_history_key"]
        ):
            ambiguous += 1
            continue
        activity_ordinal = activity["youtube_ordinal"]
        takeout_ordinal = takeout["youtube_ordinal"]
        if (
            activity_ordinal is not None
            and takeout_ordinal is not None
            and activity_ordinal != takeout_ordinal
        ):
            ambiguous += 1
            continue
        youtube_ordinal = (
            activity_ordinal if activity_ordinal is not None else takeout_ordinal
        )
        progress_source = (
            activity
            if activity_ordinal is not None or takeout_ordinal is None
            else takeout
        )
        imported_values = [
            value
            for value in (activity["imported_at"], takeout["imported_at"])
            if value
        ]
        source_type = history_source_type_for_identity(
            activity["my_activity_event_id"],
            takeout["takeout_history_key"],
            youtube_ordinal,
        )
        match_type = history_match_type_for_identity(
            activity["my_activity_event_id"],
            takeout["takeout_history_key"],
            youtube_ordinal,
        )
        conn.execute(
            "DELETE FROM history_events WHERE event_id = ?",
            (takeout["event_id"],),
        )
        conn.execute(
            """
            UPDATE history_events
            SET takeout_history_key=?, takeout_row_key=?,
                source_type=?, match_type=?, youtube_ordinal=?,
                watch_progress_percent=?, watch_resume_seconds=?,
                observed_at=COALESCE(?, observed_at), imported_at=?, updated_at=?
            WHERE event_id=?
            """,
            (
                takeout["takeout_history_key"],
                takeout["takeout_row_key"],
                source_type,
                match_type,
                youtube_ordinal,
                progress_source["watch_progress_percent"],
                progress_source["watch_resume_seconds"],
                progress_source["observed_at"],
                min(imported_values) if imported_values else now,
                now,
                activity["event_id"],
            ),
        )
        merged += 1
    return {"merged": merged, "ambiguous": ambiguous}


def rebuild_history_reconciliation(
    conn: sqlite3.Connection,
    timezone_name: str = DEFAULT_DISPLAY_TIMEZONE,
) -> dict[str, int]:
    now = utc_now()
    youtube_rows = conn.execute(
        """
        SELECT *
        FROM history_events
        WHERE youtube_ordinal IS NOT NULL
          AND watched_at IS NULL
        ORDER BY youtube_ordinal
        """
    ).fetchall()
    exact_rows = conn.execute(
        """
        SELECT *
        FROM history_events
        WHERE youtube_ordinal IS NULL
          AND COALESCE(watched_at, '') <> ''
          AND (takeout_history_key IS NOT NULL OR my_activity_event_id IS NOT NULL)
        ORDER BY watched_at DESC, event_id
        """
    ).fetchall()

    exact_by_video_date: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in exact_rows:
        match_date = local_date_for_utc_instant(row["watched_at"], timezone_name)
        exact_by_video_date.setdefault((row["video_id"], match_date), []).append(row)

    matched_exact: set[str] = set()
    matched = 0
    for youtube in youtube_rows:
        key = (youtube["video_id"], youtube["watch_date"])
        if not key[0] or not key[1]:
            continue
        candidates = exact_by_video_date.get(key, [])
        for exact in candidates:
            exact_key = exact["event_id"]
            if exact_key in matched_exact:
                continue
            matched_exact.add(exact_key)
            source_type = history_source_type_for_identity(
                exact["my_activity_event_id"],
                exact["takeout_history_key"],
                youtube["youtube_ordinal"],
            )
            match_type = history_match_type_for_identity(
                exact["my_activity_event_id"],
                exact["takeout_history_key"],
                youtube["youtube_ordinal"],
            )
            conn.execute(
                """
                UPDATE history_events
                SET source_type=?, match_type=?,
                    youtube_ordinal=?, watch_progress_percent=?, watch_resume_seconds=?,
                    observed_at=?, updated_at=?
                WHERE event_id=?
                """,
                (
                    source_type,
                    match_type,
                    youtube["youtube_ordinal"],
                    youtube["watch_progress_percent"],
                    youtube["watch_resume_seconds"],
                    youtube["observed_at"],
                    now,
                    exact["event_id"],
                ),
            )
            conn.execute("DELETE FROM history_events WHERE event_id = ?", (youtube["event_id"],))
            matched += 1
            break
    inserted = conn.execute("SELECT COUNT(*) FROM history_events").fetchone()[0]
    return {
        "rows": inserted,
        "matched": matched,
        "takeout": sum(bool(row["takeout_history_key"]) for row in exact_rows),
        "my_activity": sum(bool(row["my_activity_event_id"]) for row in exact_rows),
        "exact": len(exact_rows),
        "youtube": len(youtube_rows),
    }


def _ensure_playlist_parent(
    conn: sqlite3.Connection,
    playlist_id: str,
    *,
    title: str = "",
    updated_at: str | None = None,
    restore_tombstone: bool = False,
) -> bool:
    playlist_id = playlist_id.strip()
    if not playlist_id:
        raise ValueError("Playlist ID is required")
    if playlist_is_tombstoned(conn, playlist_id):
        if not restore_tombstone:
            return False
        clear_playlist_tombstone(conn, playlist_id)
    conn.execute(
        """
        INSERT INTO playlists(playlist_id, title, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(playlist_id) DO NOTHING
        """,
        (playlist_id, title.strip() or playlist_id, updated_at or utc_now()),
    )
    return True


def _playlist_change_signature(
    conn: sqlite3.Connection,
    playlist_id: str,
) -> tuple[Any, ...] | None:
    playlist = conn.execute(
        """
        SELECT title, description, owner_channel_id, visibility, ownership, video_count
        FROM playlists
        WHERE playlist_id = ?
        """,
        (playlist_id,),
    ).fetchone()
    if not playlist:
        return None
    collaborators = tuple(
        (int(row["position"]), str(row["channel_id"] or ""))
        for row in conn.execute(
            """
            SELECT position, channel_id
            FROM playlist_collaborators
            WHERE playlist_id = ?
            ORDER BY position, channel_id
            """,
            (playlist_id,),
        )
    )
    items = tuple(
        (
            int(row["position"]),
            str(row["video_id"] or ""),
            str(row["membership_state"] or ""),
            str(row["unavailable_kind"] or ""),
        )
        for row in conn.execute(
            """
            SELECT position, video_id, membership_state, unavailable_kind
            FROM playlist_items
            WHERE playlist_id = ?
            ORDER BY position
            """,
            (playlist_id,),
        )
    )
    return (
        tuple(playlist),
        collaborators,
        items,
    )


def _record_playlist_change(
    conn: sqlite3.Connection,
    playlist_id: str,
    changed_at: str,
) -> None:
    conn.execute(
        """
        UPDATE playlists
        SET last_changed_at = CASE
          WHEN last_changed_at IS NULL OR last_changed_at < ? THEN ?
          ELSE last_changed_at
        END
        WHERE playlist_id = ?
        """,
        (changed_at, changed_at, playlist_id),
    )


def _refresh_playlist_known_change_at(
    conn: sqlite3.Connection,
    playlist_id: str,
) -> None:
    row = conn.execute(
        """
        SELECT NULLIF(
          MAX(
            COALESCE(p.created_at, ''),
            COALESCE((
              SELECT MAX(pi.added_at)
              FROM playlist_items pi
              WHERE pi.playlist_id = p.playlist_id
                AND pi.added_at IS NOT NULL
                AND pi.added_at <> ''
            ), '')
          ),
          ''
        ) AS known_change_at
        FROM playlists p
        WHERE p.playlist_id = ?
        """,
        (playlist_id,),
    ).fetchone()
    if row and row["known_change_at"]:
        _record_playlist_change(conn, playlist_id, str(row["known_change_at"]))


def save_playlist_scan(
    conn: sqlite3.Connection,
    playlist_id: str,
    videos: list[dict[str, Any]],
    status: str,
    error: str,
    playlist_metadata: dict[str, Any] | None = None,
) -> tuple[int, int]:
    previous_signature = _playlist_change_signature(conn, playlist_id)
    previous_scan = conn.execute(
        "SELECT scan_status FROM playlist_scans WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchone()
    had_change_baseline = bool(
        previous_scan
        and (
            previous_scan["scan_status"] == "ok"
            or conn.execute(
                "SELECT 1 FROM playlist_items WHERE playlist_id = ? LIMIT 1",
                (playlist_id,),
            ).fetchone()
        )
    )
    normalized_videos: list[dict[str, Any]] = []
    for next_position, video in enumerate(videos, start=1):
        cleaned = dict(video)
        cleaned["position"] = next_position
        normalized_videos.append(cleaned)
    videos = normalized_videos
    unavailable_count = sum(
        1 for video in videos if video_availability_category(video) == "unavailable"
    )
    now = utc_now()
    existing_playlist = conn.execute(
        """
        SELECT ownership, library_missing_at, youtube_updated_date
        FROM playlists
        WHERE playlist_id = ?
        """,
        (playlist_id,),
    ).fetchone()
    if (
        status == "ok"
        and playlist_metadata
        and existing_playlist
        and existing_playlist["library_missing_at"]
        and playlist_ownership_for_owner(
            conn,
            str(playlist_metadata.get("owner_channel_id") or ""),
            str(playlist_metadata.get("owner") or ""),
            existing=str(existing_playlist["ownership"] or "unknown"),
        )
        == "others"
    ):
        delete_playlist_and_orphaned_unwatched_videos(conn, playlist_id)
        return 0, 0
    _ensure_playlist_parent(
        conn,
        playlist_id,
        title=str((playlist_metadata or {}).get("title") or ""),
        updated_at=now,
        restore_tombstone=True,
    )
    retained = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM playlist_items
            WHERE playlist_id = ?
              AND (membership_state = 'retained_unavailable' OR source_quality = 'takeout')
            ORDER BY position
            """,
            (playlist_id,),
        )
    ]
    conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
    for video in videos:
        channel_id = upsert_channel(
            conn,
            video.get("channel_id") or "",
            title=video.get("channel") or "",
            source="playlist",
            updated_at=now,
        )
        video_id = (video.get("video_id") or "").strip()
        if video_id:
            upsert_video(
                conn,
                video_id,
                title=video.get("title") or "",
                channel_id=channel_id,
                channel_title=video.get("channel") or "",
                duration_text=video.get("duration_text") or "",
                is_playable=video.get("is_playable"),
                availability=video.get("availability") or "",
                source="playlist",
                checked_at=now,
                updated_at=now,
            )
        conn.execute(
            """
            INSERT INTO playlist_items(
              playlist_id, position, video_id, membership_state, unavailable_kind,
              source_quality, match_type, match_confidence, added_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'youtube', ?, ?, NULL, ?)
            """,
            (
                playlist_id,
                video["position"],
                video_id or None,
                "current" if video_id else "unresolved_unavailable",
                (video.get("availability") or "unavailable") if not video_id else "",
                "" if video_id else "ambiguous_hidden_slot",
                "video_id" if video_id else "hidden_slot_only",
                now,
            ),
        )
    current_ids = {str(video.get("video_id") or "") for video in videos if video.get("video_id")}
    retained_position = (len(videos) + 1) * 1000
    for item in retained:
        if item.get("video_id") in current_ids:
            continue
        conn.execute(
            """
            INSERT INTO playlist_items(
              playlist_id, position, video_id, membership_state, unavailable_kind,
              source_quality, match_type, match_confidence, added_at, updated_at
            ) VALUES (?, ?, ?, 'retained_unavailable', ?, ?, ?, ?, ?, ?)
            """,
            (
                playlist_id,
                retained_position,
                item.get("video_id"),
                item.get("unavailable_kind") or "unavailable",
                item.get("source_quality") or "takeout",
                item.get("match_type") or "ambiguous_hidden_candidate",
                item.get("match_confidence") or "takeout_missing",
                item.get("added_at"),
                now,
            ),
        )
        retained_position += 1
    conn.execute(
        """
        INSERT INTO playlist_scans(
          playlist_id, scanned_at, video_count, unavailable_count, scan_status, scan_error
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(playlist_id) DO UPDATE SET
          scanned_at=excluded.scanned_at,
          video_count=excluded.video_count,
          unavailable_count=excluded.unavailable_count,
          scan_status=excluded.scan_status,
          scan_error=excluded.scan_error
        """,
        (playlist_id, now, len(videos), unavailable_count, status, error),
    )
    if playlist_metadata:
        metadata = {
            key: str(playlist_metadata.get(key) or "").strip()
            for key in (
                "title",
                "description",
                "owner_channel_id",
                "owner_thumbnail_url",
                "owner_thumbnail_path",
                "visibility",
                "thumbnail_url",
                "thumbnail_path",
                "youtube_updated_date",
            )
        }
        metadata["youtube_updated_date"] = merge_youtube_playlist_updated_date(
            existing_playlist["youtube_updated_date"] if existing_playlist else "",
            metadata["youtube_updated_date"],
            str(playlist_metadata.get("youtube_updated_text") or ""),
        )
        if metadata["owner_channel_id"]:
            metadata["owner_channel_id"] = upsert_channel(
                conn,
                metadata["owner_channel_id"],
                title=str(playlist_metadata.get("owner") or "").strip(),
                thumbnail_url=metadata["owner_thumbnail_url"],
                thumbnail_path=metadata["owner_thumbnail_path"],
                source="playlist_owner",
                updated_at=now,
            )
        ownership = playlist_ownership_for_owner(
            conn,
            metadata["owner_channel_id"],
            str(playlist_metadata.get("owner") or "").strip(),
            existing=(
                str(existing_playlist["ownership"] or "unknown")
                if existing_playlist
                else "unknown"
            ),
        )
        if playlist_metadata.get("collaborators_authoritative"):
            collaborators = playlist_metadata.get("collaborators") or []
            conn.execute(
                "DELETE FROM playlist_collaborators WHERE playlist_id = ?",
                (playlist_id,),
            )
            seen_collaborator_ids: set[str] = set()
            for position, collaborator in enumerate(collaborators):
                if not isinstance(collaborator, dict):
                    continue
                channel_id = str(collaborator.get("channel_id") or "").strip()
                if (
                    not channel_id
                    or channel_id == metadata["owner_channel_id"]
                    or channel_id in seen_collaborator_ids
                ):
                    continue
                seen_collaborator_ids.add(channel_id)
                channel_id = upsert_channel(
                    conn,
                    channel_id,
                    title=str(collaborator.get("title") or "").strip(),
                    thumbnail_url=str(collaborator.get("thumbnail_url") or "").strip(),
                    thumbnail_path=str(collaborator.get("thumbnail_path") or "").strip(),
                    source="playlist_collaborator",
                    updated_at=now,
                )
                conn.execute(
                    """
                    INSERT INTO playlist_collaborators(playlist_id, channel_id, position)
                    VALUES (?, ?, ?)
                    """,
                    (playlist_id, channel_id, position),
                )
        metadata["video_count"] = max(0, int(playlist_metadata.get("video_count") or 0))
        conn.execute(
            """
            INSERT INTO playlists(
              playlist_id, title, description, owner_channel_id, visibility,
              youtube_updated_date, ownership, video_count, thumbnail_url,
              thumbnail_path, fetch_status, fetch_error, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(playlist_id) DO UPDATE SET
              title=COALESCE(NULLIF(excluded.title, ''), playlists.title),
              description=COALESCE(NULLIF(excluded.description, ''), playlists.description),
              youtube_updated_date=COALESCE(
                NULLIF(excluded.youtube_updated_date, ''),
                playlists.youtube_updated_date
              ),
              owner_channel_id=COALESCE(excluded.owner_channel_id, playlists.owner_channel_id),
              visibility=COALESCE(NULLIF(excluded.visibility, ''), playlists.visibility),
              ownership=CASE
                WHEN excluded.ownership <> 'unknown' THEN excluded.ownership
                ELSE playlists.ownership
              END,
              video_count=CASE
                WHEN excluded.video_count > 0 THEN excluded.video_count
                ELSE playlists.video_count
              END,
              thumbnail_url=COALESCE(NULLIF(excluded.thumbnail_url, ''), playlists.thumbnail_url),
              thumbnail_path=COALESCE(NULLIF(excluded.thumbnail_path, ''), playlists.thumbnail_path),
              fetch_status=excluded.fetch_status,
              fetch_error=excluded.fetch_error,
              library_missing_at=NULL,
              updated_at=excluded.updated_at
            """,
            (
                playlist_id,
                metadata["title"],
                metadata["description"],
                metadata["owner_channel_id"] or None,
                metadata["visibility"],
                metadata["youtube_updated_date"] or None,
                ownership,
                metadata["video_count"],
                metadata["thumbnail_url"],
                metadata["thumbnail_path"],
                status,
                error,
                now,
            ),
        )
        if status == "ok":
            conn.execute(
                """
                UPDATE playlists
                SET metadata_checked_at = ?
                WHERE playlist_id = ?
                """,
                (now, playlist_id),
            )
    if (
        status == "ok"
        and had_change_baseline
        and previous_signature is not None
        and previous_signature != _playlist_change_signature(conn, playlist_id)
    ):
        _record_playlist_change(conn, playlist_id, now)
    return len(videos), unavailable_count


def save_playlist_metadata_observation(
    conn: sqlite3.Connection,
    playlist_id: str,
    playlist_metadata: dict[str, Any],
) -> bool:
    existing_playlist = conn.execute(
        "SELECT ownership, youtube_updated_date FROM playlists WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchone()
    if not existing_playlist:
        return False
    now = utc_now()
    metadata = {
        key: str(playlist_metadata.get(key) or "").strip()
        for key in (
            "title",
            "description",
            "owner_channel_id",
            "owner_thumbnail_url",
            "owner_thumbnail_path",
            "visibility",
            "thumbnail_url",
            "thumbnail_path",
            "youtube_updated_date",
        )
    }
    metadata["youtube_updated_date"] = merge_youtube_playlist_updated_date(
        existing_playlist["youtube_updated_date"],
        metadata["youtube_updated_date"],
        str(playlist_metadata.get("youtube_updated_text") or ""),
    )
    if metadata["owner_channel_id"]:
        metadata["owner_channel_id"] = upsert_channel(
            conn,
            metadata["owner_channel_id"],
            title=str(playlist_metadata.get("owner") or "").strip(),
            thumbnail_url=metadata["owner_thumbnail_url"],
            thumbnail_path=metadata["owner_thumbnail_path"],
            source="playlist_owner",
            updated_at=now,
        )
    ownership = playlist_ownership_for_owner(
        conn,
        metadata["owner_channel_id"],
        str(playlist_metadata.get("owner") or "").strip(),
        existing=str(existing_playlist["ownership"] or "unknown"),
    )
    if playlist_metadata.get("collaborators_authoritative"):
        collaborators = playlist_metadata.get("collaborators") or []
        conn.execute(
            "DELETE FROM playlist_collaborators WHERE playlist_id = ?",
            (playlist_id,),
        )
        seen_collaborator_ids: set[str] = set()
        for position, collaborator in enumerate(collaborators):
            if not isinstance(collaborator, dict):
                continue
            channel_id = str(collaborator.get("channel_id") or "").strip()
            if (
                not channel_id
                or channel_id == metadata["owner_channel_id"]
                or channel_id in seen_collaborator_ids
            ):
                continue
            seen_collaborator_ids.add(channel_id)
            channel_id = upsert_channel(
                conn,
                channel_id,
                title=str(collaborator.get("title") or "").strip(),
                thumbnail_url=str(collaborator.get("thumbnail_url") or "").strip(),
                thumbnail_path=str(collaborator.get("thumbnail_path") or "").strip(),
                source="playlist_collaborator",
                updated_at=now,
            )
            conn.execute(
                """
                INSERT INTO playlist_collaborators(playlist_id, channel_id, position)
                VALUES (?, ?, ?)
                """,
                (playlist_id, channel_id, position),
            )
    video_count = max(0, int(playlist_metadata.get("video_count") or 0))
    has_video_count = bool(playlist_metadata.get("has_video_count"))
    cursor = conn.execute(
        """
        UPDATE playlists
        SET title=COALESCE(NULLIF(?, ''), title),
            description=COALESCE(NULLIF(?, ''), description),
            youtube_updated_date=COALESCE(NULLIF(?, ''), youtube_updated_date),
            owner_channel_id=COALESCE(?, owner_channel_id),
            visibility=COALESCE(NULLIF(?, ''), visibility),
            ownership=CASE WHEN ? <> 'unknown' THEN ? ELSE ownership END,
            video_count=CASE WHEN ? THEN ? ELSE video_count END,
            thumbnail_url=COALESCE(NULLIF(?, ''), thumbnail_url),
            thumbnail_path=COALESCE(NULLIF(?, ''), thumbnail_path),
            metadata_checked_at=?,
            fetch_status='ok',
            fetch_error='',
            updated_at=?
        WHERE playlist_id = ?
        """,
        (
            metadata["title"],
            metadata["description"],
            metadata["youtube_updated_date"],
            metadata["owner_channel_id"] or None,
            metadata["visibility"],
            ownership,
            ownership,
            1 if has_video_count else 0,
            video_count,
            metadata["thumbnail_url"],
            metadata["thumbnail_path"],
            now,
            now,
            playlist_id,
        ),
    )
    return bool(cursor.rowcount)


def save_liked_video_reactions(
    conn: sqlite3.Connection,
    videos: list[dict[str, Any]],
    *,
    replace: bool = True,
) -> tuple[int, int]:
    deduped: list[dict[str, Any]] = []
    seen_video_ids: set[str] = set()
    unavailable_count = 0
    for video in videos:
        video_id = str(video.get("video_id") or "").strip()
        if not video_id or video_id in seen_video_ids:
            if not video_id:
                unavailable_count += 1
            continue
        seen_video_ids.add(video_id)
        deduped.append(video)
        if video_availability_category(video) == "unavailable":
            unavailable_count += 1

    now = utc_now()
    if replace:
        conn.execute(
            "UPDATE videos SET reaction = '', updated_at = ? WHERE reaction = 'LIKE'",
            (now,),
        )
    for video in deduped:
        video_id = str(video.get("video_id") or "").strip()
        channel_id = upsert_channel(
            conn,
            str(video.get("channel_id") or ""),
            title=str(video.get("channel") or ""),
            source="playlist",
            updated_at=now,
        )
        upsert_video(
            conn,
            video_id,
            title=str(video.get("title") or ""),
            channel_id=channel_id,
            channel_title=str(video.get("channel") or ""),
            duration_text=str(video.get("duration_text") or ""),
            is_playable=video.get("is_playable"),
            availability=str(video.get("availability") or ""),
            source="playlist",
            checked_at=now,
            updated_at=now,
        )
        conn.execute(
            "UPDATE videos SET reaction = 'LIKE', updated_at = ? WHERE video_id = ?",
            (now, video_id),
        )
    return len(deduped), unavailable_count


def save_playlist_scan_error(
    conn: sqlite3.Connection,
    playlist_id: str,
    error: str,
) -> tuple[int, int]:
    now = utc_now()
    previous = conn.execute(
        """
        SELECT video_count, unavailable_count
        FROM playlist_scans
        WHERE playlist_id = ?
        """,
        (playlist_id,),
    ).fetchone()
    video_count = int(previous["video_count"] or 0) if previous else 0
    unavailable_count = int(previous["unavailable_count"] or 0) if previous else 0
    if not _ensure_playlist_parent(conn, playlist_id, updated_at=now):
        return video_count, unavailable_count
    conn.execute(
        """
        INSERT INTO playlist_scans(
          playlist_id, scanned_at, video_count, unavailable_count, scan_status, scan_error
        )
        VALUES (?, ?, ?, ?, 'error', ?)
        ON CONFLICT(playlist_id) DO UPDATE SET
          scanned_at=excluded.scanned_at,
          video_count=playlist_scans.video_count,
          unavailable_count=playlist_scans.unavailable_count,
          scan_status=excluded.scan_status,
          scan_error=excluded.scan_error
        """,
        (playlist_id, now, video_count, unavailable_count, error),
    )
    return video_count, unavailable_count


def playlist_missing_status(conn: sqlite3.Connection, playlist_id: str) -> str:
    row = conn.execute(
        """
        SELECT ownership
        FROM playlists
        WHERE playlist_id = ?
        """,
        (playlist_id,),
    ).fetchone()
    if not row:
        return "removed" if playlist_is_tombstoned(conn, playlist_id) else "unavailable"
    return "removed" if row["ownership"] == "mine" else "unavailable"


def save_playlist_missing_status(
    conn: sqlite3.Connection,
    playlist_id: str,
    status: str,
    error: str,
) -> tuple[int, int]:
    if status not in {"removed", "unavailable"}:
        raise ValueError(f"Unsupported playlist missing status: {status}")
    now = utc_now()
    previous = conn.execute(
        """
        SELECT video_count, unavailable_count
        FROM playlist_scans
        WHERE playlist_id = ?
        """,
        (playlist_id,),
    ).fetchone()
    video_count = int(previous["video_count"] or 0) if previous else 0
    unavailable_count = int(previous["unavailable_count"] or 0) if previous else 0
    if status == "removed":
        tombstone_playlist(
            conn,
            playlist_id,
            reason="authenticated_missing",
            observed_at=now,
        )
        return video_count, unavailable_count
    if not _ensure_playlist_parent(conn, playlist_id, updated_at=now):
        conn.execute(
            "UPDATE playlist_tombstones SET last_confirmed_at=? WHERE playlist_id=?",
            (now, playlist_id),
        )
        return video_count, unavailable_count
    conn.execute(
        """
        UPDATE playlists
        SET in_library=0, library_missing_at=NULL,
            fetch_status = ?, fetch_error = ?, updated_at = ?
        WHERE playlist_id = ?
        """,
        (status, error, now, playlist_id),
    )
    conn.execute(
        """
        INSERT INTO playlist_scans(
          playlist_id, scanned_at, video_count, unavailable_count, scan_status, scan_error
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(playlist_id) DO UPDATE SET
          scanned_at=excluded.scanned_at,
          video_count=playlist_scans.video_count,
          unavailable_count=playlist_scans.unavailable_count,
          scan_status=excluded.scan_status,
          scan_error=excluded.scan_error
        """,
        (playlist_id, now, video_count, unavailable_count, status, error),
    )
    return video_count, unavailable_count


def rebuild_playlist_reconciliation(
    conn: sqlite3.Connection,
    playlist_id: str | None = None,
) -> dict[str, int]:
    where = "WHERE playlist_id = ?" if playlist_id else ""
    params: tuple[Any, ...] = (playlist_id,) if playlist_id else ()
    rows = conn.execute(
        f"SELECT membership_state, COUNT(*) AS count FROM playlist_items {where} GROUP BY membership_state",
        params,
    ).fetchall()
    counts = {row["membership_state"]: int(row["count"] or 0) for row in rows}
    playlist_count = conn.execute(
        f"SELECT COUNT(DISTINCT playlist_id) FROM playlist_items {where}",
        params,
    ).fetchone()[0]
    return {
        "playlists": int(playlist_count or 0),
        "rows": sum(counts.values()),
        "inferred": 0,
        "ambiguous": counts.get("retained_unavailable", 0) + counts.get("unresolved_unavailable", 0),
    }


def playlist_scan_candidate_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
    force: bool = False,
) -> list[sqlite3.Row]:
    where = ["p.playlist_id <> ''"]
    params: list[Any] = []
    if not force:
        where.append(
            """
            COALESCE(ps.scan_status, '') NOT IN ('removed', 'unavailable')
            AND
            (
              ps.playlist_id IS NULL
              OR ps.scan_status <> 'ok'
              OR (p.video_count > 0 AND p.video_count <> COALESCE(ps.video_count, -1))
            )
            """
        )
    sql = f"""
        SELECT p.playlist_id,
               p.title,
               p.video_count AS playlist_video_count,
               COALESCE(ps.scanned_at, '') AS scanned_at,
               COALESCE(ps.scan_status, '') AS scan_status,
               COALESCE(ps.video_count, 0) AS video_count,
               COALESCE(ps.unavailable_count, 0) AS unavailable_count
        FROM playlists p
        LEFT JOIN playlist_scans ps ON ps.playlist_id = p.playlist_id
        WHERE {" AND ".join(where)}
        ORDER BY
          CASE WHEN ps.playlist_id IS NULL THEN 0 ELSE 1 END,
          COALESCE(ps.scanned_at, ''),
          p.title COLLATE NOCASE
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(max(0, offset))
    return conn.execute(sql, params).fetchall()


def worker_queue_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
) -> list[sqlite3.Row]:
    sql = f"""
        SELECT w.queue_id,
               w.subject_key,
               w.worker_type,
               w.task_type,
               w.video_id,
               w.clip_id,
               w.channel_id,
               w.playlist_id,
               w.channel_title,
               w.current_title,
               w.source_key,
               w.playlist_count,
               w.priority,
               w.manual,
               w.plugin_subject_id,
               w.payload_json,
               w.created_at,
               w.updated_at,
               p.title AS playlist_title,
               COALESCE(NULLIF(c.title, ''), '') AS known_channel_title,
               COALESCE(NULLIF(clip.title, ''), '') AS known_clip_title,
               p.video_count AS playlist_video_count,
               COALESCE(ps.scan_status, '') AS scan_status,
               COALESCE(ps.video_count, 0) AS video_count,
               COALESCE(ps.unavailable_count, 0) AS unavailable_count
        FROM worker_queue w
        LEFT JOIN playlists p ON p.playlist_id = w.playlist_id
        LEFT JOIN playlist_scans ps ON ps.playlist_id = w.playlist_id
        LEFT JOIN channels c
          ON c.channel_id = CASE
            WHEN w.worker_type = 'metadata' AND w.task_type = 'channel'
              THEN COALESCE(NULLIF(w.channel_id, ''), w.video_id)
            ELSE ''
          END
        LEFT JOIN clips clip ON clip.clip_id = w.clip_id
        ORDER BY {worker_queue_order_sql("w")}
    """
    params: list[Any] = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(max(0, offset))
    return conn.execute(sql, params).fetchall()


def worker_queue_rows_by_id(
    conn: sqlite3.Connection,
    queue_ids: Sequence[int],
) -> list[sqlite3.Row]:
    ids = sorted({int(queue_id) for queue_id in queue_ids if int(queue_id) > 0})
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return conn.execute(
        f"""
        SELECT w.queue_id,
               w.subject_key,
               w.worker_type,
               w.task_type,
               w.video_id,
               w.clip_id,
               w.channel_id,
               w.playlist_id,
               w.channel_title,
               w.current_title,
               w.source_key,
               w.playlist_count,
               w.priority,
               w.manual,
               w.plugin_subject_id,
               w.payload_json,
               w.created_at,
               w.updated_at,
               p.title AS playlist_title,
               COALESCE(NULLIF(c.title, ''), '') AS known_channel_title,
               COALESCE(NULLIF(clip.title, ''), '') AS known_clip_title,
               p.video_count AS playlist_video_count,
               COALESCE(ps.scan_status, '') AS scan_status,
               COALESCE(ps.video_count, 0) AS video_count,
               COALESCE(ps.unavailable_count, 0) AS unavailable_count
        FROM worker_queue w
        LEFT JOIN playlists p ON p.playlist_id = w.playlist_id
        LEFT JOIN playlist_scans ps ON ps.playlist_id = w.playlist_id
        LEFT JOIN channels c
          ON c.channel_id = CASE
            WHEN w.worker_type = 'metadata' AND w.task_type = 'channel'
              THEN COALESCE(NULLIF(w.channel_id, ''), w.video_id)
            ELSE ''
          END
        LEFT JOIN clips clip ON clip.clip_id = w.clip_id
        WHERE w.queue_id IN ({placeholders})
        ORDER BY {worker_queue_order_sql("w")}
        """,
        ids,
    ).fetchall()


def worker_queue_event_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(event_id), 0) AS event_id FROM worker_queue_events").fetchone()
    return int(row["event_id"] or 0)


def worker_queue_events_after(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    limit: int = 500,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT event_id, queue_id, operation, created_at
        FROM worker_queue_events
        WHERE event_id > ?
        ORDER BY event_id
        LIMIT ?
        """,
        (max(0, int(event_id)), max(1, min(5000, int(limit)))),
    ).fetchall()


_WORKER_LOG_TABLES = {
    "metadataLogs": "metadata_worker_log",
    "playlistScanLogs": "playlist_scan_worker_log",
    "liveHistoryLogs": "live_history_worker_log",
    "placeholderRecoveryLogs": "placeholder_recovery_worker_log",
    "pluginWorkerLogs": "plugin_worker_log",
}


def worker_log_select(name: str) -> str:
    table = _WORKER_LOG_TABLES[name]
    if name == "pluginWorkerLogs":
        return f"""
            SELECT l.*,
                   COALESCE(NULLIF(v.title, ''), l.subject_id) AS subject_title,
                   CASE WHEN v.video_id IS NULL THEN '' ELSE l.subject_id END AS display_id
            FROM {table} l
            LEFT JOIN videos v ON v.video_id = l.subject_id
        """
    if name == "playlistScanLogs":
        return f"""
            SELECT l.*,
                   COALESCE(NULLIF(p.title, ''), l.playlist_id) AS subject_title,
                   l.playlist_id AS display_id
            FROM {table} l
            LEFT JOIN playlists p ON p.playlist_id = l.playlist_id
        """
    if name == "placeholderRecoveryLogs":
        return f"""
            SELECT l.*,
                   COALESCE(NULLIF(v.title, ''), '') AS subject_title,
                   l.video_id AS display_id
            FROM {table} l
            LEFT JOIN videos v ON v.video_id = l.video_id
        """
    if name == "metadataLogs":
        identifier_condition = """
            v.video_id IS NOT NULL
            OR clip.clip_id IS NOT NULL
            OR c.channel_id IS NOT NULL
            OR legacy_c.channel_id IS NOT NULL
            OR (
              LENGTH(l.video_id) = 11
              AND l.video_id NOT GLOB '*[^A-Za-z0-9_-]*'
            )
            OR l.video_id LIKE 'UC%'
            OR l.video_id LIKE 'HC%'
            OR l.video_id LIKE '@%'
        """
        return f"""
            SELECT l.*,
                   CASE
                     WHEN v.video_id IS NOT NULL
                       THEN COALESCE(NULLIF(v.title, ''), '')
                     WHEN clip.clip_id IS NOT NULL
                       THEN COALESCE(NULLIF(clip.title, ''), '')
                     WHEN c.channel_id IS NOT NULL
                       THEN COALESCE(NULLIF(c.title, ''), l.video_id)
                     WHEN legacy_c.channel_id IS NOT NULL
                       THEN COALESCE(NULLIF(legacy_c.title, ''), l.video_id)
                     WHEN {identifier_condition}
                       THEN ''
                     ELSE l.video_id
                   END AS subject_title,
                   CASE
                     WHEN legacy_c.channel_id IS NOT NULL THEN legacy_c.channel_id
                     WHEN {identifier_condition} THEN l.video_id
                     ELSE ''
                   END AS display_id
            FROM {table} l
            LEFT JOIN videos v ON v.video_id = l.video_id
            LEFT JOIN clips clip ON clip.clip_id = l.video_id
            LEFT JOIN channels c ON c.channel_id = l.video_id
            LEFT JOIN channels legacy_c
              ON legacy_c.channel_id = (
                SELECT c2.channel_id
                FROM channels c2
                WHERE (l.level = 'channel' OR l.level LIKE 'channel %')
                  AND c2.title = l.video_id COLLATE NOCASE
                ORDER BY c2.channel_id
                LIMIT 1
              )
        """
    return f"""
        SELECT l.*,
               COALESCE(NULLIF(v.title, ''), '') AS subject_title,
               CASE WHEN v.video_id IS NULL THEN '' ELSE l.video_id END AS display_id
        FROM {table} l
        LEFT JOIN videos v ON v.video_id = l.video_id
    """


def worker_log_cursors(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        name: int(
            conn.execute(f"SELECT COALESCE(MAX(id), 0) AS id FROM {table}").fetchone()["id"] or 0
        )
        for name, table in _WORKER_LOG_TABLES.items()
    }


def worker_log_snapshot(conn: sqlite3.Connection, *, limit: int = 80) -> dict[str, list[sqlite3.Row]]:
    row_limit = max(1, min(500, int(limit)))
    return {
        name: conn.execute(
            worker_log_select(name) + " ORDER BY l.id DESC LIMIT ?",
            (row_limit,),
        ).fetchall()
        for name in _WORKER_LOG_TABLES
    }


def worker_logs_after(
    conn: sqlite3.Connection,
    cursors: dict[str, int],
    *,
    limit: int = 500,
) -> dict[str, list[sqlite3.Row]]:
    row_limit = max(1, min(5000, int(limit)))
    return {
        name: conn.execute(
            worker_log_select(name) + " WHERE l.id > ? ORDER BY l.id LIMIT ?",
            (max(0, int(cursors.get(name, 0))), row_limit),
        ).fetchall()
        for name in _WORKER_LOG_TABLES
    }


def _worker_log_source(name: str, level: str) -> str:
    normalized_level = str(level or "").lower()
    if name == "metadataLogs":
        if normalized_level.startswith("queue "):
            return "queue"
        if normalized_level.startswith("placeholder "):
            return "placeholder"
        if normalized_level.startswith("clip "):
            return "clip"
        return "metadata"
    if name == "pluginWorkerLogs":
        raise ValueError("Plugin log sources require the log row")
    return {
        "playlistScanLogs": "playlist",
        "liveHistoryLogs": "history",
        "placeholderRecoveryLogs": "placeholder",
    }[name]


def _worker_log_display_level(name: str, level: str) -> str:
    value = str(level or "")
    if name == "metadataLogs":
        lowered = value.lower()
        if lowered.startswith("queue "):
            return value[6:]
        if lowered.startswith("placeholder "):
            return value[12:]
        if lowered.startswith("clip "):
            return value[5:]
    return value


def _worker_log_severity(level: str) -> str:
    tokens = str(level or "").strip().lower().split()
    if "error" in tokens:
        return "error"
    if "warn" in tokens or "warning" in tokens:
        return "warn"
    if "debug" in tokens:
        return "debug"
    return "info"


def _worker_log_count(
    conn: sqlite3.Connection,
    name: str,
    table: str,
    source: str,
    severity: str,
) -> int:
    params: list[str] = []
    where_sql = ""
    if name == "pluginWorkerLogs" and source.startswith("plugin:"):
        where_sql = " WHERE plugin_id = ?"
        params.append(source.removeprefix("plugin:"))

    rows = conn.execute(
        f"SELECT level, COUNT(*) AS count FROM {table}{where_sql} GROUP BY level",
        params,
    ).fetchall()
    severity_ranks = {"info": 0, "warn": 1, "error": 2, "debug": 3}
    selected_rank = severity_ranks.get(severity, severity_ranks["debug"])
    total = 0
    for row in rows:
        level = str(row["level"] or "")
        if name == "metadataLogs" and source and _worker_log_source(name, level) != source:
            continue
        if severity_ranks[_worker_log_severity(level)] > selected_rank:
            continue
        total += int(row["count"] or 0)
    return total


def _worker_log_filter_sql(
    name: str,
    source: str,
    severity: str,
) -> tuple[str, list[str]] | None:
    clauses: list[str] = []
    params: list[str] = []
    fixed_source = {
        "playlistScanLogs": "playlist",
        "liveHistoryLogs": "history",
        "placeholderRecoveryLogs": "placeholder",
    }.get(name)
    if name == "pluginWorkerLogs":
        if source.startswith("plugin:"):
            clauses.append("l.plugin_id = ?")
            params.append(source.removeprefix("plugin:"))
        elif source:
            return None
    elif fixed_source:
        if source and source != fixed_source:
            return None
    elif source == "queue":
        clauses.append("LOWER(l.level) LIKE 'queue %'")
    elif source == "placeholder":
        clauses.append("LOWER(l.level) LIKE 'placeholder %'")
    elif source == "metadata":
        clauses.append(
            "LOWER(l.level) NOT LIKE 'queue %' AND LOWER(l.level) NOT LIKE 'placeholder %' "
            "AND LOWER(l.level) NOT LIKE 'clip %'"
        )
    elif source == "clip":
        clauses.append("LOWER(l.level) LIKE 'clip %'")
    elif source:
        return None

    padded_level = "(' ' || LOWER(TRIM(l.level)) || ' ')"
    if severity == "info":
        clauses.append(
            f"INSTR({padded_level}, ' error ') = 0 AND "
            f"INSTR({padded_level}, ' warn ') = 0 AND "
            f"INSTR({padded_level}, ' warning ') = 0 AND "
            f"INSTR({padded_level}, ' debug ') = 0"
        )
    elif severity == "warn":
        clauses.append(
            f"INSTR({padded_level}, ' error ') = 0 AND "
            f"INSTR({padded_level}, ' debug ') = 0"
        )
    elif severity == "error":
        clauses.append(f"INSTR({padded_level}, ' debug ') = 0")
    return (" AND ".join(clauses) if clauses else "1 = 1", params)


def _normalized_worker_log(name: str, row: sqlite3.Row) -> dict[str, Any]:
    source = (
        f"plugin:{row['plugin_id']}"
        if name == "pluginWorkerLogs"
        else _worker_log_source(name, row["level"])
    )
    identifier = row["display_id"] or ""
    subject = row["subject_title"] or ""
    if not subject:
        if name == "metadataLogs":
            subject = "" if identifier else (row["video_id"] or "")
        elif name == "liveHistoryLogs":
            subject = row["video_id"] or ""
        elif name == "placeholderRecoveryLogs":
            subject = ""
        elif name == "playlistScanLogs":
            subject = row["playlist_id"] or ""
        elif name == "pluginWorkerLogs":
            subject = row["subject_id"] or ""
    return {
        "stream": name,
        "id": int(row["id"]),
        "created_at": row["created_at"],
        "source": source,
        "level": _worker_log_display_level(name, row["level"]),
        "identifier": identifier,
        "subject_id": subject,
        "message": row["message"],
    }


def worker_log_page(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    offset: int = 0,
    source: str = "",
    severity: str = "",
) -> tuple[list[dict[str, Any]], int]:
    row_limit = max(1, min(200, int(limit)))
    row_offset = max(0, int(offset))
    source_filter = str(source or "").strip().lower()
    severity_filter = str(severity or "").strip().lower()
    if source_filter not in {"", "queue", "metadata", "clip", "playlist", "history", "placeholder"} and not re.fullmatch(
        r"plugin:[a-z][a-z0-9_-]*", source_filter
    ):
        raise ValueError(f"Unknown log source: {source}")
    if severity_filter not in {"", "info", "warn", "error", "debug"}:
        raise ValueError(f"Unknown log severity: {severity}")

    candidates: list[dict[str, Any]] = []
    total = 0
    candidate_limit = row_offset + row_limit
    for name, table in _WORKER_LOG_TABLES.items():
        filter_result = _worker_log_filter_sql(name, source_filter, severity_filter)
        if filter_result is None:
            continue
        where_sql, params = filter_result
        total += _worker_log_count(
            conn,
            name,
            table,
            source_filter,
            severity_filter,
        )
        rows = conn.execute(
            worker_log_select(name)
            + f" WHERE {where_sql} ORDER BY l.created_at DESC, l.id DESC LIMIT ?",
            [*params, candidate_limit],
        ).fetchall()
        candidates.extend(_normalized_worker_log(name, row) for row in rows)

    candidates.sort(
        key=lambda row: (
            str(row["created_at"] or ""),
            str(row["stream"]),
            int(row["id"]),
        ),
        reverse=True,
    )
    return candidates[row_offset : row_offset + row_limit], total


def worker_queue_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM worker_queue").fetchone()
    return int(row["count"] or 0)


def worker_queue_type_count(conn: sqlite3.Connection, worker_type: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM worker_queue WHERE worker_type = ?",
        ((worker_type or "").strip(),),
    ).fetchone()
    return int(row["count"] or 0)


def _admin_runtime_status_from_connection(
    conn: sqlite3.Connection,
    queue_dispatcher: "WorkerQueueDispatcher | None" = None,
) -> dict[str, Any]:
    queue_count = worker_queue_count(conn)
    queue_running = queue_dispatcher.is_running() if queue_dispatcher else False
    queue_stopping = queue_dispatcher.is_stopping() if queue_dispatcher else False
    queue_stats = (
        queue_dispatcher.stats(queue_count)
        if queue_dispatcher
        else {
            "started_at": "",
            "elapsed_seconds": 0,
            "eta_seconds": 0,
            "eta_available": False,
            "initial_count": 0,
            "completed_count": 0,
            "remaining_count": queue_count,
        }
    )
    return {
        "workerQueueRunning": queue_running,
        "workerQueueStopping": queue_stopping,
        "workerQueueStats": queue_stats,
        "workerQueueCount": queue_count,
        "cookieAuthStatuses": cookie_auth_statuses(conn),
        "archivarixBlock": external_service_block(conn, "archivarix"),
        "proxyBlock": external_service_block(conn, "proxy"),
    }


def admin_runtime_status(
    db_path: Path,
    queue_dispatcher: "WorkerQueueDispatcher | None" = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        return _admin_runtime_status_from_connection(conn, queue_dispatcher)
    finally:
        conn.close()


def external_service_block(conn: sqlite3.Connection, service: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM external_service_blocks WHERE service = ?",
        (service,),
    ).fetchone()
    if not row:
        return {
            "service": service,
            "blocked": False,
            "reason_code": "",
            "message": "",
            "blocked_at": "",
            "retry_after": "",
            "run_id": "",
            "queue_id": 0,
            "retry_eligible": False,
            "manual_retry_required": False,
        }
    return {
        **dict(row),
        "blocked": True,
        "retry_eligible": True,
        "manual_retry_required": not bool(row["retry_after"]),
    }


COOKIE_AUTH_SERVICES = ("youtube", "google", "archivarix")
COOKIE_AUTH_STATES = {"valid", "expired", "rejected", "missing", "error"}
COOKIE_AUTH_SERVICE_LABELS = {
    "youtube": "YouTube",
    "google": "Google My Activity",
    "archivarix": "Archivarix",
}


def cookie_auth_statuses(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    statuses = {
        service: {
            "service": service,
            "status": "unknown",
            "checked_at": "",
            "message": "",
        }
        for service in COOKIE_AUTH_SERVICES
    }
    for row in conn.execute("SELECT * FROM cookie_auth_status"):
        service = str(row["service"] or "")
        if service in statuses:
            statuses[service] = dict(row)
    return statuses


def record_cookie_auth_status(
    conn: sqlite3.Connection,
    service: str,
    status: str,
    message: str = "",
    *,
    checked_at: str | None = None,
) -> None:
    service = (service or "").strip().lower()
    status = (status or "").strip().lower()
    if service not in COOKIE_AUTH_SERVICES:
        raise ValueError(f"Unknown cookie service: {service}")
    if status not in COOKIE_AUTH_STATES:
        raise ValueError(f"Unknown cookie authentication status: {status}")
    previous = conn.execute(
        "SELECT status FROM cookie_auth_status WHERE service = ?",
        (service,),
    ).fetchone()
    previous_status = str(previous["status"] or "") if previous else ""
    conn.execute(
        """
        INSERT INTO cookie_auth_status(service, status, checked_at, message)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(service) DO UPDATE SET
          status = excluded.status,
          checked_at = excluded.checked_at,
          message = excluded.message
        """,
        (service, status, checked_at or utc_now(), str(message or "")),
    )
    if status != "valid" and status != previous_status:
        detail = str(message or "").strip()
        log_worker_queue_event(
            conn,
            "warn",
            (
                f"{COOKIE_AUTH_SERVICE_LABELS[service]} cookie status changed to "
                f"{status}"
                + (f": {detail}" if detail else "")
            ),
        )


def clear_cookie_auth_status(conn: sqlite3.Connection, service: str) -> bool:
    service = (service or "").strip().lower()
    if service not in COOKIE_AUTH_SERVICES:
        raise ValueError(f"Unknown cookie service: {service}")
    return bool(
        conn.execute(
            "DELETE FROM cookie_auth_status WHERE service = ?",
            (service,),
        ).rowcount
    )


def cookie_auth_failure_status(message: str) -> str:
    normalized = str(message or "").casefold()
    if "expired" in normalized:
        return "expired"
    if "missing" in normalized:
        return "missing"
    if "could not be read" in normalized:
        return "error"
    return "rejected"


def set_external_service_block(
    conn: sqlite3.Connection,
    service: str,
    reason_code: str,
    message: str,
    *,
    run_id: str = "",
    queue_id: int = 0,
    retry_after: str = "",
) -> dict[str, Any]:
    conn.execute(
        """
        INSERT INTO external_service_blocks(
          service, reason_code, message, blocked_at, retry_after, run_id, queue_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(service) DO UPDATE SET
          reason_code = excluded.reason_code,
          message = excluded.message,
          blocked_at = excluded.blocked_at,
          retry_after = excluded.retry_after,
          run_id = excluded.run_id,
          queue_id = excluded.queue_id
        """,
        (service, reason_code, message, utc_now(), retry_after, run_id, queue_id),
    )
    return external_service_block(conn, service)


def clear_external_service_block(conn: sqlite3.Connection, service: str) -> bool:
    cursor = conn.execute("DELETE FROM external_service_blocks WHERE service = ?", (service,))
    return cursor.rowcount > 0


def clear_worker_queue_type(conn: sqlite3.Connection, worker_type: str) -> int:
    worker_type = (worker_type or "").strip()
    count = worker_queue_type_count(conn, worker_type)
    conn.execute("DELETE FROM worker_queue WHERE worker_type = ?", (worker_type,))
    return count


def clear_automatic_worker_queue_type(conn: sqlite3.Connection, worker_type: str) -> int:
    worker_type = (worker_type or "").strip()
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM worker_queue
        WHERE worker_type = ? AND COALESCE(manual, 0) = 0
        """,
        (worker_type,),
    ).fetchone()
    count = int(row["count"] or 0)
    conn.execute(
        "DELETE FROM worker_queue WHERE worker_type = ? AND COALESCE(manual, 0) = 0",
        (worker_type,),
    )
    return count


def clear_worker_queue(conn: sqlite3.Connection) -> int:
    count = worker_queue_count(conn)
    conn.execute("DELETE FROM worker_queue")
    return count


def remove_worker_queue_entry(conn: sqlite3.Connection, queue_id: int) -> bool:
    cursor = conn.execute("DELETE FROM worker_queue WHERE queue_id = ?", (queue_id,))
    return cursor.rowcount > 0


def enqueue_history_task(conn: sqlite3.Connection, mode: str, *, priority: int = 100, manual: bool = True) -> str:
    mode = (mode or "recent").strip() or "recent"
    now = utc_now()
    subject_key = f"history:{mode}"
    conn.execute(
        """
        INSERT INTO worker_queue(
          subject_key, worker_type, task_type, video_id, channel_id, playlist_id,
          channel_title, current_title, source_key, playlist_count, priority, manual, created_at, updated_at
        )
        VALUES (?, 'history', ?, '', '', '', '', ?, '', 0, ?, ?, ?, ?)
        ON CONFLICT(subject_key) DO UPDATE SET
          worker_type='history',
          task_type=excluded.task_type,
          current_title=excluded.current_title,
          priority=MIN(worker_queue.priority, excluded.priority),
          manual=MAX(worker_queue.manual, excluded.manual),
          updated_at=excluded.updated_at
        """,
        (
            subject_key,
            mode,
            "Verify history" if mode == "verify" else "Fetch history",
            int(priority),
            1 if manual else 0,
            now,
            now,
        ),
    )
    return subject_key


def enqueue_account_sync_task(
    conn: sqlite3.Connection,
    *,
    priority: int = 100,
    manual: bool = True,
) -> str:
    now = utc_now()
    subject_key = "account:sync"
    conn.execute(
        """
        INSERT INTO worker_queue(
          subject_key, worker_type, task_type, video_id, channel_id, playlist_id,
          channel_title, current_title, source_key, playlist_count, priority,
          manual, created_at, updated_at
        )
        VALUES (?, 'account', 'sync', '', '', '', '', 'Collect personal activity', '', 0,
                ?, ?, ?, ?)
        ON CONFLICT(subject_key) DO UPDATE SET
          worker_type='account', task_type='sync',
          current_title='Collect personal activity',
          priority=MIN(worker_queue.priority, excluded.priority),
          manual=MAX(worker_queue.manual, excluded.manual),
          updated_at=excluded.updated_at
        """,
        (subject_key, int(priority), 1 if manual else 0, now, now),
    )
    return subject_key


def enqueue_playlist_scan_item(
    conn: sqlite3.Connection,
    playlist_id: str,
    *,
    title: str = "",
    source_key: str = "",
    priority: int = 100,
    manual: bool = False,
    payload: dict[str, Any] | None = None,
) -> str:
    now = utc_now()
    playlist_id = (playlist_id or "").strip()
    if not playlist_id:
        raise ValueError("Playlist queue item needs a playlist ID")
    if playlist_id == LIKED_VIDEOS_PLAYLIST_ID:
        title = "Liked videos"
    if not title:
        row = conn.execute(
            "SELECT title FROM playlists WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        if row:
            title = title or row["title"] or ""
    subject_key = playlist_queue_subject_key(playlist_id)
    payload_json = json.dumps(
        payload or {},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    conn.execute(
        """
        INSERT INTO worker_queue(
          subject_key, worker_type, task_type, video_id, channel_id, playlist_id,
          channel_title, current_title, source_key, playlist_count, priority, manual,
          payload_json, created_at, updated_at
        )
        VALUES (?, 'playlist', 'scan', '', '', ?, '', ?, ?, 0, ?, ?, ?, ?, ?)
        ON CONFLICT(subject_key) DO UPDATE SET
          worker_type='playlist',
          task_type='scan',
          playlist_id=excluded.playlist_id,
          current_title=COALESCE(NULLIF(excluded.current_title, ''), worker_queue.current_title),
          source_key=COALESCE(NULLIF(excluded.source_key, ''), worker_queue.source_key),
          priority=MIN(worker_queue.priority, excluded.priority),
          manual=MAX(worker_queue.manual, excluded.manual),
          payload_json=CASE
            WHEN excluded.manual = 1 THEN excluded.payload_json
            ELSE worker_queue.payload_json
          END,
          updated_at=excluded.updated_at
        """,
        (
            subject_key,
            playlist_id,
            title or playlist_id,
            source_key or (playlist_id if manual else ""),
            int(priority),
            1 if manual else 0,
            payload_json,
            now,
            now,
        ),
    )
    return subject_key


def enqueue_playlist_discovery_item(
    conn: sqlite3.Connection,
    *,
    priority: int = -1,
    mode: str = "all",
) -> str:
    now = utc_now()
    mode = (mode or "all").strip().lower()
    if mode not in {"all", "new"}:
        raise ValueError("Playlist discovery mode must be all or new")
    subject_key = "playlist:discover-current"
    conn.execute(
        """
        INSERT INTO worker_queue(
          subject_key, worker_type, task_type, video_id, channel_id, playlist_id,
          channel_title, current_title, source_key, playlist_count, priority, manual,
          created_at, updated_at
        )
        VALUES (?, 'playlist', 'discover', '', '', '', '', 'Current YouTube playlists',
                ?, 0, ?, 0, ?, ?)
        ON CONFLICT(subject_key) DO UPDATE SET
          worker_type='playlist',
          task_type='discover',
          source_key=CASE
            WHEN worker_queue.source_key = 'all' OR excluded.source_key = 'all' THEN 'all'
            ELSE excluded.source_key
          END,
          priority=MIN(worker_queue.priority, excluded.priority),
          updated_at=excluded.updated_at
        """,
        (subject_key, mode, int(priority), now, now),
    )
    return subject_key


def playlist_scan_queue_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
    queue_id: int = 0,
) -> list[sqlite3.Row]:
    sql = f"""
        SELECT w.queue_id,
               w.subject_key,
               w.task_type,
               w.playlist_id,
               COALESCE(NULLIF(w.current_title, ''), p.title, w.playlist_id) AS title,
               p.video_count AS playlist_video_count,
               COALESCE(ps.scanned_at, '') AS scanned_at,
               COALESCE(ps.scan_status, '') AS scan_status,
               COALESCE(ps.video_count, 0) AS video_count,
               COALESCE(ps.unavailable_count, 0) AS unavailable_count,
               w.source_key,
               w.priority,
               w.manual,
               w.payload_json,
               w.created_at,
               w.updated_at
        FROM worker_queue w
        LEFT JOIN playlists p ON p.playlist_id = w.playlist_id
        LEFT JOIN playlist_scans ps ON ps.playlist_id = w.playlist_id
        WHERE w.worker_type = 'playlist'
        ORDER BY {worker_queue_order_sql("w")}
    """
    params: list[Any] = []
    if queue_id:
        sql = sql.replace(
            "WHERE w.worker_type = 'playlist'",
            "WHERE w.worker_type = 'playlist' AND w.queue_id = ?",
        )
        params.append(queue_id)
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(max(0, offset))
    return conn.execute(sql, params).fetchall()


def playlist_scan_queue_count(conn: sqlite3.Connection) -> int:
    return worker_queue_type_count(conn, "playlist")


def clear_playlist_scan_queue(conn: sqlite3.Connection) -> int:
    return clear_worker_queue_type(conn, "playlist")


def enqueue_all_playlist_scan_items(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    discover_current: bool = False,
) -> dict[str, int]:
    rows = playlist_scan_candidate_rows(conn, force=force)
    queued_before = playlist_scan_queue_count(conn)
    if discover_current:
        enqueue_playlist_discovery_item(conn, mode="all")
    enqueue_playlist_scan_item(
        conn,
        LIKED_VIDEOS_PLAYLIST_ID,
        title="Liked videos",
        priority=0,
        manual=False,
    )
    for index, row in enumerate(rows, start=1):
        if row["playlist_id"] == LIKED_VIDEOS_PLAYLIST_ID:
            continue
        enqueue_playlist_scan_item(
            conn,
            row["playlist_id"] or "",
            title=row["title"] or "",
            priority=index,
            manual=False,
        )
    queued_after = playlist_scan_queue_count(conn)
    return {
        "cleared": 0,
        "inserted": max(0, queued_after - queued_before),
        "queued": queued_after,
    }


def enqueue_all_playlist_metadata_scan_items(
    conn: sqlite3.Connection,
) -> dict[str, int | str]:
    rows = conn.execute(
        """
        SELECT playlist_id, title
        FROM playlists
        WHERE playlist_id <> ''
          AND fetch_status <> 'removed'
        ORDER BY title COLLATE NOCASE, playlist_id
        """
    ).fetchall()
    queued_before = playlist_scan_queue_count(conn)
    for index, row in enumerate(rows, start=1):
        enqueue_playlist_scan_item(
            conn,
            row["playlist_id"] or "",
            title=row["title"] or "",
            source_key="playlist_metadata_only",
            priority=index,
            manual=True,
            payload={"metadata_only": True},
        )
    queued_after = playlist_scan_queue_count(conn)
    return {
        "mode": "metadata_only",
        "selected": len(rows),
        "cleared": 0,
        "inserted": max(0, queued_after - queued_before),
        "queued": queued_after,
    }


def rebuild_playlist_scan_queue(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    discover_current: bool = False,
) -> dict[str, int]:
    cleared = clear_playlist_scan_queue(conn)
    stats = enqueue_all_playlist_scan_items(
        conn,
        force=force,
        discover_current=discover_current,
    )
    return {**stats, "cleared": cleared}


FEATURE_BACKFILL_KINDS = {
    "video_visibility",
    "playlist_metadata",
    "channel_account",
}


def feature_backfill_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "video_visibility": int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM videos
                WHERE visibility_checked_at IS NULL
                """
            ).fetchone()[0]
            or 0
        ),
        "video_incomplete": int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM videos
                WHERE availability = 'unknown'
                   OR COALESCE(channel_id, '') = ''
                """
            ).fetchone()[0]
            or 0
        ),
        "playlist_metadata": int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM playlists
                WHERE fetch_status <> 'removed'
                  AND metadata_checked_at IS NULL
                """
            ).fetchone()[0]
            or 0
        ),
        "playlist_incomplete": int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM playlists
                WHERE fetch_status <> 'removed'
                  AND (
                    COALESCE(owner_channel_id, '') = ''
                    OR COALESCE(visibility, '') = ''
                  )
                """
            ).fetchone()[0]
            or 0
        ),
        "channel_account": int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM channels
                WHERE COALESCE(status, '') NOT IN ('terminated', 'deleted')
                  AND (
                    subscription_checked_at IS NULL
                    OR (subscribed = 1 AND notification_checked_at IS NULL)
                  )
                """
            ).fetchone()[0]
            or 0
        ),
        "channel_notification_missing": int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM channels
                WHERE subscribed = 1
                  AND notification_checked_at IS NULL
                  AND COALESCE(status, '') NOT IN ('terminated', 'deleted')
                """
            ).fetchone()[0]
            or 0
        ),
    }


def feature_backfill_candidate_rows(
    conn: sqlite3.Connection,
    kind: str,
    *,
    limit: int = 0,
) -> list[sqlite3.Row]:
    kind = (kind or "").strip().lower()
    if kind not in FEATURE_BACKFILL_KINDS:
        raise ValueError(f"Unsupported feature backfill kind: {kind}")
    params: list[Any] = []
    if kind == "channel_account":
        sql = """
            SELECT channel_id AS video_id,
                   channel_id,
                   COALESCE(NULLIF(title, ''), channel_id) AS channel_title,
                   0 AS playlist_count,
                   '' AS current_title,
                   'channel' AS metadata_source,
                   0 AS priority
            FROM channels
            WHERE COALESCE(status, '') NOT IN ('terminated', 'deleted')
              AND (
                subscription_checked_at IS NULL
                OR (subscribed = 1 AND notification_checked_at IS NULL)
              )
            ORDER BY
              CASE WHEN subscribed = 1 THEN 0 ELSE 1 END,
              COALESCE(fetched_at, ''),
              title COLLATE NOCASE,
              channel_id
        """
    elif kind == "playlist_metadata":
        sql = """
            SELECT p.playlist_id,
                   p.title,
                   COALESCE(ps.scanned_at, '') AS scanned_at
            FROM playlists p
            LEFT JOIN playlist_scans ps ON ps.playlist_id = p.playlist_id
            WHERE p.fetch_status <> 'removed'
              AND p.metadata_checked_at IS NULL
            ORDER BY
              COALESCE(ps.scanned_at, ''),
              p.title COLLATE NOCASE,
              p.playlist_id
        """
    else:
        sql = """
            WITH playlist_counts AS (
              SELECT video_id, COUNT(DISTINCT playlist_id) AS playlist_count
              FROM playlist_items
              WHERE video_id IS NOT NULL
              GROUP BY video_id
            ),
            history_stats AS (
              SELECT video_id,
                     MAX(COALESCE(watched_at, watch_date, '')) AS latest_history_at
              FROM history_events
              GROUP BY video_id
            )
            SELECT v.video_id,
                   COALESCE(v.channel_id, '') AS channel_id,
                   COALESCE(ch.title, '') AS channel_title,
                   COALESCE(pc.playlist_count, 0) AS playlist_count,
                   COALESCE(v.title, '') AS current_title,
                   CASE
                     WHEN COALESCE(pc.playlist_count, 0) > 0 THEN 'playlist'
                     ELSE 'history'
                   END AS metadata_source,
                   CASE
                     WHEN COALESCE(pc.playlist_count, 0) > 0 THEN 2
                     ELSE 3
                   END AS priority,
                   COALESCE(hs.latest_history_at, '') AS latest_history_at
            FROM videos v
            LEFT JOIN channels ch ON ch.channel_id = v.channel_id
            LEFT JOIN playlist_counts pc ON pc.video_id = v.video_id
            LEFT JOIN history_stats hs ON hs.video_id = v.video_id
            WHERE v.visibility_checked_at IS NULL
            ORDER BY
              priority,
              CASE
                WHEN COALESCE(pc.playlist_count, 0) = 0
                  THEN COALESCE(hs.latest_history_at, '')
                ELSE ''
              END DESC,
              COALESCE(v.fetched_at, ''),
              current_title COLLATE NOCASE,
              v.video_id
        """
    if limit:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))
    return conn.execute(sql, params).fetchall()


def enqueue_feature_backfill(
    conn: sqlite3.Connection,
    kind: str,
    *,
    limit: int = 0,
) -> dict[str, int | str]:
    kind = (kind or "").strip().lower()
    rows = feature_backfill_candidate_rows(conn, kind, limit=limit)
    before = worker_queue_count(conn)
    if kind == "playlist_metadata":
        for index, row in enumerate(rows):
            enqueue_playlist_scan_item(
                conn,
                row["playlist_id"] or "",
                title=row["title"] or "",
                priority=index + 1,
                manual=False,
            )
    else:
        for row in rows:
            enqueue_metadata_item(
                conn,
                video_id=row["video_id"] or "",
                channel_id=row["channel_id"] or "",
                channel_title=row["channel_title"] or "",
                current_title=row["current_title"] or "",
                metadata_source=row["metadata_source"] or "history",
                source_key=f"feature-backfill:{kind}",
                playlist_count=int(row["playlist_count"] or 0),
                priority=int(row["priority"] or 0),
                manual=False,
            )
    after = worker_queue_count(conn)
    inserted = max(0, after - before)
    return {
        "kind": kind,
        "selected": len(rows),
        "inserted": inserted,
        "already_queued": max(0, len(rows) - inserted),
        "queued": after,
    }


def metadata_queue_candidate_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
    force: bool = False,
    stale_days: int = 30,
    metadata_kind: str = "all",
    never_fetched_only: bool = False,
) -> list[sqlite3.Row]:
    stale_before = utc_days_ago(stale_days)
    metadata_kind = (metadata_kind or "all").strip().lower()
    if metadata_kind not in {"all", "video", "channel"}:
        raise ValueError("Metadata kind must be all, video, or channel")
    conditions: list[str] = []
    params: list[Any] = []
    if never_fetched_only:
        conditions.append(
            "(fetched_at IS NULL OR broadcast_status IN ('upcoming', 'live'))"
        )
    elif not force:
        conditions.append(
            "(fetch_status = 'error' OR fetched_at IS NULL OR fetched_at < ?)"
        )
        params.append(stale_before)
    if metadata_kind == "channel":
        conditions.append("metadata_source = 'channel'")
    elif metadata_kind == "video":
        conditions.append("metadata_source <> 'channel'")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        WITH candidates AS (
          SELECT ch.channel_id AS video_id,
                 ch.channel_id,
                 COALESCE(NULLIF(ch.title, ''), ch.channel_id) AS channel_title,
                 0 AS playlist_count,
                 '' AS current_title,
                 'channel' AS metadata_source,
                 0 AS priority,
                 ch.fetch_status,
                 ch.fetched_at,
                 ch.title,
                 ch.thumbnail_path,
                 '' AS latest_history_at,
                 '' AS broadcast_status
          FROM channels ch
          UNION ALL
          SELECT v.video_id,
                 COALESCE(v.channel_id, '') AS channel_id,
                 COALESCE(ch.title, '') AS channel_title,
                 COUNT(DISTINCT pi.playlist_id) AS playlist_count,
                 COALESCE(v.title, '') AS current_title,
                 CASE WHEN COUNT(pi.playlist_id) > 0 THEN 'playlist' ELSE 'history' END AS metadata_source,
                 CASE WHEN COUNT(pi.playlist_id) > 0 THEN 2 ELSE 3 END AS priority,
                 v.fetch_status,
                 v.fetched_at,
                 v.title,
                 v.thumbnail_path,
                 COALESCE(h.latest_history_at, '') AS latest_history_at,
                 COALESCE(v.broadcast_status, '') AS broadcast_status
          FROM videos v
          LEFT JOIN channels ch ON ch.channel_id = v.channel_id
          LEFT JOIN playlist_items pi ON pi.video_id = v.video_id
          LEFT JOIN (
            SELECT video_id, MAX(COALESCE(watched_at, watch_date, '')) AS latest_history_at
            FROM history_events
            GROUP BY video_id
          ) h ON h.video_id = v.video_id
          GROUP BY v.video_id
        )
        SELECT video_id, channel_id, channel_title, playlist_count, current_title,
               metadata_source, priority, broadcast_status
        FROM candidates
        {where}
        ORDER BY priority,
                 CASE WHEN metadata_source = 'history' THEN latest_history_at ELSE '' END DESC,
                 CASE WHEN metadata_source = 'history' THEN '' ELSE COALESCE(fetched_at, '') END,
                 current_title COLLATE NOCASE,
                 video_id
    """
    if limit:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, max(0, offset)])
    return conn.execute(sql, params).fetchall()


def metadata_queue_subject_key(video_id: str, channel_id: str, metadata_source: str) -> str:
    metadata_source = (metadata_source or "").strip() or "history"
    channel_id = (channel_id or "").strip()
    video_id = (video_id or "").strip()
    if metadata_source == "channel":
        return f"metadata:channel:{channel_id or video_id}"
    return f"metadata:video:{video_id}"


def playlist_queue_subject_key(playlist_id: str) -> str:
    return f"playlist:scan:{(playlist_id or '').strip()}"


def clip_queue_subject_key(clip_id: str = "", task_type: str = "scan") -> str:
    return "clip:discover" if task_type == "discover" else f"clip:scan:{clip_id.strip()}"


def enqueue_clip_item(
    conn: sqlite3.Connection,
    *,
    clip_id: str = "",
    task_type: str = "scan",
    mode: str = "new",
    title: str = "",
    priority: int = 0,
    manual: bool = False,
) -> str:
    task_type = (task_type or "scan").strip().lower()
    clip_id = extract_clip_id(clip_id) or clip_id.strip()
    if task_type not in {"discover", "scan"}:
        raise ValueError("Clip task type must be discover or scan")
    if task_type == "scan" and not clip_id:
        raise ValueError("Clip scan needs a clip ID")
    now = utc_now()
    subject_key = clip_queue_subject_key(clip_id, task_type)
    conn.execute(
        """
        INSERT INTO worker_queue(
          subject_key, worker_type, task_type, video_id, clip_id, channel_id,
          playlist_id, channel_title, current_title, source_key,
          playlist_count, priority, manual, created_at, updated_at
        )
        VALUES (?, 'clip', ?, '', ?, '', '', '', ?, ?, 0, ?, ?, ?, ?)
        ON CONFLICT(subject_key) DO UPDATE SET
          current_title=COALESCE(NULLIF(excluded.current_title, ''), worker_queue.current_title),
          source_key=CASE
            WHEN excluded.task_type = 'discover'
             AND (worker_queue.source_key = 'all' OR excluded.source_key = 'all')
              THEN 'all'
            ELSE excluded.source_key
          END,
          priority=MIN(worker_queue.priority, excluded.priority),
          manual=MAX(worker_queue.manual, excluded.manual),
          updated_at=excluded.updated_at
        """,
        (
            subject_key,
            task_type,
            clip_id,
            title,
            mode if task_type == "discover" else "manual" if manual else "clip",
            int(priority),
            1 if manual else 0,
            now,
            now,
        ),
    )
    return subject_key


def metadata_queue_rows(
    conn: sqlite3.Connection,
    limit: int = 0,
    offset: int = 0,
    queue_id: int = 0,
) -> list[sqlite3.Row]:
    sql = f"""
        SELECT queue_id,
               subject_key,
               video_id,
               channel_id,
               channel_title,
               playlist_count,
               current_title,
               task_type AS metadata_source,
               source_key,
               priority,
               manual,
               created_at,
               updated_at
        FROM worker_queue
        WHERE worker_type = 'metadata'
        ORDER BY {worker_queue_order_sql()}
    """
    params: list[Any] = []
    if queue_id:
        sql = sql.replace(
            "WHERE worker_type = 'metadata'",
            "WHERE worker_type = 'metadata' AND queue_id = ?",
        )
        params.append(queue_id)
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(max(0, offset))
    return conn.execute(sql, params).fetchall()


def metadata_queue_count(
    conn: sqlite3.Connection,
    metadata_kind: str = "all",
) -> int:
    metadata_kind = (metadata_kind or "all").strip().lower()
    if metadata_kind == "all":
        return worker_queue_type_count(conn, "metadata")
    if metadata_kind not in {"video", "channel"}:
        raise ValueError("Metadata kind must be all, video, or channel")
    operator = "=" if metadata_kind == "channel" else "<>"
    row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM worker_queue
        WHERE worker_type = 'metadata'
          AND task_type {operator} 'channel'
        """
    ).fetchone()
    return int(row[0] or 0)


def clear_metadata_queue(conn: sqlite3.Connection, metadata_kind: str = "all") -> int:
    metadata_kind = (metadata_kind or "all").strip().lower()
    if metadata_kind == "all":
        return clear_worker_queue_type(conn, "metadata")
    count = metadata_queue_count(conn, metadata_kind=metadata_kind)
    operator = "=" if metadata_kind == "channel" else "<>"
    conn.execute(
        f"""
        DELETE FROM worker_queue
        WHERE worker_type = 'metadata'
          AND task_type {operator} 'channel'
        """
    )
    return count


def enqueue_metadata_item(
    conn: sqlite3.Connection,
    *,
    video_id: str = "",
    channel_id: str = "",
    channel_title: str = "",
    current_title: str = "",
    metadata_source: str = "history",
    source_key: str = "",
    playlist_count: int = 0,
    priority: int = 100,
    manual: bool = False,
) -> str:
    now = utc_now()
    video_id = (video_id or "").strip()
    channel_id = (channel_id or "").strip()
    metadata_source = (metadata_source or "").strip() or "history"
    reject_clip_id_as_video(video_id)
    subject_key = metadata_queue_subject_key(video_id, channel_id, metadata_source)
    if not video_id and not channel_id:
        raise ValueError("Metadata queue item needs a video ID or channel ID")
    conn.execute(
        """
        INSERT INTO worker_queue(
          subject_key, worker_type, task_type, video_id, channel_id, playlist_id,
          channel_title, current_title, source_key, playlist_count, priority, manual, created_at, updated_at
        )
        VALUES (?, 'metadata', ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(subject_key) DO UPDATE SET
          worker_type='metadata',
          task_type=excluded.task_type,
          video_id=excluded.video_id,
          channel_id=excluded.channel_id,
          channel_title=COALESCE(NULLIF(excluded.channel_title, ''), worker_queue.channel_title),
          current_title=COALESCE(NULLIF(excluded.current_title, ''), worker_queue.current_title),
          source_key=COALESCE(NULLIF(excluded.source_key, ''), worker_queue.source_key),
          playlist_count=MAX(worker_queue.playlist_count, excluded.playlist_count),
          priority=MIN(worker_queue.priority, excluded.priority),
          manual=MAX(worker_queue.manual, excluded.manual),
          updated_at=excluded.updated_at
        """,
        (
            subject_key,
            metadata_source,
            video_id,
            channel_id,
            channel_title or "",
            current_title or "",
            source_key or "",
            max(0, int(playlist_count or 0)),
            int(priority),
            1 if manual else 0,
            now,
            now,
        ),
    )
    return subject_key


def enqueue_new_history_metadata_targets(
    conn: sqlite3.Connection,
    assignments: list[dict[str, Any]],
    *,
    excluded_video_ids: set[str] | None = None,
) -> list[str]:
    excluded = excluded_video_ids or set()
    video_ids: list[str] = []
    seen: set[str] = set()
    for assignment in assignments:
        video_id = str(assignment.get("video_id") or "").strip()
        if (
            not video_id
            or assignment.get("old_ordinal") is not None
            or video_id in excluded
            or video_id in seen
        ):
            continue
        seen.add(video_id)
        video_ids.append(video_id)
    if not video_ids:
        return []
    placeholders = ", ".join("?" for _ in video_ids)
    titles = {
        row["video_id"]: row["title"] or ""
        for row in conn.execute(
            f"""
            SELECT video_id, title
            FROM videos
            WHERE video_id IN ({placeholders})
            """,
            video_ids,
        )
    }
    # Insert oldest to newest so the newest fetched item receives the highest
    # queue ID and wins the recency tie-break within this action.
    for video_id in reversed(video_ids):
        enqueue_metadata_item(
            conn,
            video_id=video_id,
            current_title=titles.get(video_id, ""),
            metadata_source="history",
            priority=0,
            manual=False,
        )
    return video_ids


def rebuild_metadata_queue(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    stale_days: int = 30,
    metadata_kind: str = "all",
) -> dict[str, int]:
    rows = metadata_queue_candidate_rows(
        conn,
        force=force,
        stale_days=stale_days,
        metadata_kind=metadata_kind,
    )
    cleared = clear_metadata_queue(conn, metadata_kind=metadata_kind)
    inserted = 0
    for index, row in enumerate(rows, start=1):
        source = row["metadata_source"] or "history"
        priority = int(row["priority"] or 0) * 1_000_000 + index
        enqueue_metadata_item(
            conn,
            video_id=row["video_id"] or "",
            channel_id=row["channel_id"] or "",
            channel_title=row["channel_title"] or "",
            current_title=row["current_title"] or "",
            metadata_source=source,
            playlist_count=int(row["playlist_count"] or 0),
            priority=priority,
            manual=False,
        )
        inserted += 1
    return {"cleared": cleared, "inserted": inserted}


def library_has_data(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT
          EXISTS(SELECT 1 FROM videos)
          OR EXISTS(SELECT 1 FROM playlists)
          OR EXISTS(SELECT 1 FROM clips)
          OR EXISTS(SELECT 1 FROM channels WHERE channel_id <> '')
          OR EXISTS(SELECT 1 FROM history_events)
        """
    ).fetchone()
    return bool(row[0])


@dataclass(frozen=True)
class LibraryQueuePlan:
    name: str
    clip_discovery_mode: str
    playlist_discovery_mode: str
    history_mode: str
    history_manual: bool
    playlist_selection: str
    include_liked_videos: bool
    metadata_selection: str
    metadata_stale_days: int
    prioritize_broadcast_rechecks: bool = False
    replace_worker_types: tuple[str, ...] = ()
    insert_at_queue_front: bool = False


INITIALIZATION_QUEUE_PLAN = LibraryQueuePlan(
    name="initialize",
    clip_discovery_mode="all",
    playlist_discovery_mode="",
    history_mode="verify",
    history_manual=True,
    playlist_selection="all",
    include_liked_videos=True,
    metadata_selection="due",
    metadata_stale_days=30,
)
UPDATE_QUEUE_PLAN = LibraryQueuePlan(
    name="update",
    clip_discovery_mode="new",
    playlist_discovery_mode="new",
    history_mode="recent",
    history_manual=False,
    playlist_selection="due",
    include_liked_videos=False,
    metadata_selection="never",
    metadata_stale_days=30,
    prioritize_broadcast_rechecks=True,
    insert_at_queue_front=True,
)
REBUILD_QUEUE_PLAN = LibraryQueuePlan(
    name="rebuild",
    clip_discovery_mode="new",
    playlist_discovery_mode="new",
    history_mode="recent",
    history_manual=False,
    playlist_selection="due",
    include_liked_videos=False,
    metadata_selection="due",
    metadata_stale_days=30,
    replace_worker_types=("account", "history", "metadata", "playlist"),
)


def worker_queue_counts_by_type(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        str(row["worker_type"] or ""): int(row["count"] or 0)
        for row in conn.execute(
            """
            SELECT worker_type, COUNT(*) AS count
            FROM worker_queue
            GROUP BY worker_type
            ORDER BY worker_type
            """
        )
    }


def enqueue_library_queue_plan(
    conn: sqlite3.Connection,
    plan: LibraryQueuePlan,
) -> dict[str, Any]:
    if plan.playlist_selection not in {"all", "due"}:
        raise ValueError(f"Unsupported playlist queue selection: {plan.playlist_selection}")
    if plan.metadata_selection not in {"due", "never"}:
        raise ValueError(f"Unsupported metadata queue selection: {plan.metadata_selection}")

    playlist_rows = playlist_scan_candidate_rows(
        conn,
        force=plan.playlist_selection == "all",
    )
    metadata_rows = metadata_queue_candidate_rows(
        conn,
        force=False,
        stale_days=plan.metadata_stale_days,
        metadata_kind="all",
        never_fetched_only=plan.metadata_selection == "never",
    )

    cleared_by_type = {
        worker_type: clear_automatic_worker_queue_type(conn, worker_type)
        for worker_type in plan.replace_worker_types
    }
    preserved_by_type = worker_queue_counts_by_type(conn)
    queued_before_plan = worker_queue_count(conn)

    playlist_priorities = [
        index
        for index, row in enumerate(playlist_rows, start=1)
        if row["playlist_id"] != LIKED_VIDEOS_PLAYLIST_ID
    ]
    playlist_count = len(playlist_priorities)
    playlist_priority_ceiling = max([0, *playlist_priorities])
    metadata_priorities: list[int] = []
    broadcast_recheck_count = 0
    for index, row in enumerate(metadata_rows, start=1):
        if (
            plan.prioritize_broadcast_rechecks
            and row["broadcast_status"] in {"live", "upcoming"}
        ):
            broadcast_recheck_count += 1
            metadata_priorities.append(
                playlist_priority_ceiling + broadcast_recheck_count
            )
            continue
        metadata_priorities.append(
            1_000_000 + int(row["priority"] or 0) * 1_000_000 + index
        )
    priority_ceiling = max(
        [
            0,
            *playlist_priorities,
            *metadata_priorities,
        ]
    )
    priority_offset = 0
    if plan.insert_at_queue_front:
        boundary = conn.execute(
            """
            SELECT MIN(priority) AS priority
            FROM worker_queue
            """
        ).fetchone()["priority"]
        if boundary is not None:
            priority_offset = min(0, int(boundary) - priority_ceiling - 1)

    enqueue_account_sync_task(conn, priority=priority_offset - 4, manual=False)
    enqueue_clip_item(
        conn,
        task_type="discover",
        mode=plan.clip_discovery_mode,
        priority=priority_offset - 3,
        manual=False,
    )
    if plan.playlist_discovery_mode:
        enqueue_playlist_discovery_item(
            conn,
            priority=priority_offset - 2,
            mode=plan.playlist_discovery_mode,
        )
    enqueue_history_task(
        conn,
        plan.history_mode,
        priority=priority_offset - 1,
        manual=plan.history_manual,
    )
    if plan.include_liked_videos:
        enqueue_playlist_scan_item(
            conn,
            LIKED_VIDEOS_PLAYLIST_ID,
            title="Liked videos",
            source_key=plan.name,
            priority=priority_offset,
            manual=False,
        )

    for index, row in enumerate(playlist_rows, start=1):
        if row["playlist_id"] == LIKED_VIDEOS_PLAYLIST_ID:
            continue
        enqueue_playlist_scan_item(
            conn,
            row["playlist_id"] or "",
            title=row["title"] or "",
            source_key=plan.name,
            priority=priority_offset + index,
            manual=False,
        )

    for row, relative_priority in zip(metadata_rows, metadata_priorities, strict=True):
        source = row["metadata_source"] or "history"
        priority = priority_offset + relative_priority
        enqueue_metadata_item(
            conn,
            video_id=row["video_id"] or "",
            channel_id=row["channel_id"] or "",
            channel_title=row["channel_title"] or "",
            current_title=row["current_title"] or "",
            metadata_source=source,
            source_key=plan.name,
            playlist_count=int(row["playlist_count"] or 0),
            priority=priority,
            manual=False,
        )

    queued_after = worker_queue_count(conn)
    selected = (
        3
        + bool(plan.playlist_discovery_mode)
        + bool(plan.include_liked_videos)
        + playlist_count
        + len(metadata_rows)
    )
    inserted = max(0, queued_after - queued_before_plan)
    return {
        "plan": plan.name,
        "account": 1,
        "clips": 1,
        "discovery": 1 if plan.playlist_discovery_mode else 0,
        "history": 1,
        "liked_videos": 1 if plan.include_liked_videos else 0,
        "playlists": playlist_count,
        "playlist_scans": playlist_count + (1 if plan.include_liked_videos else 0),
        "metadata": len(metadata_rows),
        "selected": selected,
        "inserted": inserted,
        "already_queued": max(0, selected - inserted),
        "queued": queued_after,
        "cleared": sum(cleared_by_type.values()),
        "cleared_by_type": cleared_by_type,
        "preserved": queued_before_plan,
        "preserved_by_type": preserved_by_type,
    }


def enqueue_update_tasks(conn: sqlite3.Connection) -> dict[str, int]:
    stats = enqueue_library_queue_plan(conn, UPDATE_QUEUE_PLAN)
    return {
        key: int(stats[key])
        for key in (
            "account",
            "clips",
            "discovery",
            "history",
            "liked_videos",
            "playlists",
            "metadata",
            "selected",
            "inserted",
            "already_queued",
            "queued",
        )
    }


def enqueue_initialization_tasks(conn: sqlite3.Connection) -> dict[str, int | bool]:
    had_data = library_has_data(conn)
    stats = enqueue_library_queue_plan(conn, INITIALIZATION_QUEUE_PLAN)
    return {
        "had_data": had_data,
        "metadata": int(stats["metadata"]),
        "playlists": int(stats["playlist_scans"]),
        "account": int(stats["account"]),
        "clips": int(stats["clips"]),
        "history": int(stats["history"]),
        "selected": int(stats["selected"]),
        "inserted": int(stats["inserted"]),
        "already_queued": int(stats["already_queued"]),
        "queued": int(stats["queued"]),
    }


def rebuild_library_queue(conn: sqlite3.Connection) -> dict[str, Any]:
    return enqueue_library_queue_plan(conn, REBUILD_QUEUE_PLAN)


def enqueue_playlist_metadata_targets(
    conn: sqlite3.Connection,
    playlist_id: str,
    *,
    never_fetched_only: bool = False,
    manual: bool = True,
) -> dict[str, str]:
    playlist_id = (playlist_id or "").strip()
    if not playlist_id:
        raise ValueError("Enter a YouTube playlist URL or playlist ID.")
    metadata_condition = ""
    if never_fetched_only:
        metadata_condition = "AND pi.membership_state = 'current' AND v.fetched_at IS NULL"
    rows = conn.execute(
        f"""
        SELECT pi.video_id,
               MAX(v.title) AS title,
               MIN(pi.position) AS position
        FROM playlist_items pi
        JOIN videos v ON v.video_id = pi.video_id
        WHERE pi.playlist_id = ?
          {metadata_condition}
        GROUP BY pi.video_id
        ORDER BY position
        """,
        (playlist_id,),
    ).fetchall()
    if not rows:
        if never_fetched_only:
            return {
                "subject_key": f"playlist:{playlist_id}",
                "video_id": "",
                "channel_id": "",
                "metadata_source": "playlist",
                "playlist_id": playlist_id,
                "queued_count": "0",
                "inserted_count": "0",
            }
        raise ValueError(f"No known videos found for playlist {playlist_id}. Scan the playlist first.")
    queued_before = worker_queue_count(conn)
    for index, row in enumerate(rows):
        enqueue_metadata_item(
            conn,
            video_id=row["video_id"] or "",
            current_title=row["title"] or "",
            metadata_source="playlist",
            source_key=playlist_id,
            priority=index,
            manual=manual,
        )
    inserted = max(0, worker_queue_count(conn) - queued_before)
    return {
        "subject_key": f"playlist:{playlist_id}",
        "video_id": "",
        "channel_id": "",
        "metadata_source": "playlist",
        "playlist_id": playlist_id,
        "queued_count": str(len(rows)),
        "inserted_count": str(inserted),
    }


def local_queue_target_from_url(target: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(target)
    if not parsed.scheme and not parsed.netloc:
        return "", ""
    host = (parsed.hostname or "").lower()
    if host.endswith("youtube.com") or host == "youtu.be":
        return "", ""

    query_params = urllib.parse.parse_qs(parsed.query)
    if (query_params.get("list") or [""])[0]:
        return "playlist", (query_params.get("list") or [""])[0].strip()
    if (query_params.get("v") or [""])[0]:
        return "video", (query_params.get("v") or [""])[0].strip()

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    singular_kind = {
        "playlists": "playlist",
        "videos": "video",
        "clips": "clip",
        "channels": "channel",
    }
    if len(parts) >= 2 and parts[0] in singular_kind:
        return singular_kind[parts[0]], urllib.parse.unquote(parts[1]).strip()
    return "", ""


def enqueue_worker_queue_target(conn: sqlite3.Connection, target: str) -> dict[str, str]:
    target = (target or "").strip()
    if not target:
        raise ValueError("Enter a video, clip, channel, or playlist URL or ID.")

    parsed = urllib.parse.urlparse(target)
    host = (parsed.hostname or "").lower()
    is_youtube_url = parsed.scheme in {"http", "https"} and (
        host.endswith("youtube.com") or host == "youtu.be"
    )

    clip_id = extract_clip_id(target)
    if clip_id:
        subject_key = enqueue_clip_item(
            conn,
            clip_id=clip_id,
            task_type="scan",
            title=target if is_youtube_url else "",
            priority=0,
            manual=True,
        )
        return {
            "subject_key": subject_key,
            "worker_type": "clip",
            "clip_id": clip_id,
            "source": "youtube" if is_youtube_url else "local",
        }

    if is_youtube_url:
        video_id = extract_video_id(target)
        playlist_id = extract_playlist_id(target) or ""
        channel_id = "" if video_id else youtube_channel_ref_from_url(target)
        if playlist_id and not video_id:
            subject_key = enqueue_playlist_scan_item(
                conn,
                playlist_id,
                source_key=target,
                priority=0,
                manual=True,
            )
            return {
                "subject_key": subject_key,
                "worker_type": "playlist",
                "playlist_id": playlist_id,
                "source": "youtube",
            }
        if channel_id:
            subject_key = enqueue_metadata_item(
                conn,
                video_id=channel_id,
                channel_id=channel_id,
                channel_title=target,
                metadata_source="channel",
                source_key=target,
                priority=0,
                manual=True,
            )
            return {
                "subject_key": subject_key,
                "worker_type": "metadata",
                "channel_id": channel_id,
                "metadata_source": "channel",
                "source": "youtube",
            }
        if video_id:
            subject_key = enqueue_metadata_item(
                conn,
                video_id=video_id,
                current_title=target,
                metadata_source="provided",
                source_key=target,
                priority=0,
                manual=True,
            )
            return {
                "subject_key": subject_key,
                "worker_type": "metadata",
                "video_id": video_id,
                "metadata_source": "provided",
                "source": "youtube",
            }
        raise ValueError("Could not identify a YouTube video, clip, channel, or playlist from that URL.")

    local_kind, local_value = local_queue_target_from_url(target)
    if local_kind:
        target = local_value

    playlist_id = extract_playlist_id(target) or ""
    if local_kind == "playlist" or (not local_kind and playlist_id):
        playlist_id = playlist_id or target.strip()
        subject_key = enqueue_playlist_scan_item(
            conn,
            playlist_id,
            source_key=target if local_kind else playlist_id,
            priority=0,
            manual=True,
        )
        return {
            "subject_key": subject_key,
            "playlist_id": playlist_id,
            "queued_count": "1",
            "worker_type": "playlist",
            "source": "local",
        }

    channel_ref = ""
    if local_kind == "channel":
        channel_ref = target.strip()
    elif target.startswith(("@", "channel/", "c/", "user/")):
        channel_ref = youtube_channel_ref_from_url(target) or target.removeprefix("channel/")
    elif target.startswith(("UC", "HC")) and len(target) >= 20:
        channel_ref = target
    if channel_ref:
        subject_key = enqueue_metadata_item(
            conn,
            video_id=channel_ref,
            channel_id=channel_ref,
            channel_title=target,
            metadata_source="channel",
            source_key="local",
            priority=0,
            manual=True,
        )
        return {
            "subject_key": subject_key,
            "worker_type": "metadata",
            "channel_id": channel_ref,
            "metadata_source": "channel",
            "source": "local",
        }

    video_id = extract_video_id(target) or target.strip()
    if local_kind == "clip":
        clip_id = extract_clip_id(target) or target.strip()
        subject_key = enqueue_clip_item(
            conn,
            clip_id=clip_id,
            task_type="scan",
            priority=0,
            manual=True,
        )
        return {
            "subject_key": subject_key,
            "worker_type": "clip",
            "clip_id": clip_id,
            "source": "local",
        }
    if local_kind and local_kind != "video":
        raise ValueError("Could not identify a video, clip, channel, or playlist from that local URL.")
    if not video_id:
        raise ValueError("Enter a video, clip, channel, or playlist URL or ID.")
    subject_key = enqueue_metadata_item(
        conn,
        video_id=video_id,
        current_title="",
        metadata_source="provided",
        source_key="local",
        priority=0,
        manual=True,
    )
    return {
        "subject_key": subject_key,
        "worker_type": "metadata",
        "video_id": video_id,
        "metadata_source": "provided",
        "source": "local",
    }


def admin_status(
    db_path: Path,
    metadata_worker: "MetadataWorker | None" = None,
    playlist_worker: "PlaylistScanWorker | None" = None,
    live_history_worker: "LiveHistoryWorker | None" = None,
    queue_dispatcher: "WorkerQueueDispatcher | None" = None,
    include_logs: bool = True,
    worker_queue_limit: int = 500,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        counts = dict(
            conn.execute(
                """
                SELECT
                  COUNT(DISTINCT video_id) AS distinct_playlist_item_videos,
                  COUNT(*) AS playlist_item_rows,
                  (SELECT COUNT(*) FROM history_events) AS history_rows,
                  (SELECT COUNT(DISTINCT video_id) FROM history_events) AS distinct_history_videos
                FROM playlist_items
                WHERE video_id IS NOT NULL
                """
            ).fetchone()
        )
        live_history_counts = dict(
            conn.execute(
                """
                SELECT
                  COUNT(*) AS live_rows,
                  COUNT(DISTINCT video_id) AS live_video_ids,
                  COALESCE(MAX(imported_at), '') AS last_imported_at
                FROM history_events
                WHERE youtube_ordinal IS NOT NULL
                """
            ).fetchone()
        )
        playlist_counts = dict(
            conn.execute(
                """
                SELECT
                  COUNT(*) AS total_playlists,
                  SUM(CASE WHEN ps.playlist_id IS NULL THEN 1 ELSE 0 END) AS unscanned_playlists,
                  SUM(CASE WHEN ps.scan_status = 'ok' THEN 1 ELSE 0 END) AS scanned_ok,
                  SUM(CASE WHEN ps.scan_status = 'unavailable' THEN 1 ELSE 0 END) AS unavailable,
                  SUM(
                    CASE
                      WHEN ps.scan_status NOT IN ('', 'ok', 'unavailable') THEN 1
                      ELSE 0
                    END
                  ) AS scan_errors
                FROM playlists p
                LEFT JOIN playlist_scans ps ON ps.playlist_id = p.playlist_id
                """
            ).fetchone()
        )
        metadata_counts = [
            dict(row)
            for row in conn.execute(
                """
                SELECT COALESCE(fetch_status, '') AS fetch_status, COUNT(*) AS count
                FROM videos
                GROUP BY COALESCE(fetch_status, '')
                ORDER BY count DESC
                """
            )
        ]
        channel_counts = dict(
            conn.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN COALESCE(thumbnail_path, '') <> '' THEN 1 ELSE 0 END) AS thumbnail_cached,
                  SUM(CASE WHEN COALESCE(thumbnail_path, '') = '' AND COALESCE(status, '') NOT IN ('terminated', 'deleted') THEN 1 ELSE 0 END) AS thumbnail_missing,
                  SUM(CASE WHEN COALESCE(status, '') IN ('terminated', 'deleted') THEN 1 ELSE 0 END) AS terminated,
                  SUM(CASE WHEN COALESCE(first_seen_at, '') <> '' THEN 1 ELSE 0 END) AS first_seen,
                  SUM(CASE WHEN COALESCE(first_seen_at, '') = '' THEN 1 ELSE 0 END) AS first_seen_missing,
                  0 AS url_missing
                FROM channels
                WHERE channel_id <> ''
                """
            ).fetchone()
        )
        clip_counts = dict(
            conn.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN ownership = 'mine' THEN 1 ELSE 0 END) AS mine,
                  SUM(CASE WHEN ownership = 'others' THEN 1 ELSE 0 END) AS others,
                  SUM(CASE WHEN ownership = 'unknown' THEN 1 ELSE 0 END) AS ownership_unknown,
                  SUM(CASE WHEN availability = 'active' THEN 1 ELSE 0 END) AS active,
                  SUM(CASE WHEN availability = 'unavailable' THEN 1 ELSE 0 END) AS unavailable,
                  SUM(CASE WHEN fetch_status IN ('', 'discovered') THEN 1 ELSE 0 END) AS metadata_pending
                FROM clips
                """
            ).fetchone()
        )
        backfill_counts = feature_backfill_counts(conn)
        has_library_data = library_has_data(conn)
        runtime_status = _admin_runtime_status_from_connection(conn, queue_dispatcher)
        worker_queue_limit = max(0, min(10000, int(worker_queue_limit if worker_queue_limit is not None else 500)))
        worker_queue_preview_rows = (
            [dict(row) for row in worker_queue_rows(conn, limit=worker_queue_limit)]
            if worker_queue_limit > 0
            else []
        )
        latest_metadata_run = conn.execute(
            """
            SELECT *
            FROM metadata_worker_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        latest_playlist_run = conn.execute(
            """
            SELECT *
            FROM playlist_scan_worker_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        latest_live_history_run = conn.execute(
            """
            SELECT *
            FROM live_history_worker_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        latest_placeholder_recovery_run = conn.execute(
            """
            SELECT *
            FROM placeholder_recovery_worker_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        archivarix_request_counts = dict(
            conn.execute(
                """
                SELECT
                  COALESCE(SUM(request_count), 0) AS total,
                  COALESCE(
                    SUM(
                      CASE
                        WHEN request_started_at >= strftime('%Y-%m-%dT00:00:00Z', 'now')
                         AND request_started_at < strftime('%Y-%m-%dT00:00:00Z', 'now', '+1 day')
                          THEN request_count
                        ELSE 0
                      END
                    ),
                    0
                  ) AS current_utc_day,
                  COALESCE(MAX(request_started_at), '') AS latest_at,
                  strftime('%Y-%m-%dT00:00:00Z', 'now') AS window_started_at,
                  strftime('%Y-%m-%dT00:00:00Z', 'now', '+1 day') AS window_ends_at
                FROM placeholder_recovery_worker_runs
                WHERE request_count > 0
                """
            ).fetchone()
        )
        metadata_logs: list[dict[str, Any]] = []
        playlist_logs: list[dict[str, Any]] = []
        live_history_logs: list[dict[str, Any]] = []
        placeholder_recovery_logs: list[dict[str, Any]] = []
        plugin_worker_logs: list[dict[str, Any]] = []
        if include_logs:
            log_snapshot = worker_log_snapshot(conn)
            metadata_logs = [dict(row) for row in log_snapshot["metadataLogs"]]
            playlist_logs = [dict(row) for row in log_snapshot["playlistScanLogs"]]
            live_history_logs = [dict(row) for row in log_snapshot["liveHistoryLogs"]]
            placeholder_recovery_logs = [
                dict(row) for row in log_snapshot["placeholderRecoveryLogs"]
            ]
            plugin_worker_logs = [
                dict(row) for row in log_snapshot["pluginWorkerLogs"]
            ]
    finally:
        conn.close()
    return {
        **runtime_status,
        "metadataRunning": metadata_worker.is_running() if metadata_worker else False,
        "metadataStopping": metadata_worker.is_stopping() if metadata_worker else False,
        "playlistScanRunning": playlist_worker.is_running() if playlist_worker else False,
        "playlistScanStopping": playlist_worker.is_stopping() if playlist_worker else False,
        "liveHistoryRunning": live_history_worker.is_running() if live_history_worker else False,
        "liveHistoryStopping": live_history_worker.is_stopping() if live_history_worker else False,
        "counts": counts,
        "liveHistoryCounts": live_history_counts,
        "playlistCounts": playlist_counts,
        "metadataCounts": metadata_counts,
        "channelCounts": channel_counts,
        "clipCounts": clip_counts,
        "hasLibraryData": has_library_data,
        "featureBackfillCounts": backfill_counts,
        "workerQueue": worker_queue_preview_rows,
        "latestRun": dict(latest_metadata_run) if latest_metadata_run else None,
        "latestMetadataRun": dict(latest_metadata_run) if latest_metadata_run else None,
        "latestPlaylistScanRun": dict(latest_playlist_run) if latest_playlist_run else None,
        "latestLiveHistoryRun": dict(latest_live_history_run) if latest_live_history_run else None,
        "latestPlaceholderRecoveryRun": (
            dict(latest_placeholder_recovery_run) if latest_placeholder_recovery_run else None
        ),
        "archivarixRequestCounts": archivarix_request_counts,
        "logs": metadata_logs,
        "metadataLogs": metadata_logs,
        "playlistScanLogs": playlist_logs,
        "liveHistoryLogs": live_history_logs,
        "placeholderRecoveryLogs": placeholder_recovery_logs,
        "pluginWorkerLogs": plugin_worker_logs,
    }


_WORKER_RUN_KINDS = ("metadata", "playlist", "history", "placeholder")


def reconcile_worker_runs(
    db_path: Path,
    metadata_worker: "MetadataWorker | None" = None,
    playlist_worker: "PlaylistScanWorker | None" = None,
    live_history_worker: "LiveHistoryWorker | None" = None,
    placeholder_recovery_worker: "PlaceholderRecoveryWorker | None" = None,
) -> None:
    worker_states = zip(
        _WORKER_RUN_KINDS,
        (
            metadata_worker,
            playlist_worker,
            live_history_worker,
            placeholder_recovery_worker,
        ),
    )
    inactive_run_kinds = [
        kind
        for kind, worker in worker_states
        if worker is None or not worker.is_running()
    ]
    now = utc_now()
    conn = connect(db_path)
    try:
        with conn:
            for kind in inactive_run_kinds:
                WorkerRunRecorder(conn, kind).interrupt_running(finished_at=now)
            WorkerRunRecorder(conn, "plugin").interrupt_running(finished_at=now)
    finally:
        conn.close()


def recover_archivarix_thumbnails(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    thumb_dir = Path(args.thumbs)
    proxy_url = configured_proxy(getattr(args, "config_data", {}))
    opener = network_opener(proxy_url)
    conn = connect(db_path)
    rows = conn.execute(
        """
        SELECT p.playlist_id, p.title, s.unavailable_count
        FROM playlist_scans s
        JOIN playlists p ON p.playlist_id = s.playlist_id
        WHERE s.unavailable_count > 0
        ORDER BY s.unavailable_count DESC, p.title COLLATE NOCASE
        """
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"Searching Archivarix for {len(rows)} affected playlists...")
    total_candidates = 0
    total_cached = 0
    for index, row in enumerate(rows, start=1):
        query = row["title"]
        try:
            videos = archivarix_search_deleted(
                query,
                page_size=args.page_size,
                opener=opener,
            )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"[{index:03d}/{len(rows):03d}] ERROR {query}: {exc}")
            continue
        candidates = [
            video
            for video in videos
            if isinstance(video, dict)
            and isinstance(video.get("videoId"), str)
            and video.get("status", "").upper().startswith("DELETED")
        ]
        cached = 0
        with conn:
            for video in candidates:
                video_id = video["videoId"]
                thumbnail_url = (
                    video.get("thumbnailArchiveUrl")
                    or video.get("thumbnailUrl")
                    or ""
                )
                thumbnail_path = cache_archivarix_thumbnail(
                    video_id,
                    thumbnail_url,
                    thumb_dir,
                    opener,
                )
                if thumbnail_path:
                    cached += 1
                if conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (video_id,)).fetchone():
                    save_video_recovery(
                        conn,
                        video_id,
                        video,
                        "found",
                        "",
                        thumbnail_url,
                        thumbnail_path,
                    )
        total_candidates += len(candidates)
        total_cached += cached
        print(
            f"[{index:03d}/{len(rows):03d}] "
            f"{len(candidates)} candidates, {cached} thumbnails - {query}"
        )
    print(f"Found {total_candidates} candidates and cached {total_cached} thumbnails.")


def normalized_playlist_title(value: str) -> str:
    value = re.sub(r"\(\d+\)$", "", value).strip()
    return value


def save_takeout_playlist_header(
    conn: sqlite3.Connection,
    playlist_id: str,
    title: str,
    visibility: str,
    observed_at: str,
) -> bool:
    if playlist_is_tombstoned(conn, playlist_id):
        return False
    conn.execute(
        """
        INSERT INTO playlists(
          playlist_id, title, visibility, ownership, in_library, updated_at
        )
        VALUES (?, ?, ?, 'mine', 1, ?)
        ON CONFLICT(playlist_id) DO UPDATE SET
          title=COALESCE(NULLIF(excluded.title, ''), playlists.title),
          visibility=COALESCE(NULLIF(excluded.visibility, ''), playlists.visibility),
          ownership='mine',
          in_library=1,
          updated_at=excluded.updated_at
        """,
        (playlist_id, title, visibility, observed_at),
    )
    return True


def import_takeout_playlists(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    takeout_dir = Path(args.takeout)
    playlists_dir = takeout_dir / "playlists"
    playlists_csv = playlists_dir / "playlists.csv"
    if not playlists_csv.exists():
        raise SystemExit(f"Takeout playlists.csv not found: {playlists_csv}")

    conn = connect(db_path)
    now = utc_now()
    playlist_rows: list[dict[str, str]] = []
    with playlists_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        playlist_rows = list(csv.DictReader(handle))

    playlist_by_title: dict[str, dict[str, str]] = {}
    for row in playlist_rows:
        title = row.get("Playlist Title (Original)", "").strip()
        if title:
            playlist_by_title[title.casefold()] = row

    with conn:
        for row in playlist_rows:
            playlist_id = row.get("Playlist ID", "").strip()
            if not playlist_id:
                continue
            save_takeout_playlist_header(
                conn,
                playlist_id,
                row.get("Playlist Title (Original)", "").strip(),
                normalize_playlist_visibility(row.get("Playlist Visibility", "")),
                now,
            )

    imported_video_rows = 0
    unmatched_files: list[str] = []
    video_files = sorted(playlists_dir.glob("*-videos.csv"))
    with conn:
        for video_file in video_files:
            title_from_file = normalized_playlist_title(video_file.name[: -len("-videos.csv")])
            playlist_row = playlist_by_title.get(title_from_file.casefold())
            if playlist_row is None:
                playlist_row = playlist_by_title.get(title_from_file.replace("_", "/").casefold())
            if playlist_row is None and title_from_file.endswith("_"):
                playlist_row = playlist_by_title.get((title_from_file[:-1] + "?").casefold())
            if playlist_row is None:
                unmatched_files.append(video_file.name)
                playlist_id = f"takeout:{title_from_file}"
                playlist_title = title_from_file
                save_takeout_playlist_header(
                    conn,
                    playlist_id,
                    playlist_title,
                    "",
                    now,
                )
            else:
                playlist_id = playlist_row.get("Playlist ID", "").strip()
                playlist_title = playlist_row.get("Playlist Title (Original)", "").strip()
            if not playlist_id:
                continue
            if playlist_is_tombstoned(conn, playlist_id):
                continue
            conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
            with video_file.open("r", encoding="utf-8-sig", newline="") as handle:
                for position, row in enumerate(csv.DictReader(handle), start=1):
                    video_id = row.get("Video ID", "").strip()
                    if not video_id:
                        continue
                    upsert_video(conn, video_id, source="takeout", updated_at=now)
                    added_at = row.get("Playlist Video Creation Timestamp", "").strip()
                    if added_at:
                        try:
                            added_at = normalize_utc_timestamp(added_at)
                        except ValueError:
                            pass
                    conn.execute(
                        """
                        INSERT INTO playlist_items(
                          playlist_id, position, video_id, membership_state,
                          source_quality, added_at, updated_at
                        )
                        VALUES (?, ?, ?, 'current', 'takeout', ?, ?)
                        """,
                        (
                            playlist_id,
                            position,
                            video_id,
                            added_at or None,
                            now,
                        ),
                    )
                    imported_video_rows += 1
            _refresh_playlist_known_change_at(conn, playlist_id)
        reconcile_stats = rebuild_playlist_reconciliation(conn)

    print(
        f"Imported {len(playlist_rows)} Takeout playlists and "
        f"{imported_video_rows} current playlist items."
    )
    print(
        f"Reconciled {reconcile_stats['rows']} playlist rows across "
        f"{reconcile_stats['playlists']} playlists."
    )
    if unmatched_files:
        print(f"Used filename-derived IDs for {len(unmatched_files)} playlist files.")


def strip_html_fragment(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def extract_video_id(value: str) -> str:
    parsed = urllib.parse.urlparse(html.unescape(value))
    if parsed.netloc.endswith("youtube.com") and parsed.path == "/watch":
        return (urllib.parse.parse_qs(parsed.query).get("v") or [""])[0]
    if parsed.netloc.endswith("youtube.com") and parsed.path.startswith(("/shorts/", "/embed/")):
        return parsed.path.strip("/").split("/", 1)[1].split("/", 1)[0]
    if parsed.netloc == "youtu.be":
        return parsed.path.strip("/")
    return ""


def parse_takeout_watch_history_text(text: str) -> list[dict[str, str]]:
    stripped = text.lstrip()
    if stripped.startswith("["):
        rows: list[dict[str, str]] = []
        try:
            records = json.loads(text)
        except json.JSONDecodeError:
            records = []
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            url = str(record.get("titleUrl") or "")
            video_id = extract_video_id(url)
            if not video_id:
                continue
            title = str(record.get("title") or "").strip()
            title = re.sub(r"^Watched\s+", "", title, flags=re.I).strip()
            subtitles = record.get("subtitles") if isinstance(record.get("subtitles"), list) else []
            channel = ""
            channel_url = ""
            if subtitles and isinstance(subtitles[0], dict):
                channel = str(subtitles[0].get("name") or "")
                channel_url = str(subtitles[0].get("url") or "")
            rows.append(
                {
                    "url": url,
                    "video_id": video_id,
                    "title": video_title_or_blank(title, video_id),
                    "channel_id": youtube_channel_id_from_url(channel_url),
                    "channel_url": channel_url,
                    "channel": channel,
                    "watched_at": str(record.get("time") or ""),
                }
            )
        return rows

    action = r"(Watched|Viewed)"
    sep = r"(?:\s|&nbsp;|\xa0)*"
    anchor = r'<a href="([^"]+)">(.*?)</a>'
    pattern = re.compile(
        action + sep + anchor + r"<br>(?:" + anchor + r"<br>)?([^<]+)<br>",
        re.IGNORECASE | re.DOTALL,
    )
    rows: list[dict[str, str]] = []
    for match in pattern.finditer(text):
        url = html.unescape(match.group(2))
        video_id = extract_video_id(url)
        if not video_id:
            continue
        rows.append(
            {
                "url": url,
                "video_id": video_id,
                "title": strip_html_fragment(match.group(3)),
                "channel_id": youtube_channel_id_from_url(html.unescape(match.group(4) or "")),
                "channel_url": html.unescape(match.group(4) or ""),
                "channel": strip_html_fragment(match.group(5) or ""),
                "watched_at": strip_html_fragment(match.group(6)),
            }
        )
    return rows


def takeout_key_from_path(path: Path) -> str:
    found = re.search(r"takeout-([0-9]{8}T[0-9]{6}Z)", path.name, re.I)
    if found:
        return found.group(1)
    return ""


def find_takeout_zips(path: Path) -> list[Path]:
    if path.is_file() and path.suffix.lower() == ".zip":
        return [path]
    if path.is_dir():
        return sorted(path.glob("takeout-*.zip"), key=lambda item: takeout_key_from_path(item) or item.name)
    return []


def find_takeout_zip(path: Path) -> Path | None:
    zips = find_takeout_zips(path)
    return zips[-1] if zips else None


def read_zip_member_text(zf: zipfile.ZipFile, suffix: str) -> str:
    normalized_suffix = suffix.replace("\\", "/").lower()
    for name in zf.namelist():
        if name.replace("\\", "/").lower().endswith(normalized_suffix):
            return zf.read(name).decode("utf-8", "replace")
    return ""


def import_takeout_playlists_zip(conn: sqlite3.Connection, zip_path: Path) -> dict[str, int]:
    now = utc_now()
    with zipfile.ZipFile(zip_path) as zf:
        playlists_text = read_zip_member_text(zf, "playlists/playlists.csv")
        if not playlists_text:
            return {
                "source_found": 0,
                "playlists": 0,
                "items": 0,
                "unmatched": 0,
                "tombstoned": 0,
            }
        playlist_rows = list(csv.DictReader(io.StringIO(playlists_text)))
        by_title = {
            (row.get("Playlist Title (Original)") or "").strip().casefold(): row
            for row in playlist_rows
            if (row.get("Playlist Title (Original)") or "").strip()
        }
        imported_playlist_ids: set[str] = set()
        tombstoned = 0
        for row in playlist_rows:
            playlist_id = (row.get("Playlist ID") or "").strip()
            if not playlist_id:
                continue
            if save_takeout_playlist_header(
                conn,
                playlist_id,
                (row.get("Playlist Title (Original)") or "").strip(),
                normalize_playlist_visibility(row.get("Playlist Visibility") or ""),
                now,
            ):
                imported_playlist_ids.add(playlist_id)
            else:
                tombstoned += 1

        item_count = 0
        unmatched = 0
        for member_name in zf.namelist():
            normalized = member_name.replace("\\", "/")
            if not normalized.lower().endswith("-videos.csv") or "/playlists/" not in normalized.lower():
                continue
            filename = normalized.rsplit("/", 1)[-1]
            title = normalized_playlist_title(filename[: -len("-videos.csv")])
            playlist_row = by_title.get(title.casefold())
            if playlist_row is None:
                playlist_row = by_title.get(title.replace("_", "/").casefold())
            if playlist_row is None and title.endswith("_"):
                playlist_row = by_title.get((title[:-1] + "?").casefold())
            if playlist_row is None:
                unmatched += 1
                continue
            playlist_id = (playlist_row.get("Playlist ID") or "").strip()
            if not playlist_id or playlist_id not in imported_playlist_ids:
                continue
            scanned = conn.execute(
                "SELECT 1 FROM playlist_scans WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()
            if not scanned:
                conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
            text = zf.read(member_name).decode("utf-8-sig", "replace")
            for position, row in enumerate(csv.DictReader(io.StringIO(text)), start=1):
                video_id = (row.get("Video ID") or "").strip()
                if not video_id:
                    continue
                upsert_video(conn, video_id, source="takeout", updated_at=now)
                if scanned:
                    continue
                added_at = (row.get("Playlist Video Creation Timestamp") or "").strip()
                if added_at:
                    try:
                        added_at = normalize_utc_timestamp(added_at)
                    except ValueError:
                        pass
                conn.execute(
                    """
                    INSERT INTO playlist_items(
                      playlist_id, position, video_id, membership_state,
                      source_quality, added_at, updated_at
                    ) VALUES (?, ?, ?, 'current', 'takeout', ?, ?)
                    """,
                    (playlist_id, position, video_id, added_at or None, now),
                )
                item_count += 1
            _refresh_playlist_known_change_at(conn, playlist_id)
    return {
        "source_found": 1,
        "playlists": len(imported_playlist_ids),
        "items": item_count,
        "unmatched": unmatched,
        "tombstoned": tombstoned,
    }


def load_takeout_subscriptions(takeout_path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for zip_path in find_takeout_zips(takeout_path):
        with zipfile.ZipFile(zip_path) as zf:
            text = read_zip_member_text(zf, "subscriptions/subscriptions.csv")
        if not text:
            continue
        for row in csv.DictReader(io.StringIO(text)):
            channel_id = (row.get("Channel Id") or row.get("Channel ID") or "").strip()
            channel_url = (row.get("Channel Url") or row.get("Channel URL") or "").strip()
            if not channel_id:
                channel_id = youtube_channel_id_from_url(channel_url)
            if not channel_id:
                continue
            rows.append(
                {
                    "channel_id": channel_id,
                    "channel_url": channel_url,
                    "title": (row.get("Channel Title") or "").strip(),
                }
            )
        if rows:
            break
    return rows


def sync_takeout_subscriptions(conn: sqlite3.Connection, takeout_path: Path) -> None:
    existing = conn.execute("SELECT COUNT(*) FROM channels WHERE subscribed = 1").fetchone()[0]
    if existing:
        return
    rows = load_takeout_subscriptions(takeout_path)
    if not rows:
        return
    now = utc_now()
    for row in rows:
        channel_id = upsert_channel(
            conn,
            row["channel_id"],
            title=row["title"],
            url=row["channel_url"],
            source="takeout_subscriptions",
            updated_at=now,
        )
        if channel_id:
            conn.execute("UPDATE channels SET subscribed = 1 WHERE channel_id = ?", (channel_id,))


def load_takeout_history_source(takeout_path: Path, requested_key: str = "") -> tuple[str, str]:
    zip_path = find_takeout_zip(takeout_path)
    if zip_path:
        history_key = requested_key or takeout_key_from_path(zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            watch_text = read_zip_member_text(zf, "history/watch-history.json")
            if not watch_text:
                watch_text = read_zip_member_text(zf, "history/watch-history.html")
        if not watch_text:
            raise SystemExit(f"Takeout watch history file not found in {zip_path}")
        if not history_key:
            raise SystemExit(f"Could not derive Takeout history key from {zip_path.name}")
        return history_key, watch_text

    history_dir = takeout_path / "history"
    watch_file = history_dir / "watch-history.html"
    if not watch_file.exists():
        raise SystemExit(f"Takeout watch history file not found in {history_dir}")
    history_key = requested_key or takeout_key_from_path(takeout_path) or takeout_path.name
    watch_text = watch_file.read_text(encoding="utf-8", errors="replace") if watch_file.exists() else ""
    return history_key, watch_text


def load_takeout_history_sources(takeout_path: Path, requested_key: str = "") -> list[tuple[Path | None, str, str]]:
    if requested_key:
        source_zip = find_takeout_zip(takeout_path)
        source_path = source_zip or takeout_path
        history_key, watch_text = load_takeout_history_source(source_path, requested_key)
        return [(source_zip, history_key, watch_text)]
    zips = find_takeout_zips(takeout_path)
    if zips:
        sources = []
        for zip_path in zips:
            history_key, watch_text = load_takeout_history_source(zip_path, "")
            sources.append((zip_path, history_key, watch_text))
        return sources
    history_key, watch_text = load_takeout_history_source(takeout_path, "")
    return [(None, history_key, watch_text)]


def existing_takeout_history_event_keys(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    return {
        history_instant_key(row["video_id"] or "", row["watched_at"] or "")
        for row in conn.execute(
            """
            SELECT video_id, watched_at
            FROM history_events
            WHERE takeout_history_key IS NOT NULL
            """
        )
    }


def takeout_import_message(stats: dict[str, Any]) -> str:
    keys = ", ".join(stats.get("imported_keys") or [])
    return (
        f"Takeout import: {stats['inserted_watch_rows']} new, "
        f"{stats['duplicate_watch_rows']} duplicates skipped, "
        f"{stats['total_watch_rows']} rows scanned from {keys}; "
        f"{stats['reconciled_rows']} reconciled, {stats['matched_rows']} matched"
    )


def import_history(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db)
    takeout_path = Path(args.takeout)
    requested_key = getattr(args, "history_key", "") or ""
    sources = load_takeout_history_sources(takeout_path, requested_key)

    conn = connect(db_path)
    imported_at = utc_now()
    total_watch_rows = 0
    inserted_watch_rows = 0
    duplicate_watch_rows = 0
    distinct_video_ids: set[str] = set()
    imported_keys: list[str] = []
    playlist_stats = {
        "source_found": 0,
        "playlists": 0,
        "items": 0,
        "unmatched": 0,
        "tombstoned": 0,
    }
    with conn:
        sync_takeout_subscriptions(conn, takeout_path)
        for zip_path, _history_key, _watch_text in reversed(sources):
            if zip_path:
                stats = import_takeout_playlists_zip(conn, zip_path)
                if stats["source_found"]:
                    playlist_stats = stats
                    break
        existing_events = existing_takeout_history_event_keys(conn)
        for _source_path, history_key, watch_text in sources:
            imported_keys.append(history_key)
            watch_rows = parse_takeout_watch_history_text(watch_text) if watch_text else []
            total_watch_rows += len(watch_rows)
            for position, row in enumerate(watch_rows, start=1):
                watched_at_iso = takeout_watch_datetime(row["watched_at"])
                event_key = history_instant_key(row["video_id"], watched_at_iso)
                if event_key in existing_events:
                    duplicate_watch_rows += 1
                    continue
                row_hash = history_row_hash(row)
                if row["video_id"]:
                    distinct_video_ids.add(row["video_id"])
                channel_id = upsert_channel(
                    conn,
                    row.get("channel_id") or youtube_channel_id_from_url(row["channel_url"]),
                    title=row["channel"],
                    url=row["channel_url"],
                    first_seen_at=watched_at_iso or None,
                    source="takeout_history",
                    updated_at=imported_at,
                )
                upsert_video(
                    conn,
                    row["video_id"],
                    title=row["title"],
                    channel_id=channel_id,
                    channel_title=row["channel"],
                    channel_url=row["channel_url"],
                    source="takeout",
                    updated_at=imported_at,
                )
                row_key = f"{row_hash}:{position}"
                conn.execute(
                    """
                    INSERT INTO history_events(
                      event_id, video_id, watched_at, watch_date, time_precision,
                      source_type, match_type, takeout_history_key, takeout_row_key,
                      imported_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'takeout', 'takeout_only', ?, ?, ?, ?)
                    """,
                    (
                        f"takeout:{history_key}:{row_key}",
                        row["video_id"],
                        watched_at_iso,
                        watched_at_iso[:10] if watched_at_iso else None,
                        "exact" if watched_at_iso else "unknown",
                        history_key,
                        row_key,
                        imported_at,
                        imported_at,
                    ),
                )
                inserted_watch_rows += 1
                existing_events.add(event_key)
        timezone_name = effective_display_timezone(getattr(args, "config_data", {}))
        merge_stats = merge_my_activity_takeout_duplicates(conn)
        stats = rebuild_history_reconciliation(conn, timezone_name)
    conn.close()
    result = {
        "total_watch_rows": total_watch_rows,
        "inserted_watch_rows": inserted_watch_rows,
        "duplicate_watch_rows": duplicate_watch_rows,
        "distinct_video_ids": len(distinct_video_ids),
        "imported_keys": imported_keys,
        "reconciled_rows": stats["rows"],
        "matched_rows": stats["matched"],
        "merged_my_activity_rows": merge_stats["merged"],
        "ambiguous_my_activity_rows": merge_stats["ambiguous"],
        "playlist_stats": playlist_stats,
    }
    print(
        f"Imported {inserted_watch_rows} new watch history rows from {', '.join(imported_keys)} "
        f"({duplicate_watch_rows} duplicates skipped, {total_watch_rows} rows scanned, "
        f"{len(distinct_video_ids)} distinct videos). "
        f"Reconciled {stats['rows']} rows ({stats['matched']} matched). "
        f"Loaded {playlist_stats['playlists']} playlists and {playlist_stats['items']} playlist items."
    )
    return result
