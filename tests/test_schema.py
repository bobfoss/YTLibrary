from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yt_library import core

from tests.support import migrated_connection


class SchemaTests(unittest.TestCase):
    def test_migrate_bootstraps_exact_schema_sql_shape(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                db_path = Path(temp_dir) / "library.sqlite3"
                conn = core.connect(db_path)
                try:
                    before_tables = {
                        row["name"]
                        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                    }
                finally:
                    conn.close()
                self.assertEqual(before_tables, set())
                core.migrate_database(db_path)
                expected = sqlite3.connect(":memory:")
                expected.row_factory = sqlite3.Row
                expected.executescript(core.SCHEMA)
                actual = core.connect(db_path)
                try:
                    expected_tables = {
                        row["name"]
                        for row in expected.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                        )
                    }
                    actual_tables = {
                        row["name"]
                        for row in actual.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                        )
                    }
                    expected_indexes = {
                        row["name"]
                        for row in expected.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
                        )
                    }
                    actual_indexes = {
                        row["name"]
                        for row in actual.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
                        )
                    }
                    expected_columns = {
                        table: [
                            row["name"]
                            for row in expected.execute(f"PRAGMA table_info({table})")
                        ]
                        for table in expected_tables
                    }
                    actual_columns = {
                        table: [
                            row["name"]
                            for row in actual.execute(f"PRAGMA table_info({table})")
                        ]
                        for table in actual_tables
                    }
                finally:
                    expected.close()
                    actual.close()
            finally:
                core.ROOT = original_root

        self.assertEqual(actual_tables, expected_tables)
        self.assertEqual(actual_columns, expected_columns)
        self.assertEqual(actual_indexes, expected_indexes)
        self.assertIn("idx_channels_title", actual_indexes)
        self.assertIn("idx_history_events_video", actual_indexes)

    def test_migrate_is_schema_only_for_existing_legacy_tables(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                db_path = Path(temp_dir) / "library.sqlite3"
                raw = sqlite3.connect(db_path)
                try:
                    raw.execute(
                        """
                        CREATE TABLE legacy_marker (
                          value TEXT NOT NULL
                        )
                        """
                    )
                    raw.execute("INSERT INTO legacy_marker(value) VALUES ('kept')")
                    raw.commit()
                finally:
                    raw.close()

                core.migrate_database(db_path)
                conn = core.connect(db_path)
                try:
                    tables = {
                        row["name"]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    marker = conn.execute("SELECT value FROM legacy_marker").fetchone()["value"]
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root

        self.assertIn("playlists", tables)
        self.assertIn("legacy_marker", tables)
        self.assertEqual(marker, "kept")

    def test_migrate_removes_legacy_app_settings_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            raw = sqlite3.connect(db_path)
            try:
                raw.execute(
                    """
                    CREATE TABLE app_settings (
                      setting_key TEXT PRIMARY KEY,
                      value TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    )
                    """
                )
                raw.execute(
                    """
                    CREATE TABLE schema_migrations (
                      version INTEGER PRIMARY KEY,
                      applied_at TEXT NOT NULL
                    )
                    """
                )
                raw.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-07-01T00:00:00Z')")
                raw.execute(
                    """
                    INSERT INTO app_settings(setting_key, value, updated_at)
                    VALUES ('display_timezone', 'America/Los_Angeles', '2026-07-01T00:00:00Z')
                    """
                )
                raw.commit()
            finally:
                raw.close()

            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                schema_version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            finally:
                conn.close()

        self.assertNotIn("app_settings", tables)
        self.assertEqual(schema_version, core.SCHEMA_VERSION)

    def test_migrate_tracks_prior_archivarix_requests_from_placeholder_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            raw = sqlite3.connect(db_path)
            try:
                raw.executescript(
                    """
                    CREATE TABLE schema_migrations (
                      version INTEGER PRIMARY KEY,
                      applied_at TEXT NOT NULL
                    );
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (4, '2026-07-14T00:00:00Z');
                    CREATE TABLE placeholder_recovery_worker_runs (
                      run_id TEXT PRIMARY KEY,
                      status TEXT NOT NULL DEFAULT '',
                      started_at TEXT NOT NULL,
                      finished_at TEXT,
                      total INTEGER NOT NULL DEFAULT 1,
                      processed INTEGER NOT NULL DEFAULT 0,
                      found INTEGER NOT NULL DEFAULT 0,
                      failed INTEGER NOT NULL DEFAULT 0,
                      queue_id INTEGER NOT NULL DEFAULT 0,
                      video_id TEXT NOT NULL DEFAULT '',
                      playlist_id TEXT NOT NULL DEFAULT '',
                      recovery_status TEXT NOT NULL DEFAULT '',
                      message TEXT NOT NULL DEFAULT ''
                    );
                    INSERT INTO placeholder_recovery_worker_runs(
                      run_id, status, started_at, recovery_status
                    ) VALUES
                      ('requested', 'complete', '2026-07-14T01:00:00Z', 'not_found'),
                      ('auth-failed', 'blocked', '2026-07-14T02:00:00Z', 'authentication_error'),
                      ('never-started', 'stopped', '2026-07-14T03:00:00Z', '');
                    """
                )
                raw.commit()
            finally:
                raw.close()

            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT run_id, request_started_at, request_count
                    FROM placeholder_recovery_worker_runs
                    ORDER BY started_at
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(rows[0]["request_started_at"], "2026-07-14T01:00:00Z")
        self.assertEqual(rows[0]["request_count"], 1)
        self.assertIsNone(rows[1]["request_started_at"])
        self.assertEqual(rows[1]["request_count"], 0)
        self.assertIsNone(rows[2]["request_started_at"])
        self.assertEqual(rows[2]["request_count"], 0)

    def test_migrate_marks_takeout_playlists_as_owned_and_in_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            legacy_schema = core.SCHEMA.replace(
                "  ownership TEXT NOT NULL DEFAULT 'unknown'\n"
                "    CHECK (ownership IN ('mine', 'others', 'unknown')),\n"
                "  in_library INTEGER NOT NULL DEFAULT 0 CHECK (in_library IN (0, 1)),\n"
                "  library_missing_at TEXT,\n",
                "",
            ).replace(
                "CREATE TABLE IF NOT EXISTS playlist_tombstones (\n"
                "  playlist_id TEXT PRIMARY KEY,\n"
                "  observed_removed_at TEXT NOT NULL,\n"
                "  reason TEXT NOT NULL DEFAULT 'authenticated_missing'\n"
                "    CHECK (reason IN ('authenticated_missing', 'missing_from_library', 'explicit_user')),\n"
                "  last_confirmed_at TEXT NOT NULL\n"
                ");\n\n",
                "",
            ).replace(
                (
                    "  reaction TEXT NOT NULL DEFAULT ''\n"
                    "    CHECK (reaction IN ('', 'LIKE', 'DISLIKE', 'INDIFFERENT')),\n"
                ),
                "  reaction TEXT NOT NULL DEFAULT '',\n",
            )
            raw = sqlite3.connect(db_path)
            try:
                raw.executescript(legacy_schema)
                raw.execute("DELETE FROM schema_migrations")
                raw.execute(
                    """
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (6, '2026-07-28T00:00:00Z')
                    """
                )
                raw.execute(
                    """
                    INSERT INTO playlists(playlist_id, title)
                    VALUES ('PLtakeout', 'Takeout playlist')
                    """
                )
                raw.execute(
                    """
                    INSERT INTO videos(video_id, title)
                    VALUES ('takeoutvid1', 'Takeout video')
                    """
                )
                raw.execute(
                    """
                    INSERT INTO playlist_items(
                      playlist_id, position, video_id, source_quality
                    )
                    VALUES ('PLtakeout', 1, 'takeoutvid1', 'takeout')
                    """
                )
                raw.commit()
            finally:
                raw.close()

            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                playlist = conn.execute(
                    """
                    SELECT ownership, in_library
                    FROM playlists
                    WHERE playlist_id = 'PLtakeout'
                    """
                ).fetchone()
                schema_version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(dict(playlist), {"ownership": "mine", "in_library": 1})
        self.assertEqual(schema_version, core.SCHEMA_VERSION)

    def test_v20_migration_tombstones_owned_removed_and_retains_foreign_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            legacy_schema = core.SCHEMA.replace(
                "  ownership TEXT NOT NULL DEFAULT 'unknown'\n"
                "    CHECK (ownership IN ('mine', 'others', 'unknown')),\n"
                "  in_library INTEGER NOT NULL DEFAULT 0 CHECK (in_library IN (0, 1)),\n"
                "  library_missing_at TEXT,\n",
                "  is_library_playlist INTEGER NOT NULL DEFAULT 0 "
                "CHECK (is_library_playlist IN (0, 1)),\n",
            ).replace(
                "CREATE TABLE IF NOT EXISTS playlist_tombstones (\n"
                "  playlist_id TEXT PRIMARY KEY,\n"
                "  observed_removed_at TEXT NOT NULL,\n"
                "  reason TEXT NOT NULL DEFAULT 'authenticated_missing'\n"
                "    CHECK (reason IN ('authenticated_missing', 'missing_from_library', 'explicit_user')),\n"
                "  last_confirmed_at TEXT NOT NULL\n"
                ");\n\n",
                "",
            ).replace(
                (
                    "  reaction TEXT NOT NULL DEFAULT ''\n"
                    "    CHECK (reaction IN ('', 'LIKE', 'DISLIKE', 'INDIFFERENT')),\n"
                ),
                "  reaction TEXT NOT NULL DEFAULT '',\n",
            )
            raw = sqlite3.connect(db_path)
            try:
                raw.executescript(legacy_schema)
                raw.execute("DELETE FROM schema_migrations")
                raw.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (19, '2026-08-01T00:00:00Z')"
                )
                raw.execute(
                    "INSERT INTO channels(channel_id, title) VALUES ('UCother', 'Other owner')"
                )
                raw.executemany(
                    """
                    INSERT INTO playlists(
                      playlist_id, title, owner_channel_id, fetch_status,
                      is_library_playlist
                    ) VALUES (?, ?, ?, 'removed', 1)
                    """,
                    [
                        ("PLowned", "Owned removed", None),
                        ("PLforeign", "Foreign removed", "UCother"),
                    ],
                )
                raw.execute(
                    """
                    INSERT INTO videos(video_id, title, reaction)
                    VALUES ('ownedvideo1', 'Owned video', 'L')
                    """
                )
                raw.execute(
                    """
                    INSERT INTO playlist_items(
                      playlist_id, position, video_id, source_quality
                    ) VALUES ('PLowned', 1, 'ownedvideo1', 'takeout')
                    """
                )
                raw.commit()
            finally:
                raw.close()

            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                owned = conn.execute(
                    "SELECT 1 FROM playlists WHERE playlist_id = 'PLowned'"
                ).fetchone()
                tombstone = conn.execute(
                    """
                    SELECT reason FROM playlist_tombstones
                    WHERE playlist_id = 'PLowned'
                    """
                ).fetchone()
                foreign = conn.execute(
                    """
                    SELECT ownership, in_library, fetch_status
                    FROM playlists
                    WHERE playlist_id = 'PLforeign'
                    """
                ).fetchone()
                retained_reaction = conn.execute(
                    "SELECT reaction FROM videos WHERE video_id = 'ownedvideo1'"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertIsNone(owned)
        self.assertEqual(tombstone["reason"], "authenticated_missing")
        self.assertEqual(
            dict(foreign),
            {"ownership": "others", "in_library": 1, "fetch_status": "unavailable"},
        )
        self.assertEqual(retained_reaction, "LIKE")

    def test_migrate_adds_nullable_channel_first_seen_without_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            legacy_schema = core.SCHEMA.replace("  first_seen_at TEXT,\n", "")
            raw = sqlite3.connect(db_path)
            try:
                raw.executescript(legacy_schema)
                raw.execute("DELETE FROM schema_migrations")
                raw.execute(
                    """
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (7, '2026-07-29T00:00:00Z')
                    """
                )
                raw.execute(
                    """
                    INSERT INTO channels(channel_id, title)
                    VALUES ('UClegacy', 'Legacy channel')
                    """
                )
                raw.commit()
            finally:
                raw.close()

            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(channels)")
                }
                first_seen_at = conn.execute(
                    "SELECT first_seen_at FROM channels WHERE channel_id = 'UClegacy'"
                ).fetchone()["first_seen_at"]
                schema_version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertIn("first_seen_at", columns)
        self.assertIsNone(first_seen_at)
        self.assertEqual(schema_version, core.SCHEMA_VERSION)

    def test_migrate_adds_channel_notification_level_without_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            legacy_schema = core.SCHEMA.replace(
                "  notification_level TEXT NOT NULL DEFAULT ''\n"
                "    CHECK (notification_level IN ('', 'all', 'personalized', 'none')),\n",
                "",
            )
            raw = sqlite3.connect(db_path)
            try:
                raw.executescript(legacy_schema)
                raw.execute("DELETE FROM schema_migrations")
                raw.execute(
                    """
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (8, '2026-07-30T00:00:00Z')
                    """
                )
                raw.execute(
                    """
                    INSERT INTO channels(channel_id, title)
                    VALUES ('UClegacy', 'Legacy channel')
                    """
                )
                raw.commit()
            finally:
                raw.close()

            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(channels)")
                }
                notification_level = conn.execute(
                    """
                    SELECT notification_level
                    FROM channels
                    WHERE channel_id = 'UClegacy'
                    """
                ).fetchone()["notification_level"]
                schema_version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertIn("notification_level", columns)
        self.assertEqual(notification_level, "")
        self.assertEqual(schema_version, core.SCHEMA_VERSION)

    def test_migrate_clears_video_id_title_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            raw = sqlite3.connect(db_path)
            try:
                raw.executescript(core.SCHEMA)
                raw.execute("DELETE FROM schema_migrations")
                raw.execute(
                    """
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (9, '2026-07-30T00:00:00Z')
                    """
                )
                raw.execute(
                    """
                    INSERT INTO videos(video_id, title)
                    VALUES ('abc12345678', 'abc12345678')
                    """
                )
                raw.execute(
                    """
                    INSERT INTO worker_queue(
                      subject_key, worker_type, task_type, video_id, current_title,
                      created_at, updated_at
                    )
                    VALUES (
                      'metadata:video:abc12345678', 'metadata', 'provided',
                      'abc12345678', 'abc12345678',
                      '2026-07-30T00:00:00Z', '2026-07-30T00:00:00Z'
                    )
                    """
                )
                raw.commit()
            finally:
                raw.close()

            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                title = conn.execute(
                    "SELECT title FROM videos WHERE video_id = 'abc12345678'"
                ).fetchone()["title"]
                current_title = conn.execute(
                    """
                    SELECT current_title
                    FROM worker_queue
                    WHERE video_id = 'abc12345678'
                    """
                ).fetchone()["current_title"]
                schema_version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(title, "")
        self.assertEqual(current_title, "")
        self.assertEqual(schema_version, core.SCHEMA_VERSION)

    def test_migrate_removes_video_watch_completion_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            legacy_schema = core.SCHEMA.replace(
                (
                    "  reaction TEXT NOT NULL DEFAULT ''\n"
                    "    CHECK (reaction IN ('', 'LIKE', 'DISLIKE', 'INDIFFERENT')),\n"
                ),
                (
                    "  reaction TEXT NOT NULL DEFAULT '',\n"
                    "  watch_progress_percent INTEGER NOT NULL DEFAULT 0,\n"
                    "  watch_resume_seconds INTEGER NOT NULL DEFAULT 0,\n"
                ),
            )
            raw = sqlite3.connect(db_path)
            try:
                raw.executescript(legacy_schema)
                raw.execute("DELETE FROM schema_migrations")
                raw.execute(
                    """
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (10, '2026-07-30T00:00:00Z')
                    """
                )
                raw.execute(
                    """
                    INSERT INTO videos(
                      video_id, title, watch_progress_percent, watch_resume_seconds
                    )
                    VALUES ('legacyvideo', 'Legacy video', 64, 217)
                    """
                )
                raw.execute(
                    """
                    INSERT INTO history_events(
                      event_id, video_id, watch_date, time_precision,
                      watch_progress_percent, watch_resume_seconds
                    )
                    VALUES (
                      'legacy-event', 'legacyvideo', '2026-07-30',
                      'date_only', 51, 180
                    )
                    """
                )
                raw.commit()
            finally:
                raw.close()

            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                video_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(videos)")
                }
                event = conn.execute(
                    """
                    SELECT watch_progress_percent, watch_resume_seconds
                    FROM history_events
                    WHERE event_id = 'legacy-event'
                    """
                ).fetchone()
                schema_version = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertNotIn("watch_progress_percent", video_columns)
        self.assertNotIn("watch_resume_seconds", video_columns)
        self.assertEqual(
            dict(event),
            {
                "watch_progress_percent": 51,
                "watch_resume_seconds": 180,
            },
        )
        self.assertEqual(schema_version, core.SCHEMA_VERSION)

    def test_migrate_adds_feature_backfill_markers_and_cleans_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            legacy_schema = (
                core.SCHEMA.replace(
                    "  subscription_checked_at TEXT,\n"
                    "  notification_checked_at TEXT,\n",
                    "",
                )
                .replace("  metadata_checked_at TEXT,\n", "")
                .replace("  visibility_checked_at TEXT,\n", "")
            )
            raw = sqlite3.connect(db_path)
            try:
                raw.executescript(legacy_schema)
                raw.execute("DELETE FROM schema_migrations")
                raw.execute(
                    """
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (11, '2026-07-30T00:00:00Z')
                    """
                )
                raw.execute(
                    """
                    INSERT INTO channels(
                      channel_id, title, subscribed, notification_level,
                      fetch_status, fetched_at
                    )
                    VALUES (
                      'UCrecent', 'Recent', 0, 'personalized',
                      'ok', '2026-07-31T04:00:00Z'
                    )
                    """
                )
                raw.execute(
                    """
                    INSERT INTO channels(
                      channel_id, title, subscribed, notification_level,
                      fetch_status, fetched_at
                    )
                    VALUES (
                      'UCold', 'Old', 1, '',
                      'ok', '2026-07-30T18:00:00Z'
                    )
                    """
                )
                raw.execute(
                    """
                    INSERT INTO playlists(
                      playlist_id, title, owner_channel_id, visibility, fetch_status
                    )
                    VALUES ('PLcomplete', 'Complete', 'UCrecent', 'public', 'ok')
                    """
                )
                raw.execute(
                    """
                    INSERT INTO playlist_scans(
                      playlist_id, scanned_at, video_count, unavailable_count,
                      scan_status, scan_error
                    )
                    VALUES (
                      'PLcomplete', '2026-07-31T03:00:00Z', 0, 0, 'ok', ''
                    )
                    """
                )
                raw.execute(
                    """
                    INSERT INTO videos(
                      video_id, title, channel_id, availability,
                      fetch_status, fetched_at
                    )
                    VALUES (
                      'recentvideo', 'Recent video', 'UCrecent', 'public',
                      'ok', '2026-07-31T03:00:00Z'
                    )
                    """
                )
                raw.commit()
            finally:
                raw.close()

            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                recent_channel = conn.execute(
                    """
                    SELECT notification_level, subscription_checked_at,
                           notification_checked_at
                    FROM channels
                    WHERE channel_id = 'UCrecent'
                    """
                ).fetchone()
                old_channel = conn.execute(
                    """
                    SELECT subscription_checked_at, notification_checked_at
                    FROM channels
                    WHERE channel_id = 'UCold'
                    """
                ).fetchone()
                playlist_checked_at = conn.execute(
                    """
                    SELECT metadata_checked_at
                    FROM playlists
                    WHERE playlist_id = 'PLcomplete'
                    """
                ).fetchone()["metadata_checked_at"]
                video_checked_at = conn.execute(
                    """
                    SELECT visibility_checked_at
                    FROM videos
                    WHERE video_id = 'recentvideo'
                    """
                ).fetchone()["visibility_checked_at"]
            finally:
                conn.close()

        self.assertEqual(recent_channel["notification_level"], "")
        self.assertEqual(
            recent_channel["subscription_checked_at"],
            "2026-07-31T04:00:00Z",
        )
        self.assertEqual(
            recent_channel["notification_checked_at"],
            "2026-07-31T04:00:00Z",
        )
        self.assertIsNone(old_channel["subscription_checked_at"])
        self.assertIsNone(old_channel["notification_checked_at"])
        self.assertEqual(playlist_checked_at, "2026-07-31T03:00:00Z")
        self.assertEqual(video_checked_at, "2026-07-31T03:00:00Z")

    def test_late_video_channel_link_does_not_backfill_first_seen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "latevideo",
                        title="Late-linked video",
                    )
                    conn.execute(
                        """
                        INSERT INTO history_events(
                          event_id, video_id, watched_at, watch_date, time_precision
                        )
                        VALUES (
                          'late-history', 'latevideo', '2021-07-06T13:28:35Z',
                          '2021-07-06', 'exact'
                        )
                        """
                    )
                    core.upsert_video(
                        conn,
                        "latevideo",
                        channel_id="UClate",
                        channel_title="Late-linked channel",
                        source="archivarix",
                        updated_at="2026-07-30T23:12:23Z",
                    )
                first_seen_at = conn.execute(
                    """
                    SELECT first_seen_at
                    FROM channels
                    WHERE channel_id = 'UClate'
                    """
                ).fetchone()["first_seen_at"]
            finally:
                conn.close()

        self.assertIsNone(first_seen_at)

    def test_manual_channel_enqueue_does_not_backfill_first_seen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_channel(conn, "UCqueued", title="Queued channel")
                    core.upsert_video(
                        conn,
                        "queuedvideo",
                        title="Queued video",
                        channel_id="UCqueued",
                    )
                    conn.execute(
                        """
                        INSERT INTO history_events(
                          event_id, video_id, watch_date, time_precision
                        )
                        VALUES (
                          'queued-history', 'queuedvideo', '2026-04-03', 'date_only'
                        )
                        """
                    )
                    conn.execute(
                        """
                        UPDATE channels
                        SET first_seen_at = NULL
                        WHERE channel_id = 'UCqueued'
                        """
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="UCqueued",
                        channel_id="UCqueued",
                        metadata_source="channel",
                        manual=False,
                    )
                automatic_value = conn.execute(
                    "SELECT first_seen_at FROM channels WHERE channel_id = 'UCqueued'"
                ).fetchone()["first_seen_at"]

                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="UCqueued",
                        channel_id="UCqueued",
                        metadata_source="channel",
                        manual=True,
                    )
                manual_value = conn.execute(
                    "SELECT first_seen_at FROM channels WHERE channel_id = 'UCqueued'"
                ).fetchone()["first_seen_at"]
            finally:
                conn.close()

        self.assertIsNone(automatic_value)
        self.assertIsNone(manual_value)

    def test_metadata_queue_can_scope_video_and_channel_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="videoqueue1",
                        metadata_source="history",
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="UCqueue",
                        channel_id="UCqueue",
                        metadata_source="channel",
                    )
                self.assertEqual(core.metadata_queue_count(conn), 2)
                self.assertEqual(
                    core.metadata_queue_count(conn, metadata_kind="video"),
                    1,
                )
                self.assertEqual(
                    core.metadata_queue_count(conn, metadata_kind="channel"),
                    1,
                )

                with conn:
                    cleared = core.clear_metadata_queue(
                        conn,
                        metadata_kind="video",
                    )
                remaining = core.metadata_queue_rows(conn)
            finally:
                conn.close()

        self.assertEqual(cleared, 1)
        self.assertEqual(
            [(row["metadata_source"], row["channel_id"]) for row in remaining],
            [("channel", "UCqueue")],
        )

    def test_manual_video_queue_keeps_missing_subject_title_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    result = core.enqueue_worker_queue_target(conn, "abc12345678")
                row = conn.execute(
                    """
                    SELECT video_id, current_title
                    FROM worker_queue
                    WHERE subject_key = ?
                    """,
                    (result["subject_key"],),
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(
            dict(row),
            {"video_id": "abc12345678", "current_title": ""},
        )

    def test_feature_backfill_counts_and_queues_only_unchecked_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_channel(conn, "UCchannelneeded", title="Needed channel")
                    core.upsert_channel(conn, "UCchanneldone", title="Done channel")
                    conn.execute(
                        """
                        UPDATE channels
                        SET subscribed = 1,
                            notification_level = 'all',
                            subscription_checked_at = '2026-07-31T01:00:00Z',
                            notification_checked_at = '2026-07-31T01:00:00Z'
                        WHERE channel_id = 'UCchanneldone'
                        """
                    )
                    conn.executemany(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, owner_channel_id, visibility,
                          metadata_checked_at
                        )
                        VALUES (?, ?, 'UCchanneldone', 'public', ?)
                        """,
                        [
                            ("PLneeded", "Needed playlist", None),
                            ("PLdone", "Done playlist", "2026-07-31T01:00:00Z"),
                        ],
                    )
                    core.upsert_video(
                        conn,
                        "neededvid01",
                        title="Needed video",
                        channel_id="UCchanneldone",
                        availability="public",
                    )
                    core.upsert_video(
                        conn,
                        "donevideo01",
                        title="Done video",
                        channel_id="UCchanneldone",
                        availability="public",
                    )
                    conn.execute(
                        """
                        UPDATE videos
                        SET visibility_checked_at = '2026-07-31T01:00:00Z'
                        WHERE video_id = 'donevideo01'
                        """
                    )

                counts = core.feature_backfill_counts(conn)
                self.assertEqual(counts["channel_account"], 1)
                self.assertEqual(counts["playlist_metadata"], 1)
                self.assertEqual(counts["video_visibility"], 1)

                with conn:
                    channel_result = core.enqueue_feature_backfill(
                        conn,
                        "channel_account",
                    )
                self.assertEqual(channel_result["inserted"], 1)
                channel_row = core.metadata_queue_rows(conn, limit=1)[0]
                self.assertEqual(channel_row["channel_id"], "UCchannelneeded")

                with conn:
                    core.clear_worker_queue(conn)
                    playlist_result = core.enqueue_feature_backfill(
                        conn,
                        "playlist_metadata",
                    )
                self.assertEqual(playlist_result["inserted"], 1)
                playlist_row = core.playlist_scan_queue_rows(conn, limit=1)[0]
                self.assertEqual(playlist_row["playlist_id"], "PLneeded")

                with conn:
                    core.clear_worker_queue(conn)
                    video_result = core.enqueue_feature_backfill(
                        conn,
                        "video_visibility",
                    )
                self.assertEqual(video_result["inserted"], 1)
                video_row = core.metadata_queue_rows(conn, limit=1)[0]
                self.assertEqual(video_row["video_id"], "neededvid01")
            finally:
                conn.close()

    def test_admin_runtime_status_reports_queue_and_service_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_worker_queue_target(conn, "runtime00123")
                    core.set_external_service_block(
                        conn,
                        "proxy",
                        "proxy_unavailable",
                        "Proxy connection refused",
                    )
            finally:
                conn.close()

            dispatcher = Mock()
            dispatcher.is_running.return_value = True
            dispatcher.is_stopping.return_value = False
            dispatcher.stats.return_value = {
                "remaining_count": 1,
                "completed_count": 2,
            }

            status = core.admin_runtime_status(db_path, dispatcher)

        self.assertTrue(status["workerQueueRunning"])
        self.assertFalse(status["workerQueueStopping"])
        self.assertEqual(status["workerQueueCount"], 1)
        self.assertEqual(status["workerQueueStats"]["completed_count"], 2)
        self.assertTrue(status["proxyBlock"]["blocked"])
        self.assertEqual(status["proxyBlock"]["message"], "Proxy connection refused")
        self.assertFalse(status["archivarixBlock"]["blocked"])
        dispatcher.stats.assert_called_once_with(1)

    def test_initialize_queues_full_scan_without_clearing_existing_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                self.assertFalse(core.library_has_data(conn))
                self.assertFalse(core.admin_status(db_path)["hasLibraryData"])
                with conn:
                    core.upsert_channel(conn, "UCinitialize", title="Initialize channel")
                    core.upsert_video(
                        conn,
                        "initvideo01",
                        title="Initialize video",
                        channel_id="UCinitialize",
                    )
                    conn.execute(
                        """
                        INSERT INTO playlists(playlist_id, title)
                        VALUES ('PLinitialize', 'Initialize playlist')
                        """
                    )
                    existing = core.enqueue_worker_queue_target(conn, "queuedvid01")
                    stats = core.enqueue_initialization_tasks(conn)
                    repeated_stats = core.enqueue_initialization_tasks(conn)

                subjects = {
                    row["subject_key"]
                    for row in conn.execute("SELECT subject_key FROM worker_queue")
                }
                self.assertTrue(core.library_has_data(conn))
                self.assertTrue(core.admin_status(db_path)["hasLibraryData"])
            finally:
                conn.close()

        self.assertTrue(stats["had_data"])
        self.assertEqual(stats["metadata"], 2)
        self.assertEqual(stats["playlists"], 2)
        self.assertEqual(stats["history"], 1)
        self.assertEqual(stats["account"], 1)
        self.assertEqual(stats["clips"], 1)
        self.assertEqual(stats["selected"], 7)
        self.assertEqual(stats["inserted"], 7)
        self.assertEqual(stats["already_queued"], 0)
        self.assertEqual(stats["queued"], 8)
        self.assertEqual(repeated_stats["inserted"], 0)
        self.assertEqual(repeated_stats["already_queued"], 7)
        self.assertEqual(repeated_stats["queued"], 8)
        self.assertIn(existing["subject_key"], subjects)
        self.assertIn("metadata:channel:UCinitialize", subjects)
        self.assertIn("metadata:video:initvideo01", subjects)
        self.assertIn("playlist:scan:LL", subjects)
        self.assertIn("playlist:scan:PLinitialize", subjects)
        self.assertIn("history:verify", subjects)
        self.assertIn("account:sync", subjects)
        self.assertIn("clip:discover", subjects)

    def test_update_queues_incremental_discovery_and_never_fetched_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_channel(conn, "UCupdatechannel", title="New channel")
                    core.upsert_video(
                        conn,
                        "newupdate01",
                        title="New video",
                        channel_id="UCupdatechannel",
                    )
                    core.upsert_video(conn, "oldupdate01", title="Old video")
                    conn.execute(
                        "UPDATE videos SET fetched_at = '2025-01-01T00:00:00Z' "
                        "WHERE video_id = 'oldupdate01'"
                    )
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLupdate', 'Due playlist')"
                    )
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLremoved', 'Removed playlist')"
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_scans(
                          playlist_id, scanned_at, video_count, unavailable_count, scan_status
                        ) VALUES ('PLremoved', '2026-07-01T00:00:00Z', 0, 0, 'removed')
                        """
                    )
                    existing = core.enqueue_worker_queue_target(conn, "queuedvid01")
                    stats = core.enqueue_update_tasks(conn)
                    repeated_stats = core.enqueue_update_tasks(conn)

                rows = conn.execute(
                    "SELECT subject_key, source_key, manual FROM worker_queue ORDER BY subject_key"
                ).fetchall()
                subjects = {row["subject_key"] for row in rows}
                discovery = next(row for row in rows if row["subject_key"] == "playlist:discover-current")
            finally:
                conn.close()

        self.assertEqual(stats["selected"], 7)
        self.assertEqual(stats["inserted"], 7)
        self.assertEqual(stats["already_queued"], 0)
        self.assertEqual(stats["queued"], 8)
        self.assertEqual(repeated_stats["inserted"], 0)
        self.assertEqual(repeated_stats["already_queued"], 7)
        self.assertIn(existing["subject_key"], subjects)
        self.assertIn("playlist:discover-current", subjects)
        self.assertIn("history:recent", subjects)
        self.assertIn("account:sync", subjects)
        self.assertIn("clip:discover", subjects)
        self.assertNotIn("playlist:scan:LL", subjects)
        self.assertIn("playlist:scan:PLupdate", subjects)
        self.assertNotIn("playlist:scan:PLremoved", subjects)
        self.assertIn("metadata:channel:UCupdatechannel", subjects)
        self.assertIn("metadata:video:newupdate01", subjects)
        self.assertNotIn("metadata:video:oldupdate01", subjects)
        self.assertEqual(discovery["source_key"], "new")
        self.assertFalse(discovery["manual"])

    def test_update_queues_plan_work_at_front_of_existing_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_channel(conn, "UCpriority", title="Priority channel")
                    core.upsert_video(
                        conn,
                        "priority001",
                        title="Priority video",
                        channel_id="UCpriority",
                    )
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLpriority', 'Priority')"
                    )
                    core.enqueue_placeholder_recovery_item(
                        conn,
                        video_id="unavailable01",
                        current_title="Unavailable video",
                        priority=-500,
                    )
                    core.upsert_video(
                        conn,
                        "background01",
                        title="Existing background task",
                        fetch_status="ok",
                        fetched_at=core.utc_now(),
                        source="youtube",
                    )
                    background_subject = core.enqueue_metadata_item(
                        conn,
                        video_id="background01",
                        current_title="Existing background task",
                        priority=-1_000,
                    )
                    existing_update_subject = core.enqueue_metadata_item(
                        conn,
                        channel_id="UCpriority",
                        channel_title="Priority channel",
                        metadata_source="channel",
                        priority=9_999,
                    )
                    placeholder_subject = core.placeholder_queue_subject_key(
                        "unavailable01"
                    )
                    core.enqueue_update_tasks(conn)

                rows = conn.execute(
                    "SELECT subject_key, priority FROM worker_queue ORDER BY priority"
                ).fetchall()
            finally:
                conn.close()

        existing_priorities = [
            row["priority"]
            for row in rows
            if row["subject_key"] in {background_subject, placeholder_subject}
        ]
        update_priorities = [
            row["priority"]
            for row in rows
            if row["subject_key"] not in {background_subject, placeholder_subject}
        ]
        self.assertTrue(update_priorities)
        self.assertLess(max(update_priorities), min(existing_priorities))
        self.assertIn(
            existing_update_subject,
            {row["subject_key"] for row in rows if row["priority"] < -1_000},
        )

    def test_update_promotes_active_broadcast_rechecks_ahead_of_bulk_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                fetched_at = core.utc_now()
                with conn:
                    for video_id, status in (
                        ("active12345", "live"),
                        ("future12345", "upcoming"),
                        ("ordinary12", ""),
                    ):
                        core.upsert_video(
                            conn,
                            video_id,
                            title=video_id,
                            video_type=("video" if status == "" else "livestream"),
                            broadcast_status=status,
                            broadcast_status_checked_at=core.utc_days_ago(2),
                            fetch_status="ok",
                            fetched_at=fetched_at,
                            source="youtube",
                        )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="ordinary12",
                        current_title="ordinary12",
                        priority=100,
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="active12345",
                        current_title="active12345",
                        priority=9_999,
                    )
                    core.enqueue_update_tasks(conn)

                priorities = {
                    row["subject_key"]: int(row["priority"])
                    for row in conn.execute(
                        "SELECT subject_key, priority FROM worker_queue"
                    )
                }
            finally:
                conn.close()

        ordinary_priority = priorities["metadata:video:ordinary12"]
        active_priority = priorities["metadata:video:active12345"]
        upcoming_priority = priorities["metadata:video:future12345"]
        self.assertLess(active_priority, ordinary_priority)
        self.assertLess(upcoming_priority, ordinary_priority)
        self.assertEqual(abs(active_priority - upcoming_priority), 1)

    def test_playlist_due_selection_ignores_scan_age_but_keeps_integrity_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.executemany(
                        """
                        INSERT INTO playlists(playlist_id, title, video_count)
                        VALUES (?, ?, ?)
                        """,
                        (
                            ("PLoldok", "Old complete scan", 2),
                            ("PLerror", "Failed scan", 2),
                            ("PLmismatch", "Count mismatch", 3),
                        ),
                    )
                    conn.executemany(
                        """
                        INSERT INTO playlist_scans(
                          playlist_id, scanned_at, video_count, unavailable_count, scan_status
                        ) VALUES (?, '2020-01-01T00:00:00Z', ?, 0, ?)
                        """,
                        (
                            ("PLoldok", 2, "ok"),
                            ("PLerror", 2, "error"),
                            ("PLmismatch", 2, "ok"),
                        ),
                    )

                due_ids = {
                    row["playlist_id"] for row in core.playlist_scan_candidate_rows(conn)
                }
                forced_ids = {
                    row["playlist_id"]
                    for row in core.playlist_scan_candidate_rows(conn, force=True)
                }
            finally:
                conn.close()

        self.assertEqual(due_ids, {"PLerror", "PLmismatch"})
        self.assertEqual(forced_ids, {"PLoldok", "PLerror", "PLmismatch"})

    def test_rebuild_queue_replaces_automatic_plan_rows_and_preserves_manual_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_channel(conn, "UCrebuildchannel", title="Rebuild channel")
                    core.upsert_video(
                        conn,
                        "duevideo001",
                        title="Due video",
                        channel_id="UCrebuildchannel",
                    )
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLrebuild', 'Due playlist')"
                    )
                    core.enqueue_account_sync_task(conn, manual=False)
                    core.enqueue_history_task(conn, "verify", manual=False)
                    core.enqueue_metadata_item(
                        conn,
                        video_id="obsolete001",
                        current_title="Obsolete manual target",
                        manual=True,
                    )
                    core.enqueue_playlist_scan_item(
                        conn,
                        "PLobsolete",
                        title="Obsolete manual playlist",
                        manual=True,
                    )
                    core.enqueue_clip_item(
                        conn,
                        task_type="discover",
                        mode="all",
                        manual=True,
                    )
                    core.enqueue_clip_item(
                        conn,
                        clip_id="UgkxRebuildClip",
                        task_type="scan",
                        manual=True,
                    )
                    now = core.utc_now()
                    conn.executemany(
                        """
                        INSERT INTO worker_queue(
                          subject_key, worker_type, task_type, current_title,
                          manual, created_at, updated_at
                        ) VALUES (?, ?, 'test', ?, ?, ?, ?)
                        """,
                        (
                            ("account:manual-preserved", "account", "Manual account", 1, now, now),
                            ("history:manual-preserved", "history", "Manual history", 1, now, now),
                            ("placeholder:preserved", "placeholder", "Recovery", 0, now, now),
                            ("plugin:example:fetch:1", "plugin", "Plugin", 0, now, now),
                            ("future:preserved", "future", "Future", 0, now, now),
                        ),
                    )

                    stats = core.rebuild_library_queue(conn)

                rows = {
                    row["subject_key"]: dict(row)
                    for row in conn.execute(
                        "SELECT subject_key, worker_type, task_type, source_key, priority, manual "
                        "FROM worker_queue ORDER BY subject_key"
                    )
                }
            finally:
                conn.close()

        self.assertEqual(
            stats["cleared_by_type"],
            {"account": 1, "history": 1, "metadata": 0, "playlist": 0},
        )
        self.assertEqual(
            stats["preserved_by_type"],
            {
                "account": 1,
                "clip": 2,
                "future": 1,
                "history": 1,
                "metadata": 1,
                "placeholder": 1,
                "playlist": 1,
                "plugin": 1,
            },
        )
        self.assertEqual(stats["preserved"], 9)
        self.assertEqual(stats["metadata"], 2)
        self.assertEqual(stats["playlist_scans"], 1)
        self.assertEqual(stats["selected"], 7)
        self.assertEqual(stats["inserted"], 6)
        self.assertEqual(stats["already_queued"], 1)
        self.assertIn("metadata:video:obsolete001", rows)
        self.assertTrue(rows["metadata:video:obsolete001"]["manual"])
        self.assertIn("playlist:scan:PLobsolete", rows)
        self.assertTrue(rows["playlist:scan:PLobsolete"]["manual"])
        self.assertNotIn("history:verify", rows)
        self.assertIn("history:recent", rows)
        self.assertIn("account:manual-preserved", rows)
        self.assertTrue(rows["account:manual-preserved"]["manual"])
        self.assertIn("history:manual-preserved", rows)
        self.assertTrue(rows["history:manual-preserved"]["manual"])
        self.assertIn("playlist:discover-current", rows)
        self.assertNotIn("playlist:scan:LL", rows)
        self.assertIn("playlist:scan:PLrebuild", rows)
        self.assertIn("metadata:video:duevideo001", rows)
        self.assertIn("metadata:channel:UCrebuildchannel", rows)
        self.assertIn("placeholder:preserved", rows)
        self.assertIn("plugin:example:fetch:1", rows)
        self.assertIn("future:preserved", rows)
        self.assertIn("clip:scan:UgkxRebuildClip", rows)
        self.assertEqual(rows["clip:discover"]["source_key"], "all")
        self.assertTrue(rows["clip:discover"]["manual"])
        self.assertEqual(rows["playlist:discover-current"]["source_key"], "new")
        self.assertEqual(rows["playlist:scan:PLrebuild"]["source_key"], "rebuild")
        self.assertEqual(rows["metadata:video:duevideo001"]["source_key"], "rebuild")
        self.assertEqual(rows["account:sync"]["priority"], -4)
        self.assertEqual(rows["history:recent"]["priority"], -1)

    def test_recent_channel_fetch_without_thumbnail_ages_out_of_metadata_queue(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
                try:
                    now = core.utc_now()
                    core.upsert_channel(
                        conn,
                        "UCvmGOqGlxOgpZDoszBbWxmA",
                        title="Example Channel",
                        thumbnail_path="",
                        source="test",
                        updated_at=now,
                    )
                    queued = core.metadata_queue_candidate_rows(conn, limit=10, stale_days=30)
                    self.assertEqual([row["video_id"] for row in queued], ["UCvmGOqGlxOgpZDoszBbWxmA"])

                    stats = core.rebuild_metadata_queue(conn, stale_days=30)
                    self.assertEqual(stats["inserted"], 1)
                    persisted = core.metadata_queue_rows(conn, limit=10)
                    self.assertEqual([row["video_id"] for row in persisted], ["UCvmGOqGlxOgpZDoszBbWxmA"])

                    with conn:
                        conn.execute(
                            """
                            INSERT INTO playlists(playlist_id, title)
                            VALUES ('PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4', 'Test playlist')
                            """
                        )
                        core.upsert_video(conn, "abc12345678", title="First", source="takeout")
                        core.upsert_video(conn, "def12345678", title="Second", source="takeout")
                        conn.executemany(
                            """
                            INSERT INTO playlist_items(playlist_id, position, video_id)
                            VALUES ('PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4', ?, ?)
                            """,
                            [(1, "abc12345678"), (2, "def12345678")],
                        )
                    with conn:
                        unified_youtube_playlist = core.enqueue_worker_queue_target(
                            conn,
                            "https://www.youtube.com/playlist?list=PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4",
                        )
                    self.assertEqual(unified_youtube_playlist["worker_type"], "playlist")
                    self.assertEqual(unified_youtube_playlist["source"], "youtube")
                    with conn:
                        core.clear_worker_queue(conn)
                        unified_local_playlist = core.enqueue_worker_queue_target(
                            conn,
                            "http://127.0.0.1:8765/playlists/PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4",
                        )
                    self.assertEqual(unified_local_playlist["worker_type"], "playlist")
                    self.assertEqual(unified_local_playlist["source"], "local")
                    self.assertEqual(unified_local_playlist["queued_count"], "1")
                    self.assertEqual(core.worker_queue_type_count(conn, "playlist"), 1)
                    queued_local_rows = core.playlist_scan_queue_rows(conn, limit=10)
                    self.assertEqual(
                        [row["playlist_id"] for row in queued_local_rows],
                        ["PLRTzPJUdKxQ_09dcCZZURVVavWaZq11E4"],
                    )
                    with conn:
                        core.clear_worker_queue(conn)
                        clip_target = core.enqueue_worker_queue_target(
                            conn,
                            "https://www.youtube.com/clip/UgkxUIUr7iJI7JSqsEGWEYebU5mV1PaMbz9s",
                        )
                    self.assertEqual(clip_target["worker_type"], "clip")
                    self.assertEqual(clip_target["source"], "youtube")
                    clip_row = conn.execute(
                        "SELECT worker_type, clip_id FROM worker_queue"
                    ).fetchone()
                    self.assertEqual(clip_row["worker_type"], "clip")
                    self.assertEqual(
                        clip_row["clip_id"],
                        "UgkxUIUr7iJI7JSqsEGWEYebU5mV1PaMbz9s",
                    )
                    with conn:
                        core.clear_worker_queue(conn)
                        local_clip_target = core.enqueue_worker_queue_target(
                            conn,
                            "http://127.0.0.1:8765/clips/UgkxUIUr7iJI7JSqsEGWEYebU5mV1PaMbz9s",
                        )
                    self.assertEqual(local_clip_target["worker_type"], "clip")
                    self.assertEqual(local_clip_target["source"], "local")
                    self.assertEqual(
                        local_clip_target["clip_id"],
                        "UgkxUIUr7iJI7JSqsEGWEYebU5mV1PaMbz9s",
                    )
                    playlist_video_rows = [
                        row
                        for row in core.metadata_queue_rows(conn, limit=10)
                        if row["metadata_source"] == "playlist"
                    ]
                    self.assertEqual(playlist_video_rows, [])

                    core.upsert_channel(
                        conn,
                        "UCvmGOqGlxOgpZDoszBbWxmA",
                        title="Example Channel",
                        thumbnail_path="",
                        fetch_status="no_metadata",
                        fetched_at=now,
                        source="test",
                        updated_at=now,
                    )
                    remaining = core.metadata_queue_candidate_rows(conn, limit=10, stale_days=30)
                    self.assertNotIn("UCvmGOqGlxOgpZDoszBbWxmA", [row["video_id"] for row in remaining])
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root

    def test_history_metadata_candidates_sort_by_latest_watch_date_descending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    videos = [
                        ("old12345678", "A alphabetically first", "2026-01-01T00:00:00Z"),
                        ("new12345678", "Z alphabetically last", "2026-03-01T00:00:00Z"),
                        ("mid12345678", "M alphabetically middle", "2026-02-01T00:00:00Z"),
                    ]
                    for video_id, title, watched_at in videos:
                        core.upsert_video(conn, video_id, title=title, source="takeout")
                        conn.execute(
                            """
                            INSERT INTO history_events(
                              event_id, video_id, watched_at, watch_date, time_precision, source_type
                            )
                            VALUES (?, ?, ?, ?, 'exact', 'takeout')
                            """,
                            (f"takeout:{video_id}", video_id, watched_at, watched_at[:10]),
                        )

                candidates = core.metadata_queue_candidate_rows(conn, limit=10, stale_days=30)
                self.assertEqual(
                    [row["video_id"] for row in candidates],
                    ["new12345678", "mid12345678", "old12345678"],
                )

                with conn:
                    core.rebuild_metadata_queue(conn, stale_days=30)
                queued = core.metadata_queue_rows(conn, limit=10)
                self.assertEqual(
                    [row["video_id"] for row in queued],
                    ["new12345678", "mid12345678", "old12345678"],
                )
            finally:
                conn.close()

    def test_update_rechecks_active_and_upcoming_broadcasts_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                fetched_at = core.utc_now()
                with conn:
                    for video_id, status in (
                        ("active12345", "live"),
                        ("future12345", "upcoming"),
                        ("ended123456", "ended"),
                        ("ordinary12", ""),
                    ):
                        core.upsert_video(
                            conn,
                            video_id,
                            title=video_id,
                            video_type=("video" if status == "" else "livestream"),
                            broadcast_status=status,
                            broadcast_status_checked_at=core.utc_days_ago(2),
                            fetch_status="ok",
                            fetched_at=fetched_at,
                            source="youtube",
                        )

                candidates = core.metadata_queue_candidate_rows(
                    conn,
                    limit=10,
                    metadata_kind="video",
                    never_fetched_only=True,
                )
            finally:
                conn.close()

        self.assertEqual(
            {row["video_id"] for row in candidates},
            {"active12345", "future12345"},
        )

    def test_save_playlist_scan_updates_playlist_metadata(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
                try:
                    with conn:
                        conn.execute(
                            """
                            INSERT INTO playlists(
                              playlist_id, title, description, visibility, video_count,
                              thumbnail_url, thumbnail_path, fetch_status, fetch_error,
                              youtube_updated_date, updated_at
                            )
                            VALUES (
                              'PLrename', 'Old name', 'Old description', 'public', 1,
                              'https://example.test/old.jpg', 'thumbs/PLrename.jpg',
                              'ok', '', '2026-08-27', '2026-07-01T00:00:00Z'
                            )
                            """
                        )
                        core.save_playlist_scan(
                            conn,
                            "PLrename",
                            [
                                {
                                    "playlist_id": "PLrename",
                                    "position": 1,
                                    "video_id": "abc12345678",
                                    "title": "Video",
                                    "channel_id": "",
                                    "channel": "",
                                    "duration_text": "1:00",
                                    "is_playable": 1,
                                    "availability": "LIVE",
                                    "url": "https://www.youtube.com/watch?v=abc12345678",
                                }
                            ],
                            "ok",
                            "",
                            playlist_metadata={
                                "title": "New name",
                                "description": "New description",
                                "owner": "New owner",
                                "owner_channel_id": "UCnewownerchannel123456789",
                                "collaborators_authoritative": True,
                                "collaborators": [
                                    {
                                        "title": "Collaborator",
                                        "channel_id": "UCcollaborator1234567890",
                                        "thumbnail_url": "https://example.test/collaborator.jpg",
                                    },
                                    {
                                        "title": "Duplicate owner",
                                        "channel_id": "UCnewownerchannel123456789",
                                    },
                                ],
                                "visibility": "unlisted",
                                "video_count": 1,
                                "youtube_updated_text": "Updated yesterday",
                                "youtube_updated_date": "2026-08-26",
                                "thumbnail_url": "https://example.test/new.jpg",
                                "url": "https://www.youtube.com/playlist?list=PLrename",
                            },
                        )
                    row = conn.execute(
                        "SELECT title, description, owner_channel_id, visibility, video_count, youtube_updated_date, thumbnail_url, thumbnail_path, metadata_checked_at FROM playlists WHERE playlist_id = 'PLrename'"
                    ).fetchone()
                    self.assertEqual(row["title"], "New name")
                    self.assertEqual(row["description"], "New description")
                    self.assertEqual(row["owner_channel_id"], "UCnewownerchannel123456789")
                    self.assertEqual(row["visibility"], "unlisted")
                    self.assertEqual(row["video_count"], 1)
                    self.assertEqual(row["youtube_updated_date"], "2026-08-26")
                    self.assertEqual(row["thumbnail_url"], "https://example.test/new.jpg")
                    self.assertEqual(row["thumbnail_path"], "thumbs/PLrename.jpg")
                    self.assertTrue(row["metadata_checked_at"])
                    channel = conn.execute(
                        "SELECT title, metadata_source FROM channels WHERE channel_id = 'UCnewownerchannel123456789'"
                    ).fetchone()
                    self.assertIsNotNone(channel)
                    self.assertEqual(channel["title"], "New owner")
                    self.assertEqual(channel["metadata_source"], "playlist_owner")
                    collaborators = conn.execute(
                        """
                        SELECT pc.channel_id, pc.position, ch.title, ch.metadata_source
                        FROM playlist_collaborators pc
                        JOIN channels ch ON ch.channel_id = pc.channel_id
                        WHERE pc.playlist_id = 'PLrename'
                        """
                    ).fetchall()
                    self.assertEqual(len(collaborators), 1)
                    self.assertEqual(collaborators[0]["channel_id"], "UCcollaborator1234567890")
                    self.assertEqual(collaborators[0]["title"], "Collaborator")
                    self.assertEqual(collaborators[0]["metadata_source"], "playlist_collaborator")
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root

    def test_playlist_scan_persistence_creates_missing_parent_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.save_playlist_scan(
                        conn,
                        "PLnewscan",
                        [
                            {
                                "playlist_id": "PLnewscan",
                                "position": 1,
                                "video_id": "newscanvid1",
                                "title": "New scan video",
                                "channel_id": "",
                                "channel": "",
                                "duration_text": "1:00",
                                "is_playable": 1,
                                "availability": "public",
                                "url": "https://www.youtube.com/watch?v=newscanvid1",
                            }
                        ],
                        "ok",
                        "",
                        playlist_metadata={
                            "title": "New playlist",
                            "video_count": 1,
                        },
                    )
                    core.save_playlist_scan_error(
                        conn,
                        "PLnewerror",
                        "playlist count unavailable",
                    )
                    core.save_playlist_missing_status(
                        conn,
                        "PLnewmissing",
                        "unavailable",
                        "authenticated YouTube 404",
                    )

                playlists = {
                    row["playlist_id"]: dict(row)
                    for row in conn.execute(
                        """
                        SELECT playlist_id, title, fetch_status
                        FROM playlists
                        WHERE playlist_id IN ('PLnewscan', 'PLnewerror', 'PLnewmissing')
                        """
                    )
                }
                scans = {
                    row["playlist_id"]: dict(row)
                    for row in conn.execute(
                        """
                        SELECT playlist_id, video_count, scan_status
                        FROM playlist_scans
                        WHERE playlist_id IN ('PLnewscan', 'PLnewerror', 'PLnewmissing')
                        """
                    )
                }
                item = conn.execute(
                    """
                    SELECT video_id
                    FROM playlist_items
                    WHERE playlist_id = 'PLnewscan'
                    """
                ).fetchone()
                foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            finally:
                conn.close()

        self.assertEqual(playlists["PLnewscan"]["title"], "New playlist")
        self.assertEqual(playlists["PLnewerror"]["title"], "PLnewerror")
        self.assertEqual(playlists["PLnewmissing"]["fetch_status"], "unavailable")
        self.assertEqual(scans["PLnewscan"]["video_count"], 1)
        self.assertEqual(scans["PLnewscan"]["scan_status"], "ok")
        self.assertEqual(scans["PLnewerror"]["scan_status"], "error")
        self.assertEqual(scans["PLnewmissing"]["scan_status"], "unavailable")
        self.assertEqual(item["video_id"], "newscanvid1")
        self.assertEqual(foreign_key_errors, [])

    def test_youtube_updated_date_is_separate_from_playlist_change_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, description, in_library, video_count,
                          last_changed_at
                        ) VALUES (
                          'PLyoutubeupdated', 'YouTube dated', 'Real description',
                          1, 3, '2026-08-20T12:34:56Z'
                        )
                        """
                    )
                    core.save_discovered_playlists(
                        conn,
                        [
                            {
                                "playlist_id": "PLyoutubeupdated",
                                "title": "YouTube dated",
                                "description": "",
                                "youtube_updated_date": "2026-08-25",
                                "video_count": "3",
                            }
                        ],
                    )
                    core.save_discovered_playlists(
                        conn,
                        [
                            {
                                "playlist_id": "PLyoutubeupdated",
                                "title": "YouTube dated",
                                "description": "",
                                "youtube_updated_date": "2026-03-09",
                                "video_count": "3",
                            }
                        ],
                    )
                row = conn.execute(
                    """
                    SELECT description, youtube_updated_date, last_changed_at
                    FROM playlists
                    WHERE playlist_id = 'PLyoutubeupdated'
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(row["description"], "Real description")
        self.assertEqual(row["youtube_updated_date"], "2026-08-25")
        self.assertEqual(row["last_changed_at"], "2026-08-20T12:34:56Z")

    def test_playlist_discovery_corrects_relative_youtube_date_rollover(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, youtube_updated_date, last_changed_at
                        ) VALUES (
                          'PLrollover', 'Rollover', '2026-08-27',
                          '2026-08-20T12:34:56Z'
                        )
                        """
                    )
                    core.save_discovered_playlists(
                        conn,
                        [
                            {
                                "playlist_id": "PLrollover",
                                "title": "Rollover",
                                "youtube_updated_text": "Updated yesterday",
                                "youtube_updated_date": "2026-08-26",
                                "video_count": 1,
                            }
                        ],
                    )
                row = conn.execute(
                    """
                    SELECT youtube_updated_date, last_changed_at
                    FROM playlists
                    WHERE playlist_id = 'PLrollover'
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(row["youtube_updated_date"], "2026-08-26")
        self.assertEqual(row["last_changed_at"], "2026-08-20T12:34:56Z")

    def test_playlist_discovery_preserves_authoritative_count_and_change_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, in_library, video_count, last_changed_at
                        ) VALUES (
                          'PLdiscoveryhint', 'Authoritative title', 1, 14,
                          '2026-08-20T12:34:56Z'
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_scans(
                          playlist_id, scanned_at, video_count, unavailable_count,
                          scan_status
                        ) VALUES (
                          'PLdiscoveryhint', '2026-08-20T12:34:56Z', 14, 0, 'ok'
                        )
                        """
                    )
                    missing_count = core.save_discovered_playlists(
                        conn,
                        [
                            {
                                "playlist_id": "PLdiscoveryhint",
                                "title": "Discovery title",
                                "video_count": 0,
                                "has_video_count": False,
                            }
                        ],
                    )
                    differing_count = core.save_discovered_playlists(
                        conn,
                        [
                            {
                                "playlist_id": "PLdiscoveryhint",
                                "title": "Discovery title",
                                "video_count": 15,
                                "has_video_count": True,
                            }
                        ],
                    )
                playlist = conn.execute(
                    """
                    SELECT title, video_count, last_changed_at
                    FROM playlists
                    WHERE playlist_id = 'PLdiscoveryhint'
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(playlist["title"], "Discovery title")
        self.assertEqual(playlist["video_count"], 14)
        self.assertEqual(playlist["last_changed_at"], "2026-08-20T12:34:56Z")
        self.assertEqual(missing_count["change_candidate_ids"], [])
        self.assertEqual(differing_count["change_candidate_ids"], ["PLdiscoveryhint"])

    def test_playlist_metadata_observation_is_a_check_not_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, visibility, video_count,
                          youtube_updated_date, last_changed_at
                        ) VALUES (
                          'PLmetadataonly', 'Metadata only', 'private', 1,
                          '2026-08-27', '2026-08-20T12:34:56Z'
                        )
                        """
                    )
                    core.upsert_video(
                        conn,
                        "member00001",
                        title="Existing member",
                        source="playlist",
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_items(playlist_id, position, video_id)
                        VALUES ('PLmetadataonly', 1, 'member00001')
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_scans(
                          playlist_id, scanned_at, video_count, unavailable_count,
                          scan_status
                        ) VALUES (
                          'PLmetadataonly', '2026-08-20T12:34:56Z', 1, 0, 'ok'
                        )
                        """
                    )
                    saved = core.save_playlist_metadata_observation(
                        conn,
                        "PLmetadataonly",
                        {
                            "title": "Metadata refreshed",
                            "visibility": "unlisted",
                            "video_count": 2,
                            "has_video_count": True,
                            "youtube_updated_text": "Updated yesterday",
                            "youtube_updated_date": "2026-08-26",
                        },
                    )
                playlist = conn.execute(
                    """
                    SELECT title, visibility, video_count, youtube_updated_date,
                           metadata_checked_at, last_changed_at
                    FROM playlists
                    WHERE playlist_id = 'PLmetadataonly'
                    """
                ).fetchone()
                items = conn.execute(
                    """
                    SELECT position, video_id
                    FROM playlist_items
                    WHERE playlist_id = 'PLmetadataonly'
                    """
                ).fetchall()
                scan = conn.execute(
                    """
                    SELECT scanned_at, video_count, scan_status
                    FROM playlist_scans
                    WHERE playlist_id = 'PLmetadataonly'
                    """
                ).fetchone()
                queue_count = core.worker_queue_count(conn)
            finally:
                conn.close()

        self.assertTrue(saved)
        self.assertEqual(playlist["title"], "Metadata refreshed")
        self.assertEqual(playlist["visibility"], "unlisted")
        self.assertEqual(playlist["video_count"], 2)
        self.assertEqual(playlist["youtube_updated_date"], "2026-08-26")
        self.assertTrue(playlist["metadata_checked_at"])
        self.assertEqual(playlist["last_changed_at"], "2026-08-20T12:34:56Z")
        self.assertEqual([tuple(row) for row in items], [(1, "member00001")])
        self.assertEqual(
            dict(scan),
            {
                "scanned_at": "2026-08-20T12:34:56Z",
                "video_count": 1,
                "scan_status": "ok",
            },
        )
        self.assertEqual(queue_count, 0)

    def test_all_playlist_metadata_queue_uses_metadata_only_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.executemany(
                        """
                        INSERT INTO playlists(playlist_id, title, fetch_status)
                        VALUES (?, ?, ?)
                        """,
                        [
                            ("PLmetadata1", "First", "ok"),
                            ("PLmetadata2", "Second", "ok"),
                            ("PLremoved", "Removed", "removed"),
                        ],
                    )
                    stats = core.enqueue_all_playlist_metadata_scan_items(conn)
                rows = conn.execute(
                    """
                    SELECT playlist_id, worker_type, task_type, manual, payload_json
                    FROM worker_queue
                    ORDER BY priority, queue_id
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(
            stats,
            {
                "mode": "metadata_only",
                "selected": 2,
                "cleared": 0,
                "inserted": 2,
                "queued": 2,
            },
        )
        self.assertEqual(
            [
                (
                    row["playlist_id"],
                    row["worker_type"],
                    row["task_type"],
                    row["manual"],
                    json.loads(row["payload_json"]),
                )
                for row in rows
            ],
            [
                ("PLmetadata1", "playlist", "scan", 1, {"metadata_only": True}),
                ("PLmetadata2", "playlist", "scan", 1, {"metadata_only": True}),
            ],
        )

    def test_playlist_last_changed_advances_only_after_a_detected_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                first_video = {
                    "video_id": "changevideo1",
                    "title": "First video",
                    "is_playable": 1,
                    "availability": "public",
                }
                second_video = {
                    "video_id": "changevideo2",
                    "title": "Second video",
                    "is_playable": 1,
                    "availability": "public",
                }

                def save_at(
                    observed_at: str,
                    videos: list[dict[str, object]],
                    *,
                    title: str = "Tracked playlist",
                ) -> None:
                    with patch("yt_library.core.utc_now", return_value=observed_at):
                        with conn:
                            core.save_playlist_scan(
                                conn,
                                "PLchanges",
                                videos,
                                "ok",
                                "",
                                playlist_metadata={
                                    "title": title,
                                    "video_count": len(videos),
                                },
                            )

                save_at("2026-08-01T01:00:00Z", [first_video])
                self.assertIsNone(
                    conn.execute(
                        "SELECT last_changed_at FROM playlists WHERE playlist_id = 'PLchanges'"
                    ).fetchone()["last_changed_at"]
                )

                save_at("2026-08-01T02:00:00Z", [first_video])
                self.assertIsNone(
                    conn.execute(
                        "SELECT last_changed_at FROM playlists WHERE playlist_id = 'PLchanges'"
                    ).fetchone()["last_changed_at"]
                )

                save_at(
                    "2026-08-01T03:00:00Z",
                    [first_video, second_video],
                )
                changed_at = conn.execute(
                    "SELECT last_changed_at FROM playlists WHERE playlist_id = 'PLchanges'"
                ).fetchone()["last_changed_at"]
                self.assertEqual(changed_at, "2026-08-01T03:00:00Z")

                save_at(
                    "2026-08-01T04:00:00Z",
                    [first_video, second_video],
                )
                unchanged_at = conn.execute(
                    "SELECT last_changed_at FROM playlists WHERE playlist_id = 'PLchanges'"
                ).fetchone()["last_changed_at"]
                self.assertEqual(unchanged_at, "2026-08-01T03:00:00Z")

                save_at(
                    "2026-08-01T05:00:00Z",
                    [first_video, second_video],
                    title="Renamed playlist",
                )
                renamed_at = conn.execute(
                    "SELECT last_changed_at FROM playlists WHERE playlist_id = 'PLchanges'"
                ).fetchone()["last_changed_at"]
                self.assertEqual(renamed_at, "2026-08-01T05:00:00Z")

                with patch(
                    "yt_library.core.utc_now",
                    return_value="2026-08-02T01:00:00Z",
                ):
                    with conn:
                        core.save_playlist_scan_error(
                            conn,
                            "PLfailedbaseline",
                            "Temporary failure",
                        )
                with patch(
                    "yt_library.core.utc_now",
                    return_value="2026-08-02T02:00:00Z",
                ):
                    with conn:
                        core.save_playlist_scan(
                            conn,
                            "PLfailedbaseline",
                            [first_video],
                            "ok",
                            "",
                            playlist_metadata={
                                "title": "First successful observation",
                                "video_count": 1,
                            },
                        )
                self.assertIsNone(
                    conn.execute(
                        """
                        SELECT last_changed_at
                        FROM playlists
                        WHERE playlist_id = 'PLfailedbaseline'
                        """
                    ).fetchone()["last_changed_at"]
                )
            finally:
                conn.close()

    def test_playlist_scan_preserves_duplicate_video_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                duplicate_video = {
                    "playlist_id": "PLduplicates",
                    "video_id": "duplicate01",
                    "title": "Repeated video",
                    "channel_id": "",
                    "channel": "",
                    "duration_text": "1:00",
                    "is_playable": 1,
                    "availability": "public",
                    "url": "https://www.youtube.com/watch?v=duplicate01",
                }
                with conn:
                    video_count, unavailable_count = core.save_playlist_scan(
                        conn,
                        "PLduplicates",
                        [
                            {**duplicate_video, "position": 4},
                            {**duplicate_video, "position": 9},
                        ],
                        "ok",
                        "",
                        playlist_metadata={"title": "Duplicates", "video_count": 2},
                    )
                    queue_result = core.enqueue_playlist_metadata_targets(
                        conn,
                        "PLduplicates",
                    )
                items = conn.execute(
                    """
                    SELECT position, video_id
                    FROM playlist_items
                    WHERE playlist_id = 'PLduplicates'
                    ORDER BY position
                    """
                ).fetchall()
                scan = conn.execute(
                    "SELECT video_count FROM playlist_scans WHERE playlist_id = 'PLduplicates'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(video_count, 2)
        self.assertEqual(unavailable_count, 0)
        self.assertEqual(
            [(row["position"], row["video_id"]) for row in items],
            [(1, "duplicate01"), (2, "duplicate01")],
        )
        self.assertEqual(scan["video_count"], 2)
        self.assertEqual(queue_result["queued_count"], "1")
        self.assertEqual(
            core.playlist_duplicate_counts([duplicate_video, duplicate_video]),
            (1, 1),
        )

    def test_playlist_occurrence_identity_uses_position_and_video_id(self) -> None:
        videos: list[dict[str, object]] = []
        positions: set[int] = set()
        entries: set[tuple[int, str]] = set()
        first = {"position": 1, "video_id": "duplicate01"}

        self.assertTrue(core.append_playlist_occurrence(videos, positions, entries, first))
        self.assertFalse(core.append_playlist_occurrence(videos, positions, entries, first))
        self.assertTrue(
            core.append_playlist_occurrence(
                videos,
                positions,
                entries,
                {"position": 2, "video_id": "duplicate01"},
            )
        )
        self.assertEqual([video["position"] for video in videos], [1, 2])

    def test_metadata_indifferent_clears_a_known_reaction_while_missing_state_preserves_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "reaction001",
                        title="Reaction test",
                        reaction="LIKE",
                        source="metadata",
                    )
                    core.upsert_video(
                        conn,
                        "reaction001",
                        reaction="",
                        source="metadata",
                    )
                preserved = conn.execute(
                    "SELECT reaction FROM videos WHERE video_id = 'reaction001'"
                ).fetchone()[0]

                with conn:
                    core.upsert_video(
                        conn,
                        "reaction001",
                        reaction="INDIFFERENT",
                        source="metadata",
                    )
                cleared = conn.execute(
                    "SELECT reaction FROM videos WHERE video_id = 'reaction001'"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(preserved, "LIKE")
        self.assertEqual(cleared, "INDIFFERENT")

    def test_liked_video_sync_replaces_likes_without_creating_playlist_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(conn, "oldliked123", title="Old like", source="metadata")
                    core.upsert_video(conn, "disliked1234", title="Disliked", source="metadata")
                    conn.execute("UPDATE videos SET reaction = 'LIKE' WHERE video_id = 'oldliked123'")
                    conn.execute("UPDATE videos SET reaction = 'DISLIKE' WHERE video_id = 'disliked1234'")
                    count, unavailable = core.save_liked_video_reactions(
                        conn,
                        [
                            {
                                "video_id": "newliked123",
                                "title": "New like",
                                "channel_id": "UC_liked",
                                "channel": "Liked Channel",
                                "is_playable": True,
                            },
                            {
                                "video_id": "newliked123",
                                "title": "Duplicate",
                                "is_playable": True,
                            },
                        ],
                    )
                reactions = {
                    row["video_id"]: row["reaction"]
                    for row in conn.execute("SELECT video_id, reaction FROM videos")
                }
                self.assertEqual(count, 1)
                self.assertEqual(unavailable, 0)
                self.assertEqual(reactions["oldliked123"], "")
                self.assertEqual(reactions["newliked123"], "LIKE")
                self.assertEqual(reactions["disliked1234"], "DISLIKE")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM playlists").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM playlist_items").fetchone()[0], 0)

                with conn:
                    core.save_liked_video_reactions(
                        conn,
                        [{"video_id": "partial12345", "title": "Partial like", "is_playable": True}],
                        replace=False,
                    )
                merged_reactions = {
                    row["video_id"]: row["reaction"]
                    for row in conn.execute("SELECT video_id, reaction FROM videos WHERE reaction <> ''")
                }
                self.assertEqual(merged_reactions["newliked123"], "LIKE")
                self.assertEqual(merged_reactions["partial12345"], "LIKE")
                self.assertEqual(merged_reactions["disliked1234"], "DISLIKE")
            finally:
                conn.close()

    def test_liked_video_sync_counts_only_canonically_unavailable_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                videos = [
                    {
                        "video_id": "publiclike1",
                        "title": "Public like",
                        "availability": "public",
                        "is_playable": True,
                    },
                    {
                        "video_id": "unlistedlk1",
                        "title": "Unlisted like",
                        "availability": "unlisted",
                        "is_playable": True,
                    },
                    {
                        "video_id": "memberslike",
                        "title": "Members-only like",
                        "availability": "subscriber_only",
                        "is_playable": False,
                    },
                    {
                        "video_id": "unavailabl1",
                        "title": "Unavailable like",
                        "availability": "unavailable",
                        "is_playable": False,
                    },
                    {
                        "video_id": "",
                        "title": "Missing ID",
                        "availability": "unavailable",
                        "is_playable": False,
                    },
                    {
                        "video_id": "unavailabl1",
                        "title": "Duplicate unavailable like",
                        "availability": "unavailable",
                        "is_playable": False,
                    },
                ]

                with conn:
                    video_count, unavailable_count = core.save_liked_video_reactions(
                        conn,
                        videos,
                    )
                reactions = {
                    row["video_id"]: row["reaction"]
                    for row in conn.execute(
                        "SELECT video_id, reaction FROM videos ORDER BY video_id"
                    )
                }
            finally:
                conn.close()

        self.assertEqual(video_count, 4)
        self.assertEqual(unavailable_count, 2)
        self.assertEqual(
            reactions,
            {
                "memberslike": "LIKE",
                "publiclike1": "LIKE",
                "unavailabl1": "LIKE",
                "unlistedlk1": "LIKE",
            },
        )

    def test_playlist_queue_rebuild_includes_liked_video_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLregular', 'Regular')")
                    stats = core.rebuild_playlist_scan_queue(conn, force=True)
                rows = core.playlist_scan_queue_rows(conn)
                self.assertEqual(stats["inserted"], 2)
                self.assertEqual([row["playlist_id"] for row in rows], ["LL", "PLregular"])
                self.assertEqual(rows[0]["title"], "Liked videos")
                with conn:
                    core.clear_playlist_scan_queue(conn)
                    core.enqueue_playlist_scan_item(conn, "LL", title="LL", manual=True)
                self.assertEqual(core.playlist_scan_queue_rows(conn)[0]["title"], "Liked videos")
            finally:
                conn.close()

    def test_playlist_queue_rebuild_can_prepend_live_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLregular', 'Regular')")
                    stats = core.rebuild_playlist_scan_queue(
                        conn,
                        force=True,
                        discover_current=True,
                    )
                rows = core.playlist_scan_queue_rows(conn)
                self.assertEqual(stats["inserted"], 3)
                self.assertEqual(
                    [(row["task_type"], row["playlist_id"]) for row in rows],
                    [("discover", ""), ("scan", "LL"), ("scan", "PLregular")],
                )
            finally:
                conn.close()

    def test_save_playlist_scan_error_preserves_existing_counts(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
                try:
                    with conn:
                        conn.execute(
                            "INSERT INTO playlists(playlist_id, title) VALUES ('PLpartial', 'Partial scan')"
                        )
                        core.save_playlist_scan(
                            conn,
                            "PLpartial",
                            [
                                {
                                    "playlist_id": "PLpartial",
                                    "position": 1,
                                    "video_id": "abc12345678",
                                    "title": "Video",
                                    "channel_id": "",
                                    "channel": "",
                                    "duration_text": "1:00",
                                    "is_playable": 1,
                                    "availability": "LIVE",
                                    "url": "https://www.youtube.com/watch?v=abc12345678",
                                }
                            ],
                            "ok",
                            "",
                        )
                        core.save_playlist_scan_error(conn, "PLpartial", "Parsed 1 videos, but playlist metadata says 2 videos")
                    row = conn.execute(
                        "SELECT video_count, unavailable_count, scan_status, scan_error FROM playlist_scans WHERE playlist_id = 'PLpartial'"
                    ).fetchone()
                    self.assertEqual(row["video_count"], 1)
                    self.assertEqual(row["unavailable_count"], 0)
                    self.assertEqual(row["scan_status"], "error")
                    self.assertIn("metadata says 2", row["scan_error"])
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id = 'PLpartial'").fetchone()[0],
                        1,
                    )
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root

    def test_owned_missing_playlist_is_tombstoned_and_orphan_videos_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, visibility, ownership, in_library
                        )
                        VALUES ('PLmissing', 'Missing', 'private', 'mine', 1)
                        """
                    )
                    core.upsert_video(conn, "keptvideo01", title="Kept video", source="playlist")
                    core.upsert_video(conn, "dropvideo01", title="Drop video", source="playlist")
                    conn.execute(
                        """
                        INSERT INTO history_events(
                          event_id, video_id, watch_date, time_precision, source_type, match_type
                        ) VALUES ('kept-watch', 'keptvideo01', '2026-07-28', 'date_only', 'youtube', 'video_id_date')
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_items(
                          playlist_id, position, video_id, membership_state,
                          source_quality, match_type
                        )
                        VALUES
                          ('PLmissing', 1, 'keptvideo01', 'retained_unavailable',
                           'takeout', 'ambiguous_hidden_candidate'),
                          ('PLmissing', 2, 'dropvideo01', 'current', 'youtube', 'video_id')
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_scans(
                          playlist_id, scanned_at, video_count, unavailable_count, scan_status
                        ) VALUES ('PLmissing', '2026-07-28T00:00:00Z', 2, 0, 'ok')
                        """
                    )
                    self.assertEqual(
                        core.playlist_missing_status(conn, "PLmissing"),
                        "removed",
                    )
                    counts = core.save_playlist_missing_status(
                        conn,
                        "PLmissing",
                        "removed",
                        "authenticated YouTube 404",
                    )
                self.assertEqual(counts, (2, 0))
                playlist = conn.execute(
                    "SELECT 1 FROM playlists WHERE playlist_id = 'PLmissing'"
                ).fetchone()
                self.assertIsNone(playlist)
                tombstone = conn.execute(
                    """
                    SELECT reason
                    FROM playlist_tombstones
                    WHERE playlist_id = 'PLmissing'
                    """
                ).fetchone()
                self.assertEqual(tombstone["reason"], "authenticated_missing")
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM playlist_items WHERE playlist_id = 'PLmissing'"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT title FROM videos WHERE video_id = 'keptvideo01'"
                    ).fetchone()[0],
                    "Kept video",
                )
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM videos WHERE video_id = 'dropvideo01'"
                    ).fetchone()
                )
                self.assertEqual(core.playlist_scan_candidate_rows(conn), [])
                self.assertEqual(core.playlist_scan_candidate_rows(conn, force=True), [])
            finally:
                conn.close()

    def test_playlist_missing_status_uses_unavailable_without_ownership_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(playlist_id, title, owner_channel_id)
                        VALUES ('PLforeign', 'Foreign', NULL)
                        """
                    )
                self.assertEqual(
                    core.playlist_missing_status(conn, "PLforeign"),
                    "unavailable",
                )
            finally:
                conn.close()

    def test_missing_foreign_playlist_is_verified_before_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_channel(conn, "UCother", title="Other owner")
                    conn.execute(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, owner_channel_id, ownership, in_library
                        ) VALUES ('PLforeign', 'Foreign', 'UCother', 'others', 1)
                        """
                    )
                    core.upsert_video(
                        conn,
                        "keptforeign1",
                        title="Watched foreign video",
                        source="playlist",
                    )
                    core.upsert_video(
                        conn,
                        "dropforeign1",
                        title="Unwatched foreign video",
                        source="playlist",
                    )
                    conn.execute(
                        """
                        INSERT INTO history_events(
                          event_id, video_id, watch_date, time_precision,
                          source_type, match_type
                        ) VALUES (
                          'foreign-watch', 'keptforeign1', '2026-07-28',
                          'date_only', 'youtube', 'video_id_date'
                        )
                        """
                    )
                    conn.executemany(
                        """
                        INSERT INTO playlist_items(playlist_id, position, video_id)
                        VALUES ('PLforeign', ?, ?)
                        """,
                        [(1, "keptforeign1"), (2, "dropforeign1")],
                    )
                    reconciliation = core.reconcile_missing_library_playlists(conn, set())

                self.assertEqual(reconciliation["verify_ids"], ["PLforeign"])
                pending = conn.execute(
                    """
                    SELECT ownership, in_library, library_missing_at, fetch_status
                    FROM playlists
                    WHERE playlist_id = 'PLforeign'
                    """
                ).fetchone()
                self.assertEqual(pending["ownership"], "others")
                self.assertEqual(pending["in_library"], 0)
                self.assertIsNotNone(pending["library_missing_at"])
                self.assertNotEqual(pending["fetch_status"], "unavailable")

                with conn:
                    result = core.save_playlist_scan(
                        conn,
                        "PLforeign",
                        [{"video_id": "liveforeign1", "is_playable": True}],
                        "ok",
                        "",
                        playlist_metadata={
                            "title": "Foreign",
                            "owner": "Other owner",
                            "owner_channel_id": "UCother",
                            "video_count": 1,
                        },
                    )
                self.assertEqual(result, (0, 0))
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM playlists WHERE playlist_id = 'PLforeign'"
                    ).fetchone()
                )
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM playlist_tombstones WHERE playlist_id = 'PLforeign'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT 1 FROM videos WHERE video_id = 'keptforeign1'"
                    ).fetchone()
                )
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM videos WHERE video_id = 'dropforeign1'"
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_inaccessible_foreign_playlist_is_retained_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, ownership, in_library
                        ) VALUES ('PLforeign', 'Foreign', 'others', 1)
                        """
                    )
                    core.upsert_video(conn, "foreignvideo1", source="playlist")
                    conn.execute(
                        """
                        INSERT INTO playlist_items(playlist_id, position, video_id)
                        VALUES ('PLforeign', 1, 'foreignvideo1')
                        """
                    )
                    core.reconcile_missing_library_playlists(conn, set())
                    status = core.playlist_missing_status(conn, "PLforeign")
                    core.save_playlist_missing_status(
                        conn,
                        "PLforeign",
                        status,
                        "authenticated YouTube 404",
                    )
                playlist = conn.execute(
                    """
                    SELECT ownership, in_library, library_missing_at, fetch_status
                    FROM playlists
                    WHERE playlist_id = 'PLforeign'
                    """
                ).fetchone()
                self.assertEqual(status, "unavailable")
                self.assertEqual(playlist["ownership"], "others")
                self.assertEqual(playlist["in_library"], 0)
                self.assertIsNone(playlist["library_missing_at"])
                self.assertEqual(playlist["fetch_status"], "unavailable")
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM playlist_items WHERE playlist_id = 'PLforeign'"
                    ).fetchone()[0],
                    1,
                )
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM playlist_tombstones WHERE playlist_id = 'PLforeign'"
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_missing_unknown_playlist_is_retained_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, ownership, in_library
                        ) VALUES ('PLunknown', 'Unknown', 'unknown', 1)
                        """
                    )
                    reconciliation = core.reconcile_missing_library_playlists(conn, set())
                playlist = conn.execute(
                    """
                    SELECT in_library, library_missing_at, fetch_status
                    FROM playlists
                    WHERE playlist_id = 'PLunknown'
                    """
                ).fetchone()
                self.assertEqual(reconciliation["unavailable"], 1)
                self.assertEqual(reconciliation["verify_ids"], [])
                self.assertEqual(playlist["in_library"], 0)
                self.assertIsNone(playlist["library_missing_at"])
                self.assertEqual(playlist["fetch_status"], "unavailable")
            finally:
                conn.close()

    def test_playlist_missing_status_uses_removed_for_library_playlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, visibility, ownership, in_library
                        )
                        VALUES ('PLlibrary', 'Library', 'private', 'mine', 1)
                        """
                    )
                self.assertEqual(
                    core.playlist_missing_status(conn, "PLlibrary"),
                    "removed",
                )
            finally:
                conn.close()

    def test_missing_owned_playlist_is_tombstoned_from_complete_library_feed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, ownership, in_library
                        ) VALUES ('PLmine', 'Mine', 'mine', 1)
                        """
                    )
                    reconciliation = core.reconcile_missing_library_playlists(conn, set())
                self.assertEqual(reconciliation["tombstoned"], 1)
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM playlists WHERE playlist_id = 'PLmine'"
                    ).fetchone()
                )
                tombstone = conn.execute(
                    """
                    SELECT reason FROM playlist_tombstones
                    WHERE playlist_id = 'PLmine'
                    """
                ).fetchone()
                self.assertEqual(tombstone["reason"], "missing_from_library")
            finally:
                conn.close()

    def test_recovered_live_video_keeps_youtube_availability_unknown(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            core.ROOT = Path(temp_dir)
            try:
                conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
                try:
                    with conn:
                        conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('pl1', 'Playlist')")
                        core.save_video_recovery(
                            conn,
                            "KRhofr57Na8",
                            {"title": "Can You Safely Drink Your Own Pee?", "status": "LIVE"},
                            "found",
                            "",
                        )
                        conn.execute(
                            """
                            INSERT INTO playlist_items(
                              playlist_id, position, video_id, membership_state, source_quality, match_type
                            ) VALUES ('pl1', 1, 'KRhofr57Na8', 'retained_unavailable', 'takeout', 'ambiguous_hidden_candidate')
                            """
                        )

                    row = conn.execute(
                        """
                        SELECT v.is_playable, v.availability, vr.archivarix_status
                        FROM videos v
                        JOIN video_recovery vr ON vr.video_id = v.video_id
                        WHERE v.video_id = 'KRhofr57Na8'
                        """
                    ).fetchone()
                    self.assertIsNotNone(row)
                    self.assertIsNone(row["is_playable"])
                    self.assertEqual(row["availability"], "unknown")
                    self.assertEqual(row["archivarix_status"], "LIVE")
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root


if __name__ == "__main__":
    unittest.main()
