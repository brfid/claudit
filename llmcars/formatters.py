"""Field constants, number formatting, table rendering, and shared registries.

This module also owns two cross-cutting registries to avoid duplicating
them across `cli`, `aggregation`, `collectors`, and `tui`:

- `SOURCES`  — the set of supported data sources (Claude Code, Cline).
- `data_dir()` — single helper for the on-disk root; everything else
  derives from it so the package rename lands in one place.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

# Field name constants
FIELD_TOKENS_IN = 'tokensIn'
FIELD_TOKENS_OUT = 'tokensOut'
FIELD_CACHE_WRITES = 'cacheWrites'
FIELD_CACHE_READS = 'cacheReads'
FIELD_COST = 'cost'
FIELD_CACHE_SAVINGS = 'cacheSavings'
FIELD_REQUESTS = 'requests'

FIELD_NAMES = [
    FIELD_TOKENS_IN, FIELD_TOKENS_OUT, FIELD_CACHE_WRITES,
    FIELD_CACHE_READS, FIELD_COST, FIELD_CACHE_SAVINGS, FIELD_REQUESTS,
]

FLOAT_FIELDS = {FIELD_COST, FIELD_CACHE_SAVINGS}


# ── Source registry ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Source:
    """One supported data source.

    Attributes:
      cli_name: Value accepted by ``--source`` (``cline``, ``claude-code``).
      short_name: Ledger ``source`` field value (``cline``, ``cc``).
      display_name: Human-readable label (``Cline``, ``Claude Code``).
      aliases: Extra CLI strings that should resolve to ``cli_name``
        (e.g. ``cc`` for ``claude-code``).
    """
    cli_name: str
    short_name: str
    display_name: str
    aliases: tuple = ()


SOURCES: List[Source] = [
    Source("cline",       "cline", "Cline"),
    Source("claude-code", "cc",    "Claude Code", aliases=("cc",)),
]

# Derived lookups
SOURCE_MAP: Dict[str, str] = {s.cli_name: s.short_name for s in SOURCES}
SOURCE_DISPLAY: Dict[str, str] = {s.short_name: s.display_name for s in SOURCES}
SOURCE_ALIASES: Dict[str, str] = {
    alias: s.cli_name for s in SOURCES for alias in s.aliases
}
SOURCE_CLI_CHOICES: List[str] = ["all"] + [s.cli_name for s in SOURCES]


def resolve_source_cli(value: str) -> str:
    """Normalize a ``--source`` CLI value, expanding known aliases."""
    return SOURCE_ALIASES.get(value, value)


# ── Data-dir helper ───────────────────────────────────────────────────────

APP_NAME = "llmcars"
# Legacy dir we'll auto-migrate from on first run.
LEGACY_APP_NAMES = ("claudit",)


def _xdg_data_home() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg)
    return Path.home() / ".local" / "share"


def data_dir(create: bool = True) -> Path:
    """Return the on-disk data root for llmcars.

    If an old ``claudit`` data dir exists and the new one doesn't, migrate
    it by renaming. Prints a single-line notice. Idempotent.
    """
    base = _xdg_data_home()
    new_dir = base / APP_NAME
    if not new_dir.exists():
        for legacy in LEGACY_APP_NAMES:
            legacy_dir = base / legacy
            if legacy_dir.exists():
                try:
                    legacy_dir.rename(new_dir)
                    print(f"Migrated data: {legacy_dir} → {new_dir}")
                    break
                except OSError:
                    # Cross-device or permission issue; leave both alone.
                    pass
    if create:
        new_dir.mkdir(parents=True, exist_ok=True)
    return new_dir


def cache_dir(create: bool = True) -> Path:
    """Return the on-disk cache root for llmcars (holds the TUI pidfile)."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    new_dir = base / APP_NAME
    if not new_dir.exists():
        for legacy in LEGACY_APP_NAMES:
            legacy_dir = base / legacy
            if legacy_dir.exists():
                try:
                    legacy_dir.rename(new_dir)
                    print(f"Migrated cache: {legacy_dir} → {new_dir}")
                    break
                except OSError:
                    pass
    if create:
        new_dir.mkdir(parents=True, exist_ok=True)
    return new_dir


# ── Field-dict + formatting helpers ───────────────────────────────────────

def init_field_dict() -> Dict:
    """Initialize a dictionary with zeros for all field names."""
    return {field: 0.0 if field in FLOAT_FIELDS else 0 for field in FIELD_NAMES}


def format_number(num: int) -> str:
    """Format large numbers with K, M, B suffixes (whole numbers)."""
    if num >= 1_000_000_000:
        return f"{round(num / 1_000_000_000)}B"
    if num >= 1_000_000:
        return f"{round(num / 1_000_000)}M"
    if num >= 1_000:
        return f"{round(num / 1_000)}K"
    return f"{num:,}"


def format_cost(amount: float) -> str:
    """Format dollar amount: $0.52 under $1, $134 at/above."""
    if abs(amount) < 1.0:
        return f"${amount:,.2f}"
    return f"${amount:,.0f}"


def _fmt_int(x) -> str:
    return format_number(int(x))


# Column configuration: (field_name, header, width, format_func)
COLUMNS = [
    ('date', 'Date', 12, str),
    ('requests', 'Reqs', 8, _fmt_int),
    ('tokensIn', 'In (tok)', 10, _fmt_int),
    ('tokensOut', 'Out (tok)', 10, _fmt_int),
    ('cacheWrites', 'CW (tok)', 10, _fmt_int),
    ('cacheReads', 'CR (tok)', 10, _fmt_int),
    ('cacheSavings', 'Saved ($)', 10, format_cost),
    ('cost', 'Cost ($)', 10, format_cost),
]


def format_tokens(num: int, compact: bool = False) -> str:
    """Format token counts with MTok/KTok suffixes.

    compact=True rounds to whole numbers (for chart axes).
    Default shows one decimal when non-integer (for stat boxes and tables).
    """
    for threshold, divisor, suffix in [
        (1_000_000_000, 1_000_000_000, " GTok"),
        (1_000_000, 1_000_000, " MTok"),
        (1_000, 1_000, " KTok"),
    ]:
        if num >= threshold:
            value = num / divisor
            if compact or value == int(value):
                return f"{round(value)}{suffix}"
            return f"{value:.1f}{suffix}"
    return f"{num:,}"


def format_table_header() -> str:
    parts = []
    for field, header, width, _ in COLUMNS:
        if field == 'date':
            parts.append(f"{header:<{width}}")
        else:
            parts.append(f"{header:>{width}}")
    return " ".join(parts)


def format_table_row(date: str, data: Dict) -> str:
    parts = []
    for field, _, width, format_func in COLUMNS:
        if field == 'date':
            parts.append(f"{date:<{width}}")
        else:
            value = data.get(field, 0)
            formatted = format_func(value)
            parts.append(f"{formatted:>{width}}")
    return " ".join(parts)


def calculate_totals(daily_data: Dict[str, Dict]) -> Dict:
    totals = init_field_dict()
    for data in daily_data.values():
        for field in FIELD_NAMES:
            totals[field] += data.get(field, 0)
    return totals


def calculate_averages(totals: Dict, num_days: int) -> Dict:
    if num_days == 0:
        return init_field_dict()
    averages = init_field_dict()
    for field in FIELD_NAMES:
        averages[field] = totals[field] / num_days
    return averages
