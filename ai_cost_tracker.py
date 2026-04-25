#!/usr/bin/env python3
"""
AI Cost Tracker - Parse Cline and Claude Code data to generate daily cost summaries.

Reads task/session data from Cline (VS Code extension) and/or Claude Code (CLI),
stores entries in a local ledger for persistence, and generates a daily breakdown
of API costs and token usage.
"""

import argparse
import json
import os
import platform
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Model-specific pricing (per million tokens)
# Adjust these if your Bedrock/API pricing differs
MODEL_PRICING = {
    'claude-opus-4-6': {
        'input': 5.00,
        'output': 25.00,
        'cache_write': 6.25,
        'cache_read': 0.50,
    },
    'claude-sonnet-4-5-20250929': {
        'input': 3.00,
        'output': 15.00,
        'cache_write': 3.75,
        'cache_read': 0.30,
    },
}

DEFAULT_PRICING = MODEL_PRICING['claude-sonnet-4-5-20250929']

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

# Column configuration: (field_name, header, width, format_func)
COLUMNS = [
    ('date', 'Date', 12, str),
    ('requests', 'Requests', 10, lambda x: format_number(int(x))),
    ('tokensIn', 'Tokens In', 12, lambda x: format_tokens(int(x))),
    ('tokensOut', 'Tokens Out', 12, lambda x: format_tokens(int(x))),
    ('cacheWrites', 'Cache W', 12, lambda x: format_tokens(int(x))),
    ('cacheReads', 'Cache R', 12, lambda x: format_tokens(int(x))),
    ('cacheSavings', 'Saved ($)', 10, lambda x: format_cost(x)),
    ('cost', 'Cost ($)', 10, lambda x: format_cost(x)),
]


def get_model_pricing(model: Optional[str]) -> Dict[str, float]:
    """Get pricing for a specific model, falling back to defaults."""
    if model and model in MODEL_PRICING:
        return MODEL_PRICING[model]
    return DEFAULT_PRICING


def calculate_cost(tokens_in: int, tokens_out: int, cache_writes: int,
                   cache_reads: int, model: Optional[str] = None) -> float:
    """Calculate cost from token counts and model pricing."""
    pricing = get_model_pricing(model)
    cost = (
        tokens_in * pricing['input'] +
        tokens_out * pricing['output'] +
        cache_writes * pricing['cache_write'] +
        cache_reads * pricing['cache_read']
    ) / 1_000_000
    return cost


def calculate_cache_savings(cache_reads: int, total_input: int,
                            model: Optional[str] = None) -> float:
    """Calculate cost savings from prompt caching."""
    if cache_reads == 0:
        return 0.0
    pricing = get_model_pricing(model)
    savings_per_million = pricing['input'] - pricing['cache_read']
    return (cache_reads * savings_per_million) / 1_000_000


def init_field_dict() -> Dict:
    """Initialize a dictionary with zeros for all field names."""
    return {field: 0.0 if field in FLOAT_FIELDS else 0 for field in FIELD_NAMES}


def format_number(num: int) -> str:
    """Format large numbers with K, M, B suffixes (whole numbers)."""
    if num >= 1_000_000_000:
        return f"{round(num / 1_000_000_000)}B"
    elif num >= 1_000_000:
        return f"{round(num / 1_000_000)}M"
    elif num >= 1_000:
        return f"{round(num / 1_000)}K"
    return f"{num:,}"


def format_cost(amount: float) -> str:
    """Format dollar amount: $0.52 under $1, $134 at/above."""
    if abs(amount) < 1.0:
        return f"${amount:,.2f}"
    return f"${amount:,.0f}"


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


# ---------------------------------------------------------------------------
# Ledger: persistent local record of all ingested API calls
# ---------------------------------------------------------------------------

def get_ledger_path(override: Optional[str] = None) -> Path:
    """Return path to the ledger file (next to this script by default)."""
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "ledger.json"


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


def _new_ingest_state() -> Dict:
    return {"_version": 1, "files": {}}


def load_ingest_state(path: Path) -> Dict:
    return _load_json(
        path, _new_ingest_state(),
        validate=lambda d: isinstance(d, dict) and "files" in d,
    )


def save_ingest_state(path: Path, state: Dict) -> None:
    _atomic_json_write(path, state)


def file_needs_processing(filepath: Path, state: Dict) -> Tuple[bool, int]:
    """Check if file needs processing. Returns (needs_processing, seek_offset)."""
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


def update_file_state(state: Dict, filepath: Path, byte_offset: int) -> None:
    try:
        stat = filepath.stat()
    except OSError:
        return
    state.setdefault("files", {})[str(filepath)] = {
        "byte_offset": byte_offset,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def ingest(ledger: Dict[str, Dict], new_entries: Dict[str, Dict]) -> int:
    """Merge new entries into ledger. Returns count of entries added."""
    added = 0
    for entry_id, entry_data in new_entries.items():
        if entry_id not in ledger:
            ledger[entry_id] = entry_data
            added += 1
    return added


def run_ingest(ledger_path: Path, ledger: Dict, source: str = "all",
               no_ingest: bool = False, force_ingest: bool = False,
               verbose: bool = False) -> int:
    """Run ingest pipeline, return count of new entries added."""
    if no_ingest:
        return 0

    state_path = get_ingest_state_path(ledger_path)
    if force_ingest or not ledger:
        ingest_state = _new_ingest_state()
    else:
        ingest_state = load_ingest_state(state_path)

    new_entries = {}
    if source in ('all', 'cline'):
        new_entries.update(collect_cline_data(verbose, ingest_state=ingest_state))
    if source in ('all', 'claude-code'):
        new_entries.update(collect_claude_code_data(verbose, ingest_state=ingest_state))

    added = ingest(ledger, new_entries)
    if added > 0:
        save_ledger(ledger_path, ledger)
    save_ingest_state(state_path, ingest_state)
    return added


# ---------------------------------------------------------------------------
# Cline data parsing
# ---------------------------------------------------------------------------

def get_cline_data_dir() -> Optional[Path]:
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        p = home / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"
    elif system == "Windows":
        p = home / "AppData" / "Roaming" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"
    elif system == "Linux":
        p = home / ".config" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"
    else:
        return None
    return p if p.exists() else None


def find_cline_task_directories(base_path: Path) -> List[Path]:
    tasks_dir = base_path / "tasks"
    if not tasks_dir.exists():
        return []
    return [d for d in tasks_dir.iterdir() if d.is_dir()]


def parse_ui_messages(task_dir: Path, verbose: bool = False) -> Tuple[List[Dict], bool]:
    ui_messages_file = task_dir / "ui_messages.json"
    if not ui_messages_file.exists():
        return [], True

    try:
        with open(ui_messages_file, 'r') as f:
            return json.load(f), True
    except (json.JSONDecodeError, IOError) as e:
        error_str = str(e)
        if "Expecting value: line 1 column 1" in error_str or "Unterminated string" in error_str:
            return [], True
        if verbose:
            print(f"Warning: Could not parse {ui_messages_file}: {e}")
        return [], False


def extract_cline_entries(messages: List[Dict], task_dir_name: str) -> Dict[str, Dict]:
    """Extract keyed cost entries from Cline UI messages."""
    entries = {}
    for msg in messages:
        if msg.get('say') != 'api_req_started':
            continue
        try:
            ts = msg.get('ts')
            if not ts:
                continue
            dt = datetime.fromtimestamp(ts / 1000.0)
            text_data = json.loads(msg.get('text', '{}'))
            cache_reads = text_data.get(FIELD_CACHE_READS, 0)
            total_input = (text_data.get(FIELD_TOKENS_IN, 0) +
                           text_data.get(FIELD_CACHE_WRITES, 0) +
                           cache_reads)

            entry_id = f"cline:{task_dir_name}:{int(ts)}"
            entries[entry_id] = {
                'source': 'cline',
                'ts': dt.isoformat(),
                FIELD_TOKENS_IN: text_data.get(FIELD_TOKENS_IN, 0),
                FIELD_TOKENS_OUT: text_data.get(FIELD_TOKENS_OUT, 0),
                FIELD_CACHE_WRITES: text_data.get(FIELD_CACHE_WRITES, 0),
                FIELD_CACHE_READS: cache_reads,
                FIELD_COST: text_data.get(FIELD_COST, 0.0),
                FIELD_CACHE_SAVINGS: calculate_cache_savings(cache_reads, total_input),
            }
        except (json.JSONDecodeError, ValueError, KeyError):
            continue
    return entries


def collect_cline_data(verbose: bool,
                      ingest_state: Optional[Dict] = None) -> Dict[str, Dict]:
    """Collect cost entries from Cline task directories."""
    cline_data_dir = get_cline_data_dir()
    if not cline_data_dir:
        if verbose:
            print("Cline: data directory not found, skipping")
        return {}

    task_dirs = find_cline_task_directories(cline_data_dir)
    if not task_dirs:
        if verbose:
            print("Cline: no task directories found")
        return {}

    all_entries = {}
    ok = 0
    fail = 0
    skipped = 0
    for task_dir in task_dirs:
        ui_file = task_dir / "ui_messages.json"
        if ingest_state is not None and ui_file.exists():
            needs, _ = file_needs_processing(ui_file, ingest_state)
            if not needs:
                skipped += 1
                continue

        messages, success = parse_ui_messages(task_dir, verbose=verbose)
        if success:
            ok += 1
        else:
            fail += 1
        all_entries.update(extract_cline_entries(messages, task_dir.name))

        if ingest_state is not None and ui_file.exists():
            try:
                stat = ui_file.stat()
                update_file_state(ingest_state, ui_file, stat.st_size)
            except OSError:
                pass

    if verbose:
        print(f"Cline: parsed {ok}/{len(task_dirs)} tasks ({skipped} skipped), "
              f"{len(all_entries)} API calls")
        if fail > 0:
            print(f"  Failed: {fail} tasks")

    return all_entries


# ---------------------------------------------------------------------------
# Claude Code data parsing
# ---------------------------------------------------------------------------

def get_claude_code_dir() -> Path:
    return Path.home() / ".claude"


def find_claude_code_session_files(base_path: Path) -> List[Path]:
    """Find all session JSONL files across all projects."""
    projects_dir = base_path / "projects"
    if not projects_dir.exists():
        return []
    return (
        list(projects_dir.glob("*/*.jsonl"))
        + list(projects_dir.glob("*/*/subagents/*.jsonl"))
    )


def parse_claude_code_session(session_file: Path, verbose: bool = False,
                              seek_offset: int = 0) -> Tuple[Dict[str, Dict], int]:
    """Parse a Claude Code session JSONL file, extracting final usage per API call.

    Returns (entries, final_byte_offset).
    """
    final_messages = {}
    last_good_offset = seek_offset

    try:
        with open(session_file, 'r') as f:
            if seek_offset > 0:
                f.seek(seek_offset)
            while True:
                line = f.readline()
                if not line:
                    break
                line_end = f.tell()
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                last_good_offset = line_end

                if obj.get('type') != 'assistant':
                    continue

                msg = obj.get('message', {})
                usage = msg.get('usage')
                if not usage:
                    continue

                msg_id = msg.get('id')
                if not msg_id:
                    continue

                stop_reason = msg.get('stop_reason')
                if stop_reason is not None:
                    final_messages[msg_id] = {
                        'model': msg.get('model'),
                        'usage': usage,
                        'timestamp': obj.get('timestamp'),
                    }

    except (IOError, OSError) as e:
        if verbose:
            print(f"Warning: Could not read {session_file}: {e}")
        return {}, seek_offset

    entries = {}
    for msg_id, data in final_messages.items():
        try:
            ts_str = data['timestamp']
            # Stored as UTC-naive; entry_local_dt() converts to local at read time
            dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00')).replace(tzinfo=None)
            usage = data['usage']
            model = data.get('model')

            tokens_in = usage.get('input_tokens', 0)
            tokens_out = usage.get('output_tokens', 0)
            cache_writes = usage.get('cache_creation_input_tokens', 0)
            cache_reads = usage.get('cache_read_input_tokens', 0)

            cost = calculate_cost(tokens_in, tokens_out, cache_writes, cache_reads, model)
            total_input = tokens_in + cache_writes + cache_reads

            entry_id = f"cc:{msg_id}"
            entries[entry_id] = {
                'source': 'cc',
                'ts': dt.isoformat(),
                FIELD_TOKENS_IN: tokens_in,
                FIELD_TOKENS_OUT: tokens_out,
                FIELD_CACHE_WRITES: cache_writes,
                FIELD_CACHE_READS: cache_reads,
                FIELD_COST: cost,
                FIELD_CACHE_SAVINGS: calculate_cache_savings(cache_reads, total_input, model),
            }
        except (ValueError, KeyError, TypeError):
            continue

    return entries, last_good_offset


def collect_claude_code_data(verbose: bool,
                            ingest_state: Optional[Dict] = None) -> Dict[str, Dict]:
    """Collect cost entries from all Claude Code sessions."""
    cc_dir = get_claude_code_dir()
    if not cc_dir.exists():
        if verbose:
            print("Claude Code: data directory not found, skipping")
        return {}

    session_files = find_claude_code_session_files(cc_dir)
    if not session_files:
        if verbose:
            print("Claude Code: no session files found")
        return {}

    all_entries = {}
    parsed = 0
    skipped = 0
    for sf in session_files:
        if ingest_state is not None:
            needs, offset = file_needs_processing(sf, ingest_state)
            if not needs:
                skipped += 1
                continue
        else:
            offset = 0

        entries, final_offset = parse_claude_code_session(
            sf, verbose=verbose, seek_offset=offset)
        all_entries.update(entries)
        parsed += 1

        if ingest_state is not None:
            update_file_state(ingest_state, sf, final_offset)

    if verbose:
        print(f"Claude Code: parsed {parsed} sessions ({skipped} skipped), "
              f"{len(all_entries)} API calls")

    return all_entries


# ---------------------------------------------------------------------------
# Aggregation and output
# ---------------------------------------------------------------------------


def entry_local_dt(entry: Dict) -> datetime:
    """Parse entry timestamp to local time.

    CC entries are stored as UTC-naive; Cline entries are already local.
    """
    dt = datetime.fromisoformat(entry['ts'])
    if entry.get('source') == 'cc':
        dt = dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return dt


def aggregate_by_day(ledger: Dict[str, Dict],
                     source_filter: Optional[str] = None,
                     date_from: Optional[str] = None,
                     date_to: Optional[str] = None) -> Dict[str, Dict]:
    """Aggregate ledger entries by day, optionally filtering by source and date range.

    date_from/date_to are ISO date strings (YYYY-MM-DD), inclusive.
    """
    daily_data = defaultdict(init_field_dict)

    for entry_id, entry in ledger.items():
        if source_filter and entry.get('source') != source_filter:
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

    for day in sorted_days:
        lines.append(format_table_row(day, daily_data[day]))

    lines.append("")
    totals = calculate_totals({day: daily_data[day] for day in sorted_days})
    lines.append(format_table_row("TOTAL", totals))

    averages = calculate_averages(totals, len(sorted_days))
    lines.append(format_table_row("AVERAGE", averages))

    return "\n".join(lines)


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
  ai_cost_tracker.py                       # Both sources, last 30 active days
  ai_cost_tracker.py --days 7              # Last 7 active days
  ai_cost_tracker.py --all                 # All days with activity
  ai_cost_tracker.py --source cline        # Cline only
  ai_cost_tracker.py --source claude-code  # Claude Code only
  ai_cost_tracker.py --tui                 # Interactive dashboard
  ai_cost_tracker.py --cached              # Report from stored data, skip scanning
  ai_cost_tracker.py --rescan              # Rescan all files from scratch
        """
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
        '--tui', action='store_true',
        help='launch interactive dashboard (requires textual, textual-plotext)'
    )
    return parser.parse_args()


def compute_date_window(days: Optional[int]) -> Optional[str]:
    """Compute a date_from string that covers enough history for N display days.

    Over-fetches by 2x to account for inactive days in the range.
    """
    if days is None:
        return None
    buffer_days = max(days * 2, days + 30)
    cutoff = datetime.now() - timedelta(days=buffer_days)
    return cutoff.strftime('%Y-%m-%d')


def main():
    args = parse_arguments()

    if args.tui:
        try:
            import importlib.util
            _tui_path = Path(__file__).resolve().parent / "tui.py"
            _spec = importlib.util.spec_from_file_location("tui", _tui_path)
            _tui = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_tui)
            CostTrackerApp = _tui.CostTrackerApp
        except ImportError:
            print("TUI requires: pip install textual textual-plotext")
            return 1
        app = CostTrackerApp(
            ledger_path_override=args.ledger_path,
            source_filter=args.source,
            no_ingest=args.cached,
            force_ingest=args.rescan,
        )
        app.run()
        return 0

    ledger_path = get_ledger_path(args.ledger_path)
    ledger = load_ledger(ledger_path)

    added = run_ingest(ledger_path, ledger, source=args.source,
                       no_ingest=args.cached, force_ingest=args.rescan,
                       verbose=args.verbose)
    if added > 0 and not args.quiet:
        print(f"Ledger: {added} new entries ({len(ledger)} total)")

    source_map = {'claude-code': 'cc', 'cline': 'cline'}
    source_filter = None if args.source == 'all' else source_map.get(args.source, args.source)

    limit_days = None if args.all else args.days
    date_from = compute_date_window(limit_days)

    daily_data = aggregate_by_day(ledger, source_filter=source_filter,
                                  date_from=date_from)

    # Build title from what's actually in the filtered data
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


if __name__ == "__main__":
    exit(main())
