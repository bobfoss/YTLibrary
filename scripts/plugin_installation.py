"""Controller entry point for durable package maintenance."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yt_library.plugin_installation import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
