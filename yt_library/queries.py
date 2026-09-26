"""Read models for the library and history web views."""

from __future__ import annotations

import calendar
import re
import sqlite3
import urllib.parse
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .annotations import (
    annotation_presence,
    annotation_search_matches,
    attach_annotations,
)
from .core import (
    archivarix_media_url,
    history_match_type_label,
    history_source_type_label,
    history_time_quality_label,
    history_time_quality_note,
    playlist_match_type_label,
    playlist_match_type_note,
    preferred_youtube_channel_reference,
    preferred_youtube_channel_url,
    video_availability_category as _video_availability_category,
    video_availability_category_sql as _video_availability_category_sql,
    wayback_video_url,
    youtube_channel_ref_from_url,
    youtube_playlist_url,
    youtube_video_url,
)


def _history_event_order_sql(alias: str = "he") -> str:
    watch_date = (
        f"COALESCE(NULLIF({alias}.watch_date, ''), "
        f"substr({alias}.watched_at, 1, 10), '')"
    )
    return (
        f"{watch_date} DESC, "
        f"CASE WHEN {alias}.youtube_ordinal IS NULL THEN 1 ELSE 0 END, "
        f"{alias}.youtube_ordinal, "
        f"{alias}.watched_at DESC, "
        f"{alias}.event_id"
    )



def clean_playlist_owner_name(value: str) -> str:
    value = (value or "").strip()
    return value[3:].strip() if value.lower().startswith("by ") else value


def library_bootstrap_data(conn: sqlite3.Connection) -> dict[str, Any]:
    groups = [
        dict(row)
        for row in conn.execute("SELECT * FROM groups ORDER BY COALESCE(parent_key, ''), position, name")
    ]
    memberships = [
        dict(row)
        for row in conn.execute(
            """
            SELECT gp.group_key, gp.playlist_id, gp.position
            FROM group_playlists gp
            JOIN playlists p ON p.playlist_id = gp.playlist_id
            ORDER BY gp.group_key, gp.position, p.title COLLATE NOCASE
            """
        )
    ]
    counts = dict(
        conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM videos) AS videos,
              (SELECT COUNT(*) FROM clips) AS clips,
              (SELECT COUNT(*) FROM playlists) AS playlists,
              (SELECT COUNT(*) FROM history_events) AS history,
              (SELECT COUNT(*) FROM channels) AS channels
            """
        ).fetchone()
    )
    return {"groups": groups, "memberships": memberships, "counts": counts}


def _video_collection_category_sql(
    *,
    video_id: str = "video_id",
    availability: str = "availability",
    is_playable: str = "is_playable",
    source_quality: str = "source_quality",
    match_type: str = "match_type",
) -> str:
    availability_category_sql = _video_availability_category_sql(
        video_id=video_id,
        availability=availability,
        is_playable=is_playable,
    )
    return f"""
        CASE
          WHEN {source_quality} = 'takeout'
           AND {match_type} = 'ambiguous_hidden_candidate'
            THEN 'removed'
          ELSE ({availability_category_sql})
        END
    """


def _playlist_unavailable_counts_ctes() -> str:
    category_sql = _video_collection_category_sql(
        video_id="pi.video_id",
        availability=(
            "CASE WHEN pi.video_id IS NULL "
            "THEN pi.unavailable_kind ELSE v.availability END"
        ),
        is_playable="v.is_playable",
        source_quality="pi.source_quality",
        match_type="pi.match_type",
    )
    return f"""
        playlist_video_categories AS (
          SELECT pi.playlist_id,
                 CASE
                   WHEN COALESCE(pi.video_id, '') <> '' THEN 'video:' || pi.video_id
                   ELSE 'slot:' || pi.playlist_id || ':' || pi.position
                 END AS count_key,
                 {category_sql} AS collection_category
          FROM playlist_items pi
          LEFT JOIN videos v ON v.video_id = pi.video_id
        ),
        playlist_unavailable_counts AS (
          SELECT playlist_id, COUNT(DISTINCT count_key) AS unavailable_count
          FROM playlist_video_categories
          WHERE collection_category = 'unavailable'
          GROUP BY playlist_id
        )
    """


def _attach_playlist_collaborators(
    conn: sqlite3.Connection,
    playlists: list[dict[str, Any]],
) -> None:
    playlist_ids = [
        str(playlist.get("playlist_id") or "").strip()
        for playlist in playlists
        if str(playlist.get("playlist_id") or "").strip()
    ]
    collaborators_by_playlist: dict[str, list[dict[str, Any]]] = {
        playlist_id: [] for playlist_id in playlist_ids
    }
    if playlist_ids:
        placeholders = ", ".join("?" for _ in playlist_ids)
        for row in conn.execute(
            f"""
            SELECT pc.playlist_id, pc.position,
                   ch.channel_id, ch.title, ch.aliases, ch.thumbnail_path, ch.status
            FROM playlist_collaborators pc
            JOIN channels ch ON ch.channel_id = pc.channel_id
            WHERE pc.playlist_id IN ({placeholders})
            ORDER BY pc.playlist_id, pc.position, ch.channel_id
            """,
            playlist_ids,
        ):
            collaborator = dict(row)
            collaborator["channel_reference"] = preferred_youtube_channel_reference(
                collaborator.get("channel_id", ""),
                collaborator.get("aliases", ""),
            )
            collaborator["channel_url"] = preferred_youtube_channel_url(
                collaborator.get("channel_id", ""),
                collaborator.get("aliases", ""),
            )
            collaborators_by_playlist[row["playlist_id"]].append(collaborator)
    for playlist in playlists:
        playlist["collaborators"] = collaborators_by_playlist.get(
            str(playlist.get("playlist_id") or ""),
            [],
        )


def _attach_channel_featured_channels(
    conn: sqlite3.Connection,
    channels: list[dict[str, Any]],
) -> None:
    channel_ids = list(
        dict.fromkeys(
            str(channel.get("channel_id") or "").strip()
            for channel in channels
            if str(channel.get("channel_id") or "").strip()
        )
    )
    featured_by_owner: dict[str, list[dict[str, Any]]] = {
        channel_id: [] for channel_id in channel_ids
    }
    if channel_ids:
        placeholders = ", ".join("?" for _ in channel_ids)
        for row in conn.execute(
            f"""
            SELECT cfc.owner_channel_id, cfc.featured_channel_id,
                   cfc.title AS observed_title,
                   cfc.channel_reference AS observed_reference,
                   cfc.position,
                   ch.channel_id AS cataloged_channel_id,
                   COALESCE(ch.title, '') AS cataloged_title,
                   COALESCE(ch.aliases, '') AS cataloged_aliases
            FROM channel_featured_channels cfc
            LEFT JOIN channels ch ON ch.channel_id = cfc.featured_channel_id
            WHERE cfc.owner_channel_id IN ({placeholders})
            ORDER BY cfc.owner_channel_id, cfc.position, cfc.featured_channel_id
            """,
            channel_ids,
        ):
            item = dict(row)
            cataloged = bool(item.pop("cataloged_channel_id"))
            cataloged_title = str(item.pop("cataloged_title") or "").strip()
            cataloged_aliases = str(item.pop("cataloged_aliases") or "").strip()
            observed_title = str(item.pop("observed_title") or "").strip()
            observed_reference = str(item.pop("observed_reference") or "").strip()
            featured_channel_id = str(item.get("featured_channel_id") or "").strip()
            preferred_reference = (
                preferred_youtube_channel_reference(
                    featured_channel_id,
                    cataloged_aliases,
                )
                if cataloged
                else ""
            )
            external_reference = (
                preferred_reference or observed_reference or featured_channel_id
            )
            item["title"] = (
                cataloged_title
                or observed_title
                or featured_channel_id
            )
            item["cataloged"] = cataloged
            item["preferred_reference"] = preferred_reference
            item["url"] = preferred_youtube_channel_url(external_reference)
            featured_by_owner[row["owner_channel_id"]].append(item)
    for channel in channels:
        channel["featured_channels"] = featured_by_owner.get(
            str(channel.get("channel_id") or ""),
            [],
        )


def _playlist_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            WITH {_playlist_unavailable_counts_ctes()}
            SELECT p.*,
                   COALESCE(s.video_count, 0) AS scanned_video_count,
                   COALESCE(puc.unavailable_count, 0) AS unavailable_count,
                   s.scanned_at,
                   COALESCE(s.scan_status, '') AS scan_status,
                   COALESCE(ch.title, '') AS owner_channel_title,
                   COALESCE(ch.aliases, '') AS owner_channel_aliases,
                   COALESCE(ch.thumbnail_path, '') AS owner_channel_thumbnail_path,
                   COALESCE(ch.status, '') AS owner_channel_status
            FROM playlists p
            LEFT JOIN playlist_scans s ON s.playlist_id = p.playlist_id
            LEFT JOIN playlist_unavailable_counts puc
              ON puc.playlist_id = p.playlist_id
            LEFT JOIN channels ch ON ch.channel_id = p.owner_channel_id
            ORDER BY p.title COLLATE NOCASE
            """
        )
    ]
    for playlist in rows:
        playlist["owner_channel_title"] = clean_playlist_owner_name(
            playlist.get("owner_channel_title") or ""
        )
        playlist["url"] = youtube_playlist_url(playlist.get("playlist_id", ""))
        playlist["owner_channel_reference"] = preferred_youtube_channel_reference(
            playlist.get("owner_channel_id", ""),
            playlist.get("owner_channel_aliases", ""),
        )
        playlist["owner_channel_url"] = preferred_youtube_channel_url(
            playlist.get("owner_channel_id", ""),
            playlist.get("owner_channel_aliases", ""),
        )
    _attach_playlist_collaborators(conn, rows)
    attach_annotations(conn, "playlist", rows)
    return rows


def _playlist_availability_category(playlist: dict[str, Any]) -> str:
    if str(playlist.get("fetch_status") or "") == "unavailable":
        return "unavailable"
    visibility = str(playlist.get("visibility") or "").strip().lower()
    if visibility in {"private", "public", "unlisted"}:
        return visibility
    return "unknown"


def _playlist_ownership_category(playlist: dict[str, Any]) -> str:
    ownership = str(playlist.get("ownership") or "unknown").strip().lower()
    return ownership if ownership in {"mine", "others"} else "ownership_unknown"


def _playlist_list_category(playlist: dict[str, Any]) -> str:
    availability = _playlist_availability_category(playlist)
    if availability != "unknown":
        return availability
    if _playlist_ownership_category(playlist) == "others":
        return "others"
    return "unknown"


def playlist_list_data(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    owner_channel_id: str = "",
    visibilities: set[str] | None = None,
    include_unavailable: bool = False,
    sort: str = "title",
    unavailable_only: bool = False,
    group_key: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    params: dict[str, Any] = {
        "pattern": _omni_like_pattern(query.strip()),
        "owner_channel_id": owner_channel_id.strip(),
        "group_key": group_key.strip(),
        "unavailable_only": int(unavailable_only),
    }
    filtered_cte = f"""
        WITH {_playlist_unavailable_counts_ctes()},
        playlist_rows AS (
          SELECT p.*,
                 COALESCE(s.video_count, 0) AS scanned_video_count,
                 COALESCE(puc.unavailable_count, 0) AS unavailable_count,
                 s.scanned_at,
                 COALESCE(s.scan_status, '') AS scan_status,
                 CASE
                   WHEN lower(trim(COALESCE(ch.title, ''))) LIKE 'by %'
                     THEN trim(substr(trim(ch.title), 4))
                   ELSE trim(COALESCE(ch.title, ''))
                 END AS owner_channel_title,
                 COALESCE(ch.aliases, '') AS owner_channel_aliases,
                 COALESCE(ch.thumbnail_path, '') AS owner_channel_thumbnail_path,
                 COALESCE(ch.status, '') AS owner_channel_status
          FROM playlists p
          LEFT JOIN playlist_scans s ON s.playlist_id = p.playlist_id
          LEFT JOIN playlist_unavailable_counts puc
            ON puc.playlist_id = p.playlist_id
          LEFT JOIN channels ch ON ch.channel_id = p.owner_channel_id
          WHERE (
              :pattern = '%%'
              OR lower(
                   p.title || ' ' || COALESCE(ch.title, '') || ' ' ||
                   COALESCE(p.owner_channel_id, '') || ' ' || p.visibility || ' ' ||
                   p.description || ' ' || p.playlist_id
                 ) LIKE :pattern ESCAPE '\\'
            )
            AND (
              :owner_channel_id = ''
              OR p.owner_channel_id = :owner_channel_id
            )
            AND (
              :group_key = ''
              OR EXISTS (
                SELECT 1
                FROM group_playlists gp
                WHERE gp.playlist_id = p.playlist_id
                  AND (
                    gp.group_key = :group_key
                    OR gp.group_key IN (
                      SELECT group_key FROM groups WHERE parent_key = :group_key
                    )
                  )
              )
            )
            AND (:unavailable_only = 0 OR COALESCE(puc.unavailable_count, 0) > 0)
        ),
        categorized AS (
          SELECT playlist_rows.*,
                 CASE
                   WHEN fetch_status = 'unavailable' THEN 'unavailable'
                   WHEN lower(trim(visibility)) IN ('private', 'public', 'unlisted')
                     THEN lower(trim(visibility))
                   WHEN ownership = 'others'
                     THEN 'others'
                   ELSE 'unknown'
                 END AS list_category
          FROM playlist_rows
        )
    """
    categories = ("private", "public", "unlisted", "others", "unknown", "unavailable")
    count_rows = conn.execute(
        filtered_cte
        + "SELECT list_category, COUNT(*) AS count FROM categorized GROUP BY list_category",
        params,
    ).fetchall()
    counts = {category: 0 for category in categories}
    counts.update({row["list_category"]: row["count"] for row in count_rows})

    category_clause = ""
    if visibilities is not None:
        selected_categories = set(visibilities)
        if include_unavailable:
            selected_categories.add("unavailable")
        selected = sorted(selected_categories & set(categories))
        if selected:
            placeholders = ", ".join(f":category_{index}" for index in range(len(selected)))
            category_clause = f"WHERE list_category IN ({placeholders})"
            params.update(
                {f"category_{index}": value for index, value in enumerate(selected)}
            )
        else:
            category_clause = "WHERE 0"
    elif not include_unavailable:
        category_clause = "WHERE list_category <> 'unavailable'"
    total = conn.execute(
        filtered_cte + f"SELECT COUNT(*) FROM categorized {category_clause}",
        params,
    ).fetchone()[0]
    if total and offset >= total:
        offset = ((total - 1) // limit) * limit
    order_by = {
        "title_desc": "COALESCE(NULLIF(title, ''), playlist_id) COLLATE NOCASE DESC, playlist_id",
        "newest_updated": (
            "COALESCE(last_changed_at, '') DESC, "
            "COALESCE(NULLIF(title, ''), playlist_id) COLLATE NOCASE, playlist_id"
        ),
        "oldest_updated": (
            "COALESCE(last_changed_at, ''), "
            "COALESCE(NULLIF(title, ''), playlist_id) COLLATE NOCASE, playlist_id"
        ),
        "most_videos": (
            "scanned_video_count DESC, "
            "COALESCE(NULLIF(title, ''), playlist_id) COLLATE NOCASE, playlist_id"
        ),
        "most_unavailable": (
            "unavailable_count DESC, "
            "COALESCE(NULLIF(title, ''), playlist_id) COLLATE NOCASE, playlist_id"
        ),
    }.get(
        sort,
        "COALESCE(NULLIF(title, ''), playlist_id) COLLATE NOCASE, playlist_id",
    )
    params.update({"limit": limit, "offset": offset})
    rows = [
        dict(row)
        for row in conn.execute(
            filtered_cte
            + f"""
              SELECT * FROM categorized
              {category_clause}
              ORDER BY {order_by}
              LIMIT :limit OFFSET :offset
            """,
            params,
        )
    ]
    for playlist in rows:
        playlist.pop("list_category", None)
        playlist["url"] = youtube_playlist_url(playlist.get("playlist_id", ""))
        playlist["owner_channel_reference"] = preferred_youtube_channel_reference(
            playlist.get("owner_channel_id", ""),
            playlist.get("owner_channel_aliases", ""),
        )
        playlist["owner_channel_url"] = preferred_youtube_channel_url(
            playlist.get("owner_channel_id", ""),
            playlist.get("owner_channel_aliases", ""),
        )
    _attach_playlist_collaborators(conn, rows)
    attach_annotations(conn, "playlist", rows)
    return {
        "results": rows,
        "total": total,
        "counts": counts,
        "limit": limit,
        "offset": offset,
    }


def playlist_detail_data(conn: sqlite3.Connection, playlist_id: str) -> dict[str, Any] | None:
    return next((row for row in _playlist_rows(conn) if row.get("playlist_id") == playlist_id), None)


def _video_candidate_query(
    *,
    scope: str,
    playlist_id: str = "",
    channel_id: str = "",
    query: str = "",
    search_fields: set[str] | None = None,
    has_video_search_matches: bool = False,
    has_annotation_search_matches: bool = False,
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {"query": f"%{_omni_like_pattern(query.strip())[1:-1]}%"}
    active_search_fields = {"titles", "descriptions"} if search_fields is None else set(search_fields)
    search_clauses = []
    if "titles" in active_search_fields:
        title_haystack = (
            "COALESCE(v.title, '') || ' ' || COALESCE(v.video_id, '')"
            if scope == "channel"
            else "COALESCE(v.title, '') || ' ' || COALESCE(ch.title, '') || ' ' || "
            "COALESCE(v.video_id, '')"
        )
        search_clauses.append(
            f"lower({title_haystack}) LIKE :query ESCAPE '\\'"
        )
    if "descriptions" in active_search_fields:
        search_clauses.append("lower(COALESCE(v.description, '')) LIKE :query ESCAPE '\\'")
    if has_video_search_matches:
        search_clauses.append(
            "EXISTS (SELECT 1 FROM temp.video_collection_search_matches matches "
            "WHERE matches.video_id = v.video_id)"
        )
    if has_annotation_search_matches:
        search_clauses.append(
            "EXISTS (SELECT 1 FROM temp.video_collection_annotation_search_matches matches "
            "WHERE matches.video_id = v.video_id)"
        )
    query_match_sql = " OR ".join(search_clauses) or "0"
    query_clause = f"AND (:query = '%%' OR ({query_match_sql}))"
    history_scope_clause = ""
    if playlist_id:
        params["playlist_id"] = playlist_id
        history_scope_clause = """
          WHERE video_id IN (
            SELECT video_id
            FROM playlist_items
            WHERE playlist_id = :playlist_id AND video_id IS NOT NULL
          )
        """
    elif channel_id:
        params["channel_id"] = channel_id
        history_scope_clause = (
            """
              WHERE video_id IN (
                SELECT video_id
                FROM videos
                WHERE channel_id = :channel_id
              )
            """
            if scope == "channel"
            else """
              WHERE video_id IN (
                SELECT pi.video_id
                FROM playlist_items pi
                JOIN videos candidate_video ON candidate_video.video_id = pi.video_id
                WHERE candidate_video.channel_id = :channel_id
              )
            """
        )
    history_cte = f"""
      WITH history_stats AS (
        SELECT video_id,
               COUNT(*) AS watch_count,
               MAX(COALESCE(watched_at, watch_date)) AS latest_watch_at,
               MAX(watch_progress_percent) AS watch_progress_percent
        FROM history_events
        {history_scope_clause}
        GROUP BY video_id
      )
    """
    if scope in {"liked", "channel"}:
        if scope == "liked":
            source_clause = "upper(v.reaction) = 'LIKE'"
        else:
            params["channel_id"] = channel_id
            source_clause = "v.channel_id = :channel_id" if channel_id else "0"
        sql = history_cte + f"""
          SELECT v.video_id, v.title, v.title AS metadata_title, v.upload_date AS metadata_upload_date,
                 v.updated_at, '' AS playlist_id, '' AS playlist_title, 0 AS position,
                 '' AS membership_state, '' AS unavailable_kind, '' AS source_quality,
                 '' AS match_type, '' AS match_confidence, '' AS added_at,
                 v.is_playable, v.availability, v.reaction, v.uploader_category,
                 v.note,
                 v.video_type, v.broadcast_status, v.ai_disclosure,
                 COALESCE(hs.watch_progress_percent, 0) AS watch_progress_percent,
                 COALESCE(hs.watch_count, 0) AS watch_count,
                 COALESCE(hs.latest_watch_at, '') AS latest_watch_at,
                 100 AS completeness_score
          FROM videos v
          LEFT JOIN channels ch ON ch.channel_id = v.channel_id
          LEFT JOIN history_stats hs ON hs.video_id = v.video_id
          WHERE {source_clause}
          {query_clause}
        """
    else:
        where = []
        if playlist_id:
            where.append("pi.playlist_id = :playlist_id")
            params["playlist_id"] = playlist_id
        if channel_id:
            where.append("v.channel_id = :channel_id")
            params["channel_id"] = channel_id
        sql = history_cte + f"""
          SELECT pi.video_id, COALESCE(v.title, '') AS title,
                 COALESCE(v.title, '') AS metadata_title,
                 COALESCE(v.upload_date, '') AS metadata_upload_date,
                 COALESCE(v.updated_at, pi.updated_at) AS updated_at,
                 pi.playlist_id, p.title AS playlist_title, pi.position,
                 pi.membership_state, pi.unavailable_kind, pi.source_quality,
                 pi.match_type, pi.match_confidence, COALESCE(pi.added_at, '') AS added_at,
                 v.is_playable, COALESCE(v.reaction, '') AS reaction,
                 COALESCE(v.uploader_category, '') AS uploader_category,
                 COALESCE(v.note, '') AS note,
                 COALESCE(v.video_type, '') AS video_type,
                 v.broadcast_status,
                 v.ai_disclosure,
                 COALESCE(hs.watch_progress_percent, 0) AS watch_progress_percent,
                 CASE WHEN pi.video_id IS NULL THEN pi.unavailable_kind ELSE v.availability END AS availability,
                 COALESCE(hs.watch_count, 0) AS watch_count,
                 COALESCE(hs.latest_watch_at, '') AS latest_watch_at,
                 (CASE WHEN v.thumbnail_path != '' THEN 12 ELSE 0 END
                   + CASE WHEN v.title != '' THEN 8 ELSE 0 END
                   + CASE WHEN ch.title != '' THEN 5 ELSE 0 END
                   + CASE WHEN v.description != '' THEN 4 ELSE 0 END
                   + CASE WHEN COALESCE(v.is_playable, 0) = 1 THEN 2 ELSE 0 END) AS completeness_score
          FROM playlist_items pi
          JOIN playlists p ON p.playlist_id = pi.playlist_id
          LEFT JOIN videos v ON v.video_id = pi.video_id
          LEFT JOIN channels ch ON ch.channel_id = v.channel_id
          LEFT JOIN history_stats hs ON hs.video_id = pi.video_id
          WHERE {' AND '.join(where) if where else '1 = 1'}
          {query_clause}
        """
    return sql, params


VIDEO_AVAILABILITY_CATEGORIES = (
    "public",
    "unlisted",
    "private",
    "members_only",
    "unavailable",
    "unknown",
)
VIDEO_COMPLETION_CATEGORIES = (
    "complete",
    "partial",
    "partial_below_minimum",
    "unknown",
    "never_watched",
)
VIDEO_REACTION_CATEGORIES = ("none", "liked", "disliked")
VIDEO_TYPE_CATEGORIES = ("video", "short", "livestream", "movie", "unknown")
BROADCAST_STATUS_CATEGORIES = ("live", "ended", "upcoming", "unknown")
AI_DISCLOSURE_CATEGORIES = ("made_with_ai", "no_disclosure", "unknown")
NO_UPLOADER_CATEGORY_FILTER = "__no_category__"


def _bounded_partial_min_percent(value: Any) -> int:
    try:
        return max(1, min(99, int(value)))
    except (TypeError, ValueError):
        return 1


def _video_collection_category(item: dict[str, Any]) -> str:
    if (
        item.get("source_quality") == "takeout"
        and item.get("match_type") == "ambiguous_hidden_candidate"
    ):
        return "removed"
    return _video_availability_category(item)


def _video_type_category(item: Mapping[str, Any]) -> str:
    video_type = str(item.get("video_type") or "").strip().lower()
    return video_type if video_type in VIDEO_TYPE_CATEGORIES[:-1] else "unknown"


def _video_broadcast_status_category(item: Mapping[str, Any]) -> str:
    if _video_type_category(item) != "livestream":
        return "not_applicable"
    status = item.get("broadcast_status")
    if status is None:
        return "unknown"
    normalized = str(status).strip().lower()
    return normalized if normalized in BROADCAST_STATUS_CATEGORIES[:-1] else "unknown"


def _video_ai_disclosure_category(item: Mapping[str, Any]) -> str:
    disclosure = item.get("ai_disclosure")
    if disclosure is None:
        return "unknown"
    return "made_with_ai" if bool(disclosure) else "no_disclosure"


def _video_completion_category(item: dict[str, Any]) -> str:
    if not item.get("video_id") or item.get("virtual_video"):
        return "unknown"
    progress = int(item.get("watch_progress_percent") or 0)
    if progress >= 100:
        return "complete"
    if progress > 0:
        return "partial"
    if int(item.get("watch_count") or 0) > 0:
        return "unknown"
    return "never_watched"


def _video_matches_completion_filter(
    item: dict[str, Any],
    selected_filters: set[str],
    partial_min_percent: int,
) -> bool:
    return (
        _video_completion_filter_category(item, partial_min_percent)
        in selected_filters
    )


def _video_completion_filter_category(
    item: dict[str, Any],
    partial_min_percent: int,
) -> str:
    category = _video_completion_category(item)
    if (
        category == "partial"
        and int(item.get("watch_progress_percent") or 0) < partial_min_percent
    ):
        return "partial_below_minimum"
    return category


def video_collection_data(
    conn: sqlite3.Connection,
    *,
    scope: str = "playlist",
    playlist_id: str = "",
    channel_id: str = "",
    query: str = "",
    search_fields: set[str] | None = None,
    include_public: bool = True,
    include_unlisted: bool = True,
    include_private: bool = True,
    include_unavailable: bool = True,
    include_members_only: bool | None = None,
    include_unknown: bool = True,
    include_removed: bool = True,
    duplicates_only: bool = False,
    completion_filters: set[str] | None = None,
    reaction_filters: set[str] | None = None,
    video_type_filters: set[str] | None = None,
    broadcast_status_filters: set[str] | None = None,
    ai_disclosure_filters: set[str] | None = None,
    uploader_category_filters: set[str] | None = None,
    note_filters: set[str] | None = None,
    included_video_ids: Collection[str] | None = None,
    excluded_video_ids: Collection[str] = (),
    video_facet_memberships: Mapping[str, Collection[str]] | None = None,
    video_search_match_ids: Collection[str] = (),
    video_search_match_memberships: Mapping[str, Collection[str]] | None = None,
    partial_min_percent: int = 1,
    sort: str = "newest_added",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    active_video_search_match_memberships = {
        str(plugin_id): {
            str(video_id).strip()
            for video_id in video_ids
            if str(video_id).strip()
        }
        for plugin_id, video_ids in (video_search_match_memberships or {}).items()
    }
    active_search_match_ids = {
        str(video_id).strip()
        for video_id in video_search_match_ids
        if str(video_id).strip()
    }
    for video_ids in active_video_search_match_memberships.values():
        active_search_match_ids.update(video_ids)
    annotation_matches = annotation_search_matches(
        conn,
        query,
        search_notes=search_fields is None or "notes" in search_fields,
        search_tags=search_fields is None or "tags" in search_fields,
        entity_kinds={"video"},
    )["video"]
    conn.execute("DROP TABLE IF EXISTS temp.video_collection_annotation_search_matches")
    if query.strip() and annotation_matches:
        conn.execute(
            """
            CREATE TEMP TABLE video_collection_annotation_search_matches(
              video_id TEXT PRIMARY KEY
            ) WITHOUT ROWID
            """
        )
        conn.executemany(
            """
            INSERT INTO temp.video_collection_annotation_search_matches(video_id)
            VALUES (?)
            """,
            ((video_id,) for video_id in annotation_matches),
        )
    conn.execute("DROP TABLE IF EXISTS temp.video_collection_search_matches")
    if query.strip() and active_search_match_ids:
        conn.execute(
            "CREATE TEMP TABLE video_collection_search_matches(video_id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        conn.executemany(
            "INSERT INTO temp.video_collection_search_matches(video_id) VALUES (?)",
            ((video_id,) for video_id in active_search_match_ids),
        )

    active_included_video_ids = (
        None
        if included_video_ids is None
        else {
            str(video_id).strip()
            for video_id in included_video_ids
            if str(video_id).strip()
        }
    )
    conn.execute("DROP TABLE IF EXISTS temp.video_collection_included")
    if active_included_video_ids is not None:
        conn.execute(
            "CREATE TEMP TABLE video_collection_included(video_id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        conn.executemany(
            "INSERT INTO temp.video_collection_included(video_id) VALUES (?)",
            ((video_id,) for video_id in active_included_video_ids),
        )

    active_excluded_video_ids = {
        str(video_id).strip()
        for video_id in excluded_video_ids
        if str(video_id).strip()
    }
    conn.execute("DROP TABLE IF EXISTS temp.video_collection_excluded")
    if active_excluded_video_ids:
        conn.execute(
            "CREATE TEMP TABLE video_collection_excluded(video_id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        conn.executemany(
            "INSERT INTO temp.video_collection_excluded(video_id) VALUES (?)",
            ((video_id,) for video_id in active_excluded_video_ids),
        )

    candidate_sql, params = _video_candidate_query(
        scope=scope,
        playlist_id=playlist_id,
        channel_id=channel_id,
        query=query,
        search_fields=search_fields,
        has_video_search_matches=bool(query.strip() and active_search_match_ids),
        has_annotation_search_matches=bool(query.strip() and annotation_matches),
    )
    selected_categories = {
        category
        for category, enabled in {
            "public": include_public,
            "unlisted": include_unlisted,
            "private": include_private,
            "unavailable": include_unavailable,
            "members_only": (
                include_unavailable
                if include_members_only is None
                else include_members_only
            ),
            "unknown": include_unknown,
            "removed": include_removed,
        }.items()
        if enabled
    }
    selected_completion_filters = (
        set(VIDEO_COMPLETION_CATEGORIES)
        if completion_filters is None
        else set(completion_filters) & set(VIDEO_COMPLETION_CATEGORIES)
    )
    selected_reaction_filters = (
        set(VIDEO_REACTION_CATEGORIES)
        if reaction_filters is None
        else set(reaction_filters) & set(VIDEO_REACTION_CATEGORIES)
    )
    selected_video_type_filters = (
        set(VIDEO_TYPE_CATEGORIES)
        if video_type_filters is None
        else set(video_type_filters) & set(VIDEO_TYPE_CATEGORIES)
    )
    selected_broadcast_status_filters = (
        set(BROADCAST_STATUS_CATEGORIES)
        if broadcast_status_filters is None
        else set(broadcast_status_filters) & set(BROADCAST_STATUS_CATEGORIES)
    )
    selected_ai_disclosure_filters = (
        set(AI_DISCLOSURE_CATEGORIES)
        if ai_disclosure_filters is None
        else set(ai_disclosure_filters) & set(AI_DISCLOSURE_CATEGORIES)
    )
    selected_uploader_category_filters = (
        None
        if uploader_category_filters is None
        else {
            str(category).strip()
            for category in uploader_category_filters
            if str(category).strip()
        }
    )
    selected_note_filters = (
        set(OMNI_SEARCH_NOTE_FILTERS)
        if note_filters is None
        else set(note_filters) & set(OMNI_SEARCH_NOTE_FILTERS)
    )
    partial_min_percent = _bounded_partial_min_percent(partial_min_percent)
    params["partial_min_percent"] = partial_min_percent
    collection_category_sql = _video_collection_category_sql()
    availability_category_sql = _video_availability_category_sql()
    completion_category_sql = """
        CASE
          WHEN COALESCE(video_id, '') = '' THEN 'unknown'
          WHEN COALESCE(watch_progress_percent, 0) >= 100 THEN 'complete'
          WHEN COALESCE(watch_progress_percent, 0) > 0
           AND COALESCE(watch_progress_percent, 0) < :partial_min_percent
            THEN 'partial_below_minimum'
          WHEN COALESCE(watch_progress_percent, 0) > 0 THEN 'partial'
          WHEN COALESCE(watch_count, 0) > 0 THEN 'unknown'
          ELSE 'never_watched'
        END
    """
    reaction_category_sql = """
        CASE upper(COALESCE(reaction, ''))
          WHEN 'LIKE' THEN 'liked'
          WHEN 'DISLIKE' THEN 'disliked'
          ELSE 'none'
        END
    """
    uploader_category_sql = """
        CASE
          WHEN trim(COALESCE(uploader_category, '')) = '' THEN '__no_category__'
          ELSE trim(uploader_category)
        END
    """
    video_type_sql = """
        CASE lower(trim(COALESCE(video_type, '')))
          WHEN 'video' THEN 'video'
          WHEN 'short' THEN 'short'
          WHEN 'livestream' THEN 'livestream'
          WHEN 'movie' THEN 'movie'
          ELSE 'unknown'
        END
    """
    broadcast_status_sql = """
        CASE
          WHEN lower(trim(COALESCE(video_type, ''))) <> 'livestream'
            THEN 'not_applicable'
          ELSE CASE lower(trim(COALESCE(broadcast_status, '')))
            WHEN 'live' THEN 'live'
            WHEN 'ended' THEN 'ended'
            WHEN 'upcoming' THEN 'upcoming'
            ELSE 'unknown'
          END
        END
    """
    ai_disclosure_sql = """
        CASE ai_disclosure
          WHEN 1 THEN 'made_with_ai'
          WHEN 0 THEN 'no_disclosure'
          ELSE 'unknown'
        END
    """
    note_presence_sql = """
        CASE WHEN trim(COALESCE(note, '')) <> ''
          THEN 'with_note' ELSE 'without_note' END
    """
    categorized_cte = f"""
        WITH raw_candidates AS MATERIALIZED (
          {candidate_sql}
        ),
        candidate_occurrences AS (
          SELECT raw_candidates.*,
                 CASE
                   WHEN COALESCE(video_id, '') = '' THEN 1
                   ELSE COUNT(*) OVER (PARTITION BY playlist_id, video_id)
                 END AS playlist_occurrence_count
          FROM raw_candidates
        ),
        categorized AS (
          SELECT candidate_occurrences.*,
                 {availability_category_sql} AS availability_category,
                 {collection_category_sql} AS collection_category,
                 {completion_category_sql} AS completion_category,
                 {reaction_category_sql} AS reaction_category,
                 {video_type_sql} AS video_type_category,
                 {broadcast_status_sql} AS broadcast_status_category,
                 {ai_disclosure_sql} AS ai_disclosure_category,
                 {note_presence_sql} AS note_presence_category,
                 {uploader_category_sql} AS uploader_category_category,
                 CASE
                   WHEN COALESCE(video_id, '') <> '' THEN 'video:' || video_id
                   ELSE 'slot:' || playlist_id || ':' || position
                 END AS count_key
          FROM candidate_occurrences
        )
    """
    plugin_filter_clauses = []
    if active_included_video_ids is not None:
        plugin_filter_clauses.append(
            "EXISTS (SELECT 1 FROM temp.video_collection_included included "
            "WHERE included.video_id = categorized.video_id)"
        )
    if active_excluded_video_ids:
        plugin_filter_clauses.append(
            "NOT EXISTS (SELECT 1 FROM temp.video_collection_excluded excluded "
            "WHERE excluded.video_id = categorized.video_id)"
        )
    plugin_filter_clause = " AND ".join(plugin_filter_clauses) or "1"
    duplicate_count = 0
    if playlist_id:
        duplicate_count = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(occurrence_count), 0)
                FROM (
                  SELECT COUNT(*) AS occurrence_count
                  FROM playlist_items
                  WHERE playlist_id = ?
                    AND video_id IS NOT NULL
                  GROUP BY video_id
                  HAVING COUNT(*) > 1
                )
                """,
                (playlist_id,),
            ).fetchone()[0]
            or 0
        )
    count_rows = conn.execute(
        categorized_cte
        + f"""
          SELECT 'collection' AS count_type, collection_category AS category,
                 COUNT(DISTINCT count_key) AS count
          FROM categorized
          WHERE {plugin_filter_clause}
          GROUP BY collection_category
          UNION ALL
          SELECT 'completion', completion_category, COUNT(DISTINCT count_key)
          FROM categorized
          WHERE {plugin_filter_clause}
          GROUP BY completion_category
          UNION ALL
          SELECT 'reaction', reaction_category, COUNT(DISTINCT count_key)
          FROM categorized
          WHERE {plugin_filter_clause}
          GROUP BY reaction_category
          UNION ALL
          SELECT 'video_type', video_type_category, COUNT(DISTINCT count_key)
          FROM categorized
          WHERE {plugin_filter_clause}
          GROUP BY video_type_category
          UNION ALL
          SELECT 'broadcast_status', broadcast_status_category, COUNT(DISTINCT count_key)
          FROM categorized
          WHERE {plugin_filter_clause} AND broadcast_status_category <> 'not_applicable'
          GROUP BY broadcast_status_category
          UNION ALL
          SELECT 'ai_disclosure', ai_disclosure_category, COUNT(DISTINCT count_key)
          FROM categorized
          WHERE {plugin_filter_clause}
          GROUP BY ai_disclosure_category
          UNION ALL
          SELECT 'uploader_category', uploader_category_category,
                 COUNT(DISTINCT count_key)
          FROM categorized
          WHERE {plugin_filter_clause}
          GROUP BY uploader_category_category
          UNION ALL
          SELECT 'note', note_presence_category, COUNT(DISTINCT count_key)
          FROM categorized
          WHERE {plugin_filter_clause}
          GROUP BY note_presence_category
        """,
        params,
    ).fetchall()
    counts = {category: 0 for category in (*VIDEO_AVAILABILITY_CATEGORIES, "removed")}
    completion_counts = {category: 0 for category in VIDEO_COMPLETION_CATEGORIES}
    reaction_counts = {
        "total": 0,
        **{category: 0 for category in VIDEO_REACTION_CATEGORIES},
    }
    video_type_counts = {
        "total": 0,
        **{category: 0 for category in VIDEO_TYPE_CATEGORIES},
    }
    broadcast_status_counts = {
        "total": 0,
        **{category: 0 for category in BROADCAST_STATUS_CATEGORIES},
    }
    ai_disclosure_counts = {
        "total": 0,
        **{category: 0 for category in AI_DISCLOSURE_CATEGORIES},
    }
    uploader_category_counts = {
        "total": 0,
        NO_UPLOADER_CATEGORY_FILTER: 0,
    }
    note_counts = {category: 0 for category in OMNI_SEARCH_NOTE_FILTERS}
    for row in count_rows:
        if row["count_type"] == "collection":
            target = counts
        elif row["count_type"] == "completion":
            target = completion_counts
        elif row["count_type"] == "reaction":
            target = reaction_counts
        elif row["count_type"] == "video_type":
            target = video_type_counts
        elif row["count_type"] == "broadcast_status":
            target = broadcast_status_counts
        elif row["count_type"] == "ai_disclosure":
            target = ai_disclosure_counts
        elif row["count_type"] == "note":
            target = note_counts
        else:
            target = uploader_category_counts
        target[row["category"]] = row["count"]
    reaction_counts["total"] = sum(
        reaction_counts[category] for category in VIDEO_REACTION_CATEGORIES
    )
    video_type_counts["total"] = sum(
        video_type_counts[category] for category in VIDEO_TYPE_CATEGORIES
    )
    broadcast_status_counts["total"] = sum(
        broadcast_status_counts[category]
        for category in BROADCAST_STATUS_CATEGORIES
    )
    ai_disclosure_counts["total"] = sum(
        ai_disclosure_counts[category]
        for category in AI_DISCLOSURE_CATEGORIES
    )
    uploader_category_counts["total"] = sum(
        count
        for category, count in uploader_category_counts.items()
        if category != "total"
    )
    distinct_total = sum(counts.values())

    filter_params = dict(params)
    category_placeholders = ", ".join(
        f":collection_{index}" for index in range(len(selected_categories))
    )
    filter_params.update(
        {
            f"collection_{index}": value
            for index, value in enumerate(sorted(selected_categories))
        }
    )
    completion_placeholders = ", ".join(
        f":completion_{index}" for index in range(len(selected_completion_filters))
    )
    filter_params.update(
        {
            f"completion_{index}": value
            for index, value in enumerate(sorted(selected_completion_filters))
        }
    )
    selected_clause = (
        f"collection_category IN ({category_placeholders})"
        if selected_categories
        else "0"
    )
    completion_clause = (
        f"completion_category IN ({completion_placeholders})"
        if selected_completion_filters
        else "0"
    )
    reaction_placeholders = ", ".join(
        f":reaction_{index}" for index in range(len(selected_reaction_filters))
    )
    filter_params.update(
        {
            f"reaction_{index}": value
            for index, value in enumerate(sorted(selected_reaction_filters))
        }
    )
    reaction_clause = (
        f"reaction_category IN ({reaction_placeholders})"
        if selected_reaction_filters
        else "0"
    )
    video_type_placeholders = ", ".join(
        f":video_type_{index}" for index in range(len(selected_video_type_filters))
    )
    filter_params.update(
        {
            f"video_type_{index}": value
            for index, value in enumerate(sorted(selected_video_type_filters))
        }
    )
    video_type_clause = (
        f"video_type_category IN ({video_type_placeholders})"
        if selected_video_type_filters
        else "0"
    )
    broadcast_status_placeholders = ", ".join(
        f":broadcast_status_{index}"
        for index in range(len(selected_broadcast_status_filters))
    )
    filter_params.update(
        {
            f"broadcast_status_{index}": value
            for index, value in enumerate(sorted(selected_broadcast_status_filters))
        }
    )
    broadcast_status_clause = (
        f"(broadcast_status_category = 'not_applicable' "
        f"OR broadcast_status_category IN ({broadcast_status_placeholders}))"
        if selected_broadcast_status_filters
        else "broadcast_status_category = 'not_applicable'"
    )
    ai_disclosure_placeholders = ", ".join(
        f":ai_disclosure_{index}"
        for index in range(len(selected_ai_disclosure_filters))
    )
    filter_params.update(
        {
            f"ai_disclosure_{index}": value
            for index, value in enumerate(sorted(selected_ai_disclosure_filters))
        }
    )
    ai_disclosure_clause = (
        f"ai_disclosure_category IN ({ai_disclosure_placeholders})"
        if selected_ai_disclosure_filters
        else "0"
    )
    if selected_uploader_category_filters is None:
        uploader_category_clause = "1"
    else:
        uploader_category_placeholders = ", ".join(
            f":uploader_category_{index}"
            for index in range(len(selected_uploader_category_filters))
        )
        filter_params.update(
            {
                f"uploader_category_{index}": value
                for index, value in enumerate(sorted(selected_uploader_category_filters))
            }
        )
        uploader_category_clause = (
            f"uploader_category_category IN ({uploader_category_placeholders})"
            if selected_uploader_category_filters
            else "0"
        )
    note_placeholders = ", ".join(
        f":note_{index}" for index in range(len(selected_note_filters))
    )
    filter_params.update(
        {
            f"note_{index}": value
            for index, value in enumerate(sorted(selected_note_filters))
        }
    )
    note_clause = (
        f"note_presence_category IN ({note_placeholders})"
        if selected_note_filters
        else "0"
    )
    duplicate_clause = (
        "playlist_occurrence_count > 1"
        if playlist_id and duplicates_only
        else "1"
    )
    native_filter_clause = (
        f"{video_type_clause} AND {broadcast_status_clause} AND {ai_disclosure_clause} "
        f"AND {selected_clause} AND {completion_clause} "
        f"AND {reaction_clause} "
        f"AND {uploader_category_clause} AND {note_clause} AND {duplicate_clause}"
    )

    active_video_facet_memberships = {
        str(plugin_id): {
            str(video_id).strip()
            for video_id in video_ids
            if str(video_id).strip()
        }
        for plugin_id, video_ids in (video_facet_memberships or {}).items()
    }
    facet_rows = conn.execute(
        categorized_cte
        + f"""
          SELECT count_key, video_id
          FROM categorized
          WHERE {native_filter_clause}
        """,
        filter_params,
    ).fetchall()
    facet_video_ids = {
        str(row["count_key"]): str(row["video_id"] or "")
        for row in facet_rows
    }
    video_facet_counts = {
        plugin_id: {
            "present": sum(
                1 for video_id in facet_video_ids.values() if video_id in video_ids
            ),
            "absent": sum(
                1 for video_id in facet_video_ids.values() if video_id not in video_ids
            ),
        }
        for plugin_id, video_ids in active_video_facet_memberships.items()
    }
    rank_partition = (
        "count_key, playlist_id, position"
        if playlist_id
        else "count_key"
    )
    page_cte = categorized_cte + f""",
        filtered AS (
          SELECT *
          FROM categorized
          WHERE {native_filter_clause} AND {plugin_filter_clause}
        ),
        ranked AS (
          SELECT filtered.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY {rank_partition}
                   ORDER BY
                     CASE WHEN membership_state = 'current' THEN 0 ELSE 1 END,
                     completeness_score DESC
                 ) AS candidate_rank
          FROM filtered
        )
    """
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    total = conn.execute(
        page_cte + "SELECT COUNT(*) FROM ranked WHERE candidate_rank = 1",
        filter_params,
    ).fetchone()[0]
    if total and offset >= total:
        offset = ((total - 1) // limit) * limit
    title_sql = "COALESCE(NULLIF(metadata_title, ''), title, '') COLLATE NOCASE"
    order_by = {
        "oldest_added": (
            f"COALESCE(NULLIF(added_at, ''), metadata_upload_date, ''), {title_sql}, count_key"
        ),
        "most_watched": f"watch_count DESC, {title_sql}, count_key",
        "playlist_order": (
            "playlist_title COLLATE NOCASE, position, COALESCE(video_id, ''), count_key"
        ),
        "title": f"{title_sql}, count_key",
        "title_desc": f"{title_sql} DESC, count_key",
    }.get(
        sort,
        f"COALESCE(NULLIF(added_at, ''), metadata_upload_date, '') DESC, {title_sql} DESC, count_key",
    )
    filter_params.update({"limit": limit, "offset": offset})
    page_candidates = [
        dict(row)
        for row in conn.execute(
            page_cte
            + f"""
              SELECT *
              FROM ranked
              WHERE candidate_rank = 1
              ORDER BY {order_by}
              LIMIT :limit OFFSET :offset
            """,
            filter_params,
        )
    ]
    exact_memberships = {
        (item.get("video_id") or "", index): {
            key: item.get(key)
            for key in (
                "playlist_id",
                "playlist_title",
                "position",
                "membership_state",
                "unavailable_kind",
                "source_quality",
                "match_type",
                "match_confidence",
                "added_at",
                "availability",
            )
        }
        for index, item in enumerate(page_candidates)
    }
    wrappers = [_omni_result("video", 0, dict(item), matched_description=False) for item in page_candidates]
    _hydrate_omni_videos(conn, wrappers)
    _add_omni_video_links(conn, wrappers)
    results = []
    for index, wrapper in enumerate(wrappers):
        item = wrapper["item"]
        if scope == "playlist":
            item.update(exact_memberships.get((item.get("video_id") or "", index), {}))
            item["url"] = youtube_video_url(
                item.get("video_id") or "",
                item.get("playlist_id") or "",
            )
            item["playlist_url"] = youtube_playlist_url(item.get("playlist_id") or "")
        item.pop("completeness_score", None)
        item.pop("completion_category", None)
        item.pop("reaction_category", None)
        item.pop("video_type_category", None)
        item.pop("broadcast_status_category", None)
        item.pop("ai_disclosure_category", None)
        item.pop("uploader_category_category", None)
        item.pop("note_presence_category", None)
        item.pop("count_key", None)
        item.pop("playlist_occurrence_count", None)
        item.pop("candidate_rank", None)
        video_id = str(item.get("video_id") or "")
        item["pluginFacets"] = {
            plugin_id: video_id in video_ids
            for plugin_id, video_ids in active_video_facet_memberships.items()
        }
        item["pluginSearchMatches"] = sorted(
            plugin_id
            for plugin_id, video_ids in active_video_search_match_memberships.items()
            if video_id in video_ids
        )
        results.append(item)
    return {
        "results": results,
        "total": total,
        "distinctTotal": distinct_total,
        "counts": counts,
        "completionCounts": completion_counts,
        "reactionCounts": reaction_counts,
        "videoTypeCounts": video_type_counts,
        "broadcastStatusCounts": broadcast_status_counts,
        "aiDisclosureCounts": ai_disclosure_counts,
        "uploaderCategoryCounts": uploader_category_counts,
        "noteCounts": note_counts,
        "metaCounts": {"videoPlugins": video_facet_counts},
        "duplicateCount": duplicate_count,
        "limit": limit,
        "offset": offset,
    }


def _channel_list_category(channel: dict[str, Any]) -> str:
    if _channel_status_category(channel) == "terminated":
        return "terminated"
    return _channel_subscription_category(channel)


def _channel_subscription_category(channel: dict[str, Any]) -> str:
    return "subscribed" if int(channel.get("subscribed") or 0) else "non_subscribed"


def _channel_status_category(channel: dict[str, Any]) -> str:
    return (
        "terminated"
        if str(channel.get("status") or "").lower() in {"terminated", "deleted"}
        else "active"
    )


def channel_list_data(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    categories: set[str] | None = None,
    subscribed_only: bool = False,
    sort: str = "title",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    pattern = _omni_like_pattern(query.strip())
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    params: dict[str, Any] = {
        "pattern": pattern,
        "subscribed_only": int(subscribed_only),
    }
    category_sql = """
        CASE
          WHEN lower(COALESCE(status, '')) IN ('terminated', 'deleted') THEN 'terminated'
          WHEN subscribed = 1 THEN 'subscribed'
          ELSE 'non_subscribed'
        END
    """
    filtered_cte = f"""
        WITH filtered AS (
          SELECT channels.*, {category_sql} AS list_category
          FROM channels
          WHERE (
              :pattern = '%%'
              OR lower(
                   title || ' ' || channel_id || ' ' || aliases || ' ' || description || ' ' ||
                   status || ' ' || status_reason
                 ) LIKE :pattern ESCAPE '\\'
            )
            AND (:subscribed_only = 0 OR subscribed = 1)
        )
    """
    count_rows = conn.execute(
        filtered_cte + "SELECT list_category, COUNT(*) AS count FROM filtered GROUP BY list_category",
        params,
    ).fetchall()
    counts = {
        key: 0 for key in ("subscribed", "non_subscribed", "terminated")
    }
    counts.update({row["list_category"]: row["count"] for row in count_rows})

    category_clause = ""
    if categories is not None:
        selected = sorted(set(categories) & set(counts))
        if selected:
            placeholders = ", ".join(f":category_{index}" for index in range(len(selected)))
            category_clause = f"WHERE list_category IN ({placeholders})"
            params.update(
                {f"category_{index}": value for index, value in enumerate(selected)}
            )
        else:
            category_clause = "WHERE 0"

    total = conn.execute(
        filtered_cte + f"SELECT COUNT(*) FROM filtered {category_clause}",
        params,
    ).fetchone()[0]
    if total and offset >= total:
        offset = ((total - 1) // limit) * limit
    order_by = {
        "title_desc": "COALESCE(NULLIF(title, ''), channel_id) COLLATE NOCASE DESC, channel_id",
        "newest_updated": (
            "COALESCE(subscribed_at, first_seen_at, updated_at, fetched_at, '') DESC, "
            "COALESCE(NULLIF(title, ''), channel_id) COLLATE NOCASE, channel_id"
        ),
        "oldest_updated": (
            "COALESCE(subscribed_at, first_seen_at, updated_at, fetched_at, ''), "
            "COALESCE(NULLIF(title, ''), channel_id) COLLATE NOCASE, channel_id"
        ),
    }.get(
        sort,
        "COALESCE(NULLIF(title, ''), channel_id) COLLATE NOCASE, channel_id",
    )
    params.update({"limit": limit, "offset": offset})
    rows = [
        dict(row)
        for row in conn.execute(
            filtered_cte
            + f"""
              SELECT * FROM filtered
              {category_clause}
              ORDER BY {order_by}
              LIMIT :limit OFFSET :offset
            """,
            params,
        )
    ]
    for row in rows:
        row.pop("list_category", None)
        row["preferred_reference"] = preferred_youtube_channel_reference(
            row.get("channel_id") or "",
            row.get("aliases") or "",
        )
        row["url"] = preferred_youtube_channel_url(
            row.get("channel_id") or "",
            row.get("aliases") or "",
        )
    _attach_channel_featured_channels(conn, rows)
    attach_annotations(conn, "channel", rows)
    return {
        "results": rows,
        "total": total,
        "counts": counts,
        "limit": limit,
        "offset": offset,
    }


def resolve_channel_id(conn: sqlite3.Connection, channel_reference: str) -> str:
    channel_reference = (channel_reference or "").strip()
    if not channel_reference:
        return ""
    wanted = youtube_channel_ref_from_url(channel_reference) or channel_reference
    for row in conn.execute(
        "SELECT channel_id, aliases FROM channels WHERE trim(aliases) <> ''"
    ):
        if preferred_youtube_channel_reference("", row["aliases"]).casefold() == wanted.casefold():
            return str(row["channel_id"])
    direct = conn.execute(
        "SELECT channel_id FROM channels WHERE channel_id = ?",
        (channel_reference,),
    ).fetchone()
    if direct is not None:
        return str(direct["channel_id"])
    return ""


def channel_detail_data(conn: sqlite3.Connection, channel_reference: str) -> dict[str, Any] | None:
    channel_id = resolve_channel_id(conn, channel_reference)
    if not channel_id:
        return None
    row = conn.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["preferred_reference"] = preferred_youtube_channel_reference(
        channel_id,
        item.get("aliases") or "",
    )
    item["url"] = preferred_youtube_channel_url(channel_id, item.get("aliases") or "")
    _attach_channel_featured_channels(conn, [item])
    attach_annotations(conn, "channel", [item])
    return item


def channel_summaries_data(
    conn: sqlite3.Connection,
    channel_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    normalized_ids = list(dict.fromkeys(channel_id for channel_id in channel_ids if channel_id))
    if not normalized_ids:
        return {"channels": []}
    placeholders = ", ".join("?" for _channel_id in normalized_ids)
    rows_by_id = {
        str(row["channel_id"]): dict(row)
        for row in conn.execute(
            f"SELECT * FROM channels WHERE channel_id IN ({placeholders})",
            normalized_ids,
        )
    }
    channels = []
    for channel_id in normalized_ids:
        item = rows_by_id.get(channel_id)
        if item is None:
            continue
        item["preferred_reference"] = preferred_youtube_channel_reference(
            channel_id,
            item.get("aliases") or "",
        )
        item["url"] = preferred_youtube_channel_url(
            channel_id,
            item.get("aliases") or "",
        )
        channels.append(item)
    _attach_channel_featured_channels(conn, channels)
    attach_annotations(conn, "channel", channels)
    return {"channels": channels}


def video_detail_data(conn: sqlite3.Connection, video_id: str) -> dict[str, Any] | None:
    exists = conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    if exists is None:
        return None
    wrappers = [_omni_result("video", 0, {"video_id": video_id}, matched_description=False)]
    _hydrate_omni_videos(conn, wrappers)
    _add_omni_video_links(conn, wrappers)
    video = wrappers[0]["item"]
    video["url"] = youtube_video_url(video_id)
    return video


def projected_video_data(projection: Mapping[str, Any]) -> dict[str, Any]:
    video_id = str(projection.get("video_id") or "").strip()
    title = str(projection.get("title") or "").strip()
    plugin_ids = sorted(
        {
            plugin_id
            for value in projection.get("projection_plugin_ids") or ()
            if (plugin_id := str(value).strip())
        }
    )
    return {
        "video_id": video_id,
        "title": title,
        "metadata_title": title,
        "metadata_description": "",
        "metadata_thumbnail_path": "",
        "metadata_channel_thumbnail_path": "",
        "metadata_channel": "",
        "metadata_channel_id": "",
        "metadata_channel_aliases": "",
        "metadata_channel_reference": "",
        "metadata_channel_url": "",
        "metadata_duration": "",
        "metadata_upload_date": "",
        "metadata_fetch_status": "",
        "duration_text": "",
        "uploader_category": "",
        "video_type": "",
        "broadcast_status": None,
        "broadcast_started_at": None,
        "broadcast_ended_at": None,
        "broadcast_status_checked_at": None,
        "movie_rating": "",
        "movie_release_date": "",
        "movie_offer": "",
        "max_video_height": None,
        "spatial_format": None,
        "stereo_layout": None,
        "dynamic_range": None,
        "license": None,
        "location_name": None,
        "content_check_required": None,
        "content_check_reason": None,
        "ai_disclosure": None,
        "ai_disclosure_text": None,
        "reaction": "",
        "is_playable": None,
        "availability": "",
        "playlist_count": 0,
        "playlist_links": [],
        "watch_count": 0,
        "watch_progress_percent": 0,
        "watch_resume_seconds": 0,
        "watch_dates": [],
        "latest_watch_at": "",
        "latest_youtube_ordinal": 0,
        "collection_category": "unknown",
        "availability_category": "unknown",
        "recovered_status": "",
        "archive_url": "",
        "video_file_url": "",
        "match_label": "",
        "match_note": "",
        "updated_at": "",
        "url": youtube_video_url(video_id),
        "virtual_video": True,
        "projection_plugin_ids": plugin_ids,
        "plugin_badges": [{"label": "Not in library"}],
    }


def video_summaries_data(
    conn: sqlite3.Connection,
    video_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    normalized_ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
    wrappers = [
        _omni_result("video", 0, {"video_id": video_id}, matched_description=False)
        for video_id in normalized_ids
    ]
    _hydrate_omni_videos(conn, wrappers)
    _add_omni_video_links(conn, wrappers)
    return {
        "videos": [
            wrapper["item"]
            for wrapper in wrappers
            if "metadata_title" in wrapper["item"]
        ]
    }


OMNI_SEARCH_FIELDS = {"titles", "descriptions", "notes", "tags"}
OMNI_SEARCH_SORTS = {
    "relevance",
    "title",
    "title_desc",
    "newest",
    "oldest",
    "most_watched",
    "type",
}
OMNI_SEARCH_KIND_ORDER = ("video", "clip", "playlist", "channel")
OMNI_SEARCH_META_FILTERS = {
    "video": VIDEO_AVAILABILITY_CATEGORIES,
    "playlist": ("private", "public", "unlisted", "unavailable", "unknown"),
}
OMNI_SEARCH_REACTION_FILTERS = VIDEO_REACTION_CATEGORIES
OMNI_SEARCH_VIDEO_TYPE_FILTERS = VIDEO_TYPE_CATEGORIES
OMNI_SEARCH_BROADCAST_STATUS_FILTERS = BROADCAST_STATUS_CATEGORIES
OMNI_SEARCH_AI_DISCLOSURE_FILTERS = AI_DISCLOSURE_CATEGORIES
OMNI_SEARCH_COMPLETION_FILTERS = VIDEO_COMPLETION_CATEGORIES
OMNI_SEARCH_PLAYLIST_MEMBERSHIP_FILTERS = ("member", "non_member")
OMNI_SEARCH_PLAYLIST_OWNERSHIP_FILTERS = ("mine", "others", "ownership_unknown")
OMNI_SEARCH_CHANNEL_SUBSCRIPTION_FILTERS = ("subscribed", "non_subscribed")
OMNI_SEARCH_CHANNEL_STATUS_FILTERS = ("active", "terminated")
OMNI_SEARCH_CLIP_OWNERSHIP_FILTERS = ("mine", "others", "ownership_unknown")
OMNI_SEARCH_NOTE_FILTERS = ("with_note", "without_note")


_CLIP_RELATIVE_AGE_RE = re.compile(
    r"^(?:clipped\s+)?(?:about\s+)?(?P<count>\d+|an?|one)\s+"
    r"(?P<unit>second|minute|hour|day|week|month|year)s?\s+ago$",
    re.IGNORECASE,
)


def _clip_relative_sort_date(label: str, observed_at: str) -> str:
    normalized = " ".join((label or "").strip().split())
    if not normalized or not observed_at:
        return ""
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    relative = normalized.casefold()
    if relative.startswith("clipped "):
        relative = relative[len("clipped ") :]
    if relative in {"today", "just now"}:
        return observed.date().isoformat()
    if relative == "yesterday":
        return (observed - timedelta(days=1)).date().isoformat()
    match = _CLIP_RELATIVE_AGE_RE.match(normalized)
    if not match:
        return ""
    raw_count = match.group("count").casefold()
    count = int(raw_count) if raw_count.isdigit() else 1
    unit = match.group("unit").casefold()
    if unit in {"month", "year"}:
        months = count * (12 if unit == "year" else 1)
        month_index = observed.year * 12 + observed.month - 1 - months
        year, zero_based_month = divmod(month_index, 12)
        month = zero_based_month + 1
        day = min(observed.day, calendar.monthrange(year, month)[1])
        return observed.date().replace(year=year, month=month, day=day).isoformat()
    seconds_per_unit = {
        "second": 1,
        "minute": 60,
        "hour": 60 * 60,
        "day": 24 * 60 * 60,
        "week": 7 * 24 * 60 * 60,
    }
    return (
        observed - timedelta(seconds=count * seconds_per_unit[unit])
    ).date().isoformat()


def _omni_like_pattern(query: str) -> str:
    escaped = query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _date_only_sort_at(value: str, timezone_name: str) -> str:
    if len(value) != 10:
        return value
    try:
        local_date = datetime.fromisoformat(value).date()
        zone = ZoneInfo(timezone_name or "UTC")
    except (ValueError, ZoneInfoNotFoundError):
        return value
    local_end = datetime.combine(local_date, time(23, 59, 59), tzinfo=zone)
    return local_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _omni_result(
    kind: str,
    score: int,
    item: dict[str, Any],
    *,
    matched_description: bool,
    display_timezone: str = "UTC",
) -> dict[str, Any]:
    sort_date_fallback = False
    if kind == "video":
        title = item.get("metadata_title") or item.get("title") or ""
        latest_watch_at = item.get("latest_watch_at") or ""
        latest_watch_sort_at = _date_only_sort_at(latest_watch_at, display_timezone)
        sort_date = (
            latest_watch_sort_at
            or item.get("added_at")
            or item.get("metadata_upload_date")
            or item.get("updated_at")
            or ""
        )
        sort_date_fallback = not bool(latest_watch_at)
        watch_count = int(item.get("watch_count") or 0)
    elif kind == "clip":
        title = item.get("title") or item.get("clip_id") or ""
        clipped_at = item.get("clipped_at") or ""
        relative_sort_date = _clip_relative_sort_date(
            str(item.get("clipped_at_text") or ""),
            str(item.get("clipped_at_observed_at") or ""),
        )
        sort_date = clipped_at or relative_sort_date
        sort_date = sort_date or item.get("clipped_at_observed_at") or item.get("updated_at") or ""
        sort_date_fallback = not bool(clipped_at or relative_sort_date)
        watch_count = 0
    elif kind == "channel":
        title = item.get("title") or item.get("channel_id") or ""
        subscribed_at = item.get("subscribed_at") or ""
        first_seen_at = item.get("first_seen_at") or ""
        sort_date = subscribed_at or first_seen_at or item.get("updated_at") or item.get("fetched_at") or ""
        sort_date_fallback = not bool(subscribed_at or first_seen_at)
        watch_count = 0
    else:
        title = item.get("title") or item.get("playlist_id") or ""
        last_changed_at = item.get("last_changed_at") or ""
        sort_date = last_changed_at
        sort_date_fallback = not bool(last_changed_at)
        watch_count = 0
    return {
        "kind": kind,
        "score": score,
        "matchedDescription": matched_description,
        "item": item,
        "_title": str(title).casefold(),
        "_sort_date": str(sort_date),
        "_sort_date_fallback": sort_date_fallback,
        "_watch_count": watch_count,
        "_history_ordinal": int(item.get("latest_youtube_ordinal") or 0),
        "_clip_feed_ordinal": int(item.get("youtube_feed_ordinal") or 0),
    }


def _sort_omni_results(results: list[dict[str, Any]], sort: str) -> None:
    kind_rank = {kind: rank for rank, kind in enumerate(OMNI_SEARCH_KIND_ORDER)}
    results.sort(key=lambda result: (result["_title"], kind_rank.get(result["kind"], 99)))
    if sort == "relevance":
        results.sort(key=lambda result: (result["score"], kind_rank.get(result["kind"], 99)))
    elif sort == "title_desc":
        results.sort(key=lambda result: result["_title"], reverse=True)
    elif sort == "newest":
        results.sort(
            key=lambda result: (
                not bool(
                    result["_history_ordinal"] or result["_clip_feed_ordinal"]
                ),
                result["_history_ordinal"] or result["_clip_feed_ordinal"] or 0,
            )
        )
        results.sort(key=lambda result: result["_sort_date"], reverse=True)
        results.sort(key=lambda result: result["_sort_date_fallback"])
    elif sort == "oldest":
        results.sort(
            key=lambda result: (
                not bool(result["_clip_feed_ordinal"]),
                -(result["_clip_feed_ordinal"] or 0),
            )
        )
        results.sort(key=lambda result: result["_sort_date"])
    elif sort == "most_watched":
        results.sort(key=lambda result: result["_watch_count"], reverse=True)
    elif sort == "type":
        results.sort(key=lambda result: kind_rank.get(result["kind"], 99))


def _selected_omni_meta_filters(
    selected: set[str] | None,
    kind: str,
) -> set[str]:
    allowed = set(OMNI_SEARCH_META_FILTERS[kind])
    return allowed if selected is None else selected & allowed


def _assign_omni_meta_categories(
    conn: sqlite3.Connection,
    results: list[dict[str, Any]],
) -> None:
    playlist_categories = {
        row["playlist_id"]: (
            _playlist_availability_category(row),
            _playlist_ownership_category(row),
        )
        for row in _playlist_rows(conn)
    } if any(result["kind"] == "playlist" for result in results) else {}
    for result in results:
        item = result["item"]
        result["notePresence"] = annotation_presence(item)
        if result["kind"] == "video":
            availability_category = _video_availability_category(item)
            item["availability_category"] = availability_category
            category = item.get("collection_category") or availability_category
        elif result["kind"] == "clip":
            result["clipOwnership"] = (
                "ownership_unknown"
                if str(item.get("ownership") or "unknown") == "unknown"
                else str(item.get("ownership") or "unknown")
            )
            continue
        elif result["kind"] == "channel":
            result["channelSubscription"] = _channel_subscription_category(item)
            result["channelStatus"] = _channel_status_category(item)
            continue
        else:
            category, ownership = playlist_categories.get(
                item.get("playlist_id") or "",
                (
                    _playlist_availability_category(item),
                    _playlist_ownership_category(item),
                ),
            )
            result["playlistOwnership"] = ownership
        result["metaCategory"] = category


def _omni_meta_counts(results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts = {
        f"{kind}s": {
            "total": 0,
            **{category: 0 for category in categories},
        }
        for kind, categories in OMNI_SEARCH_META_FILTERS.items()
    }
    counts["channels"] = {
        "total": 0,
        **{category: 0 for category in OMNI_SEARCH_NOTE_FILTERS},
        **{category: 0 for category in OMNI_SEARCH_CHANNEL_SUBSCRIPTION_FILTERS},
        **{category: 0 for category in OMNI_SEARCH_CHANNEL_STATUS_FILTERS},
    }
    counts["clips"] = {
        "total": 0,
        **{category: 0 for category in OMNI_SEARCH_NOTE_FILTERS},
        **{category: 0 for category in OMNI_SEARCH_CLIP_OWNERSHIP_FILTERS},
    }
    counts["playlists"].update(
        {
            **{category: 0 for category in OMNI_SEARCH_NOTE_FILTERS},
            **{category: 0 for category in OMNI_SEARCH_PLAYLIST_OWNERSHIP_FILTERS},
        }
    )
    counts["videos"].update(
        {category: 0 for category in OMNI_SEARCH_NOTE_FILTERS}
    )
    for result in results:
        group = counts[f"{result['kind']}s"]
        group["total"] += 1
        group[result["notePresence"]] += 1
        if result["kind"] == "channel":
            group[result["channelSubscription"]] += 1
            group[result["channelStatus"]] += 1
        elif result["kind"] == "playlist":
            group[result["metaCategory"]] += 1
            group[result["playlistOwnership"]] += 1
        elif result["kind"] == "clip":
            group[result["clipOwnership"]] += 1
        else:
            group[result["metaCategory"]] += 1
    return counts


def _omni_video_reaction_category(result: dict[str, Any]) -> str:
    reaction = str(result["item"].get("reaction") or "").strip().upper()
    if reaction == "LIKE":
        return "liked"
    if reaction == "DISLIKE":
        return "disliked"
    return "none"


def _omni_reaction_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "total": 0,
        **{category: 0 for category in OMNI_SEARCH_REACTION_FILTERS},
    }
    for result in results:
        if result["kind"] != "video":
            continue
        counts["total"] += 1
        counts[_omni_video_reaction_category(result)] += 1
    return counts


def _omni_video_completion_category(
    result: dict[str, Any],
    partial_min_percent: int = 1,
) -> str:
    return _video_completion_filter_category(
        result["item"],
        partial_min_percent,
    )


def _omni_completion_counts(
    results: list[dict[str, Any]],
    partial_min_percent: int = 1,
) -> dict[str, int]:
    partial_min_percent = _bounded_partial_min_percent(partial_min_percent)
    counts = {
        "total": 0,
        **{category: 0 for category in OMNI_SEARCH_COMPLETION_FILTERS},
    }
    for result in results:
        if result["kind"] != "video":
            continue
        counts["total"] += 1
        counts[_omni_video_completion_category(result, partial_min_percent)] += 1
    return counts


def _omni_video_playlist_membership_category(result: dict[str, Any]) -> str:
    item = result["item"]
    if not item.get("video_id") or int(item.get("playlist_count") or 0) > 0:
        return "member"
    return "non_member"


def _omni_playlist_membership_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "total": 0,
        **{category: 0 for category in OMNI_SEARCH_PLAYLIST_MEMBERSHIP_FILTERS},
    }
    for result in results:
        if result["kind"] != "video":
            continue
        counts["total"] += 1
        counts[_omni_video_playlist_membership_category(result)] += 1
    return counts


def _omni_video_uploader_category(result: dict[str, Any]) -> str:
    return (
        str(result["item"].get("uploader_category") or "").strip()
        or NO_UPLOADER_CATEGORY_FILTER
    )


def _omni_video_type_category(result: dict[str, Any]) -> str:
    return _video_type_category(result["item"])


def _omni_video_type_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "total": 0,
        **{category: 0 for category in OMNI_SEARCH_VIDEO_TYPE_FILTERS},
    }
    for result in results:
        if result["kind"] != "video":
            continue
        counts["total"] += 1
        counts[_omni_video_type_category(result)] += 1
    return counts


def _omni_video_broadcast_status_category(result: dict[str, Any]) -> str:
    return _video_broadcast_status_category(result["item"])


def _omni_video_broadcast_status_counts(
    results: list[dict[str, Any]],
) -> dict[str, int]:
    counts = {
        "total": 0,
        **{category: 0 for category in OMNI_SEARCH_BROADCAST_STATUS_FILTERS},
    }
    for result in results:
        if result["kind"] != "video":
            continue
        category = _omni_video_broadcast_status_category(result)
        if category == "not_applicable":
            continue
        counts["total"] += 1
        counts[category] += 1
    return counts


def _omni_video_ai_disclosure_category(result: dict[str, Any]) -> str:
    return _video_ai_disclosure_category(result["item"])


def _omni_video_ai_disclosure_counts(
    results: list[dict[str, Any]],
) -> dict[str, int]:
    counts = {
        "total": 0,
        **{category: 0 for category in OMNI_SEARCH_AI_DISCLOSURE_FILTERS},
    }
    for result in results:
        if result["kind"] != "video":
            continue
        counts["total"] += 1
        counts[_omni_video_ai_disclosure_category(result)] += 1
    return counts


def _known_uploader_categories(conn: sqlite3.Connection) -> tuple[str, ...]:
    categories = {
        str(row[0]).strip()
        for row in conn.execute(
            """
            SELECT DISTINCT trim(uploader_category)
            FROM videos
            WHERE trim(uploader_category) <> ''
            """
        )
        if str(row[0]).strip()
    }
    return tuple(sorted(categories, key=str.casefold))


def _omni_uploader_category_counts(
    results: list[dict[str, Any]],
    known_categories: Collection[str],
) -> dict[str, int]:
    counts = {
        "total": 0,
        NO_UPLOADER_CATEGORY_FILTER: 0,
        **{category: 0 for category in known_categories},
    }
    for result in results:
        if result["kind"] != "video":
            continue
        category = _omni_video_uploader_category(result)
        counts["total"] += 1
        counts.setdefault(category, 0)
        counts[category] += 1
    return counts


def _playlist_links_by_video(
    conn: sqlite3.Connection,
    video_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    links_by_video: dict[str, list[dict[str, Any]]] = {}
    if video_ids:
        placeholders = ",".join("?" for _ in video_ids)
        for row in conn.execute(
            f"""
            SELECT DISTINCT pi.video_id, pi.playlist_id, p.title, pi.membership_state
            FROM playlist_items pi
            JOIN playlists p ON p.playlist_id = pi.playlist_id
            WHERE pi.video_id IN ({placeholders})
            ORDER BY p.title COLLATE NOCASE
            """,
            video_ids,
        ):
            links_by_video.setdefault(row["video_id"], []).append(
                {
                    "playlist_id": row["playlist_id"],
                    "title": row["title"] or row["playlist_id"],
                    "removed": row["membership_state"] == "retained_unavailable",
                }
            )
    return links_by_video


def _add_video_playlist_links(
    conn: sqlite3.Connection,
    items: list[dict[str, Any]],
) -> None:
    video_ids = sorted({item.get("video_id") for item in items if item.get("video_id")})
    links_by_video = _playlist_links_by_video(conn, video_ids)
    for item in items:
        item["playlist_links"] = links_by_video.get(
            item.get("video_id") or "",
            item.get("playlist_links", []),
        )


def _add_omni_video_links(conn: sqlite3.Connection, results: list[dict[str, Any]]) -> None:
    _add_video_playlist_links(
        conn,
        [result["item"] for result in results if result["kind"] == "video"],
    )


def _hydrate_video_identity(item: dict[str, Any], playlist_id: str = "") -> None:
    video_id = item.get("video_id") or ""
    item["url"] = youtube_video_url(video_id, playlist_id)
    item["metadata_channel_reference"] = preferred_youtube_channel_reference(
        item.get("metadata_channel_id") or "",
        item.get("metadata_channel_aliases") or "",
    )
    item["metadata_channel_url"] = preferred_youtube_channel_url(
        item.get("metadata_channel_id") or "",
        item.get("metadata_channel_aliases") or "",
    )
    item["watch_dates"] = [
        value for value in (item.pop("watch_dates_text", "") or "").split("|") if value
    ]


def _hydrate_omni_videos(conn: sqlite3.Connection, results: list[dict[str, Any]]) -> None:
    video_ids = sorted(
        {
            result["item"].get("video_id")
            for result in results
            if result["kind"] == "video" and result["item"].get("video_id")
        }
    )
    if not video_ids:
        return
    placeholders = ",".join("?" for _ in video_ids)
    rows = conn.execute(
        f"""
        WITH playlist_choice AS (
          SELECT pi.*,
                 p.title AS playlist_title,
                 ROW_NUMBER() OVER (
                   PARTITION BY pi.video_id
                   ORDER BY CASE WHEN pi.membership_state = 'current' THEN 0 ELSE 1 END,
                            p.title COLLATE NOCASE,
                            pi.position
                 ) AS choice_rank
          FROM playlist_items pi
          JOIN playlists p ON p.playlist_id = pi.playlist_id
          WHERE pi.video_id IN ({placeholders})
        ),
        history_stats AS (
          SELECT video_id,
                 COUNT(*) AS watch_count,
                 GROUP_CONCAT(COALESCE(watch_date, substr(watched_at, 1, 10)), '|') AS watch_dates,
                 MAX(COALESCE(watched_at, watch_date)) AS latest_watch_at,
                 MAX(watch_progress_percent) AS watch_progress_percent,
                 MAX(watch_resume_seconds) AS watch_resume_seconds
          FROM history_events
          WHERE video_id IN ({placeholders})
          GROUP BY video_id
        )
        SELECT COALESCE(pc.playlist_id, '') AS playlist_id,
               COALESCE(pc.position, 0) AS position,
               COALESCE(pc.membership_state, '') AS membership_state,
               COALESCE(pc.unavailable_kind, '') AS unavailable_kind,
               COALESCE(pc.source_quality, '') AS source_quality,
               COALESCE(pc.match_type, '') AS match_type,
               COALESCE(pc.match_confidence, '') AS match_confidence,
               pc.added_at,
               COALESCE(pc.playlist_title, '') AS playlist_title,
               v.video_id,
               v.title,
               COALESCE(v.channel_id, '') AS channel_id,
               COALESCE(ch.title, '') AS channel,
               v.duration_text,
               v.is_playable,
               v.availability,
               v.title AS metadata_title,
               v.description AS metadata_description,
               v.note,
               COALESCE(v.channel_id, '') AS metadata_channel_id,
               COALESCE(ch.title, '') AS metadata_channel,
               COALESCE(ch.aliases, '') AS metadata_channel_aliases,
               v.duration_text AS metadata_duration,
               v.upload_date AS metadata_upload_date,
               v.uploader_category,
               v.video_type,
               v.broadcast_status,
               v.broadcast_started_at,
               v.broadcast_ended_at,
               v.broadcast_status_checked_at,
               v.movie_rating,
               v.movie_release_date,
               v.movie_offer,
               v.max_video_height,
               v.spatial_format,
               v.stereo_layout,
               v.dynamic_range,
               v.license,
               v.location_name,
               v.content_check_required,
               v.content_check_reason,
               v.ai_disclosure,
               v.ai_disclosure_text,
               v.thumbnail_path AS metadata_thumbnail_path,
               COALESCE(ch.thumbnail_path, '') AS metadata_channel_thumbnail_path,
               v.fetch_status AS metadata_fetch_status,
               v.reaction,
               COALESCE(hs.watch_progress_percent, 0) AS watch_progress_percent,
               COALESCE(hs.watch_resume_seconds, 0) AS watch_resume_seconds,
               COALESCE(hs.watch_count, 0) AS watch_count,
               COALESCE(hs.watch_dates, '') AS watch_dates_text,
               COALESCE(hs.latest_watch_at, '') AS latest_watch_at,
               COALESCE(vr.archivarix_status, '') AS recovered_status,
               vr.archive_capture_at,
               vr.media_available,
               v.updated_at
        FROM videos v
        LEFT JOIN channels ch ON ch.channel_id = v.channel_id
        LEFT JOIN video_recovery vr ON vr.video_id = v.video_id
        LEFT JOIN playlist_choice pc ON pc.video_id = v.video_id AND pc.choice_rank = 1
        LEFT JOIN history_stats hs ON hs.video_id = v.video_id
        WHERE v.video_id IN ({placeholders})
        """,
        [*video_ids, *video_ids, *video_ids],
    ).fetchall()
    hydrated = {row["video_id"]: dict(row) for row in rows}
    for result in results:
        if result["kind"] != "video":
            continue
        video_id = result["item"].get("video_id") or ""
        hydrated_item = hydrated.get(video_id)
        if not hydrated_item:
            continue
        item = dict(hydrated_item)
        item["availability_category"] = _video_availability_category(item)
        if "collection_category" in result["item"]:
            item["collection_category"] = result["item"]["collection_category"]
        _hydrate_video_identity(item, item.get("playlist_id") or "")
        item["playlist_url"] = youtube_playlist_url(item.get("playlist_id") or "")
        item["archive_url"] = wayback_video_url(video_id, item.get("archive_capture_at"))
        item["video_file_url"] = archivarix_media_url(video_id) if item.get("media_available") else ""
        item["match_label"] = playlist_match_type_label(item.get("match_type") or "")
        item["match_note"] = playlist_match_type_note(item.get("match_type") or "")
        result["item"] = item
    attach_annotations(
        conn,
        "video",
        [result["item"] for result in results if result["kind"] == "video"],
    )


def _populate_omni_video_filter_table(
    conn: sqlite3.Connection,
    table_name: str,
    groups: Sequence[Collection[str]],
) -> None:
    conn.execute(f"DROP TABLE IF EXISTS temp.{table_name}")
    conn.execute(
        f"""
        CREATE TEMP TABLE {table_name}(
          filter_index INTEGER NOT NULL,
          video_id TEXT NOT NULL,
          PRIMARY KEY(filter_index, video_id)
        ) WITHOUT ROWID
        """
    )
    conn.executemany(
        f"INSERT INTO temp.{table_name}(filter_index, video_id) VALUES (?, ?)",
        (
            (filter_index, str(video_id))
            for filter_index, video_ids in enumerate(groups)
            for video_id in video_ids
        ),
    )


def _populate_omni_video_facet_table(
    conn: sqlite3.Connection,
    memberships: Mapping[str, Collection[str]],
) -> None:
    conn.execute("DROP TABLE IF EXISTS temp.omni_video_facets")
    conn.execute(
        """
        CREATE TEMP TABLE omni_video_facets(
          plugin_id TEXT NOT NULL,
          video_id TEXT NOT NULL,
          PRIMARY KEY(plugin_id, video_id)
        ) WITHOUT ROWID
        """
    )
    conn.executemany(
        "INSERT INTO temp.omni_video_facets(plugin_id, video_id) VALUES (?, ?)",
        (
            (plugin_id, str(video_id))
            for plugin_id, video_ids in memberships.items()
            for video_id in video_ids
        ),
    )


def _omni_sql_set_clause(
    column: str,
    values: Collection[str],
    prefix: str,
    params: dict[str, Any],
) -> str:
    normalized = sorted(set(values))
    if not normalized:
        return "0"
    placeholders = []
    for index, value in enumerate(normalized):
        name = f"{prefix}_{index}"
        params[name] = value
        placeholders.append(f":{name}")
    return f"{column} IN ({', '.join(placeholders)})"


def _omni_video_sql_data(
    conn: sqlite3.Connection,
    *,
    params: dict[str, Any],
    query: str,
    search_titles: bool,
    search_descriptions: bool,
    has_annotation_search_matches: bool,
    annotation_search_match_sql: str,
    has_video_search_matches: bool,
    video_search_match_sql: str,
    video_source: str,
    selected_meta_filters: Collection[str],
    selected_reaction_filters: Collection[str],
    selected_completion_filters: Collection[str],
    selected_playlist_membership_filters: Collection[str],
    selected_video_type_filters: Collection[str],
    selected_broadcast_status_filters: Collection[str],
    selected_ai_disclosure_filters: Collection[str],
    selected_uploader_category_filters: Collection[str],
    selected_note_filters: Collection[str],
    known_uploader_categories: Collection[str],
    partial_min_percent: int,
    active_video_id_filters: Sequence[Collection[str]],
    active_video_id_exclusion_filters: Sequence[Collection[str]],
    active_video_facet_memberships: Mapping[str, Collection[str]],
    active_video_search_match_memberships: Mapping[str, Collection[str]],
    sort: str,
    candidate_limit: int,
    display_timezone: str,
) -> dict[str, Any]:
    _populate_omni_video_filter_table(
        conn,
        "omni_video_included_filters",
        active_video_id_filters,
    )
    _populate_omni_video_filter_table(
        conn,
        "omni_video_excluded_filters",
        active_video_id_exclusion_filters,
    )
    _populate_omni_video_facet_table(conn, active_video_facet_memberships)

    video_title_match = """
        (
          lower(
            v.title || ' ' || COALESCE(ch.title, '') || ' ' || v.video_id || ' ' ||
            v.reaction || ' ' || v.availability
          ) LIKE :pattern ESCAPE '\\'
          OR EXISTS (
            SELECT 1
            FROM playlist_items search_pi
            JOIN playlists search_p ON search_p.playlist_id = search_pi.playlist_id
            WHERE search_pi.video_id = v.video_id
              AND lower(search_p.title) LIKE :pattern ESCAPE '\\'
          )
        )
    """
    video_description_match = "lower(v.description) LIKE :pattern ESCAPE '\\'"
    video_matches = []
    if query:
        if search_titles:
            video_matches.append(video_title_match)
        if search_descriptions:
            video_matches.append(video_description_match)
        if has_video_search_matches:
            video_matches.append(video_search_match_sql)
        if has_annotation_search_matches:
            video_matches.append(annotation_search_match_sql)
    else:
        video_matches.append("1 = 1")
    video_title_hit = (
        "1" if not query else (video_title_match if search_titles else "0")
    )
    availability_sql = _video_availability_category_sql(
        video_id="video_id",
        availability="availability",
        is_playable="is_playable",
    )
    partial_min_percent = _bounded_partial_min_percent(partial_min_percent)
    sql_params = dict(params)
    sql_params.update(
        {
            "partial_min_percent": partial_min_percent,
            "included_filter_count": len(active_video_id_filters),
            "candidate_limit": max(1, int(candidate_limit)),
            "display_timezone": display_timezone or "UTC",
        }
    )
    conn.create_function(
        "omni_date_sort_at",
        2,
        _date_only_sort_at,
        deterministic=True,
    )
    conn.create_function(
        "omni_casefold",
        1,
        lambda value: str(value or "").casefold(),
        deterministic=True,
    )
    candidate_channel_join = (
        "LEFT JOIN channels ch ON ch.channel_id = v.channel_id" if query else ""
    )
    video_source_clause = "1"
    if video_source == "liked":
        video_source_clause = "upper(COALESCE(v.reaction, '')) = 'LIKE'"
    elif video_source == "playlist_member":
        video_source_clause = """
            EXISTS (
              SELECT 1
              FROM playlist_items source_pi
              WHERE source_pi.video_id = v.video_id
            )
        """
    conn.execute("DROP TABLE IF EXISTS temp.omni_playlist_stats")
    conn.execute(
        """
        CREATE TEMP TABLE omni_playlist_stats AS
        SELECT video_id,
               MIN(COALESCE(added_at, '')) AS added_at,
               COUNT(*) AS playlist_count
        FROM playlist_items
        WHERE video_id IS NOT NULL
        GROUP BY video_id
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX temp.idx_omni_playlist_stats_id "
        "ON omni_playlist_stats(video_id)"
    )
    conn.execute("DROP TABLE IF EXISTS temp.omni_history_stats")
    conn.execute(
        """
        CREATE TEMP TABLE omni_history_stats AS
        SELECT video_id,
               COUNT(*) AS watch_count,
               MAX(COALESCE(watched_at, watch_date, '')) AS latest_watch_at,
               MAX(watch_progress_percent) AS watch_progress_percent,
               COALESCE(
                 9223372036854775807 - CAST(
                   substr(
                     MAX(
                       CASE WHEN youtube_ordinal IS NOT NULL THEN
                         COALESCE(watched_at, watch_date, '') || char(31) ||
                         printf('%019d', 9223372036854775807 - youtube_ordinal)
                       END
                     ),
                     -19
                   ) AS INTEGER
                 ),
                 0
               ) AS latest_youtube_ordinal
        FROM history_events
        GROUP BY video_id
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX temp.idx_omni_history_stats_id "
        "ON omni_history_stats(video_id)"
    )
    conn.execute("DROP TABLE IF EXISTS temp.omni_video_candidates")
    conn.execute(
        f"""
        CREATE TEMP TABLE omni_video_candidates AS
        WITH candidate_videos AS (
          SELECT v.video_id,
                 v.rowid AS source_order,
                 COALESCE(v.title, '') AS title,
                 COALESCE(v.upload_date, '') AS metadata_upload_date,
                 COALESCE(v.updated_at, '') AS updated_at,
                 COALESCE(v.reaction, '') AS reaction,
                 v.is_playable,
                 COALESCE(v.availability, '') AS availability,
                 COALESCE(v.uploader_category, '') AS uploader_category,
                 COALESCE(v.video_type, '') AS video_type,
                 v.broadcast_status,
                 v.ai_disclosure,
                 COALESCE(v.note, '') AS note,
                 CASE WHEN {video_title_hit} THEN 1 ELSE 0 END AS title_hit,
                 CASE WHEN {video_search_match_sql} THEN 1 ELSE 0 END AS plugin_search_hit,
                 CASE WHEN {annotation_search_match_sql} THEN 1 ELSE 0 END AS annotation_search_hit
          FROM videos v
          {candidate_channel_join}
          WHERE ({' OR '.join(f'({match})' for match in video_matches)})
            AND ({video_source_clause})
        ),
        enriched AS (
          SELECT candidate.*,
                 COALESCE(ps.added_at, '') AS added_at,
                 COALESCE(ps.playlist_count, 0) AS playlist_count,
                 COALESCE(hs.watch_count, 0) AS watch_count,
                 COALESCE(hs.latest_watch_at, '') AS latest_watch_at,
                 COALESCE(hs.latest_youtube_ordinal, 0) AS latest_youtube_ordinal,
                 COALESCE(hs.watch_progress_percent, 0) AS watch_progress_percent
          FROM candidate_videos candidate
          LEFT JOIN temp.omni_playlist_stats ps ON ps.video_id = candidate.video_id
          LEFT JOIN temp.omni_history_stats hs ON hs.video_id = candidate.video_id
        ),
        categorized AS (
          SELECT video_id,
                 source_order,
                 title_hit,
                 plugin_search_hit,
                 annotation_search_hit,
                 {availability_sql} AS availability_category,
                 CASE upper(COALESCE(reaction, ''))
                   WHEN 'LIKE' THEN 'liked'
                   WHEN 'DISLIKE' THEN 'disliked'
                   ELSE 'none'
                 END AS reaction_category,
                 CASE
                   WHEN COALESCE(watch_progress_percent, 0) >= 100 THEN 'complete'
                   WHEN COALESCE(watch_progress_percent, 0) > 0
                    AND COALESCE(watch_progress_percent, 0) < :partial_min_percent
                     THEN 'partial_below_minimum'
                   WHEN COALESCE(watch_progress_percent, 0) > 0 THEN 'partial'
                   WHEN COALESCE(watch_count, 0) > 0 THEN 'unknown'
                   ELSE 'never_watched'
                 END AS completion_category,
                 CASE WHEN COALESCE(playlist_count, 0) > 0
                   THEN 'member' ELSE 'non_member' END AS membership_category,
                 CASE lower(trim(COALESCE(video_type, '')))
                   WHEN 'video' THEN 'video'
                   WHEN 'short' THEN 'short'
                   WHEN 'livestream' THEN 'livestream'
                   WHEN 'movie' THEN 'movie'
                   ELSE 'unknown'
                 END AS video_type_category,
                 CASE
                   WHEN lower(trim(COALESCE(video_type, ''))) <> 'livestream'
                     THEN 'not_applicable'
                   ELSE CASE lower(trim(COALESCE(broadcast_status, '')))
                     WHEN 'live' THEN 'live'
                     WHEN 'ended' THEN 'ended'
                     WHEN 'upcoming' THEN 'upcoming'
                     ELSE 'unknown'
                   END
                 END AS broadcast_status_category,
                 CASE ai_disclosure
                   WHEN 1 THEN 'made_with_ai'
                   WHEN 0 THEN 'no_disclosure'
                   ELSE 'unknown'
                 END AS ai_disclosure_category,
                 CASE WHEN trim(COALESCE(uploader_category, '')) = ''
                   THEN '{NO_UPLOADER_CATEGORY_FILTER}'
                   ELSE trim(uploader_category)
                 END AS uploader_category_category,
                 CASE
                   WHEN title_hit = 1 THEN 0
                   WHEN annotation_search_hit = 1 THEN 2
                   ELSE 3
                 END AS score,
                 CASE WHEN trim(note) <> '' THEN 'with_note' ELSE 'without_note' END
                   AS note_presence_category,
                 watch_count,
                 latest_youtube_ordinal,
                 CASE WHEN latest_watch_at = '' THEN 1 ELSE 0 END AS sort_date_fallback,
                 COALESCE(
                   NULLIF(omni_date_sort_at(latest_watch_at, :display_timezone), ''),
                   NULLIF(added_at, ''),
                   NULLIF(metadata_upload_date, ''),
                   updated_at,
                   ''
                 ) AS sort_date,
                 omni_casefold(title) AS sort_title
          FROM enriched
        )
        SELECT * FROM categorized
        """,
        sql_params,
    )
    conn.execute(
        "CREATE UNIQUE INDEX temp.idx_omni_video_candidates_id "
        "ON omni_video_candidates(video_id)"
    )

    plugin_match_clauses = []
    if active_video_id_filters:
        plugin_match_clauses.append(
            """
            (SELECT COUNT(DISTINCT included.filter_index)
             FROM temp.omni_video_included_filters included
             WHERE included.video_id = candidate.video_id) = :included_filter_count
            """
        )
    if active_video_id_exclusion_filters:
        plugin_match_clauses.append(
            """
            NOT EXISTS (
              SELECT 1 FROM temp.omni_video_excluded_filters excluded
              WHERE excluded.video_id = candidate.video_id
            )
            """
        )
    plugin_match_clause = " AND ".join(plugin_match_clauses) or "1"
    native_clauses = [
        _omni_sql_set_clause(
            "candidate.video_type_category",
            selected_video_type_filters,
            "selected_video_type",
            sql_params,
        ),
        _omni_sql_set_clause(
            "candidate.ai_disclosure_category",
            selected_ai_disclosure_filters,
            "selected_ai_disclosure",
            sql_params,
        ),
        _omni_sql_set_clause(
            "candidate.availability_category",
            selected_meta_filters,
            "selected_video_meta",
            sql_params,
        ),
        _omni_sql_set_clause(
            "candidate.reaction_category",
            selected_reaction_filters,
            "selected_video_reaction",
            sql_params,
        ),
        _omni_sql_set_clause(
            "candidate.completion_category",
            selected_completion_filters,
            "selected_video_completion",
            sql_params,
        ),
        _omni_sql_set_clause(
            "candidate.membership_category",
            selected_playlist_membership_filters,
            "selected_video_membership",
            sql_params,
        ),
        _omni_sql_set_clause(
            "candidate.uploader_category_category",
            selected_uploader_category_filters,
            "selected_uploader_category",
            sql_params,
        ),
        _omni_sql_set_clause(
            "candidate.note_presence_category",
            selected_note_filters,
            "selected_video_note",
            sql_params,
        ),
    ]
    broadcast_clause = _omni_sql_set_clause(
        "candidate.broadcast_status_category",
        selected_broadcast_status_filters,
        "selected_broadcast_status",
        sql_params,
    )
    native_clauses.append(
        f"(candidate.broadcast_status_category = 'not_applicable' OR {broadcast_clause})"
    )
    native_filter_clause = " AND ".join(f"({clause})" for clause in native_clauses)

    count_rows = conn.execute(
        f"""
        SELECT 'meta' AS count_type, availability_category AS category, COUNT(*) AS count
        FROM temp.omni_video_candidates candidate
        WHERE {plugin_match_clause}
        GROUP BY availability_category
        UNION ALL
        SELECT 'reaction', reaction_category, COUNT(*)
        FROM temp.omni_video_candidates candidate
        WHERE {plugin_match_clause}
        GROUP BY reaction_category
        UNION ALL
        SELECT 'completion', completion_category, COUNT(*)
        FROM temp.omni_video_candidates candidate
        WHERE {plugin_match_clause}
        GROUP BY completion_category
        UNION ALL
        SELECT 'membership', membership_category, COUNT(*)
        FROM temp.omni_video_candidates candidate
        WHERE {plugin_match_clause}
        GROUP BY membership_category
        UNION ALL
        SELECT 'video_type', video_type_category, COUNT(*)
        FROM temp.omni_video_candidates candidate
        WHERE {plugin_match_clause}
        GROUP BY video_type_category
        UNION ALL
        SELECT 'broadcast_status', broadcast_status_category, COUNT(*)
        FROM temp.omni_video_candidates candidate
        WHERE {plugin_match_clause} AND broadcast_status_category <> 'not_applicable'
        GROUP BY broadcast_status_category
        UNION ALL
        SELECT 'ai_disclosure', ai_disclosure_category, COUNT(*)
        FROM temp.omni_video_candidates candidate
        WHERE {plugin_match_clause}
        GROUP BY ai_disclosure_category
        UNION ALL
        SELECT 'uploader_category', uploader_category_category, COUNT(*)
        FROM temp.omni_video_candidates candidate
        WHERE {plugin_match_clause}
        GROUP BY uploader_category_category
        UNION ALL
        SELECT 'note', note_presence_category, COUNT(*)
        FROM temp.omni_video_candidates candidate
        WHERE {plugin_match_clause}
        GROUP BY note_presence_category
        """,
        sql_params,
    ).fetchall()
    counts = {
        "meta": {category: 0 for category in OMNI_SEARCH_META_FILTERS["video"]},
        "reaction": {category: 0 for category in OMNI_SEARCH_REACTION_FILTERS},
        "completion": {category: 0 for category in OMNI_SEARCH_COMPLETION_FILTERS},
        "membership": {
            category: 0 for category in OMNI_SEARCH_PLAYLIST_MEMBERSHIP_FILTERS
        },
        "video_type": {category: 0 for category in OMNI_SEARCH_VIDEO_TYPE_FILTERS},
        "broadcast_status": {
            category: 0 for category in OMNI_SEARCH_BROADCAST_STATUS_FILTERS
        },
        "ai_disclosure": {
            category: 0 for category in OMNI_SEARCH_AI_DISCLOSURE_FILTERS
        },
        "uploader_category": {
            NO_UPLOADER_CATEGORY_FILTER: 0,
            **{category: 0 for category in known_uploader_categories},
        },
        "note": {category: 0 for category in OMNI_SEARCH_NOTE_FILTERS},
    }
    for row in count_rows:
        counts[row["count_type"]][row["category"]] = int(row["count"] or 0)

    native_total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM temp.omni_video_candidates candidate "
            f"WHERE {native_filter_clause}",
            sql_params,
        ).fetchone()[0]
        or 0
    )
    facet_counts = {}
    for plugin_id in active_video_facet_memberships:
        facet_params = dict(sql_params)
        facet_params["facet_plugin_id"] = plugin_id
        present = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM temp.omni_video_candidates candidate
                WHERE {native_filter_clause}
                  AND EXISTS (
                    SELECT 1 FROM temp.omni_video_facets facet
                    WHERE facet.plugin_id = :facet_plugin_id
                      AND facet.video_id = candidate.video_id
                  )
                """,
                facet_params,
            ).fetchone()[0]
            or 0
        )
        facet_counts[plugin_id] = {
            "present": present,
            "absent": native_total - present,
        }

    filtered_total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM temp.omni_video_candidates candidate "
            f"WHERE {native_filter_clause} AND {plugin_match_clause}",
            sql_params,
        ).fetchone()[0]
        or 0
    )
    order_by = {
        "relevance": "candidate.score, candidate.sort_title, candidate.source_order",
        "title": "candidate.sort_title, candidate.source_order",
        "title_desc": "candidate.sort_title DESC, candidate.source_order",
        "oldest": "candidate.sort_date, candidate.sort_title, candidate.source_order",
        "most_watched": (
            "candidate.watch_count DESC, candidate.sort_title, candidate.source_order"
        ),
        "type": "candidate.sort_title, candidate.source_order",
    }.get(
        sort,
        "candidate.sort_date_fallback, candidate.sort_date DESC, "
        "CASE WHEN candidate.latest_youtube_ordinal = 0 THEN 1 ELSE 0 END, "
        "candidate.latest_youtube_ordinal, candidate.sort_title, candidate.source_order",
    )
    rows = conn.execute(
        f"""
        SELECT *
        FROM temp.omni_video_candidates candidate
        WHERE {native_filter_clause} AND {plugin_match_clause}
        ORDER BY {order_by}
        LIMIT :candidate_limit
        """,
        sql_params,
    ).fetchall()
    results = []
    for row in rows:
        item = {
            "video_id": row["video_id"],
            "availability_category": row["availability_category"],
            "collection_category": row["availability_category"],
        }
        title_hit = bool(row["title_hit"])
        plugin_search_hit = bool(row["plugin_search_hit"])
        annotation_search_hit = bool(row["annotation_search_hit"])
        result = {
            "kind": "video",
            "score": int(row["score"]),
            "matchedDescription": (
                not title_hit and not plugin_search_hit and not annotation_search_hit
            ),
            "item": item,
            "_title": row["sort_title"],
            "_sort_date": row["sort_date"],
            "_sort_date_fallback": bool(row["sort_date_fallback"]),
            "_watch_count": int(row["watch_count"] or 0),
            "_history_ordinal": int(row["latest_youtube_ordinal"] or 0),
            "_clip_feed_ordinal": 0,
        }
        result["pluginSearchMatch"] = plugin_search_hit
        result["pluginSearchMatches"] = sorted(
            plugin_id
            for plugin_id, video_ids in active_video_search_match_memberships.items()
            if item["video_id"] in video_ids
        )
        result["_native_pre_filtered"] = True
        result["_sql_video_candidate"] = True
        results.append(result)
    return {
        "results": results,
        "video_ids": {
            row[0]
            for row in conn.execute(
                "SELECT video_id FROM temp.omni_video_candidates"
            )
        },
        "filtered_total": filtered_total,
        "counts": counts,
        "facet_counts": facet_counts,
    }


def omni_search_data(
    conn: sqlite3.Connection,
    query: str,
    *,
    search_fields: set[str] | None = None,
    result_kinds: set[str] | None = None,
    playlist_group_key: str = "",
    playlist_id_filter: Collection[str] | None = None,
    channel_group_key: str = "",
    channel_id_filter: Collection[str] | None = None,
    video_source: str = "",
    channel_source: str = "",
    video_meta_filters: set[str] | None = None,
    video_reaction_filters: set[str] | None = None,
    video_completion_filters: set[str] | None = None,
    video_partial_min_percent: int = 1,
    video_playlist_membership_filters: set[str] | None = None,
    video_type_filters: set[str] | None = None,
    video_broadcast_status_filters: set[str] | None = None,
    video_ai_disclosure_filters: set[str] | None = None,
    video_uploader_category_filters: set[str] | None = None,
    video_id_filters: Sequence[Collection[str]] = (),
    video_id_exclusion_filters: Sequence[Collection[str]] = (),
    video_facet_memberships: Mapping[str, Collection[str]] | None = None,
    video_search_match_ids: Collection[str] = (),
    video_search_match_memberships: Mapping[str, Collection[str]] | None = None,
    video_projections: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    clip_id_filters: Sequence[Collection[str]] = (),
    clip_id_exclusion_filters: Sequence[Collection[str]] = (),
    clip_facet_memberships: Mapping[str, Collection[str]] | None = None,
    clip_search_match_ids: Collection[str] = (),
    clip_search_match_memberships: Mapping[str, Collection[str]] | None = None,
    channel_subscription_filters: set[str] | None = None,
    channel_status_filters: set[str] | None = None,
    clip_ownership_filters: set[str] | None = None,
    video_note_filters: set[str] | None = None,
    clip_note_filters: set[str] | None = None,
    playlist_note_filters: set[str] | None = None,
    channel_note_filters: set[str] | None = None,
    playlist_meta_filters: set[str] | None = None,
    playlist_ownership_filters: set[str] | None = None,
    sort: str | None = None,
    limit: int = 100,
    offset: int = 0,
    display_timezone: str = "UTC",
) -> dict[str, Any]:
    query = query.strip()
    active_result_kinds = (
        set(OMNI_SEARCH_KIND_ORDER)
        if result_kinds is None
        else set(result_kinds) & set(OMNI_SEARCH_KIND_ORDER)
    )
    playlist_group_key = playlist_group_key.strip()
    channel_group_key = channel_group_key.strip()
    video_source = video_source if video_source in {"playlist_member", "liked"} else ""
    channel_source = channel_source if channel_source in {"subscribed", "terminated"} else ""
    active_search_fields = set(
        search_fields if search_fields is not None else OMNI_SEARCH_FIELDS
    ) & OMNI_SEARCH_FIELDS
    default_sort = "relevance" if query else "newest"
    sort = sort if sort in OMNI_SEARCH_SORTS else default_sort
    limit = max(1, min(int(limit), 5000))
    offset = max(0, int(offset))
    pattern = _omni_like_pattern(query) if query else "%"
    params = {
        "pattern": pattern,
        "playlist_group_key": playlist_group_key,
        "channel_group_key": channel_group_key,
        "video_source": video_source,
        "channel_source": channel_source,
    }
    active_playlist_id_filter = (
        None
        if playlist_id_filter is None
        else frozenset(
            playlist_id
            for value in playlist_id_filter
            if (playlist_id := str(value).strip())
        )
    )
    if active_playlist_id_filter is not None:
        conn.execute("DROP TABLE IF EXISTS temp.omni_playlist_group_filter")
        conn.execute(
            """
            CREATE TEMP TABLE omni_playlist_group_filter(
              playlist_id TEXT PRIMARY KEY
            ) WITHOUT ROWID
            """
        )
        conn.executemany(
            "INSERT INTO temp.omni_playlist_group_filter(playlist_id) VALUES (?)",
            ((playlist_id,) for playlist_id in active_playlist_id_filter),
        )
    playlist_group_filter_sql = (
        """
        EXISTS (
          SELECT 1
          FROM temp.omni_playlist_group_filter plugin_group
          WHERE plugin_group.playlist_id = p.playlist_id
        )
        """
        if active_playlist_id_filter is not None
        else """
        :playlist_group_key = ''
        OR EXISTS (
          SELECT 1
          FROM group_playlists gp
          WHERE gp.playlist_id = p.playlist_id
            AND (
              gp.group_key = :playlist_group_key
              OR gp.group_key IN (
                SELECT group_key
                FROM groups
                WHERE parent_key = :playlist_group_key
              )
            )
        )
        """
    )
    active_channel_id_filter = (
        None
        if channel_id_filter is None
        else frozenset(
            channel_id
            for value in channel_id_filter
            if (channel_id := str(value).strip())
        )
    )
    if active_channel_id_filter is not None:
        conn.execute("DROP TABLE IF EXISTS temp.omni_channel_group_filter")
        conn.execute(
            """
            CREATE TEMP TABLE omni_channel_group_filter(
              channel_id TEXT PRIMARY KEY
            ) WITHOUT ROWID
            """
        )
        conn.executemany(
            "INSERT INTO temp.omni_channel_group_filter(channel_id) VALUES (?)",
            ((channel_id,) for channel_id in active_channel_id_filter),
        )
    channel_group_filter_sql = (
        """
        EXISTS (
          SELECT 1
          FROM temp.omni_channel_group_filter plugin_group
          WHERE plugin_group.channel_id = ch.channel_id
        )
        """
        if active_channel_id_filter is not None
        else ":channel_group_key = ''"
    )
    search_titles = "titles" in active_search_fields
    search_descriptions = "descriptions" in active_search_fields
    annotation_matches = annotation_search_matches(
        conn,
        query,
        search_notes="notes" in active_search_fields,
        search_tags="tags" in active_search_fields,
        entity_kinds=active_result_kinds,
    )
    conn.execute("DROP TABLE IF EXISTS temp.omni_annotation_search_matches")
    conn.execute(
        """
        CREATE TEMP TABLE omni_annotation_search_matches(
          entity_kind TEXT NOT NULL,
          entity_id TEXT NOT NULL,
          PRIMARY KEY(entity_kind, entity_id)
        ) WITHOUT ROWID
        """
    )
    conn.executemany(
        """
        INSERT INTO temp.omni_annotation_search_matches(entity_kind, entity_id)
        VALUES (?, ?)
        """,
        (
            (entity_kind, entity_id)
            for entity_kind, entity_ids in annotation_matches.items()
            for entity_id in entity_ids
        ),
    )
    annotation_match_sql = {
        "video": (
            "EXISTS (SELECT 1 FROM temp.omni_annotation_search_matches annotation "
            "WHERE annotation.entity_kind = 'video' AND annotation.entity_id = v.video_id)"
        ),
        "clip": (
            "EXISTS (SELECT 1 FROM temp.omni_annotation_search_matches annotation "
            "WHERE annotation.entity_kind = 'clip' AND annotation.entity_id = c.clip_id)"
        ),
        "playlist": (
            "EXISTS (SELECT 1 FROM temp.omni_annotation_search_matches annotation "
            "WHERE annotation.entity_kind = 'playlist' AND annotation.entity_id = p.playlist_id)"
        ),
        "channel": (
            "EXISTS (SELECT 1 FROM temp.omni_annotation_search_matches annotation "
            "WHERE annotation.entity_kind = 'channel' AND annotation.entity_id = ch.channel_id)"
        ),
    }
    active_video_id_filters = [frozenset(values) for values in video_id_filters]
    active_video_id_exclusion_filters = [
        frozenset(values) for values in video_id_exclusion_filters
    ]
    active_video_facet_memberships = {
        str(plugin_id): frozenset(values)
        for plugin_id, values in (video_facet_memberships or {}).items()
    }
    active_video_search_match_memberships = {
        str(plugin_id): frozenset(values)
        for plugin_id, values in (video_search_match_memberships or {}).items()
    }
    active_video_projections = {
        str(plugin_id): {
            str(video_id): projection
            for video_id, projection in projections.items()
            if isinstance(projection, Mapping)
        }
        for plugin_id, projections in (video_projections or {}).items()
        if isinstance(projections, Mapping)
    }
    active_video_search_match_ids = frozenset(video_search_match_ids).union(
        *active_video_search_match_memberships.values()
    )
    has_video_search_matches = bool(query and active_video_search_match_ids)
    if has_video_search_matches:
        conn.execute("DROP TABLE IF EXISTS temp.omni_video_search_matches")
        conn.execute(
            """
            CREATE TEMP TABLE omni_video_search_matches(
              video_id TEXT PRIMARY KEY
            ) WITHOUT ROWID
            """
        )
        conn.executemany(
            "INSERT INTO temp.omni_video_search_matches(video_id) VALUES (?)",
            ((video_id,) for video_id in active_video_search_match_ids),
        )
    video_search_match_sql = (
        "EXISTS (SELECT 1 FROM temp.omni_video_search_matches "
        "WHERE video_id = v.video_id)"
        if has_video_search_matches
        else "0"
    )
    active_clip_id_filters = [frozenset(values) for values in clip_id_filters]
    active_clip_id_exclusion_filters = [
        frozenset(values) for values in clip_id_exclusion_filters
    ]
    active_clip_facet_memberships = {
        str(plugin_id): frozenset(values)
        for plugin_id, values in (clip_facet_memberships or {}).items()
    }
    active_clip_search_match_memberships = {
        str(plugin_id): frozenset(values)
        for plugin_id, values in (clip_search_match_memberships or {}).items()
    }
    active_clip_search_match_ids = frozenset(clip_search_match_ids).union(
        *active_clip_search_match_memberships.values()
    )
    has_clip_search_matches = bool(query and active_clip_search_match_ids)
    if has_clip_search_matches:
        conn.execute("DROP TABLE IF EXISTS temp.omni_clip_search_matches")
        conn.execute(
            """
            CREATE TEMP TABLE omni_clip_search_matches(
              clip_id TEXT PRIMARY KEY
            ) WITHOUT ROWID
            """
        )
        conn.executemany(
            "INSERT INTO temp.omni_clip_search_matches(clip_id) VALUES (?)",
            ((clip_id,) for clip_id in active_clip_search_match_ids),
        )
    clip_search_match_sql = (
        "EXISTS (SELECT 1 FROM temp.omni_clip_search_matches "
        "WHERE clip_id = c.clip_id)"
        if has_clip_search_matches
        else "0"
    )
    video_partial_min_percent = _bounded_partial_min_percent(
        video_partial_min_percent
    )
    selected_meta_filters = {
        "video": _selected_omni_meta_filters(video_meta_filters, "video"),
        "playlist": _selected_omni_meta_filters(playlist_meta_filters, "playlist"),
    }
    selected_reaction_filters = (
        set(OMNI_SEARCH_REACTION_FILTERS)
        if video_reaction_filters is None
        else set(video_reaction_filters) & set(OMNI_SEARCH_REACTION_FILTERS)
    )
    selected_completion_filters = (
        set(OMNI_SEARCH_COMPLETION_FILTERS)
        if video_completion_filters is None
        else set(video_completion_filters) & set(OMNI_SEARCH_COMPLETION_FILTERS)
    )
    selected_playlist_membership_filters = (
        set(OMNI_SEARCH_PLAYLIST_MEMBERSHIP_FILTERS)
        if video_playlist_membership_filters is None
        else set(video_playlist_membership_filters)
        & set(OMNI_SEARCH_PLAYLIST_MEMBERSHIP_FILTERS)
    )
    selected_video_type_filters = (
        set(OMNI_SEARCH_VIDEO_TYPE_FILTERS)
        if video_type_filters is None
        else set(video_type_filters) & set(OMNI_SEARCH_VIDEO_TYPE_FILTERS)
    )
    selected_broadcast_status_filters = (
        set(OMNI_SEARCH_BROADCAST_STATUS_FILTERS)
        if video_broadcast_status_filters is None
        else set(video_broadcast_status_filters)
        & set(OMNI_SEARCH_BROADCAST_STATUS_FILTERS)
    )
    selected_ai_disclosure_filters = (
        set(OMNI_SEARCH_AI_DISCLOSURE_FILTERS)
        if video_ai_disclosure_filters is None
        else set(video_ai_disclosure_filters)
        & set(OMNI_SEARCH_AI_DISCLOSURE_FILTERS)
    )
    known_uploader_categories = _known_uploader_categories(conn)
    allowed_uploader_category_filters = {
        NO_UPLOADER_CATEGORY_FILTER,
        *known_uploader_categories,
    }
    selected_uploader_category_filters = (
        allowed_uploader_category_filters
        if video_uploader_category_filters is None
        else set(video_uploader_category_filters) & allowed_uploader_category_filters
    )
    selected_playlist_ownership_filters = (
        set(OMNI_SEARCH_PLAYLIST_OWNERSHIP_FILTERS)
        if playlist_ownership_filters is None
        else set(playlist_ownership_filters) & set(OMNI_SEARCH_PLAYLIST_OWNERSHIP_FILTERS)
    )
    selected_channel_subscription_filters = (
        set(OMNI_SEARCH_CHANNEL_SUBSCRIPTION_FILTERS)
        if channel_subscription_filters is None
        else set(channel_subscription_filters)
        & set(OMNI_SEARCH_CHANNEL_SUBSCRIPTION_FILTERS)
    )
    selected_channel_status_filters = (
        set(OMNI_SEARCH_CHANNEL_STATUS_FILTERS)
        if channel_status_filters is None
        else set(channel_status_filters) & set(OMNI_SEARCH_CHANNEL_STATUS_FILTERS)
    )
    selected_clip_ownership_filters = (
        set(OMNI_SEARCH_CLIP_OWNERSHIP_FILTERS)
        if clip_ownership_filters is None
        else set(clip_ownership_filters) & set(OMNI_SEARCH_CLIP_OWNERSHIP_FILTERS)
    )
    selected_note_filters = {
        "video": (
            set(OMNI_SEARCH_NOTE_FILTERS)
            if video_note_filters is None
            else set(video_note_filters) & set(OMNI_SEARCH_NOTE_FILTERS)
        ),
        "clip": (
            set(OMNI_SEARCH_NOTE_FILTERS)
            if clip_note_filters is None
            else set(clip_note_filters) & set(OMNI_SEARCH_NOTE_FILTERS)
        ),
        "playlist": (
            set(OMNI_SEARCH_NOTE_FILTERS)
            if playlist_note_filters is None
            else set(playlist_note_filters) & set(OMNI_SEARCH_NOTE_FILTERS)
        ),
        "channel": (
            set(OMNI_SEARCH_NOTE_FILTERS)
            if channel_note_filters is None
            else set(channel_note_filters) & set(OMNI_SEARCH_NOTE_FILTERS)
        ),
    }
    results: list[dict[str, Any]] = []
    video_sql_data: dict[str, Any] = {
        "filtered_total": 0,
        "counts": {},
        "facet_counts": {},
    }

    if "clip" in active_result_kinds and (
        not query
        or search_titles
        or has_clip_search_matches
        or bool(annotation_matches.get("clip"))
    ):
        clip_title_match = """
            lower(
              c.title || ' ' || c.clip_id || ' ' || c.owner_title || ' ' ||
              COALESCE(owner.title, '') || ' ' || COALESCE(source.title, '') || ' ' ||
              COALESCE(source_channel.title, '') || ' ' || COALESCE(c.source_video_id, '')
            ) LIKE :pattern ESCAPE '\\'
        """
        clip_matches = []
        if query:
            if search_titles:
                clip_matches.append(clip_title_match)
            if has_clip_search_matches:
                clip_matches.append(clip_search_match_sql)
            if annotation_matches.get("clip"):
                clip_matches.append(annotation_match_sql["clip"])
        else:
            clip_matches.append("1 = 1")
        clip_title_hit = "1" if not query else (clip_title_match if search_titles else "0")
        for row in conn.execute(
            f"""
            SELECT c.*,
                   COALESCE(owner.title, c.owner_title) AS resolved_owner_title,
                   COALESCE(owner.aliases, '') AS owner_aliases,
                   COALESCE(owner.thumbnail_path, c.owner_thumbnail_path) AS resolved_owner_thumbnail_path,
                   COALESCE(source.title, '') AS source_video_title,
                   COALESCE(source.thumbnail_path, '') AS source_thumbnail_path,
                   COALESCE(source.reaction, '') AS reaction,
                   COALESCE(source.uploader_category, '') AS uploader_category,
                   COALESCE(source.availability, 'unknown') AS source_availability,
                   source.is_playable AS source_is_playable,
                   COALESCE(source.channel_id, '') AS source_channel_id,
                   COALESCE(source_channel.title, '') AS source_channel_title,
                   COALESCE(source_channel.aliases, '') AS source_channel_aliases,
                   COALESCE(source_channel.thumbnail_path, '') AS source_channel_thumbnail_path,
                   CASE WHEN {clip_title_hit} THEN 1 ELSE 0 END AS title_hit,
                   CASE WHEN {clip_search_match_sql} THEN 1 ELSE 0 END AS plugin_search_hit,
                   CASE WHEN {annotation_match_sql['clip']} THEN 1 ELSE 0 END AS annotation_search_hit
            FROM clips c
            LEFT JOIN channels owner ON owner.channel_id = c.owner_channel_id
            LEFT JOIN videos source ON source.video_id = c.source_video_id
            LEFT JOIN channels source_channel ON source_channel.channel_id = source.channel_id
            WHERE ({' OR '.join(f'({match})' for match in clip_matches)})
            """,
            params,
        ):
            item = dict(row)
            title_hit = bool(item.pop("title_hit"))
            plugin_search_hit = bool(item.pop("plugin_search_hit"))
            annotation_search_hit = bool(item.pop("annotation_search_hit"))
            item["video_id"] = item.get("source_video_id") or ""
            item["url"] = f"https://www.youtube.com/clip/{urllib.parse.quote(item['clip_id'])}"
            item["owner_channel_reference"] = preferred_youtube_channel_reference(
                item.get("owner_channel_id") or "",
                item.get("owner_aliases") or "",
            )
            item["owner_channel_url"] = preferred_youtube_channel_url(
                item.get("owner_channel_id") or "",
                item.get("owner_aliases") or "",
            )
            source_channel_id = str(item.get("source_channel_id") or "")
            item["source_channel_reference"] = preferred_youtube_channel_reference(
                source_channel_id,
                item.get("source_channel_aliases") or "",
            )
            item["source_channel_url"] = preferred_youtube_channel_url(
                source_channel_id,
                item.get("source_channel_aliases") or "",
            )
            result = _omni_result(
                    "clip",
                    1 if title_hit else 2 if annotation_search_hit else 4,
                    item,
                    matched_description=False,
                )
            result["pluginSearchMatch"] = plugin_search_hit
            result["pluginSearchMatches"] = sorted(
                plugin_id
                for plugin_id, clip_ids in active_clip_search_match_memberships.items()
                if str(item.get("clip_id") or "") in clip_ids
            )
            results.append(result)

    if "playlist" in active_result_kinds and (
        not query or search_titles or search_descriptions or bool(annotation_matches.get("playlist"))
    ):
        playlist_title_match = """
            lower(
              p.title || ' ' || COALESCE(owner.title, '') || ' ' ||
              COALESCE(p.owner_channel_id, '') || ' ' || p.visibility || ' ' || p.playlist_id
            ) LIKE :pattern ESCAPE '\\'
        """
        playlist_description_match = "lower(p.description) LIKE :pattern ESCAPE '\\'"
        playlist_matches = []
        if query:
            if search_titles:
                playlist_matches.append(playlist_title_match)
            if search_descriptions:
                playlist_matches.append(playlist_description_match)
            if annotation_matches.get("playlist"):
                playlist_matches.append(annotation_match_sql["playlist"])
        else:
            playlist_matches.append("1 = 1")
        playlist_title_hit = (
            "1" if not query else (playlist_title_match if search_titles else "0")
        )
        for row in conn.execute(
            f"""
            WITH {_playlist_unavailable_counts_ctes()}
            SELECT p.*,
                   COALESCE(ps.video_count, 0) AS scanned_video_count,
                   COALESCE(puc.unavailable_count, 0) AS unavailable_count,
                   ps.scanned_at,
                   COALESCE(ps.scan_status, '') AS scan_status,
                   COALESCE(owner.title, '') AS owner_channel_title,
                   COALESCE(owner.aliases, '') AS owner_channel_aliases,
                   COALESCE(owner.thumbnail_path, '') AS owner_channel_thumbnail_path,
                   COALESCE(owner.status, '') AS owner_channel_status,
                   CASE WHEN {playlist_title_hit} THEN 1 ELSE 0 END AS title_hit,
                   CASE WHEN {annotation_match_sql['playlist']} THEN 1 ELSE 0 END AS annotation_search_hit
            FROM playlists p
            LEFT JOIN playlist_scans ps ON ps.playlist_id = p.playlist_id
            LEFT JOIN playlist_unavailable_counts puc
              ON puc.playlist_id = p.playlist_id
            LEFT JOIN channels owner ON owner.channel_id = p.owner_channel_id
            WHERE ({' OR '.join(f'({match})' for match in playlist_matches)})
              AND ({playlist_group_filter_sql})
            """,
            params,
        ):
            item = dict(row)
            title_hit = bool(item.pop("title_hit"))
            annotation_search_hit = bool(item.pop("annotation_search_hit"))
            item["url"] = youtube_playlist_url(item.get("playlist_id") or "")
            item["owner_channel_reference"] = preferred_youtube_channel_reference(
                item.get("owner_channel_id") or "",
                item.get("owner_channel_aliases") or "",
            )
            item["owner_channel_url"] = preferred_youtube_channel_url(
                item.get("owner_channel_id") or "",
                item.get("owner_channel_aliases") or "",
            )
            results.append(_omni_result(
                "playlist",
                2 if title_hit else 3 if annotation_search_hit else 5,
                item,
                matched_description=not title_hit and not annotation_search_hit,
            ))

    if "channel" in active_result_kinds and (
        not query or search_titles or search_descriptions or bool(annotation_matches.get("channel"))
    ):
        channel_title_match = """
            lower(
              ch.title || ' ' || ch.channel_id || ' ' || ch.aliases || ' ' ||
              ch.status
            ) LIKE :pattern ESCAPE '\\'
        """
        channel_description_match = "lower(ch.description || ' ' || ch.status_reason) LIKE :pattern ESCAPE '\\'"
        channel_matches = []
        if query:
            if search_titles:
                channel_matches.append(channel_title_match)
            if search_descriptions:
                channel_matches.append(channel_description_match)
            if annotation_matches.get("channel"):
                channel_matches.append(annotation_match_sql["channel"])
        else:
            channel_matches.append("1 = 1")
        channel_title_hit = (
            "1" if not query else (channel_title_match if search_titles else "0")
        )
        for row in conn.execute(
            f"""
            SELECT ch.*,
                   CASE WHEN {channel_title_hit} THEN 1 ELSE 0 END AS title_hit,
                   CASE WHEN {annotation_match_sql['channel']} THEN 1 ELSE 0 END AS annotation_search_hit
            FROM channels ch
            WHERE ({' OR '.join(f'({match})' for match in channel_matches)})
              AND ({channel_group_filter_sql})
              AND (
                :channel_source = ''
                OR (:channel_source = 'subscribed' AND ch.subscribed = 1)
                OR (
                  :channel_source = 'terminated'
                  AND lower(COALESCE(ch.status, '')) IN ('terminated', 'deleted')
                )
              )
            """,
            params,
        ):
            item = dict(row)
            title_hit = bool(item.pop("title_hit"))
            annotation_search_hit = bool(item.pop("annotation_search_hit"))
            item["preferred_reference"] = preferred_youtube_channel_reference(
                item.get("channel_id") or "",
                item.get("aliases") or "",
            )
            item["url"] = preferred_youtube_channel_url(
                item.get("channel_id") or "",
                item.get("aliases") or "",
            )
            results.append(_omni_result(
                "channel",
                1 if title_hit else 2 if annotation_search_hit else 4,
                item,
                matched_description=not title_hit and not annotation_search_hit,
            ))

    if (
        "video" in active_result_kinds
        and (
            not query
            or search_titles
            or search_descriptions
            or has_video_search_matches
            or bool(annotation_matches.get("video"))
        )
    ):
        video_sql_data = _omni_video_sql_data(
            conn,
            params=params,
            query=query,
            search_titles=search_titles,
            search_descriptions=search_descriptions,
            has_annotation_search_matches=bool(annotation_matches.get("video")),
            annotation_search_match_sql=annotation_match_sql["video"],
            has_video_search_matches=has_video_search_matches,
            video_search_match_sql=video_search_match_sql,
            video_source=video_source,
            selected_meta_filters=selected_meta_filters["video"],
            selected_reaction_filters=selected_reaction_filters,
            selected_completion_filters=selected_completion_filters,
            selected_playlist_membership_filters=selected_playlist_membership_filters,
            selected_video_type_filters=selected_video_type_filters,
            selected_broadcast_status_filters=selected_broadcast_status_filters,
            selected_ai_disclosure_filters=selected_ai_disclosure_filters,
            selected_uploader_category_filters=selected_uploader_category_filters,
            selected_note_filters=selected_note_filters["video"],
            known_uploader_categories=known_uploader_categories,
            partial_min_percent=video_partial_min_percent,
            active_video_id_filters=active_video_id_filters,
            active_video_id_exclusion_filters=active_video_id_exclusion_filters,
            active_video_facet_memberships=active_video_facet_memberships,
            active_video_search_match_memberships=active_video_search_match_memberships,
            sort=sort,
            candidate_limit=offset + limit,
            display_timezone=display_timezone,
        )
        results.extend(video_sql_data["results"])

    if "video" in active_result_kinds and video_source == "":
        library_video_ids = set(video_sql_data.get("video_ids") or ())
        projected_items: dict[str, dict[str, Any]] = {}
        for plugin_id, projections in active_video_projections.items():
            search_matches = active_video_search_match_memberships.get(
                plugin_id,
                frozenset(),
            )
            for video_id, projection in projections.items():
                if (
                    not video_id
                    or video_id in library_video_ids
                    or (query and video_id not in search_matches)
                ):
                    continue
                item = projected_items.get(video_id)
                if item is None:
                    item = projected_video_data(projection)
                    item["projection_plugin_ids"] = []
                    projected_items[video_id] = item
                if not item["metadata_title"] and projection.get("title"):
                    title = str(projection["title"]).strip()
                    item["title"] = title
                    item["metadata_title"] = title
                item["projection_plugin_ids"].append(plugin_id)
        for video_id, item in projected_items.items():
            matching_plugin_ids = sorted(
                plugin_id
                for plugin_id, video_ids in active_video_search_match_memberships.items()
                if video_id in video_ids
            )
            item["projection_plugin_ids"] = sorted(
                set(item["projection_plugin_ids"])
            )
            result = _omni_result(
                "video",
                3 if query else 0,
                item,
                matched_description=False,
                display_timezone=display_timezone,
            )
            result["pluginSearchMatch"] = bool(query and matching_plugin_ids)
            result["pluginSearchMatches"] = matching_plugin_ids
            results.append(result)

    if (
        "video" in active_result_kinds
        and video_source != "liked"
        and (not query or search_titles)
    ):
        for row in conn.execute(
            """
            SELECT pi.playlist_id,
                   pi.position,
                   pi.membership_state,
                   pi.unavailable_kind,
                   pi.source_quality,
                   pi.match_type,
                   pi.match_confidence,
                   pi.added_at,
                   pi.updated_at,
                   p.title AS playlist_title
            FROM playlist_items pi
            JOIN playlists p ON p.playlist_id = pi.playlist_id
            WHERE pi.video_id IS NULL
              AND lower(
                p.title || ' ' || p.playlist_id || ' ' || pi.unavailable_kind || ' unavailable video'
              ) LIKE :pattern ESCAPE '\\'
            """,
            params,
        ):
            item = dict(row)
            item.update(
                {
                    "video_id": "",
                    "title": "Unavailable video",
                    "metadata_title": "Unavailable video",
                    "metadata_description": "",
                    "metadata_thumbnail_path": "",
                    "metadata_channel_thumbnail_path": "",
                    "metadata_channel": "",
                    "metadata_channel_id": "",
                    "metadata_duration": "",
                    "reaction": "",
                    "is_playable": 0,
                    "availability": item.get("unavailable_kind") or "unavailable",
                    "playlist_count": 1,
                    "watch_count": 0,
                    "watch_dates": [],
                    "collection_category": "unavailable",
                    "availability_category": "unavailable",
                    "playlist_url": youtube_playlist_url(item.get("playlist_id") or ""),
                    "playlist_links": [
                        {
                            "playlist_id": item.get("playlist_id") or "",
                            "title": item.get("playlist_title") or item.get("playlist_id") or "",
                            "removed": False,
                        }
                    ],
                }
            )
            item["match_label"] = playlist_match_type_label(item.get("match_type") or "")
            item["match_note"] = playlist_match_type_note(item.get("match_type") or "")
            results.append(_omni_result("video", 0, item, matched_description=False))

    _assign_omni_meta_categories(conn, results)
    meta_counts = _omni_meta_counts([])
    reaction_counts = _omni_reaction_counts([])
    video_type_counts = _omni_video_type_counts([])
    broadcast_status_counts = _omni_video_broadcast_status_counts([])
    ai_disclosure_counts = _omni_video_ai_disclosure_counts([])
    completion_counts = _omni_completion_counts([], video_partial_min_percent)
    playlist_membership_counts = _omni_playlist_membership_counts([])
    uploader_category_counts = _omni_uploader_category_counts(
        [],
        known_uploader_categories,
    )
    video_facet_counts = {
        plugin_id: {"present": 0, "absent": 0}
        for plugin_id in active_video_facet_memberships
    }
    clip_facet_counts = {
        plugin_id: {"present": 0, "absent": 0}
        for plugin_id in active_clip_facet_memberships
    }
    filtered_results: list[dict[str, Any]] = []

    for result in results:
        kind = result["kind"]
        item = result["item"]
        native_match = False
        plugin_match = True

        if kind == "channel":
            native_match = (
                result["channelSubscription"] in selected_channel_subscription_filters
                and result["channelStatus"] in selected_channel_status_filters
                and result["notePresence"] in selected_note_filters["channel"]
            )
        elif kind == "playlist":
            native_match = (
                result["metaCategory"] in selected_meta_filters[kind]
                and result["playlistOwnership"] in selected_playlist_ownership_filters
                and result["notePresence"] in selected_note_filters["playlist"]
            )
        elif kind == "clip":
            clip_id = str(item.get("clip_id") or "")
            native_match = (
                result["clipOwnership"] in selected_clip_ownership_filters
                and result["notePresence"] in selected_note_filters["clip"]
            )
            for plugin_id, clip_ids in active_clip_facet_memberships.items():
                if native_match:
                    facet = "present" if clip_id in clip_ids else "absent"
                    clip_facet_counts[plugin_id][facet] += 1
            plugin_match = all(
                clip_id in clip_ids for clip_ids in active_clip_id_filters
            ) and all(
                clip_id not in clip_ids
                for clip_ids in active_clip_id_exclusion_filters
            )
        elif kind == "video":
            if result.pop("_native_pre_filtered", False):
                filtered_results.append(result)
                continue
            video_id = str(item.get("video_id") or "")
            video_type_category = _omni_video_type_category(result)
            broadcast_status_category = _omni_video_broadcast_status_category(result)
            ai_disclosure_category = _omni_video_ai_disclosure_category(result)
            reaction_category = _omni_video_reaction_category(result)
            completion_category = _omni_video_completion_category(
                result,
                video_partial_min_percent,
            )
            membership_category = _omni_video_playlist_membership_category(result)
            uploader_category = _omni_video_uploader_category(result)
            native_match = (
                video_type_category in selected_video_type_filters
                and (
                    broadcast_status_category == "not_applicable"
                    or broadcast_status_category in selected_broadcast_status_filters
                )
                and ai_disclosure_category in selected_ai_disclosure_filters
                and result["metaCategory"] in selected_meta_filters[kind]
                and reaction_category in selected_reaction_filters
                and completion_category in selected_completion_filters
                and membership_category in selected_playlist_membership_filters
                and uploader_category in selected_uploader_category_filters
                and result["notePresence"] in selected_note_filters["video"]
            )
            for plugin_id, video_ids in active_video_facet_memberships.items():
                if native_match:
                    facet = "present" if video_id in video_ids else "absent"
                    video_facet_counts[plugin_id][facet] += 1
            plugin_match = all(
                video_id in video_ids for video_ids in active_video_id_filters
            ) and all(
                video_id not in video_ids
                for video_ids in active_video_id_exclusion_filters
            )

        if not plugin_match:
            continue

        group = meta_counts[f"{kind}s"]
        group["total"] += 1
        group[result["notePresence"]] += 1
        if kind == "channel":
            group[result["channelSubscription"]] += 1
            group[result["channelStatus"]] += 1
        elif kind == "playlist":
            group[result["metaCategory"]] += 1
            group[result["playlistOwnership"]] += 1
        elif kind == "clip":
            group[result["clipOwnership"]] += 1
        elif kind == "video":
            group[result["metaCategory"]] += 1
            reaction_counts["total"] += 1
            reaction_counts[reaction_category] += 1
            video_type_counts["total"] += 1
            video_type_counts[video_type_category] += 1
            if broadcast_status_category != "not_applicable":
                broadcast_status_counts["total"] += 1
                broadcast_status_counts[broadcast_status_category] += 1
            ai_disclosure_counts["total"] += 1
            ai_disclosure_counts[ai_disclosure_category] += 1
            completion_counts["total"] += 1
            completion_counts[completion_category] += 1
            playlist_membership_counts["total"] += 1
            playlist_membership_counts[membership_category] += 1
            uploader_category_counts["total"] += 1
            uploader_category_counts.setdefault(uploader_category, 0)
            uploader_category_counts[uploader_category] += 1

        if native_match:
            filtered_results.append(result)

    sql_counts = video_sql_data.get("counts") or {}
    for category, count in sql_counts.get("meta", {}).items():
        meta_counts["videos"][category] += count
        meta_counts["videos"]["total"] += count
    for category, count in sql_counts.get("reaction", {}).items():
        reaction_counts[category] += count
        reaction_counts["total"] += count
    for category, count in sql_counts.get("video_type", {}).items():
        video_type_counts[category] += count
        video_type_counts["total"] += count
    for category, count in sql_counts.get("broadcast_status", {}).items():
        broadcast_status_counts[category] += count
        broadcast_status_counts["total"] += count
    for category, count in sql_counts.get("ai_disclosure", {}).items():
        ai_disclosure_counts[category] += count
        ai_disclosure_counts["total"] += count
    for category, count in sql_counts.get("completion", {}).items():
        completion_counts[category] += count
        completion_counts["total"] += count
    for category, count in sql_counts.get("membership", {}).items():
        playlist_membership_counts[category] += count
        playlist_membership_counts["total"] += count
    for category, count in sql_counts.get("uploader_category", {}).items():
        uploader_category_counts.setdefault(category, 0)
        uploader_category_counts[category] += count
        uploader_category_counts["total"] += count
    for category, count in sql_counts.get("note", {}).items():
        meta_counts["videos"][category] += count
    for plugin_id, counts in (video_sql_data.get("facet_counts") or {}).items():
        for category in ("present", "absent"):
            video_facet_counts[plugin_id][category] += counts[category]

    if video_facet_counts:
        meta_counts["videoPlugins"] = video_facet_counts
    if clip_facet_counts:
        meta_counts["clipPlugins"] = clip_facet_counts
    sql_video_results = []
    other_results = []
    supplemental_video_results = []
    for result in filtered_results:
        if result.pop("_sql_video_candidate", False):
            sql_video_results.append(result)
        elif result["kind"] == "video":
            supplemental_video_results.append(result)
        else:
            other_results.append(result)
    results = [*other_results, *sql_video_results, *supplemental_video_results]
    _sort_omni_results(results, sort)
    materialized_video_count = len(sql_video_results)
    total = (
        len(results)
        - materialized_video_count
        + int(video_sql_data["filtered_total"] or 0)
    )
    if total and offset >= total:
        offset = ((total - 1) // limit) * limit
    page = results[offset : offset + limit]
    _attach_playlist_collaborators(
        conn,
        [result["item"] for result in page if result["kind"] == "playlist"],
    )
    _attach_channel_featured_channels(
        conn,
        [result["item"] for result in page if result["kind"] == "channel"],
    )
    for entity_kind in ("clip", "playlist", "channel"):
        attach_annotations(
            conn,
            entity_kind,
            [result["item"] for result in page if result["kind"] == entity_kind],
        )
    for result in page:
        if result["kind"] == "video":
            video_id = str(result["item"].get("video_id") or "")
            result["pluginFacets"] = {
                plugin_id: video_id in video_ids
                for plugin_id, video_ids in active_video_facet_memberships.items()
            }
        elif result["kind"] == "clip":
            clip_id = str(result["item"].get("clip_id") or "")
            result["pluginFacets"] = {
                plugin_id: clip_id in clip_ids
                for plugin_id, clip_ids in active_clip_facet_memberships.items()
            }
    _hydrate_omni_videos(conn, page)
    _add_omni_video_links(conn, page)
    counts = {
        "videos": int(video_sql_data["filtered_total"] or 0)
        + len(supplemental_video_results),
        "clips": sum(1 for result in results if result["kind"] == "clip"),
        "playlists": sum(1 for result in results if result["kind"] == "playlist"),
        "channels": sum(1 for result in results if result["kind"] == "channel"),
    }
    for result in page:
        result.pop("_title", None)
        result.pop("_sort_date", None)
        result.pop("_sort_date_fallback", None)
        result.pop("_watch_count", None)
        result.pop("_history_ordinal", None)
    return {
        "query": query,
        "searchFields": sorted(active_search_fields),
        "resultKinds": sorted(active_result_kinds),
        "playlistGroupKey": playlist_group_key,
        "channelGroupKey": channel_group_key,
        "videoSource": video_source,
        "channelSource": channel_source,
        "sort": sort,
        "limit": limit,
        "offset": offset,
        "total": total,
        "counts": counts,
        "metaCounts": meta_counts,
        "reactionCounts": reaction_counts,
        "videoTypeCounts": video_type_counts,
        "broadcastStatusCounts": broadcast_status_counts,
        "aiDisclosureCounts": ai_disclosure_counts,
        "completionCounts": completion_counts,
        "playlistMembershipCounts": playlist_membership_counts,
        "uploaderCategoryCounts": uploader_category_counts,
        "results": page,
    }


def clip_detail_data(conn: sqlite3.Connection, clip_id: str) -> dict[str, Any] | None:
    payload = omni_search_data(
        conn,
        clip_id,
        search_fields={"titles"},
        result_kinds={"clip"},
        limit=10,
    )
    for result in payload["results"]:
        if result["kind"] == "clip" and result["item"].get("clip_id") == clip_id:
            item = result["item"]
            item["pluginFacets"] = result.get("pluginFacets", {})
            return item
    return None


HISTORY_SEARCH_FIELDS = {"titles", "descriptions"}


def _active_history_search_fields(search_fields: set[str] | None) -> set[str]:
    return HISTORY_SEARCH_FIELDS.copy() if search_fields is None else search_fields & HISTORY_SEARCH_FIELDS


def _history_filter_conditions(
    query: str,
    channel_id: str,
    search_fields: set[str] | None = None,
) -> tuple[list[str], list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    normalized_query = query.strip().lower()
    if normalized_query:
        active_search_fields = _active_history_search_fields(search_fields)
        search_conditions: list[str] = []
        if "titles" in active_search_fields:
            search_conditions.append(
                "lower(COALESCE(v.title, '') || ' ' || COALESCE(ch.title, '') || "
                "' ' || v.video_id || ' ' || COALESCE(v.upload_date, '')) LIKE ?"
            )
            params.append(f"%{normalized_query}%")
        if "descriptions" in active_search_fields:
            search_conditions.append("lower(COALESCE(v.description, '')) LIKE ?")
            params.append(f"%{normalized_query}%")
        conditions.append(f"({' OR '.join(search_conditions)})" if search_conditions else "0")
    normalized_channel_id = channel_id.strip()
    if normalized_channel_id:
        conditions.append("v.channel_id = ?")
        params.append(normalized_channel_id)
    return conditions, params


def history_search_data(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 200,
    offset: int = 0,
    channel_id: str = "",
    search_fields: set[str] | None = None,
) -> dict[str, Any]:
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    conditions, params = _history_filter_conditions(query, channel_id, search_fields)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    ordering = _history_event_order_sql()
    availability_category_sql = _video_availability_category_sql(
        video_id="v.video_id",
        availability="v.availability",
        is_playable="v.is_playable",
    )
    filtered = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM history_events he
        JOIN videos v ON v.video_id = he.video_id
        LEFT JOIN channels ch ON ch.channel_id = v.channel_id
        {where}
        """,
        params,
    ).fetchone()["count"]
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            WITH page_events AS MATERIALIZED (
              SELECT he.event_id, he.video_id
              FROM history_events he
              JOIN videos v ON v.video_id = he.video_id
              LEFT JOIN channels ch ON ch.channel_id = v.channel_id
              {where}
              ORDER BY {ordering}
              LIMIT ? OFFSET ?
            ),
            page_video_ids AS MATERIALIZED (
              SELECT DISTINCT video_id FROM page_events
            ),
            counts AS MATERIALIZED (
              SELECT history.video_id,
                     COUNT(*) AS watch_count,
                     GROUP_CONCAT(
                       COALESCE(history.watch_date, substr(history.watched_at, 1, 10)),
                       '|'
                     ) AS watch_dates
              FROM history_events history
              JOIN page_video_ids page_video ON page_video.video_id = history.video_id
              GROUP BY history.video_id
            )
            SELECT he.event_id AS reconciled_id,
                   COALESCE(he.takeout_history_key, 'youtube') AS history_key,
                   COALESCE(he.youtube_ordinal, 0) AS position,
                   'Watched' AS action,
                   he.video_id,
                   v.title,
                   COALESCE(ch.title, '') AS channel,
                   he.watched_at,
                   he.watch_date,
                   he.source_type,
                   he.match_type,
                   he.time_precision AS time_quality,
                   he.youtube_ordinal,
                   COALESCE(he.takeout_history_key, '') AS takeout_history_key,
                   COALESCE(he.takeout_row_key, '') AS takeout_row_hash,
                   he.imported_at,
                   v.title AS metadata_title,
                   v.description AS metadata_description,
                   COALESCE(v.channel_id, '') AS metadata_channel_id,
                   COALESCE(ch.title, '') AS metadata_channel,
                   COALESCE(ch.aliases, '') AS metadata_channel_aliases,
                   v.duration_text AS metadata_duration,
                   v.uploader_category,
                   v.video_type,
                   v.broadcast_status,
                   v.broadcast_started_at,
                   v.broadcast_ended_at,
                   v.broadcast_status_checked_at,
                   v.movie_rating,
                   v.movie_release_date,
                   v.movie_offer,
                   v.max_video_height,
                   v.spatial_format,
                   v.stereo_layout,
                   v.dynamic_range,
                   v.license,
                   v.location_name,
                   v.content_check_required,
                   v.content_check_reason,
                   v.ai_disclosure,
                   v.ai_disclosure_text,
                   v.thumbnail_path AS metadata_thumbnail_path,
                   COALESCE(ch.thumbnail_path, '') AS metadata_channel_thumbnail_path,
                   v.reaction,
                   v.is_playable,
                   v.availability,
                   {availability_category_sql} AS availability_category,
                   COALESCE(vr.archivarix_status, '') AS recovered_status,
                   vr.archive_capture_at,
                   vr.media_available,
                   COALESCE(he.watch_progress_percent, 0) AS watch_progress_percent,
                   COALESCE(he.watch_resume_seconds, 0) AS watch_resume_seconds,
                   counts.watch_count,
                   counts.watch_dates AS watch_dates_text,
                   v.fetch_status AS metadata_fetch_status
            FROM page_events page
            JOIN history_events he ON he.event_id = page.event_id
            JOIN videos v ON v.video_id = he.video_id
            LEFT JOIN channels ch ON ch.channel_id = v.channel_id
            LEFT JOIN video_recovery vr ON vr.video_id = v.video_id
            JOIN counts ON counts.video_id = he.video_id
            ORDER BY {ordering}
            """,
            [*params, limit, offset],
        )
    ]
    _add_video_playlist_links(conn, rows)
    attach_annotations(conn, "video", rows)
    for row in rows:
        _hydrate_video_identity(row)
        row["archive_url"] = wayback_video_url(
            row.get("video_id") or "",
            row.get("archive_capture_at"),
        )
        row["video_file_url"] = (
            archivarix_media_url(row.get("video_id") or "")
            if row.get("media_available")
            else ""
        )
        row["source_label"] = history_source_type_label(row.get("source_type") or "")
        row["time_quality_label"] = history_time_quality_label(row.get("time_quality") or "")
        row["match_label"] = history_match_type_label(row.get("match_type") or "")
        row["time_quality_note"] = history_time_quality_note(row.get("time_quality") or "")
        row["history_badges"] = [
            value
            for value in (row["time_quality_label"],)
            if value
        ]
    totals = dict(
        conn.execute(
            """
            SELECT COUNT(*) AS watch_rows, COUNT(DISTINCT video_id) AS distinct_watch_videos
            FROM history_events
            """
        ).fetchone()
    )
    return {
        "query": query,
        "channel_id": channel_id,
        "search_fields": sorted(_active_history_search_fields(search_fields)),
        "limit": limit,
        "offset": offset,
        "watch": rows,
        "totals": {**totals, "filtered_watch_rows": int(filtered or 0)},
    }


def history_activity_data(
    conn: sqlite3.Connection,
    start_date: str = "",
    end_date: str = "",
    channel_id: str = "",
    query: str = "",
    search_fields: set[str] | None = None,
) -> dict[str, Any]:
    conditions = ["COALESCE(he.watch_date, substr(he.watched_at, 1, 10)) IS NOT NULL"]
    history_conditions, params = _history_filter_conditions(query, channel_id, search_fields)
    conditions.extend(history_conditions)
    where = " AND ".join(conditions)
    daily_rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT COALESCE(he.watch_date, substr(he.watched_at, 1, 10)) AS watch_date,
                   COUNT(*) AS watch_count
            FROM history_events he
            JOIN videos v ON v.video_id = he.video_id
            LEFT JOIN channels ch ON ch.channel_id = v.channel_id
            WHERE {where}
            GROUP BY COALESCE(he.watch_date, substr(he.watched_at, 1, 10))
            ORDER BY watch_date DESC
            """,
            params,
        )
    ]
    offset = 0
    activity: list[dict[str, Any]] = []
    for row in daily_rows:
        watch_date = row["watch_date"]
        watch_count = int(row["watch_count"] or 0)
        if (not start_date or watch_date >= start_date) and (not end_date or watch_date <= end_date):
            activity.append(
                {
                    "watch_date": watch_date,
                    "watch_count": watch_count,
                    "offset": offset,
                }
            )
        offset += watch_count
    return {
        "start_date": start_date,
        "end_date": end_date,
        "channel_id": channel_id.strip(),
        "query": query.strip(),
        "search_fields": sorted(_active_history_search_fields(search_fields)),
        "activity": activity,
    }
