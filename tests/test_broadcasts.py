from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from yt_library import core, queries
from yt_library.broadcasts import (
    broadcast_recheck_due,
    broadcast_recheck_interval_hours,
    effective_broadcast_status,
)
from yt_library.config import configured_broadcast_polling, normalize_config
from yt_library.plugins import PluginPlanningContext, _library_videos_by_id
from tests.support import migrated_connection


class BroadcastPollingTests(unittest.TestCase):
    now = datetime(2026, 10, 2, 20, tzinfo=timezone.utc)

    def interval(self, status="live", start=None, watch=None, config=None):
        return broadcast_recheck_interval_hours(
            status, start, watch, now=self.now, config=config or {}
        )

    def test_intervals_and_recent_watch_override(self):
        self.assertEqual(self.interval(start="2026-10-02T12:00:00Z"), 1)
        self.assertEqual(self.interval(start="2026-10-01T20:00:00Z"), 6)
        self.assertEqual(self.interval(start="2026-09-25T20:00:00Z"), 6)
        self.assertEqual(self.interval(start="2020-01-01T00:00:00Z"), 24)
        self.assertEqual(
            self.interval(start="2020-01-01T00:00:00Z", watch="2026-10-01T12:00:00Z"), 1
        )
        self.assertEqual(
            self.interval(start="2020-01-01T00:00:00Z", watch="2026-09-01T12:00:00Z"),
            24,
        )
        self.assertEqual(self.interval("upcoming", "2026-10-03T20:00:00Z"), 1)
        self.assertEqual(self.interval("upcoming", "2026-10-04T20:00:00Z"), 6)
        self.assertEqual(self.interval(), 1)
        self.assertEqual(self.interval(start="bad"), 1)
        self.assertEqual(self.interval(start="2026-11-01T00:00:00Z"), 1)

    def test_date_only_recent_watch_uses_display_timezone(self):
        self.assertEqual(
            self.interval(
                start="2020-01-01T00:00:00Z",
                watch="2026-09-25",
                config={"display_timezone": "America/Los_Angeles"},
            ),
            1,
        )
        self.assertEqual(
            self.interval(
                start="2020-01-01T00:00:00Z",
                watch="2026-09-24",
                config={"display_timezone": "America/Los_Angeles"},
            ),
            24,
        )
        self.assertEqual(
            self.interval(start="2020-01-01T00:00:00Z", watch="2026-11-01"), 24
        )

    def test_due_slots_stagger_without_starvation_or_repeat(self):
        checks = {f"stream-{i}": self.now.isoformat() for i in range(96)}
        daily_counts = []
        for hour in range(1, 25):
            now = self.now + timedelta(hours=hour)
            due = [
                video_id
                for video_id, checked in checks.items()
                if broadcast_recheck_due(
                    video_id,
                    "live",
                    "2020-01-01T00:00:00Z",
                    checked,
                    None,
                    now=now,
                    config={},
                )
            ]
            daily_counts.append(len(due))
            for video_id in due:
                checks[video_id] = now.isoformat()
                self.assertFalse(
                    broadcast_recheck_due(
                        video_id,
                        "live",
                        "2020-01-01T00:00:00Z",
                        checks[video_id],
                        None,
                        now=now,
                        config={},
                    )
                )
        self.assertEqual(sum(daily_counts), 96)
        self.assertLess(max(daily_counts), 20)
        self.assertTrue(
            all(datetime.fromisoformat(value) > self.now for value in checks.values())
        )
        self.assertTrue(
            broadcast_recheck_due(
                "s", "live", None, None, None, now=self.now, config={}
            )
        )
        self.assertFalse(
            broadcast_recheck_due(
                "s", "ended", None, None, None, now=self.now, config={}
            )
        )
        self.assertFalse(
            broadcast_recheck_due(
                "s", "live", None, "2026-11-01T00:00:00Z", None, now=self.now, config={}
            )
        )

    def test_config_normalization_and_overrides(self):
        policy = configured_broadcast_polling(
            {
                "broadcast_polling": {
                    "recent_watch_days": "bad",
                    "normal_hours": 0,
                    "long_running_hours": float("inf"),
                }
            }
        )
        self.assertEqual(policy["recent_watch_days"], 7)
        self.assertEqual(policy["normal_hours"], 1)
        self.assertEqual(policy["long_running_hours"], 24)
        self.assertEqual(
            normalize_config({})["broadcast_polling"], configured_broadcast_polling({})
        )
        self.assertEqual(
            self.interval(
                start="2020-01-01T00:00:00Z",
                config={"broadcast_polling": {"long_running_hours": 48}},
            ),
            48,
        )


class BroadcastAvailabilityTests(unittest.TestCase):
    def test_available_private_and_unobserved_are_not_cleared(self):
        self.assertEqual(effective_broadcast_status("live", "private", 1), "live")
        self.assertEqual(effective_broadcast_status("live", "unknown", None), "live")
        self.assertEqual(effective_broadcast_status("ended", "unavailable", 0), "ended")
        self.assertEqual(
            effective_broadcast_status("upcoming", "public", 0), "upcoming"
        )

    def test_unavailable_upsert_clears_live_without_fabricating_end(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = migrated_connection(Path(temp) / "test.sqlite3")
            try:
                core.upsert_video(
                    conn,
                    "stream00001",
                    title="Keep identity",
                    source="metadata",
                    video_type="livestream",
                    broadcast_status="live",
                    broadcast_started_at="2020-01-01T00:00:00Z",
                    is_playable=1,
                    availability="public",
                )
                core.store_video_metadata(
                    conn,
                    {
                        "video_id": "stream00001",
                        "yt_status": "ERROR",
                        "availability": "unavailable",
                        "is_playable": False,
                    },
                    "no_metadata",
                    "",
                    updated_at="2026-10-02T20:00:00Z",
                )
                row = conn.execute("SELECT * FROM videos").fetchone()
                self.assertIsNone(row["broadcast_status"])
                self.assertIsNone(row["broadcast_ended_at"])
                self.assertEqual(row["broadcast_started_at"], "2020-01-01T00:00:00Z")
                self.assertEqual(row["title"], "Keep identity")
                core.upsert_video(
                    conn,
                    "stream00001",
                    source="metadata",
                    broadcast_status="live",
                    is_playable=1,
                    availability="public",
                )
                self.assertEqual(
                    conn.execute("SELECT broadcast_status FROM videos").fetchone()[0],
                    "live",
                )
            finally:
                conn.close()

    def test_legacy_unavailable_live_is_hidden_in_every_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "test.sqlite3"
            conn = migrated_connection(path)
            try:
                core.upsert_video(
                    conn,
                    "stream00001",
                    title="Unavailable stream",
                    source="metadata",
                    video_type="livestream",
                    is_playable=0,
                    availability="unavailable",
                    fetched_at="2026-10-02T20:00:00Z",
                )
                conn.execute("UPDATE videos SET broadcast_status='live'")
                conn.execute(
                    "INSERT INTO history_events(event_id,video_id,watch_date,time_precision,source_type) VALUES ('event1','stream00001','2026-10-01','date_only','youtube')"
                )
                conn.commit()
                self.assertIsNone(
                    queries.video_detail_data(conn, "stream00001")["broadcast_status"]
                )
                self.assertIsNone(
                    queries.video_summaries_data(conn, ["stream00001"])["videos"][0][
                        "broadcast_status"
                    ]
                )
                history = queries.history_search_data(conn, "", limit=1)
                self.assertIsNone(history["watch"][0]["broadcast_status"])
                self.assertIsNone(
                    list(PluginPlanningContext(conn, "test").library_videos())[0][
                        "broadcast_status"
                    ]
                )
                self.assertIsNone(
                    _library_videos_by_id(path, ("stream00001",))[0]["broadcast_status"]
                )
                omni = queries.omni_search_data(
                    conn,
                    "",
                    result_kinds={"video"},
                    video_type_filters={"livestream"},
                    video_broadcast_status_filters={"live"},
                )
                self.assertEqual(omni["total"], 0)
                self.assertEqual(omni["broadcastStatusCounts"]["live"], 0)
                self.assertEqual(
                    core.metadata_queue_candidate_rows(conn, never_fetched_only=True),
                    [],
                )
                self.assertEqual(
                    len(
                        core.metadata_queue_candidate_rows(
                            conn, force=True, metadata_kind="video"
                        )
                    ),
                    1,
                )
            finally:
                conn.close()

    def test_recent_full_scan_suppresses_recheck_but_history_and_manual_stay(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = migrated_connection(Path(temp) / "test.sqlite3")
            try:
                core.upsert_video(
                    conn,
                    "stream00001",
                    title="Stream",
                    source="metadata",
                    video_type="livestream",
                    broadcast_status="live",
                    broadcast_started_at="2020-01-01T00:00:00Z",
                    broadcast_status_checked_at="2026-10-02T20:00:00Z",
                    fetched_at="2026-10-02T20:00:00Z",
                )
                self.assertEqual(
                    core.metadata_queue_candidate_rows(
                        conn,
                        never_fetched_only=True,
                        observed_at="2026-10-02T20:30:00Z",
                    ),
                    [],
                )
                self.assertEqual(
                    len(
                        core.metadata_queue_candidate_rows(
                            conn,
                            never_fetched_only=True,
                            observed_at="2026-10-03T21:00:00Z",
                        )
                    ),
                    1,
                )
                core.enqueue_metadata_item(
                    conn, video_id="stream00001", source_key="history", manual=False
                )
                core.enqueue_update_tasks(conn)
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT * FROM worker_queue WHERE video_id='stream00001'"
                    ).fetchone()
                )
            finally:
                conn.close()
