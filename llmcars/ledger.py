"""Ledger file I/O, backup rotation, and cost recalculation.

The ledger is the durable cost record. Per-run bookkeeping (file seek
offsets, last-ingest timestamp) lives in `ingest_state.py`.

Several names from `ingest_state` are re-exported here for backwards
compatibility with callers that haven't migrated yet.
"""

import contextlib
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from ._json_io import atomic_json_write, load_json
from .formatters import (
    FIELD_CACHE_READS,
    FIELD_CACHE_SAVINGS,
    FIELD_CACHE_WRITES,
    FIELD_COST,
    FIELD_TOKENS_IN,
    FIELD_TOKENS_OUT,
    data_dir,
)
from .ingest_state import (  # noqa: F401 — re-exported for back-compat
    file_needs_processing,
    get_ingest_state_path,
    get_stored_user_text,
    hours_since_last_ingest,
    load_ingest_state,
    new_ingest_state,
    prune_orphan_file_state,
    save_ingest_state,
    stamp_ingest,
    update_file_state,
)
from .pricing import calculate_cache_savings, calculate_cost

BACKUP_RETAIN = 7


def get_ledger_path(override: Optional[str] = None) -> Path:
    if override:
        return Path(override)
    return data_dir() / "ledger.json"


def load_ledger(path: Path) -> Dict[str, Dict]:
    return load_json(path, {}, validate=lambda d: isinstance(d, dict))


def save_ledger(path: Path, ledger: Dict[str, Dict]) -> None:
    atomic_json_write(path, ledger)


def backup_dir(ledger_path: Path) -> Path:
    return ledger_path.parent / "backups"


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
        with contextlib.suppress(OSError):
            old.unlink()
    return dest


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
                        dry_run: bool = False
                        ) -> Tuple[float, float, int, int]:
    """Recompute ``cost`` and ``cacheSavings`` for every entry using current rates.

    Writes the ledger to disk when any values change, unless ``dry_run`` is
    true. Entries with no token data (e.g. synthetic agent-spawn entries)
    are left alone. Entries whose model has no configured pricing (returns
    ``None`` from ``calculate_cost``) are preserved as-is — we don't
    reprice a GPT-class entry with Sonnet rates.

    Args:
      ledger_path: Path used when writing the updated ledger.
      ledger: In-memory ledger; mutated in place when ``dry_run`` is false.
      dry_run: If true, skip the write.

    Returns:
      ``(old_total, new_total, entries_changed, entries_skipped)``. Skipped
      counts entries that had tokens + a model but no available rates.
    """
    old_total = 0.0
    new_total = 0.0
    changed = 0
    skipped = 0

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
        # we'd silently fall back to Sonnet pricing and clobber correctly
        # priced historical costs. Skip them.
        if not model:
            new_total += old_cost
            continue

        new_cost = calculate_cost(ti, to, cw, cr, model)
        if new_cost is None:
            # Family known, rates not configured (placeholder). Preserve
            # whatever cost the collector stored — for Cline that's the
            # provider-reported cost; for CC it's the last-known rate.
            new_total += old_cost
            skipped += 1
            continue

        new_savings = calculate_cache_savings(cr, model)
        new_total += new_cost

        if new_cost != old_cost:
            if not dry_run:
                entry[FIELD_COST] = new_cost
                entry[FIELD_CACHE_SAVINGS] = new_savings
            changed += 1

    if not dry_run and changed > 0:
        save_ledger(ledger_path, ledger)

    return old_total, new_total, changed, skipped
