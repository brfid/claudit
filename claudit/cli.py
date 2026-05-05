"""CLI argument parsing, ledger stats, and main entry point."""

import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .aggregation import aggregate_by_day, compute_date_window, entry_local_dt, format_output
from .formatters import SOURCE_MAP
from .ledger import (
    backup_dir,
    get_ingest_state_path,
    get_ledger_path,
    hours_since_last_ingest,
    load_ingest_state,
    load_ledger,
    recalc_ledger_costs,
)
from .pipeline import run_ingest


def _source_alias(value: str) -> str:
    """Normalize source argument, accepting 'cc' as alias for 'claude-code'."""
    aliases = {'cc': 'claude-code'}
    return aliases.get(value, value)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Track AI coding assistant costs (Cline + Claude Code).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  claudit                       # Interactive dashboard (default)
  claudit --report              # Text report, last 30 active days
  claudit --report --days 7     # Last 7 active days
  claudit --report --all        # All days with activity
  claudit --source cline        # Dashboard, Cline only
  claudit --source claude-code  # Dashboard, Claude Code only
  claudit --cached              # Skip scanning live sources
  claudit --rescan              # Rescan all files from scratch
  claudit --deep                # Re-parse every file (dedups; data-safe)
  claudit --stats               # Print ledger stats and exit
  claudit --recalc --dry-run    # Preview cost correction without writing
  claudit --recalc              # Rewrite ledger costs using current rates
        """
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        '--report', action='store_true',
        help='print text report instead of launching dashboard'
    )
    mode.add_argument(
        '--stats', action='store_true',
        help='print ledger stats (entry count, size, date range) and exit'
    )
    mode.add_argument(
        '--recalc', action='store_true',
        help='recompute cost/savings in ledger using current rates, then exit'
    )
    parser.add_argument(
        '--days', type=int, default=30, metavar='N',
        help='number of most recent active days to display (default: 30)'
    )
    parser.add_argument(
        '--all', action='store_true',
        help='show all days with activity (overrides --days)'
    )
    parser.add_argument(
        '--source', type=_source_alias,
        choices=['all', 'cline', 'claude-code'], default='all',
        help='data source (default: all)'
    )
    parser.add_argument(
        '--project', metavar='SUBSTRING',
        help='filter to entries whose project path matches this substring'
    )
    parser.add_argument(
        '--from', dest='date_from', metavar='YYYY-MM-DD',
        help='include only entries on or after this date'
    )
    parser.add_argument(
        '--to', dest='date_to', metavar='YYYY-MM-DD',
        help='include only entries on or before this date'
    )
    parser.add_argument(
        '--cached', action='store_true',
        help='report from stored data only, skip scanning live sources'
    )
    parser.add_argument(
        '--rescan', action='store_true',
        help='rescan all source files from scratch, ignoring stored state'
    )
    parser.add_argument(
        '--ledger-path', metavar='PATH',
        help='override default ledger file location'
    )
    parser.add_argument(
        '--quiet', '-q', action='store_true',
        help='suppress all status messages'
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='show detailed source discovery and error information'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='with --recalc: show what would change without writing to disk'
    )
    parser.add_argument(
        '--deep', action='store_true',
        help='re-parse every session file from byte 0 (keeps ledger entries; '
             'dedups by msg_id). Use when you suspect missed data.'
    )
    parser.add_argument(
        '--max-gap-hours', type=float, default=24.0, metavar='H',
        help='if last ingest was more than H hours ago, auto-promote to '
             'deep rescan (default: 24)'
    )
    parser.add_argument(
        '--force', action='store_true',
        help='override the single-instance lock and launch the TUI even '
             'if another claudit TUI appears to be running'
    )
    return parser.parse_args()


def _format_bytes(size_bytes: int) -> str:
    """Format a byte count as '12 B' / '1.5 KB' / '3.2 MB'."""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def print_ledger_stats(ledger_path: Path, ledger: Dict[str, Dict]) -> None:
    """Print ledger file stats — size, entry counts, date range, projects."""
    try:
        size_bytes = ledger_path.stat().st_size
    except OSError:
        size_bytes = 0

    size_str = _format_bytes(size_bytes)

    by_source: Dict[str, int] = {}
    by_project: Dict[str, int] = {}
    min_dt: Optional[datetime] = None
    max_dt: Optional[datetime] = None
    subagent_count = 0

    for entry in ledger.values():
        src = entry.get('source', '?')
        by_source[src] = by_source.get(src, 0) + 1
        proj = entry.get('project', '')
        if proj:
            by_project[proj] = by_project.get(proj, 0) + 1
        if entry.get('isSubagent'):
            subagent_count += 1
        try:
            dt = entry_local_dt(entry)
            if min_dt is None or dt < min_dt:
                min_dt = dt
            if max_dt is None or dt > max_dt:
                max_dt = dt
        except (ValueError, KeyError):
            continue

    print(f"Ledger file: {ledger_path}")
    print(f"Size:        {size_str}")
    print(f"Entries:     {len(ledger):,}")
    for src, count in sorted(by_source.items(), key=lambda x: x[1], reverse=True):
        print(f"  {src:<12} {count:,}")
    if subagent_count:
        print(f"  subagents   {subagent_count:,} (subset of cc)")
    if min_dt and max_dt:
        print(f"Range:       {min_dt.strftime('%Y-%m-%d')} → {max_dt.strftime('%Y-%m-%d')}")
    if by_project:
        print(f"Projects:    {len(by_project)}")
        for proj, count in sorted(by_project.items(), key=lambda x: x[1], reverse=True)[:10]:
            print(f"  {count:>6,}  {proj}")

    state = load_ingest_state(get_ingest_state_path(ledger_path))
    gap = hours_since_last_ingest(state)
    if gap is None:
        print("Last ingest: never")
    elif gap < 1:
        print(f"Last ingest: {int(gap * 60)}m ago")
    elif gap < 48:
        print(f"Last ingest: {gap:.1f}h ago")
    else:
        print(f"Last ingest: {gap / 24:.1f}d ago")

    bdir = backup_dir(ledger_path)
    backups = sorted(bdir.glob("ledger-*.json")) if bdir.exists() else []
    if backups:
        print(f"Backups:     {len(backups)} in {bdir}")
        print(f"  latest     {backups[-1].name}")


def main():
    args = parse_arguments()

    if args.dry_run and not args.recalc:
        print("--dry-run only applies with --recalc")
        return 2

    ledger_path = get_ledger_path(args.ledger_path)
    ledger = load_ledger(ledger_path)

    if args.recalc:
        dry = args.dry_run
        old, new, changed = recalc_ledger_costs(ledger_path, ledger, dry_run=dry)
        label = "[dry run] " if dry else ""
        print(f"{label}Entries changed: {changed:,}")
        print(f"{label}Old total: ${old:,.2f}")
        print(f"{label}New total: ${new:,.2f}")
        print(f"{label}Difference: ${new - old:,.2f}")
        if not dry and changed > 0:
            print(f"Ledger written to {ledger_path}")
        return 0

    if args.stats:
        print_ledger_stats(ledger_path, ledger)
        return 0

    if not args.report:
        try:
            from claudit.tui import CostTrackerApp
        except ImportError:
            print("TUI requires: pip install 'claudit[tui]'")
            return 1
        from claudit.pidfile import AlreadyRunning, single_instance
        try:
            with single_instance(force=args.force):
                app = CostTrackerApp(
                    ledger_path_override=args.ledger_path,
                    source_filter=args.source,
                    no_ingest=args.cached,
                    force_ingest=args.rescan,
                )
                app.run()
        except AlreadyRunning as exc:
            print(str(exc))
            return 1
        return 0

    added = run_ingest(ledger_path, ledger, source=args.source,
                       no_ingest=args.cached, force_ingest=args.rescan,
                       verbose=args.verbose, quiet=args.quiet,
                       max_gap_hours=args.max_gap_hours, deep=args.deep)
    if added > 0 and not args.quiet:
        print(f"Ledger: {added} new entries ({len(ledger)} total)")

    source_filter = None if args.source == 'all' else SOURCE_MAP.get(args.source, args.source)

    limit_days = None if args.all else args.days
    date_from = args.date_from or compute_date_window(limit_days)
    date_to = args.date_to

    daily_data = aggregate_by_day(ledger, source_filter=source_filter,
                                  date_from=date_from, date_to=date_to,
                                  project_filter=args.project)

    sources_in_data = set()
    for entry in ledger.values():
        src = entry.get('source')
        if source_filter is None or src == source_filter:
            sources_in_data.add(src)

    source_labels = {'cc': 'Claude Code', 'cline': 'Cline'}
    title_parts = [source_labels.get(s, s) for s in sorted(sources_in_data)]
    title = (" + ".join(title_parts) + " " if title_parts else "") + "Daily Cost Summary"

    if not args.quiet:
        print()

    output = format_output(daily_data, limit_days=limit_days, title=title)
    print(output)

    return 0
