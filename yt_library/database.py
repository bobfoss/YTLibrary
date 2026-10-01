"""SQLite connection, schema bootstrap, and migration support."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .history import history_match_type_for_identity, history_source_type_for_identity
from .schema import load_schema
from .time_utils import utc_now


VIDEO_VISIBILITY_CAPTURE_START = "2026-07-30T17:54:45Z"
PLAYLIST_METADATA_CAPTURE_START = "2026-07-30T20:26:38Z"
CHANNEL_SUBSCRIPTION_CAPTURE_START = "2026-07-30T20:34:50Z"
CHANNEL_NOTIFICATION_CAPTURE_START = "2026-07-30T20:55:56Z"

SCHEMA = load_schema()
SCHEMA_VERSION = 36


_DATABASE_BOOTSTRAP_LOCK = threading.Lock()


def worker_queue_order_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}priority, "
        f"{prefix}updated_at DESC, "
        f"{prefix}queue_id DESC"
    )


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def delete_playlist_and_orphaned_unwatched_videos(
    conn: sqlite3.Connection,
    playlist_id: str,
) -> int:
    video_ids = [
        str(row["video_id"] or "")
        for row in conn.execute(
            "SELECT DISTINCT video_id FROM playlist_items WHERE playlist_id = ? AND video_id IS NOT NULL",
            (playlist_id,),
        )
        if row["video_id"]
    ]
    conn.execute("DELETE FROM playlists WHERE playlist_id = ?", (playlist_id,))
    if not video_ids:
        return 0

    placeholders = ", ".join("?" for _ in video_ids)
    removable_ids = [
        row["video_id"]
        for row in conn.execute(
            f"""
            SELECT v.video_id
            FROM videos v
            WHERE v.video_id IN ({placeholders})
              AND upper(COALESCE(v.reaction, '')) NOT IN ('L', 'D', 'LIKE', 'DISLIKE')
              AND NOT EXISTS (
                SELECT 1 FROM history_events he WHERE he.video_id = v.video_id
              )
              AND NOT EXISTS (
                SELECT 1 FROM playlist_items pi WHERE pi.video_id = v.video_id
              )
              AND NOT EXISTS (
                SELECT 1 FROM clips c WHERE c.source_video_id = v.video_id
              )
            """,
            video_ids,
        )
    ]
    if not removable_ids:
        return 0
    removable_placeholders = ", ".join("?" for _ in removable_ids)
    conn.execute(
        f"""
        DELETE FROM worker_queue
        WHERE video_id IN ({removable_placeholders})
          AND worker_type IN ('metadata', 'placeholder')
        """,
        removable_ids,
    )
    conn.execute(
        f"DELETE FROM videos WHERE video_id IN ({removable_placeholders})",
        removable_ids,
    )
    return len(removable_ids)


def _playlist_owner_identity_for_migration(
    conn: sqlite3.Connection,
) -> tuple[str, str]:
    rows = conn.execute(
        """
        SELECT COALESCE(p.owner_channel_id, '') AS owner_channel_id,
               lower(trim(COALESCE(ch.title, ''))) AS owner_name,
               COUNT(*) AS playlist_count
        FROM playlists p
        LEFT JOIN channels ch ON ch.channel_id = p.owner_channel_id
        WHERE p.in_library = 1
          AND trim(COALESCE(p.owner_channel_id, '') || COALESCE(ch.title, '')) <> ''
        GROUP BY p.owner_channel_id, owner_name
        ORDER BY playlist_count DESC, owner_channel_id, owner_name
        """
    ).fetchall()
    if not rows:
        return "", ""
    top_count = int(rows[0]["playlist_count"] or 0)
    next_count = int(rows[1]["playlist_count"] or 0) if len(rows) > 1 else 0
    if top_count < 2 or top_count < max(2, next_count * 2):
        return "", ""
    return str(rows[0]["owner_channel_id"] or ""), str(rows[0]["owner_name"] or "")


def _migrate_playlist_ownership_v20(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(playlists)")}
    if "in_library" not in columns:
        conn.execute(
            "ALTER TABLE playlists RENAME COLUMN is_library_playlist TO in_library"
        )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(playlists)")}
    if "ownership" not in columns:
        conn.execute(
            """
            ALTER TABLE playlists
            ADD COLUMN ownership TEXT NOT NULL DEFAULT 'unknown'
            CHECK (ownership IN ('mine', 'others', 'unknown'))
            """
        )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(playlists)")}
    if "library_missing_at" not in columns:
        conn.execute("ALTER TABLE playlists ADD COLUMN library_missing_at TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS playlist_tombstones (
          playlist_id TEXT PRIMARY KEY,
          observed_removed_at TEXT NOT NULL,
          reason TEXT NOT NULL DEFAULT 'authenticated_missing'
            CHECK (reason IN ('authenticated_missing', 'missing_from_library', 'explicit_user')),
          last_confirmed_at TEXT NOT NULL
        );
        """
    )

    library_channel_id, library_owner_name = _playlist_owner_identity_for_migration(conn)
    rows = conn.execute(
        """
        SELECT p.playlist_id,
               COALESCE(p.owner_channel_id, '') AS owner_channel_id,
               lower(trim(COALESCE(ch.title, ''))) AS owner_name,
               EXISTS (
                 SELECT 1
                 FROM playlist_items pi
                 WHERE pi.playlist_id = p.playlist_id
                   AND pi.source_quality = 'takeout'
               ) AS has_takeout_items
        FROM playlists p
        LEFT JOIN channels ch ON ch.channel_id = p.owner_channel_id
        """
    ).fetchall()
    for row in rows:
        owner_channel_id = str(row["owner_channel_id"] or "")
        owner_name = str(row["owner_name"] or "")
        if bool(row["has_takeout_items"]) or (
            library_channel_id and owner_channel_id == library_channel_id
        ) or (
            library_owner_name and owner_name == library_owner_name
        ):
            ownership = "mine"
        elif owner_channel_id or owner_name:
            ownership = "others"
        else:
            ownership = "unknown"
        conn.execute(
            "UPDATE playlists SET ownership = ? WHERE playlist_id = ?",
            (ownership, row["playlist_id"]),
        )

    removed_rows = conn.execute(
        """
        SELECT p.playlist_id, p.ownership,
               COALESCE(ps.scanned_at, p.updated_at, ?) AS observed_removed_at
        FROM playlists p
        LEFT JOIN playlist_scans ps ON ps.playlist_id = p.playlist_id
        WHERE p.fetch_status = 'removed' OR ps.scan_status = 'removed'
        """,
        (utc_now(),),
    ).fetchall()
    for row in removed_rows:
        if row["ownership"] == "mine":
            conn.execute(
                """
                INSERT INTO playlist_tombstones(
                  playlist_id, observed_removed_at, reason, last_confirmed_at
                ) VALUES (?, ?, 'authenticated_missing', ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                  last_confirmed_at=excluded.last_confirmed_at
                """,
                (
                    row["playlist_id"],
                    row["observed_removed_at"],
                    row["observed_removed_at"],
                ),
            )
            delete_playlist_and_orphaned_unwatched_videos(conn, row["playlist_id"])
        else:
            conn.execute(
                """
                UPDATE playlists
                SET fetch_status='unavailable'
                WHERE playlist_id=?
                """,
                (row["playlist_id"],),
            )
            conn.execute(
                """
                UPDATE playlist_scans
                SET scan_status='unavailable'
                WHERE playlist_id=?
                """,
                (row["playlist_id"],),
            )


def migrate_database(db_path: Path) -> None:
    """Initialize or upgrade the database schema."""
    conn = connect(db_path)
    try:
        with _DATABASE_BOOTSTRAP_LOCK:
            with conn:
                _migrate_database(conn)
    except Exception:
        conn.close()
        raise
    else:
        conn.close()


def _bootstrap_database(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, utc_now()),
    )


def _detach_duplicate_my_activity_history(
    conn: sqlite3.Connection,
    history_row: sqlite3.Row,
    updated_at: str,
) -> None:
    has_takeout = bool(history_row["takeout_history_key"])
    has_youtube = history_row["youtube_ordinal"] is not None
    if not has_takeout and not has_youtube:
        conn.execute(
            "DELETE FROM history_events WHERE event_id = ?",
            (history_row["event_id"],),
        )
        return

    source_type = history_source_type_for_identity(
        None,
        history_row["takeout_history_key"],
        history_row["youtube_ordinal"],
    )
    match_type = history_match_type_for_identity(
        None,
        history_row["takeout_history_key"],
        history_row["youtube_ordinal"],
    )
    conn.execute(
        """
        UPDATE history_events
        SET my_activity_event_id=NULL,
            watched_at=CASE WHEN takeout_history_key IS NULL THEN NULL ELSE watched_at END,
            time_precision=CASE
              WHEN takeout_history_key IS NOT NULL THEN 'exact'
              WHEN watch_date IS NOT NULL THEN 'date_only'
              ELSE 'unknown'
            END,
            source_type=?, match_type=?, updated_at=?
        WHERE event_id=?
        """,
        (source_type, match_type, updated_at, history_row["event_id"]),
    )


def _deduplicate_my_activity_occurrences(conn: sqlite3.Connection) -> dict[str, int]:
    """Collapse repeated Google representations of the same exact occurrence."""

    now = utc_now()
    watch_removed = 0
    history_removed = 0
    history_detached = 0
    watch_groups = conn.execute(
        """
        SELECT video_id, watched_at
        FROM my_activity_watch_events
        GROUP BY video_id, watched_at
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for group in watch_groups:
        source_rows = conn.execute(
            """
            SELECT source.*,
                   history.event_id AS history_event_id,
                   history.youtube_ordinal,
                   history.takeout_history_key,
                   history.takeout_row_key,
                   history.watch_progress_percent,
                   history.watch_resume_seconds
            FROM my_activity_watch_events source
            LEFT JOIN history_events history
              ON history.my_activity_event_id = source.event_id
            WHERE source.video_id=? AND source.watched_at=?
            ORDER BY
              CASE
                WHEN history.takeout_history_key IS NOT NULL
                 AND history.youtube_ordinal IS NOT NULL THEN 3
                WHEN history.takeout_history_key IS NOT NULL THEN 2
                WHEN history.youtube_ordinal IS NOT NULL THEN 1
                ELSE 0
              END DESC,
              COALESCE(history.watch_progress_percent, 0) DESC,
              COALESCE(history.watch_resume_seconds, 0) DESC,
              source.collected_at ASC,
              source.event_id ASC
            """,
            (group["video_id"], group["watched_at"]),
        ).fetchall()
        canonical = source_rows[0]
        observed_title = next(
            (row["observed_title"] for row in source_rows if row["observed_title"]),
            "",
        )
        observed_url = next(
            (row["observed_url"] for row in source_rows if row["observed_url"]),
            "",
        )
        collected_at = min(row["collected_at"] for row in source_rows)
        updated_at = max(row["updated_at"] for row in source_rows)
        conn.execute(
            """
            UPDATE my_activity_watch_events
            SET observed_title=?, observed_url=?, collected_at=?, updated_at=?
            WHERE event_id=?
            """,
            (
                observed_title,
                observed_url,
                collected_at,
                updated_at,
                canonical["event_id"],
            ),
        )
        for duplicate in source_rows[1:]:
            history_row = conn.execute(
                "SELECT * FROM history_events WHERE my_activity_event_id = ?",
                (duplicate["event_id"],),
            ).fetchone()
            if history_row:
                had_other_evidence = bool(history_row["takeout_history_key"]) or (
                    history_row["youtube_ordinal"] is not None
                )
                _detach_duplicate_my_activity_history(conn, history_row, now)
                if had_other_evidence:
                    history_detached += 1
                else:
                    history_removed += 1
            conn.execute(
                "DELETE FROM my_activity_watch_events WHERE event_id = ?",
                (duplicate["event_id"],),
            )
            watch_removed += 1

    legacy_history_rows = conn.execute(
        """
        SELECT history.*
        FROM history_events history
        WHERE history.event_id LIKE 'my_activity:%'
          AND history.my_activity_event_id IS NULL
          AND COALESCE(history.watched_at, '') <> ''
          AND EXISTS (
            SELECT 1
            FROM my_activity_watch_events source
            WHERE source.video_id = history.video_id
              AND source.watched_at = history.watched_at
          )
        """
    ).fetchall()
    for history_row in legacy_history_rows:
        had_other_evidence = bool(history_row["takeout_history_key"]) or (
            history_row["youtube_ordinal"] is not None
        )
        _detach_duplicate_my_activity_history(conn, history_row, now)
        if had_other_evidence:
            history_detached += 1
        else:
            history_removed += 1

    subscription_removed = 0
    subscription_groups = conn.execute(
        """
        SELECT channel_id, subscribed_at
        FROM my_activity_subscription_events
        GROUP BY channel_id, subscribed_at
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for group in subscription_groups:
        source_rows = conn.execute(
            """
            SELECT *
            FROM my_activity_subscription_events
            WHERE channel_id=? AND subscribed_at=?
            ORDER BY collected_at ASC, event_id ASC
            """,
            (group["channel_id"], group["subscribed_at"]),
        ).fetchall()
        canonical = source_rows[0]
        observed_title = next(
            (row["observed_title"] for row in source_rows if row["observed_title"]),
            "",
        )
        observed_url = next(
            (row["observed_url"] for row in source_rows if row["observed_url"]),
            "",
        )
        conn.execute(
            """
            UPDATE my_activity_subscription_events
            SET observed_title=?, observed_url=?, collected_at=?, updated_at=?
            WHERE event_id=?
            """,
            (
                observed_title,
                observed_url,
                min(row["collected_at"] for row in source_rows),
                max(row["updated_at"] for row in source_rows),
                canonical["event_id"],
            ),
        )
        for duplicate in source_rows[1:]:
            conn.execute(
                "DELETE FROM my_activity_subscription_events WHERE event_id = ?",
                (duplicate["event_id"],),
            )
            subscription_removed += 1

    return {
        "watch_removed": watch_removed,
        "subscription_removed": subscription_removed,
        "history_removed": history_removed,
        "history_detached": history_detached,
    }


def _migrate_database(conn: sqlite3.Connection) -> None:
    current_version = _schema_version(conn)
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current_version} is newer than this application supports ({SCHEMA_VERSION})"
        )
    if current_version == SCHEMA_VERSION:
        return
    if current_version == 13:
        _deduplicate_my_activity_occurrences(conn)
    if 0 < current_version < 13:
        existing_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "channels" in existing_tables:
            channel_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(channels)")
            }
            if "subscribed_at" not in channel_columns:
                conn.execute("ALTER TABLE channels ADD COLUMN subscribed_at TEXT")
            if "subscribed_at_source" not in channel_columns:
                conn.execute(
                    "ALTER TABLE channels ADD COLUMN subscribed_at_source TEXT NOT NULL DEFAULT ''"
                )
        if "playlists" in existing_tables:
            playlist_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(playlists)")
            }
            if "created_at" not in playlist_columns:
                conn.execute("ALTER TABLE playlists ADD COLUMN created_at TEXT")
        if "history_events" in existing_tables:
            history_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(history_events)")
            }
            if "my_activity_event_id" not in history_columns:
                conn.execute(
                    "ALTER TABLE history_events ADD COLUMN my_activity_event_id TEXT"
                )
    if 0 < current_version < 22:
        clip_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'clips'"
        ).fetchone()
        clip_columns = (
            {row["name"] for row in conn.execute("PRAGMA table_info(clips)")}
            if clip_table
            else set()
        )
        if clip_table and "youtube_feed_ordinal" not in clip_columns:
            conn.execute("ALTER TABLE clips ADD COLUMN youtube_feed_ordinal INTEGER")
    _bootstrap_database(conn)
    if current_version < 2:
        conn.execute("DROP TABLE IF EXISTS app_settings")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (2, utc_now()),
        )
    if current_version < 5:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(placeholder_recovery_worker_runs)")
        }
        if "request_started_at" not in columns:
            conn.execute(
                "ALTER TABLE placeholder_recovery_worker_runs ADD COLUMN request_started_at TEXT"
            )
        conn.execute(
            """
            UPDATE placeholder_recovery_worker_runs
            SET request_started_at = started_at
            WHERE request_started_at IS NULL
              AND recovery_status NOT IN ('', 'authentication_error')
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (5, utc_now()),
        )
    if current_version < 6:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(placeholder_recovery_worker_runs)")
        }
        if "request_count" not in columns:
            conn.execute(
                """
                ALTER TABLE placeholder_recovery_worker_runs
                ADD COLUMN request_count INTEGER NOT NULL DEFAULT 0
                """
            )
        conn.execute(
            """
            UPDATE placeholder_recovery_worker_runs
            SET request_count = 1
            WHERE request_started_at IS NOT NULL
              AND request_count = 0
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (6, utc_now()),
        )
    if current_version < 7:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(playlists)")
        }
        if "is_library_playlist" not in columns and "in_library" not in columns:
            conn.execute(
                """
                ALTER TABLE playlists
                ADD COLUMN is_library_playlist INTEGER NOT NULL DEFAULT 0
                CHECK (is_library_playlist IN (0, 1))
                """
            )
        if "in_library" not in columns:
            conn.execute(
                """
                UPDATE playlists
                SET is_library_playlist = 1
                WHERE EXISTS (
                  SELECT 1
                  FROM playlist_items pi
                  WHERE pi.playlist_id = playlists.playlist_id
                    AND pi.source_quality = 'takeout'
                )
                """
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (7, utc_now()),
        )
    if current_version < 8:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(channels)")
        }
        if "first_seen_at" not in columns:
            conn.execute("ALTER TABLE channels ADD COLUMN first_seen_at TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (8, utc_now()),
        )
    if current_version < 9:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(channels)")
        }
        if "notification_level" not in columns:
            conn.execute(
                """
                ALTER TABLE channels
                ADD COLUMN notification_level TEXT NOT NULL DEFAULT ''
                CHECK (notification_level IN ('', 'all', 'personalized', 'none'))
                """
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (9, utc_now()),
        )
    if current_version < 10:
        conn.execute(
            """
            UPDATE videos
            SET title = ''
            WHERE TRIM(title) = video_id
            """
        )
        conn.execute(
            """
            UPDATE worker_queue
            SET current_title = ''
            WHERE video_id <> ''
              AND TRIM(current_title) = video_id
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (10, utc_now()),
        )
    if current_version < 11:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(videos)")
        }
        if "watch_progress_percent" in columns:
            conn.execute("ALTER TABLE videos DROP COLUMN watch_progress_percent")
        if "watch_resume_seconds" in columns:
            conn.execute("ALTER TABLE videos DROP COLUMN watch_resume_seconds")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (11, utc_now()),
        )
    if current_version < 12:
        channel_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(channels)")
        }
        if "subscription_checked_at" not in channel_columns:
            conn.execute("ALTER TABLE channels ADD COLUMN subscription_checked_at TEXT")
        if "notification_checked_at" not in channel_columns:
            conn.execute("ALTER TABLE channels ADD COLUMN notification_checked_at TEXT")

        playlist_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(playlists)")
        }
        if "metadata_checked_at" not in playlist_columns:
            conn.execute("ALTER TABLE playlists ADD COLUMN metadata_checked_at TEXT")

        video_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(videos)")
        }
        if "visibility_checked_at" not in video_columns:
            conn.execute("ALTER TABLE videos ADD COLUMN visibility_checked_at TEXT")

        conn.execute(
            """
            UPDATE channels
            SET notification_level = ''
            WHERE subscribed = 0
            """
        )
        conn.execute(
            """
            UPDATE channels
            SET subscription_checked_at = fetched_at
            WHERE fetch_status = 'ok'
              AND fetched_at >= ?
            """,
            (CHANNEL_SUBSCRIPTION_CAPTURE_START,),
        )
        conn.execute(
            """
            UPDATE channels
            SET notification_checked_at = fetched_at
            WHERE fetch_status = 'ok'
              AND fetched_at >= ?
              AND (subscribed = 0 OR notification_level <> '')
            """,
            (CHANNEL_NOTIFICATION_CAPTURE_START,),
        )
        conn.execute(
            """
            UPDATE playlists
            SET metadata_checked_at = (
              SELECT ps.scanned_at
              FROM playlist_scans ps
              WHERE ps.playlist_id = playlists.playlist_id
                AND ps.scan_status = 'ok'
            )
            WHERE COALESCE(owner_channel_id, '') <> ''
              AND COALESCE(visibility, '') <> ''
              AND EXISTS (
                SELECT 1
                FROM playlist_scans ps
                WHERE ps.playlist_id = playlists.playlist_id
                  AND ps.scan_status = 'ok'
                  AND ps.scanned_at >= ?
              )
            """,
            (PLAYLIST_METADATA_CAPTURE_START,),
        )
        conn.execute(
            """
            UPDATE videos
            SET visibility_checked_at = fetched_at
            WHERE fetch_status = 'ok'
              AND fetched_at >= ?
              AND availability <> 'unknown'
              AND COALESCE(channel_id, '') <> ''
            """,
            (VIDEO_VISIBILITY_CAPTURE_START,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (12, utc_now()),
        )
    if current_version < 13:
        channel_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(channels)")
        }
        if "subscribed_at" not in channel_columns:
            conn.execute("ALTER TABLE channels ADD COLUMN subscribed_at TEXT")
        if "subscribed_at_source" not in channel_columns:
            conn.execute(
                "ALTER TABLE channels ADD COLUMN subscribed_at_source TEXT NOT NULL DEFAULT ''"
            )

        playlist_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(playlists)")
        }
        if "created_at" not in playlist_columns:
            conn.execute("ALTER TABLE playlists ADD COLUMN created_at TEXT")

        history_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(history_events)")
        }
        if "my_activity_event_id" not in history_columns:
            conn.execute(
                """
                ALTER TABLE history_events
                ADD COLUMN my_activity_event_id TEXT
                REFERENCES my_activity_watch_events(event_id)
                """
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO my_activity_watch_events(
              event_id, video_id, watched_at, observed_title, observed_url,
              collected_at, updated_at
            )
            SELECT h.event_id, h.video_id, h.watched_at, COALESCE(v.title, ''),
                   'https://www.youtube.com/watch?v=' || h.video_id,
                   COALESCE(h.imported_at, h.updated_at, ?),
                   COALESCE(h.updated_at, h.imported_at, ?)
            FROM history_events h
            LEFT JOIN videos v ON v.video_id = h.video_id
            WHERE h.event_id LIKE 'my_activity:%'
              AND COALESCE(h.watched_at, '') <> ''
            """,
            (utc_now(), utc_now()),
        )
        conn.execute(
            """
            UPDATE history_events
            SET my_activity_event_id = event_id
            WHERE event_id LIKE 'my_activity:%'
              AND my_activity_event_id IS NULL
              AND EXISTS (
                SELECT 1
                FROM my_activity_watch_events source
                WHERE source.event_id = history_events.event_id
              )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_history_my_activity_event
            ON history_events(my_activity_event_id)
            WHERE my_activity_event_id IS NOT NULL
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (13, utc_now()),
        )
    if current_version < 14:
        _deduplicate_my_activity_occurrences(conn)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_my_activity_watch_occurrence
            ON my_activity_watch_events(video_id, watched_at)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_my_activity_subscription_occurrence
            ON my_activity_subscription_events(channel_id, subscribed_at)
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (14, utc_now()),
        )
    if current_version < 15:
        video_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(videos)")
        }
        if "uploader_category" not in video_columns:
            conn.execute(
                "ALTER TABLE videos ADD COLUMN uploader_category TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (15, utc_now()),
        )
    if current_version < 16:
        queue_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(worker_queue)")
        }
        if "payload_json" not in queue_columns:
            conn.execute(
                "ALTER TABLE worker_queue ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "plugin_subject_id" not in queue_columns:
            conn.execute(
                "ALTER TABLE worker_queue ADD COLUMN plugin_subject_id TEXT NOT NULL DEFAULT ''"
            )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS plugin_worker_runs (
              run_id TEXT PRIMARY KEY,
              plugin_id TEXT NOT NULL,
              worker_id TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT '',
              started_at TEXT NOT NULL,
              finished_at TEXT,
              queue_id INTEGER NOT NULL DEFAULT 0,
              subject_id TEXT NOT NULL DEFAULT '',
              outcome TEXT NOT NULL DEFAULT '',
              processed INTEGER NOT NULL DEFAULT 0,
              found INTEGER NOT NULL DEFAULT 0,
              failed INTEGER NOT NULL DEFAULT 0,
              skipped INTEGER NOT NULL DEFAULT 0,
              message TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS plugin_worker_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL DEFAULT '',
              plugin_id TEXT NOT NULL,
              worker_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              level TEXT NOT NULL DEFAULT '',
              subject_id TEXT NOT NULL DEFAULT '',
              message TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_plugin_worker_runs_process
              ON plugin_worker_runs(plugin_id, worker_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_plugin_worker_runs_subject
              ON plugin_worker_runs(plugin_id, worker_id, subject_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_plugin_worker_log_process
              ON plugin_worker_log(plugin_id, worker_id, id DESC);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (16, utc_now()),
        )
    if current_version < 17:
        conn.execute("DELETE FROM groups WHERE group_key = 'youtube-ungrouped'")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (17, utc_now()),
        )
    if current_version < 18:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS playlist_collaborators (
              playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
              channel_id TEXT NOT NULL REFERENCES channels(channel_id),
              position INTEGER NOT NULL,
              PRIMARY KEY (playlist_id, channel_id)
            );
            CREATE INDEX IF NOT EXISTS idx_playlist_collaborators_order
              ON playlist_collaborators(playlist_id, position);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (18, utc_now()),
        )
    if current_version < 19:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS clips (
              clip_id TEXT PRIMARY KEY,
              title TEXT NOT NULL DEFAULT '',
              owner_channel_id TEXT REFERENCES channels(channel_id),
              owner_title TEXT NOT NULL DEFAULT '',
              owner_thumbnail_url TEXT NOT NULL DEFAULT '',
              owner_thumbnail_path TEXT NOT NULL DEFAULT '',
              ownership TEXT NOT NULL DEFAULT 'unknown'
                CHECK (ownership IN ('mine', 'others', 'unknown')),
              source_video_id TEXT REFERENCES videos(video_id),
              start_ms INTEGER,
              end_ms INTEGER,
              view_count INTEGER,
              view_count_text TEXT NOT NULL DEFAULT '',
              clipped_at TEXT,
              clipped_at_text TEXT NOT NULL DEFAULT '',
              clipped_at_observed_at TEXT,
              thumbnail_url TEXT NOT NULL DEFAULT '',
              availability TEXT NOT NULL DEFAULT 'unknown'
                CHECK (availability IN ('active', 'unavailable', 'unknown')),
              fetch_status TEXT NOT NULL DEFAULT '',
              fetch_error TEXT NOT NULL DEFAULT '',
              fetched_at TEXT,
              last_seen_at TEXT,
              updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_clips_owner
              ON clips(ownership, owner_channel_id);
            CREATE INDEX IF NOT EXISTS idx_clips_source_video
              ON clips(source_video_id);
            CREATE INDEX IF NOT EXISTS idx_clips_fetch
              ON clips(fetch_status, fetched_at);
            """
        )
        worker_queue_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(worker_queue)")
        }
        if "clip_id" not in worker_queue_columns:
            conn.execute(
                "ALTER TABLE worker_queue ADD COLUMN clip_id TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (19, utc_now()),
        )
    if current_version < 20:
        _migrate_playlist_ownership_v20(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (20, utc_now()),
        )
    if current_version < 21:
        videos_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'videos'"
        ).fetchone()
        videos_sql = str(videos_sql_row["sql"] or "") if videos_sql_row else ""
        if "INDIFFERENT" not in videos_sql.upper():
            conn.executescript(
                """
                ALTER TABLE videos RENAME COLUMN reaction TO legacy_reaction;
                ALTER TABLE videos ADD COLUMN reaction TEXT NOT NULL DEFAULT ''
                  CHECK (reaction IN ('', 'LIKE', 'DISLIKE', 'INDIFFERENT'));
                UPDATE videos
                SET reaction = CASE upper(trim(COALESCE(legacy_reaction, '')))
                  WHEN 'L' THEN 'LIKE'
                  WHEN 'LIKE' THEN 'LIKE'
                  WHEN 'D' THEN 'DISLIKE'
                  WHEN 'DISLIKE' THEN 'DISLIKE'
                  WHEN 'INDIFFERENT' THEN 'INDIFFERENT'
                  ELSE ''
                END;
                ALTER TABLE videos DROP COLUMN legacy_reaction;
                """
            )
        playlist_run_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(playlist_scan_worker_runs)")
        }
        if "stale_days" in playlist_run_columns:
            conn.execute(
                "ALTER TABLE playlist_scan_worker_runs DROP COLUMN stale_days"
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (21, utc_now()),
        )
    if current_version < 22:
        clip_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(clips)")
        }
        if "youtube_feed_ordinal" not in clip_columns:
            conn.execute("ALTER TABLE clips ADD COLUMN youtube_feed_ordinal INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_clips_feed_ordinal "
            "ON clips(youtube_feed_ordinal)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (22, utc_now()),
        )
    if current_version < 23:
        video_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(videos)")
        }
        if "video_type" not in video_columns:
            conn.execute(
                "ALTER TABLE videos ADD COLUMN video_type TEXT NOT NULL DEFAULT '' "
                "CHECK (video_type IN ('', 'video', 'short', 'live'))"
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (23, utc_now()),
        )
    if current_version < 24:
        video_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(videos)")
        }
        videos_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'videos'"
        ).fetchone()
        videos_sql = str(videos_sql_row["sql"] or "") if videos_sql_row else ""
        if "video_type" in video_columns and "'movie'" not in videos_sql.casefold():
            conn.executescript(
                """
                ALTER TABLE videos RENAME COLUMN video_type TO legacy_video_type;
                ALTER TABLE videos ADD COLUMN video_type TEXT NOT NULL DEFAULT ''
                  CHECK (video_type IN ('', 'video', 'short', 'live', 'movie'));
                UPDATE videos SET video_type = legacy_video_type;
                ALTER TABLE videos DROP COLUMN legacy_video_type;
                """
            )
        video_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(videos)")
        }
        for column in ("movie_rating", "movie_release_date", "movie_offer"):
            if column not in video_columns:
                conn.execute(
                    f"ALTER TABLE videos ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (24, utc_now()),
        )
    if current_version < 25:
        video_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(videos)")
        }
        feature_columns = {
            "max_video_height": (
                "INTEGER CHECK (max_video_height IS NULL OR max_video_height > 0)"
            ),
            "spatial_format": (
                "TEXT CHECK (spatial_format IS NULL OR "
                "spatial_format IN ('', '360', 'vr180'))"
            ),
            "stereo_layout": (
                "TEXT CHECK (stereo_layout IS NULL OR "
                "stereo_layout IN ('', 'left_right', 'top_bottom'))"
            ),
            "dynamic_range": (
                "TEXT CHECK (dynamic_range IS NULL OR dynamic_range IN ('sdr', 'hdr'))"
            ),
            "license": "TEXT",
            "location_name": "TEXT",
        }
        for column, definition in feature_columns.items():
            if column not in video_columns:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {column} {definition}")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (25, utc_now()),
        )
    if current_version < 26:
        video_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(videos)")
        }
        videos_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'videos'"
        ).fetchone()
        videos_sql = str(videos_sql_row["sql"] or "") if videos_sql_row else ""
        if "video_type" in video_columns and "'livestream'" not in videos_sql.casefold():
            conn.executescript(
                """
                ALTER TABLE videos RENAME COLUMN video_type TO legacy_video_type;
                ALTER TABLE videos ADD COLUMN video_type TEXT NOT NULL DEFAULT ''
                  CHECK (video_type IN ('', 'video', 'short', 'livestream', 'movie'));
                UPDATE videos
                SET video_type = CASE legacy_video_type
                  WHEN 'live' THEN 'livestream'
                  ELSE legacy_video_type
                END;
                ALTER TABLE videos DROP COLUMN legacy_video_type;
                """
            )
        video_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(videos)")
        }
        broadcast_columns = {
            "broadcast_status": (
                "TEXT CHECK (broadcast_status IS NULL OR "
                "broadcast_status IN ('', 'upcoming', 'live', 'ended'))"
            ),
            "broadcast_started_at": "TEXT",
            "broadcast_ended_at": "TEXT",
            "broadcast_status_checked_at": "TEXT",
        }
        for column, definition in broadcast_columns.items():
            if column not in video_columns:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {column} {definition}")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (26, utc_now()),
        )
    if current_version < 27:
        video_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(videos)")
        }
        if "content_check_required" not in video_columns:
            conn.execute(
                "ALTER TABLE videos ADD COLUMN content_check_required INTEGER "
                "CHECK (content_check_required IN (0, 1))"
            )
        if "content_check_reason" not in video_columns:
            conn.execute("ALTER TABLE videos ADD COLUMN content_check_reason TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (27, utc_now()),
        )
    if current_version < 28:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS channel_featured_channels (
              owner_channel_id TEXT NOT NULL REFERENCES channels(channel_id) ON DELETE CASCADE,
              featured_channel_id TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '',
              channel_reference TEXT NOT NULL DEFAULT '',
              position INTEGER NOT NULL,
              PRIMARY KEY (owner_channel_id, featured_channel_id)
            );
            CREATE INDEX IF NOT EXISTS idx_channel_featured_channels_order
              ON channel_featured_channels(owner_channel_id, position);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (28, utc_now()),
        )
    if current_version < 29:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cookie_auth_status (
              service TEXT PRIMARY KEY
                CHECK(service IN ('youtube', 'google', 'archivarix')),
              status TEXT NOT NULL
                CHECK(status IN ('valid', 'expired', 'rejected', 'missing', 'error')),
              checked_at TEXT NOT NULL,
              message TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (29, utc_now()),
        )
    if current_version < 30:
        for table_name in ("videos", "clips", "playlists", "channels"):
            columns = {
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table_name})")
            }
            if "note" not in columns:
                conn.execute(
                    f"ALTER TABLE {table_name} "
                    "ADD COLUMN note TEXT NOT NULL DEFAULT ''"
                )
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM entity_note_fts")
        for entity_kind, table_name, id_column in (
            ("video", "videos", "video_id"),
            ("clip", "clips", "clip_id"),
            ("playlist", "playlists", "playlist_id"),
            ("channel", "channels", "channel_id"),
        ):
            conn.execute(
                f"""
                INSERT INTO entity_note_fts(entity_kind, entity_id, note)
                SELECT ?, {id_column}, note
                FROM {table_name}
                WHERE trim(note) <> ''
                """,
                (entity_kind,),
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (30, utc_now()),
        )
    if current_version < 31:
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_metadata_worker_log_created
              ON metadata_worker_log(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_metadata_worker_log_level
              ON metadata_worker_log(level);
            CREATE INDEX IF NOT EXISTS idx_playlist_scan_worker_log_created
              ON playlist_scan_worker_log(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_playlist_scan_worker_log_level
              ON playlist_scan_worker_log(level);
            CREATE INDEX IF NOT EXISTS idx_live_history_worker_log_created
              ON live_history_worker_log(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_live_history_worker_log_level
              ON live_history_worker_log(level);
            CREATE INDEX IF NOT EXISTS idx_placeholder_recovery_worker_log_created
              ON placeholder_recovery_worker_log(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_placeholder_recovery_worker_log_level
              ON placeholder_recovery_worker_log(level);
            CREATE INDEX IF NOT EXISTS idx_plugin_worker_log_created
              ON plugin_worker_log(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_plugin_worker_log_level
              ON plugin_worker_log(level);
            CREATE INDEX IF NOT EXISTS idx_plugin_worker_log_plugin_level
              ON plugin_worker_log(plugin_id, level);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (31, utc_now()),
        )
    if current_version < 32:
        conn.execute(
            """
            UPDATE videos
            SET is_playable = NULL,
                last_seen_available_at = NULL
            WHERE visibility_checked_at IS NULL
              AND is_playable = 1
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (32, utc_now()),
        )
    if current_version < 33:
        video_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(videos)")
        }
        if "ai_disclosure" not in video_columns:
            conn.execute(
                "ALTER TABLE videos ADD COLUMN ai_disclosure INTEGER "
                "CHECK (ai_disclosure IN (0, 1))"
            )
        if "ai_disclosure_text" not in video_columns:
            conn.execute("ALTER TABLE videos ADD COLUMN ai_disclosure_text TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (33, utc_now()),
        )
    if current_version < 34:
        playlist_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(playlists)")
        }
        if "last_changed_at" not in playlist_columns:
            conn.execute("ALTER TABLE playlists ADD COLUMN last_changed_at TEXT")
        conn.execute(
            """
            UPDATE playlists AS p
            SET last_changed_at = NULLIF(
              MAX(
                COALESCE(p.last_changed_at, ''),
                COALESCE(p.created_at, ''),
                COALESCE((
                  SELECT MAX(pi.added_at)
                  FROM playlist_items pi
                  WHERE pi.playlist_id = p.playlist_id
                    AND pi.added_at IS NOT NULL
                    AND pi.added_at <> ''
                ), ''),
                COALESCE((
                  SELECT MAX(log.created_at)
                  FROM playlist_scan_worker_log log
                  WHERE log.playlist_id = p.playlist_id
                    AND log.message LIKE '%count changed %'
                ), '')
              ),
              ''
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (34, utc_now()),
        )
    if current_version < 35:
        playlist_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(playlists)")
        }
        if "youtube_updated_date" not in playlist_columns:
            conn.execute(
                "ALTER TABLE playlists ADD COLUMN youtube_updated_date TEXT"
            )
        conn.execute(
            """
            UPDATE playlists
            SET description = ''
            WHERE lower(trim(description)) IN ('updated today', 'updated yesterday')
               OR lower(trim(description)) GLOB 'updated [0-9] day ago'
               OR lower(trim(description)) GLOB 'updated [0-9] days ago'
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (35, utc_now()),
        )
    if current_version < 36:
        video_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(videos)")
        }
        if "auto_dubbed" not in video_columns:
            conn.execute(
                "ALTER TABLE videos ADD COLUMN auto_dubbed INTEGER "
                "CHECK (auto_dubbed IN (0, 1))"
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (36, utc_now()),
        )


def _schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name = 'schema_migrations'
        """
    ).fetchone()
    if not row:
        return 0
    value = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    return int(value or 0)
