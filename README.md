# claudit

Tracks Claude API spend across Claude Code and Cline. Reads session files written by each tool, deduplicates against a local ledger, and reports cost and token breakdowns.

## Prerequisites

- Python 3.9+
- At least one supported source:
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (CLI)
  - [Cline](https://github.com/cline/cline) (VS Code extension)

For the interactive dashboard:

- [textual](https://pypi.org/project/textual/)
- [textual-plotext](https://pypi.org/project/textual-plotext/)

## Install

```bash
git clone https://github.com/brfid/claudit.git
cd claudit
pip install -e .
```

After cloning, install the pre-commit hook to block accidental commits of runtime data:

```bash
./scripts/install-hooks.sh
```

To include dashboard dependencies:

```bash
pip install -e '.[tui]'
```

## Get started

Run with no arguments to see the last 30 active days across all sources:

```bash
claudit
```

Launch the interactive dashboard:

```bash
claudit --tui
```

## Usage

### Filter by time range

```bash
claudit --days 7                               # Last 7 active days
claudit --all                                  # All days with activity
claudit --from 2026-04-01 --to 2026-04-30     # Specific range
```

`--from` / `--to` are inclusive ISO dates. When either is set, it overrides `--days`.

### Filter by source

```bash
claudit --source cline         # Cline only
claudit --source claude-code   # Claude Code only
```

### Filter by project

Case-insensitive substring match against the project path stored per entry:

```bash
claudit --project techdocs-tools
claudit --project dotfiles --days 60
```

### Inspect the ledger

```bash
claudit --stats
```

Prints file size, entry counts by source, date range, top projects, last ingest time, and backup count.

### Control scanning behavior

By default each run scans live data incrementally (unchanged files skipped, growing JSONL files resumed from the last byte offset). You can override:

```bash
claudit --cached            # Skip scanning, report from stored data only
claudit --rescan            # Ignore stored state, rescan all files from byte 0
claudit --deep              # Re-parse every session file; keeps ledger entries, deduplicates
claudit --max-gap-hours 12  # Auto-promote to deep rescan if last ingest was >12h ago (default: 24)
```

Use `--deep` when you suspect missed sessions. Use `--rescan` after recovering from a corrupt or missing ingest state.

### Recalculate costs

If Anthropic updates rates, recalculate stored costs against the current pricing table:

```bash
claudit --recalc --dry-run  # Preview changes without writing
claudit --recalc            # Rewrite costs in the ledger
```

### Other options

| Flag | Description |
|---|---|
| `--verbose`, `-v` | Show source discovery and error details |
| `--quiet`, `-q` | Suppress status messages |
| `--ledger-path PATH` | Use a different ledger file location |

## Dashboard

Launch with `claudit --tui`.

### Tabs

Number keys `1`–`0` switch tabs:

| Key | Tab | Contents |
|---|---|---|
| `1` | OVERVIEW | Six stat boxes with sparklines (today, this week, 30-day, tokens, cache, burn rate) |
| `2` | DAILY | Daily cost line chart |
| `3` | CUMULATIVE | Running total cost |
| `4` | CALENDAR | 13-week cost heatmap (GitHub-style) |
| `5` | TOKENS | Input/output/cache bar chart |
| `6` | CACHE | Savings vs efficiency |
| `7` | REQUESTS | 13-week activity heatmap |
| `8` | COST MAP | Cost by hour × day-of-week |
| `9` | CALLS | Per-call cost distribution histogram |
| `0` | OPS | Live session stats, project breakdown, model mix, call log |

### OPS tab

The OPS tab shows today's session activity:

- **Session stats** — calls, cost, $/hr rate, cache efficiency, token totals
- **Per-call distribution** — median, P95, max call cost
- **Hourly heat strip** — 24-character braille sparkline of cost by hour
- **Active projects** — top 6 with cost bars
- **Model mix** — per-family color coding (Opus, Sonnet, Haiku)
- **Stop reasons** — counts of `end_turn`, `tool_use`, `max_tokens`, etc.
- **Call log** — 100 most recent calls with time, model, tokens, cache, cost bar, project, prompt preview

New entries from auto-refresh are flagged with `★` and shown in bold. Subagent calls are marked with `↳`.

### Controls

| Key | Action |
|---|---|
| `q` | Quit |
| `r` | Toggle auto-refresh (default: on, 30s) |
| `?` | Show help |
| `j` / `k` | Scroll down / up one line |
| `J` / `K` | Scroll ten lines |
| `ctrl+d` / `ctrl+u` | Page down / up |
| `g` / `G` | Jump to top / bottom |
| `]` / `[` | Next / previous tab |
| `enter` | Expand most recent call (modal with full details) |
| `1`–`0` | Switch tabs |

The status bar shows entry count, active days, refresh state, and a `+N new` badge when auto-refresh finds new entries.

## How it works

On each run, claudit:

1. Scans session data from Cline and Claude Code in parallel.
2. Deduplicates entries against a local `ledger.json` by entry ID.
3. Back-fills missing fields on existing entries (handles schema evolution).
4. Reports cost and token breakdowns from the ledger.

Scanning is incremental — unchanged files are skipped and growing JSONL files resume from the last byte offset. State is stored in `ingest_state.json`.

If more than `--max-gap-hours` have elapsed since the last ingest (default: 24h), claudit auto-promotes to a deep rescan, re-parsing every session file from byte zero. Dedup by `msg_id` keeps the result consistent. This is the safety net against Claude Code's session cleanup window — as long as you run claudit at least once per that window, no data is lost.

The ledger is backed up daily to `backups/ledger-YYYY-MM-DD.json` (7 copies retained). To roll back, copy a backup over `ledger.json`.

### Claude Code entry fields

Each ingested Claude Code call stores:

- `source`, `ts`, `model` — provenance and timestamp
- `project` — decoded filesystem path (greedy resolver handles dashes in real directory names)
- `session` — JSONL filename stem
- `isSubagent` — `true` if the session file is under `*/subagents/*`
- `stopReason` — `end_turn`, `tool_use`, `max_tokens`, etc.
- `promptPreview` — first 80 chars of the preceding user message (tool results excluded)
- Token/cost fields: `tokensIn`, `tokensOut`, `cacheWrites`, `cacheReads`, `cost`, `cacheSavings`

### Pricing

Pricing is per model family, based on published Anthropic API rates (USD per million tokens):

| Family | Input | Output | Cache write | Cache read |
|---|---|---|---|---|
| Opus | 5.00 | 25.00 | 6.25 | 0.50 |
| Sonnet | 3.00 | 15.00 | 3.75 | 0.30 |
| Haiku | 1.00 | 5.00 | 1.25 | 0.10 |

Exact model IDs are matched first (see `MODEL_PRICING` in `pricing.py`). Unknown IDs fall back to family inference (`claude-opus-*` → Opus pricing). Unrecognized names fall back to Sonnet pricing. Verify rates against [anthropic.com/pricing](https://www.anthropic.com/pricing) if you suspect drift. Use `--recalc` to update stored costs after a rate change.

### Data safety

- **Survives session cleanup** — once ingested, entries persist in the ledger indefinitely.
- **No double-counting** — re-scanning the same data is safe; dedup is by entry ID.
- **Non-destructive** — claudit never modifies source data.

## Data files

All files are stored in `~/.local/share/claudit/` by default.

| File | Description |
|---|---|
| `ledger.json` | All ingested API call records, keyed by unique entry ID |
| `ingest_state.json` | Per-file byte offsets for incremental scanning (safe to delete) |
| `backups/ledger-YYYY-MM-DD.json` | Daily ledger snapshots, last 7 retained |

## Tests

```bash
cd tools/claudit
pytest tests/ -v
```

Covers: incremental ingest, file-state tracking, date/project/source filtering, model pricing fallback, prompt-preview extraction, schema-evolution back-fill, project-path resolution, subagent detection, gap-triggered deep rescan, backup rotation, orphan ingest-state cleanup.

## Platform support

macOS, Windows, and Linux. Auto-detects OS-appropriate data directories for each source.
