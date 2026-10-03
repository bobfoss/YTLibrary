"""Durable, controller-owned plugin maintenance; never touches plugin databases."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any
import urllib.request
import uuid

from packaging.version import Version

from . import plugin_packages as packages
from .config import configured_proxy, load_config, save_config

ROOT = packages.ROOT
CATALOG_URL = "https://raw.githubusercontent.com/bobfoss/YTLibrary/main/plugins/catalog.json"
TERMINAL = {"succeeded", "failed"}
ACTIONS = {"install", "update", "remove", "enable", "disable"}


def maintenance_pending(config: dict[str, Any]) -> bool:
    path = config.get("_config_path")
    return bool(path and Installer(Path(str(path)).resolve().parent).maintenance())


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Installer:
    def __init__(self, root: Path = ROOT) -> None:
        self.root = root.resolve()
        self.directory = self.root / ".plugin-manager"
        self.operation_path = self.directory / "operation.json"
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.running_config_matches_controller = True

    def operation(self) -> dict[str, Any] | None:
        return packages.read_json(self.operation_path) if self.operation_path.exists() else None

    def busy(self) -> bool:
        op = self.operation()
        return bool(op and op["state"] not in TERMINAL)

    def maintenance(self) -> bool:
        op = self.operation()
        return bool(op and op["state"] in {"prepared", "armed", "applying"})

    def transition(self, op: dict[str, Any], state: str, message: str, **values: Any) -> None:
        op.update(values, state=state, message=message,
                  updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        atomic_json(self.operation_path, op)
        atomic_json(self.directory / op["id"] / "operation.json", op)

    def catalog(self, candidate: dict[str, Any] | None = None) -> dict[str, Any]:
        bundled = packages.validate_catalog(packages.read_json(self.root / "plugins/catalog.json"))
        cache = self.directory / "catalog.json"
        if not cache.exists() and candidate is None:
            return bundled
        cached = packages.validate_catalog(candidate if candidate is not None else packages.read_json(cache))
        by_id = {p["id"]: p for p in bundled["plugins"]}
        for plugin in cached["plugins"]:
            prior = by_id.get(plugin["id"])
            if prior:
                if (plugin["distribution"], plugin["repository_url"]) != (prior["distribution"], prior["repository_url"]):
                    raise packages.PackageError("Catalog changed a plugin's identity")
                releases = {r["version"]: r for r in prior["releases"]}
                for release in plugin["releases"]:
                    if release["version"] in releases and release != releases[release["version"]]:
                        raise packages.PackageError("Catalog changed an immutable release")
                    releases[release["version"]] = release
                plugin = {**plugin, "releases": list(releases.values())}
            by_id[plugin["id"]] = plugin
        return packages.validate_catalog({"schema_version": 1, "plugins": list(by_id.values())})

    def refresh(self, config: dict[str, Any]) -> None:
        from .network import socks5_proxy_handlers

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise packages.PackageError("Unexpected catalog redirect")

        opener = urllib.request.build_opener(NoRedirect(), *socks5_proxy_handlers(configured_proxy(config)))
        try:
            with opener.open(CATALOG_URL, timeout=30) as response:
                content = response.read(packages.MAX_JSON_BYTES + 1)
            if len(content) > packages.MAX_JSON_BYTES:
                raise packages.PackageError("Catalog exceeds size limit")
            candidate = packages.validate_catalog(json.loads(content))
        except Exception as exc:
            raise packages.PackageError("Catalog refresh failed; the current catalog is unchanged") from exc
        with self.lock:
            if self.busy():
                raise packages.PackageError("Wait for active maintenance before refreshing the catalog")
            merged = self.catalog(candidate)
            atomic_json(self.directory / "catalog.json", merged)

    def supported(self) -> bool:
        return (os.name == "nt" and self.running_config_matches_controller
                and Path(sys.prefix).resolve() == (self.root / ".venv").resolve())

    def view(self, config: dict[str, Any], statuses: list[dict[str, Any]]) -> dict[str, Any]:
        from .plugins import PLUGIN_API_VERSION, PLUGIN_HOST_FEATURES

        catalog = self.catalog()
        inventory = packages.installed_inventory()
        plugins = []
        for plugin in catalog["plugins"]:
            matches = [p for p in inventory if p["id"] == plugin["id"] or p["distribution"] == plugin["distribution"]]
            installed = matches[0] if len(matches) == 1 else None
            releases = sorted(plugin["releases"], key=lambda r: Version(r["version"]), reverse=True)
            compatible = [r for r in releases if not packages.compatibility_errors(
                r, plugin_api=PLUGIN_API_VERSION, browser_api=2, features=PLUGIN_HOST_FEATURES)]
            latest = compatible[0] if compatible else None
            reason = ""
            if not self.supported():
                reason = "Automatic maintenance requires Windows, this checkout's .venv, and its standard configured service. Other launches use manual pip installation."
            elif len(matches) > 1:
                reason = "Conflicting installed identities; resolve manually."
            elif installed and installed["protected"]:
                reason = "Development/unknown-origin install: Update and Remove are protected. Manage code in its source checkout."
            elif not latest:
                reason = "; ".join(packages.compatibility_errors(releases[0], plugin_api=PLUGIN_API_VERSION,
                    browser_api=2, features=PLUGIN_HOST_FEATURES)) if releases else "No published package yet."
            plugin_config = config.get("plugins", {}).get(plugin["id"], {})
            can_update = bool(installed and latest and Version(latest["version"]) > Version(installed["version"]))
            if can_update and not reason and not any(r["version"] == installed["version"] for r in releases):
                reason = "Installed version has no approved rollback wheel; update manually."
            plugins.append({**plugin, "releases": releases, "installed": installed,
                            "latest": latest, "enabled": plugin_config.get("enabled") is True,
                            "config_path": plugin_config.get("config", ""), "reason": reason,
                            "can_install": bool(not matches and latest and not reason),
                            "can_update": bool(can_update and not reason),
                            "can_remove": bool(installed and not installed["protected"] and self.supported()),
                            "can_toggle": bool(installed and len(matches) == 1 and self.supported()),
                            "runtime": next((s for s in statuses if s["id"] == plugin["id"]), None)})
        operation = self.operation()
        public = {key: operation[key] for key in ("id", "action", "plugin_id", "version", "state", "message", "updated_at") if key in operation} if operation else None
        return {"plugins": plugins, "operation": public, "busy": self.busy(), "token": self.token,
                "supported": self.supported(), "catalog_url": CATALOG_URL,
                "unlisted": [p for p in inventory if not any(c["id"] == p["id"] for c in plugins)]}

    def selected(self, op: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        catalog = self.catalog()
        plugin = next((p for p in catalog["plugins"] if p["id"] == op["plugin_id"]), None)
        if not plugin:
            raise packages.PackageError("Unknown catalog plugin")
        matches = [p for p in packages.installed_inventory() if p["id"] == plugin["id"] or p["distribution"] == plugin["distribution"]]
        if len(matches) > 1 or (matches and (matches[0]["id"], matches[0]["distribution"]) != (plugin["id"], plugin["distribution"])):
            raise packages.PackageError("Conflicting installed plugin identities")
        installed = matches[0] if matches else None
        if (installed["version"] if installed else "") != op["expected_version"]:
            raise packages.PackageError("Installed version changed; reload Admin and try again")
        action = op["action"]
        if action == "install" and installed:
            raise packages.PackageError("Plugin is already installed")
        if action != "install" and not installed:
            raise packages.PackageError("Plugin is not installed")
        if installed and installed["protected"] and action in {"update", "remove"}:
            raise packages.PackageError("Development/unknown-origin installs cannot be replaced or removed")
        if action in {"install", "update"}:
            if not any(r["version"] == op["version"] for r in plugin["releases"]):
                raise packages.PackageError("Version is not approved by this catalog")
        return plugin, installed

    def launch(self, op: dict[str, Any]) -> None:
        powershell = shutil.which("pwsh")
        if not powershell:
            raise packages.PackageError("PowerShell 7 (pwsh) is required by the service controller")
        directory = self.directory / op["id"]
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "controller.log").open("ab") as log:
            subprocess.Popen([powershell, "-NoProfile", "-File", str(self.root / "scripts/service.ps1"),
                              "plugin", "-OperationId", op["id"], "-TimeoutSeconds", "300", "-Json"],
                             cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)

    def begin(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.supported():
            raise packages.PackageError("Automatic maintenance requires Windows and the project .venv")
        if set(payload) != {"action", "plugin_id", "version", "expected_version"} or any(
                not isinstance(v, str) or len(v) > 100 for v in payload.values()):
            raise packages.PackageError("Invalid package operation fields")
        if payload["action"] not in ACTIONS:
            raise packages.PackageError("Unknown package action")
        with self.lock:
            if self.busy():
                raise packages.PackageError("Another package operation is active")
            self.selected(payload)
            op = {**payload, "id": uuid.uuid4().hex}
            self.transition(op, "queued", "Waiting for the service controller lock")
            try:
                self.launch(op)
            except Exception:
                self.transition(op, "failed", "Could not start the service controller; no packages were changed")
                raise
            return op

    def get(self, operation_id: str) -> dict[str, Any]:
        if not packages.re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise packages.PackageError("Invalid operation ID")
        op = self.operation()
        if not op or op["id"] != operation_id:
            raise packages.PackageError("Operation is no longer current")
        return op

    def prepare(self, operation_id: str) -> None:
        op = self.get(operation_id)
        if op["state"] in TERMINAL:
            raise packages.PackageError("Operation already finished")
        if op["state"] not in {"queued", "preparing", "prepared"}:
            raise packages.PackageError("Interrupted maintenance requires service start/restart, then recovery verification")
        plugin, installed = self.selected(op)
        config = load_config(self.root / "yt_library.config.json")
        proxy = configured_proxy(config)
        self.transition(op, "preparing", "Verifying packages and pinned dependencies; YTL remains online")
        stage = self.directory / op["id"] / uuid.uuid4().hex
        stage.mkdir()
        plan = None
        previous = deepcopy(config.get("plugins", {}).get(plugin["id"]))
        rollback = None
        if op["action"] in {"install", "update"}:
            if installed:
                rollback = next((r for r in plugin["releases"] if r["version"] == installed["version"]), None)
                if not rollback:
                    raise packages.PackageError("No approved rollback wheel for the installed version; update manually")
                packages.download_wheel(rollback["wheel"], stage / rollback["wheel"]["filename"], proxy=proxy)
                packages.verify_release_wheel(stage / rollback["wheel"]["filename"], plugin, rollback)
            plan = packages.prepare(self.catalog(), plugin["id"], op["version"], stage / "wheels",
                                    proxy=proxy, stage_dependencies=True)
        self.transition(op, "prepared", "Packages verified; pausing workers before maintenance",
                        stage=str(stage.relative_to(self.directory)), plugin=plugin, plan=plan,
                        rollback=rollback, previous_config=previous, environment=packages.environment_snapshot())

    def arm(self, operation_id: str) -> None:
        op = self.get(operation_id)
        if op["state"] != "prepared":
            raise packages.PackageError("Operation is not prepared")
        self.selected(op)
        if op["environment"] != packages.environment_snapshot():
            raise packages.PackageError("Environment changed after preparation; no packages changed")
        self.transition(op, "armed", "Restarting into maintenance before plugin imports")

    def pip(self, arguments: list[str]) -> None:
        result = subprocess.run([sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check", *arguments],
                                cwd=self.root, capture_output=True, timeout=40)
        if result.returncode:
            raise packages.PackageError("Offline package operation failed; inspect the operation state before retrying")

    def configure(self, op: dict[str, Any], *, enabled: bool, restore: bool = False) -> None:
        config = load_config(self.root / "yt_library.config.json")
        entries = deepcopy(config.get("plugins", {}))
        prior = op.get("previous_config")
        entry = deepcopy(prior) if isinstance(prior, dict) else {}
        if restore and prior is None:
            entries.pop(op["plugin_id"], None)
        else:
            if not entry.get("config"):
                data_root = Path(str(config.get("plugin_data_directory") or "plugin-data"))
                if not data_root.is_absolute():
                    data_root = self.root / data_root
                path = data_root / op["plugin_id"] / "config.json"
                if not path.exists():
                    atomic_json(path, (op.get("plan") or {}).get("config_template", {}))
                entry["config"] = str(path)
            entry.update(enabled=enabled, name=op["plugin"]["name"])
            entries[op["plugin_id"]] = entry
        config["plugins"] = entries
        save_config(config)

    def bootstrap(self) -> None:
        """Called only by the service entry point, before CLI/server/plugin imports."""
        op = self.operation()
        if not op or op["state"] not in {"armed", "applying"}:
            return
        if op["state"] == "applying":
            self.configure(op, enabled=False)
            self.transition(op, "failed", "Maintenance was interrupted. Plugin kept disabled; inspect installed code before retrying. Data retained.")
            return
        changed = False
        try:
            self.selected(op)
            if op["environment"] != packages.environment_snapshot():
                raise packages.PackageError("Environment changed after preparation")
            self.transition(op, "applying", "Applying verified packages offline, before plugin startup")
            self.configure(op, enabled=False)
            action = op["action"]
            if action in {"install", "update"}:
                plan = op["plan"]
                stage = self.directory / op["stage"] / "wheels"
                packages.verify_release_wheel(stage / plan["wheel"], op["plugin"], plan["release"])
                lines = []
                for entry in plan["dependency_plan"]:
                    path = stage / entry["filename"]
                    if path.parent != stage or packages.sha256(path) != entry["sha256"]:
                        raise packages.PackageError("Staged wheel changed after verification")
                    lines.append(f"{path.as_uri()} --hash=sha256:{entry['sha256']}\n")
                requirements = stage / "install.txt"
                requirements.write_text("".join(lines), encoding="utf-8")
                changed = True
                self.pip(["install", "--no-index", "--no-deps", "--only-binary=:all:", "--require-hashes", "-r", str(requirements)])
            elif action == "remove":
                changed = True
                self.pip(["uninstall", "--yes", op["plugin"]["distribution"]])
            enabled = action == "enable" or (action == "update" and bool((op.get("previous_config") or {}).get("enabled")))
            self.configure(op, enabled=enabled)
            self.transition(op, "verifying", "Packages applied; waiting for service and plugin health checks", desired_enabled=enabled)
        except Exception as exc:
            rollback_message = "No package replacement was attempted."
            if changed:
                rollback_message = "Plugin kept disabled; inspect installed code before retrying."
                try:
                    if op.get("rollback"):
                        wheel = self.directory / op["stage"] / op["rollback"]["wheel"]["filename"]
                        packages.verify_release_wheel(wheel, op["plugin"], op["rollback"])
                        self.pip(["install", "--no-index", "--no-deps", "--force-reinstall", str(wheel)])
                        self.configure(op, enabled=bool((op.get("previous_config") or {}).get("enabled")), restore=True)
                        rollback_message = "Prior package restored before plugin startup; data retained."
                    elif op["action"] == "install":
                        self.pip(["uninstall", "--yes", op["plugin"]["distribution"]])
                        rollback_message = "New plugin removed; configuration and data retained."
                except Exception:
                    rollback_message = "Automatic package recovery failed. Plugin kept disabled; manual repair required."
            self.transition(op, "failed", f"{type(exc).__name__}: maintenance failed. {rollback_message}")

    def verify(self, operation_id: str, base_url: str) -> None:
        op = self.get(operation_id)
        if op["state"] != "verifying":
            raise packages.PackageError(op.get("message", "Maintenance did not reach verification"))
        matches = [p for p in packages.installed_inventory() if p["id"] == op["plugin_id"]]
        expected = op["version"] if op["action"] in {"install", "update"} else op["expected_version"]
        if op["action"] == "remove":
            if matches:
                raise packages.PackageError("Plugin distribution is still installed")
        elif len(matches) != 1 or matches[0]["version"] != expected:
            raise packages.PackageError("Installed version differs from the requested result")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base_url.rstrip("/") + "/api/admin/status?include_logs=0&queue_limit=0", timeout=30) as response:
            status = json.load(response)
        runtime = next((p for p in status["plugins"] if p["id"] == op["plugin_id"]), None)
        if op["desired_enabled"] and (not runtime or runtime.get("state") in {"error", "missing", "disabled", "incompatible"}):
            raise packages.PackageError("Package installed, but plugin startup failed. Disable it and inspect plugin status; data was not rolled back.")
        if not op["desired_enabled"] and runtime and runtime.get("enabled"):
            raise packages.PackageError("Plugin unexpectedly remains enabled")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "arm", "verify", "finish", "fail", "state"])
    parser.add_argument("operation_id")
    parser.add_argument("--url", default="")
    args = parser.parse_args()
    installer = Installer()
    try:
        op = installer.get(args.operation_id)
        if args.action == "state":
            print(op["state"])
        elif args.action == "prepare":
            installer.prepare(args.operation_id)
        elif args.action == "arm":
            installer.arm(args.operation_id)
        elif args.action == "verify":
            installer.verify(args.operation_id, args.url)
        elif args.action == "finish":
            installer.transition(op, "succeeded", "Complete. Service verified; prior queue intent restored. Plugin configuration and data retained.")
        elif op["state"] not in TERMINAL:
            installer.transition(op, "failed", "Service controller could not complete maintenance. Check this operation's controller.log and run service.ps1 start to recover queue intent.")
        return 0
    except Exception as exc:
        if args.action != "state":
            op = installer.get(args.operation_id)
            if op["state"] not in TERMINAL:
                message = str(exc) if isinstance(exc, packages.PackageError) else f"{type(exc).__name__}: preparation failed; packages not changed"
                installer.transition(op, "failed", message)
        print(str(exc) if isinstance(exc, packages.PackageError) else type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
