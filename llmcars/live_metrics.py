"""Reactive metrics layer for the TUI.

One `LiveMetrics` instance holds a `MetricsSnapshot` that every display
widget reads from. The 30s refresh tick recomputes the snapshot; Textual's
reactive system fans the change out to subscribed widgets in place.

Design notes:
  - Snapshot is frozen and replaced atomically. Widgets never see a half-
    written snapshot.
  - `clock` is part of the snapshot so time-derived values (today, this
    week, rate/hr) become stale-proof: a minute rollover is a refresh
    trigger on equal footing with a new ledger entry.
  - `daily_signature` isolates chart-worthy changes from clock ticks, so
    expensive plotext rebuilds fire only when the underlying day data
    changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from textual.reactive import reactive
from textual.widget import Widget

from .formatters import (
    FIELD_CACHE_SAVINGS,
    FIELD_COST,
    FIELD_REQUESTS,
    FIELD_TOKENS_IN,
    FIELD_TOKENS_OUT,
    calculate_totals,
    init_field_dict,
)
from .ops_data import (
    OpsView,
    aggregate_today,
    collect_entries,
    derive_ops_view,
    short_model,
    short_project,
)


# ── Clock bucket ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClockBucket:
    """Quantized clock reading.

    `minute_key` flips every minute — finest granularity used for "rate/hr"
    and "today cost" refreshes. `hour_key` and `day` allow coarser gating
    if a widget only needs hour-level freshness.
    """
    now: datetime
    minute_key: str
    hour_key: str
    day: str

    @classmethod
    def capture(cls, now: Optional[datetime] = None) -> "ClockBucket":
        now = now or datetime.now()
        return cls(
            now=now,
            minute_key=now.strftime("%Y-%m-%dT%H:%M"),
            hour_key=now.strftime("%Y-%m-%dT%H"),
            day=now.strftime("%Y-%m-%d"),
        )


# ── Overview-tab derived metrics ──────────────────────────────────────────

@dataclass(frozen=True)
class OverviewMetrics:
    """Everything the OVERVIEW tab displays, pre-computed."""
    today_cost: float
    today_requests: int
    this_week_cost: float
    wow_detail: str
    month_cost: float
    month_requests: int
    tokens_7d_total: int
    tokens_7d_in: int
    tokens_7d_out: int
    cache_savings_30d: float
    cache_eff_label: str
    burn_rate: float
    spark_7d_cost: List[float]
    spark_4w: List[float]
    spark_30d_cost: List[float]
    spark_7d_tokens: List[float]
    spark_7d_cache: List[float]
    spark_burn: List[float]


def _overview(daily: Dict[str, Dict], clock: ClockBucket) -> OverviewMetrics:
    sorted_days = sorted(daily.keys())
    today_str = clock.day
    today = clock.now
    today_data = daily.get(today_str, init_field_dict())

    last_7 = sorted_days[-7:] if len(sorted_days) >= 7 else sorted_days
    last_28 = sorted_days[-28:] if len(sorted_days) >= 28 else sorted_days
    last_30 = sorted_days[-30:] if len(sorted_days) >= 30 else sorted_days
    totals_30 = calculate_totals({d: daily[d] for d in last_30})

    spark_7d_cost = [daily[d][FIELD_COST] for d in last_7]

    week_costs: List[float] = []
    for i in range(0, len(last_28), 7):
        chunk = last_28[i:i + 7]
        week_costs.append(sum(daily[d][FIELD_COST] for d in chunk))
    spark_4w = week_costs if week_costs else [0.0]

    tokens_in_7d = sum(daily[d][FIELD_TOKENS_IN] for d in last_7)
    tokens_out_7d = sum(daily[d][FIELD_TOKENS_OUT] for d in last_7)
    spark_7d_tokens = [
        daily[d][FIELD_TOKENS_IN] + daily[d][FIELD_TOKENS_OUT]
        for d in last_7
    ]

    spark_7d_cache: List[float] = []
    for d in last_7:
        dd = daily[d]
        potential = dd[FIELD_COST] + dd[FIELD_CACHE_SAVINGS]
        spark_7d_cache.append(
            dd[FIELD_CACHE_SAVINGS] / potential * 100 if potential > 0 else 0
        )

    spark_30d_cost = [daily[d][FIELD_COST] for d in last_30]

    spark_burn: List[float] = []
    for i in range(len(last_30)):
        window = last_30[max(0, i - 6):i + 1]
        avg = sum(daily[d][FIELD_COST] for d in window) / len(window)
        spark_burn.append(avg)

    this_week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    last_week_start = (today - timedelta(days=today.weekday() + 7)).strftime("%Y-%m-%d")
    this_week_cost = sum(
        d[FIELD_COST] for day, d in daily.items()
        if this_week_start <= day <= today_str
    )
    last_week_cost = sum(
        d[FIELD_COST] for day, d in daily.items()
        if last_week_start <= day < this_week_start
    )
    if last_week_cost > 0:
        wow_delta = ((this_week_cost - last_week_cost) / last_week_cost) * 100
        wow_detail = f"{'↑' if wow_delta >= 0 else '↓'} {abs(wow_delta):.0f}% vs last week"
    else:
        wow_detail = "no prior week data"

    potential_30 = totals_30[FIELD_COST] + totals_30[FIELD_CACHE_SAVINGS]
    cache_eff_label = (
        f"{totals_30[FIELD_CACHE_SAVINGS] / potential_30 * 100:.0f}% efficiency"
        if potential_30 > 0 else ""
    )
    burn_rate = sum(spark_7d_cost) / len(last_7) if last_7 else 0

    return OverviewMetrics(
        today_cost=today_data[FIELD_COST],
        today_requests=today_data[FIELD_REQUESTS],
        this_week_cost=this_week_cost,
        wow_detail=wow_detail,
        month_cost=totals_30[FIELD_COST],
        month_requests=totals_30[FIELD_REQUESTS],
        tokens_7d_total=sum(spark_7d_tokens),
        tokens_7d_in=tokens_in_7d,
        tokens_7d_out=tokens_out_7d,
        cache_savings_30d=totals_30[FIELD_CACHE_SAVINGS],
        cache_eff_label=cache_eff_label,
        burn_rate=burn_rate,
        spark_7d_cost=spark_7d_cost,
        spark_4w=spark_4w,
        spark_30d_cost=spark_30d_cost,
        spark_7d_tokens=spark_7d_tokens,
        spark_7d_cache=spark_7d_cache,
        spark_burn=spark_burn,
    )


# ── Snapshot ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MetricsSnapshot:
    """The full TUI-facing view. Replaced atomically on each refresh."""
    clock: ClockBucket
    daily_signature: Tuple             # changes when `daily` changes
    ledger_signature: Tuple            # (len, source_filter)
    overview: OverviewMetrics
    ops: OpsView
    ops_entries: List                  # raw entries for OPS call-log rendering
    daily: Dict[str, Dict] = field(default_factory=dict)
    source_filter: Optional[str] = None


def _daily_signature(daily: Dict[str, Dict]) -> Tuple:
    """Fingerprint of the daily aggregate — cheap change detector for charts.

    Length + newest-day cost/request totals catch both new-entry-today and
    new-day-appeared changes without hashing the whole dict.
    """
    if not daily:
        return (0,)
    newest = max(daily.keys())
    d = daily[newest]
    return (
        len(daily),
        newest,
        d.get(FIELD_COST, 0),
        d.get(FIELD_REQUESTS, 0),
        d.get(FIELD_TOKENS_IN, 0),
        d.get(FIELD_TOKENS_OUT, 0),
    )


def compute_snapshot(ledger: Dict, daily: Dict[str, Dict],
                     source_filter: Optional[str],
                     clock: Optional[ClockBucket] = None) -> MetricsSnapshot:
    """Build a full `MetricsSnapshot` from raw inputs. Pure function."""
    clock = clock or ClockBucket.capture()
    entries = collect_entries(ledger, source_filter)
    stats = aggregate_today(entries, short_project, short_model)
    ops = derive_ops_view(entries, stats, now=clock.now)
    overview = _overview(daily, clock)
    return MetricsSnapshot(
        clock=clock,
        daily_signature=_daily_signature(daily),
        ledger_signature=(len(ledger), source_filter or ""),
        overview=overview,
        ops=ops,
        ops_entries=entries,
        daily=daily,
        source_filter=source_filter,
    )


# ── Reactive holder ───────────────────────────────────────────────────────

class LiveMetrics(Widget):
    """Non-visible reactive carrier for the current `MetricsSnapshot`.

    Widgets that bind to specific fields watch this carrier. Making it a
    Widget (rather than holding a free reactive) gives us Textual's normal
    DOM-lifecycle watchers and `watch()` subscriptions.
    """

    DEFAULT_CSS = "LiveMetrics { display: none; }"

    snapshot: reactive[Optional[MetricsSnapshot]] = reactive(None, layout=False)

    def update(self, snap: MetricsSnapshot) -> None:
        """Replace the current snapshot; fires watchers iff it differs."""
        self.snapshot = snap
