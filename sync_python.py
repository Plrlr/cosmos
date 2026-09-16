"""Re-sync the Android app's copy of the news_hub package.

Run from anywhere; the script locates the package as this folder's parent:

    python android/sync_python.py

Only the modules the package actually imports at runtime are copied. The
tests, ``__main__.py``, the ``news_hub.py`` import shim and ``__pycache__``
are deliberately excluded -- the app never needs them, and the shim would
shadow the real package on Android's sys.path.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

#: Everything the runtime import graph of news_hub.cli reaches:
#: __init__ (version), cli, core, geometry, sources.
RUNTIME_MODULES = ("__init__.py", "cli.py", "core.py", "geometry.py", "sources.py")


def main() -> int:
    android = Path(__file__).resolve().parent
    src = android.parent
    dst = android / "app" / "src" / "main" / "python" / "news_hub"
    if not (src / "__init__.py").is_file() or not (src / "cli.py").is_file():
        print(f"error: package not found at {src}", file=sys.stderr)
        return 1
    dst.mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_MODULES:
        shutil.copyfile(src / name, dst / name)
    print(f"Synced {len(RUNTIME_MODULES)} modules: {src} -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
