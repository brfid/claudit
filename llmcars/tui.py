"""LLMCARS — LCARS-style TUI dashboard for llmcars."""

import contextlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.widget import Widget
from textual.widgets import Button, Label, Static

from textual_plotext import PlotextPlot

from .aggregation import aggregate_by_day, entry_local_dt
from .formatters import (
    FIELD_CACHE_READS,
    FIELD_CACHE_SAVINGS,
    FIELD_CACHE_WRITES,
    FIELD_COST,
    FIELD_REQUESTS,
    FIELD_TOKENS_IN,
    FIELD_TOKENS_OUT,
    SOURCE_MAP,
    format_cost,
    format_number,
    format_tokens,
)
from .ledger import get_ledger_path, load_ledger
from .pipeline import run_ingest
from .ops_data import (
    LOG_ROW_CAP,
    RECENT_BUCKET_MIN,
    RECENT_WINDOW_HOURS,
    OpsView,
    RowSpec,
    build_row_specs,
    cost_bar,
    model_color,
    row_activity_text,
    short_model,
    short_project,
    short_tools,
)
from .live_metrics import LiveMetrics, MetricsSnapshot, compute_snapshot
from .ops_widgets import (
    EntryDetailScreen,
    FluidBar,
    HeatmapGrid,
    HelpScreen,
    LiveBorderSubtitle,
    LiveHourlyBar,
    LiveLabel,
    LiveStatBox,
    LiveStatic,
    LogRow,
)

TABS = ["OVERVIEW", "DAILY", "CUMULATIVE", "CALENDAR", "TOKENS",
        "CACHE", "REQUESTS", "COST MAP", "CALLS", "OPS", "RECENT"]


# ── Helper: aggregate by hour-of-day × day-of-week ──

def _iter_individual_entries(ledger: Dict, source_filter: Optional[str] = None):
    """Yield (dt, entry) for non-historical entries, filtered by source."""
    for entry_id, entry in ledger.items():
        if entry_id.startswith("cline:historical:"):
            continue
        if source_filter and entry.get("source") != source_filter:
            continue
        try:
            yield entry_local_dt(entry), entry
        except (ValueError, KeyError):
            continue


def aggregate_hourly_cost_heatmap(ledger: Dict, source_filter: Optional[str] = None
                                  ) -> List[List[float]]:
    """Build 7×24 grid of cost (rows=days Mon-Sun, cols=hours)."""
    grid = [[0.0] * 24 for _ in range(7)]
    for dt, entry in _iter_individual_entries(ledger, source_filter):
        grid[dt.weekday()][dt.hour] += entry.get(FIELD_COST, 0)
    return grid


# ── Heatmap day-axis labels ──

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ── Main app ──

# Panels per side-by-side tier on the OPS tab
TOP_N_PANEL = 6

# Color for the hourly-bar ghost cells
_STOP_COLORS = {
    "end_turn": "#9999CC",
    "tool_use": "#FF9900",
    "max_tokens": "#CC6699",
    "stop_sequence": "#CC9966",
    "—": "#555566",
}


class CostTrackerApp(App):
    CSS_PATH = Path(__file__).resolve().parent / "llmcars.tcss"
    TITLE = "LLMCARS"
    REFRESH_INTERVAL = 30
    NEW_ROW_HIGHLIGHT_TICKS = 2
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "toggle_refresh", "Toggle refresh"),
        Binding("?", "show_help", "Help"),
        # Tab navigation
        Binding("]", "tab_next", "Next tab", show=False),
        Binding("[", "tab_prev", "Prev tab", show=False),
        # Row navigation (OPS) / scroll (other tabs)
        Binding("j", "scroll_log(1)", "Scroll ↓", show=False),
        Binding("k", "scroll_log(-1)", "Scroll ↑", show=False),
        Binding("J", "scroll_log(10)", "Page ↓", show=False),
        Binding("K", "scroll_log(-10)", "Page ↑", show=False),
        Binding("ctrl+d", "scroll_log(10)", "Page ↓", show=False),
        Binding("ctrl+u", "scroll_log(-10)", "Page ↑", show=False),
        Binding("g", "jump_top", "Top", show=False),
        Binding("G", "jump_bottom", "Bottom", show=False),
        Binding("enter", "expand_selected", "Expand row", show=False),
        # Number shortcuts
        Binding("1", "tab('OVERVIEW')", "Overview", show=False),
        Binding("2", "tab('DAILY')", "Daily", show=False),
        Binding("3", "tab('CUMULATIVE')", "Cumulative", show=False),
        Binding("4", "tab('CALENDAR')", "Calendar", show=False),
        Binding("5", "tab('TOKENS')", "Tokens", show=False),
        Binding("6", "tab('CACHE')", "Cache", show=False),
        Binding("7", "tab('REQUESTS')", "Requests", show=False),
        Binding("8", "tab('COST MAP')", "Cost Map", show=False),
        Binding("9", "tab('CALLS')", "Calls", show=False),
        Binding("0", "tab('OPS')", "Ops", show=False),
        Binding("minus", "tab('RECENT')", "Recent", show=False),
    ]

    def __init__(self, ledger_path_override=None, source_filter="all",
                 no_ingest=False, force_ingest=False, **kwargs):
        super().__init__(**kwargs)
        self._ledger_path_override = ledger_path_override
        self._source_filter_arg = source_filter
        self._no_ingest = no_ingest
        self._force_ingest = force_ingest
        self._ledger: Dict = {}
        self._daily: Dict = {}
        self._source_filter: Optional[str] = None
        self._current_tab: str = "OVERVIEW"
        self._auto_refresh: bool = True
        self._refresh_timer = None
        # Diff tracking for auto-refresh highlight: id → ticks-remaining
        self._new_entry_ids: Dict[str, int] = {}
        self._seen_ids: set = set()
        # Selected row index in OPS call log; -1 means no selection
        self._selected_row: int = -1
        # Per-row spec cache so selection moves don't rebuild labels
        self._ops_row_specs: List[RowSpec] = []
        # Reactive snapshot carrier — mounted in compose(); every live
        # widget subscribes to its `snapshot` attribute.
        self._metrics = LiveMetrics()
        # Last chart-data signature; charts only rebuild when this changes.
        self._last_daily_sig: Optional[tuple] = None

    def _load_data(self) -> MetricsSnapshot:
        """Reload ledger, recompute aggregates, push a fresh snapshot.

        Returns the new snapshot so callers can inspect its signatures
        to decide whether a chart rebuild is warranted.
        """
        ledger_path = get_ledger_path(self._ledger_path_override)
        self._ledger = load_ledger(ledger_path)

        run_ingest(ledger_path, self._ledger, source=self._source_filter_arg,
                   no_ingest=self._no_ingest, force_ingest=self._force_ingest,
                   quiet=True)

        arg = self._source_filter_arg
        self._source_filter = None if arg == "all" else SOURCE_MAP.get(arg, arg)

        self._daily = aggregate_by_day(self._ledger, source_filter=self._source_filter)

        current_ids = set(self._ledger.keys())
        if self._seen_ids:
            # Age existing highlights
            self._new_entry_ids = {
                eid: ticks - 1
                for eid, ticks in self._new_entry_ids.items()
                if ticks > 1 and eid in current_ids
            }
            for eid in current_ids - self._seen_ids:
                self._new_entry_ids[eid] = self.NEW_ROW_HIGHLIGHT_TICKS
        self._seen_ids = current_ids

        snap = compute_snapshot(
            self._ledger, self._daily, self._source_filter,
        )
        self._metrics.update(snap)
        return snap

    @property
    def _snapshot(self) -> Optional[MetricsSnapshot]:
        return self._metrics.snapshot

    @property
    def _ops_entries_cache(self) -> list:
        """Back-compat accessor for OPS row selection + detail modal."""
        snap = self._snapshot
        return snap.ops_entries if snap else []

    def compose(self) -> ComposeResult:
        # Non-visible reactive carrier. Every live widget watches its
        # `snapshot` attribute.
        yield self._metrics

        with Horizontal(id="top-bar"):
            # Stardate-flavored timestamp: month · ISO-week · day-of-month.
            # Bound to the snapshot so it rolls over with the clock.
            yield LiveStatic(
                self._metrics,
                lambda s: s.clock.now.strftime("%m·%V·%d"),
                placeholder=datetime.now().strftime("%m·%V·%d"),
                id="top-elbow",
            )
            yield Static("LLMCARS", id="top-title")
            yield Static("", id="top-bar-line")

        with Horizontal():
            with Vertical(id="sidebar"):
                for i, tab_name in enumerate(TABS):
                    slug = tab_name.lower().replace(" ", "-")
                    btn = Button(tab_name, id=f"nav-{slug}",
                                 classes="nav-button")
                    if i == 0:
                        btn.add_class("active")
                    yield btn

            with VerticalScroll(id="main-content"):
                yield Vertical(id="panel-container")

        with Horizontal(id="bottom-bar"):
            yield Static("", id="bottom-elbow")
            yield Static("", id="bottom-status")
            yield Static("", id="bottom-bar-line")

    # Terminal height below this triggers compact sidebar (1-row nav buttons)
    COMPACT_SIDEBAR_ROWS = 45

    def on_mount(self) -> None:
        snap = self._load_data()
        self._last_daily_sig = snap.daily_signature
        self._update_status_bar()
        self._render_tab("OVERVIEW")
        self._refresh_timer = self.set_interval(
            self.REFRESH_INTERVAL, self._auto_refresh_tick
        )
        self._apply_sidebar_density()

    def on_resize(self, event) -> None:
        self._apply_sidebar_density()

    def _apply_sidebar_density(self) -> None:
        try:
            sidebar = self.query_one("#sidebar")
        except Exception:
            return
        if self.size.height < self.COMPACT_SIDEBAR_ROWS:
            sidebar.add_class("compact")
        else:
            sidebar.remove_class("compact")

    def _update_status_bar(self) -> None:
        entry_count = len(self._ledger)
        day_count = len(self._daily)
        refresh_icon = "⟳" if self._auto_refresh else "⏸"
        new_count = len(self._new_entry_ids)
        new_badge = f" · [#FF9900]+{new_count} new[/]" if new_count else ""
        dot = " [#9999CC]◤[/] "
        hint = (f"{dot}\\[/] tabs{dot}j/k · g/G{dot}ENTER details"
                f"{dot}r pause{dot}? help{dot}q quit")
        status = self.query_one("#bottom-status", Static)
        status.update(
            f"  [#FF9900]{entry_count:,}[/] entries{dot}"
            f"[#FF9900]{day_count}[/] days{dot}"
            f"{refresh_icon} {self.REFRESH_INTERVAL}s{new_badge}{hint}  ",
        )

    # Tabs whose content is pure-chart (plotext/heatmap). These don't
    # subscribe to LiveMetrics reactively, so their tick-time refresh path
    # is a full `_render_tab` — but only when the underlying daily data
    # (or hourly grid, for COST MAP) actually changed. RECENT is included
    # because its 12h window slides with the clock, not the daily aggregate.
    _CHART_TABS = frozenset({
        "DAILY", "CUMULATIVE", "CALENDAR", "TOKENS", "CACHE",
        "REQUESTS", "COST MAP", "CALLS", "RECENT",
    })

    def _auto_refresh_tick(self) -> None:
        if not self._auto_refresh:
            return
        self._force_ingest = False
        snap = self._load_data()
        self._update_status_bar()

        # Live tabs (OVERVIEW, OPS) self-update via reactive watchers.
        # Chart tabs need an explicit rebuild, but only when data changed —
        # minute rollovers alone shouldn't redraw plotext.
        if (self._current_tab in self._CHART_TABS
                and snap.daily_signature != self._last_daily_sig):
            self._last_daily_sig = snap.daily_signature
            self._render_tab(self._current_tab)
        else:
            self._last_daily_sig = snap.daily_signature

        # OPS call-log rows aren't reactive (each row's markup depends on
        # per-row selected/new state). Refresh them in place when the row
        # contents changed.
        if self._current_tab == "OPS":
            self._refresh_log_rows()

    def action_toggle_refresh(self) -> None:
        self._auto_refresh = not self._auto_refresh
        self._update_status_bar()

    def action_tab_next(self) -> None:
        idx = (TABS.index(self._current_tab) + 1) % len(TABS)
        self.action_tab(TABS[idx])

    def action_tab_prev(self) -> None:
        idx = (TABS.index(self._current_tab) - 1) % len(TABS)
        self.action_tab(TABS[idx])

    def action_jump_top(self) -> None:
        if self._current_tab == "OPS" and self._ops_entries_cache:
            self._set_selected_row(0)
        else:
            with contextlib.suppress(Exception):
                self.query_one("#main-content", VerticalScroll).scroll_home(animate=False)

    def action_jump_bottom(self) -> None:
        if self._current_tab == "OPS" and self._ops_entries_cache:
            self._set_selected_row(min(100, len(self._ops_entries_cache)) - 1)
        else:
            with contextlib.suppress(Exception):
                self.query_one("#main-content", VerticalScroll).scroll_end(animate=False)

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_scroll_log(self, delta: int) -> None:
        """On OPS: move selection. Off OPS: scroll container."""
        if self._current_tab == "OPS":
            if not self._ops_entries_cache:
                return
            max_idx = min(100, len(self._ops_entries_cache)) - 1
            # If nothing selected, j/J starts at top; k/K starts at bottom
            if self._selected_row == -1:
                new_idx = 0 if delta > 0 else max_idx
            else:
                new_idx = max(0, min(max_idx, self._selected_row + delta))
            self._set_selected_row(new_idx)
            return
        try:
            scroller = self.query_one("#main-content", VerticalScroll)
        except Exception:
            return
        scroller.scroll_relative(y=delta, animate=False)

    def _set_selected_row(self, new_idx: int) -> None:
        """Move selection in place without rebuilding the whole OPS panel.

        Updates the previously-selected row (if any) and the new one, then
        scrolls the new one into view. Falls back to a full rerender only
        when row widgets aren't mounted yet (first draw).
        """
        prev = self._selected_row
        self._selected_row = new_idx
        if not self._ops_row_specs:
            self._render_tab("OPS")
            return
        try:
            rows = list(self.query(LogRow))
        except Exception:
            self._render_tab("OPS")
            return
        if not rows:
            self._render_tab("OPS")
            return
        # Update only the two rows whose state changed
        for idx in {prev, new_idx}:
            if 0 <= idx < len(rows) and idx < len(self._ops_row_specs):
                spec = self._ops_row_specs[idx]
                text, row_class = self._render_row_spec(spec, selected=(idx == new_idx))
                rows[idx].update(text)
                rows[idx].set_classes(row_class)
        if 0 <= new_idx < len(rows):
            with contextlib.suppress(Exception):
                rows[new_idx].scroll_visible(animate=False)

    def action_expand_selected(self) -> None:
        """Show modal with full prompt + metadata for the selected entry.

        If nothing is selected, default to the top-most (most recent) entry.
        """
        if self._current_tab != "OPS" or not self._ops_entries_cache:
            return
        idx = self._selected_row if self._selected_row >= 0 else 0
        idx = max(0, min(len(self._ops_entries_cache) - 1, idx))
        dt, eid, entry = self._ops_entries_cache[idx]
        self.push_screen(EntryDetailScreen(dt, eid, entry))

    def on_log_row_clicked(self, message: "LogRow.Clicked") -> None:
        """Clicking a log row selects it."""
        if self._current_tab != "OPS":
            return
        self._set_selected_row(message.row_index)

    def on_click(self, event: Click) -> None:
        """Click outside a log row clears selection on OPS tab."""
        if self._current_tab != "OPS":
            return
        widget = getattr(event, "widget", None)
        # Walk up from clicked widget looking for a LogRow ancestor
        w = widget
        while w is not None:
            if isinstance(w, LogRow):
                return
            w = getattr(w, "parent", None)
        if self._selected_row != -1:
            self._set_selected_row(-1)

    @staticmethod
    def _tab_slug(tab_name: str) -> str:
        return tab_name.lower().replace(" ", "-")

    def _activate_nav(self, tab_name: str) -> None:
        for btn in self.query(".nav-button"):
            btn.remove_class("active")
        self.query_one(f"#nav-{self._tab_slug(tab_name)}", Button).add_class("active")

    _SLUG_TO_TAB = {t.lower().replace(" ", "-"): t for t in TABS}

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id.startswith("nav-"):
            slug = btn_id[4:]
            tab_name = self._SLUG_TO_TAB.get(slug, slug.upper())
            self._activate_nav(tab_name)
            self._render_tab(tab_name)

    def action_tab(self, tab_name: str) -> None:
        self._activate_nav(tab_name)
        self._render_tab(tab_name)

    def _render_tab(self, tab_name: str) -> None:
        self._current_tab = tab_name
        builders = {
            "OVERVIEW": self._build_overview,
            "DAILY": self._build_cost_chart,
            "CUMULATIVE": self._build_cumulative_chart,
            "CALENDAR": self._build_calendar_heatmap,
            "TOKENS": self._build_tokens_chart,
            "CACHE": self._build_cache_chart,
            "REQUESTS": self._build_activity,
            "COST MAP": self._build_spend_heatmap,
            "CALLS": self._build_cost_histogram,
            "OPS": self._build_ops,
            "RECENT": self._build_recent,
        }
        container = self.query_one("#panel-container", Vertical)
        container.remove_children()
        container.mount(builders[tab_name]())

    @staticmethod
    def _init_plt(plot: PlotextPlot):
        """Reset a PlotextPlot and return its plt handle."""
        plt = plot.plt
        plt.clear_data()
        plt.clear_figure()
        plt.theme("dark")
        plt.plot_size(None, None)
        return plt

    @staticmethod
    def _set_yticks(plt, values: list, formatter, num_ticks: int = 5,
                    yside: str = "left") -> None:
        """Set Y-axis ticks with custom formatted labels."""
        if not values or max(values) == 0:
            return
        step = max(values) / num_ticks
        positions = [step * i for i in range(num_ticks + 1)]
        labels = [formatter(v) for v in positions]
        plt.yticks(positions, labels, yside=yside)

    @staticmethod
    def _set_date_xticks(plt, sorted_days: list[str], dates: list[int],
                         max_ticks: int = 10) -> None:
        tick_step = max(1, len(sorted_days) // max_ticks)
        tick_positions = dates[::tick_step]
        tick_labels = [sorted_days[i][5:] for i in tick_positions]
        plt.xticks(tick_positions, tick_labels)

    @staticmethod
    def _chart_panel(title: str, plot: Widget,
                     subtitle: str = "") -> Widget:
        """Standard wrapper: title + optional subtitle + plot, LCARS chart-panel."""
        children: list[Widget] = [Label(f"  {title}", classes="chart-title")]
        if subtitle:
            children.append(Label(f"  {subtitle}", classes="chart-subtitle"))
        children.append(plot)
        return Vertical(*children, classes="chart-panel")

    def _make_chart(self, title: str, draw_fn: Callable,
                    subtitle: str = "") -> Widget:
        """Wrap a plotext draw function in the standard chart-panel scaffolding.

        `draw_fn(plt)` is deferred until the PlotextPlot widget is mounted.
        It owns plotting + axis setup; this helper owns init, refresh, and
        panel wrapping.
        """
        plot = PlotextPlot()

        def on_mount_chart():
            plt = self._init_plt(plot)
            draw_fn(plt)
            plot.refresh()

        plot.call_after_refresh(on_mount_chart)
        return self._chart_panel(title, plot, subtitle)

    # ── Overview tab ──

    def _build_overview(self) -> Widget:
        """OVERVIEW stats row — every value binds to the snapshot.

        Minute ticks and new-entry ticks both produce a new snapshot; the
        LiveStatBox children re-render in place.
        """
        m = lambda snap: snap.overview  # noqa: E731

        stats_row = Horizontal(
            LiveStatBox(
                self._metrics, "TODAY",
                value_selector=lambda s: format_cost(m(s).today_cost),
                detail_selector=lambda s: f"{m(s).today_requests:,} requests",
                spark_selector=lambda s: m(s).spark_7d_cost,
                spark_label="7d cost ▸", classes="stat-box",
            ),
            LiveStatBox(
                self._metrics, "THIS WEEK",
                value_selector=lambda s: format_cost(m(s).this_week_cost),
                detail_selector=lambda s: m(s).wow_detail,
                spark_selector=lambda s: m(s).spark_4w,
                spark_label="4wk weekly ▸", classes="stat-box",
            ),
            LiveStatBox(
                self._metrics, "30-DAY",
                value_selector=lambda s: format_cost(m(s).month_cost),
                detail_selector=lambda s: f"{format_number(m(s).month_requests)} requests",
                spark_selector=lambda s: m(s).spark_30d_cost,
                spark_label="30d daily ▸", classes="stat-box",
            ),
            LiveStatBox(
                self._metrics, "TOKENS (7d)",
                value_selector=lambda s: format_tokens(m(s).tokens_7d_total),
                detail_selector=lambda s: (
                    f"{format_tokens(m(s).tokens_7d_in)} in / "
                    f"{format_tokens(m(s).tokens_7d_out)} out"
                ),
                spark_selector=lambda s: m(s).spark_7d_tokens,
                spark_label="7d tokens ▸", classes="stat-box",
            ),
            LiveStatBox(
                self._metrics, "CACHE HIT",
                value_selector=lambda s: format_cost(m(s).cache_savings_30d),
                detail_selector=lambda s: m(s).cache_eff_label,
                spark_selector=lambda s: m(s).spark_7d_cache,
                spark_label="7d efficiency ▸", classes="stat-box",
            ),
            LiveStatBox(
                self._metrics, "BURN RATE",
                value_selector=lambda s: f"{format_cost(m(s).burn_rate)}/day",
                detail_selector=lambda s: "7-day rolling avg",
                spark_selector=lambda s: m(s).spark_burn,
                spark_label="30d avg ▸", classes="stat-box",
            ),
            id="overview-panel",
        )

        return Vertical(stats_row, classes="chart-panel")

    # ── Cost timeline chart ──

    def _build_cost_chart(self) -> Widget:
        sorted_days = sorted(self._daily.keys())[-60:]
        dates = list(range(len(sorted_days)))
        costs = [self._daily[d][FIELD_COST] for d in sorted_days]
        total_cost = sum(costs)

        def draw(plt):
            plt.plot(dates, costs, marker="braille", color=(255, 153, 0))
            self._set_date_xticks(plt, sorted_days, dates)
            self._set_yticks(plt, costs, format_cost)

        subtitle = (
            f"{sorted_days[0]} → {sorted_days[-1]}  ◥  "
            f"Total: {format_cost(total_cost)}  ◥  "
            f"Avg: {format_cost(total_cost / len(costs))}/day"
            if sorted_days else ""
        )
        return self._make_chart("DAILY COST ($)", draw, subtitle)

    # ── Tokens charts ──
    #
    # Cache reads typically dwarf input/output by 10–500×, so a single shared
    # axis crushes the I/O bars to invisibility. Split into two stacked
    # panels with independent scales: top shows generated traffic (input +
    # output), bottom shows cache traffic (writes + reads). Each subtitle
    # carries the totals so quick comparisons don't require eyeballing the
    # axis.

    def _build_tokens_chart(self) -> Widget:
        sorted_days = sorted(self._daily.keys())[-30:]
        if not sorted_days:
            return self._chart_panel("No token data", Label(""))

        labels = [d[5:] for d in sorted_days]
        tokens_in = [self._daily[d][FIELD_TOKENS_IN] for d in sorted_days]
        tokens_out = [self._daily[d][FIELD_TOKENS_OUT] for d in sorted_days]
        cache_w = [self._daily[d][FIELD_CACHE_WRITES] for d in sorted_days]
        cache_r = [self._daily[d][FIELD_CACHE_READS] for d in sorted_days]

        def _fmt_tok(v):
            return format_tokens(int(v), compact=True)

        def draw_io(plt):
            plt.multiple_bar(
                labels,
                [tokens_in, tokens_out],
                labels=["Input", "Output"],
                color=[(255, 153, 0), (204, 102, 153)],
            )
            self._set_yticks(plt, tokens_in + tokens_out, _fmt_tok)

        def draw_cache(plt):
            plt.multiple_bar(
                labels,
                [cache_w, cache_r],
                labels=["Cache Write", "Cache Read"],
                color=[(153, 153, 204), (204, 153, 204)],
            )
            self._set_yticks(plt, cache_w + cache_r, _fmt_tok)

        io_subtitle = (
            f"30d totals  ◥  "
            f"In: {format_tokens(sum(tokens_in))}  ◥  "
            f"Out: {format_tokens(sum(tokens_out))}"
        )
        cache_subtitle = (
            f"30d totals  ◥  "
            f"Writes: {format_tokens(sum(cache_w))}  ◥  "
            f"Reads: {format_tokens(sum(cache_r))}"
        )

        io_panel = self._make_chart(
            "GENERATED TOKENS — INPUT & OUTPUT (30d)",
            draw_io, io_subtitle,
        )
        cache_panel = self._make_chart(
            "CACHE TOKENS — WRITES & READS (30d)",
            draw_cache, cache_subtitle,
        )
        return Vertical(io_panel, cache_panel, classes="chart-panel chart-stack")

    # ── Cache chart ──

    def _build_cache_chart(self) -> Widget:
        sorted_days = sorted(self._daily.keys())[-60:]
        dates = list(range(len(sorted_days)))
        savings = [self._daily[d][FIELD_CACHE_SAVINGS] for d in sorted_days]
        costs = [self._daily[d][FIELD_COST] for d in sorted_days]
        pcts = [
            (s / (s + c) * 100) if (s + c) > 0 else 0
            for s, c in zip(savings, costs)
        ]
        total_saved = sum(savings)

        def draw(plt):
            plt.plot(dates, savings, marker="braille", label="Savings ($)",
                     color=(153, 153, 204))
            plt.plot(dates, pcts, marker="braille", label="Efficiency (%)",
                     color=(255, 153, 0), yside="right")
            self._set_date_xticks(plt, sorted_days, dates)
            self._set_yticks(plt, savings, format_cost, yside="left")
            self._set_yticks(plt, pcts, lambda v: f"{v:.0f}%", yside="right")

        subtitle = (
            f"Total saved: {format_cost(total_saved)}  ◥  "
            f"Avg efficiency: {sum(pcts) / len(pcts):.0f}%"
            if pcts else ""
        )
        return self._make_chart("CACHE PERFORMANCE", draw, subtitle)

    # ── Activity heatmap ──

    def _build_activity(self) -> Widget:
        reqs_by_date = {d: self._daily[d][FIELD_REQUESTS] for d in self._daily}
        grid, month_ticks, month_labels, *_ = self._build_weeks_grid(
            {d: float(v) for d, v in reqs_by_date.items()}
        )

        total_requests = sum(reqs_by_date.values())
        active_days = sum(1 for v in reqs_by_date.values() if v > 0)
        peak_day = max(reqs_by_date, key=reqs_by_date.get) if reqs_by_date else None
        peak_count = reqs_by_date[peak_day] if peak_day else 0

        x_labels = list(zip(month_ticks, month_labels)) if month_ticks else []
        heatmap = HeatmapGrid(
            grid,
            y_labels=DAY_NAMES,
            x_labels=x_labels,
            color_low=(40, 30, 70),
            color_high=(180, 180, 240),
        )

        subtitle = (
            f"{active_days} active days  ◥  "
            f"Total: {total_requests:,}  ◥  "
            f"Peak: {peak_day[5:] if peak_day else '—'} ({peak_count:,})"
        )
        return self._chart_panel(
            "ACTIVITY HEATMAP — REQUESTS (40 weeks)", heatmap, subtitle,
        )

    # ── Calendar heatmap (GitHub-style) ──

    CALENDAR_WEEKS = 40

    @staticmethod
    def _build_weeks_grid(daily_values: Dict[str, float], num_weeks: int = CALENDAR_WEEKS):
        """Build a 7-row × N-col grid ending on the week of today."""
        today = datetime.now()
        grid_end = today + timedelta(days=(6 - today.weekday()))
        grid_start = grid_end - timedelta(days=(num_weeks - 1) * 7 + 6)

        grid = [[0.0] * num_weeks for _ in range(7)]
        for w in range(num_weeks):
            for d in range(7):
                date = grid_start + timedelta(days=w * 7 + d)
                date_str = date.strftime("%Y-%m-%d")
                if date_str in daily_values:
                    grid[d][w] = daily_values[date_str]

        month_ticks = []
        month_labels = []
        for w in range(num_weeks):
            date = grid_start + timedelta(days=w * 7)
            if date.day <= 7:
                month_ticks.append(w)
                month_labels.append(date.strftime("%b"))

        return grid, month_ticks, month_labels, grid_start, grid_end

    def _build_calendar_heatmap(self) -> Widget:
        cost_by_date = {d: self._daily[d][FIELD_COST] for d in self._daily}
        grid, month_ticks, month_labels, grid_start, grid_end = self._build_weeks_grid(cost_by_date)

        active_days = sum(1 for v in cost_by_date.values() if v > 0)
        total_cost = sum(cost_by_date.values())
        max_cost = max(cost_by_date.values()) if cost_by_date else 0

        x_labels = list(zip(month_ticks, month_labels)) if month_ticks else []
        heatmap = HeatmapGrid(
            grid,
            y_labels=DAY_NAMES,
            x_labels=x_labels,
            color_low=(60, 30, 0),
            color_high=(255, 153, 0),
        )

        subtitle = (
            f"{grid_start.strftime('%Y-%m-%d')} → "
            f"{grid_end.strftime('%Y-%m-%d')}  ◥  "
            f"{active_days} active days  ◥  "
            f"Total: {format_cost(total_cost)}  ◥  "
            f"Peak: {format_cost(max_cost)}"
        )
        return self._chart_panel(
            "CALENDAR HEATMAP — DAILY COST (40 weeks)", heatmap, subtitle,
        )

    # ── Spend heatmap (cost by hour × day-of-week) ──

    def _build_spend_heatmap(self) -> Widget:
        grid = aggregate_hourly_cost_heatmap(self._ledger, self._source_filter)

        flat = [v for row in grid for v in row if v > 0]
        total_cost = sum(v for row in grid for v in row)
        if flat:
            peak_val = max(flat)
            peak_idx = [(d, h) for d in range(7) for h in range(24)
                        if grid[d][h] == peak_val][0]
            peak_label = f"{DAY_NAMES[peak_idx[0]]} {peak_idx[1]:02d}:00 ({format_cost(peak_val)})"
        else:
            peak_label = "—"

        # Hour ticks every 3 hours so labels don't clobber each other
        hour_ticks = [(h, f"{h:02d}") for h in range(24) if h % 3 == 0]
        heatmap = HeatmapGrid(
            grid,
            y_labels=DAY_NAMES,
            x_labels=hour_ticks,
            color_low=(80, 30, 0),
            color_high=(255, 140, 0),
        )

        return self._chart_panel(
            "SPEND HEATMAP — COST BY HOUR × DAY", heatmap,
            f"Peak: {peak_label}  ◥  Total: {format_cost(total_cost)}",
        )

    # ── Session cost histogram ──

    def _build_cost_histogram(self) -> Widget:
        """Log-spaced bucket histogram with horizontal bars + percentile flags.

        Plotext's plt.hist with linear bins is dominated by the tiny-cost mass
        and hides the interesting tail. We bucket costs into intuitive dollar
        ranges (<$0.01, $0.01-$0.05, ..., >$5) and draw horizontal count bars
        with percentile markers inline.
        """
        costs = []
        for _dt, entry in _iter_individual_entries(self._ledger, self._source_filter):
            c = entry.get(FIELD_COST, 0)
            if c > 0:
                costs.append(c)

        if not costs:
            return self._chart_panel("No cost data", Label(""))

        costs.sort()
        median = costs[len(costs) // 2]
        p95 = costs[int(len(costs) * 0.95)]
        p99 = costs[int(len(costs) * 0.99)]
        mean = sum(costs) / len(costs)

        # Log-spaced buckets covering typical API call cost range
        bucket_edges = [0, 0.001, 0.005, 0.01, 0.05, 0.10, 0.25, 0.50,
                        1.00, 2.50, 5.00, float('inf')]
        bucket_labels = [
            "< $0.001", "$0.001–$0.005", "$0.005–$0.01",
            "$0.01–$0.05", "$0.05–$0.10", "$0.10–$0.25", "$0.25–$0.50",
            "$0.50–$1.00", "$1.00–$2.50", "$2.50–$5.00", "> $5.00",
        ]
        bucket_counts = [0] * len(bucket_labels)
        bucket_totals = [0.0] * len(bucket_labels)
        for c in costs:
            for i in range(len(bucket_edges) - 1):
                if bucket_edges[i] <= c < bucket_edges[i + 1]:
                    bucket_counts[i] += 1
                    bucket_totals[i] += c
                    break

        total_calls = len(costs)
        total_cost = sum(costs)
        max_count = max(bucket_counts) if bucket_counts else 1

        # Percentile positions mapped to bucket index
        def bucket_of(c: float) -> int:
            for i in range(len(bucket_edges) - 1):
                if bucket_edges[i] <= c < bucket_edges[i + 1]:
                    return i
            return len(bucket_counts) - 1
        median_b = bucket_of(median)
        p95_b = bucket_of(p95)
        p99_b = bucket_of(p99)

        bar_width = 40
        rows: list[Widget] = [
            Label(
                f" [#9999CC]Calls[/] [#FF9900]{total_calls:,}[/]  "
                f"[#9999CC]Total[/] [#FF9900]{format_cost(total_cost)}[/]  "
                f"[#9999CC]Median[/] [#FFCC99]{format_cost(median)}[/]  "
                f"[#9999CC]Mean[/] [#FFCC99]{format_cost(mean)}[/]  "
                f"[#9999CC]P95[/] [#FFCC99]{format_cost(p95)}[/]  "
                f"[#9999CC]P99[/] [#FFCC99]{format_cost(p99)}[/]",
                classes="ops-kv-line", markup=True,
            ),
            Label(" ", classes="ops-kv-line"),
            Label(
                f" [b]{'BUCKET':<16}[/b] {'COUNT':>7} {'SHARE':>6}  "
                f"{'DISTRIBUTION':<{bar_width}}  {'COST SUM':>8}",
                classes="ops-log-header", markup=True,
            ),
        ]

        for i, (lbl, count, tot) in enumerate(
            zip(bucket_labels, bucket_counts, bucket_totals)
        ):
            share = count / total_calls * 100 if total_calls else 0
            bar_len = int(count / max_count * bar_width) if max_count else 0
            bar = "█" * bar_len + "░" * (bar_width - bar_len)
            # Inline percentile flags at their bucket
            flags = ""
            if i == median_b:
                flags += "[#9999CC] ◀ med[/]"
            if i == p95_b:
                flags += "[#CC6699] ◀ p95[/]"
            if i == p99_b:
                flags += "[#FF9900] ◀ p99[/]"
            cost_str = format_cost(tot) if tot > 0 else "—"
            rows.append(Label(
                f" [#FFCC99]{lbl:<16}[/] [#FF9900]{count:>7,}[/] "
                f"[dim]{share:>5.1f}%[/]  "
                f"[#FF9900]{bar}[/]{flags}  [#CC9966]{cost_str:>8}[/]",
                classes="ops-kv-line", markup=True,
            ))

        rows.append(Label(" ", classes="ops-kv-line"))
        rows.append(Label(
            " [dim]Buckets are log-spaced; most calls cluster at the low end. "
            "Percentile markers (◀) flag where the median, P95, and P99 fall.[/]",
            classes="ops-kv-line", markup=True,
        ))

        panel = Vertical(*rows, classes="ops-panel")
        panel.border_title = "◖ COST DISTRIBUTION ◗"
        panel.border_subtitle = "per API call"
        return Vertical(panel, classes="chart-panel")

    # ── Cumulative cost chart ──

    def _build_cumulative_chart(self) -> Widget:
        sorted_days = sorted(self._daily.keys())
        dates = list(range(len(sorted_days)))

        cumulative = []
        running = 0.0
        for d in sorted_days:
            running += self._daily[d][FIELD_COST]
            cumulative.append(running)

        def draw(plt):
            plt.plot(dates, cumulative, marker="braille", color=(255, 153, 0))
            self._set_date_xticks(plt, sorted_days, dates, max_ticks=8)
            self._set_yticks(plt, cumulative, format_cost)

        subtitle = (
            f"{sorted_days[0]} → {sorted_days[-1]}  ◥  "
            f"Total: {format_cost(cumulative[-1])}"
            if cumulative else ""
        )
        return self._make_chart("CUMULATIVE COST", draw, subtitle)

    # ── RECENT tab ──────────────────────────────────────────────────────
    #
    # OPS shows "today since midnight"; RECENT shows the rolling-12h window
    # at 15-minute granularity. Three stacked bar charts (cost / requests /
    # tokens) plus a stat row (1h / 6h / 12h totals) give a true "what just
    # happened" lens that doesn't reset at midnight or wash out late at
    # night.

    def _build_recent(self) -> Widget:
        snap = self._snapshot
        if snap is None:
            return Vertical(classes="chart-panel")
        rv = snap.recent
        now = snap.clock.now

        # X-axis labels — show every 4 buckets (every hour) so labels read
        # cleanly. Use HH:MM format.
        bucket_labels = [
            t.strftime("%H:%M") if i % 4 == 0 else ""
            for i, t in enumerate(rv.bucket_starts)
        ]

        def draw_cost(plt):
            plt.bar(bucket_labels, rv.cost_series, color=(255, 153, 0))
            self._set_yticks(plt, rv.cost_series, format_cost)

        def draw_reqs(plt):
            plt.bar(bucket_labels, rv.request_series, color=(204, 102, 153))
            self._set_yticks(
                plt, [float(v) for v in rv.request_series],
                lambda v: f"{int(v)}",
            )

        def draw_tokens(plt):
            plt.bar(bucket_labels, rv.token_series, color=(153, 153, 204))
            self._set_yticks(
                plt, [float(v) for v in rv.token_series],
                lambda v: format_tokens(int(v), compact=True),
            )

        # Stat-band row: 1h / 6h / 12h, plus most-recent activity timestamp.
        if rv.last_call_dt is None:
            last_label = "no recent calls"
        else:
            age = now - rv.last_call_dt
            mins = int(age.total_seconds() // 60)
            if mins < 1:
                last_label = "just now"
            elif mins < 60:
                last_label = f"{mins}m ago"
            else:
                last_label = f"{mins // 60}h{mins % 60:02d}m ago"

        stat_row = Horizontal(
            self._build_recent_stat(
                "LAST 1h", "",
                value=format_cost(rv.cost_1h),
                detail=f"{rv.requests_1h:,} requests",
            ),
            self._build_recent_stat(
                "LAST 6h", "alt",
                value=format_cost(rv.cost_6h),
                detail=f"{rv.requests_6h:,} requests",
            ),
            self._build_recent_stat(
                "LAST 12h", "accent",
                value=format_cost(rv.cost_12h),
                detail=f"{rv.requests_12h:,} requests · "
                       f"{format_tokens(rv.tokens_12h)} tokens",
            ),
            self._build_recent_stat(
                "LATEST", "",
                value=last_label,
                detail=(rv.last_call_dt.strftime("%H:%M:%S")
                        if rv.last_call_dt else "—"),
            ),
            classes="ops-stat-row",
        )

        recent_summary = Vertical(
            stat_row,
            classes="ops-panel ops-panel-session",
        )
        recent_summary.border_title = "◖ RECENT ACTIVITY ◗"
        recent_summary.border_subtitle = (
            f"rolling {RECENT_WINDOW_HOURS}h window · "
            f"{RECENT_BUCKET_MIN}-min buckets"
        )

        # Three stacked sub-charts.
        cost_panel = self._make_chart(
            f"COST PER {RECENT_BUCKET_MIN}-MIN BUCKET ($)",
            draw_cost,
            f"12h total: {format_cost(rv.cost_12h)}  ◥  "
            f"peak: {format_cost(max(rv.cost_series, default=0))}",
        )
        req_panel = self._make_chart(
            f"REQUESTS PER {RECENT_BUCKET_MIN}-MIN BUCKET",
            draw_reqs,
            f"12h total: {rv.requests_12h:,}  ◥  "
            f"peak: {max(rv.request_series, default=0):,}",
        )
        tok_panel = self._make_chart(
            f"TOKENS PER {RECENT_BUCKET_MIN}-MIN BUCKET",
            draw_tokens,
            f"12h total: {format_tokens(rv.tokens_12h)}  ◥  "
            f"peak: {format_tokens(max(rv.token_series, default=0))}",
        )

        # Side-by-side ranking row: model mix + project mix in the window.
        ranking_panels = self._build_recent_ranking_panels(rv)

        children: List[Widget] = [recent_summary, cost_panel, req_panel, tok_panel]
        if ranking_panels is not None:
            children.append(ranking_panels)
        return Vertical(*children, classes="chart-panel chart-stack")

    def _build_recent_stat(self, label: str, accent: str,
                           value: str, detail: str) -> Vertical:
        """Static stat cell for the RECENT summary row.

        Unlike the OPS cells, this isn't bound to LiveLabel — RECENT
        rebuilds the whole tab on each tick (it's in `_CHART_TABS`), so a
        plain string is fine and avoids re-subscribing 4 watchers per cell.
        """
        cls = "ops-stat-cell"
        if accent:
            cls += f" ops-stat-cell-{accent}"
        return Vertical(
            Label(f" [#9999CC]{label}[/]",
                  classes="ops-stat-cell-label", markup=True),
            Label(f" [#FF9900]{value}[/]",
                  classes="ops-stat-cell-value", markup=True),
            Label(f" [dim]{detail}[/]",
                  classes="ops-stat-cell-detail", markup=True),
            classes=cls,
        )

    def _build_recent_ranking_panels(self, rv) -> Optional[Widget]:
        """Side-by-side MODEL / PROJECT cost panels for the 12h window."""
        panels: list[Widget] = []
        if rv.model_cost_12h:
            top = sorted(
                rv.model_cost_12h.items(), key=lambda x: x[1], reverse=True,
            )[:TOP_N_PANEL]
            max_v = max((c for _, c in top), default=1) or 1
            rows = [
                self._panel_row(
                    m, format_cost(c), c / max_v if max_v else 0, model_color(m),
                )
                for m, c in top
            ]
            panels.append(self._ranked_panel(
                "MODEL MIX (12h)",
                f"{len(rv.model_cost_12h)} models · "
                f"{format_cost(rv.cost_12h)} total",
                rows,
            ))
        if rv.project_cost_12h:
            top = sorted(
                rv.project_cost_12h.items(), key=lambda x: x[1], reverse=True,
            )[:TOP_N_PANEL]
            max_v = max((c for _, c in top), default=1) or 1
            rows = [
                self._panel_row(
                    p, format_cost(c), c / max_v if max_v else 0, "#FF9900",
                )
                for p, c in top
            ]
            panels.append(self._ranked_panel(
                "PROJECTS (12h)",
                f"{len(rv.project_cost_12h)} active",
                rows,
            ))
        if not panels:
            return None
        return Horizontal(*panels, classes="ops-side-by-side")

    # ── OPS tab ──


    def _refresh_log_rows(self) -> None:
        """Re-render OPS call-log rows from the current snapshot.

        Row markup depends on per-row state (selected, is_new) that isn't
        captured by the snapshot, so each tick we regenerate specs and
        mutate rows in place. A row-count change triggers a full tab
        rebuild — that's a structural edit the mounted widgets can't
        absorb.
        """
        snap = self._snapshot
        if snap is None:
            return
        entries = snap.ops_entries

        max_log_cost = max(
            (e.get(FIELD_COST, 0) for _, _, e in entries[:LOG_ROW_CAP]),
            default=0,
        )
        specs = build_row_specs(entries, max_log_cost)
        if len(specs) != len(self._ops_row_specs):
            self._render_tab("OPS")
            return
        self._ops_row_specs = specs
        try:
            rows = list(self.query(LogRow))
        except Exception:
            return
        if len(rows) != len(specs):
            self._render_tab("OPS")
            return
        for idx, (row, spec) in enumerate(zip(rows, specs)):
            text, row_class = self._render_row_spec(
                spec, selected=(idx == self._selected_row),
            )
            row.update(text)
            row.set_classes(row_class)

    def _build_ops(self) -> Widget:
        """Build the full OPS tab.

        Session-panel cells, hourly bar, and border subtitles all bind to
        the current snapshot through Live widgets — no OpsRefs, no hand-
        rolled in-place update. Ranking panels and the call log still
        rebuild on tab render, since their row set is structurally
        variable.
        """
        snap = self._snapshot
        if snap is None:
            return Vertical(classes="chart-panel")

        children: list[Widget] = [self._build_session_panel()]
        ranking_row = self._build_ranking_panels(snap.ops)
        if ranking_row is not None:
            children.append(ranking_row)
        children.append(self._build_log_panel(snap.ops_entries))

        return Vertical(*children, classes="chart-panel")

    # ── OPS panel builders ──

    def _build_live_stat_cell(self, label: str, accent: str,
                              value_selector, detail_selector) -> Vertical:
        """One big stat cell bound to snapshot selectors."""
        cls = "ops-stat-cell"
        if accent:
            cls += f" ops-stat-cell-{accent}"
        return Vertical(
            Label(f" [#9999CC]{label}[/]",
                  classes="ops-stat-cell-label", markup=True),
            LiveLabel(
                self._metrics,
                lambda s: f" [#FF9900]{value_selector(s)}[/]",
                classes="ops-stat-cell-value",
            ),
            LiveLabel(
                self._metrics,
                lambda s: f" [dim]{detail_selector(s)}[/]",
                classes="ops-stat-cell-detail",
            ),
            classes=cls,
        )

    def _build_session_panel(self) -> Widget:
        """Top SESSION STATS panel — 5 live stat cells over the hourly bar."""
        ops = lambda snap: snap.ops         # noqa: E731
        stats = lambda snap: snap.ops.stats  # noqa: E731

        session_row = Horizontal(
            self._build_live_stat_cell(
                "CALLS", "",
                value_selector=lambda s: f"{stats(s)['count']:,}",
                detail_selector=lambda s: f"{stats(s)['subagent_count']:,} subagent",
            ),
            self._build_live_stat_cell(
                "COST", "alt",
                value_selector=lambda s: format_cost(ops(s).today_cost),
                detail_selector=lambda s: f"{format_cost(ops(s).rate_per_hr)}/hr",
            ),
            self._build_live_stat_cell(
                "CACHE", "accent",
                value_selector=lambda s: f"{ops(s).cache_eff:.0f}%",
                detail_selector=lambda s: f"saved {format_cost(stats(s)['savings'])}",
            ),
            self._build_live_stat_cell(
                "TOKENS", "alt",
                value_selector=lambda s: format_tokens(
                    stats(s)['tokens_in'] + stats(s)['tokens_out']
                ),
                detail_selector=lambda s: (
                    f"{format_tokens(stats(s)['tokens_in'])} in · "
                    f"{format_tokens(stats(s)['tokens_out'])} out"
                ),
            ),
            self._build_live_stat_cell(
                "PER-CALL", "",
                value_selector=lambda s: format_cost(ops(s).median_cost),
                detail_selector=lambda s: (
                    f"P95 {format_cost(ops(s).p95_cost)} · "
                    f"max {format_cost(ops(s).max_call_cost)}"
                ),
            ),
            classes="ops-stat-row",
        )

        hourly_wrap = self._build_hourly_wrap()

        session_panel = Vertical(
            session_row, hourly_wrap,
            classes="ops-panel ops-panel-session",
        )
        session_panel.border_title = "◖ SESSION STATS ◗"
        session_panel.border_subtitle = "today"
        return session_panel

    def _build_hourly_wrap(self) -> Widget:
        """24-cell hourly bar + tick axis, bound to snapshot."""
        spark = LiveHourlyBar(
            self._metrics,
            lambda s: s.ops.hour_cost,
            classes="ops-hourly-spark",
        )
        axis_cells: list[Widget] = []
        for h in range(24):
            lbl = f"{h:02d}" if (h % 6 == 0 or h == 23) else " "
            axis_cells.append(Static(
                f"[dim]{lbl}[/]", classes="hourly-axis", markup=True,
            ))
        axis = Horizontal(*axis_cells, classes="ops-hourly-axis")
        # LiveBorderSubtitle is display:none — it exists only to mutate
        # `wrap.border_subtitle` when the snapshot changes. Constructing
        # it requires a `wrap` reference, so we build it after.
        wrap = Vertical(spark, axis, classes="ops-hourly-wrap")
        wrap.border_title = "◖ HOURLY ACTIVITY ◗"
        wrap.compose_add_child(LiveBorderSubtitle(
            self._metrics,
            lambda s: f"cost per hour · today {format_cost(s.ops.today_cost)}",
            parent_widget=wrap,
        ))
        return wrap

    def _build_ranking_panels(self, view: OpsView) -> Optional[Widget]:
        """Build the Projects / Models / Stops / Subagents side-by-side row."""
        s = view.stats
        panels: list[Widget] = []

        sorted_projects = sorted(s["project_cost"].items(),
                                 key=lambda x: x[1], reverse=True)
        if sorted_projects:
            panels.append(self._build_projects_panel(sorted_projects))

        if s["model_counts"]:
            panels.append(self._build_models_panel(s["model_counts"]))
        if s["stop_counts"]:
            panels.append(self._build_stops_panel(s["stop_counts"]))
        if s["subagent_type_counts"]:
            panels.append(self._build_subagents_panel(s["subagent_type_counts"]))

        if not panels:
            return None
        return Horizontal(*panels, classes="ops-side-by-side")

    def _build_projects_panel(self, sorted_projects: list) -> Widget:
        top = sorted_projects[:TOP_N_PANEL]
        max_cost = max((c for _, c in top), default=1) or 1
        rows = [
            self._panel_row(proj, format_cost(cost), cost / max_cost, "#FF9900")
            for proj, cost in top
        ]
        return self._ranked_panel(
            "ACTIVE PROJECTS", f"{len(sorted_projects)} total", rows,
        )

    def _build_models_panel(self, model_counts) -> Widget:
        segments = sorted(model_counts.items(),
                          key=lambda x: x[1], reverse=True)
        max_count = segments[0][1]
        total = sum(model_counts.values()) or 1
        rows = [
            self._panel_row(m, f"{c:,}", c / max_count, model_color(m))
            for m, c in segments
        ]
        return self._ranked_panel("MODEL MIX", f"{total:,} calls", rows)

    def _build_stops_panel(self, stop_counts) -> Widget:
        ordered = sorted(stop_counts.items(),
                         key=lambda x: x[1], reverse=True)
        max_c = ordered[0][1]
        total = sum(stop_counts.values()) or 1
        rows = [
            self._panel_row(
                sr, f"{c:,}", c / max_c, _STOP_COLORS.get(sr, "#CC9966"),
            )
            for sr, c in ordered
        ]
        return self._ranked_panel("STOP REASONS", f"{total:,} turns", rows)

    def _build_subagents_panel(self, subagent_types) -> Widget:
        top = subagent_types.most_common(TOP_N_PANEL)
        max_c = top[0][1]
        total = sum(subagent_types.values()) or 1
        rows: list[Widget] = [
            self._panel_row(
                (t.split(":", 1)[-1] if ":" in t else t),
                f"{c:,}", c / max_c, "#CC99CC",
            )
            for t, c in top
        ]
        if len(subagent_types) > TOP_N_PANEL:
            hidden = len(subagent_types) - TOP_N_PANEL
            hidden_n = sum(c for _, c in subagent_types.most_common()[TOP_N_PANEL:])
            rows.append(Label(
                f" [dim]+ {hidden} more · {hidden_n:,} calls[/]",
                classes="panel-row-footer", markup=True,
            ))
        return self._ranked_panel(
            "SUBAGENT TYPES",
            f"{total:,} spawns · {len(subagent_types)} types",
            rows,
        )

    # ── Ranked-panel primitives ──

    _PANEL_LABEL_W = 18

    @staticmethod
    def _esc_markup(s: str) -> str:
        """Escape `[` so user data can't inject Textual markup."""
        return s.replace("[", r"\[")

    @classmethod
    def _panel_row(cls, label: str, value_str: str, fraction: float,
                   fill: str) -> Horizontal:
        """One unified ranked-panel row: label+value, elastic bar, end caps.

        Bars use max-normalization (leader = 100% wide) so visual contrast
        reflects *relative rank*, not absolute share.
        """
        w = cls._PANEL_LABEL_W
        safe_label = cls._esc_markup(label[:w])
        safe_value = cls._esc_markup(value_str)
        return Horizontal(
            Label(
                f" [#FFCC99]{safe_label:<{w}}[/] "
                f"[#FF9900]{safe_value:>7}[/] ",
                classes="panel-row-label", markup=True,
            ),
            Static("[#CC6699]◖[/]", classes="bar-cap", markup=True),
            FluidBar(fraction, fill_color=fill, classes="fluid-bar"),
            Static("[#CC6699]◗[/]", classes="bar-cap", markup=True),
            classes="ops-labeled-bar",
        )

    @staticmethod
    def _ranked_panel(title: str, subtitle: str, rows: List[Widget]) -> Vertical:
        panel = Vertical(*rows, classes="ops-panel ops-panel-third")
        panel.border_title = f"◖ {title} ◗"
        panel.border_subtitle = subtitle
        return panel

    # ── Call log panel ──

    def _build_log_panel(self, entries: list) -> Widget:
        """Recent-calls log panel — header + up to LOG_ROW_CAP rows."""
        max_log_cost = max(
            (e.get(FIELD_COST, 0) for _, _, e in entries[:LOG_ROW_CAP]),
            default=0,
        )
        specs = build_row_specs(entries, max_log_cost)
        self._ops_row_specs = specs

        if self._selected_row >= len(specs):
            self._selected_row = len(specs) - 1 if specs else -1

        children: list[Widget] = [Label(
            f"   {'TIME':<8} {'MODEL':<12} {'IN':>5} {'OUT':>5} "
            f"{'CACHE':>11} {'COST':>7} {'·':<8} {'↳':<1} "
            f"{'TOOLS':<8} {'PROJECT':<14} ACTIVITY",
            classes="ops-log-header",
        )]
        for idx, spec in enumerate(specs):
            text, row_class = self._render_row_spec(
                spec, selected=(idx == self._selected_row),
            )
            children.append(LogRow(
                text, classes=row_class, markup=True, row_index=idx,
            ))

        panel = Vertical(*children, classes="ops-panel ops-panel-log")
        panel.border_title = "◖ CALL LOG ◗"
        panel.border_subtitle = f"{len(specs)} most recent"
        # Live subtitle: "N most recent" changes only on row-count flips,
        # which already trigger a tab rebuild — still, binding it keeps
        # subtitle/body in lockstep even if _refresh_log_rows shortcuts.
        panel.compose_add_child(LiveBorderSubtitle(
            self._metrics,
            lambda s: f"{min(len(s.ops_entries), LOG_ROW_CAP)} most recent",
            parent_widget=panel,
        ))
        return panel

    @staticmethod
    def _row_marker(spec: RowSpec, selected: bool, is_new: bool) -> str:
        """Priority ladder for the left-gutter marker glyph."""
        if selected:
            return "►"
        if spec.is_anchor:
            return "▶" if spec.is_turn_end else "◆"
        if is_new:
            return "★"
        return " "

    @staticmethod
    def _row_classes(spec: RowSpec, selected: bool, is_new: bool) -> str:
        """CSS class list for a log row. Mutually exclusive accents."""
        base = "ops-log-row"
        if selected:
            return base + " ops-log-row-selected"
        if is_new:
            return base + " ops-log-row-new"
        if not spec.is_anchor:
            return base + " ops-log-row-cont"
        if spec.is_subagent:
            return base + " ops-log-row-subagent"
        return base

    def _render_row_spec(self, spec: RowSpec, selected: bool) -> tuple:
        """Render a row spec into (markup_text, css_class_string)."""
        e = spec.entry
        dt = spec.dt
        short_m = short_model(e.get("model"))
        color = model_color(short_m)
        tok_in = format_number(e.get(FIELD_TOKENS_IN, 0))
        tok_out = format_number(e.get(FIELD_TOKENS_OUT, 0))
        cr = e.get(FIELD_CACHE_READS, 0)
        cw = e.get(FIELD_CACHE_WRITES, 0)
        cache_str = f"{format_number(cr)}/{format_number(cw)}"
        display_cost = e.get(FIELD_COST, 0) + spec.spawn_cost
        cost = format_cost(display_cost)
        bar = cost_bar(display_cost, spec.max_log_cost)
        proj = short_project(e.get("project", ""))[:14]
        kind_marker = "[#9999CC]↳[/]" if spec.is_subagent else " "
        time_str = dt.strftime("%H:%M:%S")
        tools_str = short_tools(e.get("tools") or [])[:8]

        activity = self._row_activity(spec)
        is_new = spec.entry_id in self._new_entry_ids
        marker = self._row_marker(spec, selected, is_new)
        row_classes = self._row_classes(spec, selected, is_new)

        text = (
            f"{marker} {time_str:<8} [{color}]{short_m:<12}[/] "
            f"{tok_in:>5} {tok_out:>5} {cache_str:>11} {cost:>7} "
            f"{bar:<8} {kind_marker} {tools_str:<8} {proj:<14} {activity}"
        )
        return text, row_classes

    @staticmethod
    def _row_activity(spec: RowSpec) -> str:
        """Activity column text for a call-log row."""
        if spec.is_anchor:
            activity = row_activity_text(spec.entry)
            if spec.spawn_cost > 0:
                activity += (f" [dim #9999CC](+{format_cost(spec.spawn_cost)} "
                             f"subagents)[/]")
            return activity
        # Continuation rows: tool chain if present, else a quiet dot.
        if spec.entry.get("tools"):
            return row_activity_text(spec.entry)
        return "[dim]  ⋮[/]"


if __name__ == "__main__":
    app = CostTrackerApp()
    app.run()
