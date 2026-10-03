"""Plugin package inspection and verified installation staging.

This is the foundation for the maintenance installer, not a live pip installer.
It never activates plugins, changes the running environment, or controls workers.
"""

from __future__ import annotations

import argparse
import configparser
from email.parser import BytesParser
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any
import urllib.parse
import urllib.request
import zipfile

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "plugins" / "catalog.json"
ID = re.compile(r"[a-z][a-z0-9_-]{0,79}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_WHEEL_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
ENTRY_GROUP = "yt_library.plugins"


class PackageError(ValueError):
    """A package or installation plan cannot be safely accepted."""


def read_json(path: Path) -> Any:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise PackageError(f"JSON document is too large: {path.name}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise PackageError("Invalid plugin or feature identifier")
    return value


def _features(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise PackageError("Host features must be a bounded list")
    items = [_identifier(item) for item in value]
    if items != sorted(set(items)):
        raise PackageError("Host features must be unique and sorted")
    return items


def _api_versions(value: dict[str, Any]) -> None:
    if type(value.get("plugin_api_version")) is not int or value["plugin_api_version"] < 1:
        raise PackageError("Invalid Python plugin API version")
    if "browser_api_version" not in value:
        raise PackageError("Browser API declaration is missing (use null for backend-only plugins)")
    browser = value["browser_api_version"]
    if browser is not None and (type(browser) is not int or browser < 1):
        raise PackageError("Invalid browser API version")
    _features(value.get("required_host_features"))


def _requirements(values: Any) -> list[str]:
    if not isinstance(values, list) or len(values) > 100:
        raise PackageError("Dependencies must be a bounded list")
    for value in values:
        if not isinstance(value, str) or len(value) > 2000:
            raise PackageError("Invalid dependency")
        req = Requirement(value)
        if req.url:
            raise PackageError("Direct URL dependencies are not supported")
    return sorted(values)


def _repository(url: Any) -> str:
    if not isinstance(url, str) or not re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", url
    ) or url.endswith(("/.", "/..", ".git")):
        raise PackageError("Expected a canonical HTTPS GitHub repository URL")
    return url


def validate_catalog(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise PackageError("Unsupported catalog schema")
    plugins = value.get("plugins")
    if not isinstance(plugins, list) or len(plugins) > 100:
        raise PackageError("Invalid plugin list")
    ids: set[str] = set()
    distributions: set[str] = set()
    for plugin in plugins:
        if not isinstance(plugin, dict):
            raise PackageError("Invalid plugin record")
        plugin_id = _identifier(plugin.get("id"))
        distribution = plugin.get("distribution")
        if not isinstance(distribution, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", distribution):
            raise PackageError("Expected a normalized distribution name")
        if plugin_id in ids or distribution in distributions:
            raise PackageError("Duplicate plugin or distribution")
        ids.add(plugin_id)
        distributions.add(distribution)
        for field in ("name", "description"):
            if not isinstance(plugin.get(field), str) or not 1 <= len(plugin[field]) <= 1000:
                raise PackageError(f"Invalid plugin {field}")
        repository = _repository(plugin.get("repository_url"))
        releases = plugin.get("releases")
        if not isinstance(releases, list) or len(releases) > 200:
            raise PackageError("Invalid release list")
        versions: set[Version] = set()
        for release in releases:
            if not isinstance(release, dict):
                raise PackageError("Invalid release record")
            version = Version(release["version"])
            if str(version) != release["version"] or version in versions or version.is_prerelease or version.is_devrelease or version.local:
                raise PackageError("Expected a unique, canonical stable release version")
            versions.add(version)
            tag = "v" + str(version)
            if release.get("tag") != tag or not re.fullmatch(r"[0-9a-f]{40}", str(release.get("commit"))):
                raise PackageError("Release must identify a version tag and exact commit")
            if release.get("release_url") != f"{repository}/releases/tag/{tag}":
                raise PackageError("Release notes must belong to the catalog repository/tag")
            if release.get("license") != "GPL-3.0-or-later":
                raise PackageError("Catalog currently supports GPL-3.0-or-later releases")
            if not isinstance(release.get("requires_python"), str) or not release["requires_python"]:
                raise PackageError("Python requirement is missing")
            SpecifierSet(release["requires_python"])
            _api_versions(release)
            _requirements(release.get("dependencies"))
            for kind in ("wheel", "source"):
                artifact = release.get(kind)
                if not isinstance(artifact, dict):
                    raise PackageError(f"Missing {kind} artifact")
                filename = artifact.get("filename")
                if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", filename):
                    raise PackageError("Unsafe artifact filename")
                if kind == "wheel":
                    name, wheel_version, _build, _tags = parse_wheel_filename(filename)
                    if name != distribution or wheel_version != version:
                        raise PackageError("Wheel filename does not match release identity")
                elif not filename.endswith(".tar.gz"):
                    raise PackageError("Source artifact must be an sdist tarball")
                if artifact.get("url") != f"{repository}/releases/download/{tag}/{filename}":
                    raise PackageError("Artifact must belong to the catalog repository/tag")
                if not SHA256.fullmatch(str(artifact.get("sha256"))):
                    raise PackageError("Invalid artifact SHA-256")
                if type(artifact.get("size")) is not int or not 0 < artifact["size"] <= MAX_WHEEL_BYTES:
                    raise PackageError("Invalid artifact size")
    return value


def inspect_wheel(path: Path) -> dict[str, Any]:
    """Inspect declarations without importing or extracting package code."""
    if not 0 < path.stat().st_size <= MAX_WHEEL_BYTES:
        raise PackageError("Wheel exceeds size limit")
    name, version, _build, tags = parse_wheel_filename(path.name)
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        paths = [info.filename for info in infos]
        if len(paths) != len(set(paths)) or len(paths) > 10_000:
            raise PackageError("Duplicate or excessive wheel entries")
        if sum(info.file_size for info in infos) > MAX_EXPANDED_BYTES:
            raise PackageError("Expanded wheel exceeds size limit")
        for info in infos:
            member = PurePosixPath(info.filename)
            if (
                member.is_absolute() or ".." in member.parts or "\\" in info.orig_filename
                or ":" in info.filename or stat.S_ISLNK(info.external_attr >> 16)
                or info.filename.endswith(".pth") or any(part.endswith(".data") for part in member.parts)
            ):
                raise PackageError("Unsupported or unsafe wheel entry")
        meta_paths = [p for p in paths if p.endswith(".dist-info/METADATA")]
        if len(meta_paths) != 1:
            raise PackageError("Wheel must contain exactly one distribution")
        meta_path = meta_paths[0]
        dist_info = meta_path.rsplit("/", 1)[0]
        meta = BytesParser().parsebytes(archive.read(meta_path))
        if canonicalize_name(meta["Name"] or "") != name or Version(meta["Version"] or "0") != version:
            raise PackageError("Wheel filename and metadata disagree")
        if meta.get("License-Expression") != "GPL-3.0-or-later":
            raise PackageError("Missing GPL-3.0-or-later license expression")
        license_path = f"{dist_info}/licenses/LICENSE"
        if license_path not in paths or b"END OF TERMS AND CONDITIONS" not in archive.read(license_path):
            raise PackageError("Wheel is missing the complete license text")
        entries = configparser.ConfigParser(interpolation=None)
        entries.optionxform = str
        entries.read_string(archive.read(f"{dist_info}/entry_points.txt").decode("utf-8"))
        if ENTRY_GROUP not in entries or len(entries[ENTRY_GROUP]) != 1:
            raise PackageError("Wheel must declare exactly one YTL entry point")
        plugin_id, factory = next(iter(entries[ENTRY_GROUP].items()))
        _identifier(plugin_id)
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", factory):
            raise PackageError("Unsupported entry-point factory")
        package = factory.split(".", 1)[0].split(":", 1)[0]
        manifest_path = f"{package}/ytl-plugin.json"
        if manifest_path not in paths or archive.getinfo(manifest_path).file_size > MAX_JSON_BYTES:
            raise PackageError("Missing or oversized installation manifest")
        manifest = json.loads(archive.read(manifest_path))
        if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1 or manifest.get("id") != plugin_id:
            raise PackageError("Invalid installation manifest identity/schema")
        _api_versions(manifest)
        if not isinstance(manifest.get("config_template"), dict):
            raise PackageError("Plugin must declare a first-run config object")
        requires_python = meta.get("Requires-Python", "")
        if not requires_python:
            raise PackageError("Wheel must declare Requires-Python")
        SpecifierSet(requires_python)
        return {
            "id": plugin_id, "distribution": str(name), "version": str(version),
            "license": meta["License-Expression"], "requires_python": requires_python,
            "dependencies": _requirements(meta.get_all("Requires-Dist", [])),
            "plugin_api_version": manifest["plugin_api_version"],
            "browser_api_version": manifest["browser_api_version"],
            "required_host_features": manifest["required_host_features"],
            "config_template": manifest["config_template"], "entry_point": factory,
            "filename": path.name, "size": path.stat().st_size, "sha256": sha256(path),
            "compatible_tags": bool(tags.intersection(sys_tags())),
        }


def compatibility_errors(release: dict[str, Any], *, plugin_api: int, browser_api: int,
                         features: set[str] | frozenset[str], python_version: str | None = None) -> list[str]:
    errors: list[str] = []
    python_version = python_version or ".".join(map(str, sys.version_info[:3]))
    if Version(python_version) not in SpecifierSet(release["requires_python"]):
        errors.append(f"Requires Python {release['requires_python']}; current {python_version}")
    if release["plugin_api_version"] != plugin_api:
        errors.append(f"Requires plugin API {release['plugin_api_version']}; current {plugin_api}")
    if release["browser_api_version"] not in (None, browser_api):
        errors.append(f"Requires browser API {release['browser_api_version']}; current {browser_api}")
    missing = sorted(set(release["required_host_features"]) - features)
    if missing:
        errors.append("Missing host features: " + ", ".join(missing))
    wheel = release.get("wheel")
    if wheel and not parse_wheel_filename(wheel["filename"])[3].intersection(sys_tags()):
        errors.append("Wheel platform/interpreter tags do not match this environment")
    return errors


def installed_inventory(distributions: Any = None) -> list[dict[str, Any]]:
    """Read distribution metadata only; never load plugin factories."""
    result: list[dict[str, Any]] = []
    for dist in metadata.distributions() if distributions is None else distributions:
        entries = [entry for entry in dist.entry_points if entry.group == ENTRY_GROUP]
        if not entries:
            continue
        source = ""
        mode = "package"
        direct = dist.read_text("direct_url.json")
        if direct:
            try:
                data = json.loads(direct)
                if data.get("dir_info", {}).get("editable") is True:
                    mode = "development"
                    parsed = urllib.parse.urlsplit(data.get("url", ""))
                    if parsed.scheme == "file":
                        source = urllib.request.url2pathname(urllib.parse.unquote(parsed.path))
                    else:
                        source = "editable source"
            except (ValueError, TypeError, AttributeError):
                mode = "unknown"
        elif callable(getattr(dist, "locate_file", None)):
            location = Path(dist.locate_file("")).resolve()
            if not location.is_relative_to(Path(sys.prefix).resolve()):
                # Legacy editable/global source installs lack PEP 610 metadata.
                mode = "unknown"
                source = str(location)
        for entry in entries:
            result.append({"id": entry.name, "distribution": canonicalize_name(dist.metadata["Name"]),
                           "version": dist.version, "mode": mode, "source": source,
                           "protected": mode != "package"})
    return sorted(result, key=lambda item: (item["id"], item["distribution"]))


def verify_artifact(path: Path, artifact: dict[str, Any]) -> None:
    if path.name != artifact["filename"] or path.stat().st_size != artifact["size"] or sha256(path) != artifact["sha256"]:
        raise PackageError("Artifact filename, size or SHA-256 does not match the trusted catalog")


def verify_release_wheel(path: Path, plugin: dict[str, Any], release: dict[str, Any]) -> dict[str, Any]:
    verify_artifact(path, release["wheel"])
    details = inspect_wheel(path)
    if details["id"] != plugin["id"] or details["distribution"] != plugin["distribution"]:
        raise PackageError("Wheel plugin identity differs from the catalog")
    for field in ("version", "license", "requires_python", "plugin_api_version", "browser_api_version", "required_host_features", "dependencies"):
        if details[field] != release[field]:
            raise PackageError(f"Wheel {field} differs from the catalog")
    if not details["compatible_tags"]:
        raise PackageError("Wheel is not compatible with this interpreter/platform")
    return details


class _ReleaseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in {
            "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
        } or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise PackageError("Release download redirected to an unapproved host")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download_wheel(artifact: dict[str, Any], target: Path, *, proxy: str = "") -> None:
    # The caller must first validate the catalog's exact repository/tag URLs.
    sys.path.insert(0, str(ROOT)) if str(ROOT) not in sys.path else None
    from yt_library.network import socks5_proxy_handlers

    handlers = socks5_proxy_handlers(proxy) if proxy else []
    opener = urllib.request.build_opener(_ReleaseRedirects(), *handlers)
    request = urllib.request.Request(artifact["url"], headers={"User-Agent": "YTLibrary-plugin-packages"})
    total = 0
    with opener.open(request, timeout=30) as response, target.open("xb") as output:
        while block := response.read(64 * 1024):
            total += len(block)
            if total > artifact["size"]:
                raise PackageError("Release download exceeded its catalog size")
            output.write(block)
    verify_artifact(target, artifact)


def environment_snapshot(distributions: Any = None) -> list[dict[str, str]]:
    return sorted([
        {"name": canonicalize_name(dist.metadata["Name"]), "version": dist.version,
         "origin_sha256": hashlib.sha256((dist.read_text("direct_url.json") or "").encode()).hexdigest()}
        for dist in (metadata.distributions() if distributions is None else distributions)
    ], key=lambda item: (item["name"], item["version"], item["origin_sha256"]))


def download_dependency(url: str, target: Path, digest: str, *, proxy: str = "") -> None:
    from .network import socks5_proxy_handlers

    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org"
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise PackageError("Dependency URL is not an approved PyPI wheel")
    # PyPI artifact URLs are final; do not follow redirects to other origins.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise PackageError("Unexpected dependency redirect")

    opener = urllib.request.build_opener(NoRedirect(), *socks5_proxy_handlers(proxy))
    total = 0
    with opener.open(url, timeout=30) as response, target.open("xb") as output:
        while block := response.read(65536):
            total += len(block)
            if total > MAX_WHEEL_BYTES:
                raise PackageError("Dependency wheel exceeds size limit")
            output.write(block)
    if sha256(target) != digest:
        raise PackageError("Dependency SHA-256 mismatch")


def pip_network_arguments(proxy: str) -> list[str]:
    if not proxy:
        return []
    arguments = ["--proxy", proxy]
    if proxy.startswith(("socks5://", "socks5h://")) and metadata.version("pip") == "26.2":
        # pip 26.2's system-certificate adapter passes an HTTPS-proxy-only
        # option into SOCKS pool keys. The documented certifi mode avoids this
        # error while retaining HTTPS certificate verification. Do not disable TLS.
        arguments.append("--use-deprecated=legacy-certs")
    return arguments


def installed_plugin_requirements(exclude_distribution: str) -> list[str]:
    requirements: set[str] = set()
    for dist in metadata.distributions():
        if canonicalize_name(dist.metadata["Name"]) == exclude_distribution:
            continue
        if any(entry.group == ENTRY_GROUP for entry in dist.entry_points):
            requirements.update(_requirements(dist.requires or []))
    return sorted(requirements)


def prepare(catalog: dict[str, Any], plugin_id: str, version: str, destination: Path, *,
            local_wheel: Path | None = None, proxy: str = "", stage_dependencies: bool = False) -> dict[str, Any]:
    """Stage verified wheel + wheel-only dependency plan without installing it."""
    validate_catalog(catalog)
    plugin = next((p for p in catalog["plugins"] if p["id"] == plugin_id), None)
    if plugin is None:
        raise PackageError("Unknown catalog plugin")
    release = next((r for r in plugin["releases"] if r["version"] == version), None)
    if release is None:
        raise PackageError("Version is not approved by this catalog")
    sys.path.insert(0, str(ROOT)) if str(ROOT) not in sys.path else None
    from yt_library.plugins import PLUGIN_API_VERSION, PLUGIN_HOST_FEATURES

    errors = compatibility_errors(release, plugin_api=PLUGIN_API_VERSION, browser_api=2, features=PLUGIN_HOST_FEATURES)
    if errors:
        raise PackageError("; ".join(errors))
    existing = [p for p in installed_inventory() if p["id"] == plugin_id or p["distribution"] == plugin["distribution"]]
    if any(p["protected"] for p in existing):
        raise PackageError("Development or unknown-origin install is protected; manage it from its source checkout")
    if len(existing) > 1 or any(p["id"] != plugin_id or p["distribution"] != plugin["distribution"] for p in existing):
        raise PackageError("Conflicting installed plugin identities")
    if existing and Version(version) <= Version(existing[0]["version"]):
        raise PackageError("Selected version must be newer than the installed package")
    destination = destination.resolve()
    if destination.exists():
        raise PackageError("Preparation destination already exists; choose a new directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    before = environment_snapshot()
    with tempfile.TemporaryDirectory(prefix=".ytl-prepare-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        wheel = stage / release["wheel"]["filename"]
        if local_wheel:
            verify_artifact(local_wheel, release["wheel"])
            shutil.copyfile(local_wheel, wheel)
        else:
            download_wheel(release["wheel"], wheel, proxy=proxy)
        details = verify_release_wheel(wheel, plugin, release)
        constraints = stage / "constraints.txt"
        # Freeze everything except the explicitly selected plugin. No silent
        # upgrades/downgrades of core or other plugins, including optional extras.
        constraints.write_text("".join(
            f"{d['name']}=={d['version']}\n" for d in before if d["name"] != plugin["distribution"]
        ), encoding="utf-8")
        report_path = stage / "pip-report.json"
        command = [sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check"]
        command.extend(pip_network_arguments(proxy))
        resolution = command + ["install", "--dry-run", "--only-binary=:all:", "--report", str(report_path),
                                "--constraint", str(constraints), "--requirement", str(ROOT / "requirements.txt"),
                                *installed_plugin_requirements(plugin["distribution"]), str(wheel)]
        result = subprocess.run(resolution, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        if result.returncode:
            # Do not persist pip output: private proxy/index URLs can appear in it.
            raise PackageError("Wheel-only dependency resolution failed. Check Python/platform support and dependency conflicts; existing versions were pinned.")
        report = read_json(report_path)
        if report.get("version") != "1":
            raise PackageError("Unsupported pip installation report")
        planned = report.get("install", [])
        if not isinstance(planned, list) or len(planned) > 200:
            raise PackageError("Invalid pip dependency plan")
        installed = {d["name"]: d["version"] for d in before}
        dependency_plan = []
        for item in planned:
            name = canonicalize_name(item["metadata"]["name"])
            selected_version = item["metadata"]["version"]
            if name != plugin["distribution"] and name in installed and selected_version != installed[name]:
                raise PackageError("Dependency plan would replace an installed package")
            source = item["download_info"]
            url = urllib.parse.urlsplit(source["url"])
            digest = source.get("archive_info", {}).get("hashes", {}).get("sha256", "")
            if not url.path.endswith(".whl") or not SHA256.fullmatch(digest):
                raise PackageError("Dependency plan must contain hashed wheels only")
            if name != plugin["distribution"] and (url.scheme != "https" or url.hostname != "files.pythonhosted.org"):
                raise PackageError("Dependencies must come from PyPI wheel assets")
            entry = {"distribution": name, "version": selected_version, "sha256": digest}
            if stage_dependencies:
                filename = urllib.parse.unquote(url.path.rsplit("/", 1)[-1])
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl", filename):
                    raise PackageError("Unsafe dependency filename")
                wheel_name, wheel_version, _build, tags = parse_wheel_filename(filename)
                if wheel_name != name or str(wheel_version) != selected_version or not tags.intersection(sys_tags()):
                    raise PackageError("Dependency wheel identity/platform mismatch")
                if name != plugin["distribution"]:
                    download_dependency(source["url"], stage / filename, digest, proxy=proxy)
                entry["filename"] = filename
            dependency_plan.append(entry)
        targets = [item for item in dependency_plan if item["distribution"] == plugin["distribution"]]
        if len(targets) != 1 or targets[0]["version"] != version or targets[0]["sha256"] != release["wheel"]["sha256"]:
            raise PackageError("Dependency plan does not install the exact selected plugin artifact")
        if environment_snapshot() != before:
            raise PackageError("Environment changed during preparation; retry against the new state")
        plan = {"schema_version": 1, "state": "prepared_not_installed", "plugin_id": plugin_id,
                "version": version, "release": release, "wheel": wheel.name,
                "config_template": details["config_template"], "environment": before,
                "dependency_plan": dependency_plan, "dependencies_staged": stage_dependencies,
                "notice": "Staging only. Maintenance must revalidate the environment and obtain dependency wheels before installation."}
        report_path.unlink()
        constraints.unlink()
        write_json(stage / "plan.json", plan)
        os.rename(stage, destination)
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    catalog_parser = commands.add_parser("catalog", help="Validate the bundled or selected catalog")
    catalog_parser.add_argument("--catalog", type=Path, default=CATALOG)
    inspect_parser = commands.add_parser("inspect", help="Inspect a wheel without executing it")
    inspect_parser.add_argument("wheel", type=Path)
    commands.add_parser("inventory", help="List installed plugins and protected editable checkouts")
    prepare_parser = commands.add_parser("prepare", help="Stage and resolve an approved release; never install it")
    prepare_parser.add_argument("plugin_id")
    prepare_parser.add_argument("version")
    prepare_parser.add_argument("--catalog", type=Path, default=CATALOG)
    prepare_parser.add_argument("--destination", type=Path, required=True)
    prepare_parser.add_argument("--local-wheel", type=Path, help="Offline maintainer artifact; still must match the catalog")
    args = parser.parse_args()
    try:
        if args.command == "catalog":
            result = validate_catalog(read_json(args.catalog))
        elif args.command == "inspect":
            result = inspect_wheel(args.wheel)
        elif args.command == "inventory":
            result = installed_inventory()
        else:
            config = read_json(ROOT / "yt_library.config.json") if (ROOT / "yt_library.config.json").exists() else {}
            sys.path.insert(0, str(ROOT)) if str(ROOT) not in sys.path else None
            from yt_library.config import configured_proxy

            result = prepare(read_json(args.catalog), args.plugin_id, args.version, args.destination,
                             local_wheel=args.local_wheel, proxy=configured_proxy(config))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (PackageError, ValueError, KeyError, OSError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        print(f"Package preparation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
