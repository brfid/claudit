"""Shared JSON read/write helpers used by ledger and ingest state."""

import contextlib
import json
import os
import tempfile
from pathlib import Path


def load_json(path: Path, default, validate=None):
    """Load JSON file, returning default if missing, corrupt, or invalid."""
    if not path.exists():
        return default
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        if validate and not validate(data):
            return default
        return data
    except (json.JSONDecodeError, IOError):
        return default


def atomic_json_write(path: Path, data) -> None:
    """Write JSON atomically via tmp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, separators=(',', ':'))
        os.rename(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
