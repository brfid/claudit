# Plan: multi-root scanning with provenance tagging

## Problem

Claudit only scans `~/.claude/projects/`. Tools that run Claude Code with an isolated `HOME` (build-trials, other sandboxed harnesses) write session JSONLs under a scratch tree like `.../runs/<trial>/_scratch_home/.claude/projects/`. That spend is invisible to claudit today.

Separately, trials run through AWS Bedrock rather than the Anthropic direct API. Token counts in the JSONL are correct; dollar cost computed from Anthropic rates is not.

## Goal

Track all Claude Code spend by default, regardless of which `HOME` produced the session, and tag each entry with enough provenance to reason about billing path and source.

## Design

### Discovery

Walk a short, bounded set of roots at ingest time looking for `**/.claude/projects/*.jsonl`:

- `~/.claude` — primary user root, always scanned.
- `~/src`, `~/Projects`, `~/code` — scanned if they exist.
- `$TMPDIR`, `/tmp` — catch runners that use `tempfile.mkdtemp`.

Bounded depth (~6 levels). Skip `node_modules`, `.venv`, `.git`, and hidden dirs other than `.claude`. Cache discovered roots in `scan_state.json`; re-discover on `--rescan` or gap-triggered deep rescan.

### Provenance fields on each ledger entry

- `scanRoot` — absolute path of the `.claude` dir the session came from.
- `scope` — `user` (primary home), `sandboxed` (discovered under a project tree), `other` (added by config).
- `costPath` — `anthropic`, `bedrock`, or `unknown`.
- `costPathSource` — `sniffed` (from SDK init message), `configured` (matched a rule), or `default`.

Existing entries back-fill to `scanRoot=~/.claude`, `scope=user`, `costPath=anthropic`, `costPathSource=default` on next ingest.

### Pricing

`pricing.py` branches on `costPath`:

- `anthropic` — current rates.
- `bedrock` — add Bedrock rate table.
- `unknown` — fall back to Anthropic rates, flag count in `--stats`.

### Optional config

`~/.local/share/claudit/scan_config.json` for overrides:

```json
{
  "extra_roots": ["~/work/experiments"],
  "ignore_roots": ["~/src/junk"],
  "cost_path_rules": [
    {"glob": "**/build-trials/**/_scratch_home/.claude", "cost_path": "bedrock"}
  ]
}
```

Rules applied in order, first match wins. `~/.claude` is always included and always `anthropic` unless explicitly overridden.

### Reports

Default `claudit --report` groups totals by `costPath`:

```
Anthropic (direct):  $42.18
Bedrock:              $8.04
Unknown:              $0.12
```

New filter: `--cost-path anthropic|bedrock|unknown`. `--stats` shows per-scan-root entry counts. OPS tab call log gets a provenance badge (e.g., `[BR]` for Bedrock).

## Key property

No coupling: sandboxed tools never import or configure claudit. Claudit finds their data by walking. Build-trials happens to write under `_scratch_home/.claude/` because the SDK honors `HOME`; claudit just notices.

## Risks

- Filesystem walks can be slow or hit permission errors. Cache discovered roots, bound depth, skip heavy dirs.
- Default roots assume a Unix layout. Windows and Linux defaults will need their own lists.
- Bedrock token-count accuracy is fine; dollar accuracy is approximate (Bedrock's real billing includes inference-profile discounts and fees not visible in the JSONL). A later tier can add a CloudWatch/CostExplorer collector for true spend.

## Scope

### In

- Discovery across default roots.
- Provenance fields on every entry.
- `anthropic` / `bedrock` / `unknown` pricing branch.
- Config file for overrides.
- Report grouping and filter by `costPath`.
- OPS badge for non-Anthropic paths.

### Out

- CloudWatch/CostExplorer collector for true Bedrock spend (future tier).
- Auto-tagging by provider SDK fingerprint beyond SDK init sniffing.
- Cross-tool correlation (matching a build-trial run to an Anthropic entry).
- Audit and coverage of additional Claude Code launch mechanisms (worktrees, background tasks, scheduled wakeups, plugin- or SDK-spawned sessions) that may write session data outside the scanned roots or in unexpected layouts.

## Effort

~250 LOC added across `collectors.py`, `pricing.py`, `cli.py`, `aggregation.py`, `formatters.py`, and tests.
