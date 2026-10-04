from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yt_library.plugin_transport import PluginMyActivitySession
from yt_library.plugins import PluginManager, PluginWorkerRuntime, PluginYoutubeSession
from tests.support import migrated_connection
from tests.test_plugins import FakeEntryPoint, FakePlugin


class PluginServiceTests(unittest.TestCase):
    def test_account_identity_is_host_owned_lazy_and_exposed_to_plugins(self):
        plugin = FakePlugin()
        identity = Mock(return_value={"channel_id": "UC" + "a" * 22, "checked_at": "2026-10-03T00:00:00Z"})
        with patch("yt_library.youtube_identity.YoutubeAccountIdentity", return_value=identity) as factory:
            PluginManager({"plugins": {"subtitles": {"enabled": True}}},
                          entry_points=[FakeEntryPoint(lambda: plugin)],
                          youtube_cookie_file=Path("cookies.txt"), proxy_url="configured-proxy")
            identity.assert_not_called()
            self.assertEqual(plugin.context.youtube_account_identity()["channel_id"], "UC" + "a" * 22)
            factory.assert_called_once_with(Path("cookies.txt"), "configured-proxy")

    def test_discovery_adds_real_identity_without_watch_and_preserves_known_video(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "library.sqlite3"
            conn = migrated_connection(path)
            conn.close()
            runtime = PluginWorkerRuntime(path, run_id="run", queue_id=1, plugin_id="sample", worker_id="fetch", subject_id="item", stop_event=threading.Event())
            self.assertEqual(runtime.discover_videos([{"video_id": "abcdefghijk", "title": "Original title"}]), 1)
            self.assertEqual(runtime.discover_videos([{"video_id": "abcdefghijk", "title": "Replacement"}]), 0)
            conn = migrated_connection(path)
            self.assertEqual(conn.execute("SELECT title FROM videos").fetchone()[0], "Original title")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM history_events").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_queue").fetchone()[0], 1)
            conn.close()
            with self.assertRaises(ValueError):
                runtime.discover_videos([{"video_id": "https://example.com"}])

    def test_highlighted_session_factory_and_same_plugin_followup(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "library.sqlite3"
            conn = migrated_connection(path)
            conn.close()
            plugin = FakePlugin()
            factory = Mock()
            manager = PluginManager({"plugins": {"subtitles": {"enabled": True}}}, db_path=path,
                                    entry_points=[FakeEntryPoint(lambda: plugin)], youtube_session_factory=factory)
            plugin.context.youtube_video_session("abcdefghijk", comment_id="opaque.reply")
            factory.assert_called_once_with("abcdefghijk", comment_id="opaque.reply")
            runtime = PluginWorkerRuntime(path, run_id="run", queue_id=1, plugin_id="subtitles", worker_id="fetch", subject_id="item", stop_event=threading.Event())
            runtime._manager = manager
            with patch.object(manager, "enqueue_process", return_value={"queued": 1}) as enqueue:
                self.assertEqual(runtime.enqueue_process("fetch", {"video_id": ["abcdefghijk"]}), {"queued": 1})
            self.assertEqual(enqueue.call_args.args[1:3], ("subtitles", "fetch"))
            self.assertFalse(enqueue.call_args.kwargs["manual"])

    def test_google_transport_rejects_foreign_origins_and_mutating_paths(self):
        with patch("yt_library.plugin_transport._my_activity_opener", return_value=Mock()):
            session = PluginMyActivitySession(Path("cookies.txt"), "")
        for path in ("https://example.com/page", "//example.com/page", "/delete", "/product/\\example.com"):
            with self.assertRaises(ValueError):
                session.request_text(path)
        with self.assertRaises(ValueError):
            session.request_text("/page", {"delete": "all"})

    def test_next_transport_checks_authentication(self):
        session = PluginYoutubeSession(video_id="abcdefghijk", initial_data={}, opener=Mock(), cookie_jar=Mock(),
                                       api_key="public", client_version="1", client_context={"client": {}}, referer="https://www.youtube.com/watch?v=abcdefghijk")
        with patch("yt_library.core.request_youtubei_json", return_value={"responseContext": {"mainAppWebResponseContext": {"loggedOut": True}}}):
            with self.assertRaisesRegex(RuntimeError, "signed out"):
                session.request_json("next", {"continuation": "token"})


if __name__ == "__main__":
    unittest.main()
