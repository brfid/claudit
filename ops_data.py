"""Pure data reductions for the OPS dashboard.

These functions take a ledger (or a projection of it) and return primitive
Python types. No Textual, no I/O, no UI state. Keeping them here makes them
trivially testable and reusable across tabs.
"""

from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from claudit import (
    FIELD_CACHE_SAVINGS,
    FIELD_COST,
    FIELD_TOKENS_IN,
    FIELD_TOKENS_OUT,
    entry_local_dt,
)

# Ordered (prefix, short) mapping for model-name shortening
_MODEL_FAMILIES = ("opus", "sonnet", "haiku")

MODEL_COLORS = {
    "opus": "#FF9900",
    "sonnet": "#9999CC",
    "haiku": "#CC6699",
}

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


def group_by_prompt(entries: List[Tuple]) -> List[Dict]:
    """Group consecutive entries sharing a promptId into turn summaries.

    Returns a list of dicts (newest-first), one per prompt_id. Each dict:
        prompt_id:   the UUID
        start_dt:    first (oldest) call's dt
        end_dt:      last (newest) call's dt
        turns:       total assistant turns in this prompt
        cost:        summed cost (including subagent cost via parent_session)
        tokens_in/out, cache_reads/writes:  sums
        tools:       list of all tool names used across turns
        final_stop:  stop_reason of the end_turn call, or the newest call
        preview:     prompt_preview from any turn
        session:     top-level session UUID (for parent/child joining)
        models:      set of models used
        spawn_count: how many Agent invocations this prompt made

    Entries without a promptId get their own single-entry "group".
    """
    groups: Dict[str, Dict] = {}
    orphan_idx = 0
    for dt, eid, e in entries:
        if e.get("source") == "agent_spawn":
            pid = e.get("promptId") or ""
            if pid and pid in groups:
                groups[pid]["spawn_count"] += 1
            continue
        pid = e.get("promptId") or ""
        if not pid:
            pid = f"_orphan:{orphan_idx}:{eid}"
            orphan_idx += 1
        g = groups.get(pid)
        if g is None:
            g = {
                "prompt_id": pid, "start_dt": dt, "end_dt": dt,
                "turns": 0, "cost": 0.0,
                "tokens_in": 0, "tokens_out": 0,
                "cache_reads": 0, "cache_writes": 0,
                "tools": [], "final_stop": "", "preview": "",
                "session": e.get("session") or "",
                "models": set(), "spawn_count": 0,
                "is_subagent": bool(e.get("isSubagent")),
                "project": e.get("project") or "",
            }
            groups[pid] = g
        g["turns"] += 1
        g["cost"] += e.get(FIELD_COST, 0)
        g["tokens_in"] += e.get(FIELD_TOKENS_IN, 0)
        g["tokens_out"] += e.get(FIELD_TOKENS_OUT, 0)
        from claudit import FIELD_CACHE_READS, FIELD_CACHE_WRITES
        g["cache_reads"] += e.get(FIELD_CACHE_READS, 0)
        g["cache_writes"] += e.get(FIELD_CACHE_WRITES, 0)
        for t in e.get("tools") or []:
            g["tools"].append(t)
        if e.get("model"):
            g["models"].add(e["model"])
        if dt < g["start_dt"]:
            g["start_dt"] = dt
        if dt > g["end_dt"]:
            g["end_dt"] = dt
        # Keep the NEWEST preview — that's the anchor user prompt in the group
        if e.get("promptPreview") and not g["preview"]:
            g["preview"] = e["promptPreview"]
        stop = e.get("stopReason") or ""
        if stop == "end_turn" or not g["final_stop"]:
            g["final_stop"] = stop
    # Attach spawns from the second pass (those whose prompt_id was discovered
    # earlier in the first loop are already counted above).
    groups_list = list(groups.values())
    groups_list.sort(key=lambda g: g["end_dt"], reverse=True)
    return groups_list


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

def short_model(model: Optional[str]) -> str:
    """Shorten a full model ID to family-version form.

    claude-opus-4-6               → opus-4.6
    claude-sonnet-4-5-20250929    → sonnet-4.5
    claude-haiku-4-5-20251001     → haiku-4.5
    """
    if not model:
        return "—"
    for family in _MODEL_FAMILIES:
        prefix = f"claude-{family}-"
        if not model.startswith(prefix):
            continue
        tail = model[len(prefix):]
        nums = []
        for part in tail.split("-"):
            if part.isdigit() and len(part) < 4:
                nums.append(part)
            else:
                break
        return f"{family}-{'.'.join(nums)}" if nums else family
    return model.split("-")[0] if "-" in model else model


def model_color(short: str) -> str:
    """Return the LCARS hex color for a shortened model name's family."""
    family = short.split("-")[0]
    return MODEL_COLORS.get(family, "#CC9966")


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


def row_preview_text(entry: Dict) -> str:
    """Preview text for a call-log row.

    Prefers the captured user prompt. Falls back to a synthesized descriptor
    built from available metadata so the row still reads meaningfully.

    User-supplied prompt text is escaped so `[` characters in prompts can't
    inject Textual markup (which would raise MarkupError at render time).
    """
    from claudit import FIELD_CACHE_READS, FIELD_CACHE_WRITES  # local: avoid cycles

    raw = (entry.get("promptPreview") or "").replace("\n", " ").replace("\t", " ")
    clean = " ".join(raw.split())
    if clean:
        # Escape `[` so bracketed user content can't inject markup
        return f"[dim]»[/] {clean.replace('[', chr(92) + '[')}"

    bits = []
    stop = entry.get("stopReason")
    if stop:
        bits.append(f"stop={stop}")
    cr = entry.get(FIELD_CACHE_READS, 0)
    cw = entry.get(FIELD_CACHE_WRITES, 0)
    if cr or cw:
        bits.append(f"cache {_format_k(cr)}r/{_format_k(cw)}w")
    if entry.get("isSubagent"):
        bits.append("subagent turn")
    sess = entry.get("session") or ""
    if sess:
        bits.append(f"session {sess[:8]}")
    detail = " · ".join(bits) if bits else "no prompt captured"
    return f"[dim]· {detail}[/]"


def _format_k(num: int) -> str:
    """Tiny helper: 12345 → 12K. Avoids pulling format_number into this module."""
    if num >= 1_000_000:
        return f"{num // 1_000_000}M"
    if num >= 1_000:
        return f"{num // 1_000}K"
    return str(num)
