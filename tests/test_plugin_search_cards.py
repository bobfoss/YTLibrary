from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from yt_library import core, server
from yt_library.plugins import PluginManager
from yt_library.queries import omni_search_data


class UnifiedSearchCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "library.sqlite3"
        core.migrate_database(path)
        self.conn = core.connect(path)
        self.addCleanup(self.conn.close)
        for video_id, title, date in (
            ("abcdefghijk", "Needle Alpha", "2024-01-01T00:00:00Z"),
            ("ABCDEFGHIJK", "Needle Beta", "2026-01-01T00:00:00Z"),
        ):
            core.upsert_video(self.conn, video_id, title=title, source="metadata")
            self.conn.execute("INSERT INTO history_events(event_id,video_id,watched_at,watch_date,time_precision) VALUES(?,?,?,?,'exact')",
                              (video_id, video_id, date, date[:10]))
        self.descriptors = [
            {"id": "early", "pluginId": "example", "video_id": "abcdefghijk", "title": "Needle Alpha",
             "oldest_at": "2020-01-01T00:00:00Z", "newest_at": "2025-01-01T00:00:00Z", "like_count": 0},
            {"id": "late", "pluginId": "example", "video_id": "ABCDEFGHIJK", "title": "Needle Beta",
             "oldest_at": "2023-01-01T00:00:00Z", "newest_at": "2027-01-01T00:00:00Z", "like_count": 5},
        ]

    def search(self, **kwargs):
        return omni_search_data(self.conn, kwargs.pop("query", "needle"), result_kinds={"video"},
                                plugin_result_descriptors=self.descriptors, **kwargs)

    def test_native_and_plugin_cards_sort_and_paginate_together(self):
        expected = {
            "newest": ["late", "ABCDEFGHIJK", "early", "abcdefghijk"],
            "oldest": ["early", "late", "abcdefghijk", "ABCDEFGHIJK"],
            "most_liked": ["late", "early", "abcdefghijk", "ABCDEFGHIJK"],
        }
        for sort, ids in expected.items():
            with self.subTest(sort=sort):
                pages = [self.search(sort=sort, limit=1, offset=offset) for offset in range(4)]
                rows = [page["results"][0] for page in pages]
                self.assertEqual([row.get("id") or row["item"]["video_id"] for row in rows], ids)
                self.assertEqual([page["total"] for page in pages], [4] * 4)
                self.assertTrue(all(page["counts"]["plugins"] == {"example": 2} for page in pages))
                self.assertTrue(all(page["counts"]["videos"] == 2 for page in pages))

    def test_blank_search_and_video_filters_do_not_leak_plugin_cards(self):
        blank = self.search(query="  ", sort="newest")
        self.assertEqual(blank["total"], 2)
        self.assertTrue(all(row["kind"] == "video" for row in blank["results"]))
        filtered = self.search(video_id_exclusion_filters=[{"abcdefghijk"}])
        self.assertEqual(filtered["total"], 2)
        self.assertEqual(filtered["counts"]["plugins"], {"example": 1})
        self.assertEqual({row["item"]["video_id"] for row in filtered["results"]}, {"ABCDEFGHIJK"})

    def test_plugin_only_text_match_enters_the_native_video_filter_pipeline(self):
        payload = self.search(query="a phrase only the plugin indexed", search_fields=set())
        self.assertEqual(payload["total"], 4)
        self.assertEqual(payload["counts"]["plugins"], {"example": 2})
        self.assertEqual(payload["counts"]["videos"], 2)

    def test_filtered_later_page_still_uses_full_result_membership(self):
        for index in range(5):
            self.descriptors.append(dict(self.descriptors[0], id=f"extra-{index}"))
        page = self.search(sort="newest", limit=2, offset=6)
        self.assertEqual(page["total"], 9)
        self.assertEqual(page["counts"]["plugins"], {"example": 7})
        self.assertEqual(len(page["results"]), 2)


class PluginSearchBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = Mock()
        self.manager = object.__new__(PluginManager)
        self.manager._records = {"example": SimpleNamespace(instance=self.plugin)}
        self.descriptor = {"id": "one", "video_id": "abcdefghijk", "title": "Title",
                           "newest_at": "2026-01-01T02:00:00+02:00", "oldest_at": None, "like_count": 0}

    def test_descriptors_are_bounded_validated_and_blank_queries_do_not_call_plugin(self):
        self.assertEqual(self.manager.search_result_descriptors("example", "  "), [])
        self.plugin.search_result_descriptors.assert_not_called()
        self.plugin.search_result_descriptors.return_value = [self.descriptor]
        rows = self.manager.search_result_descriptors("example", "match")
        self.assertEqual(rows[0]["newest_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(rows[0]["pluginId"], "example")
        for change in ({"video_id": "bad"}, {"newest_at": "2026-01-01"}, {"like_count": -1}, {"id": ""}):
            with self.subTest(change=change):
                self.plugin.search_result_descriptors.return_value = [dict(self.descriptor, **change)]
                with self.assertRaises(ValueError):
                    self.manager.search_result_descriptors("example", "match")
        self.plugin.search_result_descriptors.return_value = [self.descriptor, self.descriptor]
        with self.assertRaises(ValueError):
            self.manager.search_result_descriptors("example", "match")

    def test_hydration_only_accepts_requested_results(self):
        self.plugin.hydrate_search_results.return_value = {"one": {"text": "Saved"}}
        self.assertEqual(self.manager.hydrate_search_results("example", ["one"], "match"), {"one": {"text": "Saved"}})
        self.plugin.hydrate_search_results.return_value = {"unrequested": {}}
        with self.assertRaises(ValueError):
            self.manager.hydrate_search_results("example", ["one"], "match")

    def test_server_requests_only_selected_plugins_and_hydrates_only_the_page(self):
        manager = Mock()
        manager.search_result_descriptors.return_value = [self.descriptor]
        data = server.plugin_search_card_query_data(manager, {"result_plugin": ["example", "example"]}, "match")
        manager.search_result_descriptors.assert_called_once_with("example", "match")
        self.assertEqual(data["descriptors"], [self.descriptor])
        manager.hydrate_search_results.return_value = {"one": {"text": "Saved"}}
        page = {"results": [{"kind": "plugin", "pluginId": "example", "id": "one", "item": {}}]}
        server.hydrate_plugin_search_cards(manager, page, "match", [])
        manager.hydrate_search_results.assert_called_once_with("example", ["one"], "match")
        self.assertEqual(page["results"][0]["item"], {"text": "Saved"})
        self.assertEqual(server.plugin_search_card_query_data(manager, {"result_plugin": ["example"]}, " "),
                         {"descriptors": [], "errors": []})

    def test_plugin_failures_leave_native_results_available(self):
        manager = Mock()
        manager.search_result_descriptors.side_effect = RuntimeError("offline")
        data = server.plugin_search_card_query_data(manager, {"result_plugin": ["example"]}, "match")
        self.assertEqual(data["descriptors"], [])
        self.assertEqual(data["errors"][0]["message"], "offline")
        manager.hydrate_search_results.side_effect = RuntimeError("unavailable")
        page = {"results": [{"kind": "video", "item": {}},
                            {"kind": "plugin", "pluginId": "example", "id": "one", "item": {}}]}
        server.hydrate_plugin_search_cards(manager, page, "match", [])
        self.assertNotIn("error", page["results"][0])
        self.assertEqual(page["results"][1]["error"], "unavailable")


if __name__ == "__main__":
    unittest.main()
