from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yt_library import __version__, cli, core
from yt_library.config import load_config

from tests.support import migrated_connection


class QueuedCliTests(unittest.TestCase):
    def test_version_exits_without_loading_config_or_touching_runtime(self) -> None:
        for argv in (["--version"], ["--config", "not-a-config.json", "--version"]):
            with self.subTest(argv=argv), patch.object(cli, "load_config") as load, patch.object(
                cli, "configure_request_pacing"
            ) as pacing, patch.object(cli, "migrate_database") as migrate, patch.object(
                cli, "serve"
            ) as serve, redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as result:
                    cli.main(argv)
                self.assertEqual(result.exception.code, 0)
                self.assertEqual(output.getvalue().strip(), f"YT Library {__version__}")
                load.assert_not_called()
                pacing.assert_not_called()
                migrate.assert_not_called()
                serve.assert_not_called()

    def test_recovery_candidate_selector_uses_current_canonical_availability(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.executemany(
                        "INSERT INTO playlists(playlist_id, title) VALUES (?, ?)",
                        (("PLpositive", "Positive stale count"), ("PLzero", "Zero stale count")),
                    )
                    for video_id, title, availability, is_playable, thumbnail in (
                        ("publicretain", "Public retained", "public", 1, "cached.jpg"),
                        ("membersret1", "Members retained", "subscriber_only", 1, "cached.jpg"),
                        ("unavailret1", "Unavailable retained", "unavailable", 0, "cached.jpg"),
                        ("unavailcurr", "Unavailable current", "unavailable", 0, ""),
                        ("terminal001", "Terminal unavailable", "unavailable", 0, ""),
                        ("retryerror1", "Retry recovery error", "unavailable", 0, "cached.jpg"),
                        ("transition1", "Availability transition", "public", 1, "cached.jpg"),
                    ):
                        core.upsert_video(
                            conn,
                            video_id,
                            title=title,
                            thumbnail_path=thumbnail,
                            is_playable=is_playable,
                            availability=availability,
                            source="metadata",
                        )
                    conn.executemany(
                        """
                        INSERT INTO playlist_items(
                          playlist_id, position, video_id, membership_state
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            ("PLpositive", 1, "publicretain", "retained_unavailable"),
                            ("PLpositive", 2, "transition1", "retained_unavailable"),
                            ("PLzero", 1, "membersret1", "retained_unavailable"),
                            ("PLzero", 2, "unavailret1", "retained_unavailable"),
                            ("PLzero", 3, "unavailcurr", "current"),
                            ("PLzero", 4, "terminal001", "retained_unavailable"),
                            ("PLzero", 5, "retryerror1", "current"),
                        ),
                    )
                    conn.executemany(
                        """
                        INSERT INTO playlist_scans(
                          playlist_id, scanned_at, video_count, unavailable_count, scan_status
                        ) VALUES (?, ?, ?, ?, 'ok')
                        """,
                        (
                            ("PLpositive", core.utc_now(), 2, 1),
                            ("PLzero", core.utc_now(), 5, 0),
                        ),
                    )
                    core.save_video_recovery(
                        conn,
                        "terminal001",
                        None,
                        "not_found",
                        "",
                        "",
                        "",
                    )
                    core.save_video_recovery(
                        conn,
                        "retryerror1",
                        None,
                        "error",
                        "temporary failure",
                    )

                automatic = core.placeholder_recovery_candidate_rows(
                    conn,
                    order_by="video",
                )
                completed = core.placeholder_recovery_candidate_rows(
                    conn,
                    include_completed=True,
                    order_by="video",
                )
                likely = core.placeholder_recovery_candidate_rows(
                    conn,
                    include_completed=True,
                    likely_unavailable_only=True,
                    order_by="video",
                )
                missing = core.placeholder_recovery_candidate_rows(
                    conn,
                    include_completed=True,
                    only_missing_thumbnails=True,
                    order_by="video",
                )
                exact = core.placeholder_recovery_candidate_rows(
                    conn,
                    include_completed=True,
                    video_id="unavailcurr",
                    order_by="video",
                )
                forced = core.playlist_placeholder_recovery_rows(
                    conn,
                    force=True,
                    playlist_id="PLzero",
                )

                with conn:
                    core.upsert_video(
                        conn,
                        "transition1",
                        is_playable=0,
                        availability="unavailable",
                        source="metadata",
                    )
                    core.upsert_video(
                        conn,
                        "unavailret1",
                        is_playable=1,
                        availability="public",
                        source="metadata",
                    )
                transitioned = core.placeholder_recovery_candidate_rows(
                    conn,
                    order_by="video",
                )
            finally:
                conn.close()

        self.assertEqual(
            [row["video_id"] for row in automatic],
            ["retryerror1", "unavailcurr", "unavailret1"],
        )
        self.assertEqual(
            [row["video_id"] for row in completed],
            ["retryerror1", "terminal001", "unavailcurr", "unavailret1"],
        )
        self.assertEqual(
            [row["video_id"] for row in likely],
            ["retryerror1", "terminal001", "unavailcurr", "unavailret1"],
        )
        self.assertEqual(
            [row["video_id"] for row in missing],
            ["terminal001", "unavailcurr", "unavailret1"],
        )
        self.assertEqual([row["video_id"] for row in exact], ["unavailcurr"])
        self.assertEqual(
            {row["video_id"] for row in forced},
            {"retryerror1", "terminal001", "unavailcurr", "unavailret1"},
        )
        self.assertEqual(
            [row["video_id"] for row in transitioned],
            ["retryerror1", "transition1", "unavailcurr"],
        )

    def test_scan_hidden_enqueues_selected_playlists_with_cookie_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            cookie_file = Path(temp_dir) / "youtube-cookies.txt"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.executemany(
                        "INSERT INTO playlists(playlist_id, title) VALUES (?, ?)",
                        (("PLbeta", "Beta"), ("PLalpha", "Alpha")),
                    )
            finally:
                conn.close()
            config = load_config(Path(temp_dir) / "config.json")
            args = argparse.Namespace(
                db=str(db_path),
                cookies=str(cookie_file),
                limit=1,
                config_data=config,
            )

            with patch(
                "yt_library.cli._run_queued_cli_batch",
                return_value={"completed": 1, "failed": 0},
            ) as run_batch:
                result = cli.scan_hidden_queued(args)

            conn = core.connect(db_path)
            try:
                rows = core.worker_queue_rows(conn)
            finally:
                conn.close()

        self.assertEqual(result, {"completed": 1, "failed": 0})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["playlist_id"], "PLalpha")
        self.assertEqual(rows[0]["worker_type"], "playlist")
        self.assertTrue(rows[0]["manual"])
        self.assertEqual(
            json.loads(rows[0]["payload_json"]),
            {"cookie_file": str(cookie_file)},
        )
        self.assertEqual(run_batch.call_args.args[1][0]["queue_id"], rows[0]["queue_id"])

    def test_recovery_cli_uses_shared_selector_and_persists_worker_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            cookie_file = Path(temp_dir) / "archivarix-cookies.txt"
            thumb_dir = Path(temp_dir) / "archivarix-thumbs"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLone', 'One')"
                    )
                    core.upsert_video(
                        conn,
                        "missingthumb1",
                        title="Missing thumbnail",
                        is_playable=False,
                        source="test",
                    )
                    core.upsert_video(
                        conn,
                        "othermissing1",
                        title="Other missing",
                        is_playable=False,
                        source="test",
                    )
                    conn.executemany(
                        """
                        INSERT INTO playlist_items(
                          playlist_id, position, video_id, membership_state
                        ) VALUES ('PLone', ?, ?, 'retained_unavailable')
                        """,
                        ((1, "missingthumb1"), (2, "othermissing1")),
                    )
                    core.enqueue_placeholder_recovery_item(
                        conn,
                        video_id="missingthumb1",
                        payload={"automatic": True},
                    )
            finally:
                conn.close()
            config = load_config(Path(temp_dir) / "config.json")
            args = argparse.Namespace(
                db=str(db_path),
                thumbs=str(thumb_dir),
                archivarix_cookies=str(cookie_file),
                video_id="missingthumb1",
                limit=0,
                only_missing=True,
                likely_unavailable_only=False,
                no_api=True,
                delay=1.25,
                refresh_metadata=True,
                config_data=config,
            )

            with patch(
                "yt_library.cli._run_queued_cli_batch",
                return_value={"completed": 1, "failed": 0},
            ) as run_batch:
                result = cli.recover_unavailable_videos_queued(args)

            conn = core.connect(db_path)
            try:
                rows = core.worker_queue_rows(conn)
                selected = next(row for row in rows if row["video_id"] == "missingthumb1")
                payload = json.loads(selected["payload_json"])
                with conn:
                    core.enqueue_placeholder_recovery_item(
                        conn,
                        video_id="missingthumb1",
                        payload={"automatic": "replacement"},
                    )
                preserved = conn.execute(
                    "SELECT task_type, manual, payload_json FROM worker_queue WHERE queue_id = ?",
                    (selected["queue_id"],),
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(result, {"completed": 1, "failed": 0})
        self.assertEqual(selected["task_type"], "thumbnail")
        self.assertTrue(selected["manual"])
        self.assertEqual(
            payload,
            {
                "cookie_file": str(cookie_file),
                "delay_seconds": 1.25,
                "no_api": True,
                "refresh_metadata": True,
                "thumbnail_dir": str(thumb_dir),
            },
        )
        self.assertEqual(preserved["task_type"], "thumbnail")
        self.assertTrue(preserved["manual"])
        self.assertEqual(json.loads(preserved["payload_json"]), payload)
        self.assertEqual(len(run_batch.call_args.args[1]), 1)
        self.assertEqual(run_batch.call_args.args[1][0]["subject_id"], "missingthumb1")


if __name__ == "__main__":
    unittest.main()
