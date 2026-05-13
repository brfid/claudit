# llmcars stability contract

External tools (notably `techdocs-hub/llm-lab/footer/cc-footer.cjs`) **vendor** small pieces of llmcars rather than import it. This file documents the API shapes those embedders rely on so we don't break them silently when refactoring internals.

If you change anything listed here, bump the **STABLE_VERSION** below and update the pinned commit hash in every embedder's header.

```text
STABLE_VERSION = 1
```

## Scope

Stable for embedders means:

- Field names and types under the headings below won't change without a STABLE_VERSION bump.
- Internal module layout, function signatures, return shapes of helpers, CLI flags, and dashboard output **are not stable**. Don't `import llmcars.<x>`; copy the small piece you need.

Out of scope (deliberately not stable):

- `llmcars/collectors.py` function names and call sites.
- `llmcars/dashboard/` rendering and HTML.
- `llmcars/cli.py` flags and positional args.
- `pyproject.toml` extras / optional deps.

## Stable: pricing dictionary shape

Source: `llmcars/llmcars/pricing.py`.

Per-family pricing is a dict with these four keys, USD per **million** tokens:

```python
{
    "input":       float,  # base prompt tokens
    "output":      float,  # generated tokens
    "cache_write": float,  # cache_creation_input_tokens
    "cache_read":  float,  # cache_read_input_tokens
}
```

Family keys (lowercase, no provider prefix): `opus`, `sonnet`, `haiku`. Other Anthropic family keys may be added over time; existing keys won't change name.

Embedder cost formula (must match):

```
cost_usd = (input_tokens       * pricing.input
          + output_tokens      * pricing.output
          + cache_creation_in  * pricing.cache_write
          + cache_read_in      * pricing.cache_read) / 1_000_000
```

Current numeric values are **not** stable across releases (vendors will copy them at a pinned commit). The *shape* and the *formula* are.

## Stable: provider-prefix strip regex

Source: `llmcars/llmcars/collectors.py`.

To resolve a Bedrock model id (e.g. `us.anthropic.claude-opus-4-7`) to a pricing family, strip a leading `<region>.<provider>.` prefix and substring-match the remainder for `opus` / `sonnet` / `haiku`:

```
^(us|global|eu|apac|apne|apse)\.(anthropic|openai|amazon|meta|mistral|cohere|ai21|google|deepseek)\.
```

The list of regions and providers is append-only; existing entries won't be removed without a STABLE_VERSION bump.

## Stable: Claude Code transcript JSONL shape (read path only)

llmcars reads the transcripts Claude Code writes at `~/.claude/projects/**/*.jsonl` (incl. `*/subagents/*.jsonl`). Embedders that walk these files rely on:

```jsonc
{
  "type": "assistant",          // filter on this
  "timestamp": "2026-...Z",     // ISO-8601 UTC; Date.parse-compatible
  "message": {
    "model": "us.anthropic.claude-opus-4-7",   // pass through prefix-strip + family match
    "usage": {
      "input_tokens":                <int>,
      "output_tokens":               <int>,
      "cache_creation_input_tokens": <int>,
      "cache_read_input_tokens":     <int>
    }
  }
}
```

Records with `type != "assistant"` or no `message.usage` are skipped. Other top-level fields (`uuid`, `parentUuid`, `sessionId`, ...) are not stable for embedders — don't depend on them.

This is upstream Claude Code's format, not llmcars' invention. If Anthropic changes it, we ship a new STABLE_VERSION and embedders re-pin.

## Stable: Claude Code statusLine stdin shape (read path only)

The dict Claude Code feeds to a `statusLine` command on stdin. Embedders rely on these fields:

```jsonc
{
  "model":          { "display_name": "Opus 4.7",
                      "id":           "us.anthropic.claude-opus-4-7" },
  "context_window": { "used_percentage": 3 },        // integer 0..100
  "cost":           { "total_cost_usd": 0.27 }       // float, current session
}
```

All other fields (`session_id`, `transcript_path`, `cwd`, `workspace`, `version`, `output_style`, `effort`, `thinking`, ...) are not stable. Embedders should `JSON.parse` and degrade gracefully on missing fields.

This is also upstream's format. Same versioning rule.

## How embedders should pin

A vendoring embedder must:

1. Copy the constants/regex/formula it needs into its own source file.
2. Put a header comment naming this file and the **commit hash** of llmcars at copy time.
3. Reference STABLE_VERSION in that comment.
4. On any drift between vendored copy and current llmcars, update the copy and bump the pin in the same commit.

Example (from `cc-footer.cjs`):

```text
// Cost math + provider-prefix stripping vendored from llmcars.
// Source of truth: ~/src/llmcars (commit <full sha>)
// Stability: llmcars/STABILITY.md, STABLE_VERSION = 1.
```

## Known embedders

- `techdocs-hub/llm-lab/footer/cc-footer.cjs` — Claude Code statusLine renderer for the workshop `claude` container.

When you add another, append it here.
