"""Pure data reductions for the OPS dashboard.

These functions take a ledger (or a projection of it) and return primitive
Python types. No Textual, no I/O, no UI state. Keeping them here makes them
trivially testable and reusable across tabs.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .aggregation import entry_local_dt
from .formatters import (
    FIELD_CACHE_READS,
    FIELD_CACHE_SAVINGS,
    FIELD_CACHE_WRITES,
    FIELD_COST,
    FIELD_TOKENS_IN,
    FIELD_TOKENS_OUT,
)
from .pricing import FAMILIES, FAMILIES_BY_KEY, family_for_model

# Single source of truth: model family → color. Built from the pricing
# registry so new families are picked up automatically.
MODEL_COLORS: Dict[str, str] = {fam.key: fam.color for fam in FAMILIES}

# Default color for families we don't yet recognize at all.
_UNKNOWN_MODEL_COLOR = "#CC9966"

# Short display codes for common Claude Code tool names
TOOL_ABBREV = {
    "Read": "R", "Write": "W", "Edit": "E",
    "Bash": "$", "Grep": "g", "Glob": "G", "LS": "l",
    "Task": "T", "Agent": "T",
    "WebFetch": "w", "WebSearch": "s",
    "TodoWrite": "✓", "TodoRead": "✓",
    "NotebookEdit": "N",
    "Skill": "K",
}


# ── Entry filtering and sorting ───────────────────────────────────────────

def collect_entries(ledger: Dict,
                    source_filter: Optional[str]) -> List[Tuple]:
    """Return [(dt, entry_id, entry), ...] sorted newest-first."""
    out = []
    for entry_id, entry in ledger.items():
        if source_filter and entry.get("source") != source_filter:
            continue
        try:
            dt = entry_local_dt(entry)
        except (ValueError, KeyError):
            continue
        out.append((dt, entry_id, entry))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def hourly_cost_today(entries: List[Tuple]) -> List[float]:
    """Return 24 floats: today's cost per hour at index 0..23."""
    today = datetime.now().strftime("%Y-%m-%d")
    hours = [0.0] * 24
    for dt, _, e in entries:
        if dt.strftime("%Y-%m-%d") == today:
            hours[dt.hour] += e.get(FIELD_COST, 0)
    return hours


def percentile(sorted_values: List[float], pct: float) -> float:
    """Return the value at percentile `pct` (∈ [0, 1])."""
    if not sorted_values:
        return 0
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * pct))
    return sorted_values[idx]


def aggregate_today(entries: List[Tuple], short_project_fn,
                    short_model_fn) -> Dict:
    """One-pass aggregation of today's entries into a stats dict.

    short_project_fn / short_model_fn are injected so aggregation has no
    implicit coupling to display helpers.

    Agent-spawn entries (source='agent_spawn') are tallied separately into
    `subagent_type_counts` and skipped for cost/token aggregation so they
    don't inflate billable totals.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    s = {
        "count": 0, "cost": 0.0, "tokens_in": 0, "tokens_out": 0,
        "savings": 0.0, "costs": [],
        "subagent_count": 0, "subagent_cost": 0.0,
        "project_cost": defaultdict(float),
        "model_counts": Counter(),
        "stop_counts": Counter(),
        "subagent_type_counts": Counter(),
        "spawn_count": 0,
        "first_dt": None,
    }
    for dt, _, e in entries:
        if dt.strftime("%Y-%m-%d") != today:
            continue
        if e.get("source") == "agent_spawn":
            s["subagent_type_counts"][e.get("subagentType") or "(none)"] += 1
            s["spawn_count"] += 1
            continue
        cost = e.get(FIELD_COST, 0)
        s["count"] += 1
        s["cost"] += cost
        s["tokens_in"] += e.get(FIELD_TOKENS_IN, 0)
        s["tokens_out"] += e.get(FIELD_TOKENS_OUT, 0)
        s["savings"] += e.get(FIELD_CACHE_SAVINGS, 0)
        if cost > 0:
            s["costs"].append(cost)
        if e.get("isSubagent"):
            s["subagent_count"] += 1
            s["subagent_cost"] += cost
        s["project_cost"][short_project_fn(e.get("project", ""))] += cost
        s["model_counts"][short_model_fn(e.get("model"))] += 1
        s["stop_counts"][e.get("stopReason") or "—"] += 1
        if s["first_dt"] is None or dt < s["first_dt"]:
            s["first_dt"] = dt
    s["costs"].sort()
    return s


def subagent_cost_rollup(entries: List[Tuple]) -> Dict[str, float]:
    """Return {parent_session_uuid: total_subagent_cost} for today.

    Used to attribute subagent-session cost back to the parent prompt.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    out: Dict[str, float] = defaultdict(float)
    for dt, _, e in entries:
        if dt.strftime("%Y-%m-%d") != today:
            continue
        if not e.get("isSubagent"):
            continue
        parent = e.get("parentSession") or ""
        if parent:
            out[parent] += e.get(FIELD_COST, 0)
    return dict(out)


# ── Display-string helpers ────────────────────────────────────────────────

def _extract_version_suffix(tail: str) -> str:
    """Pick out leading ``.``-joined numbers in a model-ID tail.

    e.g. ``"4-5-20250929"`` → ``"4.5"`` (drops the date stamp).
    Returns ``""`` if no leading digit group is present.
    """
    nums: List[str] = []
    for part in tail.split("-"):
        if part.isdigit() and len(part) < 4:
            nums.append(part)
        else:
            break
    return ".".join(nums)


def short_model(model: Optional[str]) -> str:
    """Shorten a full model ID to family-version form.

    Anthropic:
      claude-opus-4-6            → opus-4.6
      claude-sonnet-4-5-20250929 → sonnet-4.5

    OpenAI / others:
      gpt-5-5                    → gpt-5.5
      gpt-5-nano                 → gpt-5-nano
      openai.gpt-5-mini          → gpt-5-mini (after normalization upstream)
      us.amazon.nova-pro-v1      → nova (family only; version parsing is best-effort)

    Unrecognized IDs fall back to the first dash-segment.
    """
    if not model:
        return "—"
    fam = family_for_model(model)
    if fam is None:
        return model.split("-", 1)[0] if "-" in model else model

    # For the GPT-5 sub-families (nano/mini), the key already includes the
    # qualifier — don't strip it, just return as-is.
    if fam.key in ("gpt-5-nano", "gpt-5-mini"):
        return fam.key

    # For claude-*, we prefer the dot-joined version suffix.
    if fam.key in ("opus", "sonnet", "haiku"):
        prefix = f"claude-{fam.key}-"
        if model.startswith(prefix):
            ver = _extract_version_suffix(model[len(prefix):])
            return f"{fam.key}-{ver}" if ver else fam.key
        return fam.key

    # For gpt-5/gpt-4/etc, try to extract a trailing version too.
    # e.g. "gpt-5-5" → "gpt-5.5"; "gpt-5" alone → "gpt-5".
    token = fam.tokens[0]
    idx = model.lower().find(token)
    if idx >= 0:
        tail = model[idx + len(token):].lstrip("-")
        ver = _extract_version_suffix(tail)
        return f"{fam.key}.{ver}" if ver else fam.key
    return fam.key


def model_color(short: str) -> str:
    """Return the LLMCARS hex color for a shortened model name's family.

    Accepts either a full short name (``gpt-5.5``) or a bare family key
    (``gpt-5``). Falls back to ``_UNKNOWN_MODEL_COLOR`` for unknown families.
    """
    # Try progressively shorter prefixes so "gpt-5-nano" matches "gpt-5-nano"
    # before falling back to "gpt-5".
    if short in MODEL_COLORS:
        return MODEL_COLORS[short]
    # Drop the last dotted/dashed segment and retry (e.g. "gpt-5.5" → "gpt-5")
    for sep in (".", "-"):
        if sep in short:
            head = short.rsplit(sep, 1)[0]
            if head in MODEL_COLORS:
                return MODEL_COLORS[head]
    # Final fallback: first hyphen-separated segment
    head = short.split("-", 1)[0]
    return MODEL_COLORS.get(head, _UNKNOWN_MODEL_COLOR)


def short_project(project: str) -> str:
    """Return the last path component of a project path, or `—`."""
    if not project:
        return "—"
    parts = project.rstrip("/").split("/")
    return parts[-1] if parts else project


def short_tools(tools: List[str]) -> str:
    """Compact tool-use display: 'R E $' with count suffix on repeats."""
    if not tools:
        return ""
    seen = []
    counts: Dict[str, int] = {}
    for t in tools:
        ab = TOOL_ABBREV.get(t, (t[:3] if not t[0].isalpha() else t[0].upper()))
        if ab not in counts:
            seen.append(ab)
        counts[ab] = counts.get(ab, 0) + 1
    return "".join(
        f"{c}×{counts[c]}" if counts[c] > 1 else c
        for c in seen
    )


def cost_bar(cost: float, max_cost: float, width: int = 8) -> str:
    """Return a fixed-width block bar representing `cost` vs `max_cost`."""
    if max_cost <= 0:
        return " " * width
    filled = int(min(cost / max_cost, 1.0) * width)
    return "▓" * filled + "░" * (width - filled)


def _escape_markup(s: str) -> str:
    """Escape `[` so user data can't inject Textual markup."""
    return s.replace("[", chr(92) + "[")


def _tool_chain(tools: List[str], limit: int = 6) -> str:
    """Render a tool sequence compactly: 'Read → Edit×3 → Bash'."""
    if not tools:
        return ""
    runs: List[Tuple[str, int]] = []
    for t in tools:
        if runs and runs[-1][0] == t:
            runs[-1] = (t, runs[-1][1] + 1)
        else:
            runs.append((t, 1))
    parts = [f"{n}×{c}" if c > 1 else n for n, c in runs[:limit]]
    if len(runs) > limit:
        parts.append(f"+{len(runs) - limit}")
    return " → ".join(parts)


def row_activity_text(entry: Dict) -> str:
    """Activity description for a call-log row.

    Priority ladder (first match wins):
      1. User prompt (promptPreview) — actual captured text
      2. Agent spawn — show subagent type + description
      3. Tool chain — sequence of tools used in this turn
      4. Stop-reason detail — cache volume, subagent flag
      5. Session tail — last resort

    Every branch escapes user-supplied text so `[` can't inject Textual markup.
    """
    if entry.get("source") == "agent_spawn":
        stype = _escape_markup(entry.get("subagentType") or "(none)")
        desc = _escape_markup(entry.get("description") or "")
        short_type = stype.split(":")[-1] if ":" in stype else stype
        if desc:
            return f"[#CC99CC]↳ spawn[/] [#FFCC99]{short_type}[/] [dim]— {desc}[/]"
        return f"[#CC99CC]↳ spawn[/] [#FFCC99]{short_type}[/]"

    raw = (entry.get("promptPreview") or "").replace("\n", " ").replace("\t", " ")
    clean = " ".join(raw.split())
    if clean:
        return f"[dim]»[/] {_escape_markup(clean)}"

    tools = entry.get("tools") or []
    if tools:
        chain = _escape_markup(_tool_chain(tools))
        return f"[dim]⚒[/] [#CC9966]{chain}[/]"

    bits = []
    stop = entry.get("stopReason")
    if stop and stop != "end_turn":
        bits.append(f"stop={stop}")
    cr = entry.get(FIELD_CACHE_READS, 0)
    cw = entry.get(FIELD_CACHE_WRITES, 0)
    if cr or cw:
        bits.append(f"cache {_format_k(cr)}r/{_format_k(cw)}w")
    if entry.get("isSubagent"):
        bits.append("subagent turn")
    if bits:
        return f"[dim]· {' · '.join(bits)}[/]"

    sess = entry.get("session") or ""
    if sess:
        return f"[dim]· session {sess[:8]}[/]"
    return "[dim]· —[/]"


def _format_k(num: int) -> str:
    """Tiny helper: 12345 → 12K. Avoids pulling format_number into this module."""
    if num >= 1_000_000:
        return f"{num // 1_000_000}M"
    if num >= 1_000:
        return f"{num // 1_000}K"
    return str(num)


# ── Derived OPS view ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class RowSpec:
    """One call-log row's data. Rendered into markup by the TUI layer."""
    dt: datetime
    entry_id: str
    entry: Dict
    is_anchor: bool
    is_turn_end: bool
    is_subagent: bool
    spawn_cost: float
    max_log_cost: float


LOG_ROW_CAP = 100


def build_row_specs(entries: List[Tuple], max_log_cost: float) -> List[RowSpec]:
    """Compute per-row data specs once; reused for in-place selection updates.

    Each spec describes one renderable call-log row (agent_spawn entries are
    skipped; they're attributed via the subagent cost rollup).

    Anchor detection: a row is an "anchor" if it's the first turn of a user
    prompt (by promptId), or — for entries that lack a promptId — if it's a
    session-head or end_turn row. Anchors get subagent cost rollups attached.
    """
    visible = entries[:LOG_ROW_CAP]

    seen_prompt_ids: set = set()
    seen_sessions_with_anchor: set = set()
    sessions_with_end_turn = set()
    for _, _, e in visible:
        if (e.get("stopReason") or "") == "end_turn":
            sess = e.get("session") or ""
            if sess:
                sessions_with_end_turn.add(sess)

    sub_rollup = subagent_cost_rollup(entries)

    specs: List[RowSpec] = []
    for dt, entry_id, e in visible:
        if e.get("source") == "agent_spawn":
            continue

        prompt_id = e.get("promptId") or ""
        session = e.get("session") or ""
        stop = e.get("stopReason") or ""

        if prompt_id:
            is_anchor = prompt_id not in seen_prompt_ids
            if is_anchor:
                seen_prompt_ids.add(prompt_id)
            is_turn_end = stop == "end_turn"
        else:
            is_turn_end = stop == "end_turn"
            is_session_head = (
                session
                and session not in sessions_with_end_turn
                and session not in seen_sessions_with_anchor
            )
            is_anchor = is_turn_end or is_session_head
            if is_anchor and session:
                seen_sessions_with_anchor.add(session)

        spawn_cost = 0.0
        if is_anchor and session and session in sub_rollup:
            spawn_cost = sub_rollup[session]

        specs.append(RowSpec(
            dt=dt,
            entry_id=entry_id,
            entry=e,
            is_anchor=is_anchor,
            is_turn_end=is_turn_end,
            is_subagent=bool(e.get("isSubagent")),
            spawn_cost=spawn_cost,
            max_log_cost=max_log_cost,
        ))
    return specs


@dataclass(frozen=True)
class OpsView:
    """Pre-computed numbers the OPS dashboard displays.

    Single source of truth for both the initial render and in-place updates,
    so the two code paths can't drift.
    """
    stats: Dict                 # raw aggregate_today result
    today_cost: float
    rate_per_hr: float
    cache_eff: float            # percentage (0..100)
    median_cost: float
    p95_cost: float
    max_call_cost: float
    hour_cost: List[float]      # 24 floats


def derive_ops_view(entries: List[Tuple], stats: Dict,
                    now: Optional[datetime] = None) -> OpsView:
    """Compute derived OPS numbers from `(entries, stats)` in one place."""
    now = now or datetime.now()
    if stats["first_dt"]:
        elapsed_hrs = max((now - stats["first_dt"]).total_seconds() / 3600, 0.01)
        rate_per_hr = stats["cost"] / elapsed_hrs
    else:
        rate_per_hr = 0.0
    potential = stats["cost"] + stats["savings"]
    cache_eff = (stats["savings"] / potential * 100) if potential > 0 else 0.0
    costs = stats["costs"]
    return OpsView(
        stats=stats,
        today_cost=stats["cost"],
        rate_per_hr=rate_per_hr,
        cache_eff=cache_eff,
        median_cost=percentile(costs, 0.5),
        p95_cost=percentile(costs, 0.95),
        max_call_cost=costs[-1] if costs else 0.0,
        hour_cost=hourly_cost_today(entries),
    )
