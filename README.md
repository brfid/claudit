# llmcars

Tracks LLM API spend across Claude Code and Cline. Reads session files written by each tool, deduplicates against a local ledger, and reports cost and token breakdowns across multiple providers (Anthropic, OpenAI, Amazon Bedrock, Google, Meta, Mistral).

Styled after [LCARS](https://en.wikipedia.org/wiki/LCARS) — hence *LLMCARS*.

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
git clone https://github.com/brfid/llmcars.git
cd llmcars
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

### Migrating from `claudit`

The package was renamed from `claudit` to `llmcars`. On first run, llmcars automatically renames legacy data/cache dirs:

- `~/.local/share/claudit/` → `~/.local/share/llmcars/`
- `~/.cache/claudit/` → `~/.cache/llmcars/`

No manual migration needed. The old `claudit` CLI is gone — use `llmcars`.

## Get started

Run with no arguments to launch the interactive dashboard:

```bash
llmcars
```

Print a text report instead:

```bash
llmcars --report
```

## Usage

### Filter by time range

```bash
llmcars --report --days 7                              # Last 7 active days
llmcars --report --all                                 # All days with activity
llmcars --report --from 2026-04-01 --to 2026-04-30    # Specific range
```

`--from` / `--to` are inclusive ISO dates. When either is set, it overrides `--days`.

### Filter by source

```bash
llmcars --source cline         # Cline only
llmcars --source claude-code   # Claude Code only
```

### Filter by project

Case-insensitive substring match against the project path stored per entry:

```bash
llmcars --project techdocs-tools
llmcars --project dotfiles --days 60
```

### Inspect the ledger

```bash
llmcars --stats
```

Prints file size, entry counts by source, date range, top projects, last ingest time, and backup count.

### Control scanning behavior

By default each run scans live data incrementally (unchanged files skipped, growing JSONL files resumed from the last byte offset). You can override:

```bash
llmcars --cached            # Skip scanning, report from stored data only
llmcars --rescan            # Ignore stored state, rescan all files from byte 0
llmcars --deep              # Re-parse every session file; keeps ledger entries, deduplicates
llmcars --max-gap-hours 12  # Auto-promote to deep rescan if last ingest was >12h ago (default: 24)
```

Use `--deep` when you suspect missed sessions. Use `--rescan` after recovering from a corrupt or missing ingest state.

### Recalculate costs

If a provider updates rates, recalculate stored costs against the current pricing table:

```bash
llmcars --recalc --dry-run  # Preview changes without writing
llmcars --recalc            # Rewrite costs in the ledger
```

Entries whose model family has no configured rates (see [Pricing](#pricing) below) are **skipped** — their stored cost is preserved. `--recalc` prints a `skipped` count alongside `changed`.

### Other options

| Flag | Description |
|---|---|
| `--verbose`, `-v` | Show source discovery and error details |
| `--quiet`, `-q` | Suppress status messages |
| `--ledger-path PATH` | Use a different ledger file location |

## Dashboard

Launch with `llmcars` (the default mode).

### Tabs

Number keys `1`–`0` switch tabs:

| Key | Tab | Contents |
|---|---|---|
| `1` | OVERVIEW | Six stat boxes with sparklines (today, this week, 30-day, tokens, cache, burn rate) |
| `2` | DAILY | Daily cost line chart |
| `3` | CUMULATIVE | Running total cost |
| `4` | CALENDAR | 40-week cost heatmap (GitHub-style) |
| `5` | TOKENS | Input/output/cache bar chart |
| `6` | CACHE | Savings vs efficiency |
| `7` | REQUESTS | 40-week activity heatmap |
| `8` | COST MAP | Cost by hour × day-of-week |
| `9` | CALLS | Per-call cost distribution histogram |
| `0` | OPS | Live session stats, project breakdown, model mix, call log |

### OPS tab

The OPS tab shows today's session activity:

- **Session stats** — calls, cost, $/hr rate, cache efficiency, token totals
- **Per-call distribution** — median, P95, max call cost
- **Hourly heat strip** — 24-character braille sparkline of cost by hour
- **Active projects** — top 6 with cost bars
- **Model mix** — per-family color coding (Opus, Sonnet, Haiku, GPT-5, Nova, …)
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

On each run, llmcars:

1. Scans session data from Cline and Claude Code in parallel.
2. Deduplicates entries against a local `ledger.json` by entry ID.
3. Back-fills missing fields on existing entries (handles schema evolution).
4. Reports cost and token breakdowns from the ledger.

Scanning is incremental — unchanged files are skipped and growing JSONL files resume from the last byte offset. State is stored in `ingest_state.json`.

If more than `--max-gap-hours` have elapsed since the last ingest (default: 24h), llmcars auto-promotes to a deep rescan, re-parsing every session file from byte zero. Dedup by entry ID keeps the result consistent. This is the safety net against Claude Code's session cleanup window — as long as you run llmcars at least once per that window, no data is lost.

The ledger is backed up daily to `backups/ledger-YYYY-MM-DD.json` (7 copies retained). To roll back, copy a backup over `ledger.json`.

### Provider support

Cline reports model IDs with provider prefixes when routing through AWS Bedrock or other gateways (e.g. `us.anthropic.claude-opus-4-7`, `us.openai.gpt-5-5`, `us.amazon.nova-pro-v1`). The collector strips recognized prefixes (`anthropic`, `openai`, `amazon`, `meta`, `mistral`, `cohere`, `ai21`, `google`, `deepseek`) so the same entry matches regardless of the gateway.

Model family detection is substring-based with more-specific families listed first: `gpt-5-nano` matches before bare `gpt-5`. New families are added by appending to the `FAMILIES` list in `pricing.py`.

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

Pricing is per model family, keyed off a single `FAMILIES` registry in `llmcars/pricing.py`. Rates are USD per million tokens.

| Family | Input | Output | Cache write | Cache read | Source |
|---|---|---|---|---|---|
| Opus | 5.00 | 25.00 | 6.25 | 0.50 | [anthropic.com/pricing](https://www.anthropic.com/pricing) |
| Sonnet | 3.00 | 15.00 | 3.75 | 0.30 | [anthropic.com/pricing](https://www.anthropic.com/pricing) |
| Haiku | 1.00 | 5.00 | 1.25 | 0.10 | [anthropic.com/pricing](https://www.anthropic.com/pricing) |
| GPT-5 | 1.25 | 10.00 | 1.25 | 0.125 | [openai.com/pricing](https://openai.com/api/pricing) |
| GPT-5 mini | 0.25 | 2.00 | 0.25 | 0.025 | [openai.com/pricing](https://openai.com/api/pricing) |
| GPT-5 nano | 0.05 | 0.40 | 0.05 | 0.005 | [openai.com/pricing](https://openai.com/api/pricing) |
| GPT-4 | — | — | — | — | Placeholder (rates unset) |
| GPT (generic) | — | — | — | — | Placeholder |
| Nova | — | — | — | — | Placeholder |
| Gemini | — | — | — | — | Placeholder |
| Llama | — | — | — | — | Placeholder |
| Mistral | — | — | — | — | Placeholder |

**Cost-source policy.** Three branches:

1. **Exact model-ID hit** in `MODEL_PRICING` (e.g. `claude-sonnet-4-5-20250929`) — use those rates.
2. **Family match with rates** — use the family's rates.
3. **Family match, no rates** (placeholder) — `calculate_cost` returns `None`. Callers **must not** overwrite any existing cost. For Cline, the provider already reports a per-call cost inline (`api_req_started.cost`), so the ingested entry keeps that real value. For Claude Code entries on unpriced families, cost is left at whatever was first computed (typically 0).

This matters: **`--recalc` never silently repriceses a GPT or Nova entry with Sonnet rates.** Add real rates to `FAMILIES` in `pricing.py` to enable recompute.

Exact model IDs are matched first. Verify rates against each provider's pricing page if you suspect drift. Use `--recalc` to update stored costs after a rate change. AWS Bedrock may charge a small premium on top of the published direct-API rates; the tables above use direct-API numbers.

### Data safety

- **Survives session cleanup** — once ingested, entries persist in the ledger indefinitely.
- **No double-counting** — re-scanning the same data is safe; dedup is by entry ID.
- **Non-destructive** — llmcars never modifies source data.

## Data files

All files are stored in `~/.local/share/llmcars/` by default (`$XDG_DATA_HOME/llmcars/` if set).

| File | Description |
|---|---|
| `ledger.json` | All ingested API call records, keyed by unique entry ID |
| `ingest_state.json` | Per-file byte offsets for incremental scanning (safe to delete) |
| `backups/ledger-YYYY-MM-DD.json` | Daily ledger snapshots, last 7 retained |

Pidfile and caches live in `~/.cache/llmcars/` (`$XDG_CACHE_HOME/llmcars/` if set).

## Tests

```bash
cd llmcars
pytest tests/ -v
```

Covers: incremental ingest, file-state tracking, date/project/source filtering, model pricing fallback, prompt-preview extraction, schema-evolution back-fill, project-path resolution, subagent detection, gap-triggered deep rescan, backup rotation, orphan ingest-state cleanup, cross-provider model normalization, None-as-unpriced recalc semantics.

## Platform support

macOS, Windows, and Linux. Auto-detects OS-appropriate data directories for each source.
