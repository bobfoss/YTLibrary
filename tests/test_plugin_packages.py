from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.request
import zipfile

from yt_library import plugin_packages as packages


class PluginPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.wheel = self.root / "yt_example-1.2.3-py3-none-any.whl"
        self.manifest = {
            "schema_version": 1, "id": "example", "plugin_api_version": 2,
            "browser_api_version": None, "required_host_features": [], "config_template": {},
        }
        self.files = {
            "yt_example/plugin.py": b"raise RuntimeError('Inspection must never import code')\n",
            "yt_example/ytl-plugin.json": json.dumps(self.manifest).encode(),
            "yt_example-1.2.3.dist-info/METADATA": (
                b"Metadata-Version: 2.4\nName: yt-example\nVersion: 1.2.3\n"
                b"License-Expression: GPL-3.0-or-later\nRequires-Python: >=3.12\n"
                b"Requires-Dist: example-dependency>=1\n\n"
            ),
            "yt_example-1.2.3.dist-info/entry_points.txt": b"[yt_library.plugins]\nexample = yt_example.plugin:Plugin\n",
            "yt_example-1.2.3.dist-info/licenses/LICENSE": b"Test fixture: END OF TERMS AND CONDITIONS\n",
        }
        self.write_wheel()
        self.release = {
            "version": "1.2.3", "tag": "v1.2.3", "commit": "a" * 40,
            "release_url": "https://github.com/example/yt-example/releases/tag/v1.2.3",
            "license": "GPL-3.0-or-later", "requires_python": ">=3.12",
            "plugin_api_version": 2, "browser_api_version": None, "required_host_features": [],
            "dependencies": ["example-dependency>=1"],
            "wheel": self.artifact(self.wheel.name, self.wheel.stat().st_size, packages.sha256(self.wheel)),
            "source": self.artifact("yt_example-1.2.3.tar.gz", 100, "b" * 64),
        }
        self.plugin = {"id": "example", "name": "Example", "description": "Generic fixture",
                       "distribution": "yt-example", "repository_url": "https://github.com/example/yt-example",
                       "releases": [self.release]}
        self.catalog = {"schema_version": 1, "plugins": [self.plugin]}

    def artifact(self, filename, size, sha256):
        return {"filename": filename, "size": size, "sha256": sha256,
                "url": "https://github.com/example/yt-example/releases/download/v1.2.3/" + filename}

    def write_wheel(self):
        with zipfile.ZipFile(self.wheel, "w") as archive:
            for name, content in self.files.items():
                info = zipfile.ZipInfo()
                info.filename = name
                archive.writestr(info, content)

    def test_bundled_catalog_has_no_implicit_releases(self):
        catalog = packages.validate_catalog(packages.read_json(packages.CATALOG))
        self.assertTrue(catalog["plugins"])
        self.assertTrue(all(isinstance(p["releases"], list) for p in catalog["plugins"]))

    def test_catalog_and_wheel_agree_without_importing_plugin(self):
        packages.validate_catalog(self.catalog)
        details = packages.verify_release_wheel(self.wheel, self.plugin, self.release)
        self.assertEqual(details["entry_point"], "yt_example.plugin:Plugin")
        self.assertEqual(details["config_template"], {})
        self.assertEqual(details["dependencies"], ["example-dependency>=1"])

    def test_catalog_rejects_invalid_versions_urls_hashes_and_identities(self):
        cases = [
            ("version", "1.2.3rc1"), ("version", "1.2.3+local"), ("tag", "main"),
            ("commit", "main"), ("release_url", "https://example.net"),
            ("license", "GPL-3.0-only"), ("requires_python", ""),
            ("plugin_api_version", True), ("browser_api_version", 0),
            ("required_host_features", ["z", "a"]),
            ("dependencies", ["example @ https://example.net/setup.tar.gz"]),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                catalog = copy.deepcopy(self.catalog)
                catalog["plugins"][0]["releases"][0][field] = value
                with self.assertRaises(ValueError):
                    packages.validate_catalog(catalog)
        for field, value in [("filename", "../a.whl"), ("sha256", "a"), ("size", True),
                             ("size", packages.MAX_WHEEL_BYTES + 1), ("url", "http://localhost/a.whl")]:
            with self.subTest(field=field):
                catalog = copy.deepcopy(self.catalog)
                catalog["plugins"][0]["releases"][0]["wheel"][field] = value
                with self.assertRaises(ValueError):
                    packages.validate_catalog(catalog)

    def test_duplicate_catalog_identity_or_version_rejected(self):
        self.catalog["plugins"].append(copy.deepcopy(self.plugin))
        with self.assertRaises(packages.PackageError):
            packages.validate_catalog(self.catalog)
        self.catalog["plugins"].pop()
        self.plugin["releases"].append(copy.deepcopy(self.release))
        with self.assertRaises(packages.PackageError):
            packages.validate_catalog(self.catalog)

    def test_metadata_mismatch_and_missing_license_rejected(self):
        path = "yt_example-1.2.3.dist-info/METADATA"
        self.files[path] = self.files[path].replace(b"Name: yt-example", b"Name: unrelated")
        self.write_wheel()
        with self.assertRaisesRegex(packages.PackageError, "disagree"):
            packages.inspect_wheel(self.wheel)
        self.files[path] = self.files[path].replace(b"Name: unrelated", b"Name: yt-example")
        del self.files["yt_example-1.2.3.dist-info/licenses/LICENSE"]
        self.write_wheel()
        with self.assertRaisesRegex(packages.PackageError, "license"):
            packages.inspect_wheel(self.wheel)

    def test_wheel_rejects_path_traversal_startup_hooks_and_data_installs(self):
        for name in ("../evil.py", "/evil.py", "foo\\evil.py", "C:/evil.py", "startup.pth", "yt_example.data/scripts/run"):
            with self.subTest(name=name):
                self.files[name] = b"bad"
                self.write_wheel()
                with self.assertRaisesRegex(packages.PackageError, "unsafe"):
                    packages.inspect_wheel(self.wheel)
                del self.files[name]

    def test_wheel_rejects_duplicate_entries(self):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.wheel, "a") as archive:
                archive.writestr("yt_example/plugin.py", b"duplicate")
        with self.assertRaisesRegex(packages.PackageError, "Duplicate"):
            packages.inspect_wheel(self.wheel)

    def test_tampered_wheel_and_false_catalog_claims_rejected(self):
        release = copy.deepcopy(self.release)
        release["wheel"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(packages.PackageError, "SHA-256"):
            packages.verify_release_wheel(self.wheel, self.plugin, release)
        release = copy.deepcopy(self.release)
        release["required_host_features"] = ["invented"]
        with self.assertRaisesRegex(packages.PackageError, "required_host_features"):
            packages.verify_release_wheel(self.wheel, self.plugin, release)

    def test_compatibility_reports_each_mismatch(self):
        release = {**self.release, "requires_python": ">=4", "plugin_api_version": 3,
                   "browser_api_version": 3, "required_host_features": ["missing"]}
        errors = packages.compatibility_errors(release, plugin_api=2, browser_api=2,
                                                features=frozenset(), python_version="3.12.0")
        self.assertEqual(len(errors), 4)
        self.assertEqual(packages.compatibility_errors(self.release, plugin_api=2, browser_api=2,
                                                       features=frozenset(), python_version="3.12.0"), [])

    def test_inventory_marks_editable_and_unreadable_origins_protected(self):
        entry = SimpleNamespace(name="example", group=packages.ENTRY_GROUP, load=Mock(side_effect=AssertionError))
        for direct, mode in [(None, "package"), ("broken", "unknown"),
                             (json.dumps({"dir_info": {"editable": True}, "url": "file:///C:/Source%20Code"}), "development")]:
            with self.subTest(mode=mode):
                dist = SimpleNamespace(metadata={"Name": "yt_example"}, version="1.2.3", entry_points=[entry],
                                       read_text=lambda name: direct)
                record, = packages.installed_inventory([dist])
                self.assertEqual(record["mode"], mode)
                self.assertEqual(record["protected"], mode != "package")
                entry.load.assert_not_called()

    def test_download_redirects_cannot_escape_release_hosts(self):
        handler = packages._ReleaseRedirects()
        request = urllib.request.Request(self.release["wheel"]["url"])
        for url in ("http://github.com/a", "https://localhost/a", "https://github.com.evil.test/a", "https://user@github.com/a"):
            with self.subTest(url=url), self.assertRaises(packages.PackageError):
                handler.redirect_request(request, None, 302, "Found", {}, url)

    def test_proxy_workaround_is_narrow_and_never_disables_tls(self):
        with patch.object(packages.metadata, "version", return_value="26.2"):
            arguments = packages.pip_network_arguments("socks5h://localhost:1080")
            self.assertIn("--use-deprecated=legacy-certs", arguments)
            self.assertNotIn("--trusted-host", arguments)
            self.assertEqual(packages.pip_network_arguments("http://localhost:8080"), ["--proxy", "http://localhost:8080"])
        with patch.object(packages.metadata, "version", return_value="26.1"):
            self.assertEqual(packages.pip_network_arguments("socks5h://localhost:1080"), ["--proxy", "socks5h://localhost:1080"])

    def test_resolution_keeps_other_plugins_extras_and_excludes_old_target(self):
        entry = SimpleNamespace(group=packages.ENTRY_GROUP)
        other = SimpleNamespace(metadata={"Name": "other"}, entry_points=[entry], requires=["example[extra]>=2"])
        target = SimpleNamespace(metadata={"Name": "yt-example"}, entry_points=[entry], requires=["example<2"])
        with patch.object(packages.metadata, "distributions", return_value=[other, target]):
            self.assertEqual(packages.installed_plugin_requirements("yt-example"), ["example[extra]>=2"])

    def test_preparation_refuses_protected_installs_before_download(self):
        record = {"id": "example", "distribution": "yt-example", "version": "1.0", "protected": True}
        with patch.object(packages, "installed_inventory", return_value=[record]), patch.object(packages, "download_wheel") as download:
            with self.assertRaisesRegex(packages.PackageError, "protected"):
                packages.prepare(self.catalog, "example", "1.2.3", self.root / "prepared")
            download.assert_not_called()

    def test_preparation_rejects_unknown_selection_and_existing_output(self):
        with self.assertRaisesRegex(packages.PackageError, "not approved"):
            packages.prepare(self.catalog, "example", "9.9", self.root / "prepared")
        with patch.object(packages, "installed_inventory", return_value=[]):
            with self.assertRaisesRegex(packages.PackageError, "already exists"):
                packages.prepare(self.catalog, "example", "1.2.3", self.root)

    def resolve_mock(self, command, **kwargs):
        self.assertIn("--dry-run", command)
        self.assertIn("--only-binary=:all:", command)
        constraints = Path(command[command.index("--constraint") + 1]).read_text()
        self.assertEqual(constraints, "example-dependency==1.0\n")
        wheel = Path(command[-1])
        packages.write_json(Path(command[command.index("--report") + 1]), {
            "version": "1", "install": [{"metadata": {"name": "yt-example", "version": "1.2.3"},
                "download_info": {"url": wheel.as_uri(), "archive_info": {"hashes": {"sha256": packages.sha256(wheel)}}}}],
        })
        return SimpleNamespace(returncode=0)

    def test_preparation_is_staging_only_and_pins_existing_environment(self):
        snapshot = [{"name": "example-dependency", "version": "1.0", "origin_sha256": ""}]
        with patch.object(packages, "installed_inventory", return_value=[]), patch.object(packages, "environment_snapshot", return_value=snapshot), patch.object(packages.subprocess, "run", side_effect=self.resolve_mock):
            destination = self.root / "prepared"
            result = packages.prepare(self.catalog, "example", "1.2.3", destination, local_wheel=self.wheel)
        self.assertEqual(result["state"], "prepared_not_installed")
        self.assertEqual(result["environment"], snapshot)
        self.assertEqual(packages.read_json(destination / "plan.json"), result)
        self.assertEqual(sorted(p.name for p in destination.iterdir()), ["plan.json", self.wheel.name])

    def test_failed_resolution_leaves_no_ready_plan_or_environment_changes(self):
        with patch.object(packages, "installed_inventory", return_value=[]), patch.object(packages.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(packages.PackageError, "resolution failed"):
                packages.prepare(self.catalog, "example", "1.2.3", self.root / "prepared", local_wheel=self.wheel)
        self.assertFalse((self.root / "prepared").exists())
        self.assertFalse(list(self.root.glob(".ytl-prepare-*")))

    def test_stale_environment_during_preparation_rejected(self):
        before = [{"name": "example-dependency", "version": "1.0", "origin_sha256": ""}]
        after = [{**before[0], "version": "2.0"}]
        with patch.object(packages, "installed_inventory", return_value=[]), patch.object(packages, "environment_snapshot", side_effect=[before, after]), patch.object(packages.subprocess, "run", side_effect=self.resolve_mock):
            with self.assertRaisesRegex(packages.PackageError, "Environment changed"):
                packages.prepare(self.catalog, "example", "1.2.3", self.root / "prepared", local_wheel=self.wheel)
        self.assertFalse((self.root / "prepared").exists())

    def test_real_pip_preflight_is_offline_and_does_not_install(self):
        meta_path = "yt_example-1.2.3.dist-info/METADATA"
        self.files[meta_path] = self.files[meta_path].replace(b"Requires-Dist: example-dependency>=1\n", b"")
        self.files["yt_example-1.2.3.dist-info/WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        self.write_wheel()
        self.release["dependencies"] = []
        self.release["wheel"] = self.artifact(self.wheel.name, self.wheel.stat().st_size, packages.sha256(self.wheel))
        (self.root / "requirements.txt").write_text("", encoding="utf-8")
        before = packages.environment_snapshot()
        real_run = subprocess.run

        def offline_run(command, **kwargs):
            self.assertIn("--dry-run", command)
            return real_run(command + ["--no-index"], **kwargs)

        with patch.object(packages, "ROOT", self.root), patch.object(packages, "installed_plugin_requirements", return_value=[]), patch.object(packages.subprocess, "run", side_effect=offline_run):
            result = packages.prepare(self.catalog, "example", "1.2.3", self.root / "prepared", local_wheel=self.wheel)
        self.assertEqual(result["state"], "prepared_not_installed")
        self.assertEqual(packages.environment_snapshot(), before)
        self.assertEqual(result["dependency_plan"], [{"distribution": "yt-example", "version": "1.2.3", "sha256": packages.sha256(self.wheel)}])

    def test_real_wheel_smoke_accepts_optional_shutdown_and_external_config(self):
        self.files["yt_example/__init__.py"] = b""
        self.files["yt_example/plugin.py"] = b'''class Plugin:
    plugin_id = "example"
    plugin_version = "1.2.3"
    plugin_api_version = 2

    def start(self, context):
        import json
        self.path = context.resolve_path(context.plugin_config["config"])
        self.config = json.loads(self.path.read_text())

    def status(self):
        return {"state": "ready"}
'''
        self.files["yt_example-1.2.3.dist-info/WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        self.files["yt_example-1.2.3.dist-info/RECORD"] = b""
        self.write_wheel()
        result = subprocess.run([sys.executable, str(packages.ROOT / "scripts" / "smoke_plugin_wheel.py"), str(self.wheel)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"fresh_start": "ok"', result.stdout)


if __name__ == "__main__":
    unittest.main()
