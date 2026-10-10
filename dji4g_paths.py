"""Shared platform defaults for private state and the process-wide AT lock."""

import os
import sys
from pathlib import Path


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "dji4g"
    # XDG paths must be absolute; ignore relative values rather than using cwd.
    configured = os.environ.get("XDG_DATA_HOME", "")
    base = Path(configured) if configured else Path.home() / ".local" / "share"
    if not base.is_absolute():
        base = Path.home() / ".local" / "share"
    return base / "dji4g"
