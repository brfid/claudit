# claudit

Tracks Claude API spend across Claude Code and Cline.

## Prerequisites

- Python 3.9+
- At least one supported AI coding assistant:
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (CLI)
  - [Cline](https://github.com/cline/cline) (VS Code extension)

For the interactive dashboard:

- [textual](https://pypi.org/project/textual/)
- [textual-plotext](https://pypi.org/project/textual-plotext/)

## Install

No package installation required. Clone the repo and run directly:

```bash
python3 claudit.py
```

To install dashboard dependencies:

```bash
pip install textual textual-plotext
```

Optional: add a shell alias for quick access:

```bash
alias claudit="python3 /path/to/claudit.py"
```

## Get started

Run with no arguments to see the last 30 active days across all sources:

```bash
python3 claudit.py
```

Launch the interactive dashboard:

```bash
python3 claudit.py --tui
```

## Usage

### Filter by time range

```bash
python3 claudit.py --days 7                        # Last 7 active days
python3 claudit.py --all                           # All days with activity
python3 claudit.py --from 2026-04-01 --to 2026-04-30  # Specific range
```

`--from` / `--to` are inclusive ISO dates. When either is set, it overrides the `--days` window.

### Filter by source

```bash
python3 claudit.py --source cline         # Cline only
python3 claudit.py --source claude-code   # Claude Code only
```

### Filter by project

Case-insensitive substring match against the project path stored per entry:

```bash
python3 claudit.py --project techdocs-tools
python3 claudit.py --project dotfiles --days 60
```

### Inspect the ledger

```bash
python3 claudit.py --stats
```

Prints file size, entry counts by source, date range, and top projects.

### Control scanning behavior

Each run scans live data from your AI assistants and merges it into a local ledger. You can control this:

```bash
python3 claudit.py --cached    # Skip scanning, report from stored data only
python3 claudit.py --rescan    # Ignore stored state, rescan everything
```

`--rescan` is useful after schema changes — missing fields on old entries (such as `model`, `project`, `promptPreview`, `isSubagent`, `session`) are back-filled without overwriting existing values.

### Other options

| Flag | Description |
|---|---|
| `--verbose`, `-v` | Show source discovery and error details |
| `--quiet`, `-q` | Suppress status messages |
| `--ledger-path PATH` | Use a different ledger file location |

## Dashboard

The `--tui` flag launches an interactive terminal dashboard. Visual theme inspired by [LCARS](https://en.wikipedia.org/wiki/LCARS).

### Tabs

Number keys `1`–`0` switch tabs:

| Key | Tab | Contents |
|---|---|---|
| `1` | OVERVIEW | Six stat boxes with sparklines (today, this week, 30-day, tokens, cache, burn rate) |
| `2` | DAILY | Daily cost line chart |
| `3` | CUMULATIVE | Running total cost |
| `4` | CALENDAR | 365-day cost heatmap (GitHub-style) |
| `5` | TOKENS | Input/output/cache bar chart |
| `6` | CACHE | Savings vs efficiency |
| `7` | REQUESTS | 365-day activity heatmap |
| `8` | COST MAP | Cost by hour × day-of-week |
| `9` | CALLS | Per-call cost distribution histogram |
| `0` | OPS | Live session stats, project breakdown, model mix, call log |

### OPS tab

The OPS tab shows today's session activity:

- **Session stats** — calls, cost, $/hr rate, cache efficiency, token totals
- **Per-call distribution** — median, P95, max call cost
- **Hourly heat strip** — 24-character braille sparkline of cost distribution by hour
- **Active projects** — top 6 with cost bars
- **Model mix** — colored per-family (opus=amber, sonnet=periwinkle, haiku=mauve)
- **Stop reasons** — counts of `end_turn`, `tool_use`, `max_tokens`, etc.
- **Call log** — 100 most recent calls with time, model, tokens, cache, cost bar, project, prompt preview

New entries from auto-refresh are flagged with `★` and shown in bold peach. Subagent calls are marked with `↳`.

### Controls

| Key | Action |
|---|---|
| `q` | Quit |
| `r` | Toggle auto-refresh (default: on, 30s) |
| `j` / `k` | Scroll down / up one line |
| `J` / `K` | Scroll ten lines |
| `enter` | Expand most recent call (modal with full details) |
| `1`–`0` | Switch tabs |

The status bar shows entry count, active days, refresh state, and a `+N new` badge when auto-refresh finds new entries.

## How it works

On each run, the tracker:

1. Scans session data from Cline and/or Claude Code in parallel.
2. Deduplicates entries against a local `ledger.json` using unique entry IDs.
3. Back-fills missing fields on existing entries (schema evolution).
4. Reports cost and token breakdowns from the ledger.

Scanning is incremental — unchanged files are skipped, and growing JSONL files resume from the last byte offset. State is stored in `ingest_state.json`.

### Claude Code entry fields

Each ingested Claude Code call stores:

- `source`, `ts`, `model` — provenance and timestamp
- `project` — decoded filesystem path (greedy resolver handles dashes in real directory names)
- `session` — JSONL filename stem
- `isSubagent` — `true` if the session file is under `*/subagents/*`
- `stopReason` — `end_turn`, `tool_use`, `max_tokens`, etc.
- `promptPreview` — first 80 chars of the preceding user text message (skips tool results)
- Token/cost fields: `tokensIn`, `tokensOut`, `cacheWrites`, `cacheReads`, `cost`, `cacheSavings`

### Pricing

Pricing is per model family, based on published Anthropic API rates (USD per million tokens):

| Family | Input | Output | Cache write (5m) | Cache read |
|---|---|---|---|---|
| Opus | 15.00 | 75.00 | 18.75 | 1.50 |
| Sonnet | 3.00 | 15.00 | 3.75 | 0.30 |
| Haiku | 1.00 | 5.00 | 1.25 | 0.10 |

Exact model IDs are matched first (see `MODEL_PRICING` in `claudit.py`). Unknown IDs fall back to family inference (`claude-opus-*` → Opus pricing). Unrecognized names fall back to Sonnet pricing. Verify rates against [anthropic.com/pricing](https://www.anthropic.com/pricing) if you suspect drift. Existing ledger entries keep the cost computed at ingest time; to re-price retroactively you'd need to rescan and drop the ledger first.

### Data safety

- **Survives cache removal** — once ingested, entries persist in the ledger.
- **No double-counting** — re-scanning the same data is safe.
- **Non-destructive** — the tracker never modifies source data.

## Data files

| File | Description |
|---|---|
| `ledger.json` | All ingested API call records, keyed by unique entry ID |
| `ingest_state.json` | File processing cursors for incremental scanning (safe to delete) |

## Tests

```bash
cd tools/claudit
python3 -m pytest test_incremental.py -v
```

Covers: incremental ingest, file-state tracking, date/project/source filtering, model pricing fallback, prompt-preview extraction, schema-evolution back-fill, project-path resolution, subagent detection.

## Platform support

macOS, Windows, and Linux. Auto-detects OS-appropriate data directories for each source.
