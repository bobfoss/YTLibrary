from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from yt_library import core, network, request_pacing
from yt_library.config import load_config
from yt_library.queries import video_detail_data
from yt_library.workers import (
    MetadataWorker,
    PlaceholderRecoveryWorker,
    WorkerQueueDispatcher,
)

from tests.support import migrated_connection


class CoreHelperTests(unittest.TestCase):
    def test_socks5_proxy_parser_supports_remote_dns_and_credentials(self) -> None:
        proxy = network.parse_socks5_proxy_url(
            "socks5h://user%20name:pass%2Fword@127.0.0.1:1080"
        )

        self.assertIsNotNone(proxy)
        self.assertEqual(proxy.host, "127.0.0.1")
        self.assertEqual(proxy.port, 1080)
        self.assertTrue(proxy.remote_dns)
        self.assertEqual(proxy.username, "user name")
        self.assertEqual(proxy.password, "pass/word")
        self.assertFalse(
            network.parse_socks5_proxy_url("socks5://localhost:1081").remote_dns
        )
        self.assertIsNone(network.parse_socks5_proxy_url(""))

    def test_socks5_proxy_parser_rejects_unsupported_or_ambiguous_urls(self) -> None:
        invalid_values = (
            "http://127.0.0.1:1080",
            "socks5://127.0.0.1",
            "socks5://127.0.0.1:1080/path",
            "socks5://:1080",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    network.parse_socks5_proxy_url(value)

    def test_socks5_connection_routes_destination_through_pysocks(self) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        fake_socket = object()

        class FakeSocks:
            SOCKS5 = 2

            @staticmethod
            def create_connection(*args, **kwargs):
                calls.append((args, kwargs))
                return fake_socket

        proxy = network.parse_socks5_proxy_url(
            "socks5h://proxy-user:proxy-pass@localhost:1080"
        )
        connection = network._Socks5HTTPConnection(
            "www.youtube.com",
            port=80,
            timeout=12,
            socks_module=FakeSocks,
            proxy=proxy,
        )

        connection.connect()

        self.assertIs(connection.sock, fake_socket)
        self.assertEqual(calls[0][0], (("www.youtube.com", 80),))
        self.assertEqual(calls[0][1]["proxy_type"], FakeSocks.SOCKS5)
        self.assertEqual(calls[0][1]["proxy_addr"], "localhost")
        self.assertEqual(calls[0][1]["proxy_port"], 1080)
        self.assertTrue(calls[0][1]["proxy_rdns"])
        self.assertEqual(calls[0][1]["proxy_username"], "proxy-user")
        self.assertEqual(calls[0][1]["proxy_password"], "proxy-pass")

    def test_socks5_connection_promotes_proxy_connection_failure(self) -> None:
        class ProxyConnectionError(OSError):
            pass

        class FakeSocks:
            SOCKS5 = 2

            @staticmethod
            def create_connection(*_args, **_kwargs):
                raise ProxyConnectionError(
                    "Error connecting to SOCKS5 proxy "
                    "socks5h://secret-user:secret-pass@127.0.0.1:1081"
                )

        proxy = network.parse_socks5_proxy_url(
            "socks5h://secret-user:secret-pass@127.0.0.1:1081"
        )
        connection = network._Socks5HTTPConnection(
            "www.youtube.com",
            port=80,
            timeout=12,
            socks_module=FakeSocks,
            proxy=proxy,
        )

        with self.assertRaises(network.ProxyUnavailableError) as raised:
            connection.connect()

        self.assertIn("127.0.0.1:1081", str(raised.exception))
        self.assertNotIn("secret-user", str(raised.exception))
        self.assertNotIn("secret-pass", str(raised.exception))
        self.assertIsInstance(
            network.proxy_unavailable_error(
                ConnectionResetError("connection reset"),
                "socks5h://127.0.0.1:1081",
            ),
            network.ProxyUnavailableError,
        )

    def test_proxy_failure_classifier_ignores_non_proxy_request_errors(self) -> None:
        self.assertIsNone(
            network.proxy_unavailable_error(
                urllib.error.URLError("YouTube request timed out"),
                "socks5h://127.0.0.1:1081",
            )
        )
        self.assertIsNone(
            network.proxy_unavailable_error(
                RuntimeError("Error connecting to SOCKS5 proxy"),
                "",
            )
        )

    def test_proxy_failure_classifier_recognizes_ytdlp_proxy_errors(self) -> None:
        class ProxyError(Exception):
            pass

        ProxyError.__module__ = "yt_dlp.networking.exceptions"
        error = network.proxy_unavailable_error(
            ProxyError("proxy transport failed"),
            "socks5h://127.0.0.1:1081",
        )

        self.assertIsInstance(error, network.ProxyUnavailableError)
        self.assertIn("127.0.0.1:1081", str(error))

    def test_socks5_https_handler_uses_its_tls_context(self) -> None:
        class FakeSocks:
            SOCKS5 = 2

        proxy = network.parse_socks5_proxy_url("socks5h://localhost:1080")
        handler = network._Socks5HTTPSHandler(FakeSocks, proxy)
        request = urllib.request.Request("https://www.youtube.com/")

        with patch.object(handler, "do_open", return_value="response") as do_open:
            response = handler.https_open(request)

        self.assertEqual(response, "response")
        do_open.assert_called_once_with(
            handler._connection,
            request,
            context=handler._context,
        )

    def test_ytdlp_proxy_options_preserve_supported_proxy_url(self) -> None:
        self.assertEqual(
            network.ytdlp_proxy_options("socks5://127.0.0.1:1080"),
            {"proxy": "socks5://127.0.0.1:1080"},
        )
        self.assertEqual(network.ytdlp_proxy_options(""), {})

    def test_proxy_probe_connects_through_configured_socks5_proxy(self) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        class FakeSocket:
            closed = False

            def close(self) -> None:
                self.closed = True

        fake_socket = FakeSocket()

        class FakeSocks:
            SOCKS5 = 2

            @staticmethod
            def create_connection(*args, **kwargs):
                calls.append((args, kwargs))
                return fake_socket

        with patch("yt_library.network._load_socks_module", return_value=FakeSocks):
            available, message = network.probe_socks5_proxy(
                "socks5h://proxy-user:proxy-pass@localhost:1081",
                timeout=2,
            )

        self.assertTrue(available)
        self.assertEqual(message, "")
        self.assertEqual(calls[0][0], (("www.youtube.com", 443),))
        self.assertEqual(calls[0][1]["timeout"], 2)
        self.assertTrue(calls[0][1]["proxy_rdns"])
        self.assertTrue(fake_socket.closed)

    def test_proxy_probe_reports_sanitized_connection_failure(self) -> None:
        class ProxyConnectionError(OSError):
            pass

        class FakeSocks:
            SOCKS5 = 2

            @staticmethod
            def create_connection(*_args, **_kwargs):
                raise ProxyConnectionError(
                    "Error connecting to SOCKS5 proxy "
                    "socks5h://secret-user:secret-pass@127.0.0.1:1081"
                )

        with patch("yt_library.network._load_socks_module", return_value=FakeSocks):
            available, message = network.probe_socks5_proxy(
                "socks5h://secret-user:secret-pass@127.0.0.1:1081"
            )

        self.assertFalse(available)
        self.assertIn("127.0.0.1:1081", message)
        self.assertNotIn("secret-user", message)
        self.assertNotIn("secret-pass", message)

    def test_missing_pysocks_is_a_proxy_outage(self) -> None:
        with patch(
            "yt_library.network.importlib.import_module",
            side_effect=ImportError("No module named 'socks'"),
        ):
            with self.assertRaises(network.ProxyUnavailableError) as raised:
                network.socks5_proxy_handlers("socks5h://127.0.0.1:1081")

        self.assertIn("requires PySocks", str(raised.exception))

    def test_metadata_worker_passes_proxy_to_poll_run(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        captured_args: list[object] = []
        worker = MetadataWorker()

        def hold_worker(*args) -> None:
            captured_args.extend(args)
            entered.set()
            release.wait(2)

        with patch.object(worker, "_run", side_effect=hold_worker):
            result = worker.start(
                Path("library.sqlite3"),
                Path("cookies.txt"),
                Path("thumbs"),
                delay=0,
                limit=1,
                force=False,
                stale_days=30,
                proxy_url="socks5h://127.0.0.1:1080",
            )
            self.assertTrue(result["started"])
            self.assertTrue(entered.wait(1))
            self.assertEqual(captured_args[-1], "socks5h://127.0.0.1:1080")
            release.set()
            deadline = time.time() + 1
            while worker.is_alive() and time.time() < deadline:
                time.sleep(0.01)
            self.assertFalse(worker.is_alive())

    def test_dispatcher_passes_general_proxy_to_queue_run(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        captured_args: list[object] = []
        dispatcher = WorkerQueueDispatcher()

        def hold_dispatcher(*args) -> None:
            captured_args.extend(args)
            entered.set()
            release.wait(2)

        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_config(Path(temp_dir) / "config.json")
            config["use_proxy"] = True
            config["proxy"] = "socks5h://127.0.0.1:1080"
            core.migrate_database(Path(temp_dir) / "library.sqlite3")
            with (
                patch.object(dispatcher, "_run", side_effect=hold_dispatcher),
                patch(
                    "yt_library.workers.probe_socks5_proxy",
                    return_value=(True, "Proxy available"),
                ),
            ):
                result = dispatcher.start(
                    Path(temp_dir) / "library.sqlite3",
                    Path(temp_dir) / "cookies.txt",
                    Path(temp_dir) / "thumbs",
                    config,
                )
                self.assertTrue(result["started"])
                self.assertTrue(entered.wait(1))
                self.assertEqual(captured_args[-3], "socks5h://127.0.0.1:1080")
                self.assertEqual(captured_args[-2]["proxy"], "socks5h://127.0.0.1:1080")
                self.assertIsNone(captured_args[-1])
                release.set()
                deadline = time.time() + 1
                while dispatcher.is_alive() and time.time() < deadline:
                    time.sleep(0.01)
                self.assertFalse(dispatcher.is_alive())

    def test_dispatcher_start_clears_proxy_hold_after_successful_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.set_external_service_block(
                        conn,
                        "proxy",
                        "proxy_unavailable",
                        "SOCKS5 proxy is unavailable",
                    )
            finally:
                conn.close()
            config = load_config(Path(temp_dir) / "config.json")
            config["use_proxy"] = True
            config["proxy"] = "socks5h://127.0.0.1:1081"
            dispatcher = WorkerQueueDispatcher()

            with (
                patch(
                    "yt_library.workers.probe_socks5_proxy",
                    return_value=(True, ""),
                ) as probe,
                patch.object(
                    dispatcher,
                    "_start_background",
                    return_value={"started": True},
                ) as start_background,
            ):
                result = dispatcher.start(
                    db_path,
                    Path(temp_dir) / "youtube-cookies.txt",
                    Path(temp_dir) / "video-thumbs",
                    config,
                )

            self.assertTrue(result["started"])
            probe.assert_called_once_with("socks5h://127.0.0.1:1081")
            start_background.assert_called_once()
            conn = core.connect(db_path)
            try:
                self.assertFalse(core.external_service_block(conn, "proxy")["blocked"])
                recovery_log = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(recovery_log["level"], "queue info")
                self.assertIn("Proxy connectivity restored", recovery_log["message"])
            finally:
                conn.close()

    def test_dispatcher_start_probes_configured_proxy_without_existing_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()
            config = load_config(Path(temp_dir) / "config.json")
            config["use_proxy"] = True
            config["proxy"] = "socks5h://127.0.0.1:1081"
            dispatcher = WorkerQueueDispatcher()

            with (
                patch(
                    "yt_library.workers.probe_socks5_proxy",
                    return_value=(True, ""),
                ) as probe,
                patch.object(
                    dispatcher,
                    "_start_background",
                    return_value={"started": True},
                ) as start_background,
            ):
                result = dispatcher.start(
                    db_path,
                    Path(temp_dir) / "youtube-cookies.txt",
                    Path(temp_dir) / "video-thumbs",
                    config,
                )

            self.assertTrue(result["started"])
            probe.assert_called_once_with("socks5h://127.0.0.1:1081")
            start_background.assert_called_once()
            conn = core.connect(db_path)
            try:
                self.assertFalse(core.external_service_block(conn, "proxy")["blocked"])
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM metadata_worker_log"
                    ).fetchone()[0],
                    0,
                )
            finally:
                conn.close()

    def test_dispatcher_start_sets_proxy_hold_after_fresh_failed_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()
            config = load_config(Path(temp_dir) / "config.json")
            config["use_proxy"] = True
            config["proxy"] = "socks5h://127.0.0.1:1081"
            dispatcher = WorkerQueueDispatcher()
            failure = "SOCKS5 proxy 127.0.0.1:1081 is unavailable"

            with (
                patch(
                    "yt_library.workers.probe_socks5_proxy",
                    return_value=(False, failure),
                ) as probe,
                patch.object(dispatcher, "_start_background") as start_background,
            ):
                result = dispatcher.start(
                    db_path,
                    Path(temp_dir) / "youtube-cookies.txt",
                    Path(temp_dir) / "video-thumbs",
                    config,
                )

            self.assertFalse(result["started"])
            self.assertTrue(result["blocked"])
            self.assertEqual(result["message"], failure)
            probe.assert_called_once_with("socks5h://127.0.0.1:1081")
            start_background.assert_not_called()
            conn = core.connect(db_path)
            try:
                block = core.external_service_block(conn, "proxy")
                self.assertTrue(block["blocked"])
                self.assertEqual(block["reason_code"], "proxy_unavailable")
                self.assertEqual(block["message"], failure)
            finally:
                conn.close()

    def test_dispatcher_start_retains_proxy_hold_after_failed_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.set_external_service_block(
                        conn,
                        "proxy",
                        "proxy_unavailable",
                        "SOCKS5 proxy is unavailable",
                    )
            finally:
                conn.close()
            config = load_config(Path(temp_dir) / "config.json")
            config["use_proxy"] = True
            config["proxy"] = "socks5h://127.0.0.1:1081"
            dispatcher = WorkerQueueDispatcher()
            failure = "SOCKS5 proxy 127.0.0.1:1081 is unavailable"

            with (
                patch(
                    "yt_library.workers.probe_socks5_proxy",
                    return_value=(False, failure),
                ),
                patch.object(dispatcher, "_start_background") as start_background,
            ):
                result = dispatcher.start(
                    db_path,
                    Path(temp_dir) / "youtube-cookies.txt",
                    Path(temp_dir) / "video-thumbs",
                    config,
                )

            self.assertFalse(result["started"])
            self.assertTrue(result["blocked"])
            self.assertEqual(result["message"], failure)
            start_background.assert_not_called()
            conn = core.connect(db_path)
            try:
                self.assertTrue(core.external_service_block(conn, "proxy")["blocked"])
                failure_log = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(failure_log["level"], "queue error")
                self.assertIn("proxy is unavailable", failure_log["message"])
            finally:
                conn.close()

    def test_placeholder_worker_passes_general_proxy_to_run(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        captured_args: list[object] = []
        worker = PlaceholderRecoveryWorker()

        def hold_worker(*args) -> None:
            captured_args.extend(args)
            entered.set()
            release.wait(2)

        with patch.object(worker, "_run", side_effect=hold_worker):
            result = worker.start(
                Path("library.sqlite3"),
                Path("archivarix-cookies.txt"),
                Path("thumbs"),
                proxy_url="socks5h://127.0.0.1:1080",
            )
            self.assertTrue(result["started"])
            self.assertTrue(entered.wait(1))
            self.assertEqual(captured_args[-1], "socks5h://127.0.0.1:1080")
            release.set()
            deadline = time.time() + 1
            while worker.is_alive() and time.time() < deadline:
                time.sleep(0.01)
            self.assertFalse(worker.is_alive())

    def test_local_asset_path_maps_external_thumbnail_storage_to_web_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            app_root = temp_root / "release"
            external_video_thumbs = temp_root / "storage" / "video_thumbs"
            thumbnail = external_video_thumbs / "video-id.jpg"
            thumbnail.parent.mkdir(parents=True)
            thumbnail.write_bytes(b"thumbnail")

            with (
                patch.object(core, "ROOT", app_root),
                patch.object(core, "DEFAULT_VIDEO_THUMB_DIR", external_video_thumbs),
            ):
                self.assertEqual(
                    core.local_asset_path(thumbnail),
                    "video_thumbs/video-id.jpg",
                )

    def test_request_pacer_spaces_request_starts(self) -> None:
        clock = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        pacer = core.RequestPacer(
            6.0,
            10.0,
            monotonic=lambda: clock[0],
            sleep=sleep,
            uniform=lambda minimum, maximum: 8.0,
        )

        pacer.wait()
        clock[0] = 2.0
        pacer.wait()

        self.assertEqual(sleeps, [6.0])
        self.assertEqual(clock[0], 8.0)

    def test_youtube_request_url_matching_excludes_unrelated_hosts(self) -> None:
        self.assertTrue(core.is_youtube_request_url("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(core.is_youtube_request_url("https://i.ytimg.com/vi/abc/hqdefault.jpg"))
        self.assertTrue(core.is_youtube_request_url("https://rr1.googlevideo.com/videoplayback"))
        self.assertTrue(core.is_youtube_request_url("https://yt3.ggpht.com/avatar"))
        self.assertTrue(core.is_youtube_request_url("https://yt3.googleusercontent.com/avatar"))
        self.assertFalse(core.is_youtube_request_url("https://youtube.com.example.test/watch?v=abc"))
        self.assertFalse(core.is_youtube_request_url("https://archivarix.com/search"))

    def test_archivarix_request_url_matching_includes_api_and_archive_hosts(self) -> None:
        self.assertTrue(core.is_archivarix_request_url("https://tube.archivarix.net/api/search"))
        self.assertTrue(core.is_archivarix_request_url("https://web.archive.org/web/example"))
        self.assertFalse(core.is_archivarix_request_url("https://archivarix.net.example.test/search"))
        self.assertFalse(core.is_archivarix_request_url("https://www.youtube.com/watch?v=abc"))

    def test_request_pacing_routes_both_sites_to_one_global_pacer(self) -> None:
        opener = Mock()
        request_pacer = Mock()
        with patch.object(request_pacing, "_request_pacer", request_pacer):
            core.open_with_request_pacing(
                opener,
                urllib.request.Request("https://yt3.ggpht.com/avatar"),
                timeout=12,
            )
            core.open_with_request_pacing(
                opener,
                urllib.request.Request("https://tube.archivarix.net/api/search"),
                timeout=18,
            )
            core.open_with_request_pacing(
                opener,
                urllib.request.Request("https://example.test/resource"),
                timeout=6,
            )

        self.assertEqual(request_pacer.wait.call_count, 2)
        self.assertEqual(opener.open.call_count, 3)

    def test_ytdlp_urlopen_uses_the_shared_request_pacer(self) -> None:
        class BaseYoutubeDL:
            def __init__(self, options):
                self.options = options

            def urlopen(self, request):
                return f"opened:{request}"

        class FakeYtdlpModule:
            YoutubeDL = BaseYoutubeDL

        request_pacer = Mock()
        with patch.object(request_pacing, "_request_pacer", request_pacer):
            ydl = core.request_paced_youtube_dl(
                FakeYtdlpModule,
                {"quiet": True},
            )
            result = ydl.urlopen("playlist-request")

        request_pacer.wait.assert_called_once_with()
        self.assertEqual(result, "opened:playlist-request")
        self.assertEqual(ydl.options, {"quiet": True})

    def test_placeholder_recovery_exposes_its_persisted_run_id(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        worker = PlaceholderRecoveryWorker()

        def hold_worker(*_args) -> None:
            entered.set()
            release.wait(2)

        with patch.object(worker, "_run", side_effect=hold_worker):
            result = worker.start(Path("library.sqlite3"), Path("cookies.txt"), Path("thumbs"))
            self.assertTrue(entered.wait(1))
            self.assertTrue(result["started"])
            self.assertTrue(result["run_id"])

            stopped = worker.stop()
            self.assertEqual(stopped["run_id"], result["run_id"])
            release.set()
            deadline = time.time() + 1
            while worker.is_alive() and time.time() < deadline:
                time.sleep(0.01)
            self.assertFalse(worker.is_alive())

    def test_thread_worker_lifecycle_rejects_duplicate_start_and_reports_stopping(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        worker = MetadataWorker()

        def hold_worker(*_args) -> None:
            entered.set()
            release.wait(2)

        with patch.object(worker, "_run", side_effect=hold_worker):
            first = worker.start(
                Path("library.sqlite3"),
                Path("cookies.txt"),
                Path("thumbs"),
                delay=0,
                limit=1,
                force=False,
                stale_days=30,
            )
            self.assertTrue(entered.wait(1))
            duplicate = worker.start(
                Path("library.sqlite3"),
                Path("cookies.txt"),
                Path("thumbs"),
                delay=0,
                limit=1,
                force=False,
                stale_days=30,
            )

            self.assertTrue(first["started"])
            self.assertFalse(duplicate["started"])
            self.assertEqual(duplicate["run_id"], first["run_id"])
            self.assertTrue(worker.is_running())

            stopped = worker.stop()
            self.assertTrue(stopped["stopping"])
            self.assertEqual(stopped["run_id"], first["run_id"])
            self.assertFalse(worker.is_running())
            self.assertTrue(worker.is_stopping())
            self.assertTrue(worker.is_alive())

            release.set()
            deadline = time.time() + 1
            while worker.is_alive() and time.time() < deadline:
                time.sleep(0.01)
            self.assertFalse(worker.is_alive())

    def test_archivarix_recovery_does_not_start_when_stop_is_requested(self) -> None:
        stop_event = threading.Event()
        stop_event.set()
        with (
            patch("yt_library.core.cache_archivarix_thumbnail") as cache_thumbnail,
            patch("yt_library.core.archivarix_lookup_video") as lookup_video,
        ):
            result = core.recover_archivarix_video(
                "abc12345678",
                Path("unused"),
                object(),
                stop_event=stop_event,
            )

        self.assertEqual(result[3:], ("stopped", "Stop requested"))
        cache_thumbnail.assert_not_called()
        lookup_video.assert_not_called()

    def test_archivarix_session_status_requires_a_current_session_cookie(self) -> None:
        class Cookie:
            def __init__(self, expires: int | None) -> None:
                self.name = "__Secure-better-auth.session_token"
                self.domain = "tube.archivarix.net"
                self.expires = expires

        with patch("yt_library.core.load_cookie_jar", return_value=[Cookie(200)]):
            self.assertEqual(core.archivarix_session_status(Path("unused"), now=100), (True, ""))
        with patch("yt_library.core.load_cookie_jar", return_value=[Cookie(100)]):
            valid, message = core.archivarix_session_status(Path("unused"), now=100)
            self.assertFalse(valid)
            self.assertIn("expired", message)
        with patch("yt_library.core.load_cookie_jar", return_value=[]):
            valid, message = core.archivarix_session_status(Path("unused"), now=100)
            self.assertFalse(valid)
            self.assertIn("missing", message)

    def test_archivarix_quota_text_is_detected(self) -> None:
        self.assertEqual(
            core.archivarix_quota_message_from_text("Limit reached: 500 searches per day"),
            "Archivarix daily search limit reached",
        )
        self.assertEqual(core.archivarix_quota_message_from_text("ordinary response"), "")

    def test_archivarix_search_uses_supplied_network_opener(self) -> None:
        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read() -> bytes:
                return json.dumps(
                    {"data": {"videos": [{"videoId": "proxytest1"}]}}
                ).encode("utf-8")

        class Opener:
            def __init__(self) -> None:
                self.calls: list[tuple[object, object]] = []

            def open(self, request, timeout=None):
                self.calls.append((request, timeout))
                return Response()

        opener = Opener()
        videos = core.archivarix_search_deleted(
            "Proxy test",
            page_size=25,
            opener=opener,
        )

        self.assertEqual(videos, [{"videoId": "proxytest1"}])
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0][1], 30)
        self.assertIn("tube.archivarix.net/api/fts", opener.calls[0][0].full_url)

    def test_archivarix_timeout_errors_are_classified(self) -> None:
        self.assertTrue(core.archivarix_timeout_error(TimeoutError("read timed out")))
        self.assertTrue(
            core.archivarix_timeout_error(
                urllib.error.URLError(TimeoutError("connection timed out"))
            )
        )
        self.assertFalse(core.archivarix_timeout_error(OSError("connection reset")))

    def test_youtube_session_status_requires_a_current_login_cookie(self) -> None:
        class Cookie:
            def __init__(self, name: str, domain: str, expires: int | None) -> None:
                self.name = name
                self.domain = domain
                self.expires = expires

        with patch(
            "yt_library.core.load_cookie_jar",
            return_value=[Cookie("SID", ".youtube.com", 200)],
        ):
            self.assertEqual(core.youtube_session_status(Path("unused"), now=100), (True, ""))
        with patch(
            "yt_library.core.load_cookie_jar",
            return_value=[Cookie("SID", ".youtube.com", 100)],
        ):
            valid, message = core.youtube_session_status(Path("unused"), now=100)
            self.assertFalse(valid)
            self.assertIn("expired", message)
        with patch("yt_library.core.load_cookie_jar", return_value=[]):
            valid, message = core.youtube_session_status(Path("unused"), now=100)
            self.assertFalse(valid)
            self.assertIn("missing", message)
        with (
            patch(
                "yt_library.core.load_cookie_jar",
                return_value=[Cookie("SID", ".youtube.com", 200)],
            ),
            patch("yt_library.core.load_cookie_opener", return_value=object()),
            patch(
                "yt_library.core.request_text",
                return_value="Watch history isn't viewable when signed out",
            ),
        ):
            valid, message = core.youtube_session_status(Path("unused"), now=100, verify_remote=True)
            self.assertFalse(valid)
            self.assertIn("not accepted", message)

    def test_youtube_page_authentication_uses_logged_in_state(self) -> None:
        logged_in = 'ytcfg.set({"LOGGED_IN":true}); ServiceLogin recaptcha'
        logged_out = 'ytcfg.set({"LOGGED_IN":false});'
        self.assertIs(core.youtube_page_login_state(logged_in), True)
        self.assertIs(core.youtube_page_login_state(logged_out), False)
        self.assertIsNone(core.youtube_page_login_state("ServiceLogin"))
        self.assertTrue(core.youtube_page_is_authenticated(logged_in))
        self.assertFalse(core.youtube_page_is_authenticated(logged_out))
        self.assertFalse(core.youtube_page_is_authenticated("ServiceLogin"))
        self.assertFalse(core.youtube_page_requires_login(logged_in))
        self.assertTrue(core.youtube_page_requires_login(logged_out))
        self.assertTrue(core.youtube_page_requires_login("ServiceLogin"))
        self.assertFalse(core.youtube_page_requires_login("playlist header"))

    def test_youtube_playlist_missing_requires_authenticated_404_without_header(self) -> None:
        logged_in = 'ytcfg.set({"LOGGED_IN":true});'
        missing_error = (
            "[youtube:tab] ERROR - Requested entity was not found. "
            "Unable to download API page: HTTP Error 404: Not Found"
        )
        self.assertTrue(
            core.youtube_playlist_is_missing(
                logged_in,
                {"video_count": 0, "has_video_count": False},
                missing_error,
            )
        )
        self.assertFalse(
            core.youtube_playlist_is_missing(
                'ytcfg.set({"LOGGED_IN":false});',
                {"video_count": 0, "has_video_count": False},
                missing_error,
            )
        )
        self.assertFalse(
            core.youtube_playlist_is_missing(
                logged_in,
                {"video_count": 1, "has_video_count": True},
                missing_error,
            )
        )
        self.assertFalse(
            core.youtube_playlist_is_missing(
                logged_in,
                {"video_count": 0, "has_video_count": False},
                "Incomplete yt initial data received",
            )
        )

    def test_youtube_page_diagnostics_classify_authentication_challenges(self) -> None:
        page = """
        ytcfg.set({
          "LOGGED_IN": false,
          "INNERTUBE_CLIENT_NAME": "WEB",
          "INNERTUBE_CLIENT_VERSION": "2.20260714.00.00"
        });
        var ytInitialPlayerResponse = {
          "playabilityStatus": {
            "status": "LOGIN_REQUIRED",
            "reason": "Sign in to confirm you're not a bot"
          }
        };
        <a href="https://accounts.google.com/ServiceLogin">Sign in</a>
        """
        diagnostics = core.youtube_page_diagnostics(page, "watch page")
        self.assertIn("operation=watch page", diagnostics)
        self.assertIn("logged_in=false", diagnostics)
        self.assertIn("service_login", diagnostics)
        self.assertIn("bot_check", diagnostics)
        self.assertIn("player_status=LOGIN_REQUIRED", diagnostics)
        self.assertIn("client=WEB", diagnostics)

    def test_youtube_request_error_diagnostics_sanitize_http_failure(self) -> None:
        error = urllib.error.HTTPError(
            "https://www.youtube.com/watch?v=private-id",
            429,
            "Too Many Requests",
            {"Retry-After": "120", "Content-Type": "text/html; charset=utf-8"},
            None,
        )
        diagnostics = core.youtube_request_error_diagnostics(error, "watch metadata")
        self.assertIn("status=429", diagnostics)
        self.assertIn("retry_after=120", diagnostics)
        self.assertIn("content_type=text/html", diagnostics)
        self.assertNotIn("private-id", diagnostics)

    def test_temporary_ytdlp_cookie_file_does_not_modify_configured_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_file = Path(temp_dir) / "cookies.txt"
            cookie_file.write_text("original cookies", encoding="utf-8")

            with core.temporary_ytdlp_cookie_file(cookie_file) as working_cookie_file:
                self.assertIsNotNone(working_cookie_file)
                self.assertNotEqual(working_cookie_file, cookie_file)
                self.assertEqual(working_cookie_file.read_text(encoding="utf-8"), "original cookies")
                working_cookie_file.write_text("yt-dlp updates", encoding="utf-8")

            self.assertEqual(cookie_file.read_text(encoding="utf-8"), "original cookies")
            self.assertFalse(working_cookie_file.exists())

    def test_youtube_ytdlp_probe_diagnostics_classify_cookie_rotation(self) -> None:
        diagnostics = core.youtube_ytdlp_probe_diagnostics(
            [
                "The provided YouTube account cookies are no longer valid. "
                "They have likely been rotated in the browser.",
                "web_safari player response playability status: LOGIN_REQUIRED",
            ],
            succeeded=False,
            deno_available=True,
            ejs_available=True,
        )

        self.assertIn("yt_dlp_probe=cookies_rotated", diagnostics)
        self.assertIn("deno=available", diagnostics)
        self.assertIn("ejs=available", diagnostics)
        self.assertIn("clients=web_safari", diagnostics)

    def test_youtube_ytdlp_probe_diagnostics_classify_bot_challenge(self) -> None:
        diagnostics = core.youtube_ytdlp_probe_diagnostics(
            ["ERROR: Sign in to confirm you're not a bot"],
            succeeded=False,
            deno_available=False,
            ejs_available=False,
        )

        self.assertIn("yt_dlp_probe=bot_challenge", diagnostics)
        self.assertIn("deno=missing", diagnostics)
        self.assertIn("ejs=missing", diagnostics)

    def test_youtube_ytdlp_probe_diagnostics_classify_rejected_login(self) -> None:
        diagnostics = core.youtube_ytdlp_probe_diagnostics(
            ["Login details are needed to download this content"],
            succeeded=False,
            deno_available=True,
            ejs_available=True,
        )

        self.assertIn("yt_dlp_probe=login_required", diagnostics)

    def test_history_date_from_relative_and_month_labels(self) -> None:
        today = date(2026, 7, 6)

        self.assertEqual(core.history_date_from_label("Today", today), "2026-07-06")
        self.assertEqual(core.history_date_from_label("Yesterday", today), "2026-07-05")
        self.assertEqual(core.history_date_from_label("Monday", today), "2026-06-29")
        self.assertEqual(core.history_date_from_label("Jun 30", today), "2026-06-30")
        self.assertEqual(core.history_date_from_label("Dec 31", today), "2025-12-31")

    def test_history_fetch_requests_browse_data_in_configured_timezone(self) -> None:
        jar = core.http.cookiejar.CookieJar()
        with (
            patch.object(core, "load_cookie_jar", return_value=jar),
            patch.object(core, "request_text", return_value="history page"),
            patch.object(
                core,
                "extract_ytcfg",
                return_value={
                    "INNERTUBE_API_KEY": "api-key",
                    "INNERTUBE_CLIENT_NAME": "WEB",
                    "INNERTUBE_CLIENT_VERSION": "client-version",
                },
            ),
            patch.object(core, "request_youtubei_json", return_value={}) as request,
        ):
            rows = core.fetch_youtube_history_web(
                Path("cookies.txt"),
                timezone_name="America/Los_Angeles",
            )

        self.assertEqual(rows, [])
        payload = request.call_args.args[3]
        self.assertEqual(payload["browseId"], "FEhistory")
        self.assertEqual(payload["context"]["client"]["timeZone"], "America/Los_Angeles")
        self.assertIn(payload["context"]["client"]["utcOffsetMinutes"], {-480, -420})

    def test_watch_datetime_helpers_normalize_offsets(self) -> None:
        self.assertEqual(
            core.takeout_watch_datetime("July 4, 2026, 5:27:45 AM PDT"),
            "2026-07-04T12:27:45Z",
        )
        self.assertEqual(
            core.takeout_watch_datetime("2026-07-04T05:27:45.123Z"),
            "2026-07-04T05:27:45Z",
        )
        self.assertEqual(
            core.local_date_for_utc_instant("2026-07-04T05:27:45Z", "America/Los_Angeles"),
            "2026-07-03",
        )

    def test_id_and_numeric_helpers(self) -> None:
        self.assertEqual(core.extract_video_id("https://www.youtube.com/watch?v=abc-123_DEF"), "abc-123_DEF")
        self.assertEqual(core.extract_video_id("https://youtu.be/abc-123_DEF"), "abc-123_DEF")
        self.assertEqual(core.extract_video_id("https://www.youtube.com/shorts/abc-123_DEF"), "abc-123_DEF")
        self.assertEqual(core.extract_video_id("https://www.youtube.com/embed/abc-123_DEF"), "abc-123_DEF")
        self.assertEqual(
            core.youtube_channel_id_from_url("https://www.youtube.com/channel/UCvmGOqGlxOgpZDoszBbWxmA"),
            "UCvmGOqGlxOgpZDoszBbWxmA",
        )
        self.assertEqual(core.youtube_channel_ref_from_url("https://www.youtube.com/@ESSIGI"), "@ESSIGI")
        self.assertEqual(core.youtube_channel_url("@ESSIGI"), "https://www.youtube.com/@ESSIGI")
        self.assertEqual(core.youtube_channel_url("c/Example"), "https://www.youtube.com/c/Example")
        self.assertEqual(
            core.first_channel_alias("https://www.youtube.com/@first, @second"),
            "@first",
        )
        self.assertEqual(
            core.preferred_youtube_channel_reference(
                "UCvmGOqGlxOgpZDoszBbWxmA",
                "youtube.com/@first, @second",
            ),
            "@first",
        )
        self.assertEqual(
            core.preferred_youtube_channel_url(
                "UCvmGOqGlxOgpZDoszBbWxmA",
                "youtube.com/@first, @second",
            ),
            "https://www.youtube.com/@first",
        )
        self.assertEqual(
            core.preferred_youtube_channel_url("UCvmGOqGlxOgpZDoszBbWxmA"),
            "https://www.youtube.com/channel/UCvmGOqGlxOgpZDoszBbWxmA",
        )
        self.assertEqual(
            core.local_queue_target_from_url("http://127.0.0.1:8765/playlists/PLexample"),
            ("playlist", "PLexample"),
        )
        self.assertEqual(
            core.local_queue_target_from_url("http://127.0.0.1:8765/videos/abc12345678"),
            ("video", "abc12345678"),
        )
        self.assertEqual(
            core.local_queue_target_from_url(
                "http://127.0.0.1:8765/clips/UgkxUIUr7iJI7JSqsEGWEYebU5mV1PaMbz9s"
            ),
            ("clip", "UgkxUIUr7iJI7JSqsEGWEYebU5mV1PaMbz9s"),
        )
        self.assertEqual(
            core.local_queue_target_from_url("http://127.0.0.1:8765/#video=abc12345678"),
            ("", ""),
        )
        self.assertEqual(core.format_duration(65), "1:05")
        self.assertEqual(core.format_duration(3661), "1:01:01")
        self.assertEqual(core.bounded_int("140"), 100)
        self.assertEqual(core.bounded_int("-5"), 0)
        self.assertTrue(core.playlist_entry_is_unavailable("[Deleted video]"))
        self.assertTrue(core.playlist_entry_is_unavailable("Private video"))
        self.assertTrue(core.playlist_entry_is_unavailable("Regular title", "needs_auth"))
        self.assertFalse(core.playlist_entry_is_unavailable("Regular title", "public"))
        self.assertTrue(core.playlist_zero_result_is_suspicious(0, "HTTP Error 403", 1))
        self.assertFalse(core.playlist_zero_result_is_suspicious(1, "HTTP Error 403", 1))
        self.assertFalse(core.playlist_zero_result_is_suspicious(0, "", 1))
        self.assertFalse(core.playlist_zero_result_is_suspicious(0, "HTTP Error 403", 0))
        self.assertTrue(core.playlist_scan_is_incomplete(100, 101))
        self.assertFalse(core.playlist_scan_is_incomplete(101, 101))
        self.assertFalse(core.playlist_scan_is_incomplete(101, 0))
        self.assertTrue(core.playlist_scan_requires_exact_count({"visibility": "private"}))
        self.assertTrue(core.playlist_scan_requires_exact_count({"owner_channel_id": "", "visibility": ""}))
        self.assertFalse(core.playlist_scan_requires_exact_count({"owner_channel_id": "UCother"}))
        self.assertFalse(core.playlist_scan_requires_exact_count({}, known_owner_channel_id="UCother"))
        self.assertTrue(core.playlist_scan_requires_exact_count({}, known_visibility="private"))

    def test_clip_parsers_keep_clip_and_source_video_metadata_distinct(self) -> None:
        clip_id = "UgkxUIUr7iJI7JSqsEGWEYebU5mV1PaMbz9s"
        grid = {
            "videoId": "source12345",
            "title": {"simpleText": "My clip title"},
            "shortBylineText": {
                "runs": [
                    {
                        "text": "Source uploader",
                        "navigationEndpoint": {
                            "browseEndpoint": {
                                "browseId": "UC_source",
                                "canonicalBaseUrl": "/@source-uploader",
                            }
                        },
                    }
                ]
            },
            "viewCountText": {"simpleText": "12 views"},
            "publishedTimeText": {"simpleText": "Clipped 4 months ago"},
            "thumbnail": {"thumbnails": [{"url": "https://example/clip.jpg"}]},
            "navigationEndpoint": {
                "watchEndpoint": {
                    "clipConfig": {
                        "postId": clip_id,
                        "startTimeMs": "1000",
                        "endTimeMs": "22000",
                    }
                }
            },
            "menu": {"simpleText": "Delete clip"},
        }

        discovered = core.parse_clip_grid_renderer(grid)

        self.assertEqual(core.extract_clip_id(f"https://www.youtube.com/clip/{clip_id}"), clip_id)
        self.assertEqual(discovered["title"], "My clip title")
        self.assertEqual(discovered["source_video_id"], "source12345")
        self.assertEqual(discovered["source_channel_id"], "UC_source")
        self.assertEqual(discovered["ownership"], "mine")
        self.assertEqual(discovered["view_count"], 12)
        self.assertEqual(discovered["end_ms"] - discovered["start_ms"], 21000)

        initial_data = {
            "clipAttributionRenderer": {
                "title": {"simpleText": "My clip title"},
                "clipAuthor": {"simpleText": "Gir Bot"},
                "authorAvatar": {
                    "thumbnails": [{"url": "https://example/clip-owner.jpg"}]
                },
                "createdText": {"simpleText": "12 views · 4 months ago"},
            },
            "videoPrimaryInfoRenderer": {
                "title": {"simpleText": "Full source video title"}
            },
            "videoOwnerRenderer": {
                "title": {"simpleText": "Source uploader"},
                "thumbnail": {
                    "thumbnails": [{"url": "https://example/source-owner.jpg"}]
                },
                "navigationEndpoint": {
                    "browseEndpoint": {
                        "browseId": "UC_source",
                        "canonicalBaseUrl": "/@source-uploader",
                    }
                },
            },
            "menu": {"simpleText": "Delete clip"},
            "frameworkUpdates": {
                "entityBatchUpdate": {
                    "mutations": [
                        {"payload": {"likeStatusEntity": {"likeStatus": "LIKE"}}}
                    ]
                }
            },
        }
        player_response = {
            "clipConfig": {
                "postId": clip_id,
                "startTimeMs": "1000",
                "endTimeMs": "22000",
            },
            "videoDetails": {
                "videoId": "source12345",
                "title": "Full source video title",
                "author": "Source uploader",
                "channelId": "UC_source",
                "lengthSeconds": "3661",
                "viewCount": "9876",
                "thumbnail": {"thumbnails": [{"url": "https://example/source.jpg"}]},
            },
            "microformat": {
                "playerMicroformatRenderer": {
                    "uploadDate": "2026-01-02",
                    "category": "Travel & Events",
                }
            },
            "playabilityStatus": {"status": "OK"},
        }

        detail = core.parse_clip_page(initial_data, player_response, clip_id)

        self.assertEqual(detail["title"], "My clip title")
        self.assertEqual(detail["owner_title"], "Gir Bot")
        self.assertEqual(detail["owner_thumbnail_url"], "https://example/clip-owner.jpg")
        self.assertEqual(detail["source_title"], "Full source video title")
        self.assertEqual(detail["source_channel_id"], "UC_source")
        self.assertEqual(detail["source_channel_url"], "/@source-uploader")
        self.assertEqual(
            detail["source_channel_thumbnail_url"],
            "https://example/source-owner.jpg",
        )
        self.assertEqual(detail["source_duration_text"], "1:01:01")
        self.assertEqual(detail["source_uploader_category"], "Travel & Events")
        self.assertEqual(detail["source_reaction"], "LIKE")
        self.assertEqual(detail["view_count"], 12)
        self.assertEqual(detail["clipped_at_text"], "Clipped 4 months ago")
        self.assertEqual(detail["ownership"], "mine")

    def test_clip_ids_cannot_enter_video_storage_or_metadata_queue(self) -> None:
        clip_id = "UgkxUIUr7iJI7JSqsEGWEYebU5mV1PaMbz9s"
        video_id_with_similar_prefix = "Ugk12345678"

        self.assertTrue(core.is_youtube_clip_id(clip_id))
        self.assertFalse(core.is_youtube_clip_id(video_id_with_similar_prefix))
        self.assertEqual(core.extract_clip_id(clip_id), clip_id)
        self.assertEqual(core.extract_clip_id(video_id_with_similar_prefix), "")

        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                routed = core.enqueue_worker_queue_target(conn, clip_id)
                self.assertEqual(routed["worker_type"], "clip")
                self.assertEqual(routed["clip_id"], clip_id)

                with self.assertRaisesRegex(ValueError, "clip IDs"):
                    core.upsert_video(conn, clip_id, source="test")
                with self.assertRaisesRegex(ValueError, "clip IDs"):
                    core.enqueue_metadata_item(
                        conn,
                        video_id=clip_id,
                        metadata_source="provided",
                    )
                self.assertIsNone(
                    conn.execute(
                        "SELECT video_id FROM videos WHERE video_id = ?",
                        (clip_id,),
                    ).fetchone()
                )

                core.upsert_video(conn, video_id_with_similar_prefix, source="test")
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT video_id FROM videos WHERE video_id = ?",
                        (video_id_with_similar_prefix,),
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_playlist_owner_visibility_helpers(self) -> None:
        self.assertEqual(core.normalize_playlist_visibility(" Public playlist "), "public")
        self.assertEqual(core.split_playlist_owner_visibility("Private"), ("", "private"))
        self.assertEqual(core.split_playlist_owner_visibility("Gir Bot"), ("Gir Bot", ""))
        metadata = core.playlist_metadata_from_ytdlp_info(
            {"title": "Example", "uploader": "Gir Bot", "availability": "unlisted"},
            "PLexample",
        )
        self.assertEqual(metadata["owner"], "Gir Bot")
        self.assertEqual(metadata["visibility"], "unlisted")
        visibility_only = core.playlist_metadata_from_ytdlp_info(
            {"title": "Example", "availability": "unlisted"},
            "PLexample",
        )
        self.assertEqual(visibility_only["owner"], "")
        self.assertEqual(visibility_only["visibility"], "unlisted")

    def test_playlist_updated_labels_parse_to_dates_without_inventing_times(self) -> None:
        observed_at = "2026-08-26T06:30:00Z"
        timezone_name = "America/Los_Angeles"

        self.assertEqual(
            core.parse_youtube_playlist_updated_date(
                "Updated today",
                observed_at=observed_at,
                timezone_name=timezone_name,
            ),
            "2026-08-25",
        )
        self.assertEqual(
            core.parse_youtube_playlist_updated_date(
                "Updated yesterday",
                observed_at=observed_at,
                timezone_name=timezone_name,
            ),
            "2026-08-24",
        )
        self.assertEqual(
            core.parse_youtube_playlist_updated_date(
                "Updated 4 days ago",
                observed_at=observed_at,
                timezone_name=timezone_name,
            ),
            "2026-08-21",
        )
        self.assertEqual(
            core.parse_youtube_playlist_updated_date(
                "Last updated on Mar 9, 2026",
                observed_at=observed_at,
                timezone_name=timezone_name,
            ),
            "2026-03-09",
        )
        self.assertEqual(
            core.parse_youtube_playlist_updated_date(
                "Updated recently",
                observed_at=observed_at,
                timezone_name=timezone_name,
            ),
            "",
        )

        lockup = {
            "contentId": "PLupdated",
            "contentType": "LOCKUP_CONTENT_TYPE_PLAYLIST",
            "metadata": {
                "lockupMetadataViewModel": {
                    "title": {"content": "Updated playlist"},
                    "metadata": {
                        "contentMetadataViewModel": {
                            "metadataRows": [
                                {
                                    "metadataParts": [
                                        {"text": {"content": "Private"}},
                                        {"text": {"content": "Playlist"}},
                                    ]
                                },
                                {
                                    "metadataParts": [
                                        {"text": {"content": "Updated today"}}
                                    ]
                                },
                            ]
                        }
                    },
                }
            },
        }
        parsed = core.parse_playlist_lockup(
            lockup,
            observed_at=observed_at,
            timezone_name=timezone_name,
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["youtube_updated_text"], "Updated today")
        self.assertEqual(parsed["youtube_updated_date"], "2026-08-25")
        self.assertEqual(parsed["description"], "")
        self.assertFalse(parsed["has_video_count"])

        lockup["metadata"]["lockupMetadataViewModel"]["metadata"][
            "contentMetadataViewModel"
        ]["metadataRows"].append(
            {
                "metadataParts": [
                    {"text": {"content": "0 videos"}},
                ]
            }
        )
        empty = core.parse_playlist_lockup(lockup)
        self.assertEqual(empty["video_count"], 0)
        self.assertTrue(empty["has_video_count"])

    def test_youtube_updated_date_merge_corrects_relative_label_rollover(self) -> None:
        self.assertEqual(
            core.merge_youtube_playlist_updated_date(
                "2026-08-27",
                "2026-08-26",
                "Updated yesterday",
            ),
            "2026-08-26",
        )
        self.assertEqual(
            core.merge_youtube_playlist_updated_date(
                "2026-08-27",
                "2026-08-26",
                "Updated 2 days ago",
            ),
            "2026-08-26",
        )
        self.assertEqual(
            core.merge_youtube_playlist_updated_date(
                "2026-08-27",
                "2026-03-09",
                "Last updated on Mar 9, 2026",
            ),
            "2026-08-27",
        )
        self.assertEqual(
            core.merge_youtube_playlist_updated_date(
                "2026-08-27",
                "",
                "",
            ),
            "2026-08-27",
        )

    def test_extract_playlist_metadata_reads_page_header_count_and_visibility(self) -> None:
        initial_data = {
            "header": {
                "pageHeaderRenderer": {
                    "content": {
                        "pageHeaderViewModel": {
                            "metadata": {
                                "contentMetadataViewModel": {
                                    "metadataRows": [
                                        {
                                            "metadataParts": [
                                                {"text": {"content": "Playlist"}},
                                                {"text": {"content": "Unlisted"}},
                                                {"text": {"content": "150 videos"}},
                                                {"text": {"content": "143 views"}},
                                            ]
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            }
        }
        html = f"<script>var ytInitialData = {json.dumps(initial_data)};</script>"

        metadata = core.extract_playlist_metadata(html, "PLexample")

        self.assertEqual(metadata["video_count"], 150)
        self.assertTrue(metadata["has_video_count"])
        self.assertNotIn("view_count", metadata)
        self.assertEqual(metadata["visibility"], "unlisted")
        self.assertEqual(metadata["owner"], "")
        self.assertFalse(core.extract_playlist_metadata("<html></html>", "PLexample")["has_video_count"])

        owner_data = {
            "header": {
                "playlistHeaderRenderer": {
                    "title": {"simpleText": "Foreign Playlist"},
                    "ownerText": {
                        "runs": [
                            {
                                "text": "Other Channel",
                                "navigationEndpoint": {
                                    "browseEndpoint": {
                                        "browseId": "UCabcdefghijklmnopqrstuv",
                                    }
                                },
                            }
                        ]
                    },
                    "numVideosText": {"simpleText": "2 videos"},
                }
            }
        }
        owner_html = f"<script>var ytInitialData = {json.dumps(owner_data)};</script>"
        owner_metadata = core.extract_playlist_metadata(owner_html, "PLforeign")
        self.assertEqual(owner_metadata["owner"], "Other Channel")
        self.assertEqual(owner_metadata["owner_channel_id"], "UCabcdefghijklmnopqrstuv")

        sidebar_data = {
            "header": owner_data["header"],
            "sidebar": {
                "playlistSidebarRenderer": {
                    "items": [
                        {
                            "playlistSidebarPrimaryInfoRenderer": {
                                "stats": [
                                    {"simpleText": "2 videos"},
                                    {"simpleText": "14 views"},
                                    {
                                        "runs": [
                                            {"text": "Last updated on Aug 17, 2026"}
                                        ]
                                    },
                                ],
                                "badges": [
                                    {
                                        "metadataBadgeRenderer": {
                                            "icon": {"iconType": "PRIVACY_UNLISTED"},
                                            "label": "Unlisted",
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            },
        }
        sidebar_html = f"<script>var ytInitialData = {json.dumps(sidebar_data)};</script>"
        sidebar_metadata = core.extract_playlist_metadata(
            sidebar_html,
            "PLforeign",
            observed_at="2026-08-26T17:00:00Z",
            timezone_name="America/Los_Angeles",
        )
        self.assertEqual(sidebar_metadata["owner"], "Other Channel")
        self.assertEqual(
            sidebar_metadata["owner_channel_id"],
            "UCabcdefghijklmnopqrstuv",
        )
        self.assertEqual(sidebar_metadata["visibility"], "unlisted")
        self.assertEqual(sidebar_metadata["youtube_updated_date"], "2026-08-17")

        microformat_data = {
            "header": owner_data["header"],
            "microformat": {
                "microformatDataRenderer": {
                    "noindex": True,
                    "unlisted": True,
                }
            },
        }
        microformat_html = (
            f"<script>var ytInitialData = {json.dumps(microformat_data)};</script>"
        )
        microformat_metadata = core.extract_playlist_metadata(
            microformat_html,
            "PLforeign",
        )
        self.assertEqual(microformat_metadata["owner"], "Other Channel")
        self.assertEqual(microformat_metadata["visibility"], "unlisted")

        attributed_data = {
            "header": {
                "pageHeaderRenderer": {
                    "content": {
                        "pageHeaderViewModel": {
                            "metadata": {
                                "contentMetadataViewModel": {
                                    "metadataRows": [
                                        {
                                            "metadataParts": [
                                                {
                                                    "avatarStack": {
                                                        "avatarStackViewModel": {
                                                            "text": {
                                                                "content": "by alt Tabby",
                                                                "commandRuns": [
                                                                    {
                                                                        "onTap": {
                                                                            "innertubeCommand": {
                                                                                "browseEndpoint": {
                                                                                    "browseId": "UC9M9ViKcwu5rdRwLDmernrg",
                                                                                    "canonicalBaseUrl": "/@alttabby3633",
                                                                                }
                                                                            }
                                                                        }
                                                                    }
                                                                ],
                                                            },
                                                            "avatar": {
                                                                "avatarViewModel": {
                                                                    "image": {
                                                                        "sources": [
                                                                            {
                                                                                "url": "https://yt3.example/small.jpg",
                                                                                "width": 48,
                                                                            },
                                                                            {
                                                                                "url": "https://yt3.example/large.jpg",
                                                                                "width": 176,
                                                                            },
                                                                        ]
                                                                    }
                                                                }
                                                            },
                                                        }
                                                    }
                                                }
                                            ]
                                        },
                                        {
                                            "metadataParts": [
                                                {"text": {"content": "Playlist"}},
                                                {"text": {"content": "361 videos"}},
                                                {"text": {"content": "320 views"}},
                                            ]
                                        },
                                    ]
                                }
                            }
                        }
                    }
                }
            }
        }
        attributed_html = f"<script>var ytInitialData = {json.dumps(attributed_data)};</script>"
        attributed_metadata = core.extract_playlist_metadata(attributed_html, "PLforeign")
        self.assertEqual(attributed_metadata["owner"], "alt Tabby")
        self.assertEqual(attributed_metadata["owner_channel_id"], "UC9M9ViKcwu5rdRwLDmernrg")
        self.assertEqual(attributed_metadata["owner_thumbnail_url"], "https://yt3.example/large.jpg")
        self.assertEqual(attributed_metadata["video_count"], 361)
        self.assertFalse(core.playlist_scan_requires_exact_count(attributed_metadata))

    def test_playlist_collaboration_metadata_keeps_owner_singular(self) -> None:
        shared_part = {
            "avatarStack": {
                "avatarStackViewModel": {
                    "avatars": [{"avatarViewModel": {}}, {"avatarViewModel": {}}],
                    "text": {"content": "by Gir Bot and 1 other"},
                }
            }
        }
        self.assertEqual(core.playlist_owner_from_metadata_part(shared_part), ("", ""))

        def participant(
            title: str,
            channel_id: str,
            thumbnail_url: str,
            *,
            owner: bool = False,
        ) -> dict[str, object]:
            value: dict[str, object] = {
                "title": {
                    "content": title,
                    "rendererContext": {
                        "commandContext": {
                            "onTap": {
                                "innertubeCommand": {
                                    "browseEndpoint": {"browseId": channel_id}
                                }
                            }
                        }
                    },
                },
                "avatar": {
                    "avatarViewModel": {
                        "image": {
                            "sources": [
                                {"url": thumbnail_url, "width": 88},
                            ]
                        }
                    }
                },
            }
            if owner:
                value["metadata"] = {
                    "contentMetadataViewModel": {
                        "metadataRows": [{"metadataParts": [{"text": {"content": "Owner"}}]}]
                    }
                }
            return {"contentListItemViewModel": value}

        response = {
            "content": {
                "engagementPanelSectionListRenderer": {
                    "content": {
                        "playlistCollaborationViewModel": {
                            "playlistCollaborators": [
                                participant(
                                    "Gir Bot",
                                    "UCnUc4Kc09vNJ3yBu6-MJHTQ",
                                    "https://yt3.example/gir.jpg",
                                    owner=True,
                                ),
                                participant(
                                    "alt Tabby",
                                    "UC9M9ViKcwu5rdRwLDmernrg",
                                    "https://yt3.example/tabby.jpg",
                                ),
                            ]
                        }
                    }
                }
            }
        }

        metadata = core.parse_playlist_collaboration_metadata(response)

        self.assertEqual(metadata["owner"], "Gir Bot")
        self.assertEqual(metadata["owner_channel_id"], "UCnUc4Kc09vNJ3yBu6-MJHTQ")
        self.assertEqual(
            metadata["collaborators"],
            [
                {
                    "title": "alt Tabby",
                    "channel_id": "UC9M9ViKcwu5rdRwLDmernrg",
                    "thumbnail_url": "https://yt3.example/tabby.jpg",
                }
            ],
        )
        self.assertTrue(metadata["collaborators_authoritative"])

    def test_channel_subscription_state_uses_active_entity_not_button_templates(self) -> None:
        initial_data = {
            "header": {
                "subscribeButtonViewModel": {
                    "subscribeButtonContent": {
                        "subscribeState": {"subscribed": False},
                    },
                    "unsubscribeButtonContent": {
                        "subscribeState": {"subscribed": True},
                    },
                }
            },
            "frameworkUpdates": {
                "entityBatchUpdate": {
                    "mutations": [
                        {
                            "payload": {
                                "subscriptionStateEntity": {
                                    "subscribed": True,
                                }
                            }
                        },
                        {
                            "payload": {
                                "subscriptionNotificationStateEntity": {
                                    "state": "SUBSCRIPTION_NOTIFICATION_STATE_ALL",
                                }
                            }
                        }
                    ]
                }
            },
        }

        self.assertIs(core.extract_channel_subscription_state(initial_data), True)
        self.assertEqual(core.extract_channel_notification_level(initial_data), "all")
        self.assertEqual(
            core.normalize_channel_notification_level(
                "SUBSCRIPTION_NOTIFICATION_STATE_OCCASIONAL"
            ),
            "personalized",
        )
        self.assertEqual(
            core.normalize_channel_notification_level(
                "SUBSCRIPTION_NOTIFICATION_STATE_NONE"
            ),
            "none",
        )
        self.assertEqual(core.normalize_channel_notification_level("unknown"), "")
        self.assertIs(
            core.extract_channel_subscription_state(
                {
                    "header": {
                        "subscriptionButtonRenderer": {
                            "subscribed": False,
                        }
                    }
                }
            ),
            False,
        )
        self.assertIsNone(core.extract_channel_subscription_state({}))

    def test_channel_subscription_state_matches_requested_channel_entity(self) -> None:
        def entity_key(channel_id: str) -> str:
            encoded = base64.urlsafe_b64encode(
                b"\x12\x18" + channel_id.encode() + b" 3(\x01"
            ).decode()
            return encoded.rstrip("=")

        channel_id = "UCMGQaKbhEpkFGTk3-TTeNIA"
        initial_data = {
            "frameworkUpdates": {
                "entityBatchUpdate": {
                    "mutations": [
                        {
                            "payload": {
                                "subscriptionStateEntity": {
                                    "key": entity_key("UCbiQpdAl6P_pLWIC44EoDsg"),
                                    "subscribed": False,
                                }
                            }
                        },
                        {
                            "payload": {
                                "subscriptionStateEntity": {
                                    "key": entity_key(channel_id),
                                    "subscribed": True,
                                }
                            }
                        },
                        {
                            "payload": {
                                "subscriptionNotificationStateEntity": {
                                    "key": entity_key(channel_id),
                                    "state": "SUBSCRIPTION_NOTIFICATION_STATE_OCCASIONAL",
                                }
                            }
                        },
                    ]
                }
            }
        }

        self.assertIs(
            core.extract_channel_subscription_state(initial_data, channel_id),
            True,
        )
        self.assertEqual(
            core.extract_channel_notification_level(initial_data, channel_id),
            "personalized",
        )

    def test_channel_metadata_ignores_logged_out_subscription_state(self) -> None:
        channel_id = "UCchannel12345678901234"
        initial_data = {
            "metadata": {
                "channelMetadataRenderer": {
                    "title": "Example Channel",
                    "externalId": channel_id,
                    "avatar": {
                        "thumbnails": [
                            {"url": "https://example.test/channel.jpg", "width": 176}
                        ]
                    },
                }
            },
            "frameworkUpdates": {
                "entityBatchUpdate": {
                    "mutations": [
                        {
                            "payload": {
                                "subscriptionStateEntity": {
                                    "subscribed": True,
                                }
                            }
                        },
                        {
                            "payload": {
                                "subscriptionNotificationStateEntity": {
                                    "state": "SUBSCRIPTION_NOTIFICATION_STATE_ALL",
                                }
                            }
                        }
                    ]
                }
            },
        }
        page = f"<script>var ytInitialData = {json.dumps(initial_data)};</script>"

        with (
            patch("yt_library.core.request_text", return_value=page),
            patch("yt_library.core.youtube_page_is_authenticated", return_value=False),
            patch("yt_library.core.cache_channel_thumbnail", return_value=""),
        ):
            logged_out = core.fetch_channel_metadata(
                object(),
                channel_id,
                Path("thumbs"),
            )
        with (
            patch("yt_library.core.request_text", return_value=page),
            patch("yt_library.core.youtube_page_is_authenticated", return_value=True),
            patch("yt_library.core.cache_channel_thumbnail", return_value=""),
        ):
            authenticated = core.fetch_channel_metadata(
                object(),
                channel_id,
                Path("thumbs"),
            )

        self.assertEqual(logged_out["channel_subscribed"], "")
        self.assertEqual(logged_out["channel_notification_level"], "")
        self.assertEqual(authenticated["channel_subscribed"], "1")
        self.assertEqual(authenticated["channel_notification_level"], "all")

    def test_channel_metadata_fetch_resolves_handle_to_canonical_identity(self) -> None:
        def entity_key(channel_id: str) -> str:
            encoded = base64.urlsafe_b64encode(
                b"\x12\x18" + channel_id.encode() + b" 3(\x01"
            ).decode()
            return encoded.rstrip("=")

        channel_id = "UC3JYkcrAkY2wW7mJ9mwhofw"
        initial_data = {
            "metadata": {
                "channelMetadataRenderer": {
                    "title": "FRUHD",
                    "externalId": channel_id,
                    "channelUrl": "/@FRUHD",
                    "ownerUrls": ["https://www.youtube.com/@FRUHD"],
                    "avatar": {
                        "thumbnails": [
                            {"url": "https://example.test/fruhd.jpg", "width": 176}
                        ]
                    },
                }
            },
            "frameworkUpdates": {
                "entityBatchUpdate": {
                    "mutations": [
                        {
                            "payload": {
                                "subscriptionStateEntity": {
                                    "key": entity_key(channel_id),
                                    "subscribed": True,
                                }
                            }
                        },
                        {
                            "payload": {
                                "subscriptionNotificationStateEntity": {
                                    "key": entity_key(channel_id),
                                    "state": "SUBSCRIPTION_NOTIFICATION_STATE_ALL",
                                }
                            }
                        },
                    ]
                }
            },
        }
        page = f"<script>var ytInitialData = {json.dumps(initial_data)};</script>"

        with (
            patch("yt_library.core.request_text", return_value=page),
            patch("yt_library.core.youtube_page_is_authenticated", return_value=True),
            patch("yt_library.core.cache_channel_thumbnail", return_value=""),
        ):
            metadata = core.fetch_channel_metadata(
                object(),
                "@FRUHD",
                Path("thumbs"),
            )

        self.assertEqual(metadata["channel_id"], channel_id)
        self.assertEqual(metadata["requested_channel_reference"], "@FRUHD")
        self.assertEqual(metadata["channel_aliases"], "@FRUHD")
        self.assertEqual(metadata["channel_subscribed"], "1")
        self.assertEqual(metadata["channel_notification_level"], "all")

    def test_channel_metadata_fetch_uses_only_the_direct_channel_page(self) -> None:
        opener = Mock()
        channel_id = "UCchannel12345678901234"
        page = "<script>var ytInitialData = {};</script>"
        with (
            patch.object(core, "request_text", return_value=page) as request_text,
            patch.object(core, "youtube_page_is_authenticated", return_value=True),
            patch.object(core, "load_cookie_opener", return_value=Mock()),
            patch.object(core, "archivarix_lookup_channel", return_value={}),
            patch.object(core, "cache_channel_thumbnail", return_value=""),
        ):
            metadata = core.fetch_channel_metadata(opener, channel_id, Path("thumbs"))

        request_text.assert_called_once_with(
            opener,
            f"https://www.youtube.com/channel/{channel_id}",
        )
        self.assertEqual(metadata["channel_id"], channel_id)

    def test_channel_status_observation_requires_matching_youtube_evidence(self) -> None:
        channel_id = "UCchannel12345678901234"
        active_page = f"""
            <script>var ytInitialData = {json.dumps({
                "metadata": {
                    "channelMetadataRenderer": {
                        "title": "Active Channel",
                        "externalId": channel_id,
                        "channelUrl": f"/channel/{channel_id}",
                    }
                }
            })};</script>
        """
        active = core.extract_channel_page_metadata(active_page, channel_id)
        self.assertTrue(active["channel_status_observed"])
        self.assertEqual(active["channel_status"], "")

        other_channel_page = f"""
            <script>var ytInitialData = {json.dumps({
                "metadata": {
                    "channelMetadataRenderer": {
                        "title": "Wrong Channel",
                        "externalId": "UCotherchannel1234567890",
                    }
                }
            })};</script>
        """
        other = core.extract_channel_page_metadata(other_channel_page, channel_id)
        self.assertFalse(other["channel_status_observed"])
        self.assertEqual(other["channel_id"], channel_id)
        self.assertEqual(other["channel"], "")

        terminated = core.extract_channel_page_metadata(
            "<html>This account has been terminated for violating YouTube policy.</html>",
            channel_id,
        )
        self.assertTrue(terminated["channel_status_observed"])
        self.assertEqual(terminated["channel_status"], "terminated")
        self.assertTrue(terminated["channel_status_reason"])

        inconclusive_pages = {
            "generic title": "<title>YouTube</title>",
            "login": "<title>Sign in - YouTube</title> ServiceLogin",
            "consent": "<title>Before you continue</title> consent.youtube.com",
            "captcha": "<title>YouTube</title><div class='g-recaptcha'></div>",
            "parsing": "<script>var ytInitialData = {not-json};</script>",
        }
        for label, page in inconclusive_pages.items():
            with self.subTest(label=label):
                metadata = core.extract_channel_page_metadata(page, channel_id)
                self.assertFalse(metadata["channel_status_observed"])
                self.assertEqual(metadata["channel_status"], "")

    def test_channel_metadata_merge_respects_youtube_status_authority(self) -> None:
        archivarix_deleted = {
            "channel_id": "UCchannel12345678901234",
            "channel": "Recovered Channel",
            "channel_status": "deleted",
            "channel_status_reason": "Deleted/terminated channel reported by Archivarix.",
        }
        active_youtube = {
            "channel_id": "UCchannel12345678901234",
            "channel": "Live Channel",
            "channel_status": "",
            "channel_status_reason": "",
            "channel_status_observed": True,
        }

        active = core.merge_channel_metadata(active_youtube, archivarix_deleted)
        self.assertEqual(active["channel_status"], "")
        self.assertEqual(active["channel_status_reason"], "")
        self.assertTrue(active["channel_status_observed"])

        fallback = core.merge_channel_metadata(
            {
                "channel_id": "UCchannel12345678901234",
                "channel_status": "",
                "channel_status_reason": "",
                "channel_status_observed": False,
            },
            archivarix_deleted,
        )
        self.assertEqual(fallback["channel_status"], "deleted")
        self.assertEqual(
            fallback["channel_status_reason"],
            "Deleted/terminated channel reported by Archivarix.",
        )
        self.assertFalse(fallback["channel_status_observed"])

    def test_channel_status_transitions_require_authoritative_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                def stored_status(channel_id: str) -> tuple[str, str]:
                    row = conn.execute(
                        "SELECT status, status_reason FROM channels WHERE channel_id = ?",
                        (channel_id,),
                    ).fetchone()
                    return row["status"], row["status_reason"]

                for prior_status in ("terminated", "deleted"):
                    channel_id = f"UC_{prior_status}"
                    with conn:
                        core.upsert_channel(
                            conn,
                            channel_id,
                            title="Recovered Channel",
                            status=prior_status,
                            status_reason=f"Previously {prior_status}",
                        )
                        core.store_channel_metadata(
                            conn,
                            {
                                "channel_id": channel_id,
                                "channel": "Recovered Channel",
                                "channel_status": "",
                                "channel_status_reason": "",
                                "channel_status_observed": True,
                            },
                            "ok",
                        )
                    self.assertEqual(stored_status(channel_id), ("", ""))

                channel_id = "UC_active_transition"
                with conn:
                    core.upsert_channel(conn, channel_id, title="Active Channel")
                    core.store_channel_metadata(
                        conn,
                        {
                            "channel_id": channel_id,
                            "channel": "Active Channel",
                            "channel_status": "terminated",
                            "channel_status_reason": "This account has been terminated.",
                            "channel_status_observed": True,
                        },
                        "ok",
                    )
                self.assertEqual(
                    stored_status(channel_id),
                    ("terminated", "This account has been terminated."),
                )

                with conn:
                    core.store_channel_metadata(
                        conn,
                        {
                            "channel_id": channel_id,
                            "channel": "Active Channel",
                            "channel_status": "",
                            "channel_status_reason": "",
                            "channel_status_observed": True,
                        },
                        "ok",
                    )
                    core.store_channel_metadata(
                        conn,
                        {
                            "channel_id": channel_id,
                            "channel": "Active Channel",
                            "channel_status": "",
                            "channel_status_reason": "",
                            "channel_status_observed": True,
                        },
                        "ok",
                    )
                self.assertEqual(stored_status(channel_id), ("", ""))
            finally:
                conn.close()

    def test_inconclusive_channel_updates_preserve_status_and_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                channel_id = "UC_preserved_status"
                with conn:
                    core.upsert_channel(
                        conn,
                        channel_id,
                        title="Known Channel",
                        status="terminated",
                        status_reason="Known YouTube termination",
                    )
                updates = (
                    ("ok", "", ""),
                    ("error", "", ""),
                    (
                        "ok",
                        "deleted",
                        "Deleted/terminated channel reported by Archivarix.",
                    ),
                )
                for fetch_status, incoming_status, incoming_reason in updates:
                    with self.subTest(fetch_status=fetch_status, incoming_status=incoming_status):
                        with conn:
                            core.store_channel_metadata(
                                conn,
                                {
                                    "channel_id": channel_id,
                                    "channel": "Known Channel",
                                    "channel_status": incoming_status,
                                    "channel_status_reason": incoming_reason,
                                    "channel_status_observed": False,
                                },
                                fetch_status,
                                "transport or parsing failure" if fetch_status == "error" else "",
                            )
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

    def test_channel_notification_level_preserves_unknown_and_clears_unsubscribed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_channel(conn, "UCchannel", title="Example Channel")
                    conn.execute(
                        """
                        UPDATE channels
                        SET subscribed = 1, notification_level = 'all'
                        WHERE channel_id = 'UCchannel'
                        """
                    )
                    core.store_channel_metadata(
                        conn,
                        {
                            "channel_id": "UCchannel",
                            "channel": "Example Channel",
                            "channel_subscribed": "",
                            "channel_notification_level": "",
                        },
                        "ok",
                    )
                preserved = conn.execute(
                    """
                    SELECT subscribed, notification_level
                    FROM channels
                    WHERE channel_id = 'UCchannel'
                    """
                ).fetchone()
                self.assertEqual(preserved["subscribed"], 1)
                self.assertEqual(preserved["notification_level"], "all")

                with conn:
                    core.store_channel_metadata(
                        conn,
                        {
                            "channel_id": "UCchannel",
                            "channel": "Example Channel",
                            "channel_subscribed": "0",
                            "channel_notification_level": "personalized",
                        },
                        "ok",
                        updated_at="2026-07-30T22:00:00Z",
                    )
                unsubscribed = conn.execute(
                    """
                    SELECT subscribed, notification_level,
                           subscription_checked_at, notification_checked_at
                    FROM channels
                    WHERE channel_id = 'UCchannel'
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(unsubscribed["subscribed"], 0)
        self.assertEqual(unsubscribed["notification_level"], "")
        self.assertEqual(
            unsubscribed["subscription_checked_at"],
            "2026-07-30T22:00:00Z",
        )
        self.assertEqual(
            unsubscribed["notification_checked_at"],
            "2026-07-30T22:00:00Z",
        )

    def test_store_channel_metadata_repairs_handle_keyed_channel_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_channel(
                        conn,
                        "@FRUHD",
                        title="FRUHD",
                    )
                    core.upsert_video(
                        conn,
                        "fruhdvideo1",
                        title="FRUHD video",
                        channel_id="@FRUHD",
                    )
                    core.store_channel_metadata(
                        conn,
                        {
                            "channel_id": "UC3JYkcrAkY2wW7mJ9mwhofw",
                            "requested_channel_reference": "@FRUHD",
                            "channel": "FRUHD",
                            "channel_aliases": "@FRUHD",
                            "channel_aliases_observed": True,
                            "channel_subscribed": "1",
                            "channel_notification_level": "all",
                        },
                        "ok",
                    )
                stale = conn.execute(
                    "SELECT 1 FROM channels WHERE channel_id = '@FRUHD'"
                ).fetchone()
                canonical = conn.execute(
                    """
                    SELECT subscribed, notification_level
                    FROM channels
                    WHERE channel_id = 'UC3JYkcrAkY2wW7mJ9mwhofw'
                    """
                ).fetchone()
                video_channel_id = conn.execute(
                    "SELECT channel_id FROM videos WHERE video_id = 'fruhdvideo1'"
                ).fetchone()["channel_id"]
            finally:
                conn.close()

        self.assertIsNone(stale)
        self.assertEqual(
            (canonical["subscribed"], canonical["notification_level"]),
            (1, "all"),
        )
        self.assertEqual(video_channel_id, "UC3JYkcrAkY2wW7mJ9mwhofw")

    def test_successful_video_metadata_marks_visibility_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.store_video_metadata(
                        conn,
                        {
                            "video_id": "checkedvid1",
                            "title": "Checked video",
                            "availability": "unlisted",
                            "playability_status": "OK",
                        },
                        "ok",
                        updated_at="2026-07-30T22:30:00Z",
                    )
                row = conn.execute(
                    """
                    SELECT availability, visibility_checked_at
                    FROM videos
                    WHERE video_id = 'checkedvid1'
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(row["availability"], "unlisted")
        self.assertEqual(row["visibility_checked_at"], "2026-07-30T22:30:00Z")

    def test_playlist_continuation_prefers_command_executor_token(self) -> None:
        data = {
            "continuationItemRenderer": {
                "continuationEndpoint": {
                    "continuationCommand": {"token": "wrong-token"},
                    "commandExecutorCommand": {
                        "commands": [
                            {"playlistVotingRefreshPopupCommand": {}},
                            {"continuationCommand": {"token": "playlist-token"}},
                        ]
                    },
                }
            }
        }

        self.assertEqual(core.playlist_continuation_token(data), "playlist-token")

    def test_playlist_continuation_reads_view_model_token(self) -> None:
        data = {
            "continuationItemViewModel": {
                "continuationCommand": {
                    "innertubeCommand": {
                        "continuationCommand": {"token": "view-model-token"}
                    }
                }
            }
        }

        self.assertEqual(core.playlist_continuation_token(data), "view-model-token")

    def test_parse_takeout_watch_history_json(self) -> None:
        rows = core.parse_takeout_watch_history_text(
            """
            [
              {
                "title": "Watched Example Video",
                "titleUrl": "https://www.youtube.com/watch?v=vid123",
                "subtitles": [{
                  "name": "Example Channel",
                  "url": "https://www.youtube.com/channel/UCvmGOqGlxOgpZDoszBbWxmA"
                }],
                "time": "2026-07-04T05:27:45.123Z"
              },
              {
                "titleUrl": "https://www.youtube.com/watch?v=blanktitle1",
                "time": "2026-07-05T05:27:45.123Z"
              }
            ]
            """
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["video_id"], "vid123")
        self.assertEqual(rows[0]["title"], "Example Video")
        self.assertEqual(rows[0]["channel"], "Example Channel")
        self.assertEqual(rows[0]["channel_id"], "UCvmGOqGlxOgpZDoszBbWxmA")
        self.assertEqual(rows[1]["video_id"], "blanktitle1")
        self.assertEqual(rows[1]["title"], "")

    def test_missing_video_titles_remain_blank(self) -> None:
        video_id = "abc12345678"
        self.assertEqual(core.video_title_or_blank(video_id, video_id), "")
        self.assertEqual(core.video_title_or_blank("Unavailable video", video_id), "")
        self.assertEqual(
            core.video_title_or_blank(
                f"https://www.youtube.com/watch?v={video_id}",
                video_id,
            ),
            "",
        )
        self.assertEqual(core.video_title_or_blank("A real title", video_id), "A real title")

        history_row = core.parse_history_lockup({"contentId": video_id}, "2026-07-30")
        self.assertIsNotNone(history_row)
        self.assertEqual(history_row["title"], "")

        shorts_row = core.parse_shorts_lockup(
            "PLexample",
            {
                "onTap": {
                    "innertubeCommand": {
                        "reelWatchEndpoint": {"videoId": video_id}
                    }
                }
            },
            1,
        )
        self.assertEqual(shorts_row["title"], "")

        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        video_id,
                        title=video_id,
                        source="playlist",
                    )
                stored_title = conn.execute(
                    "SELECT title FROM videos WHERE video_id = ?",
                    (video_id,),
                ).fetchone()["title"]
            finally:
                conn.close()
        self.assertEqual(stored_title, "")

    def test_playlist_rows_require_explicit_positive_playability(self) -> None:
        base_renderer = {
            "videoId": "playlistvid1",
            "title": {"simpleText": "Playlist video"},
        }

        inferred = core.parse_video_renderer("PLexample", base_renderer, 1)
        explicit = core.parse_video_renderer(
            "PLexample",
            {**base_renderer, "isPlayable": True},
            1,
        )
        unavailable = core.parse_video_renderer(
            "PLexample",
            {**base_renderer, "isPlayable": False},
            1,
        )

        self.assertIsNone(inferred["is_playable"])
        self.assertEqual(inferred["availability"], "public")
        self.assertEqual(explicit["is_playable"], 1)
        self.assertEqual(explicit["availability"], "public")
        self.assertEqual(unavailable["is_playable"], 0)
        self.assertEqual(unavailable["availability"], "unavailable")
        self.assertEqual(core.playlist_video_playability({"is_playable": 1}), 1)
        self.assertEqual(
            core.playlist_video_playability(
                {},
                "Playlist video",
                "subscriber_only",
            ),
            0,
        )

    def test_playlist_lockup_shapes_do_not_infer_positive_playability(self) -> None:
        panel = core.parse_panel_video_renderer(
            "PLexample",
            {
                "videoId": "panelvideo1",
                "title": {"simpleText": "Panel video"},
            },
            1,
        )
        lockup = core.parse_video_lockup(
            "PLexample",
            {
                "contentId": "lockupvideo1",
                "metadata": {
                    "lockupMetadataViewModel": {
                        "title": {"content": "Lockup video"},
                    }
                },
            },
            2,
        )
        short = core.parse_shorts_lockup(
            "PLexample",
            {
                "onTap": {
                    "innertubeCommand": {
                        "reelWatchEndpoint": {"videoId": "shortvideo1"}
                    }
                },
                "accessibilityText": "Short video, 100 views",
            },
            3,
        )

        for row in (panel, lockup, short):
            self.assertIsNone(row["is_playable"])
            self.assertEqual(row["availability"], "public")

    def test_import_history_syncs_takeout_subscriptions(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            core.ROOT = root
            try:
                db_path = root / "library.sqlite3"
                core.migrate_database(db_path)
                zip_path = root / "takeout-20260704T052745Z-001.zip"
                with zipfile.ZipFile(zip_path, "w") as zf:
                    zf.writestr(
                        "Takeout/YouTube and YouTube Music/history/watch-history.json",
                        json.dumps(
                            [
                                {
                                    "title": "Watched Example Video",
                                    "titleUrl": "https://www.youtube.com/watch?v=vid123",
                                    "subtitles": [
                                        {
                                            "name": "Example Channel",
                                            "url": "https://www.youtube.com/channel/UCvmGOqGlxOgpZDoszBbWxmA",
                                        }
                                    ],
                                    "time": "2026-07-04T05:27:45.123Z",
                                }
                            ]
                        ),
                    )
                    zf.writestr(
                        "Takeout/YouTube and YouTube Music/subscriptions/subscriptions.csv",
                        (
                            "Channel Id,Channel Url,Channel Title\n"
                            "UCsubscribed12345678901234,https://www.youtube.com/channel/UCsubscribed12345678901234,Subscribed Channel\n"
                        ),
                    )

                first_import = core.import_history(
                    argparse.Namespace(
                        db=str(db_path),
                        takeout=str(root),
                        history_key="",
                    )
                )
                conn = core.connect(db_path)
                try:
                    with conn:
                        conn.execute(
                            """
                            INSERT INTO history_events(
                              event_id, video_id, watch_date, time_precision,
                              source_type, match_type, youtube_ordinal, imported_at, updated_at
                            ) VALUES (
                              'youtube:7', 'vid123', '2026-07-03', 'date_only',
                              'youtube', 'youtube_only', 7, '2026-07-04T06:00:00Z', '2026-07-04T06:00:00Z'
                            )
                            """
                        )
                        core.rebuild_history_reconciliation(conn, "America/Los_Angeles")
                    subscribed = conn.execute(
                        "SELECT title, subscribed FROM channels WHERE channel_id = ?",
                        ("UCsubscribed12345678901234",),
                    ).fetchone()
                    history_count = conn.execute(
                        "SELECT COUNT(*) FROM history_events WHERE takeout_history_key IS NOT NULL"
                    ).fetchone()[0]
                finally:
                    conn.close()

                second_import = core.import_history(
                    argparse.Namespace(db=str(db_path), takeout=str(root), history_key="")
                )
                conn = core.connect(db_path)
                try:
                    matched_ordinal = conn.execute(
                        "SELECT youtube_ordinal FROM history_events WHERE takeout_history_key IS NOT NULL"
                    ).fetchone()[0]
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root

        self.assertIsNotNone(subscribed)
        self.assertEqual(subscribed["title"], "Subscribed Channel")
        self.assertEqual(subscribed["subscribed"], 1)
        self.assertEqual(history_count, 1)
        self.assertEqual(matched_ordinal, 7)
        self.assertEqual(first_import["inserted_watch_rows"], 1)
        self.assertEqual(second_import["inserted_watch_rows"], 0)
        self.assertEqual(second_import["duplicate_watch_rows"], 1)

    def test_import_history_reads_all_takeout_zips_and_skips_duplicates(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            core.ROOT = root
            try:
                db_path = root / "library.sqlite3"
                core.migrate_database(db_path)
                duplicate = {
                    "title": "Watched Duplicate Video",
                    "titleUrl": "https://www.youtube.com/watch?v=dup123",
                    "subtitles": [{"name": "Example Channel", "url": "https://www.youtube.com/channel/UCvmGOqGlxOgpZDoszBbWxmA"}],
                    "time": "2026-07-04T05:27:45.123Z",
                }
                exports = [
                    (
                        "takeout-20260704T052745Z-001.zip",
                        [
                            duplicate,
                            {
                                "title": "Watched Older Video",
                                "titleUrl": "https://www.youtube.com/watch?v=old123",
                                "time": "2026-07-03T05:27:45.123Z",
                            },
                        ],
                    ),
                    (
                        "takeout-20260705T052745Z-001.zip",
                        [
                            duplicate,
                            {
                                "title": "Watched Newer Video",
                                "titleUrl": "https://www.youtube.com/watch?v=new123",
                                "time": "2026-07-05T05:27:45.123Z",
                            },
                        ],
                    ),
                ]
                for filename, rows in exports:
                    with zipfile.ZipFile(root / filename, "w") as zf:
                        zf.writestr(
                            "Takeout/YouTube and YouTube Music/history/watch-history.json",
                            json.dumps(rows),
                        )

                first_import = core.import_history(
                    argparse.Namespace(db=str(db_path), takeout=str(root), history_key="")
                )
                second_import = core.import_history(
                    argparse.Namespace(db=str(db_path), takeout=str(root), history_key="")
                )
                conn = core.connect(db_path)
                try:
                    rows = conn.execute(
                        """
                        SELECT video_id, watched_at, takeout_history_key
                        FROM history_events
                        WHERE takeout_history_key IS NOT NULL
                        ORDER BY watched_at
                        """
                    ).fetchall()
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root

        self.assertEqual(len(rows), 3)
        self.assertEqual(first_import["inserted_watch_rows"], 3)
        self.assertEqual(first_import["duplicate_watch_rows"], 1)
        self.assertEqual(second_import["inserted_watch_rows"], 0)
        self.assertEqual(second_import["duplicate_watch_rows"], 4)
        self.assertEqual([row["video_id"] for row in rows], ["old123", "dup123", "new123"])
        self.assertEqual(
            sorted({row["takeout_history_key"] for row in rows}),
            ["20260704T052745Z", "20260705T052745Z"],
        )

    def test_import_history_uses_newest_playlist_snapshot_and_skips_tombstones(self) -> None:
        original_root = core.ROOT
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            core.ROOT = root
            try:
                db_path = root / "library.sqlite3"
                core.migrate_database(db_path)
                conn = core.connect(db_path)
                try:
                    with conn:
                        core.tombstone_playlist(
                            conn,
                            "PLdeleted",
                            reason="explicit_user",
                            observed_at="2026-07-02T00:00:00Z",
                        )
                finally:
                    conn.close()

                snapshots = [
                    (
                        "takeout-20260701T000000Z-001.zip",
                        [("PLold", "Old playlist", "Private", "oldvideo01")],
                    ),
                    (
                        "takeout-20260702T000000Z-001.zip",
                        [
                            ("PLnew", "New playlist", "Private", "newvideo01"),
                            ("PLdeleted", "Deleted playlist", "Private", "deletedvid1"),
                        ],
                    ),
                ]
                for filename, playlists in snapshots:
                    with zipfile.ZipFile(root / filename, "w") as zf:
                        zf.writestr(
                            "Takeout/YouTube and YouTube Music/history/watch-history.json",
                            "[]",
                        )
                        zf.writestr(
                            "Takeout/YouTube and YouTube Music/playlists/playlists.csv",
                            "Playlist ID,Playlist Title (Original),Playlist Visibility\n"
                            + "".join(
                                f"{playlist_id},{title},{visibility}\n"
                                for playlist_id, title, visibility, _video_id in playlists
                            ),
                        )
                        for _playlist_id, title, _visibility, video_id in playlists:
                            zf.writestr(
                                "Takeout/YouTube and YouTube Music/playlists/"
                                f"{title}-videos.csv",
                                "Video ID,Playlist Video Creation Timestamp\n"
                                f"{video_id},2026-07-01T00:00:00Z\n",
                            )

                result = core.import_history(
                    argparse.Namespace(db=str(db_path), takeout=str(root), history_key="")
                )
                conn = core.connect(db_path)
                try:
                    playlist_ids = [
                        row["playlist_id"]
                        for row in conn.execute(
                            "SELECT playlist_id FROM playlists ORDER BY playlist_id"
                        )
                    ]
                    video_ids = [
                        row["video_id"]
                        for row in conn.execute(
                            "SELECT video_id FROM videos ORDER BY video_id"
                        )
                    ]
                finally:
                    conn.close()
            finally:
                core.ROOT = original_root

        self.assertEqual(playlist_ids, ["PLnew"])
        self.assertEqual(video_ids, ["newvideo01"])
        self.assertEqual(result["playlist_stats"]["playlists"], 1)
        self.assertEqual(result["playlist_stats"]["items"], 1)
        self.assertEqual(result["playlist_stats"]["tombstoned"], 1)

    def test_extract_reaction_from_toggled_buttons(self) -> None:
        liked = {
            "segmentedLikeDislikeButtonViewModel": {
                "likeButtonViewModel": {
                    "toggleButtonViewModel": {
                        "isToggled": True,
                        "defaultIcon": {"iconType": "LIKE"},
                        "accessibilityText": "Unlike this video",
                    }
                }
            }
        }
        disliked = {
            "segmentedLikeDislikeButtonViewModel": {
                "dislikeButtonViewModel": {
                    "toggleButtonViewModel": {
                        "isToggled": True,
                        "defaultIcon": {"iconType": "DISLIKE"},
                        "accessibilityText": "Remove dislike",
                    }
                }
            }
        }

        self.assertEqual(core.extract_reaction_from_initial_data(liked), "LIKE")
        self.assertEqual(core.extract_reaction_from_initial_data(disliked), "DISLIKE")
        self.assertEqual(core.extract_reaction_from_initial_data({"isToggled": False}), "")

    def test_extract_reaction_from_like_status_entity(self) -> None:
        liked = {
            "segmentedLikeDislikeButtonViewModel": {
                "likeButtonViewModel": {
                    "likeButtonViewModel": {
                        "likeStatusEntity": {"likeStatus": "LIKE"}
                    }
                }
            }
        }
        disliked = {
            "segmentedLikeDislikeButtonViewModel": {
                "likeButtonViewModel": {
                    "likeButtonViewModel": {
                        "likeStatusEntity": {"likeStatus": "DISLIKE"}
                    }
                }
            }
        }
        indifferent = {
            "segmentedLikeDislikeButtonViewModel": {
                "likeButtonViewModel": {
                    "likeButtonViewModel": {
                        "likeStatusEntity": {"likeStatus": "INDIFFERENT"}
                    }
                }
            }
        }

        self.assertEqual(core.extract_reaction_from_initial_data(liked), "LIKE")
        self.assertEqual(core.extract_reaction_from_initial_data(disliked), "DISLIKE")
        self.assertEqual(core.extract_reaction_from_initial_data(indifferent), "INDIFFERENT")

    def test_extract_channel_handle_aliases_only_uses_owner_scoped_endpoints(self) -> None:
        channel_id = "UCYrXHY9MvPNpoa3uSGatOrA"
        initial_data = {
            "metadata": {
                "channelMetadataRenderer": {
                    "externalId": channel_id,
                    "ownerUrls": ["https://www.youtube.com/@DJICONmusic"],
                },
            },
            "tabs": [
                {
                    "tabRenderer": {
                        "endpoint": {
                            "commandMetadata": {
                                "webCommandMetadata": {
                                    "url": "/@DJICONmusic/featured",
                                },
                            },
                            "browseEndpoint": {
                                "browseId": channel_id,
                                "canonicalBaseUrl": "/@DJICONmusic",
                            },
                        },
                    },
                },
            ],
            "featured": {
                "gridChannelRenderer": {
                    "navigationEndpoint": {
                        "commandMetadata": {
                            "webCommandMetadata": {"url": "/@featured-channel"},
                        },
                        "browseEndpoint": {
                            "browseId": "UCfeatured12345678901234",
                            "canonicalBaseUrl": "/@featured-channel",
                        },
                    },
                },
            },
            "subscriptions": {
                "gridChannelRenderer": {
                    "navigationEndpoint": {
                        "commandMetadata": {
                            "webCommandMetadata": {"url": "/@subscribed-channel"},
                        },
                        "browseEndpoint": {
                            "browseId": "UCsubscribed12345678901",
                            "canonicalBaseUrl": "/@subscribed-channel",
                        },
                    },
                },
            },
        }

        self.assertEqual(
            core.extract_channel_handle_aliases(initial_data, channel_id),
            "@DJICONmusic",
        )

    def test_extract_channel_featured_channels_requires_named_shelf(self) -> None:
        owner_channel_id = "UCowner1234567890123456"

        def channel_renderer(channel_id: str, title: str, handle: str) -> dict:
            return {
                "gridChannelRenderer": {
                    "channelId": channel_id,
                    "title": {"simpleText": title},
                    "navigationEndpoint": {
                        "commandMetadata": {
                            "webCommandMetadata": {"url": f"/{handle}"},
                        },
                        "browseEndpoint": {
                            "browseId": channel_id,
                            "canonicalBaseUrl": f"/{handle}",
                        },
                    },
                }
            }

        initial_data = {
            "featured": {
                "shelfRenderer": {
                    "title": {"simpleText": "Featured"},
                    "content": {
                        "horizontalListRenderer": {
                            "items": [
                                channel_renderer(
                                    "UCfeatured12345678901234",
                                    "Featured Friend",
                                    "@featured-friend",
                                ),
                                channel_renderer(
                                    owner_channel_id,
                                    "Owner duplicate",
                                    "@owner",
                                ),
                            ]
                        }
                    },
                }
            },
            "subscriptions": {
                "shelfRenderer": {
                    "title": {"simpleText": "Subscriptions"},
                    "content": {
                        "horizontalListRenderer": {
                            "items": [
                                channel_renderer(
                                    "UCsubscribed12345678901",
                                    "Subscribed channel",
                                    "@subscribed-channel",
                                )
                            ]
                        }
                    },
                }
            },
        }

        expected = [
            {
                "channel_id": "UCfeatured12345678901234",
                "title": "Featured Friend",
                "channel_reference": "@featured-friend",
                "position": 0,
            }
        ]
        for shelf_title in ("Featured", "Featured Channels"):
            with self.subTest(shelf_title=shelf_title):
                initial_data["featured"]["shelfRenderer"]["title"] = {
                    "simpleText": shelf_title,
                }
                self.assertEqual(
                    core.extract_channel_featured_channels(
                        initial_data,
                        owner_channel_id,
                    ),
                    expected,
                )

    def test_successful_channel_metadata_authoritatively_replaces_featured_channels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_channel(conn, "UCowner", title="Owner")
                    conn.execute(
                        """
                        INSERT INTO channel_featured_channels(
                          owner_channel_id, featured_channel_id, title,
                          channel_reference, position
                        )
                        VALUES ('UCowner', 'UCold', 'Old feature', '@old', 0)
                        """
                    )
                    core.store_channel_metadata(
                        conn,
                        {
                            "channel_id": "UCowner",
                            "channel": "Owner",
                            "channel_featured_channels_observed": False,
                            "channel_featured_channels": [],
                        },
                        "error",
                    )
                preserved = conn.execute(
                    "SELECT featured_channel_id FROM channel_featured_channels "
                    "WHERE owner_channel_id = 'UCowner'"
                ).fetchone()[0]
                with conn:
                    core.store_channel_metadata(
                        conn,
                        {
                            "channel_id": "UCowner",
                            "channel": "Owner",
                            "channel_featured_channels_observed": True,
                            "channel_featured_channels": [
                                {
                                    "channel_id": "UCnew",
                                    "title": "New feature",
                                    "channel_reference": "@new",
                                }
                            ],
                        },
                        "ok",
                    )
                replaced = conn.execute(
                    "SELECT featured_channel_id, title, channel_reference, position "
                    "FROM channel_featured_channels WHERE owner_channel_id = 'UCowner'"
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(preserved, "UCold")
        self.assertEqual(
            [tuple(row) for row in replaced],
            [("UCnew", "New feature", "@new", 0)],
        )

    def test_successful_channel_metadata_replaces_polluted_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = migrated_connection(Path(temp_dir) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_channel(
                        conn,
                        "UCchannel",
                        aliases="@owner, @featured, @subscribed",
                    )
                    core.store_channel_metadata(
                        conn,
                        {
                            "channel_id": "UCchannel",
                            "channel_aliases": "@owner",
                            "channel_aliases_observed": True,
                        },
                        "ok",
                    )
                aliases = conn.execute(
                    "SELECT aliases FROM channels WHERE channel_id = 'UCchannel'"
                ).fetchone()["aliases"]
            finally:
                conn.close()

        self.assertEqual(aliases, "@owner")

    def test_resolve_metadata_target_for_direct_ids(self) -> None:
        self.assertEqual(core.resolve_metadata_target(None, "abc-123_DEF"), ("video", "abc-123_DEF"))
        self.assertEqual(
            core.resolve_metadata_target(None, "UCvmGOqGlxOgpZDoszBbWxmA"),
            ("channel", "UCvmGOqGlxOgpZDoszBbWxmA"),
        )

    def test_useful_video_metadata_rejects_youtube_unavailable_placeholder(self) -> None:
        self.assertFalse(
            core.useful_video_metadata(
                {
                    "title": "- YouTube",
                    "yt_status": "ERROR: Video unavailable",
                    "channel_id": "",
                }
            )
        )
        self.assertTrue(
            core.useful_video_metadata(
                {
                    "title": "Recovered title",
                    "yt_status": "DELETED_FULL_META",
                    "channel_id": "UC95ANqPeSKRNEH1CaCOs2ew",
                }
            )
        )
        self.assertTrue(
            core.useful_video_metadata(
                {
                    "title": "",
                    "yt_status": "DELETED_ID_ONLY",
                    "channel_id": "UCWglcpI-xTAXb_QYecQ2O4g",
                    "thumbnail_path": "video_thumbs/aeXIgKuX_zY.jpg",
                }
            )
        )

    def test_unavailable_watch_metadata_does_not_keep_header_channel(self) -> None:
        html = """
        <html><head><title>- YouTube</title></head><body>
        <script>
        var ytInitialPlayerResponse = {
          "playabilityStatus": {"status": "ERROR", "reason": {"simpleText": "Video unavailable"}},
          "videoDetails": {},
          "microformat": {"playerMicroformatRenderer": {}}
        };
        var ytInitialData = {
          "metadata": {"channelMetadataRenderer": {
            "externalId": "UCnUc4Kc09vNJ3yBu6-MJHTQ",
            "title": "Gir Bot",
            "ownerUrls": ["https://www.youtube.com/channel/UCnUc4Kc09vNJ3yBu6-MJHTQ"]
          }}
        };
        </script>
        </body></html>
        """

        metadata = core.extract_watch_metadata(html, "vy_t101tY1I")

        self.assertEqual(metadata["yt_status"], "ERROR: Video unavailable")
        self.assertEqual(metadata["channel_id"], "")
        self.assertEqual(metadata["channel"], "")
        self.assertEqual(metadata["channel_url"], "")
        self.assertEqual(metadata["channel_thumbnail_url"], "")
        self.assertEqual(metadata["availability"], "unavailable")

    def test_watch_metadata_exposes_raw_playability_status(self) -> None:
        html = """
        <html><body>
        <script>
        var ytInitialPlayerResponse = {
          "playabilityStatus": {"status": "OK"},
          "videoDetails": {"title": "Members video", "author": "Creator"},
          "microformat": {"playerMicroformatRenderer": {"isUnlisted": false}}
        };
        var ytInitialData = {};
        </script>
        </body></html>
        """

        metadata = core.extract_watch_metadata(html, "jhtY3OsTuwk")

        self.assertEqual(metadata["yt_status"], "OK")
        self.assertEqual(metadata["playability_status"], "OK")
        self.assertEqual(metadata["availability"], "public")
        self.assertEqual(core.watch_playability_value(metadata), 1)

    def test_content_check_gate_is_preserved_while_authenticated_player_resolves(self) -> None:
        html = """
        <html><body>
        <script>
        var ytInitialPlayerResponse = {
          "playabilityStatus": {
            "status": "CONTENT_CHECK_REQUIRED",
            "reason": {"simpleText": "This content may contain sensitive topics."}
          }
        };
        var ytInitialData = {};
        </script>
        <script>
        ytcfg.set({
          "INNERTUBE_API_KEY": "api-key",
          "INNERTUBE_CLIENT_NAME": "WEB",
          "INNERTUBE_CLIENT_VERSION": "2.20260811.00.00"
        });
        </script>
        </body></html>
        """
        resolved_player = {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {
                "title": "Resolved sensitive video",
                "author": "Careful Creator",
                "lengthSeconds": "90",
            },
            "microformat": {
                "playerMicroformatRenderer": {
                    "isUnlisted": False,
                    "canonicalUrl": "https://www.youtube.com/watch?v=content1234",
                }
            },
        }
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        with (
            patch.object(core, "request_text", return_value=html),
            patch.object(
                core,
                "request_youtubei_json",
                return_value=resolved_player,
            ) as request_player,
            patch.object(core, "cache_video_thumbnail", return_value=""),
            patch.object(core, "cache_channel_thumbnail", return_value=""),
        ):
            metadata = core.fetch_watch_metadata(
                opener,
                "content1234",
                Path("thumbs"),
            )

        self.assertEqual(metadata["title"], "Resolved sensitive video")
        self.assertEqual(metadata["playability_status"], "OK")
        self.assertEqual(metadata["availability"], "public")
        self.assertTrue(metadata["content_check_required"])
        self.assertEqual(
            metadata["content_check_reason"],
            "This content may contain sensitive topics.",
        )
        args = request_player.call_args.args
        self.assertEqual(args[2], "api-key")
        self.assertEqual(args[3]["videoId"], "content1234")
        self.assertTrue(args[3]["racyCheckOk"])
        self.assertTrue(args[3]["contentCheckOk"])
        self.assertEqual(request_player.call_args.kwargs["api_path"], "player")

    def test_content_check_metadata_is_stored_and_cleared_by_later_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "content1234",
                        title="Sensitive video",
                        content_check_required=True,
                        content_check_reason="Sensitive subject matter",
                        source="metadata",
                    )
                    core.upsert_video(
                        conn,
                        "content1234",
                        title="Sensitive video",
                        content_check_required=None,
                        source="metadata",
                    )
                preserved = conn.execute(
                    "SELECT content_check_required, content_check_reason "
                    "FROM videos WHERE video_id = 'content1234'"
                ).fetchone()
                self.assertEqual(tuple(preserved), (1, "Sensitive subject matter"))

                with conn:
                    core.upsert_video(
                        conn,
                        "content1234",
                        title="Sensitive video",
                        content_check_required=False,
                        content_check_reason="",
                        source="metadata",
                    )
                cleared = conn.execute(
                    "SELECT content_check_required, content_check_reason "
                    "FROM videos WHERE video_id = 'content1234'"
                ).fetchone()
                self.assertEqual(tuple(cleared), (0, ""))
            finally:
                conn.close()

    def test_watch_metadata_classifies_unlisted_visibility(self) -> None:
        html = """
        <html><body>
        <script>
        var ytInitialPlayerResponse = {
          "playabilityStatus": {"status": "OK"},
          "videoDetails": {"title": "Unlisted video", "author": "Creator"},
          "microformat": {"playerMicroformatRenderer": {
            "isUnlisted": true,
            "category": "Music",
            "isShortsEligible": false,
            "canonicalUrl": "https://www.youtube.com/watch?v=unlisted123"
          }}
        };
        var ytInitialData = {};
        </script>
        </body></html>
        """

        metadata = core.extract_watch_metadata(html, "unlisted123")

        self.assertEqual(metadata["availability"], "unlisted")
        self.assertEqual(metadata["uploader_category"], "Music")
        self.assertEqual(metadata["video_type"], "video")
        self.assertEqual(core.storable_watch_playability_value(metadata), 1)
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.store_video_metadata(conn, metadata, "ok")
                stored = conn.execute(
                    """
                    SELECT is_playable, availability, uploader_category, video_type
                    FROM videos
                    WHERE video_id = 'unlisted123'
                    """
                ).fetchone()
                self.assertEqual(
                    dict(stored),
                    {
                        "is_playable": 1,
                        "availability": "unlisted",
                        "uploader_category": "Music",
                        "video_type": "video",
                    },
                )
            finally:
                conn.close()

    def test_watch_metadata_classifies_shorts_and_livestream_video_types(self) -> None:
        fixtures = (
            (
                "short123456",
                {
                    "videoDetails": {"title": "A Short", "isLiveContent": False},
                    "microformat": {
                        "playerMicroformatRenderer": {
                            "isShortsEligible": True,
                            "canonicalUrl": "https://www.youtube.com/shorts/short123456",
                        }
                    },
                },
                "short",
            ),
            (
                "live1234567",
                {
                    "videoDetails": {"title": "A Stream", "isLiveContent": True},
                    "microformat": {
                        "playerMicroformatRenderer": {
                            "isShortsEligible": False,
                            "canonicalUrl": "https://www.youtube.com/watch?v=live1234567",
                            "liveBroadcastDetails": {"isLiveNow": False},
                        }
                    },
                },
                "livestream",
            ),
            (
                "upcoming123",
                {
                    "videoDetails": {
                        "title": "An upcoming Premiere",
                        "isLiveContent": False,
                        "isUpcoming": True,
                    },
                    "microformat": {
                        "playerMicroformatRenderer": {
                            "isShortsEligible": False,
                            "canonicalUrl": (
                                "https://www.youtube.com/watch?v=upcoming123"
                            ),
                            "liveBroadcastDetails": {
                                "isLiveNow": False,
                                "startTimestamp": "2026-08-13T16:00:00+00:00",
                            },
                        }
                    },
                },
                "livestream",
            ),
            (
                "premiere123",
                {
                    "videoDetails": {
                        "title": "A completed Premiere",
                        "isLiveContent": False,
                    },
                    "microformat": {
                        "playerMicroformatRenderer": {
                            "isShortsEligible": False,
                            "canonicalUrl": (
                                "https://www.youtube.com/watch?v=premiere123"
                            ),
                            "liveBroadcastDetails": {
                                "isLiveNow": False,
                                "startTimestamp": "2026-05-08T22:00:06+00:00",
                                "endTimestamp": "2026-05-08T22:20:42+00:00",
                            },
                        }
                    },
                },
                "video",
            ),
        )
        for video_id, player, expected_type in fixtures:
            with self.subTest(video_type=expected_type):
                player["playabilityStatus"] = {"status": "OK"}
                html = (
                    "<script>var ytInitialPlayerResponse = "
                    + json.dumps(player)
                    + "; var ytInitialData = {};</script>"
                )
                metadata = core.extract_watch_metadata(html, video_id)
                self.assertEqual(metadata["video_type"], expected_type)

    def test_youtube_broadcast_metadata_distinguishes_lifecycle_states(self) -> None:
        observed_at = "2026-08-11T12:00:00Z"
        cases = (
            (
                {"isLiveContent": True, "isLive": True},
                {
                    "liveBroadcastDetails": {
                        "isLiveNow": True,
                        "startTimestamp": "2026-08-11T11:00:00+00:00",
                    }
                },
                ("live", "2026-08-11T11:00:00Z", None),
            ),
            (
                {"isLiveContent": True, "isUpcoming": True},
                {
                    "liveBroadcastDetails": {
                        "startTimestamp": "2026-08-12T11:00:00+00:00",
                    }
                },
                ("upcoming", "2026-08-12T11:00:00Z", None),
            ),
            (
                {"isLiveContent": True},
                {
                    "liveBroadcastDetails": {
                        "startTimestamp": "2026-08-10T11:00:00+00:00",
                        "endTimestamp": "2026-08-10T12:00:00+00:00",
                    }
                },
                ("ended", "2026-08-10T11:00:00Z", "2026-08-10T12:00:00Z"),
            ),
            (
                {"isLiveContent": True},
                {},
                (None, None, None),
            ),
            (
                {"isLiveContent": False},
                {},
                ("", "", ""),
            ),
            (
                {"isLiveContent": False},
                {
                    "liveBroadcastDetails": {
                        "startTimestamp": "2026-05-08T22:00:06+00:00",
                        "endTimestamp": "2026-05-08T22:20:42+00:00",
                    }
                },
                ("", "", ""),
            ),
            (
                {},
                {},
                (None, None, None),
            ),
        )

        for details, microformat, expected in cases:
            with self.subTest(expected=expected):
                metadata = core.youtube_broadcast_metadata(
                    details,
                    microformat,
                    observed_at=observed_at,
                )
                self.assertEqual(
                    (
                        metadata["broadcast_status"],
                        metadata["broadcast_started_at"],
                        metadata["broadcast_ended_at"],
                    ),
                    expected,
                )

    def test_watch_metadata_classifies_upcoming_premiere(self) -> None:
        player = {
            "playabilityStatus": {
                "status": "LIVE_STREAM_OFFLINE",
                "reason": "Premieres in 44 hours",
            },
            "videoDetails": {
                "videoId": "upcoming123",
                "title": "An upcoming Premiere",
                "author": "Creator",
                "isLiveContent": False,
                "isUpcoming": True,
                "isPrivate": False,
            },
            "microformat": {
                "playerMicroformatRenderer": {
                    "isUnlisted": False,
                    "isShortsEligible": False,
                    "canonicalUrl": "https://www.youtube.com/watch?v=upcoming123",
                    "liveBroadcastDetails": {
                        "isLiveNow": False,
                        "startTimestamp": "2026-08-13T16:00:00+00:00",
                    },
                }
            },
        }
        html = (
            "<script>var ytInitialPlayerResponse = "
            + json.dumps(player)
            + "; var ytInitialData = {};</script>"
        )

        metadata = core.extract_watch_metadata(html, "upcoming123")

        self.assertEqual(metadata["availability"], "public")
        self.assertEqual(metadata["video_type"], "livestream")
        self.assertEqual(metadata["broadcast_status"], "upcoming")
        self.assertEqual(metadata["broadcast_started_at"], "2026-08-13T16:00:00Z")
        self.assertEqual(core.storable_watch_playability_value(metadata), 0)
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.store_video_metadata(conn, metadata, "ok")
                stored = conn.execute(
                    """
                    SELECT is_playable, availability, video_type,
                           broadcast_status, broadcast_started_at
                    FROM videos
                    WHERE video_id = 'upcoming123'
                    """
                ).fetchone()
                self.assertEqual(
                    dict(stored),
                    {
                        "is_playable": 0,
                        "availability": "public",
                        "video_type": "livestream",
                        "broadcast_status": "upcoming",
                        "broadcast_started_at": "2026-08-13T16:00:00Z",
                    },
                )
            finally:
                conn.close()

    def test_watch_metadata_classifies_and_persists_movie_metadata(self) -> None:
        player = {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {
                "videoId": "movie123456",
                "title": "Example Movie",
                "author": "YouTube Movies",
                "isLiveContent": False,
                "isTvfilmVideo": True,
            },
            "microformat": {
                "playerMicroformatRenderer": {
                    "category": "Movies",
                    "isShortsEligible": False,
                    "canonicalUrl": "https://www.youtube.com/watch?v=movie123456",
                }
            },
        }
        initial = {
            "contents": [
                {
                    "videoPrimaryInfoRenderer": {
                        "badges": [
                            {
                                "metadataBadgeRenderer": {
                                    "style": "BADGE_STYLE_TYPE_YPC",
                                    "label": "Free",
                                }
                            },
                            {
                                "metadataBadgeRenderer": {
                                    "style": "BADGE_STYLE_TYPE_MEDIA",
                                    "label": "R",
                                }
                            },
                        ]
                    }
                },
                {
                    "videoSecondaryInfoRenderer": {
                        "metadataRowContainer": {
                            "metadataRowContainerRenderer": {
                                "rows": [
                                    {
                                        "metadataRowRenderer": {
                                            "title": {"simpleText": "Rating"},
                                            "contents": [{"simpleText": "R"}],
                                        }
                                    },
                                    {
                                        "metadataRowRenderer": {
                                            "title": {"simpleText": "Release date"},
                                            "contents": [{"simpleText": "2015"}],
                                        }
                                    },
                                    {
                                        "metadataRowRenderer": {
                                            "title": {"simpleText": "Actors"},
                                            "contents": [{"simpleText": "Not stored"}],
                                        }
                                    },
                                ]
                            }
                        }
                    }
                },
            ]
        }
        html = (
            "<script>var ytInitialPlayerResponse = "
            + json.dumps(player)
            + "; var ytInitialData = "
            + json.dumps(initial)
            + ";</script>"
        )

        metadata = core.extract_watch_metadata(html, "movie123456")

        self.assertEqual(metadata["video_type"], "movie")
        self.assertEqual(metadata["movie_rating"], "R")
        self.assertEqual(metadata["movie_release_date"], "2015")
        self.assertEqual(metadata["movie_offer"], "Free")
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.store_video_metadata(conn, metadata, "ok")
                stored = conn.execute(
                    """
                    SELECT video_type, movie_rating, movie_release_date, movie_offer
                    FROM videos
                    WHERE video_id = 'movie123456'
                    """
                ).fetchone()
                self.assertEqual(
                    dict(stored),
                    {
                        "video_type": "movie",
                        "movie_rating": "R",
                        "movie_release_date": "2015",
                        "movie_offer": "Free",
                    },
                )
            finally:
                conn.close()

    def test_watch_metadata_extracts_resolution_360_and_hdr_features(self) -> None:
        player = {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {"videoId": "feature360a", "title": "Feature Video"},
            "streamingData": {
                "adaptiveFormats": [
                    {
                        "mimeType": "video/webm; codecs=\"vp9\"",
                        "height": 4320,
                        "qualityLabel": "4320s",
                        "projectionType": "EQUIRECTANGULAR",
                    },
                    {
                        "mimeType": "video/webm; codecs=\"vp9.2\"",
                        "height": 2160,
                        "qualityLabel": "2160p60 HDR",
                        "transferCharacteristics": (
                            "COLOR_TRANSFER_CHARACTERISTICS_SMPTEST2084"
                        ),
                    },
                    {"mimeType": "audio/webm; codecs=\"opus\""},
                ]
            },
        }
        html = (
            "<script>var ytInitialPlayerResponse = "
            + json.dumps(player)
            + "; var ytInitialData = {};</script>"
        )

        metadata = core.extract_watch_metadata(html, "feature360a")

        self.assertEqual(metadata["max_video_height"], 4320)
        self.assertEqual(metadata["spatial_format"], "360")
        self.assertEqual(metadata["stereo_layout"], "")
        self.assertEqual(metadata["dynamic_range"], "hdr")

    def test_watch_metadata_extracts_youtube_ai_disclosure(self) -> None:
        player = {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {"videoId": "29ItBOZKsbM", "title": "Even My Mamma"},
        }
        initial = {
            "howThisWasMadeSectionViewModel": {
                "sectionTitle": {"content": "How this was made"},
                "bodyHeader": {"content": "Made with AI"},
                "bodyText": {
                    "content": "Sounds or visuals were altered or fully generated. Learn more"
                },
            }
        }
        html = (
            "<script>var ytInitialPlayerResponse = "
            + json.dumps(player)
            + "; var ytInitialData = "
            + json.dumps(initial)
            + ";</script>"
        )

        metadata = core.extract_watch_metadata(html, "29ItBOZKsbM")

        self.assertTrue(metadata["ai_disclosure"])
        self.assertEqual(
            metadata["ai_disclosure_text"],
            "Sounds or visuals were altered or fully generated. Learn more",
        )

    def test_watch_metadata_does_not_treat_auto_dubbing_as_made_with_ai(self) -> None:
        player = {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {"videoId": "autodub12345", "title": "Auto-dubbed"},
        }
        initial = {
            "howThisWasMadeSectionViewModel": {
                "sectionTitle": {"content": "How this was made"},
                "bodyHeader": {"content": "Auto-dubbed"},
                "bodyText": {
                    "content": (
                        "Audio tracks for some languages were automatically "
                        "generated. Learn more"
                    )
                },
            }
        }
        html = (
            "<script>var ytInitialPlayerResponse = "
            + json.dumps(player)
            + "; var ytInitialData = "
            + json.dumps(initial)
            + ";</script>"
        )

        metadata = core.extract_watch_metadata(html, "autodub12345")

        self.assertFalse(metadata["ai_disclosure"])
        self.assertEqual(metadata["ai_disclosure_text"], "")
        self.assertTrue(metadata["auto_dubbed"])

    def test_auto_dubbing_requires_explicit_video_evidence(self) -> None:
        player = {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {"title": "Japanese news"},
        }
        for initial in (
            {"videoPrimaryInfoRenderer": {"badges": [
                {"metadataBadgeRenderer": {"label": "Auto-dubbed"}}
            ]}},
            {"howThisWasMadeSectionViewModel": {"bodyText": {
                "content": "Audio tracks for some languages were automatically generated."
            }}},
        ):
            with self.subTest(initial=initial):
                self.assertEqual(
                    core.youtube_auto_dubbed_metadata(player, initial),
                    {"auto_dubbed": True},
                )
        dubbed_player = {**player, "streamingData": {"adaptiveFormats": [
            {"audioTrack": {"id": "en-US.10", "isAutoDubbed": True}}
        ]}}
        self.assertEqual(
            core.youtube_auto_dubbed_metadata(dubbed_player, {}),
            {"auto_dubbed": True},
        )
        manual_player = {**player, "streamingData": {"adaptiveFormats": [
            {"audioTrack": {"id": "en.1", "displayName": "English"}},
            {"audioTrack": {"id": "ja.1", "displayName": "Japanese original"}},
        ]}}
        unrelated = {
            "recommendation": {"metadataBadgeRenderer": {"label": "Auto-dubbed"}},
            "howThisWasMadeSectionViewModel": {
                "bodyHeader": {"content": "Made with AI"},
            },
        }
        self.assertEqual(
            core.youtube_auto_dubbed_metadata(manual_player, unrelated),
            {"auto_dubbed": False},
        )
        self.assertEqual(
            core.youtube_auto_dubbed_metadata(player, {}), {"auto_dubbed": None}
        )
        self.assertEqual(
            core.youtube_auto_dubbed_metadata(
                {"playabilityStatus": {"status": "ERROR"}}, unrelated
            ),
            {"auto_dubbed": None},
        )

    def test_auto_dubbing_observations_survive_inconclusive_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                for observation, expected in ((True, 1), (None, 1), (False, 0)):
                    with conn:
                        core.store_video_metadata(
                            conn,
                            {"video_id": "autodub12345", "auto_dubbed": observation},
                            "ok" if observation is not None else "no_metadata",
                        )
                    self.assertEqual(
                        conn.execute(
                            "SELECT auto_dubbed FROM videos WHERE video_id = ?",
                            ("autodub12345",),
                        ).fetchone()[0],
                        expected,
                    )
            finally:
                conn.close()

    def test_ai_disclosure_checks_all_how_this_was_made_sections(self) -> None:
        metadata = core.youtube_ai_disclosure_metadata(
            {
                "playabilityStatus": {"status": "OK"},
                "videoDetails": {"title": "Multiple disclosures"},
            },
            {
                "sections": [
                    {
                        "howThisWasMadeSectionViewModel": {
                            "bodyHeader": {"content": "Auto-dubbed"},
                            "bodyText": {
                                "content": (
                                    "Audio tracks for some languages were "
                                    "automatically generated. Learn more"
                                )
                            },
                        }
                    },
                    {
                        "howThisWasMadeSectionViewModel": {
                            "bodyHeader": {"content": "Made with AI"},
                            "bodyText": {
                                "content": (
                                    "Sounds or visuals were altered or fully "
                                    "generated. Learn more"
                                )
                            },
                        }
                    },
                ]
            },
        )

        self.assertEqual(
            metadata,
            {
                "ai_disclosure": True,
                "ai_disclosure_text": (
                    "Sounds or visuals were altered or fully generated. Learn more"
                ),
            },
        )

    def test_ai_disclosure_uses_three_way_observation_state(self) -> None:
        observed = core.youtube_ai_disclosure_metadata(
            {
                "playabilityStatus": {"status": "OK"},
                "videoDetails": {"title": "Ordinary video"},
            },
            {},
        )
        inconclusive = core.youtube_ai_disclosure_metadata(
            {"playabilityStatus": {"status": "ERROR"}},
            {},
        )

        self.assertEqual(
            observed,
            {"ai_disclosure": False, "ai_disclosure_text": ""},
        )
        self.assertEqual(
            inconclusive,
            {"ai_disclosure": None, "ai_disclosure_text": None},
        )

    def test_ai_disclosure_is_preserved_then_cleared_by_authoritative_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "aistate1234",
                        title="AI state video",
                        ai_disclosure=True,
                        ai_disclosure_text="Altered or generated",
                        source="metadata",
                    )
                    core.upsert_video(
                        conn,
                        "aistate1234",
                        ai_disclosure=None,
                        source="metadata",
                    )
                preserved = conn.execute(
                    "SELECT ai_disclosure, ai_disclosure_text "
                    "FROM videos WHERE video_id = 'aistate1234'"
                ).fetchone()
                self.assertEqual(tuple(preserved), (1, "Altered or generated"))

                with conn:
                    core.upsert_video(
                        conn,
                        "aistate1234",
                        ai_disclosure=False,
                        ai_disclosure_text="",
                        source="metadata",
                    )
                cleared = conn.execute(
                    "SELECT ai_disclosure, ai_disclosure_text "
                    "FROM videos WHERE video_id = 'aistate1234'"
                ).fetchone()
                self.assertEqual(tuple(cleared), (0, ""))
            finally:
                conn.close()

    def test_watch_metadata_extracts_vr180_and_location_features(self) -> None:
        player = {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {"videoId": "featurevr180", "title": "Maui"},
            "playerConfig": {"vrConfig": {"partialSpherical": True}},
            "streamingData": {
                "adaptiveFormats": [
                    {
                        "mimeType": "video/webm; codecs=\"vp9\"",
                        "height": 2160,
                        "qualityLabel": "2160s",
                        "projectionType": "MESH",
                    }
                ]
            },
        }
        initial = {
            "videoPrimaryInfoRenderer": {
                "badges": [{"metadataBadgeRenderer": {"label": "VR180"}}],
                "superTitleLink": {
                    "runs": [{"text": "MAUI"}],
                    "accessibility": {
                        "accessibilityData": {
                            "label": (
                                "Link to a location restricted search for videos "
                                "geo tagged with Maui"
                            )
                        }
                    },
                },
            }
        }
        html = (
            "<script>var ytInitialPlayerResponse = "
            + json.dumps(player)
            + "; var ytInitialData = "
            + json.dumps(initial)
            + ";</script>"
        )

        metadata = core.extract_watch_metadata(html, "featurevr180")

        self.assertEqual(metadata["spatial_format"], "vr180")
        self.assertEqual(metadata["location_name"], "MAUI")

    def test_watch_metadata_extracts_stereo_and_license_features(self) -> None:
        player = {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {"videoId": "feature3dabc", "title": "Stereo Video"},
            "streamingData": {
                "adaptiveFormats": [
                    {
                        "mimeType": "video/mp4; codecs=\"avc1\"",
                        "height": 1080,
                        "projectionType": "RECTANGULAR",
                        "stereoLayout": "STEREO_LAYOUT_LEFT_RIGHT",
                    }
                ]
            },
        }
        initial = {
            "metadataRowContainerRenderer": {
                "rows": [
                    {
                        "metadataRowRenderer": {
                            "title": {"simpleText": "License"},
                            "contents": [
                                {
                                    "simpleText": (
                                        "Creative Commons Attribution license "
                                        "(reuse allowed)"
                                    )
                                }
                            ],
                        }
                    }
                ]
            }
        }
        html = (
            "<script>var ytInitialPlayerResponse = "
            + json.dumps(player)
            + "; var ytInitialData = "
            + json.dumps(initial)
            + ";</script>"
        )

        metadata = core.extract_watch_metadata(html, "feature3dabc")

        self.assertEqual(metadata["max_video_height"], 1080)
        self.assertEqual(metadata["spatial_format"], "")
        self.assertEqual(metadata["stereo_layout"], "left_right")
        self.assertEqual(metadata["dynamic_range"], "sdr")
        self.assertEqual(
            metadata["license"],
            "Creative Commons Attribution license (reuse allowed)",
        )

    def test_failed_feature_observation_preserves_prior_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "featurekeep1",
                        title="Known feature video",
                        max_video_height=2160,
                        spatial_format="360",
                        stereo_layout="left_right",
                        dynamic_range="hdr",
                        license="Creative Commons Attribution license",
                        location_name="Maui",
                        source="metadata",
                    )
                    core.upsert_video(
                        conn,
                        "featurekeep1",
                        source="metadata",
                    )
                stored = conn.execute(
                    """
                    SELECT max_video_height, spatial_format, stereo_layout,
                           dynamic_range, license, location_name
                    FROM videos
                    WHERE video_id = 'featurekeep1'
                    """
                ).fetchone()
                self.assertEqual(
                    dict(stored),
                    {
                        "max_video_height": 2160,
                        "spatial_format": "360",
                        "stereo_layout": "left_right",
                        "dynamic_range": "hdr",
                        "license": "Creative Commons Attribution license",
                        "location_name": "Maui",
                    },
                )
            finally:
                conn.close()

    def test_empty_feature_observation_clears_prior_special_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "featureclear",
                        title="Former special feature video",
                        max_video_height=4320,
                        spatial_format="360",
                        stereo_layout="left_right",
                        dynamic_range="hdr",
                        license="Creative Commons Attribution license",
                        location_name="Maui",
                        source="metadata",
                    )
                    core.upsert_video(
                        conn,
                        "featureclear",
                        max_video_height=1080,
                        spatial_format="",
                        stereo_layout="",
                        dynamic_range="sdr",
                        license="",
                        location_name="",
                        source="metadata",
                    )
                stored = conn.execute(
                    """
                    SELECT max_video_height, spatial_format, stereo_layout,
                           dynamic_range, license, location_name
                    FROM videos
                    WHERE video_id = 'featureclear'
                    """
                ).fetchone()
                self.assertEqual(
                    dict(stored),
                    {
                        "max_video_height": 1080,
                        "spatial_format": "",
                        "stereo_layout": "",
                        "dynamic_range": "sdr",
                        "license": "",
                        "location_name": "",
                    },
                )
            finally:
                conn.close()

    def test_watch_metadata_classifies_accessible_private_visibility(self) -> None:
        html = """
        <html><body>
        <script>
        var ytInitialPlayerResponse = {
          "playabilityStatus": {"status": "OK"},
          "videoDetails": {
            "title": "Private video",
            "author": "Creator",
            "isPrivate": true
          },
          "microformat": {"playerMicroformatRenderer": {"isUnlisted": false}}
        };
        var ytInitialData = {};
        </script>
        </body></html>
        """

        metadata = core.extract_watch_metadata(html, "private1234")

        self.assertEqual(metadata["availability"], "private")
        self.assertEqual(core.storable_watch_playability_value(metadata), 1)

    def test_watch_metadata_does_not_classify_bot_challenge_as_unavailable(self) -> None:
        playability = {
            "status": "LOGIN_REQUIRED",
            "reason": "Sign in to confirm you're not a bot",
        }

        self.assertEqual(core.watch_playability_availability(playability), "")

    def test_watch_metadata_classifies_members_only_playability(self) -> None:
        html = """
        <html><body>
        <script>
        var ytInitialPlayerResponse = {
          "playabilityStatus": {
            "status": "UNPLAYABLE",
            "reason": "Join this channel to get access to members-only content like this video.",
            "errorScreen": {
              "playerLegacyDesktopYpcOfferRenderer": {
                "itemTitle": "Members-only content",
                "offerDescription": "Join this channel to get access.",
                "offerId": "sponsors_only_video"
              }
            }
          },
          "videoDetails": {
            "title": "Members video",
            "author": "Creator",
            "channelId": "UCmembers123456789012345"
          },
          "microformat": {"playerMicroformatRenderer": {}}
        };
        var ytInitialData = {};
        </script>
        </body></html>
        """

        metadata = core.extract_watch_metadata(html, "members1234")

        self.assertEqual(metadata["playability_status"], "UNPLAYABLE")
        self.assertEqual(metadata["availability"], "subscriber_only")
        self.assertEqual(core.storable_watch_playability_value(metadata), 0)
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.store_video_metadata(conn, metadata, "ok")
                stored = conn.execute(
                    """
                    SELECT is_playable, availability
                    FROM videos
                    WHERE video_id = 'members1234'
                    """
                ).fetchone()
                self.assertEqual(
                    dict(stored),
                    {"is_playable": 0, "availability": "subscriber_only"},
                )
            finally:
                conn.close()

    def test_watch_metadata_classifies_accessible_members_only_badge(self) -> None:
        html = """
        <html><body>
        <script>
        var ytInitialPlayerResponse = {
          "playabilityStatus": {"status": "OK"},
          "videoDetails": {"title": "Accessible members video", "author": "Creator"},
          "microformat": {"playerMicroformatRenderer": {"isUnlisted": false}}
        };
        var ytInitialData = {
          "contents": {"twoColumnWatchNextResults": {"results": {"results": {"contents": [{
            "videoPrimaryInfoRenderer": {"badges": [{
              "metadataBadgeRenderer": {
                "style": "BADGE_STYLE_TYPE_MEMBERS_ONLY",
                "label": "Members only"
              }
            }]}
          }]}}}}
        };
        </script>
        </body></html>
        """

        metadata = core.extract_watch_metadata(html, "members5678")

        self.assertEqual(metadata["playability_status"], "OK")
        self.assertEqual(metadata["availability"], "subscriber_only")
        self.assertEqual(core.storable_watch_playability_value(metadata), 1)
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.store_video_metadata(conn, metadata, "ok")
                stored = conn.execute(
                    """
                    SELECT is_playable, availability
                    FROM videos
                    WHERE video_id = 'members5678'
                    """
                ).fetchone()
                self.assertEqual(
                    dict(stored),
                    {"is_playable": 1, "availability": "subscriber_only"},
                )
            finally:
                conn.close()

    def test_watch_metadata_fetch_uses_only_the_direct_video_page(self) -> None:
        opener = Mock()
        video_id = "-AbC123_def"
        metadata = {
            "title": "Direct video",
            "thumbnail_url": "",
            "channel_thumbnail_url": "",
        }
        with (
            patch.object(core, "request_text", return_value="watch page") as request_text,
            patch.object(core, "extract_watch_metadata", return_value=metadata),
            patch.object(core, "cache_video_thumbnail", return_value=""),
            patch.object(core, "cache_channel_thumbnail", return_value=""),
        ):
            result = core.fetch_watch_metadata(opener, video_id, Path("thumbs"))

        request_text.assert_called_once_with(
            opener,
            "https://www.youtube.com/watch?v=-AbC123_def",
        )
        self.assertNotIn("watch_progress_percent", result)
        self.assertNotIn("watch_resume_seconds", result)

    def test_store_video_metadata_updates_canonical_watch_playability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                conn.execute(
                    "INSERT INTO playlists(playlist_id, title) VALUES ('PLmembers', 'Members')"
                )
                core.save_playlist_scan(
                    conn,
                    "PLmembers",
                    [
                        {
                            "playlist_id": "PLmembers",
                            "position": 1,
                            "video_id": "jhtY3OsTuwk",
                            "title": "Members video",
                            "channel_id": "",
                            "channel": "",
                            "duration_text": "",
                            "is_playable": 0,
                            "availability": "subscriber_only",
                            "url": "https://www.youtube.com/watch?v=jhtY3OsTuwk",
                        }
                    ],
                    "ok",
                    "",
                )
                scan = conn.execute(
                    "SELECT unavailable_count FROM playlist_scans WHERE playlist_id = 'PLmembers'"
                ).fetchone()
                self.assertEqual(scan["unavailable_count"], 0)

                core.store_video_metadata(
                    conn,
                    {
                        "video_id": "jhtY3OsTuwk",
                        "title": "Members video",
                        "playability_status": "OK",
                    },
                    "ok",
                )

                row = conn.execute(
                    """
                    SELECT is_playable, availability
                    FROM videos
                    WHERE video_id = 'jhtY3OsTuwk'
                    """
                ).fetchone()
                self.assertEqual(row["is_playable"], 1)
                self.assertEqual(row["availability"], "public")

                verified_at = conn.execute(
                    "SELECT visibility_checked_at FROM videos WHERE video_id = 'jhtY3OsTuwk'"
                ).fetchone()["visibility_checked_at"]
                core.save_playlist_scan(
                    conn,
                    "PLmembers",
                    [
                        {
                            "playlist_id": "PLmembers",
                            "position": 1,
                            "video_id": "jhtY3OsTuwk",
                            "title": "Members video",
                            "channel_id": "",
                            "channel": "",
                            "duration_text": "",
                            "is_playable": None,
                            "availability": "public",
                            "url": "https://www.youtube.com/watch?v=jhtY3OsTuwk",
                        }
                    ],
                    "ok",
                    "",
                )
                after_playlist = conn.execute(
                    """
                    SELECT is_playable, visibility_checked_at
                    FROM videos
                    WHERE video_id = 'jhtY3OsTuwk'
                    """
                ).fetchone()
                self.assertEqual(after_playlist["is_playable"], 1)
                self.assertEqual(after_playlist["visibility_checked_at"], verified_at)
            finally:
                conn.close()

    def test_metadata_error_playability_does_not_downgrade_known_public_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "vweQrjtAg0U",
                        title="Playlist title",
                        source="playlist",
                        is_playable=1,
                        availability="public",
                        updated_at="2026-07-10T23:20:04Z",
                    )
                    core.store_video_metadata(
                        conn,
                        {
                            "video_id": "vweQrjtAg0U",
                            "title": "Metadata title",
                            "channel_id": "UCddem5RlB3bQe99wyY49g0g",
                            "channel": "PeriscopeFilm",
                            "playability_status": "ERROR",
                            "yt_status": "ERROR: Video unavailable",
                            "watch_progress_percent": "0",
                            "watch_resume_seconds": "0",
                        },
                        "ok",
                        updated_at="2026-07-11T08:08:18Z",
                    )

                row = conn.execute(
                    """
                    SELECT title, is_playable, availability, fetched_at,
                           last_seen_available_at, last_checked_at
                    FROM videos
                    WHERE video_id = 'vweQrjtAg0U'
                    """
                ).fetchone()
                self.assertEqual(row["title"], "Metadata title")
                self.assertEqual(row["is_playable"], 1)
                self.assertEqual(row["availability"], "public")
                self.assertEqual(row["fetched_at"], "2026-07-11T08:08:18Z")
                self.assertEqual(row["last_seen_available_at"], "2026-07-10T23:20:04Z")
                self.assertEqual(row["last_checked_at"], "2026-07-11T08:08:18Z")
            finally:
                conn.close()

    def test_metadata_ok_playability_refreshes_known_public_seen_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "vweQrjtAg0U",
                        title="Playlist title",
                        source="playlist",
                        is_playable=1,
                        availability="public",
                        updated_at="2026-07-10T23:20:04Z",
                    )
                    core.store_video_metadata(
                        conn,
                        {
                            "video_id": "vweQrjtAg0U",
                            "title": "Metadata title",
                            "channel_id": "UCddem5RlB3bQe99wyY49g0g",
                            "channel": "PeriscopeFilm",
                            "playability_status": "OK",
                            "yt_status": "OK",
                            "watch_progress_percent": "0",
                            "watch_resume_seconds": "0",
                        },
                        "ok",
                        updated_at="2026-07-12T20:30:45Z",
                    )

                row = conn.execute(
                    """
                    SELECT is_playable, availability, last_seen_available_at
                    FROM videos
                    WHERE video_id = 'vweQrjtAg0U'
                    """
                ).fetchone()
                self.assertEqual(row["is_playable"], 1)
                self.assertEqual(row["availability"], "public")
                self.assertEqual(row["last_seen_available_at"], "2026-07-12T20:30:45Z")
            finally:
                conn.close()

    def test_manual_metadata_does_not_store_watch_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "vweQrjtAg0U",
                        title="Progress video",
                        source="metadata",
                    )
                    conn.execute(
                        """
                        INSERT INTO history_events(
                          event_id, video_id, watch_date, time_precision,
                          watch_progress_percent, watch_resume_seconds
                        )
                        VALUES (
                          'history-progress', 'vweQrjtAg0U', '2026-07-30',
                          'date_only', 64, 217
                        )
                        """
                    )
                    core.store_video_metadata(
                        conn,
                        {
                            "video_id": "vweQrjtAg0U",
                            "title": "Progress video",
                            "watch_progress_percent": "37",
                            "watch_resume_seconds": "125",
                        },
                        "ok",
                    )

                event = conn.execute(
                    """
                    SELECT watch_progress_percent, watch_resume_seconds
                    FROM history_events
                    WHERE event_id = 'history-progress'
                    """
                ).fetchone()
                video_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(videos)")
                }
                self.assertEqual(
                    dict(event),
                    {
                        "watch_progress_percent": 64,
                        "watch_resume_seconds": 217,
                    },
                )
                self.assertNotIn("watch_progress_percent", video_columns)
                self.assertNotIn("watch_resume_seconds", video_columns)
            finally:
                conn.close()

    def test_metadata_from_archivarix_video_includes_channel_metadata(self) -> None:
        metadata = core.metadata_from_archivarix_video(
            "Ax8Yn8DPZe0",
            {
                "title": "Why Do Windshields Have Those Small Black Dots?",
                "description": "Video description",
                "channelExternalId": "UC95ANqPeSKRNEH1CaCOs2ew",
                "channelTitle": "History of Simple Things",
                "channelUrl": "https://www.youtube.com/channel/UC95ANqPeSKRNEH1CaCOs2ew",
                "channelDescription": "Channel description",
                "channelAliases": "youtube.com/@historyofsimplethings",
                "channelThumbnailUrl": "https://yt3.example/avatar.jpg",
                "channelThumbnailPath": "video_thumbs/UC95ANqPeSKRNEH1CaCOs2ew.jpg",
                "channelId": "12345",
                "channelStatus": "deleted",
                "channelStatusReason": "Deleted/terminated channel reported by Archivarix.",
                "duration": 488,
                "viewCount": 399359,
                "uploadDate": "2025-03-20",
                "status": "DELETED_FULL_META",
            },
            "https://archive.example/thumb.jpg",
            "video_thumbs/Ax8Yn8DPZe0.jpg",
        )

        self.assertEqual(metadata["channel_id"], "UC95ANqPeSKRNEH1CaCOs2ew")
        self.assertEqual(metadata["channel"], "History of Simple Things")
        self.assertEqual(metadata["channel_description"], "Channel description")
        self.assertEqual(metadata["channel_aliases"], "youtube.com/@historyofsimplethings")
        self.assertEqual(metadata["archivarix_channel_id"], "12345")
        self.assertEqual(metadata["channel_status"], "deleted")
        self.assertEqual(metadata["duration_text"], "8:08")

    def test_playlist_match_type_helpers_keep_notes_out_of_rows(self) -> None:
        self.assertEqual(core.playlist_match_type_label("ambiguous_hidden_candidate"), "Takeout candidate")
        self.assertEqual(
            core.playlist_match_type_note("ambiguous_hidden_candidate"),
            "missing from current playable scan; hidden slot mapping is ambiguous",
        )
        self.assertEqual(
            core.normalize_video_availability("Ax8Yn8DPZe0", "", None, "LIVE"),
            "public",
        )
        self.assertEqual(core.normalize_video_availability("Ax8Yn8DPZe0", "live"), "public")
        self.assertEqual(core.normalize_video_availability("Ax8Yn8DPZe0", "", 1), "public")
        self.assertEqual(
            core.normalize_video_availability("Ax8Yn8DPZe0", "subscriber_only", 0),
            "subscriber_only",
        )
        self.assertEqual(core.normalize_video_availability("", "private", None, "LIVE"), "unknown")
        self.assertEqual(
            core.video_availability_category(
                {
                    "video_id": "members5678",
                    "availability": "subscriber_only",
                    "is_playable": 0,
                }
            ),
            "members_only",
        )
        self.assertEqual(
            core.video_availability_category(
                {
                    "video_id": "missing5678",
                    "availability": "unavailable",
                    "is_playable": 0,
                }
            ),
            "unavailable",
        )

    def test_history_reconciliation_labels_describe_current_fields(self) -> None:
        self.assertEqual(core.history_source_type_label("takeout_youtube"), "Takeout + YouTube")
        self.assertEqual(core.history_match_type_label("video_id_date"), "matched by video/date")
        self.assertEqual(core.history_time_quality_label("unknown"), "time unknown")
        self.assertIn("observed_at", core.history_time_quality_note("unknown"))

    def test_history_day_overlap_requires_two_complete_matching_days(self) -> None:
        tracker = core.HistoryDayOverlapTracker(
            {
                "2026-07-28": Counter({"repeat123": 1}),
                "2026-07-27": Counter({"known-a": 1, "known-b": 1}),
                "2026-07-26": Counter({"known-c": 1}),
            }
        )

        reached = tracker.add_rows(
            [
                {"video_id": "repeat123", "watch_date": "2026-07-28"},
                {"video_id": "repeat123", "watch_date": "2026-07-28"},
                {"video_id": "known-a", "watch_date": "2026-07-27"},
            ]
        )
        self.assertFalse(reached)

        reached = tracker.add_rows(
            [
                {"video_id": "known-b", "watch_date": "2026-07-27"},
                {"video_id": "known-c", "watch_date": "2026-07-26"},
                {"video_id": "older", "watch_date": "2026-07-25"},
            ]
        )

        self.assertTrue(reached)
        self.assertEqual(tracker.confirmed_days, ["2026-07-27", "2026-07-26"])

    def test_youtube_history_occurrence_counts_preserve_same_day_rewatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(conn, "repeat123", title="Repeat", source="test")
                    conn.execute(
                        """
                        INSERT INTO history_events(
                          event_id, video_id, watch_date, time_precision,
                          source_type, match_type, youtube_ordinal
                        )
                        VALUES ('legacy-position-id', 'repeat123', '2026-07-28',
                                'date_only', 'youtube', 'youtube_only', 1)
                        """
                    )
                snapshot = core.youtube_history_occurrence_snapshot(conn)

                with conn:
                    stats = core.save_youtube_history_events(
                        conn,
                        [
                            {"video_id": "repeat123", "watch_date": "2026-07-28"},
                            {"video_id": "repeat123", "watch_date": "2026-07-28"},
                        ],
                        1,
                        snapshot,
                        Counter(),
                    )

                rows = conn.execute(
                    """
                    SELECT event_id, youtube_ordinal
                    FROM history_events
                    WHERE video_id = 'repeat123' AND watch_date = '2026-07-28'
                    ORDER BY youtube_ordinal
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(stats["existing"], 1)
        self.assertEqual(stats["new"], 1)
        self.assertEqual(rows[0]["event_id"], "legacy-position-id")
        self.assertNotEqual(rows[1]["event_id"], "youtube:2")
        self.assertEqual([row["youtube_ordinal"] for row in rows], [1, 2])

    def test_youtube_history_refetch_retains_progress_for_same_event_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    first = core.save_youtube_history_events(
                        conn,
                        [
                            {
                                "video_id": "repeat123",
                                "watch_date": "2026-07-30",
                                "watch_progress_percent": 64,
                                "watch_resume_seconds": 217,
                            }
                        ],
                        1,
                        {},
                        Counter(),
                    )
                snapshot = core.youtube_history_occurrence_snapshot(conn)
                with conn:
                    second = core.save_youtube_history_events(
                        conn,
                        [
                            {
                                "video_id": "repeat123",
                                "watch_date": "2026-07-30",
                                "watch_progress_percent": 0,
                                "watch_resume_seconds": 0,
                            },
                            {
                                "video_id": "repeat123",
                                "watch_date": "2026-07-30",
                                "watch_progress_percent": 0,
                                "watch_resume_seconds": 0,
                            },
                        ],
                        1,
                        snapshot,
                        Counter(),
                    )
                events = conn.execute(
                    """
                    SELECT watch_progress_percent, watch_resume_seconds
                    FROM history_events
                    WHERE video_id = 'repeat123'
                    ORDER BY youtube_ordinal
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(first["progress_guards"], [])
        self.assertEqual(
            second["progress_guards"],
            [{"video_id": "repeat123", "reported": 0, "retained": 64}],
        )
        self.assertEqual(
            [dict(row) for row in events],
            [
                {"watch_progress_percent": 64, "watch_resume_seconds": 217},
                {"watch_progress_percent": 0, "watch_resume_seconds": 0},
            ],
        )

    def test_youtube_history_sets_channel_first_seen_from_watch_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.save_youtube_history_events(
                        conn,
                        [
                            {
                                "video_id": "historyvid",
                                "watch_date": "2021-07-06",
                                "channel_id": "UChistory",
                                "channel": "History channel",
                            }
                        ],
                        1,
                        {},
                        Counter(),
                    )
                first_seen_at = conn.execute(
                    """
                    SELECT first_seen_at
                    FROM channels
                    WHERE channel_id = 'UChistory'
                    """
                ).fetchone()["first_seen_at"]
            finally:
                conn.close()

        self.assertEqual(first_seen_at, "2021-07-06")

    def test_youtube_history_warns_without_rewriting_date_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "new-video",
                        title="New video",
                        upload_date="2026-08-03T12:31:02-07:00",
                        source="metadata",
                    )
                    stats = core.save_youtube_history_events(
                        conn,
                        [{"video_id": "new-video", "watch_date": "2026-08-02"}],
                        1,
                        {},
                        Counter(),
                        "America/Los_Angeles",
                    )
                event = conn.execute(
                    "SELECT watch_date FROM history_events WHERE video_id = 'new-video'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(event["watch_date"], "2026-08-02")
        self.assertEqual(
            stats["date_conflicts"],
            [
                {
                    "event_id": core.youtube_history_event_id("new-video", "2026-08-02", 1),
                    "video_id": "new-video",
                    "watch_date": "2026-08-02",
                    "published_date": "2026-08-03",
                }
            ],
        )

    def test_youtube_takeout_match_count_is_scoped_to_the_fetched_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    for ordinal in (5, 10):
                        video_id = f"matched-{ordinal}"
                        core.upsert_video(conn, video_id, title=video_id, source="test")
                        conn.execute(
                            """
                            INSERT INTO history_events(
                              event_id, video_id, watched_at, watch_date, time_precision,
                              source_type, match_type, youtube_ordinal,
                              takeout_history_key, takeout_row_key
                            )
                            VALUES (?, ?, '2026-07-28T12:00:00Z', '2026-07-28', 'exact',
                                    'takeout_youtube', 'video_id_date', ?, 'takeout', ?)
                            """,
                            (f"takeout-{ordinal}", video_id, ordinal, f"row-{ordinal}"),
                        )

                first_batch = core.youtube_takeout_match_count(conn, 1, 5)
                second_batch = core.youtube_takeout_match_count(conn, 6, 5)
            finally:
                conn.close()

        self.assertEqual(first_batch, 1)
        self.assertEqual(second_batch, 1)

    def test_youtube_history_order_normalizes_legacy_duplicate_ordinals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    for event_id, video_id, ordinal in (
                        ("event-a", "video-a", 1),
                        ("event-b", "video-b", 1),
                        ("event-c", "video-c", 3),
                    ):
                        core.upsert_video(conn, video_id, title=video_id, source="test")
                        conn.execute(
                            """
                            INSERT INTO history_events(
                              event_id, video_id, watch_date, time_precision,
                              source_type, match_type, youtube_ordinal
                            )
                            VALUES (?, ?, '2026-07-28', 'date_only',
                                    'youtube', 'youtube_only', ?)
                            """,
                            (event_id, video_id, ordinal),
                        )
                snapshot = core.youtube_history_occurrence_snapshot(conn)

                with conn:
                    core.synchronize_youtube_history_order(
                        conn,
                        snapshot,
                        set(),
                        processed=0,
                        shift=0,
                    )

                ordinals = [
                    row["youtube_ordinal"]
                    for row in conn.execute(
                        "SELECT youtube_ordinal FROM history_events ORDER BY youtube_ordinal"
                    )
                ]
            finally:
                conn.close()

        self.assertEqual(ordinals, [1, 2, 3])

    def test_canonical_video_prefers_current_youtube_and_retains_unavailable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(conn, "video123", title="Takeout title", source="takeout")
                    core.upsert_video(conn, "video123", title="Current title", source="playlist", is_playable=1)
                    core.upsert_video(conn, "video123", title="Older export title", source="takeout")
                    core.upsert_video(
                        conn,
                        "video123",
                        title="Deleted video",
                        source="metadata",
                        is_playable=0,
                        availability="deleted",
                    )
                row = conn.execute(
                    "SELECT title, is_playable, availability FROM videos WHERE video_id = 'video123'"
                ).fetchone()
                self.assertEqual(dict(row), {"title": "Current title", "is_playable": 0, "availability": "deleted"})
            finally:
                conn.close()

    def test_archivarix_title_replaces_higher_priority_video_id_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "rXJrevMFMFw",
                        title="rXJrevMFMFw",
                        description="Current YouTube description",
                        source="metadata",
                        fetch_status="no_metadata",
                    )
                    core.save_video_recovery(
                        conn,
                        "rXJrevMFMFw",
                        {
                            "title": "Astronomer Visualizes The True Scale Of The Universe",
                            "description": "Archived description",
                            "status": "DELETED_FULL_META",
                        },
                        "found",
                        "",
                    )

                row = conn.execute(
                    """
                    SELECT title, description, is_playable, availability,
                           metadata_source, last_checked_at
                    FROM videos
                    WHERE video_id = 'rXJrevMFMFw'
                    """
                ).fetchone()
                self.assertEqual(
                    dict(row),
                    {
                        "title": "Astronomer Visualizes The True Scale Of The Universe",
                        "description": "Current YouTube description",
                        "is_playable": None,
                        "availability": "unknown",
                        "metadata_source": "metadata",
                        "last_checked_at": None,
                    },
                )
            finally:
                conn.close()

    def test_archivarix_statuses_do_not_override_canonical_youtube_availability(self) -> None:
        recovery_cases = (
            ("LIVE", "found", {"status": "LIVE"}),
            ("DELETED_FULL_META", "found", {"status": "DELETED_FULL_META"}),
            ("DELETED_ID_ONLY", "found", {"status": "DELETED_ID_ONLY"}),
            ("NOT_FOUND", "not_found", None),
        )
        canonical_cases = (
            ("public", 1, "public"),
            ("unlisted", 1, "unlisted"),
            ("private", 1, "private"),
            ("subscriber_only", 1, "members_only"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    for state_index, (availability, is_playable, _) in enumerate(
                        canonical_cases
                    ):
                        for recovery_index, (_, search_status, video) in enumerate(
                            recovery_cases
                        ):
                            video_id = f"state{state_index}{recovery_index}video"
                            core.upsert_video(
                                conn,
                                video_id,
                                title=f"Canonical {availability}",
                                is_playable=is_playable,
                                availability=availability,
                                source="metadata",
                            )
                            core.save_video_recovery(
                                conn,
                                video_id,
                                video,
                                search_status,
                                "",
                            )

                for state_index, (availability, is_playable, category) in enumerate(
                    canonical_cases
                ):
                    for recovery_index, (status, _, _) in enumerate(recovery_cases):
                        video_id = f"state{state_index}{recovery_index}video"
                        row = conn.execute(
                            """
                            SELECT v.video_id, v.availability, v.is_playable,
                                   vr.archivarix_status
                            FROM videos v
                            JOIN video_recovery vr ON vr.video_id = v.video_id
                            WHERE v.video_id = ?
                            """,
                            (video_id,),
                        ).fetchone()
                        with self.subTest(availability=availability, status=status):
                            self.assertEqual(row["availability"], availability)
                            self.assertEqual(row["is_playable"], is_playable)
                            self.assertEqual(row["archivarix_status"], status)
                            self.assertEqual(
                                core.video_availability_category(dict(row)),
                                category,
                            )
            finally:
                conn.close()

    def test_archivarix_recovery_is_independent_before_and_after_youtube_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.save_video_recovery(
                        conn,
                        "recoverFirst",
                        {
                            "title": "Recovered identity",
                            "description": "Recovered description",
                            "status": "DELETED_FULL_META",
                            "videoFileUrl": "https://archive.example/video.mp4",
                        },
                        "found",
                        "",
                    )
                recovery_first = conn.execute(
                    """
                    SELECT v.title, v.is_playable, v.availability,
                           vr.archivarix_status, vr.media_available
                    FROM videos v
                    JOIN video_recovery vr ON vr.video_id = v.video_id
                    WHERE v.video_id = 'recoverFirst'
                    """
                ).fetchone()
                self.assertEqual(recovery_first["title"], "Recovered identity")
                self.assertIsNone(recovery_first["is_playable"])
                self.assertEqual(recovery_first["availability"], "unknown")
                self.assertEqual(recovery_first["archivarix_status"], "DELETED_FULL_META")
                self.assertEqual(recovery_first["media_available"], 1)

                with conn:
                    core.store_video_metadata(
                        conn,
                        {
                            "video_id": "recoverFirst",
                            "title": "Current YouTube identity",
                            "playability_status": "OK",
                            "yt_status": "OK",
                        },
                        "ok",
                    )
                    core.save_video_recovery(
                        conn,
                        "recoverFirst",
                        {"title": "Older recovered identity", "status": "NOT_FOUND"},
                        "not_found",
                        "",
                    )

                youtube_current = conn.execute(
                    """
                    SELECT v.title, v.is_playable, v.availability,
                           vr.archivarix_status, vr.media_available
                    FROM videos v
                    JOIN video_recovery vr ON vr.video_id = v.video_id
                    WHERE v.video_id = 'recoverFirst'
                    """
                ).fetchone()
                self.assertEqual(youtube_current["title"], "Current YouTube identity")
                self.assertEqual(youtube_current["is_playable"], 1)
                self.assertEqual(youtube_current["availability"], "public")
                self.assertEqual(youtube_current["archivarix_status"], "NOT_FOUND")
                self.assertEqual(youtube_current["media_available"], 1)
            finally:
                conn.close()

    def test_archivarix_media_availability_tracks_only_authoritative_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                def media_available(video_id: str) -> int | None:
                    return conn.execute(
                        "SELECT media_available FROM video_recovery WHERE video_id = ?",
                        (video_id,),
                    ).fetchone()["media_available"]

                with conn:
                    core.save_video_recovery(
                        conn,
                        "mediaToOne",
                        None,
                        "error",
                        "temporary failure",
                    )
                self.assertIsNone(media_available("mediaToOne"))

                with conn:
                    core.save_video_recovery(
                        conn,
                        "mediaToOne",
                        {"videoFileUrl": "https://archive.example/media.mp4"},
                        "found",
                        "",
                    )
                    core.save_video_recovery(
                        conn,
                        "mediaToZero",
                        {"videoFileUrl": "   "},
                        "found",
                        "",
                    )
                self.assertEqual(media_available("mediaToOne"), 1)
                self.assertEqual(media_available("mediaToZero"), 0)

                with conn:
                    core.save_video_recovery(
                        conn,
                        "mediaToOne",
                        {"videoFileUrl": "\t  "},
                        "found",
                        "",
                    )
                self.assertEqual(media_available("mediaToOne"), 0)
                self.assertEqual(video_detail_data(conn, "mediaToOne")["video_file_url"], "")

                with conn:
                    core.save_video_recovery(
                        conn,
                        "mediaToOne",
                        {"videoFileUrl": "https://archive.example/media-restored.mp4"},
                        "found",
                        "",
                    )
                self.assertEqual(media_available("mediaToOne"), 1)
                self.assertTrue(video_detail_data(conn, "mediaToOne")["video_file_url"])

                non_authoritative_statuses = (
                    "error",
                    "timeout",
                    "rate_limited",
                    "thumbnail_only",
                    "stopped",
                    "not_found",
                )
                for search_status in non_authoritative_statuses:
                    with self.subTest(search_status=search_status):
                        with conn:
                            core.save_video_recovery(
                                conn,
                                "mediaToOne",
                                None,
                                search_status,
                                "non-authoritative result",
                                thumbnail_path=(
                                    "archivarix_thumbs/mediaToOne.jpg"
                                    if search_status == "thumbnail_only"
                                    else ""
                                ),
                            )
                        self.assertEqual(media_available("mediaToOne"), 1)
            finally:
                conn.close()

    def test_transient_recovery_failure_preserves_archivarix_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.save_video_recovery(
                        conn,
                        "preserve001",
                        {
                            "status": "DELETED_FULL_META",
                            "channelId": "archivarix-channel-1",
                            "archiveUrl": "https://web.archive.org/web/20260812161701/https://youtube.com/watch?v=preserve001",
                            "videoFileUrl": "https://archive.example/media.mp4",
                        },
                        "found",
                        "",
                    )
                    core.save_video_recovery(
                        conn,
                        "preserve001",
                        None,
                        "rate_limited",
                        "Archivarix daily search limit reached",
                    )

                recovery = conn.execute(
                    """
                    SELECT archivarix_status, archivarix_channel_id,
                           archive_capture_at, media_available,
                           search_status, search_error
                    FROM video_recovery
                    WHERE video_id = 'preserve001'
                    """
                ).fetchone()
                self.assertEqual(
                    dict(recovery),
                    {
                        "archivarix_status": "DELETED_FULL_META",
                        "archivarix_channel_id": "archivarix-channel-1",
                        "archive_capture_at": "2026-08-12T16:17:01Z",
                        "media_available": 1,
                        "search_status": "rate_limited",
                        "search_error": "Archivarix daily search limit reached",
                    },
                )
                self.assertTrue(core.placeholder_recovery_is_complete(conn, "preserve001"))
            finally:
                conn.close()

    def test_refresh_exact_history_dates_uses_iana_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.upsert_video(conn, "video123", title="Example", source="takeout")
                    conn.execute(
                        """
                        INSERT INTO history_events(
                          event_id, video_id, watched_at, watch_date, time_precision,
                          source_type, match_type, imported_at, updated_at
                        ) VALUES (
                          'takeout:one', 'video123', '2026-07-04T05:27:45Z', '2026-07-04', 'exact',
                          'takeout', 'takeout_only', '2026-07-04T06:00:00Z', '2026-07-04T06:00:00Z'
                        )
                        """
                    )
                    core.refresh_exact_history_dates(conn, "America/Los_Angeles")
                watch_date = conn.execute(
                    "SELECT watch_date FROM history_events WHERE event_id = 'takeout:one'"
                ).fetchone()[0]
                self.assertEqual(watch_date, "2026-07-03")
            finally:
                conn.close()

    def test_cookie_auth_status_tracks_and_clears_remote_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                initial = core.cookie_auth_statuses(conn)
                self.assertEqual(initial["youtube"]["status"], "unknown")
                self.assertEqual(initial["google"]["checked_at"], "")

                with conn:
                    core.record_cookie_auth_status(
                        conn,
                        "youtube",
                        "valid",
                        "Authenticated request accepted.",
                        checked_at="2026-08-19T19:20:21Z",
                    )
                current = core.cookie_auth_statuses(conn)["youtube"]
                self.assertEqual(current["status"], "valid")
                self.assertEqual(current["checked_at"], "2026-08-19T19:20:21Z")
                self.assertEqual(current["message"], "Authenticated request accepted.")

                with conn:
                    self.assertTrue(core.clear_cookie_auth_status(conn, "youtube"))
                self.assertEqual(
                    core.cookie_auth_statuses(conn)["youtube"]["status"],
                    "unknown",
                )
            finally:
                conn.close()

    def test_cookie_auth_failure_status_distinguishes_known_failures(self) -> None:
        self.assertEqual(
            core.cookie_auth_failure_status("Archivarix cookie expired"),
            "expired",
        )
        self.assertEqual(
            core.cookie_auth_failure_status("Cookie file is missing"),
            "missing",
        )
        self.assertEqual(
            core.cookie_auth_failure_status("YouTube login session was rejected"),
            "rejected",
        )

    def test_cookie_auth_status_logs_each_unusable_transition_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = migrated_connection(Path(tmp) / "library.sqlite3")
            try:
                with conn:
                    core.record_cookie_auth_status(
                        conn,
                        "google",
                        "valid",
                        "Authenticated request accepted.",
                    )
                    core.record_cookie_auth_status(
                        conn,
                        "google",
                        "rejected",
                        "Signed-out response received.",
                    )
                    core.record_cookie_auth_status(
                        conn,
                        "google",
                        "rejected",
                        "Repeated signed-out response received.",
                    )
                    core.record_cookie_auth_status(
                        conn,
                        "google",
                        "valid",
                        "Authenticated request accepted again.",
                    )
                    core.record_cookie_auth_status(
                        conn,
                        "google",
                        "expired",
                        "Cookie expired.",
                    )

                rows = conn.execute(
                    """
                    SELECT level, video_id, message
                    FROM metadata_worker_log
                    ORDER BY id
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["level"], "queue warn")
        self.assertEqual(rows[0]["video_id"], "")
        self.assertEqual(
            rows[0]["message"],
            "Google My Activity cookie status changed to rejected: "
            "Signed-out response received.",
        )
        self.assertEqual(
            rows[1]["message"],
            "Google My Activity cookie status changed to expired: Cookie expired.",
        )


if __name__ == "__main__":
    unittest.main()
