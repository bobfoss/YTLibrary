from __future__ import annotations

import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from yt_library import core, network, workers
from yt_library.config import load_config
from yt_library.workers import (
    ClipWorker,
    LiveHistoryWorker,
    MetadataWorker,
    PlaceholderRecoveryWorker,
    PlaylistScanWorker,
    WorkerQueueDispatcher,
)

from tests.support import migrated_connection


class WorkerQueueTests(unittest.TestCase):
    def test_clip_worker_saves_clip_identity_and_queues_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_clip_item(
                        conn,
                        clip_id="UgkxWorkerClip123",
                        title="Clip title",
                        manual=True,
                    )
                row = dict(core.worker_queue_rows(conn)[0])
            finally:
                conn.close()

            metadata = {
                "clip_id": "UgkxWorkerClip123",
                "title": "Clip title",
                "owner_channel_id": "UC_clip_owner",
                "owner_title": "Clip owner",
                "ownership": "others",
                "source_video_id": "sourceworker1",
                "source_title": "Source video title",
                "source_channel_id": "UC_source_owner",
                "source_channel": "Source uploader",
                "source_duration_text": "0:21",
                "source_uploader_category": "Music",
                "source_reaction": "LIKE",
                "start_ms": 1_000,
                "end_ms": 22_000,
                "view_count": 4,
                "view_count_text": "4 views",
                "clipped_at_text": "Clipped 2 months ago",
                "availability": "active",
                "fetch_status": "ok",
            }
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.fetch_clip_metadata", return_value=metadata),
            ):
                plugin_manager = Mock()
                ClipWorker()._run(
                    "clip-run",
                    db_path,
                    Path(temp_dir) / "missing-cookies.txt",
                    row,
                    "",
                    plugin_manager,
                )

            conn = core.connect(db_path)
            try:
                clip = conn.execute(
                    "SELECT * FROM clips WHERE clip_id = 'UgkxWorkerClip123'"
                ).fetchone()
                source = conn.execute(
                    "SELECT * FROM videos WHERE video_id = 'sourceworker1'"
                ).fetchone()
                queue = conn.execute(
                    "SELECT subject_key FROM worker_queue ORDER BY subject_key"
                ).fetchall()
                log = conn.execute(
                    "SELECT level, video_id, message FROM metadata_worker_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(clip["title"], "Clip title")
        self.assertEqual(clip["source_video_id"], "sourceworker1")
        self.assertEqual(source["reaction"], "LIKE")
        self.assertEqual(source["uploader_category"], "Music")
        self.assertEqual([row["subject_key"] for row in queue], ["metadata:video:sourceworker1"])
        self.assertEqual(tuple(log), ("clip info", "UgkxWorkerClip123", "ok: Clip title"))
        plugin_manager.enqueue_hook.assert_called_once()
        hook_args = plugin_manager.enqueue_hook.call_args.args
        self.assertEqual(hook_args[1:], (
            "clip_scan",
            {
                "clip_id": ["UgkxWorkerClip123"],
                "source_video_id": ["sourceworker1"],
            },
        ))
        self.assertEqual(plugin_manager.enqueue_hook.call_args.kwargs, {"manual": True})

    def test_worker_log_wrappers_write_to_their_owned_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.log_worker_event(conn, "metadata-run", "info", "metadata", "video-1")
                    core.log_worker_queue_event(conn, "warning", "queue", run_id="queue-run")
                    core.log_playlist_scan_event(
                        conn,
                        "playlist-run",
                        "info",
                        "playlist",
                        "playlist-1",
                    )
                    core.log_live_history_event(conn, "history-run", "info", "history", "video-2")
                    core.log_placeholder_recovery_event(
                        conn,
                        "placeholder-run",
                        "info",
                        "placeholder",
                        "video-3",
                    )

                metadata_rows = conn.execute(
                    "SELECT run_id, level, video_id, message FROM metadata_worker_log ORDER BY id"
                ).fetchall()
                playlist_row = conn.execute(
                    "SELECT run_id, level, playlist_id, message FROM playlist_scan_worker_log"
                ).fetchone()
                history_row = conn.execute(
                    "SELECT run_id, level, video_id, message FROM live_history_worker_log"
                ).fetchone()
                placeholder_row = conn.execute(
                    "SELECT run_id, level, video_id, message FROM placeholder_recovery_worker_log"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(
            [tuple(row) for row in metadata_rows],
            [
                ("metadata-run", "info", "video-1", "metadata"),
                ("queue-run", "queue warning", "", "queue"),
            ],
        )
        self.assertEqual(tuple(playlist_row), ("playlist-run", "info", "playlist-1", "playlist"))
        self.assertEqual(tuple(history_row), ("history-run", "info", "video-2", "history"))
        self.assertEqual(
            tuple(placeholder_row),
            ("placeholder-run", "info", "video-3", "placeholder"),
        )

    def test_history_date_conflict_warning_is_deduplicated_across_workers(self) -> None:
        conflict = {
            "event_id": "history-event",
            "video_id": "new-video",
            "watch_date": "2026-08-02",
            "published_date": "2026-08-03",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    workers.log_history_date_conflicts(
                        conn,
                        "history-run",
                        [conflict, conflict],
                        worker_type="history",
                    )
                    workers.log_history_date_conflicts(
                        conn,
                        "metadata-run",
                        [conflict],
                        worker_type="metadata",
                    )
                history_rows = conn.execute(
                    "SELECT level, video_id, message FROM live_history_worker_log"
                ).fetchall()
                metadata_count = conn.execute(
                    "SELECT COUNT(*) FROM metadata_worker_log"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(len(history_rows), 1)
        self.assertEqual(history_rows[0]["level"], "warn")
        self.assertEqual(history_rows[0]["video_id"], "new-video")
        self.assertIn("retained because YouTube may republish videos", history_rows[0]["message"])
        self.assertEqual(metadata_count, 0)

    def test_recent_history_uses_small_batch_and_stops_after_two_matching_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            existing = [
                ("known-a", "2026-07-27"),
                ("known-b", "2026-07-27"),
                ("known-c", "2026-07-26"),
                ("known-d", "2026-07-26"),
            ]
            try:
                with conn:
                    for ordinal, (video_id, watch_date) in enumerate(existing, start=1):
                        core.upsert_video(conn, video_id, title=video_id, source="test")
                        if ordinal == 1:
                            conn.execute(
                                """
                                INSERT INTO history_events(
                                  event_id, video_id, watched_at, watch_date, time_precision,
                                  source_type, match_type, youtube_ordinal,
                                  takeout_history_key, takeout_row_key
                                )
                                VALUES (?, ?, '2026-07-27T12:00:00Z', ?, 'exact',
                                        'takeout_youtube', 'video_id_date', ?,
                                        'takeout', 'known-a-row')
                                """,
                                (f"existing-{ordinal}", video_id, watch_date, ordinal),
                            )
                        else:
                            conn.execute(
                                """
                                INSERT INTO history_events(
                                  event_id, video_id, watch_date, time_precision,
                                  source_type, match_type, youtube_ordinal
                                )
                                VALUES (?, ?, ?, 'date_only', 'youtube', 'youtube_only', ?)
                                """,
                                (f"existing-{ordinal}", video_id, watch_date, ordinal),
                            )
            finally:
                conn.close()

            fetched_rows = [
                {"video_id": "repeat-current", "watch_date": "2026-07-28"}
                for _ in range(195)
            ]
            fetched_rows.extend(
                {"video_id": video_id, "watch_date": watch_date}
                for video_id, watch_date in existing
            )
            fetched_rows.append({"video_id": "older-new", "watch_date": "2026-07-25"})

            worker = LiveHistoryWorker()
            with patch.object(workers, "fetch_youtube_history_web", return_value=fetched_rows) as fetch:
                worker._run(
                    "recent-history-run",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    "recent",
                    "UTC",
                )

            second_worker = LiveHistoryWorker()
            with patch.object(workers, "fetch_youtube_history_web", return_value=fetched_rows) as second_fetch:
                second_worker._run(
                    "second-recent-history-run",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    "recent",
                    "UTC",
                )

            conn = core.connect(db_path)
            try:
                run = conn.execute(
                    "SELECT * FROM live_history_worker_runs WHERE run_id = 'recent-history-run'"
                ).fetchone()
                second_run = conn.execute(
                    "SELECT * FROM live_history_worker_runs WHERE run_id = 'second-recent-history-run'"
                ).fetchone()
                logs = conn.execute(
                    """
                    SELECT video_id, message FROM live_history_worker_log
                    WHERE run_id = 'recent-history-run'
                    ORDER BY rowid
                    """
                ).fetchall()
                event_counts = conn.execute(
                    """
                    SELECT COUNT(*) AS events,
                           COUNT(DISTINCT youtube_ordinal) AS distinct_ordinals,
                           MIN(youtube_ordinal) AS first_ordinal,
                           MAX(youtube_ordinal) AS last_ordinal
                    FROM history_events
                    WHERE youtube_ordinal IS NOT NULL
                    """
                ).fetchone()
                reconciled = conn.execute(
                    """
                    SELECT time_precision, watched_at, youtube_ordinal
                    FROM history_events
                    WHERE event_id = 'existing-1'
                    """
                ).fetchone()
                queued_metadata = core.metadata_queue_rows(conn)
            finally:
                conn.close()

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(second_fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs["limit"], core.RECENT_HISTORY_BATCH_SIZE)
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["processed"], 200)
        self.assertEqual(run["found"], 196)
        self.assertEqual(run["skipped"], 4)
        self.assertIn("2 matching complete days", run["message"])
        self.assertTrue(
            any(
                "196 new watches, 4 existing watches, 1 Takeout matches" in message
                for message in (row["message"] for row in logs)
            )
        )
        self.assertTrue(all(row["video_id"] == "" for row in logs))
        self.assertEqual(second_run["found"], 0)
        self.assertEqual(second_run["skipped"], 200)
        self.assertEqual(dict(event_counts), {
            "events": 200,
            "distinct_ordinals": 200,
            "first_ordinal": 1,
            "last_ordinal": 200,
        })
        self.assertEqual(reconciled["time_precision"], "exact")
        self.assertEqual(reconciled["watched_at"], "2026-07-27T12:00:00Z")
        self.assertEqual(reconciled["youtube_ordinal"], 196)
        self.assertIn("2 metadata queued", run["message"])
        self.assertEqual(
            [row["video_id"] for row in queued_metadata],
            ["repeat-current", "older-new"],
        )

    def test_dispatcher_caps_concurrent_metadata_tasks_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    for index in range(3):
                        core.enqueue_metadata_item(
                            conn,
                            video_id=f"concurrent{index}",
                            current_title=f"Concurrent {index}",
                            metadata_source="history",
                            priority=index,
                        )
            finally:
                conn.close()

            release = threading.Event()
            two_started = threading.Event()
            state_lock = threading.Lock()
            active = 0
            peak = 0
            started = 0

            def fetch_metadata(_opener, video_id, _thumb_dir, **_kwargs):
                nonlocal active, peak, started
                with state_lock:
                    active += 1
                    started += 1
                    peak = max(peak, active)
                    if started >= 2:
                        two_started.set()
                release.wait(2)
                with state_lock:
                    active -= 1
                return {
                    "video_id": video_id,
                    "title": f"Fetched {video_id}",
                    "duration_text": "1:00",
                    "yt_status": "OK",
                }

            dispatcher = WorkerQueueDispatcher()
            config = load_config(Path(temp_dir) / "config.json")
            config.update(
                {
                    "dispatch_mode": "throttle",
                    "job_dispatch_delay_seconds": 10.0,
                    "youtube_max_in_flight": 2,
                    "archivarix_max_in_flight": 1,
                }
            )
            with (
                patch("yt_library.workers.fetch_watch_metadata", side_effect=fetch_metadata),
                patch("yt_library.workers.fetch_new_channel_metadata_if_needed", return_value=({}, "", "")),
            ):
                result = dispatcher.start(
                    db_path,
                    Path(temp_dir) / "missing-youtube-cookies.txt",
                    Path(temp_dir) / "thumbs",
                    config,
                )
                self.assertTrue(result["started"])
                self.assertTrue(two_started.wait(2))
                time.sleep(0.1)
                with state_lock:
                    self.assertEqual(peak, 2)
                    self.assertEqual(started, 2)
                release.set()
                deadline = time.time() + 3
                while dispatcher.is_running() and time.time() < deadline:
                    time.sleep(0.05)

            self.assertFalse(dispatcher.is_running())
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_count(conn), 0)
            finally:
                conn.close()

    def test_dispatcher_drains_active_plugins_for_higher_priority_exclusive_work(self) -> None:
        for exclusive_priority in (-3, 3):
            with self.subTest(exclusive_priority=exclusive_priority), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                db_path = root / "library.sqlite3"
                conn = migrated_connection(db_path)
                try:
                    with conn:
                        conn.executemany(
                            """
                            INSERT INTO worker_queue(
                              subject_key, worker_type, task_type, source_key,
                              priority, created_at, updated_at
                            ) VALUES (?, 'plugin', 'fetch', 'sample', ?, ?, ?)
                            """,
                            [(f"plugin-{index}", index, core.utc_now(), core.utc_now())
                             for index in range(3)],
                        )
                finally:
                    conn.close()

                events: list[str] = []
                active: set[str] = set()

                def consume(worker_type: str) -> dict[str, bool]:
                    self.assertFalse(active, "Exclusive work overlapped an active job")
                    events.append(worker_type)
                    conn = core.connect(db_path)
                    try:
                        with conn:
                            conn.execute("DELETE FROM worker_queue WHERE worker_type = ?", (worker_type,))
                    finally:
                        conn.close()
                    return {"started": True, "completed": True}

                class FakePluginWorker:
                    def start(self, _db_path: Path, _manager: object, row: dict, **_kwargs: object) -> dict[str, bool]:
                        self.queue_id = row["queue_id"]
                        self.name = row["subject_key"]
                        self.polls = 0
                        active.add(self.name)
                        events.append(f"start {self.name}")
                        if self.name == "plugin-1":
                            conn = core.connect(db_path)
                            try:
                                with conn:
                                    core.enqueue_account_sync_task(conn, priority=exclusive_priority)
                                    core.enqueue_playlist_scan_item(
                                        conn, "PLexclusive", priority=exclusive_priority + 1,
                                    )
                                    core.enqueue_history_task(conn, "recent", priority=exclusive_priority + 2)
                            finally:
                                conn.close()
                        return {"started": True}

                    def is_alive(self) -> bool:
                        self.polls += 1
                        if self.polls < 2:
                            return True
                        if self.name in active:
                            active.remove(self.name)
                            events.append(f"finish {self.name}")
                            conn = core.connect(db_path)
                            try:
                                with conn:
                                    core.remove_worker_queue_entry(conn, self.queue_id)
                            finally:
                                conn.close()
                        return False

                    def stop(self) -> None:
                        raise AssertionError("Active jobs must finish naturally")

                process = {"service": "youtube", "maxInFlight": 2}
                manager = Mock()
                manager.process_definitions.return_value = {("sample", "fetch"): process}
                manager.process_definition.return_value = process
                dispatcher = WorkerQueueDispatcher()
                dispatcher._plugin_manager = manager
                dispatcher.update_dispatch_settings("throttle", 0, 2, 1)
                with (
                    patch("yt_library.workers.PluginTaskWorker", FakePluginWorker),
                    patch("yt_library.workers.run_optional_account_sync", side_effect=lambda *_args: consume("account")),
                    patch.object(workers.PLAYLIST_SCAN_WORKER, "start", side_effect=lambda *_args, **_kwargs: consume("playlist")),
                    patch.object(workers.PLAYLIST_SCAN_WORKER, "is_running", return_value=False),
                    patch.object(workers.PLAYLIST_SCAN_WORKER, "proxy_block_reason", return_value=""),
                    patch.object(workers.LIVE_HISTORY_WORKER, "start", side_effect=lambda *_args, **_kwargs: consume("history")),
                    patch.object(workers.LIVE_HISTORY_WORKER, "is_running", return_value=False),
                    patch.object(workers.LIVE_HISTORY_WORKER, "proxy_block_reason", return_value=""),
                ):
                    dispatcher._run(
                        db_path, root / "cookies.txt", root / "thumbs", "UTC",
                        root / "archive-cookies.txt", root / "archive-thumbs",
                    )

                launches = [event for event in events if not event.startswith("finish ")]
                if exclusive_priority < 0:
                    self.assertEqual(launches, [
                        "start plugin-0", "start plugin-1", "account", "playlist",
                        "history", "start plugin-2",
                    ])
                    self.assertLess(events.index("finish plugin-1"), events.index("account"))
                else:
                    self.assertEqual(launches, [
                        "start plugin-0", "start plugin-1", "start plugin-2",
                        "account", "playlist", "history",
                    ])
                conn = core.connect(db_path)
                try:
                    self.assertEqual(core.worker_queue_count(conn), 0)
                finally:
                    conn.close()

    def test_dispatcher_settings_changes_apply_during_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    for index in range(2):
                        core.enqueue_metadata_item(
                            conn,
                            video_id=f"retimed{index}",
                            current_title=f"Retimed {index}",
                            metadata_source="history",
                            priority=index,
                        )
            finally:
                conn.close()

            first_started = threading.Event()
            second_started = threading.Event()
            started_at: list[float] = []

            def fetch_metadata(_opener, video_id, _thumb_dir, **_kwargs):
                started_at.append(time.monotonic())
                if len(started_at) == 1:
                    first_started.set()
                else:
                    second_started.set()
                return {
                    "video_id": video_id,
                    "title": f"Fetched {video_id}",
                    "duration_text": "1:00",
                    "yt_status": "OK",
                }

            dispatcher = WorkerQueueDispatcher()
            config = load_config(Path(temp_dir) / "config.json")
            config.update(
                {
                    "dispatch_mode": "delay",
                    "job_dispatch_delay_seconds": 10.0,
                    "youtube_max_in_flight": 1,
                }
            )
            with (
                patch("yt_library.workers.fetch_watch_metadata", side_effect=fetch_metadata),
                patch("yt_library.workers.fetch_new_channel_metadata_if_needed", return_value=({}, "", "")),
            ):
                result = dispatcher.start(
                    db_path,
                    Path(temp_dir) / "missing-youtube-cookies.txt",
                    Path(temp_dir) / "thumbs",
                    config,
                )
                self.assertTrue(result["started"])
                self.assertTrue(first_started.wait(1))
                self.assertFalse(second_started.wait(0.2))

                settings = dispatcher.update_dispatch_settings(
                    "delay",
                    0.0,
                    2,
                    1,
                )
                self.assertEqual(
                    settings,
                    {
                        "dispatch_mode": "delay",
                        "job_dispatch_delay_seconds": 0.0,
                        "effective_job_dispatch_delay_seconds": 0.0,
                        "youtube_max_in_flight": 2,
                        "archivarix_max_in_flight": 1,
                    },
                )
                self.assertTrue(second_started.wait(1))

                deadline = time.time() + 2
                while dispatcher.is_running() and time.time() < deadline:
                    time.sleep(0.02)

            self.assertFalse(dispatcher.is_running())
            self.assertEqual(len(started_at), 2)
            self.assertLess(started_at[1] - started_at[0], 2.0)

    def test_dispatch_delay_is_global_across_worker_types(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLglobal', 'Global')"
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="global-delay-video",
                        current_title="Global delay video",
                        priority=0,
                    )
                    core.enqueue_playlist_scan_item(
                        conn,
                        "PLglobal",
                        priority=1,
                    )
            finally:
                conn.close()

            launches: list[tuple[str, float]] = []

            def metadata_start(
                _worker,
                worker_db_path,
                _cookie_file,
                _thumb_dir,
                **kwargs,
            ):
                launches.append(("metadata", time.monotonic()))
                queue_id = int(kwargs["queue_id"])
                worker_conn = core.connect(worker_db_path)
                try:
                    with worker_conn:
                        core.remove_worker_queue_entry(worker_conn, queue_id)
                finally:
                    worker_conn.close()
                return {"started": True, "run_id": "fake-metadata"}

            def playlist_start(worker_db_path, *_args, **_kwargs):
                launches.append(("playlist", time.monotonic()))
                worker_conn = core.connect(worker_db_path)
                try:
                    with worker_conn:
                        row = worker_conn.execute(
                            "SELECT queue_id FROM worker_queue WHERE worker_type = 'playlist'"
                        ).fetchone()
                        core.remove_worker_queue_entry(
                            worker_conn,
                            int(row["queue_id"]),
                        )
                finally:
                    worker_conn.close()
                return {"started": True, "run_id": "fake-playlist"}

            dispatcher = WorkerQueueDispatcher()
            config = load_config(Path(temp_dir) / "config.json")
            config.update(
                {
                    "dispatch_mode": "delay",
                    "job_dispatch_delay_seconds": 0.2,
                    "youtube_max_in_flight": 1,
                    "archivarix_max_in_flight": 1,
                }
            )
            with (
                patch.object(MetadataWorker, "start", new=metadata_start),
                patch.object(MetadataWorker, "is_alive", return_value=False),
                patch.object(MetadataWorker, "blocked_reason", return_value=""),
                patch.object(
                    workers.PLAYLIST_SCAN_WORKER,
                    "start",
                    side_effect=playlist_start,
                ),
                patch.object(
                    workers.PLAYLIST_SCAN_WORKER,
                    "is_running",
                    return_value=False,
                ),
            ):
                result = dispatcher.start(
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    config,
                )
                self.assertTrue(result["started"])
                deadline = time.time() + 2
                while dispatcher.is_running() and time.time() < deadline:
                    time.sleep(0.01)

            self.assertFalse(dispatcher.is_running())
            self.assertEqual([worker_type for worker_type, _ in launches], ["metadata", "playlist"])
            self.assertGreaterEqual(launches[1][1] - launches[0][1], 0.18)

    def test_youtube_authentication_block_does_not_stop_placeholder_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            youtube_cookie_file = Path(temp_dir) / "youtube-cookies.txt"
            youtube_cookie_file.write_text("provided", encoding="utf-8")
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="authblocked1",
                        current_title="Authentication blocked",
                        metadata_source="history",
                        priority=0,
                    )
                    conn.execute(
                        """
                        INSERT INTO worker_queue(
                          subject_key, worker_type, video_id, current_title,
                          priority, created_at, updated_at
                        )
                        VALUES ('placeholder:recoverme01', 'placeholder', 'recoverme01',
                                'Recover me', 0, ?, ?)
                        """,
                        (core.utc_now(), core.utc_now()),
                    )
                    core.enqueue_playlist_scan_item(
                        conn,
                        "PLyoutubeBlocked",
                        title="YouTube blocked playlist",
                        priority=1,
                    )
                    core.enqueue_history_task(conn, "recent", priority=1)
            finally:
                conn.close()

            dispatcher = WorkerQueueDispatcher()
            with (
                patch(
                    "yt_library.workers.youtube_session_status",
                    return_value=(False, "YouTube login session is not accepted by YouTube"),
                ),
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "ok")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    return_value=(None, "", "", "not_found", ""),
                ),
            ):
                dispatcher._run(
                    db_path,
                    youtube_cookie_file,
                    Path(temp_dir) / "video-thumbs",
                    "UTC",
                    Path(temp_dir) / "archivarix-cookies.txt",
                    Path(temp_dir) / "archivarix-thumbs",
                    0.0,
                    1,
                    0.0,
                    1,
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 1)
                self.assertEqual(core.worker_queue_type_count(conn, "playlist"), 1)
                self.assertEqual(core.worker_queue_type_count(conn, "history"), 1)
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 0)
                placeholder_run = conn.execute(
                    """
                    SELECT status, recovery_status, message
                    FROM placeholder_recovery_worker_runs
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(
                    tuple(placeholder_run),
                    ("complete", "not_found", "not found"),
                )
            finally:
                conn.close()

    def test_proxy_failure_stops_all_dispatch_and_retains_pending_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="proxyfail01",
                        current_title="Proxy failure",
                        metadata_source="history",
                        priority=0,
                    )
                    conn.execute(
                        """
                        INSERT INTO worker_queue(
                          subject_key, worker_type, video_id, current_title,
                          priority, created_at, updated_at
                        )
                        VALUES ('placeholder:proxyhold01', 'placeholder', 'proxyhold01',
                                'Proxy-held placeholder', 1, ?, ?)
                        """,
                        (core.utc_now(), core.utc_now()),
                    )
            finally:
                conn.close()

            dispatcher = WorkerQueueDispatcher()
            config = load_config(Path(temp_dir) / "config.json")
            config.update(
                {
                    "use_proxy": True,
                    "proxy": "socks5h://127.0.0.1:1081",
                    "job_dispatch_delay_seconds": 0,
                    "youtube_max_in_flight": 1,
                    "archivarix_max_in_flight": 1,
                }
            )

            def wait_for_stop(*_args, **kwargs):
                stop_event = kwargs["stop_event"]
                stop_event.wait(2)
                return None, "", "", "stopped", "Stop requested"

            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch(
                    "yt_library.workers.fetch_watch_metadata",
                    side_effect=network.ProxyUnavailableError(
                        "SOCKS5 proxy 127.0.0.1:1081 is unavailable"
                    ),
                ),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    side_effect=wait_for_stop,
                ),
            ):
                dispatcher._run(
                    db_path,
                    Path(temp_dir) / "missing-youtube-cookies.txt",
                    Path(temp_dir) / "video-thumbs",
                    "UTC",
                    Path(temp_dir) / "archivarix-cookies.txt",
                    Path(temp_dir) / "archivarix-thumbs",
                    15.0,
                    30.0,
                    3,
                    0.0,
                    config["proxy"],
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 1)
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                block = core.external_service_block(conn, "proxy")
                self.assertTrue(block["blocked"])
                self.assertEqual(block["reason_code"], "proxy_unavailable")
                self.assertEqual(block["queue_id"], 1)
                run = conn.execute(
                    """
                    SELECT status, processed, message
                    FROM metadata_worker_runs
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(run["status"], "blocked")
                self.assertEqual(run["processed"], 0)
                self.assertIn("Metadata worker paused", run["message"])
                queue_log = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    WHERE level = 'queue error'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(queue_log["level"], "queue error")
                self.assertIn("Worker queue paused", queue_log["message"])
                self.assertIn("pending items were retained", queue_log["message"])
            finally:
                conn.close()

    def test_missing_pysocks_stops_dispatch_and_retains_pending_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="missingSocks1",
                        current_title="Missing SOCKS dependency",
                        metadata_source="history",
                        priority=0,
                    )
            finally:
                conn.close()

            dispatcher = WorkerQueueDispatcher()
            with patch(
                "yt_library.network.importlib.import_module",
                side_effect=ImportError("No module named 'socks'"),
            ):
                dispatcher._run(
                    db_path,
                    Path(temp_dir) / "missing-youtube-cookies.txt",
                    Path(temp_dir) / "video-thumbs",
                    "UTC",
                    Path(temp_dir) / "archivarix-cookies.txt",
                    Path(temp_dir) / "archivarix-thumbs",
                    15.0,
                    30.0,
                    3,
                    0.0,
                    "socks5h://127.0.0.1:1081",
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 1)
                block = core.external_service_block(conn, "proxy")
                self.assertTrue(block["blocked"])
                self.assertEqual(block["reason_code"], "proxy_unavailable")
                self.assertIn("requires PySocks", block["message"])
                run = conn.execute(
                    """
                    SELECT status, processed, message
                    FROM metadata_worker_runs
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(run["status"], "blocked")
                self.assertEqual(run["processed"], 0)
                self.assertIn("requires PySocks", run["message"])
                queue_log = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    WHERE level = 'queue error'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(queue_log["level"], "queue error")
                self.assertIn("Worker queue paused", queue_log["message"])
                self.assertIn("pending items were retained", queue_log["message"])
            finally:
                conn.close()

    def test_playlist_proxy_failure_retains_queue_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLproxyhold', 'Proxy hold')"
                    )
                    core.enqueue_playlist_scan_item(conn, "PLproxyhold", manual=True)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.request_text",
                    side_effect=network.ProxyUnavailableError(
                        "SOCKS5 proxy 127.0.0.1:1081 is unavailable"
                    ),
                ),
            ):
                worker._run(
                    "playlist-proxy-hold",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                    proxy_url="socks5h://127.0.0.1:1081",
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "playlist"), 1)
                self.assertTrue(core.external_service_block(conn, "proxy")["blocked"])
                run = conn.execute(
                    "SELECT status, message FROM playlist_scan_worker_runs WHERE run_id = ?",
                    ("playlist-proxy-hold",),
                ).fetchone()
                self.assertEqual(run["status"], "blocked")
                self.assertIn("Playlist scan paused", run["message"])
            finally:
                conn.close()

    def test_history_proxy_failure_retains_queue_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_history_task(conn, "recent", priority=0, manual=True)
            finally:
                conn.close()

            worker = LiveHistoryWorker()
            with patch(
                "yt_library.workers.fetch_youtube_history_web",
                side_effect=network.ProxyUnavailableError(
                    "SOCKS5 proxy 127.0.0.1:1081 is unavailable"
                ),
            ):
                worker._run(
                    "history-proxy-hold",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    "recent",
                    "UTC",
                    "socks5h://127.0.0.1:1081",
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "history"), 1)
                self.assertTrue(core.external_service_block(conn, "proxy")["blocked"])
                run = conn.execute(
                    "SELECT status, message FROM live_history_worker_runs WHERE run_id = ?",
                    ("history-proxy-hold",),
                ).fetchone()
                self.assertEqual(run["status"], "blocked")
                self.assertIn("History fetch paused", run["message"])
            finally:
                conn.close()

    def test_placeholder_proxy_failure_retains_queue_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO worker_queue(
                          subject_key, worker_type, video_id, current_title,
                          priority, created_at, updated_at
                        )
                        VALUES ('placeholder:proxyhold02', 'placeholder', 'proxyhold02',
                                'Proxy-held placeholder', 0, ?, ?)
                        """,
                        (core.utc_now(), core.utc_now()),
                    )
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()
            with (
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    side_effect=network.ProxyUnavailableError(
                        "SOCKS5 proxy 127.0.0.1:1081 is unavailable"
                    ),
                ),
            ):
                worker._run(
                    "placeholder-proxy-hold",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    proxy_url="socks5h://127.0.0.1:1081",
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                self.assertTrue(core.external_service_block(conn, "proxy")["blocked"])
                self.assertTrue(core.admin_status(db_path)["proxyBlock"]["blocked"])
                self.assertFalse(core.external_service_block(conn, "archivarix")["blocked"])
                run = conn.execute(
                    """
                    SELECT status, recovery_status, message
                    FROM placeholder_recovery_worker_runs
                    WHERE run_id = ?
                    """,
                    ("placeholder-proxy-hold",),
                ).fetchone()
                self.assertEqual(run["status"], "blocked")
                self.assertEqual(run["recovery_status"], "proxy_unavailable")
                self.assertIn("Placeholder recovery paused", run["message"])
            finally:
                conn.close()

    def test_no_youtube_metadata_queues_archivarix_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="unavailable1",
                        current_title="Unavailable example",
                        metadata_source="history",
                        priority=7,
                    )
            finally:
                conn.close()

            worker = MetadataWorker()
            plugin_manager = Mock()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.fetch_watch_metadata",
                    return_value={"video_id": "unavailable1", "title": "", "yt_status": "ERROR"},
                ),
                patch("yt_library.workers.recover_archivarix_video") as recover,
            ):
                worker._run(
                    "test-archivarix-handoff",
                    db_path,
                    Path(temp_dir) / "missing-youtube-cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                    plugin_manager=plugin_manager,
                )

            recover.assert_not_called()
            plugin_manager.enqueue_hook.assert_not_called()
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 0)
                row = core.placeholder_worker_queue_rows(conn, limit=1)[0]
                self.assertEqual(row["video_id"], "unavailable1")
                self.assertEqual(row["priority"], 7)
                log = conn.execute(
                    """
                    SELECT message
                    FROM metadata_worker_log
                    WHERE run_id = 'test-archivarix-handoff'
                    """
                ).fetchone()
                self.assertEqual(
                    log["message"],
                    "no metadata from YouTube; placeholder recovery queued",
                )
            finally:
                conn.close()

    def test_no_youtube_metadata_does_not_requeue_completed_archivarix_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.save_video_recovery(
                        conn,
                        "unavailable1",
                        {"title": "Recovered title", "status": "DELETED_FULL_META"},
                        "found",
                        "",
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="unavailable1",
                        current_title="Unavailable example",
                        metadata_source="history",
                        priority=7,
                    )
            finally:
                conn.close()

            worker = MetadataWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.fetch_watch_metadata",
                    return_value={"video_id": "unavailable1", "title": "", "yt_status": "ERROR"},
                ),
            ):
                worker._run(
                    "test-completed-archivarix-handoff",
                    db_path,
                    Path(temp_dir) / "missing-youtube-cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 0)
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 0)
                log = conn.execute(
                    """
                    SELECT message
                    FROM metadata_worker_log
                    WHERE run_id = 'test-completed-archivarix-handoff'
                    """
                ).fetchone()
                self.assertEqual(
                    log["message"],
                    "no metadata from YouTube; completed placeholder recovery already recorded",
                )
            finally:
                conn.close()

    def test_automatic_placeholder_enqueue_skips_completed_recovery_but_manual_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.save_video_recovery(
                        conn,
                        "completed01",
                        None,
                        "not_found",
                        "",
                    )
                    automatic_inserted = core.enqueue_placeholder_recovery_item(
                        conn,
                        video_id="completed01",
                    )
                    manual_inserted = core.enqueue_placeholder_recovery_item(
                        conn,
                        video_id="completed01",
                        manual=True,
                    )

                self.assertFalse(automatic_inserted)
                self.assertTrue(manual_inserted)
                row = conn.execute(
                    "SELECT manual FROM worker_queue WHERE video_id = 'completed01'"
                ).fetchone()
                self.assertEqual(row["manual"], 1)
            finally:
                conn.close()

    def test_successful_video_metadata_notifies_plugin_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="abcdefghijk",
                        current_title="New video",
                        metadata_source="history",
                        priority=0,
                        manual=True,
                    )
            finally:
                conn.close()

            metadata = {
                "video_id": "abcdefghijk",
                "title": "New video",
                "description": "",
                "channel_id": "",
                "channel": "",
                "channel_url": "",
                "duration_text": "1:00",
                "view_count": "",
                "upload_date": "",
                "thumbnail_url": "",
                "thumbnail_path": "",
                "channel_thumbnail_url": "",
                "channel_thumbnail_path": "",
                "reaction": "",
                "watch_progress_percent": "0",
                "watch_resume_seconds": "0",
                "yt_status": "OK",
            }
            plugin_manager = Mock()
            plugin_manager.enqueue_hook.return_value = []
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.fetch_watch_metadata", return_value=metadata),
                patch(
                    "yt_library.workers.fetch_new_channel_metadata_if_needed",
                    return_value=({}, "", ""),
                ),
            ):
                MetadataWorker()._run(
                    "test-video-plugin-notification",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                    plugin_manager=plugin_manager,
                )

            plugin_manager.enqueue_hook.assert_called_once()
            hook_args = plugin_manager.enqueue_hook.call_args.args
            self.assertEqual(
                hook_args[1:],
                ("video_scan", {"video_id": ["abcdefghijk"]}),
            )
            self.assertEqual(plugin_manager.enqueue_hook.call_args.kwargs, {"manual": True})

    def test_metadata_worker_stops_when_cookie_authentication_expires(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            cookie_file = Path(temp_dir) / "cookies.txt"
            cookie_file.write_text("provided", encoding="utf-8")
            conn = migrated_connection(db_path)
            try:
                with conn:
                    for index in range(2):
                        core.enqueue_metadata_item(
                            conn,
                            video_id=f"authcheck{index}",
                            current_title=f"Auth check {index}",
                            metadata_source="history",
                            priority=index,
                        )
            finally:
                conn.close()

            metadata = {
                "video_id": "authcheck0",
                "title": "Authenticated metadata",
                "description": "",
                "channel_id": "",
                "channel": "",
                "channel_url": "",
                "duration_text": "1:00",
                "view_count": "",
                "upload_date": "",
                "thumbnail_url": "",
                "thumbnail_path": "",
                "channel_thumbnail_url": "",
                "channel_thumbnail_path": "",
                "reaction": "LIKE",
                "watch_progress_percent": "0",
                "watch_resume_seconds": "0",
                "yt_status": "OK",
            }
            worker = MetadataWorker()
            with (
                patch(
                    "yt_library.workers.youtube_session_status",
                    return_value=(True, ""),
                ) as session_status,
                patch(
                    "yt_library.workers.cached_youtube_authentication_probe",
                    return_value=(
                        "yt_dlp_probe=cookies_rotated; deno=available; ejs=available"
                    ),
                ) as ytdlp_probe,
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.fetch_watch_metadata",
                    side_effect=[
                        metadata,
                        core.YouTubeAuthenticationError(
                            "YouTube login session is not accepted by YouTube",
                            "operation=watch page; logged_in=false; markers=bot_check",
                        ),
                    ],
                ) as fetch_metadata,
                patch("yt_library.workers.fetch_new_channel_metadata_if_needed", return_value=({}, "", "")),
            ):
                worker._run(
                    "test-auth-expired",
                    db_path,
                    cookie_file,
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=0,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                )

            self.assertEqual(session_status.call_count, 2)
            self.assertEqual(fetch_metadata.call_count, 2)
            ytdlp_probe.assert_called_once_with(cookie_file, "")
            self.assertIn("not accepted", worker.blocked_reason())
            conn = core.connect(db_path)
            try:
                run = conn.execute(
                    "SELECT status, processed, message FROM metadata_worker_runs WHERE run_id = 'test-auth-expired'"
                ).fetchone()
                self.assertEqual(run["status"], "error")
                self.assertEqual(run["processed"], 1)
                self.assertIn("not accepted", run["message"])
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 1)
                remaining = core.metadata_queue_rows(conn)[0]
                self.assertEqual(remaining["video_id"], "authcheck1")
                auth_status = core.cookie_auth_statuses(conn)["youtube"]
                self.assertEqual(auth_status["status"], "rejected")
                self.assertIn("not accepted", auth_status["message"])
                debug_log = conn.execute(
                    """
                    SELECT level, video_id, message
                    FROM metadata_worker_log
                    WHERE run_id = 'test-auth-expired' AND level = 'debug'
                    """
                ).fetchone()
                self.assertEqual(debug_log["video_id"], "authcheck1")
                self.assertIn("operation=watch page", debug_log["message"])
                self.assertIn("logged_in=false", debug_log["message"])
                self.assertIn("yt_dlp_probe=cookies_rotated", debug_log["message"])
            finally:
                conn.close()

    def test_playlist_worker_discovers_and_queues_current_playlists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            cookie_file = Path(temp_dir) / "cookies.txt"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_playlist_discovery_item(conn)
            finally:
                conn.close()

            records = [
                {
                    "playlist_id": "PLnewcurrent",
                    "title": "New current playlist",
                    "description": "Discovered live",
                    "owner": "Owner",
                    "owner_channel_id": "UCdiscoverowner1234567",
                    "owner_thumbnail_url": "",
                    "visibility": "private",
                    "video_count": "3",
                    "thumbnail_url": "https://example.test/playlist.jpg",
                    "url": "https://www.youtube.com/playlist?list=PLnewcurrent",
                },
                {
                    "playlist_id": "LL",
                    "title": "Liked videos",
                    "description": "",
                    "owner": "",
                    "owner_channel_id": "",
                    "owner_thumbnail_url": "",
                    "visibility": "private",
                    "video_count": "1",
                    "thumbnail_url": "",
                    "url": "https://www.youtube.com/playlist?list=LL",
                },
            ]
            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.fetch_current_youtube_playlists",
                    return_value=(object(), records),
                ) as discover,
            ):
                worker._run(
                    "test-playlist-discovery",
                    db_path,
                    cookie_file,
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            discover.assert_called_once_with(
                cookie_file,
                proxy_url="",
                timezone_name="UTC",
            )
            conn = core.connect(db_path)
            try:
                playlist = conn.execute(
                    "SELECT title, visibility, video_count, ownership, in_library "
                    "FROM playlists WHERE playlist_id = 'PLnewcurrent'"
                ).fetchone()
                queued = core.playlist_scan_queue_rows(conn)
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log "
                    "WHERE run_id = 'test-playlist-discovery'"
                ).fetchone()
                legacy_group = conn.execute(
                    """
                    SELECT 1
                    FROM groups
                    WHERE group_key = 'youtube-ungrouped'
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(
            dict(playlist),
            {
                "title": "New current playlist",
                "visibility": "private",
                "video_count": 3,
                "ownership": "others",
                "in_library": 1,
            },
        )
        self.assertEqual(
            [(row["task_type"], row["playlist_id"]) for row in queued],
            [("scan", "PLnewcurrent")],
        )
        self.assertTrue(queued[0]["manual"])
        self.assertIsNone(legacy_group)
        self.assertEqual(log["level"], "info")
        self.assertIn("1 current, 1 new, 0 existing", log["message"])

    def test_update_playlist_discovery_scans_only_new_playlists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            cookie_file = Path(temp_dir) / "cookies.txt"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title, video_count) "
                        "VALUES ('PLexisting', 'Existing', 2)"
                    )
                    core.enqueue_playlist_discovery_item(conn, mode="new")
            finally:
                conn.close()

            records = [
                {
                    "playlist_id": "PLexisting",
                    "title": "Existing updated",
                    "visibility": "private",
                    "video_count": "2",
                },
                {
                    "playlist_id": "PLbrandnew",
                    "title": "Brand new",
                    "visibility": "unlisted",
                    "video_count": "3",
                },
            ]
            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.fetch_current_youtube_playlists",
                    return_value=(object(), records),
                ),
            ):
                worker._run(
                    "test-update-playlist-discovery",
                    db_path,
                    cookie_file,
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            conn = core.connect(db_path)
            try:
                queued = core.playlist_scan_queue_rows(conn)
                existing_title = conn.execute(
                    "SELECT title FROM playlists WHERE playlist_id = 'PLexisting'"
                ).fetchone()["title"]
            finally:
                conn.close()

        self.assertEqual(existing_title, "Existing updated")
        self.assertEqual(
            [(row["playlist_id"], row["manual"]) for row in queued],
            [("PLbrandnew", 1)],
        )

    def test_playlist_discovery_queues_existing_reported_count_difference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            cookie_file = Path(temp_dir) / "cookies.txt"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, in_library, video_count, last_changed_at
                        ) VALUES (
                          'PLcountcandidate', 'Count candidate', 1, 14,
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
                          'PLcountcandidate', '2026-08-20T12:34:56Z', 14, 0, 'ok'
                        )
                        """
                    )
                    core.enqueue_playlist_discovery_item(conn, mode="new")
            finally:
                conn.close()

            records = [
                {
                    "playlist_id": "PLcountcandidate",
                    "title": "Count candidate",
                    "visibility": "public",
                    "video_count": 15,
                    "has_video_count": True,
                }
            ]
            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.fetch_current_youtube_playlists",
                    return_value=(object(), records),
                ),
            ):
                worker._run(
                    "test-count-candidate-discovery",
                    db_path,
                    cookie_file,
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            conn = core.connect(db_path)
            try:
                playlist = conn.execute(
                    """
                    SELECT video_count, last_changed_at
                    FROM playlists
                    WHERE playlist_id = 'PLcountcandidate'
                    """
                ).fetchone()
                queued = core.playlist_scan_queue_rows(conn)
                log = conn.execute(
                    """
                    SELECT message
                    FROM playlist_scan_worker_log
                    WHERE run_id = 'test-count-candidate-discovery'
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(playlist["video_count"], 14)
        self.assertEqual(playlist["last_changed_at"], "2026-08-20T12:34:56Z")
        self.assertEqual(
            [(row["playlist_id"], row["source_key"]) for row in queued],
            [("PLcountcandidate", "discovery_change_candidate")],
        )
        self.assertIn("1 change candidates", log["message"])

    def test_playlist_discovery_deletes_accessible_foreign_playlist_removed_from_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            cookie_file = Path(temp_dir) / "cookies.txt"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_channel(conn, "UCmine", title="Library owner")
                    core.upsert_channel(conn, "UCother", title="Other owner")
                    conn.executemany(
                        """
                        INSERT INTO playlists(
                          playlist_id, title, owner_channel_id, ownership, in_library
                        ) VALUES (?, ?, ?, ?, 1)
                        """,
                        [
                            ("PLcurrent", "Current", "UCmine", "mine"),
                            ("PLforeign", "Foreign", "UCother", "others"),
                        ],
                    )
                    core.upsert_video(conn, "foreignonly1", source="playlist")
                    conn.execute(
                        """
                        INSERT INTO playlist_items(playlist_id, position, video_id)
                        VALUES ('PLforeign', 1, 'foreignonly1')
                        """
                    )
                    core.enqueue_playlist_discovery_item(conn, mode="new")
            finally:
                conn.close()

            discovered = [
                {
                    "playlist_id": "PLcurrent",
                    "title": "Current",
                    "owner": "Library owner",
                    "owner_channel_id": "UCmine",
                    "visibility": "private",
                    "video_count": "1",
                }
            ]
            metadata = {
                "title": "Foreign",
                "owner": "Other owner",
                "owner_channel_id": "UCother",
                "visibility": "public",
                "video_count": 1,
                "has_video_count": True,
            }
            videos = [
                {
                    "video_id": "foreignonly1",
                    "title": "Foreign-only video",
                    "is_playable": True,
                }
            ]
            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.fetch_current_youtube_playlists",
                    return_value=(object(), discovered),
                ),
                patch("yt_library.workers.request_text", return_value="playlist page"),
                patch("yt_library.workers.extract_playlist_metadata", return_value=metadata),
                patch(
                    "yt_library.workers.scan_playlist_ytdlp",
                    return_value=(videos, metadata),
                ),
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
            ):
                worker._run(
                    "test-foreign-library-removal",
                    db_path,
                    cookie_file,
                    delay=0,
                    limit=2,
                    force=False,
                    record_summary=False,
                )

            scan_web.assert_not_called()
            conn = core.connect(db_path)
            try:
                playlist = conn.execute(
                    "SELECT 1 FROM playlists WHERE playlist_id = 'PLforeign'"
                ).fetchone()
                video = conn.execute(
                    "SELECT 1 FROM videos WHERE video_id = 'foreignonly1'"
                ).fetchone()
                tombstone = conn.execute(
                    "SELECT 1 FROM playlist_tombstones WHERE playlist_id = 'PLforeign'"
                ).fetchone()
                log = conn.execute(
                    """
                    SELECT message FROM playlist_scan_worker_log
                    WHERE run_id = 'test-foreign-library-removal'
                      AND playlist_id = 'PLforeign'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertIsNone(playlist)
        self.assertIsNone(video)
        self.assertIsNone(tombstone)
        self.assertIn("accessible foreign playlist deleted", log["message"])

    def test_playlist_worker_logs_existing_playlist_count_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLchanged', 'Changed')")
                    conn.execute(
                        """
                        INSERT INTO playlist_scans(
                          playlist_id, scanned_at, video_count, unavailable_count, scan_status
                        ) VALUES ('PLchanged', '2026-07-01T00:00:00Z', 1, 0, 'ok')
                        """
                    )
                    core.enqueue_playlist_scan_item(conn, "PLchanged", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.request_text", return_value="header page"),
                patch(
                    "yt_library.workers.extract_playlist_metadata",
                    return_value={"video_count": 2, "has_video_count": True},
                ),
                patch(
                    "yt_library.workers.scan_playlist_ytdlp",
                    return_value=([{"video_id": "first"}, {"video_id": "second"}], {}),
                ),
                patch("yt_library.workers.scan_playlist_videos"),
                patch("yt_library.workers.save_playlist_scan", return_value=(2, 0)),
                patch(
                    "yt_library.workers.enqueue_placeholder_recovery_targets",
                    return_value={"inserted": 0},
                ),
            ):
                worker._run(
                    "test-playlist-count-change",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log "
                    "WHERE run_id = 'test-playlist-count-change'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(log["level"], "info")
        self.assertIn("count changed 1 -> 2 (+1)", log["message"])

    def test_automatic_playlist_scan_queues_only_never_fetched_members(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLchanged', 'Changed')"
                    )
                    core.upsert_video(
                        conn,
                        "existing001",
                        title="Existing metadata",
                        source="metadata",
                        fetch_status="ok",
                        fetched_at="2026-08-01T00:00:00Z",
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_items(playlist_id, position, video_id)
                        VALUES ('PLchanged', 1, 'existing001')
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_scans(
                          playlist_id, scanned_at, video_count, unavailable_count, scan_status
                        ) VALUES ('PLchanged', '2026-08-01T00:00:00Z', 1, 0, 'ok')
                        """
                    )
                    core.enqueue_playlist_scan_item(conn, "PLchanged", manual=False)
            finally:
                conn.close()

            videos = [
                {
                    "playlist_id": "PLchanged",
                    "position": 1,
                    "video_id": "existing001",
                    "title": "Existing metadata",
                    "channel_id": "",
                    "channel": "",
                    "duration_text": "1:00",
                    "is_playable": 1,
                    "availability": "public",
                    "url": "https://www.youtube.com/watch?v=existing001",
                },
                {
                    "playlist_id": "PLchanged",
                    "position": 2,
                    "video_id": "newvideo001",
                    "title": "New playlist video",
                    "channel_id": "",
                    "channel": "",
                    "duration_text": "2:00",
                    "is_playable": 1,
                    "availability": "public",
                    "url": "https://www.youtube.com/watch?v=newvideo001",
                },
            ]
            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.request_text", return_value="header page"),
                patch(
                    "yt_library.workers.extract_playlist_metadata",
                    return_value={"video_count": 2, "has_video_count": True},
                ),
                patch(
                    "yt_library.workers.fetch_playlist_collaboration_metadata",
                    return_value={},
                ),
                patch(
                    "yt_library.workers.scan_playlist_ytdlp",
                    return_value=(videos, {}),
                ),
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
                patch(
                    "yt_library.workers.enqueue_placeholder_recovery_targets",
                    return_value={"inserted": 0},
                ),
            ):
                worker._run(
                    "test-playlist-new-member-metadata",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            scan_web.assert_not_called()
            conn = core.connect(db_path)
            try:
                queued = conn.execute(
                    """
                    SELECT video_id, current_title, source_key, manual
                    FROM worker_queue
                    WHERE worker_type = 'metadata'
                    ORDER BY priority, queue_id
                    """
                ).fetchall()
                log = conn.execute(
                    """
                    SELECT message
                    FROM playlist_scan_worker_log
                    WHERE run_id = 'test-playlist-new-member-metadata'
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(
            [dict(row) for row in queued],
            [
                {
                    "video_id": "newvideo001",
                    "current_title": "New playlist video",
                    "source_key": "PLchanged",
                    "manual": 0,
                }
            ],
        )
        self.assertIn("queued 1 metadata items", log["message"])

    def test_liked_video_worker_merges_short_scan_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_video(conn, "oldliked123", title="Existing like", source="metadata")
                    conn.execute("UPDATE videos SET reaction = 'LIKE' WHERE video_id = 'oldliked123'")
                    core.enqueue_playlist_scan_item(conn, "LL", manual=False)
            finally:
                conn.close()

            ytdlp_videos = [
                {
                    "video_id": "newliked123",
                    "title": "New like",
                    "is_playable": True,
                },
                {
                    "video_id": "newliked456",
                    "title": "Another like",
                    "is_playable": True,
                },
            ]
            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.request_text", return_value="header page"),
                patch(
                    "yt_library.workers.extract_playlist_metadata",
                    return_value={
                        "video_count": 3,
                        "has_video_count": True,
                        "visibility": "private",
                    },
                ),
                patch(
                    "yt_library.workers.scan_playlist_ytdlp",
                    return_value=(ytdlp_videos, {}),
                ),
                patch("yt_library.workers.youtube_session_status", return_value=(True, "")),
                patch(
                    "yt_library.workers.scan_playlist_videos",
                    return_value=[ytdlp_videos[0]],
                ),
            ):
                worker._run(
                    "test-liked-short-scan",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            conn = core.connect(db_path)
            try:
                reactions = {
                    row["video_id"]: row["reaction"]
                    for row in conn.execute(
                        "SELECT video_id, reaction FROM videos ORDER BY video_id"
                    )
                }
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log "
                    "WHERE run_id = 'test-liked-short-scan'"
                ).fetchone()
                run = conn.execute(
                    "SELECT found, failed FROM playlist_scan_worker_runs "
                    "WHERE run_id = 'test-liked-short-scan'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(reactions["oldliked123"], "LIKE")
        self.assertEqual(reactions["newliked123"], "LIKE")
        self.assertEqual(reactions["newliked456"], "LIKE")
        self.assertEqual(dict(run), {"found": 1, "failed": 0})
        self.assertEqual(log["level"], "info")
        self.assertIn("2 exposed of 3 reported", log["message"])
        self.assertIn("partial result merged, 3 canonical likes retained", log["message"])

    def test_playlist_worker_caches_playlist_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example')")
                    core.enqueue_playlist_scan_item(conn, "PLexample", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            header = {
                "title": "Example",
                "video_count": 1,
                "has_video_count": True,
                "visibility": "public",
                "youtube_updated_date": "2026-08-24",
                "thumbnail_url": "https://example.test/playlist.jpg",
            }
            videos = [
                {
                    "playlist_id": "PLexample",
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
            ]
            opener = object()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=opener),
                patch("yt_library.workers.request_text", return_value="header page"),
                patch(
                    "yt_library.workers.extract_playlist_metadata",
                    return_value=header,
                ) as extract_header,
                patch("yt_library.workers.scan_playlist_ytdlp", return_value=(videos, {})),
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
                patch("yt_library.workers.cache_thumbnail", return_value="thumbs/PLexample.jpg") as cache_thumb,
                patch("yt_library.workers.enqueue_placeholder_recovery_targets", return_value={"inserted": 0}),
            ):
                worker._run(
                    "test-playlist-thumbnail",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            scan_web.assert_not_called()
            extract_header.assert_called_once_with(
                "header page",
                "PLexample",
                timezone_name="UTC",
            )
            cache_thumb.assert_called_once_with(
                opener,
                "PLexample",
                "https://example.test/playlist.jpg",
                core.DEFAULT_THUMB_DIR,
            )
            conn = core.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT thumbnail_url, thumbnail_path, youtube_updated_date "
                    "FROM playlists WHERE playlist_id = 'PLexample'"
                ).fetchone()
                self.assertEqual(row["thumbnail_url"], "https://example.test/playlist.jpg")
                self.assertEqual(row["thumbnail_path"], "thumbs/PLexample.jpg")
                self.assertEqual(row["youtube_updated_date"], "2026-08-24")
            finally:
                conn.close()

    def test_playlist_worker_metadata_only_skips_members_and_related_queues(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO playlists(playlist_id, title, video_count)
                        VALUES ('PLmetadataonly', 'Metadata only', 1)
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
                    core.enqueue_playlist_scan_item(
                        conn,
                        "PLmetadataonly",
                        title="Metadata only",
                        manual=True,
                        payload={"metadata_only": True},
                    )
            finally:
                conn.close()

            header = {
                "title": "Metadata only",
                "video_count": 1,
                "has_video_count": True,
                "visibility": "private",
                "youtube_updated_date": "2026-08-26",
            }
            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.request_text", return_value="header page"),
                patch(
                    "yt_library.workers.extract_playlist_metadata",
                    return_value=header,
                ),
                patch(
                    "yt_library.workers.fetch_playlist_collaboration_metadata",
                    return_value={},
                ),
                patch("yt_library.workers.scan_playlist_ytdlp") as scan_ytdlp,
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
                patch(
                    "yt_library.workers.enqueue_playlist_metadata_targets"
                ) as enqueue_members,
                patch(
                    "yt_library.workers.enqueue_placeholder_recovery_targets"
                ) as enqueue_placeholders,
            ):
                worker._run(
                    "test-playlist-metadata-only",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            scan_ytdlp.assert_not_called()
            scan_web.assert_not_called()
            enqueue_members.assert_not_called()
            enqueue_placeholders.assert_not_called()
            conn = core.connect(db_path)
            try:
                playlist = conn.execute(
                    """
                    SELECT youtube_updated_date, metadata_checked_at
                    FROM playlists
                    WHERE playlist_id = 'PLmetadataonly'
                    """
                ).fetchone()
                members = conn.execute(
                    """
                    SELECT video_id
                    FROM playlist_items
                    WHERE playlist_id = 'PLmetadataonly'
                    """
                ).fetchall()
                queue_count = core.worker_queue_count(conn)
                log = conn.execute(
                    """
                    SELECT level, message
                    FROM playlist_scan_worker_log
                    WHERE run_id = 'test-playlist-metadata-only'
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(playlist["youtube_updated_date"], "2026-08-26")
        self.assertTrue(playlist["metadata_checked_at"])
        self.assertEqual([row["video_id"] for row in members], ["member00001"])
        self.assertEqual(queue_count, 0)
        self.assertEqual(log["level"], "info")
        self.assertIn("member videos skipped", log["message"])

    def test_playlist_worker_scans_new_manual_playlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_playlist_scan_item(
                        conn,
                        "PLnewmanual",
                        title="PLnewmanual",
                        manual=True,
                    )
            finally:
                conn.close()

            videos = [
                {
                    "playlist_id": "PLnewmanual",
                    "position": 1,
                    "video_id": "manualvid01",
                    "title": "Manual video",
                    "channel_id": "",
                    "channel": "",
                    "duration_text": "2:00",
                    "is_playable": 1,
                    "availability": "public",
                    "url": "https://www.youtube.com/watch?v=manualvid01",
                }
            ]
            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.request_text", return_value="header page"),
                patch(
                    "yt_library.workers.extract_playlist_metadata",
                    return_value={
                        "title": "New manual playlist",
                        "video_count": 1,
                        "has_video_count": True,
                    },
                ),
                patch(
                    "yt_library.workers.scan_playlist_ytdlp",
                    return_value=(
                        videos,
                        {"title": "New manual playlist", "video_count": 1},
                    ),
                ),
                patch(
                    "yt_library.workers.enqueue_playlist_metadata_targets",
                    return_value={"queued_count": 0},
                ),
                patch(
                    "yt_library.workers.enqueue_placeholder_recovery_targets",
                    return_value={"inserted": 0},
                ),
            ):
                worker._run(
                    "test-new-manual-playlist",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            conn = core.connect(db_path)
            try:
                playlist = conn.execute(
                    """
                    SELECT title, video_count, fetch_status
                    FROM playlists
                    WHERE playlist_id = 'PLnewmanual'
                    """
                ).fetchone()
                scan = conn.execute(
                    """
                    SELECT video_count, scan_status
                    FROM playlist_scans
                    WHERE playlist_id = 'PLnewmanual'
                    """
                ).fetchone()
                item = conn.execute(
                    """
                    SELECT video_id
                    FROM playlist_items
                    WHERE playlist_id = 'PLnewmanual'
                    """
                ).fetchone()
                queued = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM worker_queue
                    WHERE playlist_id = 'PLnewmanual'
                    """
                ).fetchone()[0]
                log = conn.execute(
                    """
                    SELECT playlist_id, level, message
                    FROM playlist_scan_worker_log
                    WHERE run_id = 'test-new-manual-playlist'
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(
            dict(playlist),
            {
                "title": "New manual playlist",
                "video_count": 1,
                "fetch_status": "ok",
            },
        )
        self.assertEqual(dict(scan), {"video_count": 1, "scan_status": "ok"})
        self.assertEqual(item["video_id"], "manualvid01")
        self.assertEqual(queued, 0)
        self.assertEqual(log["playlist_id"], "PLnewmanual")
        self.assertEqual(log["level"], "info")
        self.assertIn("1 videos", log["message"])

    def test_playlist_worker_crash_log_preserves_playlist_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_playlist_scan_item(
                        conn,
                        "PLcrash",
                        title="Crash target",
                        manual=True,
                    )
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.request_text",
                    side_effect=RuntimeError("unexpected failure"),
                ),
            ):
                worker._run(
                    "test-playlist-crash-id",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    """
                    SELECT playlist_id, level, message
                    FROM playlist_scan_worker_log
                    WHERE run_id = 'test-playlist-crash-id'
                    """
                ).fetchone()
                queued = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM worker_queue
                    WHERE playlist_id = 'PLcrash'
                    """
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(log["playlist_id"], "PLcrash")
        self.assertEqual(log["level"], "error")
        self.assertIn("unexpected failure", log["message"])
        self.assertEqual(queued, 1)

    def test_playlist_worker_uses_web_fallback_after_short_ytdlp_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example')")
                    core.enqueue_playlist_scan_item(conn, "PLexample", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            header = {"video_count": 2, "has_video_count": True, "visibility": "public"}
            ytdlp_videos = [{"video_id": "first"}]
            web_videos = [{"video_id": "first"}, {"video_id": "second"}]
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.request_text", return_value="header page"),
                patch("yt_library.workers.extract_playlist_metadata", return_value=header),
                patch("yt_library.workers.scan_playlist_ytdlp", return_value=(ytdlp_videos, {})),
                patch("yt_library.workers.youtube_session_status", return_value=(True, "")),
                patch("yt_library.workers.scan_playlist_videos", return_value=web_videos) as scan_web,
                patch("yt_library.workers.save_playlist_scan", return_value=(2, 0)),
                patch("yt_library.workers.enqueue_placeholder_recovery_targets", return_value={"inserted": 0}),
            ):
                worker._run(
                    "test-playlist-fallback",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            scan_web.assert_called_once()
            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log WHERE run_id = 'test-playlist-fallback'"
                ).fetchone()
                self.assertEqual(log["level"], "info")
                self.assertIn("2 videos", log["message"])
            finally:
                conn.close()

    def test_playlist_worker_still_uses_ytdlp_in_throttle_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLthrottle', 'Throttle')"
                    )
                    core.enqueue_playlist_scan_item(
                        conn,
                        "PLthrottle",
                        manual=False,
                    )
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            header = {
                "video_count": 1,
                "has_video_count": True,
                "visibility": "private",
            }
            videos = [{"video_id": "private-video"}]
            core.configure_request_pacing(
                {
                    "dispatch_mode": "throttle",
                    "request_delay_min_seconds": 0,
                    "request_delay_max_seconds": 0,
                }
            )
            try:
                with (
                    patch("yt_library.workers.load_cookie_opener", return_value=object()),
                    patch("yt_library.workers.request_text", return_value="header page"),
                    patch(
                        "yt_library.workers.extract_playlist_metadata",
                        return_value=header,
                    ),
                    patch(
                        "yt_library.workers.scan_playlist_ytdlp",
                        return_value=(videos, {}),
                    ) as scan_ytdlp,
                    patch("yt_library.workers.scan_playlist_videos") as scan_web,
                    patch(
                        "yt_library.workers.save_playlist_scan",
                        return_value=(1, 0),
                    ),
                    patch(
                        "yt_library.workers.enqueue_placeholder_recovery_targets",
                        return_value={"inserted": 0},
                    ),
                ):
                    worker._run(
                        "test-playlist-throttle-ytdlp",
                        db_path,
                        Path(temp_dir) / "cookies.txt",
                        delay=0,
                        limit=1,
                        force=False,
                        record_summary=False,
                    )
            finally:
                core.configure_request_pacing({"dispatch_mode": "delay"})

            scan_ytdlp.assert_called_once_with(
                "PLthrottle",
                Path(temp_dir) / "cookies.txt",
                "",
            )
            scan_web.assert_not_called()

    def test_playlist_worker_skips_when_header_count_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example')")
                    core.enqueue_playlist_scan_item(conn, "PLexample", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.request_text", return_value="header page"),
                patch("yt_library.workers.extract_playlist_metadata", return_value={"video_count": 0, "has_video_count": False}),
                patch("yt_library.workers.scan_playlist_ytdlp", return_value=([], {})) as scan_ytdlp,
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
            ):
                worker._run(
                    "test-playlist-no-header",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            scan_ytdlp.assert_called_once()
            scan_web.assert_not_called()
            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log WHERE run_id = 'test-playlist-no-header'"
                ).fetchone()
                self.assertEqual(log["level"], "error")
                self.assertIn("playlist count unavailable", log["message"])
            finally:
                conn.close()

    def test_playlist_worker_uses_ytdlp_count_for_authenticated_header_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example')")
                    core.enqueue_playlist_scan_item(conn, "PLexample", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            videos = [{"video_id": "first"}]
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.request_text",
                    return_value='ytcfg.set({"LOGGED_IN":true}); ServiceLogin recaptcha',
                ),
                patch(
                    "yt_library.workers.extract_playlist_metadata",
                    return_value={"video_count": 0, "has_video_count": False},
                ),
                patch(
                    "yt_library.workers.scan_playlist_ytdlp",
                    return_value=(videos, {"video_count": 1, "title": "Example"}),
                ) as scan_ytdlp,
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
                patch("yt_library.workers.save_playlist_scan", return_value=(1, 0)),
                patch("yt_library.workers.enqueue_placeholder_recovery_targets", return_value={"inserted": 0}),
            ):
                worker._run(
                    "test-playlist-authenticated-header-shell",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            scan_ytdlp.assert_called_once()
            scan_web.assert_not_called()
            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log "
                    "WHERE run_id = 'test-playlist-authenticated-header-shell'"
                ).fetchone()
                self.assertEqual(log["level"], "info")
                self.assertIn("1 videos", log["message"])
            finally:
                conn.close()

    def test_playlist_worker_marks_authenticated_missing_playlist_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
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
                    conn.execute(
                        """
                        INSERT INTO history_events(
                          event_id, video_id, watch_date, time_precision, source_type, match_type
                        ) VALUES ('kept-watch', 'keptvideo01', '2026-07-28', 'date_only', 'youtube', 'video_id_date')
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_items(playlist_id, position, video_id)
                        VALUES ('PLmissing', 1, 'keptvideo01')
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_scans(
                          playlist_id, scanned_at, video_count, unavailable_count, scan_status
                        ) VALUES ('PLmissing', '2026-07-28T00:00:00Z', 1, 0, 'ok')
                        """
                    )
                    core.enqueue_playlist_scan_item(conn, "PLmissing", manual=True)
            finally:
                conn.close()

            missing_error = (
                "[youtube:tab] ERROR - Requested entity was not found. "
                "Unable to download API page: HTTP Error 404: Not Found"
            )
            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.request_text",
                    return_value='ytcfg.set({"LOGGED_IN":true});',
                ),
                patch(
                    "yt_library.workers.extract_playlist_metadata",
                    return_value={"video_count": 0, "has_video_count": False},
                ),
                patch(
                    "yt_library.workers.scan_playlist_ytdlp",
                    side_effect=RuntimeError(missing_error),
                ),
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
            ):
                worker._run(
                    "test-playlist-missing",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            scan_web.assert_not_called()
            conn = core.connect(db_path)
            try:
                playlist = conn.execute(
                    "SELECT 1 FROM playlists WHERE playlist_id = 'PLmissing'"
                ).fetchone()
                self.assertIsNone(playlist)
                tombstone = conn.execute(
                    "SELECT reason FROM playlist_tombstones WHERE playlist_id = 'PLmissing'"
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
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM worker_queue WHERE playlist_id = 'PLmissing'"
                    ).fetchone()[0],
                    0,
                )
                log = conn.execute(
                    """
                    SELECT level, message
                    FROM playlist_scan_worker_log
                    WHERE run_id = 'test-playlist-missing'
                    """
                ).fetchone()
                self.assertEqual(log["level"], "info")
                self.assertIn("confirmed removed", log["message"])
                self.assertIn("tombstone recorded", log["message"])
            finally:
                conn.close()

    def test_playlist_worker_accepts_valid_header_with_login_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example')")
                    core.enqueue_playlist_scan_item(conn, "PLexample", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            header = {
                "video_count": 1,
                "has_video_count": True,
                "visibility": "private",
                "owner": "Playlist Owner",
                "owner_channel_id": "UCplaylistowner123456789",
            }
            videos = [{"video_id": "first"}]
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.request_text", return_value="ServiceLogin header page"),
                patch("yt_library.workers.extract_playlist_metadata", return_value=header),
                patch(
                    "yt_library.workers.fetch_playlist_collaboration_metadata",
                    return_value={
                        "owner": "Panel Owner",
                        "owner_channel_id": "UCpanelowner123456789012",
                        "collaborators_authoritative": True,
                        "collaborators": [
                            {
                                "title": "Panel Collaborator",
                                "channel_id": "UCpanelcollaborator1234567",
                            }
                        ],
                    },
                ),
                patch("yt_library.workers.scan_playlist_ytdlp", return_value=(videos, {})) as scan_ytdlp,
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
                patch("yt_library.workers.save_playlist_scan", return_value=(1, 0)) as save_scan,
                patch("yt_library.workers.enqueue_placeholder_recovery_targets", return_value={"inserted": 0}),
            ):
                worker._run(
                    "test-playlist-valid-header-with-login-marker",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            scan_ytdlp.assert_called_once()
            scan_web.assert_not_called()
            saved_metadata = save_scan.call_args.kwargs["playlist_metadata"]
            self.assertEqual(saved_metadata["visibility"], "private")
            self.assertEqual(saved_metadata["owner"], "Panel Owner")
            self.assertEqual(saved_metadata["owner_channel_id"], "UCpanelowner123456789012")
            self.assertEqual(saved_metadata["collaborators"][0]["title"], "Panel Collaborator")
            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log WHERE run_id = 'test-playlist-valid-header-with-login-marker'"
                ).fetchone()
                self.assertEqual(log["level"], "info")
                self.assertIn("1 videos", log["message"])
            finally:
                conn.close()

    def test_playlist_worker_reports_signed_out_header_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example')")
                    core.enqueue_playlist_scan_item(conn, "PLexample", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.request_text",
                    return_value=(
                        'ytcfg.set({"LOGGED_IN":false}); '
                        "<a href='https://accounts.google.com/ServiceLogin'>Sign in</a>"
                    ),
                ),
                patch("yt_library.workers.extract_playlist_metadata", return_value={"video_count": 0, "has_video_count": False}),
                patch("yt_library.workers.scan_playlist_ytdlp") as scan_ytdlp,
                patch("yt_library.workers.scan_playlist_videos") as scan_web,
            ):
                worker._run(
                    "test-playlist-signed-out-header",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            scan_ytdlp.assert_not_called()
            scan_web.assert_not_called()
            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log WHERE run_id = 'test-playlist-signed-out-header'"
                ).fetchone()
                self.assertEqual(log["level"], "error")
                self.assertIn("login session is not accepted", log["message"])
            finally:
                conn.close()

    def test_playlist_worker_allows_foreign_playlist_short_of_reported_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO playlists(playlist_id, title) VALUES ('PLforeign', 'Foreign')")
                    core.enqueue_playlist_scan_item(conn, "PLforeign", manual=False)
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            header = {"video_count": 168, "has_video_count": True, "owner_channel_id": "UCother"}
            ytdlp_videos = [{"video_id": f"video{i}"} for i in range(100)]
            web_videos = [{"video_id": f"video{i}"} for i in range(167)]
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.request_text", return_value="header page"),
                patch("yt_library.workers.extract_playlist_metadata", return_value=header),
                patch("yt_library.workers.scan_playlist_ytdlp", return_value=(ytdlp_videos, {})),
                patch("yt_library.workers.youtube_session_status", return_value=(True, "")),
                patch("yt_library.workers.scan_playlist_videos", return_value=web_videos) as scan_web,
                patch("yt_library.workers.save_playlist_scan", return_value=(167, 1)) as save_scan,
                patch("yt_library.workers.enqueue_placeholder_recovery_targets", return_value={"inserted": 0}),
            ):
                worker._run(
                    "test-foreign-short",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                )

            scan_web.assert_called_once()
            save_scan.assert_called_once()
            saved_videos = save_scan.call_args.args[2]
            self.assertEqual(len(saved_videos), 167)
            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    "SELECT level, message FROM playlist_scan_worker_log WHERE run_id = 'test-foreign-short'"
                ).fetchone()
                self.assertEqual(log["level"], "info")
                self.assertIn("167 exposed of 168 reported", log["message"])
            finally:
                conn.close()

    def test_placeholder_recovery_targets_use_the_common_worker_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.enqueue_worker_queue_target(conn, "PLearlierWork")
                    conn.execute("UPDATE worker_queue SET priority = 25 WHERE playlist_id = 'PLearlierWork'")
                candidate = {
                    "video_id": "abc12345678",
                    "title": "Unavailable example",
                    "playlist_count": 2,
                }
                with patch("yt_library.core.playlist_placeholder_recovery_rows", return_value=[candidate]):
                    with conn:
                        first = core.enqueue_placeholder_recovery_targets(
                            conn,
                            "PLexample",
                        )
                        second = core.enqueue_placeholder_recovery_targets(
                            conn,
                            "PLexample",
                        )

                self.assertEqual(first, {"inserted": 1, "existing": 0})
                self.assertEqual(second, {"inserted": 0, "existing": 1})
                row = conn.execute(
                    "SELECT worker_type, task_type, video_id, playlist_id, current_title, source_key, priority "
                    "FROM worker_queue WHERE worker_type = 'placeholder'"
                ).fetchone()
                self.assertEqual(
                    dict(row),
                    {
                        "worker_type": "placeholder",
                        "task_type": "recover",
                        "video_id": "abc12345678",
                        "playlist_id": "PLexample",
                        "current_title": "Unavailable example",
                        "source_key": "",
                        "priority": 26,
                    },
                )
            finally:
                conn.close()

    def test_placeholder_recovery_targets_queue_only_canonically_unavailable_videos(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLcanonical', 'Canonical')"
                    )
                    for video_id, availability, is_playable in (
                        ("publickeep1", "public", 1),
                        ("memberskeep", "subscriber_only", 1),
                        ("unavailkeep", "unavailable", 0),
                        ("unavailcurr", "unavailable", 0),
                        ("terminalrec", "unavailable", 0),
                    ):
                        core.upsert_video(
                            conn,
                            video_id,
                            title=video_id,
                            availability=availability,
                            is_playable=is_playable,
                            source="metadata",
                        )
                    conn.executemany(
                        """
                        INSERT INTO playlist_items(
                          playlist_id, position, video_id, membership_state
                        ) VALUES ('PLcanonical', ?, ?, ?)
                        """,
                        (
                            (1, "publickeep1", "retained_unavailable"),
                            (2, "memberskeep", "retained_unavailable"),
                            (3, "unavailkeep", "retained_unavailable"),
                            (4, "unavailcurr", "current"),
                            (5, "terminalrec", "retained_unavailable"),
                        ),
                    )
                    core.save_video_recovery(
                        conn,
                        "terminalrec",
                        None,
                        "not_found",
                        "",
                    )
                    first = core.enqueue_placeholder_recovery_targets(
                        conn,
                        "PLcanonical",
                    )
                    second = core.enqueue_placeholder_recovery_targets(
                        conn,
                        "PLcanonical",
                    )

                queued_ids = {
                    row["video_id"]
                    for row in conn.execute(
                        """
                        SELECT video_id
                        FROM worker_queue
                        WHERE worker_type = 'placeholder'
                        """
                    )
                }
                memberships = {
                    row["video_id"]: row["membership_state"]
                    for row in conn.execute(
                        """
                        SELECT video_id, membership_state
                        FROM playlist_items
                        WHERE playlist_id = 'PLcanonical'
                        """
                    )
                }
                self.assertEqual(first, {"inserted": 2, "existing": 0})
                self.assertEqual(second, {"inserted": 0, "existing": 2})
                self.assertEqual(queued_ids, {"unavailkeep", "unavailcurr"})
                self.assertEqual(
                    memberships,
                    {
                        "publickeep1": "retained_unavailable",
                        "memberskeep": "retained_unavailable",
                        "unavailkeep": "retained_unavailable",
                        "unavailcurr": "current",
                        "terminalrec": "retained_unavailable",
                    },
                )
            finally:
                conn.close()

    def test_worker_queue_events_capture_add_update_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="abc12345678",
                        current_title="Example video",
                        metadata_source="provided",
                        priority=10,
                    )
                queue_row = conn.execute(
                    "SELECT queue_id FROM worker_queue WHERE video_id = 'abc12345678'"
                ).fetchone()
                queue_id = int(queue_row["queue_id"])
                first_cursor = core.worker_queue_event_cursor(conn)
                events = core.worker_queue_events_after(conn, 0)
                self.assertEqual([(row["queue_id"], row["operation"]) for row in events], [(queue_id, "upsert")])
                self.assertEqual(
                    [row["video_id"] for row in core.worker_queue_rows_by_id(conn, [queue_id])],
                    ["abc12345678"],
                )

                with conn:
                    conn.execute("UPDATE worker_queue SET priority = 2 WHERE queue_id = ?", (queue_id,))
                    core.remove_worker_queue_entry(conn, queue_id)
                later_events = core.worker_queue_events_after(conn, first_cursor)
                self.assertEqual(
                    [(row["queue_id"], row["operation"]) for row in later_events],
                    [(queue_id, "upsert"), (queue_id, "remove")],
                )
                self.assertEqual(core.worker_queue_rows_by_id(conn, [queue_id]), [])
            finally:
                conn.close()

    def test_worker_queue_prefers_recent_actions_within_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="olderaction1",
                        current_title="Older action",
                        metadata_source="provided",
                        priority=5,
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="neweraction1",
                        current_title="Newer action",
                        metadata_source="provided",
                        priority=5,
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="higherpriority1",
                        current_title="Higher priority",
                        metadata_source="provided",
                        priority=4,
                    )
                    conn.execute(
                        """
                        UPDATE worker_queue
                        SET updated_at = CASE video_id
                          WHEN 'olderaction1' THEN '2026-07-28T10:00:00Z'
                          WHEN 'neweraction1' THEN '2026-07-28T11:00:00Z'
                          WHEN 'higherpriority1' THEN '2026-07-28T09:00:00Z'
                        END
                        """
                    )

                rows = core.worker_queue_rows(conn)
                self.assertEqual(
                    [row["video_id"] for row in rows],
                    ["higherpriority1", "neweraction1", "olderaction1"],
                )
                next_row = WorkerQueueDispatcher()._next_row(db_path)
                self.assertEqual(next_row["video_id"], "higherpriority1")
                higher_priority_id = int(next_row["queue_id"])
                next_same_priority = WorkerQueueDispatcher()._next_row(
                    db_path,
                    excluded_queue_ids={higher_priority_id},
                )
                self.assertEqual(next_same_priority["video_id"], "neweraction1")

                with conn:
                    conn.execute(
                        """
                        UPDATE worker_queue
                        SET updated_at = '2026-07-28T12:00:00Z'
                        WHERE video_id = 'olderaction1'
                        """
                    )
                same_priority = core.metadata_queue_rows(conn)
                self.assertEqual(
                    [row["video_id"] for row in same_priority],
                    ["higherpriority1", "olderaction1", "neweraction1"],
                )
                next_refreshed = WorkerQueueDispatcher()._next_row(
                    db_path,
                    excluded_queue_ids={higher_priority_id},
                )
                self.assertEqual(next_refreshed["video_id"], "olderaction1")
            finally:
                conn.close()

    def test_worker_log_cursors_snapshot_and_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "abc12345678",
                        title="Example video",
                        source="test",
                    )
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example playlist')"
                    )
                    conn.execute(
                        "INSERT INTO metadata_worker_log(run_id, created_at, level, video_id, message) "
                        "VALUES ('run-1', '2026-07-13T12:00:00Z', 'video', 'abc12345678', 'first')"
                    )
                    conn.execute(
                        "INSERT INTO playlist_scan_worker_log(run_id, created_at, level, playlist_id, message) "
                        "VALUES ('run-1', '2026-07-13T12:00:01Z', 'info', 'PLexample', 'playlist')"
                    )
                    conn.execute(
                        "INSERT INTO placeholder_recovery_worker_log(run_id, created_at, level, video_id, message) "
                        "VALUES ('run-2', '2026-07-13T12:00:02Z', 'found', 'placeholder1', 'recovered')"
                    )

                cursors = core.worker_log_cursors(conn)
                snapshot = core.worker_log_snapshot(conn)
                self.assertEqual([row["message"] for row in snapshot["metadataLogs"]], ["first"])
                self.assertEqual(snapshot["metadataLogs"][0]["subject_title"], "Example video")
                self.assertEqual(snapshot["metadataLogs"][0]["display_id"], "abc12345678")
                self.assertEqual([row["message"] for row in snapshot["playlistScanLogs"]], ["playlist"])
                self.assertEqual(
                    snapshot["playlistScanLogs"][0]["subject_title"],
                    "Example playlist",
                )
                self.assertEqual(snapshot["playlistScanLogs"][0]["display_id"], "PLexample")
                self.assertEqual(snapshot["liveHistoryLogs"], [])
                self.assertEqual(
                    [row["message"] for row in snapshot["placeholderRecoveryLogs"]],
                    ["recovered"],
                )

                with conn:
                    core.upsert_video(
                        conn,
                        "def12345678",
                        title="Second video",
                        source="test",
                    )
                    core.upsert_video(
                        conn,
                        "ghi12345678",
                        title="History video",
                        source="test",
                    )
                    conn.execute(
                        "INSERT INTO metadata_worker_log(run_id, created_at, level, video_id, message) "
                        "VALUES ('run-1', '2026-07-13T12:00:02Z', 'video', 'def12345678', 'second')"
                    )
                    conn.execute(
                        "INSERT INTO live_history_worker_log(run_id, created_at, level, video_id, message) "
                        "VALUES ('run-1', '2026-07-13T12:00:03Z', 'info', 'ghi12345678', 'history')"
                    )

                deltas = core.worker_logs_after(conn, cursors)
                self.assertEqual([row["message"] for row in deltas["metadataLogs"]], ["second"])
                self.assertEqual(deltas["metadataLogs"][0]["subject_title"], "Second video")
                self.assertEqual(deltas["metadataLogs"][0]["display_id"], "def12345678")
                self.assertEqual(deltas["playlistScanLogs"], [])
                self.assertEqual([row["message"] for row in deltas["liveHistoryLogs"]], ["history"])
                self.assertEqual(deltas["liveHistoryLogs"][0]["subject_title"], "History video")
                self.assertEqual(deltas["liveHistoryLogs"][0]["display_id"], "ghi12345678")
                self.assertEqual(deltas["placeholderRecoveryLogs"], [])
            finally:
                conn.close()

    def test_worker_log_page_combines_filters_and_paginates_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "abc12345678",
                        title="Example video",
                        source="test",
                    )
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLexample', 'Example playlist')"
                    )
                    conn.execute(
                        "INSERT INTO metadata_worker_log(run_id, created_at, level, video_id, message) "
                        "VALUES ('run-1', '2026-07-13T12:00:04Z', 'queue error', 'abc12345678', 'queue')"
                    )
                    conn.execute(
                        "INSERT INTO metadata_worker_log(run_id, created_at, level, video_id, message) "
                        "VALUES ('run-1', '2026-07-13T12:00:03Z', 'video', 'abc12345678', 'metadata')"
                    )
                    conn.execute(
                        "INSERT INTO playlist_scan_worker_log(run_id, created_at, level, playlist_id, message) "
                        "VALUES ('run-1', '2026-07-13T12:00:02Z', 'warning', 'PLexample', 'playlist')"
                    )
                    conn.execute(
                        "INSERT INTO metadata_worker_log(run_id, created_at, level, video_id, message) "
                        "VALUES ('run-1', '2026-07-13T12:00:01Z', 'placeholder warn', 'missing-id', 'queued placeholder')"
                    )
                    conn.execute(
                        "INSERT INTO placeholder_recovery_worker_log(run_id, created_at, level, video_id, message) "
                        "VALUES ('run-1', '2026-07-13T12:00:00Z', 'found', 'missing-id', 'placeholder')"
                    )
                    conn.execute(
                        "INSERT INTO metadata_worker_log(run_id, created_at, level, video_id, message) "
                        "VALUES ('run-1', '2026-07-13T11:59:59Z', 'video debug', 'abc12345678', 'debug metadata')"
                    )

                rows, total = core.worker_log_page(conn, limit=2)
                self.assertEqual(total, 6)
                self.assertEqual([row["message"] for row in rows], ["queue", "metadata"])
                self.assertEqual(rows[0]["source"], "queue")
                self.assertEqual(rows[0]["level"], "error")
                self.assertEqual(rows[0]["identifier"], "abc12345678")

                rows, total = core.worker_log_page(conn, limit=2, offset=2)
                self.assertEqual(total, 6)
                self.assertEqual([row["message"] for row in rows], ["playlist", "queued placeholder"])

                rows, total = core.worker_log_page(conn, source="placeholder")
                self.assertEqual(total, 2)
                self.assertEqual(
                    {(row["stream"], row["message"]) for row in rows},
                    {
                        ("metadataLogs", "queued placeholder"),
                        ("placeholderRecoveryLogs", "placeholder"),
                    },
                )

                rows, total = core.worker_log_page(conn, severity="info")
                self.assertEqual(total, 2)
                self.assertEqual([row["message"] for row in rows], ["metadata", "placeholder"])

                rows, total = core.worker_log_page(conn, severity="warn")
                self.assertEqual(total, 4)
                self.assertEqual(
                    [row["message"] for row in rows],
                    ["metadata", "playlist", "queued placeholder", "placeholder"],
                )

                rows, total = core.worker_log_page(conn, severity="error")
                self.assertEqual(total, 5)
                self.assertNotIn("debug metadata", [row["message"] for row in rows])

                rows, total = core.worker_log_page(conn, severity="debug")
                self.assertEqual(total, 6)
                self.assertIn("debug metadata", [row["message"] for row in rows])
            finally:
                conn.close()

    def test_stopped_placeholder_recovery_keeps_its_queue_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                candidate = {
                    "video_id": "abc12345678",
                    "title": "Unavailable example",
                    "playlist_count": 1,
                }
                with patch("yt_library.core.playlist_placeholder_recovery_rows", return_value=[candidate]):
                    with conn:
                        core.enqueue_placeholder_recovery_targets(conn, "PLexample")
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()

            def stop_during_recovery(*args, **kwargs):
                worker._stop.set()
                return None, "", "", "stopped", "Stop requested"

            with (
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.recover_archivarix_video", side_effect=stop_during_recovery),
            ):
                worker._run(
                    "test-placeholder-stopped",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                run = conn.execute(
                    "SELECT status, video_id, message FROM placeholder_recovery_worker_runs WHERE run_id = ?",
                    ("test-placeholder-stopped",),
                ).fetchone()
                self.assertEqual(tuple(run), ("stopped", "abc12345678", "Stop requested"))
                logs = conn.execute(
                    "SELECT run_id, level, message FROM placeholder_recovery_worker_log WHERE run_id = ? ORDER BY id",
                    ("test-placeholder-stopped",),
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in logs],
                    [
                        ("test-placeholder-stopped", "warn", "Stop requested"),
                    ],
                )
            finally:
                conn.close()

    def test_rate_limited_placeholder_recovery_keeps_queue_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                candidate = {
                    "video_id": "abc12345678",
                    "title": "Unavailable example",
                    "playlist_count": 1,
                }
                with patch("yt_library.core.playlist_placeholder_recovery_rows", return_value=[candidate]):
                    with conn:
                        core.enqueue_placeholder_recovery_targets(conn, "PLexample")
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()
            with (
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    return_value=(None, "", "", "rate_limited", "Archivarix daily search limit reached"),
                ),
            ):
                worker._run(
                    "test-placeholder-rate-limited",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                self.assertEqual(worker.blocked_reason(), "Archivarix daily search limit reached")
                block = core.external_service_block(conn, "archivarix")
                self.assertTrue(block["blocked"])
                self.assertEqual(block["reason_code"], "rate_limited")
                self.assertEqual(block["run_id"], "test-placeholder-rate-limited")
                self.assertTrue(block["retry_eligible"])
                run = conn.execute(
                    """
                    SELECT status, processed, failed, recovery_status, video_id,
                           request_started_at, request_count, message
                    FROM placeholder_recovery_worker_runs
                    WHERE run_id = ?
                    """,
                    ("test-placeholder-rate-limited",),
                ).fetchone()
                self.assertEqual(
                    tuple(run),
                    (
                        "blocked",
                        1,
                        1,
                        "rate_limited",
                        "abc12345678",
                        run["request_started_at"],
                        1,
                        "Archivarix daily search limit reached",
                    ),
                )
                self.assertTrue(run["request_started_at"])
                logs = conn.execute(
                    "SELECT run_id, level, message FROM placeholder_recovery_worker_log WHERE run_id = ? ORDER BY id",
                    ("test-placeholder-rate-limited",),
                ).fetchall()
                self.assertEqual(logs[-1]["level"], "warn")
                self.assertEqual(logs[-1]["message"], "Archivarix daily search limit reached")
                status = core.admin_status(db_path, include_logs=True, worker_queue_limit=0)
                self.assertEqual(
                    status["latestPlaceholderRecoveryRun"]["run_id"],
                    "test-placeholder-rate-limited",
                )
                self.assertEqual(
                    status["placeholderRecoveryLogs"][0]["run_id"],
                    "test-placeholder-rate-limited",
                )
                self.assertTrue(status["archivarixBlock"]["blocked"])
                self.assertEqual(status["archivarixRequestCounts"]["current_utc_day"], 1)
                self.assertEqual(status["archivarixRequestCounts"]["total"], 1)
                self.assertRegex(
                    status["archivarixRequestCounts"]["window_started_at"],
                    r"^\d{4}-\d{2}-\d{2}T00:00:00Z$",
                )
                self.assertRegex(
                    status["archivarixRequestCounts"]["window_ends_at"],
                    r"^\d{4}-\d{2}-\d{2}T00:00:00Z$",
                )
                self.assertEqual(
                    status["archivarixRequestCounts"]["latest_at"],
                    run["request_started_at"],
                )
            finally:
                conn.close()

    def test_archivarix_request_count_uses_current_utc_day(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.executemany(
                        """
                        INSERT INTO placeholder_recovery_worker_runs(
                          run_id, status, started_at, request_started_at, request_count
                        )
                        VALUES (?, 'complete', ?, ?, ?)
                        """,
                        (
                            (
                                "old-window",
                                "2000-01-01T12:00:00Z",
                                "2000-01-01T12:00:00Z",
                                3,
                            ),
                            (
                                "current-window",
                                core.utc_now(),
                                core.utc_now(),
                                2,
                            ),
                        ),
                    )
            finally:
                conn.close()

            counts = core.admin_status(db_path)["archivarixRequestCounts"]
            self.assertEqual(counts["current_utc_day"], 2)
            self.assertEqual(counts["total"], 5)

    def test_placeholder_timeout_retries_then_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                candidate = {
                    "video_id": "abc12345678",
                    "title": "Unavailable example",
                    "playlist_count": 1,
                }
                with patch("yt_library.core.playlist_placeholder_recovery_rows", return_value=[candidate]):
                    with conn:
                        core.enqueue_placeholder_recovery_targets(conn, "PLexample")
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()
            with (
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    side_effect=[
                        (None, "", "", "timeout", "The read operation timed out"),
                        (None, "", "", "not_found", ""),
                    ],
                ) as recover,
            ):
                worker._run(
                    "test-placeholder-timeout-recovered",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    retry_attempts=3,
                    retry_backoff_seconds=0,
                )

            self.assertEqual(recover.call_count, 2)
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 0)
                run = conn.execute(
                    """
                    SELECT status, processed, failed, recovery_status, request_count, message
                    FROM placeholder_recovery_worker_runs
                    WHERE run_id = ?
                    """,
                    ("test-placeholder-timeout-recovered",),
                ).fetchone()
                self.assertEqual(
                    tuple(run),
                    ("complete", 1, 0, "not_found", 2, "not found"),
                )
                logs = conn.execute(
                    """
                    SELECT level, message
                    FROM placeholder_recovery_worker_log
                    WHERE run_id = ?
                    ORDER BY id
                    """,
                    ("test-placeholder-timeout-recovered",),
                ).fetchall()
                self.assertEqual(logs[0]["level"], "warn")
                self.assertIn("attempt 1/3", logs[0]["message"])
                self.assertEqual(logs[-1]["message"], "not found")
                self.assertEqual(core.admin_status(db_path)["archivarixRequestCounts"]["total"], 2)
            finally:
                conn.close()

    def test_placeholder_found_log_uses_archivarix_status_and_recovered_title(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            video_id = "abc12345678"
            placeholder_url = f"https://www.youtube.com/watch?v={video_id}"
            recovered_title = "Recovered Archivarix title"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        video_id,
                        title=placeholder_url,
                        source="youtube_history",
                    )
                    core.enqueue_placeholder_recovery_item(
                        conn,
                        video_id=video_id,
                        current_title=placeholder_url,
                    )
            finally:
                conn.close()

            with (
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    return_value=(
                        {
                            "title": recovered_title,
                            "status": "DELETED_FULL_META",
                        },
                        "",
                        "",
                        "found",
                        "",
                    ),
                ),
            ):
                PlaceholderRecoveryWorker()._run(
                    "test-placeholder-found",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                )

            conn = core.connect(db_path)
            try:
                video = conn.execute(
                    "SELECT title FROM videos WHERE video_id = ?",
                    (video_id,),
                ).fetchone()
                log = conn.execute(
                    """
                    SELECT level, message
                    FROM placeholder_recovery_worker_log
                    WHERE run_id = ?
                    """,
                    ("test-placeholder-found",),
                ).fetchone()
                rows, total = core.worker_log_page(conn, source="placeholder")
            finally:
                conn.close()

        self.assertEqual(video["title"], recovered_title)
        self.assertEqual(tuple(log), ("found", "DELETED_FULL_META"))
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["identifier"], video_id)
        self.assertEqual(rows[0]["subject_id"], recovered_title)
        self.assertEqual(rows[0]["message"], "DELETED_FULL_META")

    def test_placeholder_timeout_exhaustion_keeps_queue_entry_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                candidate = {
                    "video_id": "abc12345678",
                    "title": "Unavailable example",
                    "playlist_count": 1,
                }
                with patch("yt_library.core.playlist_placeholder_recovery_rows", return_value=[candidate]):
                    with conn:
                        core.enqueue_placeholder_recovery_targets(conn, "PLexample")
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()
            with (
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    return_value=(None, "", "", "timeout", "The read operation timed out"),
                ) as recover,
            ):
                worker._run(
                    "test-placeholder-timeout-exhausted",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    retry_attempts=3,
                    retry_backoff_seconds=0,
                )

            self.assertEqual(recover.call_count, 3)
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                run = conn.execute(
                    """
                    SELECT status, processed, failed, recovery_status, request_count, message
                    FROM placeholder_recovery_worker_runs
                    WHERE run_id = ?
                    """,
                    ("test-placeholder-timeout-exhausted",),
                ).fetchone()
                self.assertEqual(
                    tuple(run)[:5],
                    ("blocked", 1, 1, "timeout", 3),
                )
                self.assertIn("timed out after 3 attempts", run["message"])
                block = core.external_service_block(conn, "archivarix")
                self.assertTrue(block["blocked"])
                self.assertEqual(block["reason_code"], "timeout")
                self.assertEqual(block["queue_id"], 1)
                self.assertEqual(worker.blocked_reason(), run["message"])
                self.assertEqual(core.admin_status(db_path)["archivarixRequestCounts"]["total"], 3)
            finally:
                conn.close()

    def test_placeholder_request_error_keeps_queue_entry_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                candidate = {
                    "video_id": "abc12345678",
                    "title": "Unavailable example",
                    "playlist_count": 1,
                }
                with patch("yt_library.core.playlist_placeholder_recovery_rows", return_value=[candidate]):
                    with conn:
                        core.enqueue_placeholder_recovery_targets(conn, "PLexample")
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()
            with (
                patch("yt_library.workers.archivarix_session_status", return_value=(True, "")),
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    return_value=(None, "", "", "error", "connection reset"),
                ) as recover,
            ):
                worker._run(
                    "test-placeholder-request-error",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    retry_attempts=3,
                    retry_backoff_seconds=0,
                )

            recover.assert_called_once()
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                run = conn.execute(
                    """
                    SELECT status, recovery_status, request_count, message
                    FROM placeholder_recovery_worker_runs
                    WHERE run_id = ?
                    """,
                    ("test-placeholder-request-error",),
                ).fetchone()
                self.assertEqual(tuple(run)[:3], ("blocked", "error", 1))
                self.assertIn("queue item retained", run["message"])
                self.assertEqual(
                    core.external_service_block(conn, "archivarix")["reason_code"],
                    "request_error",
                )
            finally:
                conn.close()

    def test_placeholder_authentication_block_is_persisted_and_keeps_queue_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                candidate = {
                    "video_id": "abc12345678",
                    "title": "Unavailable example",
                    "playlist_count": 1,
                }
                with patch("yt_library.core.playlist_placeholder_recovery_rows", return_value=[candidate]):
                    with conn:
                        core.enqueue_placeholder_recovery_targets(conn, "PLexample")
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()
            with (
                patch(
                    "yt_library.workers.archivarix_session_status",
                    return_value=(False, "Archivarix cookie expired"),
                ),
                patch("yt_library.workers.recover_archivarix_video") as recover,
            ):
                worker._run(
                    "test-placeholder-auth-blocked",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                )

            recover.assert_not_called()
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                run = conn.execute(
                    """
                    SELECT status, processed, failed, recovery_status, message
                    FROM placeholder_recovery_worker_runs
                    WHERE run_id = ?
                    """,
                    ("test-placeholder-auth-blocked",),
                ).fetchone()
                self.assertEqual(
                    tuple(run),
                    ("blocked", 0, 1, "authentication_error", "Archivarix cookie expired"),
                )
                block = core.external_service_block(conn, "archivarix")
                self.assertEqual(block["reason_code"], "authentication_error")
                self.assertEqual(block["queue_id"], 1)
                with conn:
                    self.assertTrue(core.clear_external_service_block(conn, "archivarix"))
                self.assertFalse(core.external_service_block(conn, "archivarix")["blocked"])
            finally:
                conn.close()

    def test_dispatcher_respects_persisted_archivarix_block_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO worker_queue(
                          subject_key, worker_type, video_id, current_title,
                          priority, created_at, updated_at
                        )
                        VALUES ('placeholder:abc12345678', 'placeholder', 'abc12345678',
                                'Unavailable example', 0, ?, ?)
                        """,
                        (core.utc_now(), core.utc_now()),
                    )
                    core.set_external_service_block(
                        conn,
                        "archivarix",
                        "rate_limited",
                        "Archivarix daily search limit reached",
                        run_id="prior-run",
                        queue_id=1,
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="youtubeStillRuns",
                        current_title="YouTube still runs",
                        metadata_source="history",
                        priority=1,
                    )
            finally:
                conn.close()

            dispatcher = WorkerQueueDispatcher()
            with (
                patch("yt_library.workers.PlaceholderRecoveryWorker.start") as start_placeholder,
                patch(
                    "yt_library.workers.fetch_watch_metadata",
                    return_value={
                        "video_id": "youtubeStillRuns",
                        "title": "YouTube still runs",
                        "duration_text": "1:00",
                        "yt_status": "OK",
                    },
                ),
                patch("yt_library.workers.fetch_new_channel_metadata_if_needed", return_value=({}, "", "")),
            ):
                dispatcher._run(
                    db_path,
                    Path(temp_dir) / "youtube-cookies.txt",
                    Path(temp_dir) / "video-thumbs",
                    "UTC",
                    Path(temp_dir) / "archivarix-cookies.txt",
                    Path(temp_dir) / "archivarix-thumbs",
                    0.0,
                    1,
                    0.0,
                    1,
                )

            start_placeholder.assert_not_called()
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 1)
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 0)
                self.assertTrue(core.external_service_block(conn, "archivarix")["blocked"])
            finally:
                conn.close()

    def test_dispatcher_logs_queue_start_blocked_by_failed_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="proxyRestart1",
                        current_title="Proxy restart",
                        metadata_source="history",
                        priority=0,
                    )
                    core.set_external_service_block(
                        conn,
                        "proxy",
                        "proxy_unavailable",
                        "SOCKS5 proxy 127.0.0.1:1081 is unavailable",
                    )
            finally:
                conn.close()

            dispatcher = WorkerQueueDispatcher()
            with patch.object(MetadataWorker, "start") as start_metadata:
                dispatcher._run(
                    db_path,
                    Path(temp_dir) / "youtube-cookies.txt",
                    Path(temp_dir) / "video-thumbs",
                    "UTC",
                    Path(temp_dir) / "archivarix-cookies.txt",
                    Path(temp_dir) / "archivarix-thumbs",
                    15.0,
                    30.0,
                    3,
                    0.0,
                    "socks5h://127.0.0.1:1081",
                )

            start_metadata.assert_not_called()
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 1)
                queue_log = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    WHERE level = 'queue error'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(queue_log["level"], "queue error")
                self.assertIn("queue start blocked", queue_log["message"].lower())
                self.assertIn("proxy is unavailable", queue_log["message"])
                self.assertNotIn("still unavailable", queue_log["message"])
            finally:
                conn.close()

    def test_reconcile_worker_runs_interrupts_only_inactive_worker_types(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    run_tables = (
                        "metadata_worker_runs",
                        "playlist_scan_worker_runs",
                        "live_history_worker_runs",
                        "placeholder_recovery_worker_runs",
                    )
                    for table, run_id in zip(
                        run_tables,
                        (
                            "active-metadata",
                            "orphaned-playlist",
                            "orphaned-history",
                            "orphaned-placeholder",
                        ),
                    ):
                        conn.execute(
                            f"""
                            INSERT INTO {table}(run_id, status, started_at, message)
                            VALUES (?, 'running', '2026-07-14T12:00:00Z', 'Started')
                            """,
                            (run_id,),
                        )
            finally:
                conn.close()

            active_metadata_worker = Mock()
            active_metadata_worker.is_running.return_value = True
            core.reconcile_worker_runs(db_path, metadata_worker=active_metadata_worker)

            conn = core.connect(db_path)
            try:
                metadata_row = conn.execute(
                    "SELECT status, finished_at, message FROM metadata_worker_runs"
                ).fetchone()
                self.assertEqual(tuple(metadata_row), ("running", None, "Started"))
                for table in run_tables[1:]:
                    row = conn.execute(
                        f"SELECT status, finished_at, message FROM {table}"
                    ).fetchone()
                    self.assertEqual(row["status"], "interrupted")
                    self.assertTrue(row["finished_at"])
                    self.assertIn("interrupted by server restart", row["message"])
            finally:
                conn.close()

    def test_dispatch_metadata_error_acknowledges_queue_entry_without_summary_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="abc12345678",
                        current_title="Example video",
                        metadata_source="provided",
                        priority=0,
                        manual=True,
                    )
            finally:
                conn.close()

            worker = MetadataWorker()
            plugin_manager = Mock()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.fetch_watch_metadata",
                    side_effect=urllib.error.URLError("offline for test"),
                ),
            ):
                worker._run(
                    "test-run",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                    plugin_manager=plugin_manager,
                )

            plugin_manager.enqueue_hook.assert_not_called()
            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_count(conn), 0)
                run = conn.execute(
                    "SELECT status, total, processed, failed FROM metadata_worker_runs WHERE run_id = 'test-run'"
                ).fetchone()
                self.assertEqual(dict(run), {"status": "complete", "total": 1, "processed": 1, "failed": 1})
                logs = conn.execute(
                    "SELECT level, message FROM metadata_worker_log WHERE run_id = 'test-run' ORDER BY id"
                ).fetchall()
                self.assertEqual(len(logs), 1)
                self.assertEqual(logs[0]["level"], "provided error")
                self.assertNotIn("Worker complete", logs[0]["message"])
                self.assertNotIn("Queued", logs[0]["message"])
            finally:
                conn.close()

    def test_metadata_worker_does_not_log_or_store_watch_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "abc12345678",
                        title="History video",
                        source="metadata",
                    )
                    conn.execute(
                        """
                        INSERT INTO history_events(
                          event_id, video_id, watch_date, time_precision,
                          watch_progress_percent
                        )
                        VALUES (
                          'history-progress', 'abc12345678', '2026-07-30',
                          'date_only', 64
                        )
                        """
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="abc12345678",
                        current_title="History video",
                        metadata_source="history",
                        priority=0,
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="def12345678",
                        current_title="Manual video",
                        metadata_source="provided",
                        priority=1,
                        manual=True,
                    )
            finally:
                conn.close()

            def watch_metadata(_opener, video_id, _thumb_dir, require_authenticated=False):
                del require_authenticated
                return {
                    "video_id": video_id,
                    "title": "History video" if video_id == "abc12345678" else "Manual video",
                    "duration_text": "1:00",
                    "watch_progress_percent": "0" if video_id == "abc12345678" else "87",
                    "watch_resume_seconds": "0",
                    "yt_status": "OK",
                }

            worker = MetadataWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.fetch_watch_metadata", side_effect=watch_metadata),
                patch("yt_library.workers.fetch_new_channel_metadata_if_needed", return_value=({}, "", "")),
            ):
                worker._run(
                    "test-watch-progress-log",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=2,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                )

            conn = core.connect(db_path)
            try:
                logs = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    WHERE run_id = 'test-watch-progress-log'
                    ORDER BY id
                    """
                ).fetchall()
                self.assertEqual(
                    [dict(row) for row in logs],
                    [
                        {
                            "level": "history",
                            "message": "ok: History video",
                        },
                        {
                            "level": "provided",
                            "message": "ok: Manual video",
                        },
                    ],
                )
                progress = conn.execute(
                    """
                    SELECT watch_progress_percent
                    FROM history_events
                    WHERE event_id = 'history-progress'
                    """
                ).fetchone()["watch_progress_percent"]
                self.assertEqual(progress, 64)
            finally:
                conn.close()

    def test_metadata_channel_uses_channel_id_in_queue_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            channel_id = "UCchannel12345678901234"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_channel(
                        conn,
                        channel_id,
                        title="Queued Channel",
                        status="terminated",
                        status_reason="Previously terminated",
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id=channel_id,
                        channel_id=channel_id,
                        channel_title=channel_id,
                        metadata_source="channel",
                        priority=0,
                        manual=True,
                    )
                queue_row = core.worker_queue_rows(conn, limit=1)[0]
                self.assertEqual(queue_row["channel_id"], channel_id)
                self.assertEqual(queue_row["known_channel_title"], "Queued Channel")
            finally:
                conn.close()

            channel_metadata = {
                "channel_id": channel_id,
                "channel": "Fetched Channel",
                "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                "channel_description": "",
                "channel_aliases": "",
                "channel_thumbnail_url": "",
                "channel_thumbnail_path": "",
                "archivarix_channel_id": "",
                "channel_status": "",
                "channel_status_reason": "",
                "channel_status_observed": True,
                "channel_subscribed": "1",
                "channel_notification_level": "all",
            }
            worker = MetadataWorker()
            plugin_manager = Mock()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.fetch_channel_metadata", return_value=channel_metadata),
            ):
                worker._run(
                    "test-channel-id-log",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                    plugin_manager=plugin_manager,
                )

            plugin_manager.enqueue_hook.assert_not_called()
            conn = core.connect(db_path)
            try:
                log = conn.execute(
                    """
                    SELECT level, video_id, message
                    FROM metadata_worker_log
                    WHERE run_id = 'test-channel-id-log'
                    """
                ).fetchone()
                self.assertEqual(log["level"], "channel")
                self.assertEqual(log["video_id"], channel_id)
                self.assertEqual(log["message"], "ok: Fetched Channel")
                channel_state = conn.execute(
                    """
                    SELECT status, status_reason, subscribed, notification_level
                    FROM channels
                    WHERE channel_id = ?
                    """,
                    (channel_id,),
                ).fetchone()
                self.assertEqual(channel_state["status"], "")
                self.assertEqual(channel_state["status_reason"], "")
                self.assertEqual(channel_state["subscribed"], 1)
                self.assertEqual(channel_state["notification_level"], "all")
                display_log = core.worker_log_snapshot(conn)["metadataLogs"][0]
                self.assertEqual(display_log["display_id"], channel_id)
                self.assertEqual(display_log["subject_title"], "Fetched Channel")
                with conn:
                    conn.execute(
                        """
                        INSERT INTO metadata_worker_log(
                          run_id, created_at, level, video_id, message
                        )
                        VALUES (
                          'legacy-channel-log', '2026-07-13T12:00:00Z',
                          'channel', 'Fetched Channel', 'legacy channel message'
                        )
                        """
                    )
                legacy_log = next(
                    row
                    for row in core.worker_log_snapshot(conn)["metadataLogs"]
                    if row["message"] == "legacy channel message"
                )
                self.assertEqual(legacy_log["display_id"], channel_id)
                self.assertEqual(legacy_log["subject_title"], "Fetched Channel")
            finally:
                conn.close()

    def test_channel_fetch_failures_preserve_termination_state(self) -> None:
        failures = {
            "transport": urllib.error.URLError("offline for test"),
            "proxy": urllib.error.URLError("proxy unavailable for test"),
            "timeout": TimeoutError("channel fetch timed out"),
            "authentication": core.YouTubeAuthenticationError(
                "YouTube login session is not accepted by YouTube",
                "operation=channel page; logged_in=false; markers=captcha",
            ),
        }
        for label, failure in failures.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "library.sqlite3"
                channel_id = f"UC_preserve_{label}"
                conn = migrated_connection(db_path)
                try:
                    with conn:
                        core.upsert_channel(
                            conn,
                            channel_id,
                            title="Terminated Channel",
                            status="terminated",
                            status_reason="Known YouTube termination",
                        )
                        core.enqueue_metadata_item(
                            conn,
                            video_id=channel_id,
                            channel_id=channel_id,
                            channel_title="Terminated Channel",
                            metadata_source="channel",
                            priority=0,
                            manual=True,
                        )
                finally:
                    conn.close()

                worker = MetadataWorker()
                with (
                    patch("yt_library.workers.load_cookie_opener", return_value=object()),
                    patch(
                        "yt_library.workers.fetch_channel_metadata",
                        side_effect=failure,
                    ),
                ):
                    worker._run(
                        f"test-channel-{label}",
                        db_path,
                        Path(temp_dir) / "cookies.txt",
                        Path(temp_dir) / "thumbs",
                        delay=0,
                        limit=1,
                        force=False,
                        stale_days=30,
                        record_summary=False,
                    )

                conn = core.connect(db_path)
                try:
                    row = conn.execute(
                        "SELECT status, status_reason FROM channels WHERE channel_id = ?",
                        (channel_id,),
                    ).fetchone()
                    self.assertEqual(
                        (row["status"], row["status_reason"]),
                        ("terminated", "Known YouTube termination"),
                    )
                finally:
                    conn.close()

    def test_manual_channel_worker_does_not_backfill_first_seen_after_handle_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            channel_id = "UCresolved123456789012"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "resolvedvid",
                        title="Resolved video",
                        channel_id=channel_id,
                    )
                    conn.execute(
                        """
                        INSERT INTO history_events(
                          event_id, video_id, watch_date, time_precision
                        )
                        VALUES (
                          'resolved-history', 'resolvedvid',
                          '2026-02-01', 'date_only'
                        )
                        """
                    )
                    core.enqueue_metadata_item(
                        conn,
                        video_id="@resolved",
                        channel_id="@resolved",
                        channel_title="@resolved",
                        metadata_source="channel",
                        manual=True,
                    )
            finally:
                conn.close()

            channel_metadata = {
                "channel_id": channel_id,
                "channel": "Resolved channel",
                "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                "channel_description": "",
                "channel_aliases": "@resolved",
                "channel_thumbnail_url": "",
                "channel_thumbnail_path": "",
                "archivarix_channel_id": "",
                "channel_status": "",
                "channel_status_reason": "",
            }
            worker = MetadataWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.fetch_channel_metadata",
                    return_value=channel_metadata,
                ),
            ):
                worker._run(
                    "test-channel-first-seen",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                )

            conn = core.connect(db_path)
            try:
                first_seen_at = conn.execute(
                    """
                    SELECT first_seen_at
                    FROM channels
                    WHERE channel_id = ?
                    """,
                    (channel_id,),
                ).fetchone()["first_seen_at"]
            finally:
                conn.close()

        self.assertIsNone(first_seen_at)

    def test_metadata_worker_fetches_new_channel_metadata_discovered_from_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_metadata_item(
                        conn,
                        video_id="abc12345678",
                        current_title="Example video",
                        metadata_source="history",
                        priority=0,
                    )
            finally:
                conn.close()

            watch_metadata = {
                "video_id": "abc12345678",
                "title": "Example video",
                "description": "",
                "channel_id": "UCnewchannel12345678901",
                "channel": "New Channel",
                "channel_url": "https://www.youtube.com/channel/UCnewchannel12345678901",
                "duration_text": "",
                "view_count": "",
                "upload_date": "",
                "thumbnail_url": "",
                "thumbnail_path": "",
                "channel_thumbnail_url": "",
                "channel_thumbnail_path": "",
                "reaction": "",
                "watch_progress_percent": "0",
                "watch_resume_seconds": "0",
                "yt_status": "OK",
            }
            channel_metadata = {
                "channel_id": "UCnewchannel12345678901",
                "channel": "New Channel",
                "channel_url": "https://www.youtube.com/channel/UCnewchannel12345678901",
                "channel_description": "About the new channel",
                "channel_aliases": "",
                "channel_thumbnail_url": "https://example.test/channel.jpg",
                "channel_thumbnail_path": "video_thumbs/UCnewchannel12345678901.jpg",
                "archivarix_channel_id": "",
                "channel_status": "",
                "channel_status_reason": "",
            }

            worker = MetadataWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.fetch_watch_metadata", return_value=watch_metadata),
                patch("yt_library.core.fetch_channel_metadata", return_value=channel_metadata) as fetch_channel,
            ):
                worker._run(
                    "test-new-channel",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                )

            fetch_channel.assert_called_once()
            conn = core.connect(db_path)
            try:
                channel = conn.execute(
                    """
                    SELECT title, description, fetch_status, fetched_at
                    FROM channels
                    WHERE channel_id = 'UCnewchannel12345678901'
                    """
                ).fetchone()
                self.assertEqual(channel["title"], "New Channel")
                self.assertEqual(channel["description"], "About the new channel")
                self.assertEqual(channel["fetch_status"], "ok")
                self.assertIsNotNone(channel["fetched_at"])
                logs = conn.execute(
                    "SELECT level, message FROM metadata_worker_log WHERE run_id = 'test-new-channel' ORDER BY id"
                ).fetchall()
                self.assertEqual([row["level"] for row in logs], ["history", "channel"])
                self.assertIn("discovered via Example video", logs[1]["message"])
            finally:
                conn.close()

    def test_metadata_worker_does_not_refetch_known_channel_discovered_from_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_channel(conn, "UCknownchannel123456789", title="Known Channel")
                    core.enqueue_metadata_item(
                        conn,
                        video_id="abc12345678",
                        current_title="Example video",
                        metadata_source="history",
                        priority=0,
                    )
            finally:
                conn.close()

            watch_metadata = {
                "video_id": "abc12345678",
                "title": "Example video",
                "description": "",
                "channel_id": "UCknownchannel123456789",
                "channel": "Known Channel",
                "channel_url": "https://www.youtube.com/channel/UCknownchannel123456789",
                "duration_text": "",
                "view_count": "",
                "upload_date": "",
                "thumbnail_url": "",
                "thumbnail_path": "",
                "channel_thumbnail_url": "",
                "channel_thumbnail_path": "",
                "reaction": "",
                "watch_progress_percent": "0",
                "watch_resume_seconds": "0",
                "yt_status": "OK",
            }

            worker = MetadataWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch("yt_library.workers.fetch_watch_metadata", return_value=watch_metadata),
                patch("yt_library.core.fetch_channel_metadata") as fetch_channel,
            ):
                worker._run(
                    "test-known-channel",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    delay=0,
                    limit=1,
                    force=False,
                    stale_days=30,
                    record_summary=False,
                )

            fetch_channel.assert_not_called()
            conn = core.connect(db_path)
            try:
                channel = conn.execute(
                    """
                    SELECT title, fetch_status, fetched_at
                    FROM channels
                    WHERE channel_id = 'UCknownchannel123456789'
                    """
                ).fetchone()
                self.assertEqual(channel["title"], "Known Channel")
                self.assertEqual(channel["fetch_status"], "")
                self.assertIsNone(channel["fetched_at"])
            finally:
                conn.close()

    def test_account_proxy_failure_stops_dispatch_and_retains_account_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "library.sqlite3"
            (root / "my_activity_cookies.txt").write_text("provided", encoding="utf-8")
            (root / "youtube_oauth_token.json").write_text("provided", encoding="utf-8")
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_account_sync_task(conn, priority=-4, manual=False)
                    core.enqueue_metadata_item(
                        conn,
                        video_id="after-account-proxy",
                        current_title="After account proxy",
                        priority=0,
                    )
                    core.record_cookie_auth_status(
                        conn,
                        "google",
                        "valid",
                        "Previous authenticated request accepted.",
                        checked_at="2026-08-19T17:00:00Z",
                    )
                account_queue_id = conn.execute(
                    "SELECT queue_id FROM worker_queue WHERE subject_key='account:sync'"
                ).fetchone()[0]
            finally:
                conn.close()

            dispatcher = WorkerQueueDispatcher()
            config = load_config(root / "config.json")
            config.update(
                {
                    "use_proxy": True,
                    "proxy": "socks5h://127.0.0.1:1081",
                    "job_dispatch_delay_seconds": 0,
                }
            )
            with patch(
                "yt_library.workers.fetch_my_activity_pages",
                side_effect=network.ProxyUnavailableError(
                    "SOCKS5 proxy 127.0.0.1:1081 is unavailable"
                ),
            ) as fetch_activity, patch(
                "yt_library.workers.build_youtube_data_service"
            ) as build_data_service:
                dispatcher._run(
                    db_path,
                    root / "youtube-cookies.txt",
                    root / "video-thumbs",
                    "UTC",
                    root / "archivarix-cookies.txt",
                    root / "archivarix-thumbs",
                    proxy_url=config["proxy"],
                    config=config,
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "account"), 1)
                self.assertEqual(core.worker_queue_type_count(conn, "metadata"), 1)
                block = core.external_service_block(conn, "proxy")
                self.assertTrue(block["blocked"])
                self.assertEqual(block["queue_id"], account_queue_id)
                messages = [
                    row["message"]
                    for row in conn.execute(
                        "SELECT message FROM metadata_worker_log ORDER BY id"
                    )
                ]
                auth_status = core.cookie_auth_statuses(conn)["google"]
            finally:
                conn.close()

            self.assertEqual(fetch_activity.call_count, 1)
            build_data_service.assert_not_called()
            self.assertTrue(
                any("pending items were retained" in message for message in messages)
            )
            self.assertEqual(auth_status["status"], "valid")
            self.assertEqual(auth_status["checked_at"], "2026-08-19T17:00:00Z")

    def test_account_failure_is_consumed_until_the_next_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "library.sqlite3"
            (root / "my_activity_cookies.txt").write_text("provided", encoding="utf-8")
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_account_sync_task(conn, priority=-4, manual=False)
            finally:
                conn.close()

            dispatcher = WorkerQueueDispatcher()
            config = load_config(root / "config.json")
            with patch(
                "yt_library.workers.fetch_my_activity_pages",
                side_effect=workers.MyActivityError("refresh the cookie export"),
            ) as fetch_activity:
                dispatcher._run(
                    db_path,
                    root / "youtube-cookies.txt",
                    root / "video-thumbs",
                    "UTC",
                    root / "archivarix-cookies.txt",
                    root / "archivarix-thumbs",
                    config=config,
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "account"), 0)
                self.assertFalse(core.external_service_block(conn, "proxy")["blocked"])
                auth_status = core.cookie_auth_statuses(conn)["google"]
                self.assertEqual(auth_status["status"], "rejected")
                self.assertIn("refresh the cookie export", auth_status["message"])
            finally:
                conn.close()
            self.assertEqual(fetch_activity.call_count, 1)

    def test_playlist_worker_targets_one_queue_row_and_uses_its_cookie_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            default_cookie = Path(temp_dir) / "default-cookies.txt"
            selected_cookie = Path(temp_dir) / "selected-cookies.txt"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.executemany(
                        "INSERT INTO playlists(playlist_id, title) VALUES (?, ?)",
                        (("PLother", "Other"), ("PLselected", "Selected")),
                    )
                    core.enqueue_playlist_scan_item(conn, "PLother", priority=0)
                    core.enqueue_playlist_scan_item(
                        conn,
                        "PLselected",
                        priority=1,
                        manual=True,
                        payload={"cookie_file": str(selected_cookie)},
                    )
                selected_queue_id = int(
                    conn.execute(
                        "SELECT queue_id FROM worker_queue WHERE playlist_id = 'PLselected'"
                    ).fetchone()["queue_id"]
                )
            finally:
                conn.close()

            worker = PlaylistScanWorker()
            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()) as load_opener,
                patch("yt_library.workers.request_text", return_value="playlist header"),
                patch(
                    "yt_library.workers.extract_playlist_metadata",
                    return_value={"video_count": 1, "has_video_count": True},
                ),
                patch(
                    "yt_library.workers.scan_playlist_ytdlp",
                    return_value=([{"video_id": "selectedvid1"}], {}),
                ) as scan_ytdlp,
                patch("yt_library.workers.save_playlist_scan", return_value=(1, 0)),
                patch(
                    "yt_library.workers.enqueue_playlist_metadata_targets",
                    return_value={"queued_count": 0},
                ),
                patch(
                    "yt_library.workers.enqueue_placeholder_recovery_targets",
                    return_value={"inserted": 0},
                ),
            ):
                worker._run(
                    "targeted-playlist-run",
                    db_path,
                    default_cookie,
                    delay=0,
                    limit=1,
                    force=False,
                    record_summary=False,
                    queue_id=selected_queue_id,
                )

            conn = core.connect(db_path)
            try:
                queued = core.playlist_scan_queue_rows(conn)
            finally:
                conn.close()

        self.assertEqual([row["playlist_id"] for row in queued], ["PLother"])
        load_opener.assert_called_once_with(selected_cookie, "")
        scan_ytdlp.assert_called_once_with("PLselected", selected_cookie, "")

    def test_placeholder_worker_honors_manual_no_api_options_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            default_cookie = Path(temp_dir) / "default-cookies.txt"
            default_thumbs = Path(temp_dir) / "default-thumbs"
            selected_cookie = Path(temp_dir) / "selected-cookies.txt"
            selected_thumbs = Path(temp_dir) / "selected-thumbs"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO playlists(playlist_id, title) VALUES ('PLone', 'One')"
                    )
                    core.upsert_video(
                        conn,
                        "recoveropts1",
                        title="Recovery options",
                        is_playable=False,
                        source="test",
                    )
                    conn.execute(
                        """
                        INSERT INTO playlist_items(
                          playlist_id, position, video_id, membership_state
                        ) VALUES ('PLone', 1, 'recoveropts1', 'retained_unavailable')
                        """
                    )
                    core.enqueue_placeholder_recovery_item(
                        conn,
                        video_id="recoveropts1",
                        playlist_id="PLone",
                        current_title="Recovery options",
                        manual=True,
                        task_type="thumbnail",
                        payload={
                            "cookie_file": str(selected_cookie),
                            "thumbnail_dir": str(selected_thumbs),
                            "refresh_metadata": False,
                            "no_api": True,
                            "delay_seconds": 1.5,
                        },
                    )
                queue_id = int(
                    conn.execute("SELECT queue_id FROM worker_queue").fetchone()["queue_id"]
                )
            finally:
                conn.close()

            worker = PlaceholderRecoveryWorker()
            with (
                patch("yt_library.workers.archivarix_session_status") as session_status,
                patch("yt_library.workers.load_cookie_opener", return_value=object()) as load_opener,
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    return_value=(None, "", "", "not_found", ""),
                ) as recover,
            ):
                worker._run(
                    "manual-placeholder-options",
                    db_path,
                    default_cookie,
                    default_thumbs,
                    queue_id=queue_id,
                    retry_attempts=4,
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 0)
                run = conn.execute(
                    """
                    SELECT status, recovery_status, request_count
                    FROM placeholder_recovery_worker_runs
                    WHERE run_id = 'manual-placeholder-options'
                    """
                ).fetchone()
            finally:
                conn.close()

        session_status.assert_not_called()
        load_opener.assert_called_once_with(selected_cookie, "")
        self.assertEqual(recover.call_args.args[:2], ("recoveropts1", selected_thumbs))
        self.assertFalse(recover.call_args.kwargs["refresh_metadata"])
        self.assertTrue(recover.call_args.kwargs["no_api"])
        self.assertEqual(recover.call_args.kwargs["delay"], 1.5)
        self.assertEqual(tuple(run), ("complete", "not_found", 1))

    def test_manual_no_api_error_does_not_block_later_archivarix_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_placeholder_recovery_item(
                        conn,
                        video_id="thumbnailerr1",
                        manual=True,
                        task_type="thumbnail",
                        payload={"no_api": True},
                    )
                queue_id = int(
                    conn.execute("SELECT queue_id FROM worker_queue").fetchone()["queue_id"]
                )
            finally:
                conn.close()

            with (
                patch("yt_library.workers.load_cookie_opener", return_value=object()),
                patch(
                    "yt_library.workers.recover_archivarix_video",
                    return_value=(None, "", "", "error", "thumbnail request failed"),
                ),
            ):
                PlaceholderRecoveryWorker()._run(
                    "manual-thumbnail-error",
                    db_path,
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    queue_id=queue_id,
                )

            conn = core.connect(db_path)
            try:
                self.assertEqual(core.worker_queue_type_count(conn, "placeholder"), 0)
                self.assertFalse(core.external_service_block(conn, "archivarix")["blocked"])
                run = conn.execute(
                    """
                    SELECT status, recovery_status, failed, message
                    FROM placeholder_recovery_worker_runs
                    WHERE run_id = 'manual-thumbnail-error'
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(tuple(run)[:3], ("complete", "error", 1))
        self.assertIn("Direct thumbnail recovery failed", run["message"])

    def test_dispatcher_can_select_targeted_thumbnail_work_through_api_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_placeholder_recovery_item(
                        conn,
                        video_id="normalrecover1",
                        task_type="recover",
                    )
                    core.enqueue_placeholder_recovery_item(
                        conn,
                        video_id="thumbnailonly1",
                        task_type="thumbnail",
                        manual=True,
                        payload={"no_api": True},
                    )
                rows = {
                    row["video_id"]: int(row["queue_id"])
                    for row in core.worker_queue_rows(conn)
                }
            finally:
                conn.close()

            selected = WorkerQueueDispatcher()._next_row(
                db_path,
                worker_types=("placeholder",),
                included_queue_ids=frozenset(rows.values()),
                placeholder_thumbnail_only=True,
            )

        self.assertEqual(selected["video_id"], "thumbnailonly1")
        self.assertEqual(selected["queue_id"], rows["thumbnailonly1"])


if __name__ == "__main__":
    unittest.main()
