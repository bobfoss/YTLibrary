from __future__ import annotations

from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import test_plugin_packages
from yt_library import plugin_installation as installation, plugin_packages as packages, server
from yt_library.config import load_config


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_plugin_packages.PluginPackageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "plugins").mkdir()
        installation.atomic_json(self.root / "plugins/catalog.json", self.fixture.catalog)
        self.installer = installation.Installer(self.root)
        self.inventory = self.enterContext(patch.object(packages, "installed_inventory", return_value=[]))
        self.environment = self.enterContext(patch.object(packages, "environment_snapshot", return_value=[]))
        self.enterContext(patch.object(self.installer, "supported", return_value=True))
        self.launch = self.enterContext(patch.object(self.installer, "launch"))
        self.payload = {"action": "install", "plugin_id": "example", "version": "1.2.3", "expected_version": ""}

    def prepared(self, action="install"):
        op = {**self.payload, "action": action, "id": "a" * 32, "plugin": self.fixture.plugin,
              "previous_config": None, "stage": "a" * 32 + "/stage", "environment": [], "rollback": None}
        stage = self.installer.directory / op["stage"] / "wheels"
        stage.mkdir(parents=True)
        shutil.copyfile(self.fixture.wheel, stage / self.fixture.wheel.name)
        op["plan"] = {"wheel": self.fixture.wheel.name, "release": self.fixture.release,
                      "config_template": {"database": "example.sqlite3"},
                      "dependency_plan": [{"filename": self.fixture.wheel.name,
                                           "sha256": packages.sha256(self.fixture.wheel)}]}
        self.installer.transition(op, "armed", "ready")
        return op

    def test_begin_rejects_overlap_and_stale_selection(self):
        self.installer.begin(self.payload)
        self.launch.assert_called_once()
        with self.assertRaisesRegex(packages.PackageError, "active"):
            self.installer.begin(self.payload)
        op = self.installer.operation()
        self.installer.transition(op, "failed", "test")
        with self.assertRaisesRegex(packages.PackageError, "version changed"):
            self.installer.begin({**self.payload, "expected_version": "9"})

    def test_development_update_remove_protected_enable_allowed(self):
        self.inventory.return_value = [{"id": "example", "distribution": "yt-example", "version": "1.0", "protected": True}]
        for action in ("remove", "update"):
            with self.assertRaisesRegex(packages.PackageError, "cannot be replaced"):
                self.installer.begin({**self.payload, "action": action, "expected_version": "1.0"})
        self.installer.begin({**self.payload, "action": "disable", "expected_version": "1.0"})

    def test_begin_rejects_extra_fields_and_unknown_actions(self):
        for payload in ({**self.payload, "url": "https://evil"}, {**self.payload, "action": "pip"}):
            with self.assertRaises(ValueError):
                self.installer.begin(payload)
        self.launch.assert_not_called()

    def test_development_reason_is_not_overwritten_by_missing_rollback(self):
        self.inventory.return_value = [{"id": "example", "distribution": "yt-example", "version": "1.0", "protected": True}]
        plugin = self.installer.view({"plugins": {}}, [])["plugins"][0]
        self.assertIn("Development", plugin["reason"])
        self.assertFalse(plugin["can_update"])
        self.assertFalse(plugin["can_remove"])

    def test_bootstrap_installs_offline_disabled_and_does_not_replay(self):
        self.prepared()
        with patch.object(self.installer, "pip") as pip:
            self.installer.bootstrap()
            self.installer.bootstrap()
        pip.assert_called_once()
        self.assertIn("--require-hashes", pip.call_args.args[0])
        self.assertEqual(self.installer.operation()["state"], "verifying")
        config = load_config(self.root / "yt_library.config.json")
        self.assertFalse(config["plugins"]["example"]["enabled"])
        path = Path(config["plugins"]["example"]["config"])
        self.assertTrue(path.is_relative_to(self.root / "plugin-data"))
        self.assertEqual(packages.read_json(path), {"database": "example.sqlite3"})

    def test_tampering_or_stale_environment_prevents_install(self):
        op = self.prepared()
        wheel = self.installer.directory / op["stage"] / "wheels" / self.fixture.wheel.name
        wheel.write_bytes(b"bad")
        with patch.object(self.installer, "pip") as pip:
            self.installer.bootstrap()
        pip.assert_not_called()
        self.assertEqual(self.installer.operation()["state"], "failed")

    def test_interrupted_apply_disables_without_reexecuting_pip(self):
        op = self.prepared()
        self.installer.transition(op, "applying", "interrupted")
        with patch.object(self.installer, "pip") as pip:
            self.installer.bootstrap()
        pip.assert_not_called()
        self.assertEqual(self.installer.operation()["state"], "failed")
        self.assertFalse(load_config(self.root / "yt_library.config.json")["plugins"]["example"]["enabled"])

    def test_failed_install_removes_new_code_but_preserves_config(self):
        self.prepared()
        with patch.object(self.installer, "pip", side_effect=[ValueError("failed"), None]) as pip:
            self.installer.bootstrap()
        self.assertEqual(pip.call_count, 2)
        self.assertEqual(pip.call_args.args[0], ["uninstall", "--yes", "yt-example"])
        self.assertTrue((self.root / "plugin-data/example/config.json").exists())

    def test_update_failure_rolls_back_prior_wheel_before_activation(self):
        op = self.prepared("update")
        op.update(expected_version="1.2.3", rollback=self.fixture.release,
                  previous_config={"config": str(self.root / "custom/config.json"), "enabled": True})
        shutil.copyfile(self.fixture.wheel, self.installer.directory / op["stage"] / self.fixture.wheel.name)
        self.inventory.return_value = [{"id": "example", "distribution": "yt-example", "version": "1.2.3", "protected": False}]
        self.installer.transition(op, "armed", "ready")
        with patch.object(self.installer, "pip", side_effect=[ValueError("failed"), None]) as pip:
            self.installer.bootstrap()
        self.assertIn("--force-reinstall", pip.call_args.args[0])
        self.assertIn("Prior package restored", self.installer.operation()["message"])
        self.assertTrue(load_config(self.root / "yt_library.config.json")["plugins"]["example"]["enabled"])

    def test_remove_preserves_owned_database_and_config(self):
        op = self.prepared("remove")
        op["expected_version"] = "1.2.3"
        own_config = self.root / "custom/config.json"
        installation.atomic_json(own_config, {"database": "untouched.sqlite3"})
        database = own_config.parent / "untouched.sqlite3"
        database.write_bytes(b"owned by plugin")
        op["previous_config"] = {"config": str(own_config), "enabled": True}
        self.inventory.return_value = [{"id": "example", "distribution": "yt-example", "version": "1.2.3", "protected": False}]
        self.installer.transition(op, "armed", "ready")
        with patch.object(self.installer, "pip") as pip:
            self.installer.bootstrap()
        pip.assert_called_once_with(["uninstall", "--yes", "yt-example"])
        self.assertEqual(database.read_bytes(), b"owned by plugin")
        self.assertEqual(packages.read_json(own_config), {"database": "untouched.sqlite3"})

    def test_catalog_merge_keeps_bundled_releases_and_rejects_republished_version(self):
        remote = deepcopy(self.fixture.catalog)
        remote["plugins"][0]["releases"] = []
        installation.atomic_json(self.installer.directory / "catalog.json", remote)
        self.assertEqual(len(self.installer.catalog()["plugins"][0]["releases"]), 1)
        remote = deepcopy(self.fixture.catalog)
        remote["plugins"][0]["releases"][0]["wheel"]["sha256"] = "f" * 64
        installation.atomic_json(self.installer.directory / "catalog.json", remote)
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.installer.catalog()

    def test_public_progress_does_not_expose_config_or_dependency_inventory(self):
        self.prepared()
        view = self.installer.view({"plugins": {}}, [])
        self.assertNotIn("previous_config", view["operation"])
        self.assertNotIn("plan", view["operation"])
        self.assertNotIn("environment", view["operation"])

    def handler(self, payload):
        handler = object.__new__(server.LibraryHandler)
        handler.plugin_installer = self.installer
        handler.config_data = {}
        handler.send_json = Mock()
        body = json.dumps(payload).encode()
        handler.rfile = io.BytesIO(body)
        handler.headers = {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765",
                           "Content-Type": "application/json", "Content-Length": str(len(body)),
                           "X-YT-Library-Admin": "1", "X-YT-Library-Token": self.installer.token}
        return handler

    def test_api_requires_same_origin_nonce_and_bounded_body(self):
        for key, value in (("Origin", "http://evil.test"), ("X-YT-Library-Token", "bad"),
                           ("Sec-Fetch-Site", "cross-site"), ("Content-Length", "99999")):
            handler = self.handler(self.payload)
            handler.headers[key] = value
            handler._handle_package_post()
            self.assertIn(handler.send_json.call_args.kwargs["status"], (403, 409))
        self.launch.assert_not_called()
        handler = self.handler(self.payload)
        handler._handle_package_post()
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 202)

    def test_runtime_package_files_are_not_statically_served(self):
        handler = self.handler({})
        for path in ("/.plugin-manager/operation.json", "/plugin-data/example/config.json", "/a/../.plugin-manager/operation.json"):
            self.assertIn(".not-a-served-plugin-resource", handler.translate_path(path))


if __name__ == "__main__":
    unittest.main()
