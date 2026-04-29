"""Ledger file I/O, file tracking state, and cost recalculation."""

import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from .formatters import FIELD_COST, FIELD_CACHE_SAVINGS, FIELD_TOKENS_IN, FIELD_TOKENS_OUT, FIELD_CACHE_WRITES, FIELD_CACHE_READS
from .pricing import calculate_cost, calculate_cache_savings

BACKUP_RETAIN = 7


def get_ledger_path(override: Optional[str] = None) -> Path:
    if override:
        return Path(override)
    data_dir = Path.home() / ".local" / "share" / "claudit"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "ledger.json"


def get_ingest_state_path(ledger_path: Path) -> Path:
    return ledger_path.parent / "ingest_state.json"


def _load_json(path: Path, default, validate=None):
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


def load_ledger(path: Path) -> Dict[str, Dict]:
    return _load_json(path, {}, validate=lambda d: isinstance(d, dict))


def _atomic_json_write(path: Path, data) -> None:
    """Write JSON atomically via tmp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, separators=(',', ':'))
        os.rename(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_ledger(path: Path, ledger: Dict[str, Dict]) -> None:
    _atomic_json_write(path, ledger)


def backup_dir(ledger_path: Path) -> Path:
    return ledger_path.parent / "backups"


def latest_backup_mtime(ledger_path: Path) -> Optional[float]:
    d = backup_dir(ledger_path)
    if not d.exists():
        return None
    backups = sorted(d.glob("ledger-*.json"))
    if not backups:
        return None
    try:
        return max(b.stat().st_mtime for b in backups)
    except OSError:
        return None


def rotate_backup(ledger_path: Path, retain: int = BACKUP_RETAIN) -> Optional[Path]:
    """Snapshot the current ledger into ``backups/ledger-YYYY-MM-DD.json``.

    Idempotent per-day: skips when a backup for today already exists. After
    writing, prunes older snapshots down to the ``retain`` most recent.

    Args:
      ledger_path: Path to the live ledger file.
      retain: Number of snapshots to keep.

    Returns:
      Path to the backup that was written, or ``None`` if the source ledger
      is missing or a backup for today already exists.
    """
    if not ledger_path.exists():
        return None
    d = backup_dir(ledger_path)
    d.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    dest = d / f"ledger-{today}.json"
    if dest.exists():
        return None
    shutil.copy2(ledger_path, dest)
    backups = sorted(d.glob("ledger-*.json"))
    for old in backups[:-retain]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def _new_ingest_state() -> Dict:
    return {"_version": 1, "files": {}, "last_ingest_at": None}


def load_ingest_state(path: Path) -> Dict:
    return _load_json(
        path, _new_ingest_state(),
        validate=lambda d: isinstance(d, dict) and "files" in d,
    )


def save_ingest_state(path: Path, state: Dict) -> None:
    _atomic_json_write(path, state)


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

    # File shrunk → reprocess from start (defensive)
    if stat.st_size < stored_size:
        return True, 0

    # File grew → resume from byte_offset (JSONL only)
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


def ingest(ledger: Dict[str, Dict], new_entries: Dict[str, Dict]) -> int:
    """Merge new entries into the ledger.

    For existing entries, fills in any keys that are missing — this covers
    schema evolution (e.g., ``model`` / ``project`` / ``promptPreview`` were
    added after some entries were first written).

    Returns:
      Count of previously-unseen entries added.
    """
    added = 0
    for entry_id, entry_data in new_entries.items():
        if entry_id not in ledger:
            ledger[entry_id] = entry_data
            added += 1
        else:
            existing = ledger[entry_id]
            for k, v in entry_data.items():
                if k not in existing and v not in (None, ""):
                    existing[k] = v
    return added


def recalc_ledger_costs(ledger_path: Path, ledger: Dict[str, Dict],
                        dry_run: bool = False) -> Tuple[float, float, int]:
    """Recompute ``cost`` and ``cacheSavings`` for every entry using current rates.

    Writes the ledger to disk when any values change, unless ``dry_run`` is
    true. Entries with no token data (e.g. synthetic agent-spawn entries)
    are left alone.

    Args:
      ledger_path: Path used when writing the updated ledger.
      ledger: In-memory ledger; mutated in place when ``dry_run`` is false.
      dry_run: If true, skip the write.

    Returns:
      ``(old_total, new_total, entries_changed)``.
    """
    old_total = 0.0
    new_total = 0.0
    changed = 0

    for entry in ledger.values():
        ti = entry.get(FIELD_TOKENS_IN, 0)
        to = entry.get(FIELD_TOKENS_OUT, 0)
        cw = entry.get(FIELD_CACHE_WRITES, 0)
        cr = entry.get(FIELD_CACHE_READS, 0)
        model = entry.get('model')

        old_cost = entry.get(FIELD_COST, 0.0)
        old_total += old_cost

        if ti == 0 and to == 0 and cw == 0 and cr == 0:
            new_total += old_cost
            continue

        # Rescued/imported entries may lack a model field. Without it,
        # calculate_cost silently falls back to Sonnet pricing, which would
        # clobber correctly-priced historical costs. Skip them.
        if not model:
            new_total += old_cost
            continue

        new_cost = calculate_cost(ti, to, cw, cr, model)
        new_savings = calculate_cache_savings(cr, model)
        new_total += new_cost

        if new_cost != old_cost:
            if not dry_run:
                entry[FIELD_COST] = new_cost
                entry[FIELD_CACHE_SAVINGS] = new_savings
            changed += 1

    if not dry_run and changed > 0:
        save_ledger(ledger_path, ledger)

    return old_total, new_total, changed
