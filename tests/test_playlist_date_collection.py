from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.support import migrated_connection
from yt_library import core
from yt_library.config import configured_youtube_data_api_enabled
from yt_library.playlist_dates import DateCollectionStopped, api_playlist_dates, apply_added_dates, collect_playlist_dates, match_music_dates, valid_added_date
from yt_library.queries import video_collection_data


class PlaylistDateCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = migrated_connection(Path(self.temp.name) / "test.sqlite3")
        self.addCleanup(self.conn.close)
        with self.conn:
            core.save_playlist_scan(self.conn, "PLtest", [{"video_id": "one"}, {"video_id": "two"}], "ok", "")

    def test_date_validation_preserves_precision(self):
        self.assertEqual(valid_added_date("2025-01-01"), "2025-01-01")
        self.assertEqual(valid_added_date("2025-01-01T01:00:00-08:00"), "2025-01-01T09:00:00Z")
        for value in (None, "", "invalid", "1900-01-01", "2999-01-01"):
            self.assertEqual(valid_added_date(value), "")

    def test_enrichment_never_overwrites_date_or_changes_membership(self):
        before = list(self.conn.execute("SELECT rowid, video_id, position, first_seen_at FROM playlist_items"))
        first = {"video_id": "one", "added_at": "2025-01-01", "added_at_source": "takeout"}
        self.assertEqual(apply_added_dates(self.conn, "PLtest", [first]), 1)
        self.assertEqual(apply_added_dates(self.conn, "PLtest", [dict(first, added_at="2025-02-01", added_at_source="youtube_api")]), 0)
        self.assertEqual(list(self.conn.execute("SELECT rowid, video_id, position, first_seen_at FROM playlist_items")), before)
        self.assertEqual(tuple(self.conn.execute("SELECT added_at, added_at_source FROM playlist_items WHERE video_id='one'").fetchone()), ("2025-01-01", "takeout"))

    def test_per_item_fallback_and_cooldown(self):
        api = [{"video_id": "one", "added_at": "2025-01-01", "added_at_source": "youtube_api"}]
        web = [{"video_id": "two", "added_at": "2025-02-01", "added_at_source": "music_web"}]
        with patch("yt_library.playlist_dates.api_playlist_dates", return_value=api) as a, patch("yt_library.playlist_dates.web_playlist_dates", return_value=web) as w:
            self.assertEqual(collect_playlist_dates(self.conn, {"youtube_data_api_enabled": True}, "PLtest"), 2)
            self.assertEqual(collect_playlist_dates(self.conn, {"youtube_data_api_enabled": True}, "PLtest"), 0)
            a.assert_called_once()
            w.assert_called_once()

    def test_web_evidence_retains_occurrence_identity_and_rejects_conflict(self):
        evidence = {"video_id": "one", "set_video_id": "a", "added_at": "2025-01-01", "added_at_source": "music_web"}
        self.assertEqual(apply_added_dates(self.conn, "PLtest", [evidence]), 1)
        self.assertEqual(self.conn.execute("SELECT set_video_id FROM playlist_items WHERE video_id='one'").fetchone()[0], "a")
        with self.conn:
            self.conn.execute("UPDATE playlist_items SET set_video_id='b' WHERE video_id='two'")
        self.assertEqual(apply_added_dates(self.conn, "PLtest", [dict(evidence, video_id="two")]), 0)
        self.assertIsNone(self.conn.execute("SELECT added_at FROM playlist_items WHERE video_id='two'").fetchone()[0])

    def test_occurrence_enrichment_preserves_known_dates_sources_and_rows(self):
        with self.conn:
            self.conn.execute("UPDATE playlist_items SET added_at='2025-01-01', added_at_source='takeout', first_seen_at='2025-02-01T00:00:00Z' WHERE video_id='one'")
        before = tuple(self.conn.execute("SELECT rowid,position,video_id,added_at,added_at_source,first_seen_at FROM playlist_items WHERE video_id='one'").fetchone())
        evidence = {"video_id": "one", "set_video_id": "090796A75D153932", "added_at": "2025-01-01", "added_at_source": "youtube_api"}
        self.assertEqual(apply_added_dates(self.conn, "PLtest", [evidence]), 1)
        self.assertEqual(tuple(self.conn.execute("SELECT rowid,position,video_id,added_at,added_at_source,first_seen_at FROM playlist_items WHERE video_id='one'").fetchone()), before)
        self.assertEqual(self.conn.execute("SELECT set_video_id FROM playlist_items WHERE video_id='one'").fetchone()[0], evidence["set_video_id"])
        self.assertEqual(apply_added_dates(self.conn, "PLtest", [evidence]), 0)

    def test_conflicting_dates_do_not_attach_an_occurrence_to_possible_readd(self):
        with self.conn:
            self.conn.execute("UPDATE playlist_items SET added_at='2025-01-01', added_at_source='takeout' WHERE video_id='one'")
        self.assertEqual(apply_added_dates(self.conn, "PLtest", [{"video_id": "one", "set_video_id": "090796A75D153932", "added_at": "2025-02-01", "added_at_source": "youtube_api"}]), 0)
        self.assertEqual(self.conn.execute("SELECT set_video_id FROM playlist_items WHERE video_id='one'").fetchone()[0], "")

    def test_ambiguous_or_foreign_occurrences_never_attach_by_position(self):
        evidence = {"video_id": "one", "set_video_id": "090796A75D153932", "added_at": "2025-01-01", "added_at_source": "youtube_api", "position": 1}
        self.assertEqual(apply_added_dates(self.conn, "PLtest", [evidence, dict(evidence, video_id="two")]), 0)
        with self.conn:
            self.conn.execute("UPDATE playlist_items SET video_id='one' WHERE video_id='two'")
        self.assertEqual(apply_added_dates(self.conn, "PLtest", [evidence]), 0)
        with self.conn:
            self.conn.execute("UPDATE playlist_items SET set_video_id=? WHERE position=1", (evidence["set_video_id"],))
        self.assertEqual(apply_added_dates(self.conn, "PLtest", [dict(evidence, video_id="not-one")]), 0)
        self.assertEqual(apply_added_dates(self.conn, "PLtest", [evidence]), 1)
        self.assertIsNone(self.conn.execute("SELECT added_at FROM playlist_items WHERE position=2").fetchone()[0])
        with self.conn:
            self.conn.execute("UPDATE playlist_items SET set_video_id=? WHERE position=2", (evidence["set_video_id"],))
        self.assertEqual(apply_added_dates(self.conn, "PLtest", [evidence]), 0)

    def test_api_stages_all_pages_with_guarded_ids_before_matching(self):
        service = Mock()
        encoded = base64.urlsafe_b64encode(b"PLtest.090796A75D153932").decode()
        pages = [{"items": [{"id": encoded, "snippet": {"resourceId": {"videoId": "one"}, "publishedAt": "2025-01-01"}}], "nextPageToken": "second"},
                 {"items": [{"id": "unknown-format", "snippet": {"resourceId": {"videoId": "two"}, "publishedAt": "2025-02-01"}}]}]
        service.playlistItems.return_value.list.return_value.execute.side_effect = pages
        with patch("yt_library.playlist_dates.build_youtube_data_service", return_value=service), patch("yt_library.playlist_dates.pace_outbound_request") as pace:
            evidence = api_playlist_dates({"youtube_oauth_client_secrets": "client.json", "youtube_oauth_token": "token.json"}, "PLtest")
        self.assertEqual([row["set_video_id"] for row in evidence], ["090796A75D153932", ""])
        self.assertEqual(pace.call_count, 2)
        self.assertEqual(service.playlistItems.return_value.list.call_args.kwargs["pageToken"], "second")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM playlist_items WHERE added_at IS NOT NULL OR set_video_id!=''").fetchone()[0], 0)
        self.assertEqual(apply_added_dates(self.conn, "PLtest", evidence), 2)
        self.assertEqual(self.conn.execute("SELECT added_at FROM playlist_items WHERE video_id='two'").fetchone()[0], "2025-02-01")

    def test_api_disabled_by_default_and_failures_preserve_dates(self):
        self.assertFalse(configured_youtube_data_api_enabled({}))
        self.assertFalse(configured_youtube_data_api_enabled({"youtube_data_api_enabled": "false"}))
        with patch("yt_library.playlist_dates.api_playlist_dates") as api, patch("yt_library.playlist_dates.web_playlist_dates", side_effect=ValueError("private failure")) as web:
            self.assertEqual(collect_playlist_dates(self.conn, {}, "PLtest"), 0)
            self.assertEqual(collect_playlist_dates(self.conn, {}, "PLtest"), 0)
            api.assert_not_called()
            web.assert_called_once()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM playlist_items WHERE first_seen_at IS NOT NULL").fetchone()[0], 0)

    def test_music_attempt_does_not_cool_down_newly_enabled_api(self):
        with patch("yt_library.playlist_dates.web_playlist_dates", return_value=[]):
            collect_playlist_dates(self.conn, {}, "PLtest")
        scan = self.conn.execute("SELECT * FROM playlist_scans").fetchone()
        self.assertIsNone(scan["api_date_checked_at"])
        self.assertTrue(scan["music_date_checked_at"])
        evidence = [{"video_id": "one", "added_at": "2025-01-01", "added_at_source": "youtube_api"}]
        with patch("yt_library.playlist_dates.api_playlist_dates", return_value=evidence) as api, patch("yt_library.playlist_dates.web_playlist_dates") as web:
            self.assertEqual(collect_playlist_dates(self.conn, {"youtube_data_api_enabled": True}, "PLtest", run_id="enabled-api"), 1)
            api.assert_called_once()
            web.assert_not_called()
        logs = [r[0] for r in self.conn.execute("SELECT message FROM playlist_scan_worker_log WHERE run_id='enabled-api'")]
        self.assertTrue(any("enriched from youtube_api" in message for message in logs))
        self.assertTrue(any("skipped music_web (24-hour cooldown; next attempt " in message and "1 memberships still missing dates" in message for message in logs))

    def test_api_cooldown_does_not_block_eligible_music(self):
        with self.conn:
            self.conn.execute("UPDATE playlist_scans SET api_date_checked_at=?", (core.utc_now(),))
        with patch("yt_library.playlist_dates.api_playlist_dates") as api, patch("yt_library.playlist_dates.web_playlist_dates", return_value=[]) as web:
            collect_playlist_dates(self.conn, {"youtube_data_api_enabled": True}, "PLtest")
            api.assert_not_called()
            web.assert_called_once()
        messages = [r[0] for r in self.conn.execute("SELECT message FROM playlist_scan_worker_log")]
        self.assertTrue(any("skipped youtube_api (24-hour cooldown" in message for message in messages))

    def test_manual_scan_bypasses_both_cooldowns_and_disabled_api_stays_off(self):
        with self.conn:
            self.conn.execute("UPDATE playlist_scans SET api_date_checked_at=?, music_date_checked_at=?", (core.utc_now(), core.utc_now()))
        with patch("yt_library.playlist_dates.api_playlist_dates", return_value=[]) as api, patch("yt_library.playlist_dates.web_playlist_dates", return_value=[]) as web:
            collect_playlist_dates(self.conn, {"youtube_data_api_enabled": True}, "PLtest", manual=True, run_id="manual")
            api.assert_called_once()
            web.assert_called_once()
            collect_playlist_dates(self.conn, {}, "PLtest", manual=True)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(web.call_count, 2)
        messages = [r[0] for r in self.conn.execute("SELECT message FROM playlist_scan_worker_log WHERE run_id='manual'")]
        self.assertTrue(any("youtube_api cooldown bypassed for manual scan" in message for message in messages))
        self.assertTrue(any("music_web cooldown bypassed for manual scan" in message for message in messages))
        with patch("yt_library.playlist_dates.api_playlist_dates") as api, patch("yt_library.playlist_dates.web_playlist_dates") as web:
            collect_playlist_dates(self.conn, {"youtube_data_api_enabled": True}, "PLtest", run_id="automatic")
            api.assert_not_called()
            web.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM playlist_scan_worker_log WHERE run_id='automatic' AND message LIKE '%24-hour cooldown%'").fetchone()[0], 2)

    def test_expired_source_cooldowns_retry(self):
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with self.conn:
            self.conn.execute("UPDATE playlist_scans SET api_date_checked_at=?, music_date_checked_at=?", (old, old))
        with patch("yt_library.playlist_dates.api_playlist_dates", return_value=[]) as api, patch("yt_library.playlist_dates.web_playlist_dates", return_value=[]) as web:
            collect_playlist_dates(self.conn, {"youtube_data_api_enabled": True}, "PLtest")
            api.assert_called_once()
            web.assert_called_once()

    def test_cancellation_clears_only_interrupted_source_timer(self):
        evidence = [{"video_id": "one", "added_at": "2025-01-01", "added_at_source": "youtube_api"}]
        with patch("yt_library.playlist_dates.api_playlist_dates", return_value=evidence), patch("yt_library.playlist_dates.web_playlist_dates", side_effect=DateCollectionStopped):
            self.assertEqual(collect_playlist_dates(self.conn, {"youtube_data_api_enabled": True}, "PLtest"), 1)
        scan = self.conn.execute("SELECT * FROM playlist_scans").fetchone()
        self.assertTrue(scan["api_date_checked_at"])
        self.assertIsNone(scan["music_date_checked_at"])
        self.assertEqual(scan["date_checked_at"], scan["api_date_checked_at"])
        with patch("yt_library.playlist_dates.api_playlist_dates") as api, patch("yt_library.playlist_dates.web_playlist_dates", return_value=[]) as web:
            collect_playlist_dates(self.conn, {"youtube_data_api_enabled": True}, "PLtest")
            api.assert_not_called()
            web.assert_called_once()

    def test_new_membership_resets_both_timers_but_unchanged_scan_does_not(self):
        stamp = core.utc_now()
        with self.conn:
            self.conn.execute("UPDATE playlist_scans SET date_checked_at=?, api_date_checked_at=?, music_date_checked_at=?", (stamp, stamp, stamp))
            core.save_playlist_scan(self.conn, "PLtest", [{"video_id": "one"}, {"video_id": "two"}], "ok", "")
        columns = "date_checked_at, api_date_checked_at, music_date_checked_at"
        self.assertEqual(tuple(self.conn.execute(f"SELECT {columns} FROM playlist_scans").fetchone()), (stamp, stamp, stamp))
        with self.conn:
            core.save_playlist_scan(self.conn, "PLtest", [{"video_id": "one"}, {"video_id": "two"}, {"video_id": "three"}], "ok", "")
        self.assertEqual(tuple(self.conn.execute(f"SELECT {columns} FROM playlist_scans").fetchone()), (None, None, None))

    def test_manual_scan_still_skips_complete_or_failed_memberships(self):
        with self.conn:
            self.conn.execute("UPDATE playlist_scans SET scan_status='error'")
        with patch("yt_library.playlist_dates.api_playlist_dates") as api, patch("yt_library.playlist_dates.web_playlist_dates") as web:
            collect_playlist_dates(self.conn, {"youtube_data_api_enabled": True}, "PLtest", manual=True)
            with self.conn:
                self.conn.execute("UPDATE playlist_scans SET scan_status='ok'")
                self.conn.execute("UPDATE playlist_items SET added_at='2025-01-01'")
            collect_playlist_dates(self.conn, {"youtube_data_api_enabled": True}, "PLtest", manual=True)
            api.assert_not_called()
            web.assert_not_called()

    def test_api_failure_falls_back_and_cancellation_does_not_stamp_members(self):
        web = [{"video_id": "one", "added_at": "2025-01-01", "added_at_source": "music_web"}]
        with patch("yt_library.playlist_dates.api_playlist_dates", side_effect=ValueError("expired")), patch("yt_library.playlist_dates.web_playlist_dates", return_value=web):
            self.assertEqual(collect_playlist_dates(self.conn, {"youtube_data_api_enabled": True}, "PLtest"), 1)
        with self.conn:
            self.conn.execute("UPDATE playlist_scans SET date_checked_at=NULL, api_date_checked_at=NULL, music_date_checked_at=NULL")
        with patch("yt_library.playlist_dates.api_playlist_dates") as api, patch("yt_library.playlist_dates.web_playlist_dates") as web:
            self.assertEqual(collect_playlist_dates(self.conn, {"youtube_data_api_enabled": True}, "PLtest", should_stop=lambda: True), 0)
            api.assert_not_called()
            web.assert_not_called()
        self.assertIsNone(self.conn.execute("SELECT date_checked_at FROM playlist_scans").fetchone()[0])

    def test_music_matches_occurrence_not_substituted_video(self):
        originals = [{"setVideoId": "a", "videoId": "one"}]
        music = [{"playlistItemData": {"playlistSetVideoId": "a", "videoId": "wrong", "voteSortValue": 1735689600}},
                 {"playlistItemData": {"playlistSetVideoId": "missing", "videoId": "two", "voteSortValue": 1735689600}}]
        result = match_music_dates(originals, music)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["video_id"], "one")
        self.assertEqual(result[0]["added_at"], "2025-01-01T00:00:00Z")
        self.assertEqual(match_music_dates(originals * 2, music), [])

    def test_sort_uses_displayed_date_and_unknowns_last(self):
        with self.conn:
            core.save_playlist_scan(self.conn, "PLtest", [{"video_id": "one"}, {"video_id": "two"}, {"video_id": "three"}], "ok", "")
            self.conn.execute("UPDATE playlist_items SET first_seen_at=NULL")
            self.conn.execute("UPDATE playlist_items SET added_at='2025-01-01' WHERE video_id='one'")
            self.conn.execute("UPDATE playlist_items SET first_seen_at='2025-02-01T01:00:00Z' WHERE video_id='two'")
            self.conn.execute("UPDATE videos SET upload_date='2030-01-01' WHERE video_id='three'")
        for sort, order in (("newest_added", ["two", "one", "three"]), ("oldest_added", ["one", "two", "three"])):
            page = video_collection_data(self.conn, playlist_id="PLtest", sort=sort)
            self.assertEqual([row["video_id"] for row in page["results"]], order)
            second = next(row for row in page["results"] if row["video_id"] == "two")
            self.assertEqual(second["first_seen_at"], "2025-02-01T01:00:00Z")

    def test_migration_preserves_membership_dates_without_first_seen_backfill(self):
        for version in range(1, 37):
            with self.subTest(version=version):
                path = Path(self.temp.name) / f"v{version}.sqlite3"
                conn = migrated_connection(path)
                with conn:
                    core.save_playlist_scan(conn, "PLold", [{"video_id": "old"}], "ok", "")
                    conn.execute("UPDATE playlist_items SET added_at='2020-01-01T01:02:03Z'")
                    before_id = conn.execute("SELECT rowid FROM playlist_items").fetchone()[0]
                    for column in ("added_at_source", "first_seen_at", "set_video_id"):
                        conn.execute(f"ALTER TABLE playlist_items DROP COLUMN {column}")
                    for column in ("date_checked_at", "membership_complete", "api_date_checked_at", "music_date_checked_at"):
                        conn.execute(f"ALTER TABLE playlist_scans DROP COLUMN {column}")
                    conn.execute("DELETE FROM schema_migrations")
                    conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, '2020-01-01')", (version,))
                conn.close()
                core.migrate_database(path)
                conn = core.connect(path)
                try:
                    row = conn.execute("SELECT rowid, added_at, first_seen_at, added_at_source FROM playlist_items").fetchone()
                    self.assertEqual(tuple(row), (before_id, "2020-01-01T01:02:03Z", None, "unknown"))
                    self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(tuple(conn.execute("SELECT api_date_checked_at, music_date_checked_at FROM playlist_scans").fetchone()), (None, None))
                finally:
                    conn.close()

    def test_v37_migration_retains_legacy_check_without_guessing_source(self):
        path = Path(self.temp.name) / "v37.sqlite3"
        conn = migrated_connection(path)
        with conn:
            core.save_playlist_scan(conn, "PLold", [{"video_id": "old"}], "ok", "")
            conn.execute("UPDATE playlist_items SET added_at='2020-01-01', added_at_source='youtube_api', set_video_id='occurrence', first_seen_at='2020-01-02T00:00:00Z'")
            conn.execute("UPDATE playlist_scans SET date_checked_at='2025-01-01T00:00:00Z'")
            before = tuple(conn.execute("SELECT rowid,* FROM playlist_items").fetchone())
            conn.execute("ALTER TABLE playlist_scans DROP COLUMN api_date_checked_at")
            conn.execute("ALTER TABLE playlist_scans DROP COLUMN music_date_checked_at")
            conn.execute("DELETE FROM schema_migrations WHERE version>=38")
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES (37,'2025-01-01')")
        conn.close()
        core.migrate_database(path)
        conn = core.connect(path)
        try:
            self.assertEqual(tuple(conn.execute("SELECT rowid,* FROM playlist_items").fetchone()), before)
            self.assertEqual(tuple(conn.execute("SELECT date_checked_at,api_date_checked_at,music_date_checked_at FROM playlist_scans").fetchone()), ("2025-01-01T00:00:00Z", None, None))
            self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 38)
            with conn:
                conn.execute("UPDATE playlist_scans SET api_date_checked_at='2025-02-01T00:00:00Z'")
        finally:
            conn.close()
        core.migrate_database(path)
        conn = core.connect(path)
        try:
            self.assertEqual(conn.execute("SELECT api_date_checked_at FROM playlist_scans").fetchone()[0], "2025-02-01T00:00:00Z")
        finally:
            conn.close()
