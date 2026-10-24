from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yt_library import core, workers
from yt_library.config import load_config
from yt_library.my_activity import MyActivityError
from yt_library.queries import channel_list_data
from yt_library.youtube_data_api import (
    YouTubeAccountSnapshot,
    YouTubePlaylist,
    YouTubePlaylistItem,
    YouTubeSubscription,
    build_youtube_data_service,
    fetch_youtube_account_snapshot,
    playlist_item_set_video_id,
)


class FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeResource:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        token = kwargs.get("pageToken") or ""
        key = (kwargs.get("playlistId") or "", token)
        return FakeRequest(self.responses[key])


class FakeService:
    def __init__(self):
        self.subscription_resource = FakeResource(
            {
                ("", ""): {
                    "items": [
                        {
                            "snippet": {
                                "title": "Channel one",
                                "publishedAt": "2020-01-02T03:04:05Z",
                                "resourceId": {"channelId": "UCone"},
                            }
                        }
                    ],
                    "nextPageToken": "second",
                },
                ("", "second"): {
                    "items": [
                        {
                            "snippet": {
                                "title": "Channel two",
                                "publishedAt": "2021-02-03T04:05:06Z",
                                "resourceId": {"channelId": "UCtwo"},
                            }
                        }
                    ]
                },
            }
        )
        self.playlist_resource = FakeResource(
            {
                ("", ""): {
                    "items": [
                        {
                            "id": "PLone",
                            "snippet": {
                                "title": "Playlist one",
                                "description": "Description",
                                "channelId": "UCowner",
                                "publishedAt": "2022-03-04T05:06:07Z",
                            },
                            "status": {"privacyStatus": "private"},
                            "contentDetails": {"itemCount": 1},
                        }
                    ]
                }
            }
        )
        self.playlist_item_resource = FakeResource(
            {
                ("PLone", ""): {
                    "items": [
                        {
                            "id": base64.urlsafe_b64encode(b"PLone.090796A75D153932").decode().rstrip("="),
                            "snippet": {
                                "title": "Video one",
                                "position": 0,
                                "publishedAt": "2023-04-05T06:07:08Z",
                                "resourceId": {"videoId": "video-one"},
                            }
                        }
                    ]
                }
            }
        )

    def subscriptions(self):
        return self.subscription_resource

    def playlists(self):
        return self.playlist_resource

    def playlistItems(self):
        return self.playlist_item_resource


class YouTubeDataApiTests(unittest.TestCase):
    def test_occurrence_decoder_requires_verified_shape_and_playlist(self) -> None:
        def encode(value):
            return base64.urlsafe_b64encode(value).decode()

        encoded = encode(b"PLtest.090796A75D153932")
        for value in (encoded, encoded.rstrip("=")):
            self.assertEqual(playlist_item_set_video_id("PLtest", value), "090796A75D153932")
        for value in (None, 123, "", "invalid!", " " + encoded, encoded + "=", "A" * 300,
                      encode(b"PLother.090796A75D153932"), encode(b"PLtestX.090796A75D153932"),
                      encode(b"PLtest.090796A75D15393"), encode(b"PLtest.090796a75d153932"),
                      encode(b"PLtest.090796A75D153932.extra"), encode(b"PLtest.\xff"), encode(b"PLtest")):
            with self.subTest(value=value):
                self.assertEqual(playlist_item_set_video_id("PLtest", value), "")
        self.assertEqual(playlist_item_set_video_id("", encoded), "")

    def test_service_uses_configured_socks_proxy(self) -> None:
        build = Mock(return_value="service")
        credentials = Mock()
        with (
            patch(
                "yt_library.youtube_data_api._google_dependencies",
                return_value=(Mock(), Mock(), Mock(), build),
            ),
            patch(
                "yt_library.youtube_data_api._load_credentials",
                return_value=credentials,
            ),
        ):
            service = build_youtube_data_service(
                Path("client.json"),
                Path("token.json"),
                "socks5h://127.0.0.1:1080",
            )

        self.assertEqual(service, "service")
        self.assertIn("http", build.call_args.kwargs)
        self.assertNotIn("credentials", build.call_args.kwargs)

    def test_fetches_all_subscription_pages_and_playlist_item_dates(self) -> None:
        service = FakeService()
        paced = []

        snapshot = fetch_youtube_account_snapshot(
            service,
            before_request=lambda: paced.append(True),
        )

        self.assertEqual([row.channel_id for row in snapshot.subscriptions], ["UCone", "UCtwo"])
        self.assertEqual(snapshot.playlists[0].published_at, "2022-03-04T05:06:07Z")
        self.assertEqual(snapshot.playlists[0].items[0].position, 1)
        self.assertEqual(snapshot.playlists[0].items[0].published_at, "2023-04-05T06:07:08Z")
        self.assertEqual(snapshot.playlists[0].items[0].set_video_id, "090796A75D153932")
        self.assertEqual(
            [call["pageToken"] for call in service.subscription_resource.calls],
            [None, "second"],
        )
        self.assertEqual(len(paced), 4)

    def test_snapshot_updates_dates_without_preserving_playlist_scan_diffs(self) -> None:
        snapshot = YouTubeAccountSnapshot(
            subscriptions=(
                YouTubeSubscription("UCcurrent", "Current", "2020-01-02T03:04:05Z"),
            ),
            playlists=(
                YouTubePlaylist(
                    "PLone",
                    "Playlist one",
                    "Description",
                    "UCcurrent",
                    "private",
                    "2022-03-04T05:06:07Z",
                    1,
                    (
                        YouTubePlaylistItem(
                            "PLone",
                            "video-one",
                            1,
                            "Video one",
                            "2023-04-05T06:07:08Z",
                            "090796A75D153932",
                        ),
                    ),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                with conn:
                    core.upsert_channel(conn, "UCold", title="Old")
                    conn.execute("UPDATE channels SET subscribed=1 WHERE channel_id='UCold'")
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLone', 'Old title')")
                    core.upsert_video(conn, "video-one", title="")
                    conn.execute(
                        "INSERT INTO playlist_items(playlist_id, position, video_id) VALUES ('PLone', 1, 'video-one')"
                    )
                    stats = core.save_youtube_data_api_snapshot(conn, snapshot)
                current = conn.execute("SELECT * FROM channels WHERE channel_id='UCcurrent'").fetchone()
                old = conn.execute("SELECT * FROM channels WHERE channel_id='UCold'").fetchone()
                playlist = conn.execute("SELECT * FROM playlists WHERE playlist_id='PLone'").fetchone()
                item = conn.execute("SELECT * FROM playlist_items WHERE playlist_id='PLone'").fetchone()
                scans = conn.execute(
                    "SELECT COUNT(*) FROM playlist_scans WHERE playlist_id='PLone'"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(stats["subscriptions"], 1)
        self.assertEqual(current["subscribed"], 1)
        self.assertEqual(current["subscribed_at"], "2020-01-02T03:04:05Z")
        self.assertEqual(current["subscribed_at_source"], "youtube_data_api")
        self.assertIsNone(current["first_seen_at"])
        self.assertEqual(old["subscribed"], 0)
        self.assertEqual(playlist["created_at"], "2022-03-04T05:06:07Z")
        self.assertEqual(item["added_at"], "2023-04-05T06:07:08Z")
        self.assertEqual(item["set_video_id"], "090796A75D153932")
        self.assertEqual(scans, 0)

    def test_optional_account_sync_logs_missing_sources_and_completes_queue_item(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "library.sqlite3"
            core.migrate_database(db_path)
            config = load_config(root / "config.json")
            conn = core.connect(db_path)
            try:
                with conn:
                    core.enqueue_account_sync_task(conn, manual=True)
                queue_id = conn.execute(
                    "SELECT queue_id FROM worker_queue WHERE subject_key='account:sync'"
                ).fetchone()[0]
                workers.run_optional_account_sync(
                    db_path,
                    config,
                    "UTC",
                    queue_id,
                )
                messages = [
                    row["message"]
                    for row in conn.execute(
                        "SELECT message FROM metadata_worker_log ORDER BY id"
                    )
                ]
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM worker_queue WHERE subject_key='account:sync'"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(remaining, 0)
        self.assertTrue(any("My Activity cookies are not configured" in value for value in messages))
        self.assertFalse(any("YouTube Data API sync" in value for value in messages))

    def test_optional_account_sync_consumes_queue_item_when_configured_source_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "library.sqlite3"
            core.migrate_database(db_path)
            config = load_config(root / "config.json")
            (root / "my_activity_cookies.txt").write_text("provided", encoding="utf-8")
            conn = core.connect(db_path)
            try:
                with conn:
                    core.enqueue_account_sync_task(conn, manual=True)
                queue_id = conn.execute(
                    "SELECT queue_id FROM worker_queue WHERE subject_key='account:sync'"
                ).fetchone()[0]
                with patch(
                    "yt_library.workers.fetch_my_activity_pages",
                    side_effect=MyActivityError("refresh the cookie export"),
                ):
                    result = workers.run_optional_account_sync(
                        db_path,
                        config,
                        "UTC",
                        queue_id,
                    )
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM worker_queue WHERE subject_key='account:sync'"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertTrue(result["completed"])
        self.assertTrue(result["source_failed"])
        self.assertIsNone(result["proxy_error"])
        self.assertEqual(remaining, 0)

    def test_channel_date_sort_prefers_subscription_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                with conn:
                    core.upsert_channel(
                        conn,
                        "UCfirstseenlater",
                        title="First seen later",
                        first_seen_at="2025-01-01T00:00:00Z",
                    )
                    core.upsert_channel(
                        conn,
                        "UCsubscribedlater",
                        title="Subscribed later",
                        first_seen_at="2021-01-01T00:00:00Z",
                    )
                    conn.execute(
                        "UPDATE channels SET subscribed_at='2020-01-01T00:00:00Z' WHERE channel_id='UCfirstseenlater'"
                    )
                    conn.execute(
                        "UPDATE channels SET subscribed_at='2024-01-01T00:00:00Z' WHERE channel_id='UCsubscribedlater'"
                    )
                data = channel_list_data(conn, sort="newest_updated")
            finally:
                conn.close()

        self.assertEqual(
            [row["channel_id"] for row in data["results"]],
            ["UCsubscribedlater", "UCfirstseenlater"],
        )

    def test_my_activity_subscription_date_outweighs_data_api_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                with conn:
                    core.upsert_channel(conn, "UCgold", title="Gold standard")
                    conn.execute(
                        """
                        UPDATE channels
                        SET subscribed=1,
                            subscribed_at='2020-01-02T03:04:05Z',
                            subscribed_at_source='my_activity'
                        WHERE channel_id='UCgold'
                        """
                    )
                    core.save_youtube_data_api_snapshot(
                        conn,
                        YouTubeAccountSnapshot(
                            subscriptions=(
                                YouTubeSubscription(
                                    "UCgold",
                                    "Gold standard",
                                    "2024-05-06T07:08:09Z",
                                ),
                            ),
                            playlists=(),
                        ),
                    )
                channel = conn.execute(
                    "SELECT * FROM channels WHERE channel_id='UCgold'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(channel["subscribed_at"], "2020-01-02T03:04:05Z")
        self.assertEqual(channel["subscribed_at_source"], "my_activity")


if __name__ == "__main__":
    unittest.main()
