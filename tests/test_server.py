from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, call, patch

from yt_library import core, server, workers
from yt_library.config import ConfigStore, load_config

from tests.support import migrated_connection


class AdminServerTests(unittest.TestCase):
    def test_playlist_metadata_only_start_queues_all_playlists_and_starts_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    conn.executemany(
                        """
                        INSERT INTO playlists(playlist_id, title)
                        VALUES (?, ?)
                        """,
                        [
                            ("PLmetadata1", "First"),
                            ("PLmetadata2", "Second"),
                        ],
                    )
            finally:
                conn.close()
            handler = object.__new__(server.LibraryHandler)
            handler.db_path = db_path
            handler._start_worker_queue = Mock(return_value={"started": True})
            handler.send_json = Mock()

            handler._handle_playlist_scan_start({"metadata_only": ["1"]})

            handler._start_worker_queue.assert_called_once_with()
            payload = handler.send_json.call_args.args[0]
            self.assertEqual(payload["queue"]["mode"], "metadata_only")
            self.assertEqual(payload["queue"]["selected"], 2)
            self.assertEqual(payload["queue"]["inserted"], 2)
            self.assertEqual(payload["dispatcher"], {"started": True})

    def test_individual_video_scan_notifies_plugin_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()
            handler = object.__new__(server.LibraryHandler)
            handler.db_path = db_path
            handler.plugin_manager = Mock()
            handler.plugin_manager.enqueue_hook.return_value = [
                {
                    "pluginId": "subtitles",
                    "workerId": "fetch",
                    "planned": 1,
                    "inserted": 1,
                    "alreadyQueued": 0,
                }
            ]
            handler.send_json = Mock()

            handler._handle_admin_action_post(
                urllib.parse.urlparse("/api/admin/queue/add-target"),
                {"target": ["abcdefghijk"]},
            )

            call = handler.plugin_manager.enqueue_hook.call_args
            self.assertEqual(
                call.args[1:],
                ("video_scan", {"video_id": ["abcdefghijk"]}),
            )
            self.assertEqual(call.kwargs, {"manual": True})
            payload = handler.send_json.call_args.args[0]
            self.assertEqual(payload["video_id"], "abcdefghijk")
            self.assertEqual(
                payload["pluginQueue"],
                handler.plugin_manager.enqueue_hook.return_value,
            )

    def test_individual_channel_scan_does_not_notify_video_plugins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()
            handler = object.__new__(server.LibraryHandler)
            handler.db_path = db_path
            handler.plugin_manager = Mock()
            handler.send_json = Mock()

            handler._handle_admin_action_post(
                urllib.parse.urlparse("/api/admin/queue/add-target"),
                {"target": ["UCabcdefghijklmnopqrstuv"]},
            )

            handler.plugin_manager.enqueue_hook.assert_not_called()
            payload = handler.send_json.call_args.args[0]
            self.assertEqual(payload["pluginQueue"], [])

    def test_plugin_admin_process_endpoint_plans_queue_and_starts_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()
            handler = object.__new__(server.LibraryHandler)
            handler.db_path = db_path
            handler.plugin_manager = Mock()
            handler.plugin_manager.enqueue_process.return_value = {
                "pluginId": "example",
                "workerId": "fetch",
                "inserted": 3,
                "alreadyQueued": 1,
            }
            handler._start_worker_queue = Mock(return_value={"started": True})
            handler.send_json = Mock()

            handler._handle_admin_action_post(
                urllib.parse.urlparse(
                    "/api/admin/plugins/example/processes/fetch/enqueue"
                ),
                {"video_id": ["abcdefghijk", "mnopqrstuvw"]},
            )

            call = handler.plugin_manager.enqueue_process.call_args
            self.assertEqual(
                call.args[1:],
                (
                    "example",
                    "fetch",
                    {"video_id": ["abcdefghijk", "mnopqrstuvw"]},
                ),
            )
            self.assertEqual(call.kwargs, {"manual": True})
            handler._start_worker_queue.assert_called_once_with()
            handler.send_json.assert_called_once_with(
                {
                    "ok": True,
                    "queue": handler.plugin_manager.enqueue_process.return_value,
                    "dispatcher": {"started": True},
                }
            )

    def test_plugin_enabled_endpoint_saves_display_name_and_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            config["plugins"] = {
                "subtitles": {
                    "enabled": True,
                    "config": "../YT Subtitles/yt_subtitles.config.json",
                }
            }
            handler = object.__new__(server.LibraryHandler)
            handler.config_data = config
            handler.plugin_manager = Mock()
            handler.plugin_manager.statuses.return_value = [
                {
                    "id": "subtitles",
                    "name": "YT Subtitles",
                    "enabled": True,
                    "state": "ready",
                }
            ]
            handler.request_restart = Mock(return_value=True)
            handler.restart_pending = lambda: handler.request_restart.called
            handler.service_started_at = "2026-08-04T08:00:00Z"
            handler.send_json = Mock()

            handler._handle_admin_action_post(
                urllib.parse.urlparse("/api/admin/plugins/subtitles/enabled"),
                {"enabled": ["0"]},
            )

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            response = handler.send_json.call_args.args[0]

        self.assertFalse(saved["plugins"]["subtitles"]["enabled"])
        self.assertEqual(saved["plugins"]["subtitles"]["name"], "YT Subtitles")
        handler.request_restart.assert_called_once_with()
        self.assertTrue(response["restartScheduled"])
        self.assertFalse(response["enabled"])
        self.assertEqual(response["service"]["status"], "restarting")

    def test_plugin_enabled_endpoint_requires_idle_workers(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        config["plugins"] = {"subtitles": {"enabled": True}}
        handler = object.__new__(server.LibraryHandler)
        handler.config_data = config
        handler.request_restart = Mock()
        handler.send_json = Mock()

        with patch.object(
            server.WORKER_QUEUE_DISPATCHER,
            "is_alive",
            return_value=True,
        ):
            handler._handle_admin_action_post(
                urllib.parse.urlparse("/api/admin/plugins/subtitles/enabled"),
                {"enabled": ["0"]},
            )

        self.assertTrue(config["plugins"]["subtitles"]["enabled"])
        handler.request_restart.assert_not_called()
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 409)

    def test_channel_alias_route_resolves_before_loading_videos(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.send_json = Mock()
        connection = Mock()
        channel = {"channel_id": "UCcanonical", "preferred_reference": "@alias"}
        videos = {"results": [], "total": 0}

        with (
            patch("yt_library.server.connect", return_value=connection),
            patch(
                "yt_library.server.channel_detail_data",
                return_value=channel,
            ) as detail_data,
            patch(
                "yt_library.server.video_collection_data",
                return_value=videos,
            ) as collection_data,
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/channels/@alias/videos?limit=1&offset=0&sort=title"
                )
            )

        detail_data.assert_called_once_with(connection, "@alias")
        collection_data.assert_called_once_with(
            connection,
            channel_id="UCcanonical",
            sort="title",
            limit=1,
            offset=0,
        )
        connection.close.assert_called_once_with()
        handler.send_json.assert_called_once_with(videos)

    def test_channel_alias_route_filters_playlists_by_canonical_owner(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.send_json = Mock()
        connection = Mock()
        channel = {"channel_id": "UCcanonical", "preferred_reference": "@alias"}
        playlists = {"results": [], "total": 0}

        with (
            patch("yt_library.server.connect", return_value=connection),
            patch(
                "yt_library.server.channel_detail_data",
                return_value=channel,
            ) as detail_data,
            patch(
                "yt_library.server.playlist_list_data",
                return_value=playlists,
            ) as list_data,
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/channels/@alias/playlists?limit=25&offset=50&sort=title_desc"
                )
            )

        detail_data.assert_called_once_with(connection, "@alias")
        list_data.assert_called_once_with(
            connection,
            owner_channel_id="UCcanonical",
            sort="title_desc",
            limit=25,
            offset=50,
        )
        connection.close.assert_called_once_with()
        handler.send_json.assert_called_once_with(playlists)

    def test_clip_detail_route_uses_generic_clip_plugin_facets(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.plugin_manager = Mock()
        handler.plugin_manager.filter_clips.return_value = (
            frozenset({"UgkxClipRoute123"}),
            frozenset(),
        )
        handler.send_json = Mock()
        connection = Mock()
        detail = {
            "clip_id": "UgkxClipRoute123",
            "source_video_id": "source12345",
            "start_ms": 1_000,
            "end_ms": 2_000,
        }

        with (
            patch("yt_library.server.connect", return_value=connection),
            patch("yt_library.server.clip_detail_data", return_value=detail),
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/clips/UgkxClipRoute123?clip_facet_plugin=subtitles"
                )
            )

        handler.plugin_manager.filter_clips.assert_called_once_with(
            "subtitles",
            "",
            (detail,),
        )
        self.assertEqual(detail["pluginFacets"], {"subtitles": True})
        handler.send_json.assert_called_once_with(detail)

    def test_clip_discovery_and_common_scan_endpoint_enqueue_clip_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()
            handler = object.__new__(server.LibraryHandler)
            handler.db_path = db_path
            handler.plugin_manager = Mock()
            handler._start_worker_queue = Mock(return_value={"started": True})
            handler.send_json = Mock()

            handler._handle_admin_action_post(
                urllib.parse.urlparse("/api/admin/clips/discover"),
                {},
            )
            handler._handle_admin_action_post(
                urllib.parse.urlparse("/api/admin/queue/add-target"),
                {
                    "target": [
                        "https://www.youtube.com/clip/UgkxUIUr7iJI7JSqsEGWEYebU5mV1PaMbz9s"
                    ]
                },
            )
            handler._handle_admin_action_post(
                urllib.parse.urlparse("/api/admin/queue/add-target"),
                {"target": ["UgkxManualClip456"]},
            )

            conn = core.connect(db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT worker_type, task_type, clip_id
                    FROM worker_queue
                    ORDER BY queue_id
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("clip", "discover", ""),
                (
                    "clip",
                    "scan",
                    "UgkxUIUr7iJI7JSqsEGWEYebU5mV1PaMbz9s",
                ),
                ("clip", "scan", "UgkxManualClip456"),
            ],
        )
        self.assertEqual(handler._start_worker_queue.call_count, 1)
        handler.plugin_manager.enqueue_hook.assert_not_called()

    def test_plugin_browser_assets_are_namespaced_and_delegated(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.plugin_manager = Mock()
        handler.plugin_manager.handle_browser_asset.return_value = (
            200,
            "text/css; charset=utf-8",
            b".example {}",
        )
        handler._send_bytes = Mock()

        handler._handle_library_get(
            urllib.parse.urlparse("/plugins/example/assets/browser.css")
        )

        handler.plugin_manager.handle_browser_asset.assert_called_once_with(
            "example", "browser.css"
        )
        handler._send_bytes.assert_called_once_with(
            b".example {}",
            "text/css; charset=utf-8",
            cache_control="no-cache",
            status=200,
        )

    def test_bootstrap_includes_generic_plugin_statuses(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.plugin_manager = Mock()
        handler.plugin_manager.statuses.return_value = [
            {"id": "example", "enabled": True, "state": "ready"}
        ]
        handler.plugin_manager.project_playlist_groups.return_value = {
            "groups": [
                {
                    "group_key": "plugin:example:group",
                    "name": "Example group",
                    "parent_key": None,
                    "position": 0,
                }
            ],
            "memberships": [
                {
                    "group_key": "plugin:example:group",
                    "playlist_id": "PLknown",
                    "position": 0,
                }
            ],
            "errors": [],
        }
        handler.plugin_manager.project_channel_groups.return_value = {
            "groups": [
                {
                    "group_key": "plugin-channel:example:group",
                    "name": "Example channels",
                    "parent_key": None,
                    "position": 0,
                }
            ],
            "memberships": [
                {
                    "group_key": "plugin-channel:example:group",
                    "channel_id": "UCknown",
                    "position": 0,
                }
            ],
            "errors": [],
        }
        handler.send_json = Mock()
        connection = Mock()
        connection.execute.side_effect = [
            [("PLknown",)],
            [("UCknown",)],
        ]

        with (
            patch("yt_library.server.connect", return_value=connection),
            patch(
                "yt_library.server.library_bootstrap_data",
                return_value={"groups": [], "memberships": [], "counts": {}},
            ),
        ):
            handler._handle_library_get(urllib.parse.urlparse("/api/bootstrap"))

        connection.close.assert_called_once_with()
        handler.send_json.assert_called_once_with(
            {
                "groups": [
                    {
                        "group_key": "plugin:example:group",
                        "name": "Example group",
                        "parent_key": None,
                        "position": 0,
                    }
                ],
                "memberships": [
                    {
                        "group_key": "plugin:example:group",
                        "playlist_id": "PLknown",
                        "position": 0,
                    }
                ],
                "counts": {},
                "channelGroups": [
                    {
                        "group_key": "plugin-channel:example:group",
                        "name": "Example channels",
                        "parent_key": None,
                        "position": 0,
                    }
                ],
                "channelMemberships": [
                    {
                        "group_key": "plugin-channel:example:group",
                        "channel_id": "UCknown",
                        "position": 0,
                    }
                ],
                "plugins": [{"id": "example", "enabled": True, "state": "ready"}],
            }
        )
        handler.plugin_manager.project_playlist_groups.assert_called_once_with(
            frozenset({"PLknown"})
        )
        handler.plugin_manager.project_channel_groups.assert_called_once_with(
            frozenset({"UCknown"})
        )

    def test_plugin_routes_are_namespaced_and_delegated(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.plugin_manager = Mock()
        handler.plugin_manager.handle_api.return_value = (200, {"hits": []})
        handler.send_json = Mock()

        handler._handle_library_get(
            urllib.parse.urlparse("/api/plugins/subtitles/search?q=history")
        )

        handler.plugin_manager.handle_api.assert_called_once_with(
            "subtitles",
            "GET",
            "search",
            {"q": ["history"]},
        )
        handler.send_json.assert_called_once_with({"hits": []}, status=200)

    def test_plugin_json_mutations_are_bounded_and_delegated(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.plugin_manager = Mock()
        handler.plugin_manager.handle_api.return_value = (
            201,
            {"conversationId": "chat-1"},
        )
        body = json.dumps({"message": "Summarize this video"}).encode("utf-8")
        handler.headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "Origin": "http://127.0.0.1:8765",
            "Host": "127.0.0.1:8765",
        }
        handler.rfile = io.BytesIO(body)
        handler.send_json = Mock()

        handler._handle_plugin_post(
            urllib.parse.urlparse("/api/plugins/llm/videos/abcdefghijk/chats")
        )

        handler.plugin_manager.handle_api.assert_called_once_with(
            "llm",
            "POST",
            "videos/abcdefghijk/chats",
            {},
            {"message": "Summarize this video"},
        )
        handler.send_json.assert_called_once_with(
            {"conversationId": "chat-1"},
            status=201,
        )

    def test_plugin_json_mutations_reject_cross_origin_and_non_objects(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.plugin_manager = Mock()
        handler.send_json = Mock()
        body = b"[]"
        handler.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Origin": "https://example.com",
            "Host": "127.0.0.1:8765",
        }
        handler.rfile = io.BytesIO(body)

        handler._handle_plugin_post(
            urllib.parse.urlparse("/api/plugins/llm/chats")
        )

        handler.send_json.assert_called_once_with(
            {"error": "Cross-origin plugin mutations are not allowed"},
            status=403,
        )
        handler.plugin_manager.handle_api.assert_not_called()

        handler.send_json.reset_mock()
        handler.headers["Origin"] = "http://127.0.0.1:8765"
        handler.rfile = io.BytesIO(body)
        handler._handle_plugin_post(
            urllib.parse.urlparse("/api/plugins/llm/chats")
        )
        handler.send_json.assert_called_once_with(
            {"error": "Plugin JSON body must be an object"},
            status=400,
        )

    def test_search_applies_generic_plugin_video_filters(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.config_data = {}
        handler.plugin_manager = Mock()
        handler.plugin_manager.filter_videos.return_value = (
            frozenset({"available", "unavailable"}),
            frozenset({"unavailable"}),
        )
        handler.plugin_manager.project_videos.return_value = {
            "unavailable": {
                "video_id": "unavailable",
                "title": "Projected video",
            }
        }
        handler.send_json = Mock()
        connection = Mock()
        payload = {"results": [], "total": 0}

        with (
            patch("yt_library.server.connect", return_value=connection),
            patch("yt_library.server.omni_search_data", return_value=payload) as search_data,
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/search?q=history&video_facet_plugin=example&video_filter_plugin=example&limit=1"
                )
            )

        handler.plugin_manager.filter_videos.assert_called_once_with("example", "history")
        handler.plugin_manager.project_videos.assert_called_once_with(
            "example",
            frozenset({"unavailable"}),
        )
        self.assertEqual(
            search_data.call_args.kwargs["video_id_filters"],
            [frozenset({"available", "unavailable"})],
        )
        self.assertEqual(
            search_data.call_args.kwargs["video_search_match_ids"],
            {"unavailable"},
        )
        self.assertEqual(
            search_data.call_args.kwargs["video_facet_memberships"],
            {"example": frozenset({"available", "unavailable"})},
        )
        self.assertEqual(
            search_data.call_args.kwargs["video_search_match_memberships"],
            {"example": frozenset({"unavailable"})},
        )
        self.assertEqual(
            search_data.call_args.kwargs["video_projections"],
            {
                "example": {
                    "unavailable": {
                        "video_id": "unavailable",
                        "title": "Projected video",
                    }
                }
            },
        )
        handler.send_json.assert_called_once_with(payload)

    def test_search_applies_generic_plugin_clip_filters(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.config_data = {}
        handler.plugin_manager = Mock()
        handler.plugin_manager.filter_clips.return_value = (
            frozenset({"clip-match"}),
            frozenset({"clip-match"}),
        )
        handler.send_json = Mock()
        connection = Mock()
        clip_rows = (
            {
                "clip_id": "clip-match",
                "source_video_id": "source12345",
                "start_ms": 1_000,
                "end_ms": 2_000,
            },
        )
        payload = {"results": [], "total": 0}

        with (
            patch("yt_library.server.connect", return_value=connection),
            patch("yt_library.server.library_clip_descriptors", return_value=clip_rows),
            patch("yt_library.server.omni_search_data", return_value=payload) as search_data,
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/search?q=spoken&clip_facet_plugin=example&clip_filter_plugin=example&clip_search_plugin=example&limit=1"
                )
            )

        handler.plugin_manager.filter_clips.assert_called_once_with(
            "example",
            "spoken",
            clip_rows,
        )
        self.assertEqual(
            search_data.call_args.kwargs["clip_id_filters"],
            [frozenset({"clip-match"})],
        )
        self.assertEqual(
            search_data.call_args.kwargs["clip_search_match_ids"],
            {"clip-match"},
        )
        self.assertEqual(
            search_data.call_args.kwargs["clip_facet_memberships"],
            {"example": frozenset({"clip-match"})},
        )
        handler.send_json.assert_called_once_with(payload)

    def test_search_resolves_plugin_playlist_group_membership(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.config_data = {}
        handler.plugin_manager = Mock()
        handler.plugin_manager.playlist_ids_for_group.return_value = frozenset(
            {"PLparent", "PLchild"}
        )
        handler.send_json = Mock()
        connection = Mock()
        connection.execute.return_value = [("PLparent",), ("PLchild",)]
        payload = {"results": [], "total": 0}

        with (
            patch("yt_library.server.connect", return_value=connection),
            patch(
                "yt_library.server.omni_search_data",
                return_value=payload,
            ) as search_data,
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/search?kinds=playlist&playlist_group_key="
                    "plugin%3Aexample%3Aparent&limit=1"
                )
            )

        handler.plugin_manager.playlist_ids_for_group.assert_called_once_with(
            "plugin:example:parent",
            frozenset({"PLparent", "PLchild"}),
        )
        self.assertEqual(
            search_data.call_args.kwargs["playlist_id_filter"],
            frozenset({"PLparent", "PLchild"}),
        )
        self.assertEqual(
            search_data.call_args.kwargs["playlist_group_key"],
            "plugin:example:parent",
        )
        self.assertEqual(connection.close.call_count, 2)
        handler.send_json.assert_called_once_with(payload)

    def test_search_resolves_plugin_channel_group_membership(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.config_data = {}
        handler.plugin_manager = Mock()
        handler.plugin_manager.channel_ids_for_group.return_value = frozenset(
            {"UCparent", "UCchild"}
        )
        handler.send_json = Mock()
        connection = Mock()
        connection.execute.return_value = [("UCparent",), ("UCchild",)]
        payload = {"results": [], "total": 0}

        with (
            patch("yt_library.server.connect", return_value=connection),
            patch(
                "yt_library.server.omni_search_data",
                return_value=payload,
            ) as search_data,
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/search?kinds=channel&channel_group_key="
                    "plugin-channel%3Aexample%3Aparent&limit=1"
                )
            )

        handler.plugin_manager.channel_ids_for_group.assert_called_once_with(
            "plugin-channel:example:parent",
            frozenset({"UCparent", "UCchild"}),
        )
        self.assertEqual(
            search_data.call_args.kwargs["channel_id_filter"],
            frozenset({"UCparent", "UCchild"}),
        )
        self.assertEqual(
            search_data.call_args.kwargs["channel_group_key"],
            "plugin-channel:example:parent",
        )
        self.assertEqual(connection.close.call_count, 2)
        handler.send_json.assert_called_once_with(payload)

    def test_search_passes_uploader_category_filters(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.config_data = {}
        handler.plugin_manager = Mock()
        handler.send_json = Mock()
        connection = Mock()
        payload = {"results": [], "total": 0}

        with (
            patch("yt_library.server.connect", return_value=connection),
            patch("yt_library.server.omni_search_data", return_value=payload) as search_data,
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/search?video_type=short,livestream"
                    "&video_broadcast_status=live,ended"
                    "&video_ai_disclosure=made_with_ai,unknown"
                    "&video_uploader_category=Science+%26+Technology,__no_category__&limit=1"
                )
            )

        self.assertEqual(
            search_data.call_args.kwargs["video_type_filters"],
            {"short", "livestream"},
        )
        self.assertEqual(
            search_data.call_args.kwargs["video_broadcast_status_filters"],
            {"live", "ended"},
        )
        self.assertEqual(
            search_data.call_args.kwargs["video_ai_disclosure_filters"],
            {"made_with_ai", "unknown"},
        )
        self.assertEqual(
            search_data.call_args.kwargs["video_uploader_category_filters"],
            {"Science & Technology", "__no_category__"},
        )
        connection.close.assert_called_once_with()
        handler.send_json.assert_called_once_with(payload)

    def test_search_applies_plugin_text_matches_without_filtering_for_presence(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.config_data = {}
        handler.plugin_manager = Mock()
        handler.plugin_manager.filter_videos.return_value = (
            frozenset({"matching", "other"}),
            frozenset({"matching"}),
        )
        handler.plugin_manager.project_videos.return_value = {
            "matching": {
                "video_id": "matching",
                "title": "Projected match",
            }
        }
        handler.send_json = Mock()

        with (
            patch("yt_library.server.connect", return_value=Mock()),
            patch(
                "yt_library.server.omni_search_data",
                return_value={"results": [], "total": 0},
            ) as search_data,
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/search?q=history&video_search_plugin=example&limit=1"
                )
            )

        handler.plugin_manager.filter_videos.assert_called_once_with("example", "history")
        handler.plugin_manager.project_videos.assert_called_once_with(
            "example",
            frozenset({"matching"}),
        )
        self.assertEqual(search_data.call_args.kwargs["video_id_filters"], [])
        self.assertEqual(
            search_data.call_args.kwargs["video_search_match_ids"],
            {"matching"},
        )
        self.assertEqual(
            search_data.call_args.kwargs["video_search_match_memberships"],
            {"example": frozenset({"matching"})},
        )

    def test_search_can_filter_plugin_presence_without_searching_plugin_text(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.config_data = {}
        handler.plugin_manager = Mock()
        video_ids = frozenset({"with-plugin-data"})
        handler.plugin_manager.filter_videos.return_value = (video_ids, frozenset())
        handler.send_json = Mock()

        with (
            patch("yt_library.server.connect", return_value=Mock()),
            patch(
                "yt_library.server.omni_search_data",
                return_value={"results": [], "total": 0},
            ) as search_data,
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/search?q=history&video_filter_plugin=example"
                    "&video_search_plugin=__none__&limit=1"
                )
            )

        handler.plugin_manager.filter_videos.assert_called_once_with("example", "")
        handler.plugin_manager.project_videos.assert_not_called()
        self.assertEqual(
            search_data.call_args.kwargs["video_id_filters"],
            [video_ids],
        )
        self.assertEqual(search_data.call_args.kwargs["video_search_match_ids"], set())
        self.assertEqual(
            search_data.call_args.kwargs["video_search_match_memberships"],
            {},
        )

    def test_search_applies_generic_plugin_video_exclusions(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.config_data = {}
        handler.plugin_manager = Mock()
        video_ids = frozenset({"with-plugin-data"})
        handler.plugin_manager.filter_videos.return_value = (video_ids, frozenset())
        handler.send_json = Mock()

        with (
            patch("yt_library.server.connect", return_value=Mock()),
            patch(
                "yt_library.server.omni_search_data",
                return_value={"results": [], "total": 0},
            ) as search_data,
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/search?video_facet_plugin=example&video_exclude_filter_plugin=example&limit=1"
                )
            )

        handler.plugin_manager.filter_videos.assert_called_once_with("example", "")
        self.assertEqual(
            search_data.call_args.kwargs["video_id_exclusion_filters"],
            [video_ids],
        )

    def test_playlist_video_route_applies_shared_search_facets(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.plugin_manager = Mock()
        handler.plugin_manager.filter_videos.side_effect = [
            (frozenset({"alpha", "beta"}), frozenset({"beta"})),
            (frozenset({"beta"}), frozenset()),
        ]
        handler.send_json = Mock()
        connection = Mock()
        payload = {"results": [], "total": 0}

        with (
            patch("yt_library.server.connect", return_value=connection),
            patch(
                "yt_library.server.video_collection_data",
                return_value=payload,
            ) as collection_data,
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/playlists/PLshared/videos?q=phrase"
                    "&search_fields=titles,subtitles&reaction=liked"
                    "&video_type=video,short&ai_disclosure=made_with_ai"
                    "&uploader_category=Music"
                    "&video_facet_plugin=subtitles"
                    "&video_filter_plugin=subtitles&video_exclude_filter_plugin=blocked"
                    "&video_search_plugin=subtitles&limit=10"
                )
            )

        handler.plugin_manager.filter_videos.assert_has_calls(
            [call("subtitles", "phrase"), call("blocked", "")]
        )
        kwargs = collection_data.call_args.kwargs
        self.assertEqual(kwargs["playlist_id"], "PLshared")
        self.assertEqual(kwargs["query"], "phrase")
        self.assertEqual(kwargs["search_fields"], {"titles", "subtitles"})
        self.assertEqual(kwargs["reaction_filters"], {"liked"})
        self.assertEqual(kwargs["video_type_filters"], {"video", "short"})
        self.assertEqual(kwargs["ai_disclosure_filters"], {"made_with_ai"})
        self.assertEqual(kwargs["uploader_category_filters"], {"Music"})
        self.assertEqual(kwargs["included_video_ids"], {"alpha", "beta"})
        self.assertEqual(kwargs["excluded_video_ids"], {"beta"})
        self.assertEqual(
            kwargs["video_facet_memberships"],
            {
                "subtitles": frozenset({"alpha", "beta"}),
                "blocked": frozenset({"beta"}),
            },
        )
        self.assertEqual(kwargs["video_search_match_ids"], {"beta"})
        self.assertEqual(
            kwargs["video_search_match_memberships"],
            {"subtitles": frozenset({"beta"})},
        )
        connection.close.assert_called_once_with()
        handler.send_json.assert_called_once_with(payload)

    def test_video_route_preserves_channel_search_scope_and_fields(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.plugin_manager = Mock()
        handler.plugin_manager.filter_videos.return_value = (
            frozenset({"channel-video"}),
            frozenset({"channel-video"}),
        )
        handler.send_json = Mock()
        connection = Mock()
        payload = {"results": [], "total": 0}

        with (
            patch("yt_library.server.connect", return_value=connection),
            patch(
                "yt_library.server.video_collection_data",
                return_value=payload,
            ) as collection_data,
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/videos?scope=channel&channel_id=UCscoped&q=needle"
                    "&search_fields=titles,notes&video_type=video"
                    "&video_facet_plugin=example&video_search_plugin=example"
                    "&limit=12&offset=24"
                )
            )

        kwargs = collection_data.call_args.kwargs
        self.assertEqual(kwargs["scope"], "channel")
        self.assertEqual(kwargs["channel_id"], "UCscoped")
        self.assertEqual(kwargs["query"], "needle")
        self.assertEqual(kwargs["search_fields"], {"titles", "notes"})
        self.assertEqual(kwargs["video_type_filters"], {"video"})
        self.assertIsNone(kwargs["included_video_ids"])
        self.assertEqual(kwargs["excluded_video_ids"], set())
        self.assertEqual(
            kwargs["video_facet_memberships"],
            {"example": frozenset({"channel-video"})},
        )
        self.assertEqual(
            kwargs["video_search_match_memberships"],
            {"example": frozenset({"channel-video"})},
        )
        self.assertEqual(kwargs["limit"], 12)
        self.assertEqual(kwargs["offset"], 24)
        connection.close.assert_called_once_with()
        handler.send_json.assert_called_once_with(payload)

    def test_get_dispatches_page_admin_and_library_routes(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler._handle_page_get = Mock(return_value=False)
        handler._handle_admin_get = Mock()
        handler._handle_library_get = Mock()

        handler.path = "/api/admin/status?include_logs=0"
        handler.do_GET()

        handler._handle_page_get.assert_called_once_with("/api/admin/status")
        admin_request = handler._handle_admin_get.call_args.args[0]
        self.assertEqual(admin_request.path, "/api/admin/status")
        self.assertEqual(admin_request.query, "include_logs=0")
        handler._handle_library_get.assert_not_called()

        handler._handle_page_get.reset_mock()
        handler._handle_admin_get.reset_mock()
        handler.path = "/api/videos?limit=1"
        handler.do_GET()

        library_request = handler._handle_library_get.call_args.args[0]
        self.assertEqual(library_request.path, "/api/videos")
        handler._handle_admin_get.assert_not_called()

    def test_admin_runtime_status_route_returns_current_process_state(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.service_status = Mock(return_value={"status": "running", "pid": 321})
        handler.send_json = Mock()
        runtime_payload = {
            "workerQueueRunning": True,
            "workerQueueStopping": False,
            "workerQueueCount": 12,
        }

        with patch(
            "yt_library.server.admin_runtime_status",
            return_value=runtime_payload.copy(),
        ) as runtime_status:
            handler._handle_admin_get(
                urllib.parse.urlparse("/api/admin/runtime/status")
            )

        runtime_status.assert_called_once_with(
            Path("library.sqlite3"),
            server.WORKER_QUEUE_DISPATCHER,
        )
        handler.send_json.assert_called_once_with(
            {
                **runtime_payload,
                "service": {"status": "running", "pid": 321},
            }
        )

    def test_service_status_includes_current_server_time(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.restart_pending = Mock(return_value=False)
        handler.service_started_at = "2026-08-21T12:00:00Z"

        with patch(
            "yt_library.server.utc_timestamp",
            return_value="2026-08-21T12:34:56Z",
        ) as timestamp:
            status = handler.service_status()

        timestamp.assert_called_once()
        self.assertEqual(status["serverTime"], "2026-08-21T12:34:56Z")
        self.assertEqual(status["startedAt"], "2026-08-21T12:00:00Z")

    def test_video_batch_route_hydrates_requested_library_videos(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.send_json = Mock()
        connection = Mock()
        payload = {"videos": [{"video_id": "first123"}]}

        with (
            patch("yt_library.server.connect", return_value=connection),
            patch("yt_library.server.video_summaries_data", return_value=payload) as summaries,
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/videos/batch?id=first123&id=second123&id=first123"
                )
            )

        summaries.assert_called_once_with(connection, ["first123", "second123"])
        connection.close.assert_called_once_with()
        handler.send_json.assert_called_once_with(payload)

    def test_channel_batch_route_hydrates_requested_library_channels(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.send_json = Mock()
        connection = Mock()
        payload = {"channels": [{"channel_id": "UC_first"}]}

        with (
            patch("yt_library.server.connect", return_value=connection),
            patch("yt_library.server.channel_summaries_data", return_value=payload) as summaries,
        ):
            handler._handle_library_get(
                urllib.parse.urlparse(
                    "/api/channels/batch?id=UC_first&id=UC_second&id=UC_first"
                )
            )

        summaries.assert_called_once_with(connection, ["UC_first", "UC_second"])
        connection.close.assert_called_once_with()
        handler.send_json.assert_called_once_with(payload)

    def test_video_detail_route_falls_back_to_optional_plugin_projection(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.plugin_manager = Mock()
        handler.plugin_manager.projected_video.return_value = {
            "video_id": "projected1",
            "title": "Projected video",
            "projection_plugin_ids": ["example"],
        }
        handler.send_json = Mock()
        connection = Mock()

        with (
            patch("yt_library.server.connect", return_value=connection),
            patch("yt_library.server.video_detail_data", return_value=None),
        ):
            handler._handle_library_get(
                urllib.parse.urlparse("/api/videos/projected1")
            )

        handler.plugin_manager.projected_video.assert_called_once_with("projected1")
        payload = handler.send_json.call_args.args[0]
        self.assertEqual(payload["video_id"], "projected1")
        self.assertEqual(payload["metadata_title"], "Projected video")
        self.assertTrue(payload["virtual_video"])
        self.assertEqual(payload["projection_plugin_ids"], ["example"])
        connection.close.assert_called_once_with()

    def test_get_page_route_stops_api_dispatch(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/theme.js"
        handler._handle_page_get = Mock(return_value=True)
        handler._handle_admin_get = Mock()
        handler._handle_library_get = Mock()

        handler.do_GET()

        handler._handle_admin_get.assert_not_called()
        handler._handle_library_get.assert_not_called()

    def test_page_routes_serve_extracted_browser_scripts(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler._send_bytes = Mock()

        self.assertTrue(handler._handle_page_get("/index.js"))
        handler._send_bytes.assert_called_once_with(
            server.INDEX_JS.encode("utf-8"),
            "text/javascript; charset=utf-8",
            cache_control="no-cache",
        )

        handler._send_bytes.reset_mock()
        self.assertTrue(handler._handle_page_get("/entity-card-extensions.js"))
        handler._send_bytes.assert_called_once_with(
            server.ENTITY_CARD_EXTENSIONS_JS.encode("utf-8"),
            "text/javascript; charset=utf-8",
            cache_control="no-cache",
        )

        handler._send_bytes.reset_mock()
        self.assertTrue(handler._handle_page_get("/search-result-presentations.js"))
        handler._send_bytes.assert_called_once_with(
            server.SEARCH_RESULT_PRESENTATIONS_JS.encode("utf-8"),
            "text/javascript; charset=utf-8",
            cache_control="no-cache",
        )

        handler._send_bytes.reset_mock()
        self.assertTrue(handler._handle_page_get("/history-workflow.js"))
        handler._send_bytes.assert_called_once_with(
            server.HISTORY_WORKFLOW_JS.encode("utf-8"),
            "text/javascript; charset=utf-8",
            cache_control="no-cache",
        )

        handler._send_bytes.reset_mock()
        self.assertTrue(handler._handle_page_get("/admin-transport.js"))
        handler._send_bytes.assert_called_once_with(
            server.ADMIN_TRANSPORT_JS.encode("utf-8"),
            "text/javascript; charset=utf-8",
            cache_control="no-cache",
        )

        handler._send_bytes.reset_mock()
        self.assertTrue(handler._handle_page_get("/admin.js"))
        handler._send_bytes.assert_called_once_with(
            server.ADMIN_JS.encode("utf-8"),
            "text/javascript; charset=utf-8",
            cache_control="no-cache",
        )

    def test_browser_page_routes_serve_the_application_shell(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.render_page = Mock(return_value=b"browser shell")
        handler._send_bytes = Mock()

        for path in (
            "/search",
            "/videos",
            "/videos/video123",
            "/clips/clip123",
            "/playlists",
            "/playlists/playlist123",
            "/channels/@alias",
            "/history",
        ):
            with self.subTest(path=path):
                handler._send_bytes.reset_mock()
                self.assertTrue(handler._handle_page_get(path))
                handler._send_bytes.assert_called_once_with(
                    b"browser shell",
                    "text/html; charset=utf-8",
                    cache_control="no-store",
                )

        self.assertFalse(handler._handle_page_get("/video/video123"))

    def test_rendered_browser_page_loads_helpers_before_index(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.display_timezone_name = Mock(return_value="UTC")
        handler.layout_settings = Mock(return_value={})

        rendered = handler.render_page(
            '<html><head></head><body><script src="/index.js"></script></body></html>'
        ).decode("utf-8")

        self.assertIn('<script src="/history-workflow.js"></script>', rendered)
        self.assertIn('<script src="/search-result-presentations.js"></script>', rendered)
        self.assertLess(
            rendered.index('<script src="/history-workflow.js"></script>'),
            rendered.index('<script src="/index.js"></script>'),
        )
        self.assertLess(
            rendered.index('<script src="/search-result-presentations.js"></script>'),
            rendered.index('<script src="/index.js"></script>'),
        )

    def test_post_dispatches_to_explicit_route_groups(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler._handle_cookie_post = Mock()
        handler._handle_preference_post = Mock()
        handler._handle_admin_configuration_post = Mock()
        handler._handle_admin_action_post = Mock()
        handler.send_error = Mock()

        cases = (
            ("/api/admin/cookies/youtube", "_handle_cookie_post"),
            ("/api/settings/page-size?value=250", "_handle_preference_post"),
            ("/api/settings/hide-empty-filters?enabled=1", "_handle_preference_post"),
            ("/api/admin/update-schedule?frequency=off", "_handle_admin_configuration_post"),
            ("/api/admin/queue/start", "_handle_admin_action_post"),
        )
        for path, expected_handler in cases:
            with self.subTest(path=path):
                for name in (
                    "_handle_cookie_post",
                    "_handle_preference_post",
                    "_handle_admin_configuration_post",
                    "_handle_admin_action_post",
                ):
                    getattr(handler, name).reset_mock()
                handler.send_error.reset_mock()
                handler.path = path

                handler.do_POST()

                getattr(handler, expected_handler).assert_called_once()
                handler.send_error.assert_not_called()

        handler.path = "/api/unknown"
        handler.do_POST()
        handler.send_error.assert_called_once_with(404, "Not found")

    def test_sort_preference_saves_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/settings/sort?context=liked&value=most_watched"
            handler.config_data = config
            handler.send_json = Mock()

            handler.do_POST()

            expected = {"liked": "most_watched"}
            self.assertEqual(config["sort_preferences"], expected)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["sort_preferences"], expected)
            handler.send_json.assert_called_once_with(
                {
                    "ok": True,
                    "context": "liked",
                    "sort": "most_watched",
                }
            )

    def test_parallel_preference_handlers_share_serialized_config_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            store = ConfigStore(config)
            paths = (
                "/api/settings/sort?context=search&value=newest",
                "/api/settings/sort?context=liked&value=most_watched",
            )
            handlers = []
            for path in paths:
                handler = object.__new__(server.LibraryHandler)
                handler.path = path
                handler.config_data = config
                handler.config_store = store
                handler.send_json = Mock()
                handlers.append(handler)

            errors: list[BaseException] = []

            def submit(handler: server.LibraryHandler) -> None:
                try:
                    handler.do_POST()
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=submit, args=(handler,))
                for handler in handlers
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            expected = {"search": "newest", "liked": "most_watched"}
            self.assertEqual(config["sort_preferences"], expected)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["sort_preferences"], expected)
            for handler in handlers:
                handler.send_json.assert_called_once()

    def test_sort_preference_rejects_mismatched_regime(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/settings/sort?context=playlist&value=newest"
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        self.assertEqual(config["sort_preferences"], {})
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_page_size_preference_saves_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/settings/page-size?value=250"
            handler.config_data = config
            handler.send_json = Mock()

            handler.do_POST()

            self.assertEqual(config["page_size"], 250)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["page_size"], 250)
            handler.send_json.assert_called_once_with({"ok": True, "pageSize": 250})

    def test_page_size_preference_rejects_unknown_values(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/settings/page-size?value=42"
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        self.assertEqual(config["page_size"], 100)
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_partial_completion_minimum_saves_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/settings/partial-completion-minimum?value=65"
            handler.config_data = config
            handler.send_json = Mock()

            handler.do_POST()

            self.assertEqual(config["partial_completion_min_percent"], 65)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["partial_completion_min_percent"], 65)
            handler.send_json.assert_called_once_with(
                {"ok": True, "partialCompletionMinPercent": 65}
            )

    def test_partial_completion_minimum_rejects_out_of_range_value(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/settings/partial-completion-minimum?value=100"
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        self.assertEqual(config["partial_completion_min_percent"], 1)
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_hide_empty_filters_preference_saves_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            request_restart = Mock()
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/settings/hide-empty-filters?enabled=0"
            handler.config_data = config
            handler.request_restart = request_restart
            handler.send_json = Mock()

            handler.do_POST()

            self.assertFalse(config["hide_empty_filters"])
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertFalse(payload["hide_empty_filters"])
            request_restart.assert_not_called()
            handler.send_json.assert_called_once_with(
                {"ok": True, "hideEmptyFilters": False}
            )

    def test_hide_empty_filters_preference_rejects_unknown_values(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/settings/hide-empty-filters?enabled=maybe"
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        self.assertTrue(config["hide_empty_filters"])
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_filter_preference_saves_and_removes_sparse_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.config_data = config
            handler.send_json = Mock()

            handler.path = (
                "/api/settings/filter-preference?"
                "key=completion.partial_below_minimum&enabled=1"
            )
            handler.do_POST()

            expected = {"completion.partial_below_minimum": True}
            self.assertEqual(config["filter_preferences"], expected)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["filter_preferences"], expected)
            handler.send_json.assert_called_with(
                {
                    "ok": True,
                    "key": "completion.partial_below_minimum",
                    "enabled": True,
                    "filterPreferences": expected,
                }
            )

            handler.path = (
                "/api/settings/filter-preference?"
                "key=completion.partial_below_minimum&enabled=0"
            )
            handler.do_POST()

            self.assertEqual(config["filter_preferences"], {})
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["filter_preferences"], {})

    def test_subtitle_search_preference_is_saved_as_an_opt_in_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.config_data = config
            handler.send_json = Mock()
            handler.path = (
                "/api/settings/filter-preference?key=plugins.subtitles.search&enabled=1"
            )

            handler.do_POST()

            self.assertEqual(
                config["filter_preferences"],
                {"plugins.subtitles.search": True},
            )
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["filter_preferences"],
                {"plugins.subtitles.search": True},
            )

    def test_search_filter_tree_state_saves_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.config_data = config
            handler.send_json = Mock()
            handler.path = (
                "/api/settings/search-filter-tree?"
                "expanded=kind:videos,facet:videos"
            )

            handler.do_POST()

            expected = ["kind:videos", "facet:videos"]
            self.assertEqual(config["search_filter_tree_expanded"], expected)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["search_filter_tree_expanded"], expected)
            handler.send_json.assert_called_once_with(
                {
                    "ok": True,
                    "searchFilterTreeExpanded": expected,
                }
            )

    def test_search_filter_tree_state_rejects_invalid_nodes(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = (
            "/api/settings/search-filter-tree?expanded=kind:videos,bad%20node"
        )
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        self.assertEqual(
            config["search_filter_tree_expanded"],
            ["kind:videos", "kind:playlists", "kind:channels"],
        )
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_navigation_group_tree_state_saves_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.config_data = config
            handler.send_json = Mock()
            handler.path = (
                "/api/settings/navigation-group-tree?"
                "collapsed=playlist-group%3Aplugin-root%3Apockettube&"
                "collapsed=channel-group%3Aplugin-root%3Apockettube&"
                "collapsed=channel-group%3Aplugin-channel%3Apockettube%3AAdventure"
            )

            handler.do_POST()

            expected = [
                "playlist-group:plugin-root:pockettube",
                "channel-group:plugin-root:pockettube",
                "channel-group:plugin-channel:pockettube:Adventure",
            ]
            self.assertEqual(config["navigation_group_tree_collapsed"], expected)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["navigation_group_tree_collapsed"], expected)
            handler.send_json.assert_called_once_with(
                {
                    "ok": True,
                    "navigationGroupTreeCollapsed": expected,
                }
            )

            handler.send_json.reset_mock()
            handler.path = "/api/settings/navigation-group-tree"
            handler.do_POST()

            self.assertEqual(config["navigation_group_tree_collapsed"], [])
            handler.send_json.assert_called_once_with(
                {
                    "ok": True,
                    "navigationGroupTreeCollapsed": [],
                }
            )

    def test_navigation_group_tree_state_rejects_invalid_nodes(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = (
            "/api/settings/navigation-group-tree?collapsed=channel-group%3A"
        )
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        self.assertEqual(config["navigation_group_tree_collapsed"], [])
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_filter_preference_rejects_unknown_key(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/settings/filter-preference?key=videos.public&enabled=1"
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        self.assertEqual(config["filter_preferences"], {})
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_layout_preference_saves_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/settings/layout?context=history&value=detailed"
            handler.config_data = config
            handler.send_json = Mock()

            handler.do_POST()

            self.assertEqual(config["history_card_layout"], "detailed")
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["history_card_layout"], "detailed")
            self.assertEqual(payload["search_card_layout"], "grid")
            handler.send_json.assert_called_once_with(
                {"ok": True, "context": "history", "layout": "detailed"}
            )

            handler.path = "/api/settings/layout?context=playlist&value=compact"
            handler.send_json.reset_mock()
            handler.do_POST()

            self.assertEqual(config["playlist_card_layout"], "compact")
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["playlist_card_layout"], "compact")
            handler.send_json.assert_called_once_with(
                {"ok": True, "context": "playlist", "layout": "compact"}
            )

            handler.path = (
                "/api/settings/layout?context=channel-playlisted-videos&value=compact"
            )
            handler.send_json.reset_mock()
            handler.do_POST()

            self.assertEqual(
                config["channel_playlisted_video_card_layout"],
                "compact",
            )
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["channel_playlisted_video_card_layout"],
                "compact",
            )
            handler.send_json.assert_called_once_with(
                {
                    "ok": True,
                    "context": "channel-playlisted-videos",
                    "layout": "compact",
                }
            )

            handler.path = (
                "/api/settings/layout?context=channel-playlists&value=detailed"
            )
            handler.send_json.reset_mock()
            handler.do_POST()

            self.assertEqual(config["channel_playlist_card_layout"], "detailed")
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["channel_playlist_card_layout"], "detailed")
            self.assertEqual(payload["channel_history_card_layout"], "detailed")
            handler.send_json.assert_called_once_with(
                {
                    "ok": True,
                    "context": "channel-playlists",
                    "layout": "detailed",
                }
            )

            handler.path = "/api/settings/layout?context=channel-history&value=grid"
            handler.send_json.reset_mock()
            handler.do_POST()

            self.assertEqual(config["channel_history_card_layout"], "grid")
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["channel_history_card_layout"], "grid")
            self.assertEqual(payload["channel_playlist_card_layout"], "detailed")
            handler.send_json.assert_called_once_with(
                {"ok": True, "context": "channel-history", "layout": "grid"}
            )

    def test_layout_preference_rejects_unknown_values(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/settings/layout?context=search&value=wide"
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        self.assertEqual(config["search_card_layout"], "grid")
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_dispatch_settings_save_config_and_reconfigure_live_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/dispatch-settings?" + urllib.parse.urlencode(
                {
                    "dispatch_mode": "throttle",
                    "job_dispatch_delay_seconds": "5",
                    "request_delay_min_seconds": "6",
                    "request_delay_max_seconds": "10",
                    "youtube_max_in_flight": "8",
                    "archivarix_max_in_flight": "2",
                }
            )
            handler.config_data = config
            handler.send_json = Mock()

            with (
                patch.object(
                    server.WORKER_QUEUE_DISPATCHER,
                    "update_dispatch_settings",
                ) as update_settings,
                patch("yt_library.server.configure_request_pacing") as configure_pacing,
            ):
                handler.do_POST()

            update_settings.assert_called_once_with("throttle", 5.0, 8, 2)
            configure_pacing.assert_called_once_with(config)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["dispatch_mode"], "throttle")
            self.assertEqual(payload["job_dispatch_delay_seconds"], 5.0)
            self.assertEqual(payload["request_delay_min_seconds"], 6.0)
            self.assertEqual(payload["request_delay_max_seconds"], 10.0)
            self.assertEqual(payload["youtube_max_in_flight"], 8)
            self.assertEqual(payload["archivarix_max_in_flight"], 2)
            response = handler.send_json.call_args.args[0]
            self.assertEqual(
                response["dispatchSettings"]["dispatch_mode"],
                "throttle",
            )
            self.assertEqual(
                response["dispatchSettings"]["effective_job_dispatch_delay_seconds"],
                0.0,
            )

    def test_dispatch_settings_reject_throttle_maximum_below_minimum(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/admin/dispatch-settings?" + urllib.parse.urlencode(
            {
                "dispatch_mode": "throttle",
                "job_dispatch_delay_seconds": "5",
                "request_delay_min_seconds": "10",
                "request_delay_max_seconds": "6",
                "youtube_max_in_flight": "8",
                "archivarix_max_in_flight": "2",
            }
        )
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        response = handler.send_json.call_args.args[0]
        self.assertIn("maximum", response["error"])
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_timezone_post_and_delete_share_transition_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            db_path = Path(temp_dir) / "library.sqlite3"
            config = load_config(config_path)
            core.migrate_database(db_path)
            handler = object.__new__(server.LibraryHandler)
            handler.config_data = config
            handler.db_path = db_path
            handler.send_json = Mock()

            with (
                patch.object(
                    server.UPDATE_SCHEDULER,
                    "schedule_changed",
                ) as schedule_changed,
                patch.object(
                    server,
                    "refresh_exact_history_dates",
                ) as refresh_dates,
            ):
                handler.path = (
                    "/api/settings/timezone?value=America%2FLos_Angeles"
                )
                handler.do_POST()

                self.assertEqual(config["display_timezone"], "America/Los_Angeles")
                schedule_changed.assert_called_once_with(config)
                self.assertEqual(refresh_dates.call_args.args[1], "America/Los_Angeles")

                schedule_changed.reset_mock()
                refresh_dates.reset_mock()
                handler.send_json.reset_mock()
                handler.path = "/api/settings/timezone"
                handler.do_DELETE()

                self.assertEqual(config["display_timezone"], "")
                schedule_changed.assert_called_once_with(config)
                self.assertEqual(refresh_dates.call_args.args[1], "UTC")

            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["display_timezone"], "")
            handler.send_json.assert_called_once_with(
                {"ok": True, "displayTimezone": ""}
            )

    def test_admin_settings_save_proxy_and_schedule_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            db_path = Path(temp_dir) / "library.sqlite3"
            config = load_config(config_path)
            core.migrate_database(db_path)
            conn = core.connect(db_path)
            try:
                with conn:
                    core.set_external_service_block(
                        conn,
                        "proxy",
                        "proxy_unavailable",
                        "Old proxy failed",
                    )
            finally:
                conn.close()
            request_restart = Mock(return_value=True)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/settings?" + urllib.parse.urlencode(
                {
                    "display_timezone": "America/Los_Angeles",
                    "week_start": "monday",
                    "hide_empty_filters": "0",
                    "use_proxy": "1",
                    "proxy": "socks5h://127.0.0.1:1081",
                }
            )
            handler.db_path = db_path
            handler.config_data = config
            handler.service_started_at = "2026-07-28T12:00:00Z"
            handler.restart_pending = lambda: request_restart.called
            handler.request_restart = request_restart
            handler.send_json = Mock()

            handler.do_POST()

            request_restart.assert_called_once_with()
            self.assertEqual(config["display_timezone"], "America/Los_Angeles")
            self.assertEqual(config["week_start"], "monday")
            self.assertFalse(config["hide_empty_filters"])
            self.assertTrue(config["use_proxy"])
            self.assertEqual(config["proxy"], "socks5h://127.0.0.1:1081")
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["use_proxy"])
            self.assertEqual(payload["proxy"], "socks5h://127.0.0.1:1081")
            conn = core.connect(db_path)
            try:
                self.assertFalse(core.external_service_block(conn, "proxy")["blocked"])
            finally:
                conn.close()
            response = handler.send_json.call_args.args[0]
            self.assertTrue(response["restartScheduled"])
            self.assertEqual(response["settings"]["weekStart"], "monday")
            self.assertFalse(response["settings"]["hideEmptyFilters"])
            self.assertEqual(response["service"]["status"], "restarting")

    def test_admin_settings_reject_invalid_week_start(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = (
            "/api/admin/settings?"
            + urllib.parse.urlencode(
                {
                    "display_timezone": "UTC",
                    "week_start": "tuesday",
                    "use_proxy": "0",
                    "proxy": "",
                }
            )
        )
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        handler.send_json.assert_called_once_with(
            {"error": "Invalid week start: tuesday"},
            status=400,
        )

    def test_admin_settings_reject_enabled_proxy_without_an_address(self) -> None:
        config = load_config(Path("missing-test-config.json"))
        handler = object.__new__(server.LibraryHandler)
        handler.path = (
            "/api/admin/settings?"
            + urllib.parse.urlencode(
                {
                    "display_timezone": "UTC",
                    "use_proxy": "1",
                    "proxy": "",
                }
            )
        )
        handler.config_data = config
        handler.send_json = Mock()

        handler.do_POST()

        response = handler.send_json.call_args.args[0]
        self.assertIn("SOCKS5 proxy URL", response["error"])
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_admin_advanced_setting_saves_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/advanced?enabled=1"
            handler.config_data = config
            handler.send_json = Mock()

            handler.do_POST()

            payload = json.loads(config_path.read_text(encoding="utf-8"))
            response = handler.send_json.call_args.args[0]

        self.assertTrue(config["admin_advanced"])
        self.assertTrue(payload["admin_advanced"])
        self.assertTrue(response["settings"]["adminAdvanced"])

    def test_archivarix_auto_retry_setting_saves_and_updates_live_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/archivarix-auto-retry?enabled=0"
            handler.config_data = config
            handler.send_json = Mock()

            with patch.object(
                server.UPDATE_SCHEDULER,
                "schedule_changed",
            ) as schedule_changed:
                handler.do_POST()

            payload = json.loads(config_path.read_text(encoding="utf-8"))
            response = handler.send_json.call_args.args[0]

        self.assertFalse(config["archivarix_auto_retry"])
        self.assertFalse(payload["archivarix_auto_retry"])
        self.assertFalse(response["settings"]["archivarixAutoRetry"])
        schedule_changed.assert_called_once_with(config)

    def test_archivarix_auto_retry_due_only_after_rate_limit_utc_day(self) -> None:
        current = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
        prior_rate_limit = {
            "blocked": True,
            "reason_code": "rate_limited",
            "blocked_at": "2026-08-09T23:59:59Z",
        }

        self.assertTrue(server.archivarix_auto_retry_due(prior_rate_limit, current))
        self.assertFalse(
            server.archivarix_auto_retry_due(
                {**prior_rate_limit, "blocked_at": "2026-08-10T00:00:00Z"},
                current,
            )
        )
        self.assertFalse(
            server.archivarix_auto_retry_due(
                {**prior_rate_limit, "reason_code": "authentication_error"},
                current,
            )
        )

    def test_archivarix_automatic_retry_clears_hold_logs_and_starts_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.set_external_service_block(
                        conn,
                        "archivarix",
                        "rate_limited",
                        "Archivarix daily search limit reached",
                    )
                    conn.execute(
                        "UPDATE external_service_blocks SET blocked_at = ? WHERE service = ?",
                        ("2020-01-01T00:00:00Z", "archivarix"),
                    )
            finally:
                conn.close()
            config = load_config(Path(temp_dir) / "config.json")
            cookie_file = Path(temp_dir) / "youtube-cookies.txt"
            video_thumbs = Path(temp_dir) / "video-thumbs"

            with (
                patch.object(
                    server.WORKER_QUEUE_DISPATCHER,
                    "allow_archivarix_retry",
                ) as allow_retry,
                patch.object(
                    server.WORKER_QUEUE_DISPATCHER,
                    "start",
                    return_value={"started": True},
                ) as start,
            ):
                result = server.retry_archivarix_queue(
                    db_path,
                    cookie_file,
                    video_thumbs,
                    config,
                    automatic=True,
                )

            conn = core.connect(db_path)
            try:
                block = core.external_service_block(conn, "archivarix")
                log = conn.execute(
                    "SELECT level, message FROM metadata_worker_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()

        self.assertTrue(result["retried"])
        self.assertTrue(result["cleared"])
        self.assertFalse(block["blocked"])
        self.assertEqual(log["level"], "queue info")
        self.assertIn("new UTC day", log["message"])
        allow_retry.assert_called_once_with()
        start.assert_called_once_with(db_path, cookie_file, video_thumbs, config)

    def test_update_schedule_endpoint_saves_and_updates_live_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "yt_library.config.json"
            config = load_config(config_path)
            handler = object.__new__(server.LibraryHandler)
            handler.path = (
                "/api/admin/update-schedule?frequency=hourly&at=04%3A30&minute=17"
            )
            handler.config_data = config
            handler.send_json = Mock()

            with patch.object(
                server.UPDATE_SCHEDULER,
                "schedule_changed",
            ) as schedule_changed:
                handler.do_POST()

            payload = json.loads(config_path.read_text(encoding="utf-8"))
            response = handler.send_json.call_args.args[0]

        self.assertEqual(payload["update_frequency"], "hourly")
        self.assertEqual(payload["update_hour_minute"], 17)
        self.assertEqual(payload["update_time"], "04:30")
        self.assertEqual(response["settings"]["updateFrequency"], "hourly")
        self.assertEqual(response["settings"]["updateHourMinute"], 17)
        self.assertEqual(response["settings"]["updateTime"], "04:30")
        schedule_changed.assert_called_once_with(config)

    def test_manual_update_records_latest_result_for_status_polling(self) -> None:
        scheduler = server.UpdateScheduler()
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/admin/update/start"
        handler.db_path = Path("library.sqlite3")
        handler.cookie_file = Path("youtube-cookies.txt")
        handler.video_thumbs = Path("video-thumbs")
        handler.config_data = {}
        handler.send_json = Mock()
        update_result = {
            "queue": {"inserted": 2, "already_queued": 36},
            "pluginQueue": [],
            "dispatcher": {"started": True},
        }

        with patch.object(
            server,
            "enqueue_library_update",
            return_value=update_result,
        ), patch.object(server, "UPDATE_SCHEDULER", scheduler):
            handler.do_POST()

        status = scheduler.status(handler.config_data)
        self.assertEqual(
            status["lastResult"],
            {
                "source": "manual",
                "queuedAt": status["lastQueuedAt"],
                "inserted": 2,
                "alreadyQueued": 36,
            },
        )
        self.assertTrue(status["lastQueuedAt"].endswith("Z"))
        handler.send_json.assert_called_once_with({"ok": True, **update_result})

    def test_update_scheduler_records_scheduled_result(self) -> None:
        scheduler = server.UpdateScheduler()

        scheduler.record_update_result(
            {"inserted": 4, "already_queued": 12},
            scheduled=True,
            queued_at="2026-08-19T18:00:00Z",
        )

        status = scheduler.status({})
        self.assertEqual(
            status["lastResult"],
            {
                "source": "scheduled",
                "queuedAt": "2026-08-19T18:00:00Z",
                "inserted": 4,
                "alreadyQueued": 12,
            },
        )
        self.assertEqual(status["lastQueuedAt"], "2026-08-19T18:00:00Z")

    def test_update_schedule_endpoint_rejects_invalid_time(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/admin/update-schedule?frequency=daily&at=25%3A00"
        handler.config_data = load_config(Path("missing-test-config.json"))
        handler.send_json = Mock()

        handler.do_POST()

        response = handler.send_json.call_args.args[0]
        self.assertIn("HH:MM", response["error"])
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_update_schedule_endpoint_rejects_invalid_frequency(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/admin/update-schedule?frequency=weekly&at=04%3A30"
        handler.config_data = load_config(Path("missing-test-config.json"))
        handler.send_json = Mock()

        handler.do_POST()

        response = handler.send_json.call_args.args[0]
        self.assertIn("frequency", response["error"])
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_update_schedule_endpoint_rejects_invalid_hour_minute(self) -> None:
        handler = object.__new__(server.LibraryHandler)
        handler.path = "/api/admin/update-schedule?frequency=hourly&minute=60"
        handler.config_data = load_config(Path("missing-test-config.json"))
        handler.send_json = Mock()

        handler.do_POST()

        response = handler.send_json.call_args.args[0]
        self.assertIn("between 0 and 59", response["error"])
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_scheduled_update_queues_incremental_work_and_starts_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()
            cookie_file = Path(temp_dir) / "cookies.txt"
            video_thumbs = Path(temp_dir) / "video_thumbs"
            config = load_config(Path(temp_dir) / "config.json")

            with patch.object(
                server.WORKER_QUEUE_DISPATCHER,
                "start",
                return_value={"started": True},
            ) as start_dispatcher:
                result = server.enqueue_library_update(
                    db_path,
                    cookie_file,
                    video_thumbs,
                    config,
                    scheduled=True,
                )

            conn = core.connect(db_path)
            try:
                queue_rows = conn.execute(
                    """
                    SELECT subject_key, task_type, manual
                    FROM worker_queue
                    ORDER BY subject_key
                    """
                ).fetchall()
                log = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(result["dispatcher"], {"started": True})
        self.assertEqual(result["queue"]["inserted"], 4)
        self.assertEqual(
            {row["subject_key"] for row in queue_rows},
            {
                "account:sync",
                "clip:discover",
                "history:recent",
                "playlist:discover-current",
            },
        )
        self.assertEqual(tuple(log)[0], "queue info")
        self.assertIn("Scheduled update queued", tuple(log)[1])
        start_dispatcher.assert_called_once_with(
            db_path,
            cookie_file,
            video_thumbs,
            config,
        )

    def test_proxy_retry_clears_hold_and_starts_dispatcher(self) -> None:
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

            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/proxy/retry"
            handler.db_path = db_path
            handler.cookie_file = Path(temp_dir) / "youtube-cookies.txt"
            handler.video_thumbs = Path(temp_dir) / "video-thumbs"
            handler.config_data = load_config(Path(temp_dir) / "config.json")
            handler.send_json = Mock()

            scheduler = server.UpdateScheduler()
            scheduler._last_error = "SOCKS5 proxy is unavailable"
            scheduler._last_error_kind = "proxy"

            blocked_when_started: list[bool] = []

            def recover_proxy(*_args, **_kwargs):
                conn = core.connect(db_path)
                try:
                    blocked_when_started.append(
                        core.external_service_block(conn, "proxy")["blocked"]
                    )
                    with conn:
                        core.clear_external_service_block(conn, "proxy")
                finally:
                    conn.close()
                return {"started": True}

            with patch.object(
                workers.WORKER_QUEUE_DISPATCHER,
                "start",
                side_effect=recover_proxy,
            ) as start, patch.object(server, "UPDATE_SCHEDULER", scheduler):
                handler.do_POST()

            self.assertEqual(blocked_when_started, [True])
            start.assert_called_once_with(
                db_path,
                handler.cookie_file,
                handler.video_thumbs,
                handler.config_data,
            )
            conn = core.connect(db_path)
            try:
                self.assertFalse(core.external_service_block(conn, "proxy")["blocked"])
                retry_log = conn.execute(
                    """
                    SELECT level, message
                    FROM metadata_worker_log
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(retry_log["level"], "queue info")
                self.assertIn("Proxy retry requested", retry_log["message"])
            finally:
                conn.close()
            response = handler.send_json.call_args.args[0]
            self.assertTrue(response["ok"])
            self.assertTrue(response["cleared"])
            self.assertFalse(response["proxyBlock"]["blocked"])
            self.assertEqual(scheduler.status(handler.config_data)["lastError"], "")

    def test_successful_queue_start_preserves_non_proxy_scheduler_error(self) -> None:
        scheduler = server.UpdateScheduler()
        scheduler._last_error = "YouTube authentication failed"
        scheduler._last_error_kind = "update"
        handler = object.__new__(server.LibraryHandler)
        handler.db_path = Path("library.sqlite3")
        handler.cookie_file = Path("youtube-cookies.txt")
        handler.video_thumbs = Path("video-thumbs")
        handler.config_data = {}

        with patch.object(
            server.WORKER_QUEUE_DISPATCHER,
            "start",
            return_value={"started": True},
        ), patch.object(server, "UPDATE_SCHEDULER", scheduler):
            handler._start_worker_queue()

        self.assertEqual(
            scheduler.status(handler.config_data)["lastError"],
            "YouTube authentication failed",
        )

    def test_feature_backfill_endpoint_queues_selected_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.upsert_video(
                        conn,
                        "backfillvid",
                        title="Backfill video",
                        availability="public",
                    )
            finally:
                conn.close()

            handler = object.__new__(server.LibraryHandler)
            handler.path = (
                "/api/admin/feature-backfill/start"
                "?kind=video_visibility&limit=1"
            )
            handler.db_path = db_path
            handler.cookie_file = Path(temp_dir) / "cookies.txt"
            handler.video_thumbs = Path(temp_dir) / "video_thumbs"
            handler.config_data = {}
            handler.send_json = Mock()

            with patch.object(
                server.WORKER_QUEUE_DISPATCHER,
                "start",
                return_value={"started": True},
            ) as start_dispatcher:
                handler.do_POST()

            response = handler.send_json.call_args.args[0]
            conn = core.connect(db_path)
            try:
                queued = conn.execute(
                    """
                    SELECT video_id
                    FROM worker_queue
                    WHERE worker_type = 'metadata'
                    """
                ).fetchone()["video_id"]
            finally:
                conn.close()

        self.assertEqual(response["queue"]["kind"], "video_visibility")
        self.assertEqual(response["queue"]["inserted"], 1)
        self.assertEqual(queued, "backfillvid")
        start_dispatcher.assert_called_once()

    def test_initialize_endpoint_queues_initial_work_and_starts_dispatcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()

            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/initialize"
            handler.db_path = db_path
            handler.cookie_file = Path(temp_dir) / "cookies.txt"
            handler.video_thumbs = Path(temp_dir) / "video_thumbs"
            handler.config_data = {}
            handler.send_json = Mock()

            with patch.object(
                server.WORKER_QUEUE_DISPATCHER,
                "start",
                return_value={"started": True},
            ) as start_dispatcher:
                handler.do_POST()

            response = handler.send_json.call_args.args[0]
            conn = core.connect(db_path)
            try:
                subjects = {
                    row["subject_key"]
                    for row in conn.execute("SELECT subject_key FROM worker_queue")
                }
            finally:
                conn.close()

        self.assertTrue(response["ok"])
        self.assertFalse(response["queue"]["had_data"])
        self.assertEqual(response["queue"]["inserted"], 4)
        self.assertEqual(
            subjects,
            {"account:sync", "clip:discover", "history:verify", "playlist:scan:LL"},
        )
        start_dispatcher.assert_called_once_with(
            db_path,
            handler.cookie_file,
            handler.video_thumbs,
            handler.config_data,
        )

    def test_rebuild_endpoint_preserves_plugin_work_and_does_not_start_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            try:
                with conn:
                    core.enqueue_account_sync_task(conn, manual=False)
                    now = core.utc_now()
                    conn.execute(
                        """
                        INSERT INTO worker_queue(
                          subject_key, worker_type, task_type, current_title,
                          created_at, updated_at
                        ) VALUES ('plugin:example:fetch:1', 'plugin', 'fetch',
                                  'Plugin task', ?, ?)
                        """,
                        (now, now),
                    )
            finally:
                conn.close()

            handler = object.__new__(server.LibraryHandler)
            handler.db_path = db_path
            handler.plugin_manager = Mock()
            handler._start_worker_queue = Mock()
            handler.send_json = Mock()
            handler.config_data = {}

            with (
                patch.object(server.WORKER_QUEUE_DISPATCHER, "is_running", return_value=False),
                patch.object(server.METADATA_WORKER, "is_running", return_value=False),
                patch.object(server.PLAYLIST_SCAN_WORKER, "is_running", return_value=False),
                patch.object(server.LIVE_HISTORY_WORKER, "is_running", return_value=False),
            ):
                handler._handle_admin_action_post(
                    urllib.parse.urlparse("/api/admin/queue/rebuild"),
                    {},
                )

            response = handler.send_json.call_args.args[0]
            conn = core.connect(db_path)
            try:
                subjects = {
                    row["subject_key"]
                    for row in conn.execute("SELECT subject_key FROM worker_queue")
                }
            finally:
                conn.close()

        self.assertTrue(response["ok"])
        self.assertEqual(response["clearedByType"]["account"], 1)
        self.assertEqual(response["preservedByType"], {"plugin": 1})
        self.assertEqual(response["clips"], 1)
        self.assertEqual(response["discovery"], 1)
        self.assertFalse(response["started"])
        self.assertIn("plugin:example:fetch:1", subjects)
        self.assertIn("clip:discover", subjects)
        self.assertIn("playlist:discover-current", subjects)
        handler.plugin_manager.enqueue_hook.assert_not_called()
        handler._start_worker_queue.assert_not_called()

    def test_fetch_history_also_queues_personal_activity_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()

            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/live-history/start"
            handler.db_path = db_path
            handler.cookie_file = Path(temp_dir) / "cookies.txt"
            handler.video_thumbs = Path(temp_dir) / "video_thumbs"
            handler.config_data = {}
            handler.send_json = Mock()

            with patch.object(
                server.WORKER_QUEUE_DISPATCHER,
                "start",
                return_value={"started": True},
            ) as start_dispatcher:
                handler.do_POST()

            conn = core.connect(db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT subject_key, current_title
                    FROM worker_queue
                    ORDER BY subject_key
                    """
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(
            {(row["subject_key"], row["current_title"]) for row in rows},
            {
                ("account:sync", "Collect personal activity"),
                ("history:recent", "Fetch history"),
            },
        )
        start_dispatcher.assert_called_once_with(
            db_path,
            handler.cookie_file,
            handler.video_thumbs,
            handler.config_data,
        )

    def test_playlist_reconcile_endpoint_records_the_completed_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/playlists/reconcile"
            handler.db_path = db_path
            handler.send_json = Mock()

            handler.do_POST()

            conn = core.connect(db_path)
            try:
                row = conn.execute(
                    """
                    SELECT status, total, processed, found, failed, message
                    FROM playlist_scan_worker_runs
                    """
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(tuple(row[:5]), ("complete", 0, 0, 0, 0))
        self.assertIn("Playlist reconciliation complete", row["message"])
        self.assertTrue(handler.send_json.call_args.args[0]["ok"])

    def test_takeout_import_endpoint_records_success_and_failure_runs(self) -> None:
        success_stats = {
            "inserted_watch_rows": 2,
            "duplicate_watch_rows": 1,
            "total_watch_rows": 3,
            "imported_keys": ["watch-history.json"],
            "reconciled_rows": 2,
            "matched_rows": 1,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            conn = migrated_connection(db_path)
            conn.close()
            handler = object.__new__(server.LibraryHandler)
            handler.path = "/api/admin/history/import-takeout"
            handler.db_path = db_path
            handler.takeout_dir = Path(temp_dir) / "takeout"
            handler.config_data = {}
            handler.send_json = Mock()

            with patch.object(server, "import_history", return_value=success_stats):
                handler.do_POST()
            with patch.object(server, "import_history", side_effect=SystemExit("No Takeout data")):
                handler.do_POST()

            conn = core.connect(db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT status, total, processed, found, failed, skipped, message
                    FROM live_history_worker_runs
                    ORDER BY started_at, run_id
                    """
                ).fetchall()
            finally:
                conn.close()

        by_status = {row["status"]: row for row in rows}
        self.assertEqual(
            tuple(by_status["complete"][:6]),
            ("complete", 3, 3, 2, 0, 1),
        )
        self.assertEqual(by_status["error"]["failed"], 1)
        self.assertEqual(by_status["error"]["message"], "No Takeout data")
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 400)

    def test_service_replacement_uses_dedicated_log_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(server, "ROOT", root),
                patch.object(server.subprocess, "Popen") as popen,
            ):
                server.launch_service_replacement()

            kwargs = popen.call_args.kwargs
            self.assertEqual(
                Path(kwargs["stdout"].name),
                root / ".codex" / "service-logs" / "yt-library.out.log",
            )
            self.assertEqual(
                Path(kwargs["stderr"].name),
                root / ".codex" / "service-logs" / "yt-library.err.log",
            )
            if server.os.name == "nt":
                self.assertEqual(kwargs["creationflags"], server.subprocess.CREATE_NO_WINDOW)
            self.assertTrue(kwargs["stdout"].closed)
            self.assertTrue(kwargs["stderr"].closed)

    def test_service_replacement_delegates_to_windows_service_host(self) -> None:
        with (
            patch.dict(
                server.os.environ,
                {server.WINDOWS_SERVICE_ENVIRONMENT_KEY: "1"},
            ),
            patch.object(server.subprocess, "Popen") as popen,
            self.assertRaises(SystemExit) as raised,
        ):
            server.launch_service_replacement()

        self.assertEqual(raised.exception.code, server.WINDOWS_SERVICE_RESTART_EXIT_CODE)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
