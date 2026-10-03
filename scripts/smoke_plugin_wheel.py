"""Test a trusted, maintainer-built plugin in a disposable install/data directory.

Unlike inspection this intentionally executes the selected wheel's code. Never
use it as a security sandbox or on an untrusted downloaded package.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

from plugin_packages import inspect_wheel


PROBE = r'''
import importlib.metadata
import inspect
import json
from pathlib import Path
import sys
from types import SimpleNamespace

target, data = map(Path, sys.argv[1:3])
sys.path.insert(0, str(target))
dist, = importlib.metadata.distributions(path=[str(target)])
entry, = [e for e in dist.entry_points if e.group == "yt_library.plugins"]
plugin = entry.load()()
module_path = Path(inspect.getfile(type(plugin))).resolve()
assert module_path.is_relative_to(target.resolve()), module_path
manifest = json.loads((module_path.parent / "ytl-plugin.json").read_text())
assert manifest["id"] == entry.name == plugin.plugin_id
assert plugin.plugin_version == dist.version
assert plugin.plugin_api_version == manifest["plugin_api_version"]
assert set(getattr(plugin, "required_host_features", ())) == set(manifest["required_host_features"])
assert manifest["browser_api_version"] == (2 if getattr(plugin, "browser_assets", ()) else None)
for asset in getattr(plugin, "browser_assets", ()):
    assert (module_path.parent / asset["path"]).is_file(), asset
assert dist.metadata["License-Expression"] == "GPL-3.0-or-later"
config = data / "config.json"
config.write_text(json.dumps(manifest["config_template"]), encoding="utf-8")
context = SimpleNamespace(root=data, config_path=data / "host.json", plugin_id=entry.name,
    plugin_config={"config": str(config)}, resolve_path=lambda value: (data / value).resolve(),
    host_features=frozenset(manifest["required_host_features"]), library_videos=lambda ids: {})
before = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
plugin.start(context)
try:
    status = plugin.status()
    assert isinstance(status, dict)
finally:
    shutdown = getattr(plugin, "shutdown", None)
    if callable(shutdown):
        shutdown()
after = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
assert before == after, "Plugin wrote runtime state into its installed package"
assert config.exists()
print(json.dumps({"id": entry.name, "version": dist.version, "fresh_start": "ok", "package_files_unchanged": True}))
'''


def smoke_wheel(path: Path) -> None:
    inspect_wheel(path)
    with tempfile.TemporaryDirectory(prefix="ytl-plugin-smoke-") as temporary:
        root = Path(temporary)
        target = root / "installed"
        data = root / "data"
        data.mkdir()
        subprocess.run([sys.executable, "-m", "pip", "--isolated", "install", "--quiet", "--no-index",
                        "--no-deps", "--target", str(target), str(path.resolve())], check=True)
        # -I removes the checkout/PYTHONPATH, while -B avoids package bytecode writes.
        # Dependencies come from the maintainer environment; the plugin itself
        # must load from the temporary wheel target, verified in the probe.
        subprocess.run([sys.executable, "-I", "-B", "-c", PROBE, str(target), str(data)], cwd=root, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    smoke_wheel(args.wheel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
