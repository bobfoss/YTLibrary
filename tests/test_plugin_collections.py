from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from yt_library.plugins import PluginManager, _browser_collection
from yt_library.server import LibraryHandler


class BrowserCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = PluginManager({"plugins": {}}, entry_points=[])
        self.plugin = SimpleNamespace(browser_collection={"label": "Example records"}, status=lambda: {})
        self.record = SimpleNamespace(plugin_id="example", configured={"enabled": True},
                                      instance=self.plugin, state="loaded", message="")
        self.manager._records["example"] = self.record

    def test_collection_metadata_and_routes_require_enabled_loaded_plugin(self) -> None:
        self.assertEqual(self.manager.statuses()[0]["browserCollection"],
                         {"path": "/example", "label": "Example records"})
        self.assertTrue(self.manager.has_browser_collection("/example"))
        self.assertTrue(self.manager.has_browser_collection("/example/"))
        for path in ("/missing", "/example/details", "/api", "/../example"):
            self.assertFalse(self.manager.has_browser_collection(path))
        self.record.configured["enabled"] = False
        self.assertFalse(self.manager.has_browser_collection("/example"))
        self.record.configured["enabled"] = True
        self.record.state = "error"
        self.assertFalse(self.manager.has_browser_collection("/example"))

    def test_collection_cannot_claim_host_routes_or_omit_label(self) -> None:
        for plugin_id in ("videos", "api", "admin", "search", "history"):
            with self.assertRaises(ValueError):
                _browser_collection(plugin_id, self.plugin)
        self.plugin.browser_collection = {}
        with self.assertRaises(ValueError):
            _browser_collection("example", self.plugin)
        self.assertIsNone(_browser_collection("example", object()))

    def test_direct_collection_page_load_uses_shell_and_disabled_route_is_absent(self) -> None:
        handler = object.__new__(LibraryHandler)
        handler.plugin_manager = self.manager
        handler.render_page = Mock(return_value=b"shell")
        handler._send_bytes = Mock()
        self.assertTrue(handler._handle_page_get("/example"))
        handler._send_bytes.assert_called_once_with(b"shell", "text/html; charset=utf-8", cache_control="no-store")
        self.record.configured["enabled"] = False
        self.assertFalse(handler._handle_page_get("/example"))
