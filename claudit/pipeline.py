"""Ingest pipeline orchestration.

Coordinates collectors with ledger persistence. This sits above the
collectors layer because it owns cross-source decisions (which collectors
to invoke based on `source`) and ledger lifecycle (load state → collect →
merge → save).

Gap-triggered deep rescan: if more than `max_gap_hours` have passed since
the last successful ingest, per-file incremental offsets are discarded and
every discoverable session file is re-parsed from byte zero. Dedup by
msg_id in `ledger.ingest()` makes this safe. This is the safety net against
Claude Code's session cleanup — as long as you run claudit at least once
per CC retention window, nothing is lost.
"""

from pathlib import Path
from typing import Dict, Optional

from .collectors import collect_claude_code_data, collect_cline_data
from .ledger import (
    _new_ingest_state,
    get_ingest_state_path,
    hours_since_last_ingest,
    ingest,
    load_ingest_state,
    prune_orphan_file_state,
    rotate_backup,
    save_ingest_state,
    save_ledger,
    stamp_ingest,
)

DEFAULT_MAX_GAP_HOURS = 24.0


def run_ingest(ledger_path: Path, ledger: Dict, source: str = "all",
               no_ingest: bool = False, force_ingest: bool = False,
               verbose: bool = False, quiet: bool = False,
               max_gap_hours: Optional[float] = None,
               deep: bool = False) -> int:
    """Run the ingest pipeline.

    Args:
      ledger_path: Path to the on-disk ledger.
      ledger: In-memory ledger, mutated with new entries.
      source: ``"all"``, ``"cline"``, or ``"claude-code"``.
      no_ingest: Skip the pipeline entirely (report from stored data).
      force_ingest: Legacy ``--rescan``. Starts from a fresh ingest state.
      verbose: Emit per-collector progress from the collectors layer.
      quiet: Suppress pipeline-level status messages (gap/prune notices).
      max_gap_hours: If the previous successful ingest was more than this
        many hours ago, auto-promote to a deep rescan. ``None`` uses
        ``DEFAULT_MAX_GAP_HOURS``.
      deep: Re-parse every file from byte 0 while keeping existing ledger
        entries. Dedup in :func:`ledger.ingest` keeps the result consistent.

    Returns:
      Count of new ledger entries added.
    """
    if no_ingest:
        return 0

    gap_limit = DEFAULT_MAX_GAP_HOURS if max_gap_hours is None else max_gap_hours
    state_path = get_ingest_state_path(ledger_path)

    existing_state = load_ingest_state(state_path)
    gap = hours_since_last_ingest(existing_state)

    gap_triggered = False
    if not deep and not force_ingest and gap is not None and gap > gap_limit:
        deep = True
        gap_triggered = True

    if force_ingest or not ledger:
        ingest_state = _new_ingest_state()
        mode = "rescan"
    elif deep:
        ingest_state = _new_ingest_state()
        mode = "deep"
    else:
        ingest_state = existing_state
        mode = "incremental"

    if not quiet and (gap_triggered or deep or mode == "rescan"):
        if gap_triggered:
            print(f"Deep rescan: {gap:.1f}h since last ingest "
                  f"(threshold {gap_limit:.0f}h)")
        elif mode == "rescan":
            print("Full rescan: ignoring stored state")
        elif deep:
            print("Deep rescan: re-parsing all files")

    # Back up the ledger before we touch it. Idempotent per day — safe to
    # call every run. Gives a rollback window if an ingest corrupts entries.
    rotate_backup(ledger_path)

    new_entries = {}
    if source in ('all', 'cline'):
        new_entries.update(collect_cline_data(verbose, ingest_state=ingest_state))
    if source in ('all', 'claude-code'):
        new_entries.update(collect_claude_code_data(verbose, ingest_state=ingest_state))

    added = ingest(ledger, new_entries)
    if added > 0:
        save_ledger(ledger_path, ledger)

    # Drop stale file-state entries (files Claude Code has since deleted).
    # Only safe during deep/rescan modes — in incremental mode we never visit
    # dead files so their offsets are harmless.
    pruned = 0
    if mode in ("deep", "rescan"):
        pruned = prune_orphan_file_state(ingest_state)

    stamp_ingest(ingest_state)
    save_ingest_state(state_path, ingest_state)

    if not quiet and pruned > 0:
        print(f"Pruned {pruned} orphan file-state entries")

    return added
