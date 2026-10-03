"""Build wheel/sdist candidates from a clean plugin Git commit, never runtime data."""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

from plugin_packages import ROOT, inspect_wheel, pip_network_arguments, read_json, sha256, validate_catalog, write_json


def build_release(repository: Path, catalog_path: Path, plugin_id: str, destination: Path) -> dict:
    repository = repository.resolve()
    catalog = validate_catalog(read_json(catalog_path))
    plugin = next(p for p in catalog["plugins"] if p["id"] == plugin_id)
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=repository).strip():
        raise ValueError("Commit or isolate changes before building a release candidate")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    archive = subprocess.check_output(["git", "archive", "--format=zip", commit], cwd=repository)
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("Output directory already exists; do not overwrite released artifacts")
    with tempfile.TemporaryDirectory(prefix="ytl-plugin-build-") as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            # git archive is local tracked source, but still reject private runtime
            # files rather than trusting packaging configuration to exclude them.
            for name in zipped.namelist():
                lower = name.lower()
                if ".." in Path(name).parts or Path(name).is_absolute() or ":" in name or "\\" in name:
                    raise ValueError("Unsafe Git archive path")
                if lower.endswith((".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".config.json", ".log")) or "cookies" in lower:
                    raise ValueError(f"Private runtime path is tracked: {name}")
            zipped.extractall(source)
        pyproject = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
        project = pyproject["project"]
        if project["name"] != plugin["distribution"]:
            raise ValueError("Project distribution differs from the catalog")
        version = project["version"]
        output = root / "dist"
        sys.path.insert(0, str(ROOT)) if str(ROOT) not in sys.path else None
        from yt_library.config import configured_proxy

        config = read_json(ROOT / "yt_library.config.json") if (ROOT / "yt_library.config.json").exists() else {}
        environment = os.environ.copy()
        proxy = configured_proxy(config)
        if proxy:
            # The isolated backend environment has no PySocks bootstrap. Fetch
            # build wheels with the host's proxy-capable pip, then build offline.
            wheelhouse = root / "build-wheels"
            wheelhouse.mkdir()
            subprocess.run([
                sys.executable, "-m", "pip", "--isolated", *pip_network_arguments(proxy),
                "download", "--only-binary=:all:", "--dest", str(wheelhouse),
                *pyproject["build-system"]["requires"], "wheel", "packaging",
            ], check=True)
            environment["PIP_NO_INDEX"] = "1"
            environment["PIP_FIND_LINKS"] = str(wheelhouse)
            environment.pop("PIP_PROXY", None)
        # build's default builds the sdist, then builds the wheel from that sdist.
        subprocess.run([sys.executable, "-m", "build", "--outdir", str(output), str(source)], check=True, env=environment)
        wheel, = output.glob("*.whl")
        sdist, = output.glob("*.tar.gz")
        details = inspect_wheel(wheel)
        if details["id"] != plugin_id or details["version"] != version:
            raise ValueError("Built wheel identity differs from project metadata")
        with tarfile.open(sdist, "r:gz") as source_archive:
            names = source_archive.getnames()
            if not any(n.endswith("/LICENSE") for n in names) or not any(n.endswith("/ytl-plugin.json") for n in names):
                raise ValueError("Source distribution is missing license or installation metadata")
        record = {key: details[key] for key in (
            "version", "license", "requires_python", "dependencies", "plugin_api_version",
            "browser_api_version", "required_host_features",
        )}
        tag = "v" + version
        record.update({"tag": tag, "commit": commit, "release_url": plugin["repository_url"] + "/releases/tag/" + tag})
        for kind, path in (("wheel", wheel), ("source", sdist)):
            record[kind] = {"filename": path.name, "size": path.stat().st_size, "sha256": sha256(path),
                            "url": f"{plugin['repository_url']}/releases/download/{tag}/{path.name}"}
        candidate = {**plugin, "releases": [record]}
        validate_catalog({"schema_version": 1, "plugins": [candidate]})
        write_json(output / "release.json", record)
        write_json(output / "catalog-candidate.json", {"schema_version": 1, "plugins": [candidate]})
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(output, destination)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("plugin_id")
    parser.add_argument("--catalog", type=Path, default=Path(__file__).resolve().parents[1] / "plugins" / "catalog.json")
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    result = build_release(args.repository, args.catalog, args.plugin_id, args.destination)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
