"""Ledger aggregation and CLI output formatting."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from .formatters import (
    FIELD_NAMES, FIELD_REQUESTS, calculate_averages, calculate_totals,
    format_table_header, format_table_row, init_field_dict,
)


def entry_local_dt(entry: Dict) -> datetime:
    """Parse an entry timestamp into local-time ``datetime``.

    Claude Code entries are stored UTC-naive and converted here. Cline
    entries are already in local time.
    """
    dt = datetime.fromisoformat(entry['ts'])
    if entry.get('source') in ('cc', 'agent_spawn'):
        dt = dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return dt


def aggregate_by_day(ledger: Dict[str, Dict],
                     source_filter: Optional[str] = None,
                     date_from: Optional[str] = None,
                     date_to: Optional[str] = None,
                     project_filter: Optional[str] = None) -> Dict[str, Dict]:
    """Aggregate ledger entries by local-time day.

    Args:
      ledger: Ledger entries keyed by entry id.
      source_filter: If set, keep only entries whose ``source`` matches.
      date_from: Inclusive lower bound, ``YYYY-MM-DD`` local time.
      date_to: Inclusive upper bound, ``YYYY-MM-DD`` local time.
      project_filter: Case-insensitive substring matched against
        ``entry["project"]``.

    Returns:
      ``{day_key: totals_dict}`` with one entry per active day.
    """
    daily_data = defaultdict(init_field_dict)
    proj_needle = project_filter.lower() if project_filter else None

    for entry in ledger.values():
        if source_filter and entry.get('source') != source_filter:
            continue

        if proj_needle and proj_needle not in entry.get('project', '').lower():
            continue

        try:
            dt = entry_local_dt(entry)
        except (ValueError, KeyError):
            continue

        day_key = dt.strftime('%Y-%m-%d')

        if date_from and day_key < date_from:
            continue
        if date_to and day_key > date_to:
            continue

        day = daily_data[day_key]

        for field in FIELD_NAMES:
            if field == FIELD_REQUESTS:
                continue
            day[field] += entry.get(field, 0)

        day[FIELD_REQUESTS] += entry.get(FIELD_REQUESTS, 1)

    return dict(daily_data)


def compute_date_window(days: Optional[int]) -> Optional[str]:
    """Compute a ``date_from`` string that covers ``days`` active days.

    Over-fetches by 2x to allow for inactive days inside the window.

    Returns:
      ``YYYY-MM-DD`` string, or ``None`` if ``days`` is ``None``.
    """
    if days is None:
        return None
    buffer_days = max(days * 2, days + 30)
    cutoff = datetime.now() - timedelta(days=buffer_days)
    return cutoff.strftime('%Y-%m-%d')


def format_output(daily_data: Dict[str, Dict], limit_days: Optional[int] = None,
                  title: str = "Daily Cost Summary") -> str:
    if not daily_data:
        return "No cost data found."

    sorted_days = sorted(daily_data.keys())
    if limit_days is not None and len(sorted_days) > limit_days:
        sorted_days = sorted_days[-limit_days:]

    lines = []
    if limit_days is None:
        lines.append(f"{title} (All {len(sorted_days)} Active Days)")
    else:
        lines.append(f"{title} (Last {len(sorted_days)} Active Days)")
    lines.append("")
    lines.append(format_table_header())

    lines.extend(format_table_row(day, daily_data[day]) for day in sorted_days)

    lines.append("")
    totals = calculate_totals({day: daily_data[day] for day in sorted_days})
    lines.append(format_table_row("TOTAL", totals))

    averages = calculate_averages(totals, len(sorted_days))
    lines.append(format_table_row("AVERAGE", averages))

    return "\n".join(lines)
