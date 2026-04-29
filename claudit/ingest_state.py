"""Per-run ingest state: file seek-offsets, last-ingest timestamp.

Kept separate from `ledger.py` because the two serve different lifecycles:
the ledger is the durable cost record; ingest state is bookkeeping that can
be discarded and rebuilt by re-parsing sources.
"""

from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from ._json_io import atomic_json_write, load_json


def get_ingest_state_path(ledger_path: Path) -> Path:
    return ledger_path.parent / "ingest_state.json"


def new_ingest_state() -> Dict:
    return {"_version": 1, "files": {}, "last_ingest_at": None}


def load_ingest_state(path: Path) -> Dict:
    return load_json(
        path, new_ingest_state(),
        validate=lambda d: isinstance(d, dict) and "files" in d,
    )


def save_ingest_state(path: Path, state: Dict) -> None:
    atomic_json_write(path, state)


def hours_since_last_ingest(state: Dict) -> Optional[float]:
    """Compute hours elapsed since the last successful ingest.

    Returns:
      Hours since the timestamp in ``state["last_ingest_at"]``, or ``None``
      if the field is missing or malformed.
    """
    ts = state.get("last_ingest_at")
    if not ts:
        return None
    try:
        prev = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    delta = datetime.now() - prev
    return delta.total_seconds() / 3600.0


def stamp_ingest(state: Dict) -> None:
    state["last_ingest_at"] = datetime.now().isoformat(timespec="seconds")


def prune_orphan_file_state(state: Dict) -> int:
    """Drop file-tracking entries for files no longer on disk.

    Ledger entries are untouched; only the per-file seek-offset bookkeeping
    in ``state["files"]`` is cleaned.

    Returns:
      Count of file entries pruned.
    """
    files = state.get("files", {})
    dead = [k for k in files if not Path(k).exists()]
    for k in dead:
        del files[k]
    return len(dead)


def file_needs_processing(filepath: Path, state: Dict) -> Tuple[bool, int]:
    """Decide whether a file must be re-read, and from where.

    Returns:
      ``(needs_processing, seek_offset)``. ``seek_offset`` is 0 for new or
      shrunken files and the stored byte offset when resuming an appended
      file.
    """
    file_key = str(filepath)
    try:
        stat = filepath.stat()
    except OSError:
        return False, 0

    stored = state.get("files", {}).get(file_key)
    if not stored:
        return True, 0

    stored_size = stored.get("size", 0)

    if stat.st_size == stored_size:
        return False, 0

    if stat.st_size < stored_size:
        return True, 0

    return True, stored.get("byte_offset", 0)


def update_file_state(state: Dict, filepath: Path, byte_offset: int,
                      last_user_text: str = "") -> None:
    try:
        stat = filepath.stat()
    except OSError:
        return
    state.setdefault("files", {})[str(filepath)] = {
        "byte_offset": byte_offset,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "last_user_text": last_user_text,
    }


def get_stored_user_text(state: Dict, filepath: Path) -> str:
    """Retrieve last captured user text for a file, for seeding incremental resumes."""
    return state.get("files", {}).get(str(filepath), {}).get("last_user_text", "")
