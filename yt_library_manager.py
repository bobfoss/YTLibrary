#!/usr/bin/env python3
"""CLI shim for YT Library Manager."""

from __future__ import annotations

def main() -> int | None:
    import sys

    # Maintenance must finish before cli imports server and discovers plugins.
    if len(sys.argv) == 1 or sys.argv[1] == "serve":
        from yt_library.plugin_installation import Installer

        Installer().bootstrap()
    from yt_library.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
