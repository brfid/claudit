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
python3 claudit.py --days 7     # Last 7 active days
python3 claudit.py --all         # All days with activity
```

### Filter by source

```bash
python3 claudit.py --source cline        # Cline only
python3 claudit.py --source claude-code   # Claude Code only
```

### Control scanning behavior

Each run scans live data from your AI assistants and merges it into a local ledger. You can control this:

```bash
python3 claudit.py --cached    # Skip scanning, report from stored data only
python3 claudit.py --rescan    # Ignore stored state, rescan everything
```

### Other options

| Flag | Description |
|---|---|
| `--verbose`, `-v` | Show source discovery and error details |
| `--quiet`, `-q` | Suppress status messages |
| `--ledger-path PATH` | Use a different ledger file location |

## How it works

On each run, the tracker:

1. Scans session data from Cline and/or Claude Code.
2. Deduplicates entries against a local `ledger.json` using unique entry IDs.
3. Reports cost and token breakdowns from the ledger.

Scanning is incremental — unchanged files are skipped, and growing JSONL files resume from the last byte offset. State is stored in `ingest_state.json`.

### Data safety

- **Survives cache removal** — once ingested, entries persist in the ledger.
- **No double-counting** — re-scanning the same data is safe.
- **Non-destructive** — the tracker never modifies source data.

## Data files

| File | Description |
|---|---|
| `ledger.json` | All ingested API call records, keyed by unique entry ID |
| `ingest_state.json` | File processing cursors for incremental scanning (safe to delete) |

## Dashboard

The `--tui` flag launches an interactive terminal dashboard. Visual theme inspired by [LCARS](https://en.wikipedia.org/wiki/LCARS).

## Platform support

macOS, Windows, and Linux. Auto-detects OS-appropriate data directories for each source.
