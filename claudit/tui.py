"""LCARS-themed TUI dashboard for claudit."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

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
    calculate_totals,
    format_cost,
    format_number,
    format_tokens,
    init_field_dict,
)
from .ledger import get_ledger_path, load_ledger
from .pipeline import run_ingest
from .ops_data import (
    aggregate_today,
    collect_entries,
    cost_bar,
    hourly_cost_today,
    model_color,
    percentile,
    row_preview_text,
    short_model,
    short_project,
    short_tools,
    subagent_cost_rollup,
)
from .ops_widgets import (
    EntryDetailScreen,
    FluidBar,
    HelpScreen,
    HourlyBar,
    LogRow,
    StatBox,
)

TABS = ["OVERVIEW", "DAILY", "CUMULATIVE", "CALENDAR", "TOKENS",
        "CACHE", "REQUESTS", "COST MAP", "CALLS", "OPS"]


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


# ── Heatmap (plotext matrix) ──

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Background color for zero cells — dim gray so empty days are visible on dark bg
_HEATMAP_ZERO = (30, 30, 30)


def _grid_to_rgb(grid: List[List[float]],
                 color_zero: tuple = _HEATMAP_ZERO,
                 color_low: tuple = (0, 80, 0),
                 color_high: tuple = (0, 255, 100)) -> List[List[tuple]]:
    """Convert a float grid to RGB tuples for matrix_plot.

    Zero cells get color_zero; non-zero cells are interpolated between
    color_low and color_high based on their fraction of the grid max.
    """
    max_val = max((v for row in grid for v in row), default=0.0)
    rgb_grid = []
    for row in grid:
        rgb_row = []
        for v in row:
            if v == 0.0 or max_val == 0.0:
                rgb_row.append(color_zero)
            else:
                t = v / max_val
                r = int(color_low[0] + t * (color_high[0] - color_low[0]))
                g = int(color_low[1] + t * (color_high[1] - color_low[1]))
                b = int(color_low[2] + t * (color_high[2] - color_low[2]))
                rgb_row.append((r, g, b))
        rgb_grid.append(rgb_row)
    return rgb_grid


# ── Main app ──

class CostTrackerApp(App):
    CSS_PATH = Path(__file__).resolve().parent / "lcars.tcss"
    TITLE = "CLAUDIT"
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
        # Cache sorted entries for expand-row + scroll (set by _build_ops)
        self._ops_entries_cache: list = []

    def _load_data(self):
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

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield Static(datetime.now().strftime("%m·%d"), id="top-elbow")
            yield Static("CLAUDIT", id="top-title")
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

    def on_mount(self) -> None:
        self._load_data()
        self._update_status_bar()
        self._render_tab("OVERVIEW")
        self._refresh_timer = self.set_interval(
            self.REFRESH_INTERVAL, self._auto_refresh_tick
        )

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

    def _auto_refresh_tick(self) -> None:
        if not self._auto_refresh:
            return
        self._force_ingest = False
        self._load_data()
        self._update_status_bar()
        self._render_tab(self._current_tab)

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
            self._selected_row = 0
            self._render_tab("OPS")
            self._scroll_selected_into_view()
        else:
            try:
                self.query_one("#main-content", VerticalScroll).scroll_home(animate=False)
            except Exception:
                pass

    def action_jump_bottom(self) -> None:
        if self._current_tab == "OPS" and self._ops_entries_cache:
            self._selected_row = min(100, len(self._ops_entries_cache)) - 1
            self._render_tab("OPS")
            self._scroll_selected_into_view()
        else:
            try:
                self.query_one("#main-content", VerticalScroll).scroll_end(animate=False)
            except Exception:
                pass

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
                self._selected_row = 0 if delta > 0 else max_idx
            else:
                self._selected_row = max(0, min(max_idx, self._selected_row + delta))
            self._render_tab("OPS")
            self._scroll_selected_into_view()
            return
        try:
            scroller = self.query_one("#main-content", VerticalScroll)
        except Exception:
            return
        scroller.scroll_relative(y=delta, animate=False)

    def _scroll_selected_into_view(self) -> None:
        try:
            rows = list(self.query(LogRow))
            if 0 <= self._selected_row < len(rows):
                rows[self._selected_row].scroll_visible(animate=False)
        except Exception:
            pass

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
        self._selected_row = message.row_index
        self._render_tab("OPS")

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
            self._selected_row = -1
            self._render_tab("OPS")

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

    # ── Overview tab ──

    def _build_overview(self) -> Widget:
        sorted_days = sorted(self._daily.keys())
        last_30 = sorted_days[-30:] if len(sorted_days) > 30 else sorted_days
        data_30 = {d: self._daily[d] for d in last_30}
        totals = calculate_totals(data_30)

        today_str = datetime.now().strftime("%Y-%m-%d")
        today = datetime.now()
        today_data = self._daily.get(today_str, init_field_dict())

        last_7 = sorted_days[-7:] if len(sorted_days) >= 7 else sorted_days
        last_28 = sorted_days[-28:] if len(sorted_days) >= 28 else sorted_days

        spark_7d_cost = [self._daily[d][FIELD_COST] for d in last_7]

        week_costs: List[float] = []
        for i in range(0, len(last_28), 7):
            chunk = last_28[i:i + 7]
            week_costs.append(sum(self._daily[d][FIELD_COST] for d in chunk))
        spark_4w = week_costs if week_costs else [0.0]

        tokens_in_7d = [self._daily[d][FIELD_TOKENS_IN] for d in last_7]
        tokens_out_7d = [self._daily[d][FIELD_TOKENS_OUT] for d in last_7]
        spark_7d_tokens = [i + o for i, o in zip(tokens_in_7d, tokens_out_7d)]

        spark_7d_cache: List[float] = []
        for d in last_7:
            dd = self._daily[d]
            potential = dd[FIELD_COST] + dd[FIELD_CACHE_SAVINGS]
            spark_7d_cache.append(
                dd[FIELD_CACHE_SAVINGS] / potential * 100 if potential > 0 else 0
            )

        spark_30d_cost = [self._daily[d][FIELD_COST] for d in last_30]

        spark_burn: List[float] = []
        for i in range(len(last_30)):
            window = last_30[max(0, i - 6):i + 1]
            avg = sum(self._daily[d][FIELD_COST] for d in window) / len(window)
            spark_burn.append(avg)

        this_week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
        last_week_start = (today - timedelta(days=today.weekday() + 7)).strftime("%Y-%m-%d")
        this_week_cost = sum(
            d[FIELD_COST] for day, d in self._daily.items()
            if this_week_start <= day <= today_str
        )
        last_week_cost = sum(
            d[FIELD_COST] for day, d in self._daily.items()
            if last_week_start <= day < this_week_start
        )
        if last_week_cost > 0:
            wow_delta = ((this_week_cost - last_week_cost) / last_week_cost) * 100
            wow_detail = f"{'↑' if wow_delta >= 0 else '↓'} {abs(wow_delta):.0f}% vs last week"
        else:
            wow_detail = "no prior week data"

        potential_30 = totals[FIELD_COST] + totals[FIELD_CACHE_SAVINGS]
        cache_eff = (
            f"{totals[FIELD_CACHE_SAVINGS] / potential_30 * 100:.0f}% efficiency"
            if potential_30 > 0 else ""
        )

        burn_rate = sum(spark_7d_cost) / len(last_7) if last_7 else 0

        stats_row = Horizontal(
            StatBox("TODAY", format_cost(today_data[FIELD_COST]),
                    f"{today_data[FIELD_REQUESTS]:,} requests",
                    spark_data=spark_7d_cost, spark_label="7d cost ▸",
                    classes="stat-box"),
            StatBox("THIS WEEK", format_cost(this_week_cost),
                    wow_detail,
                    spark_data=spark_4w, spark_label="4wk weekly ▸",
                    classes="stat-box"),
            StatBox("30-DAY", format_cost(totals[FIELD_COST]),
                    f"{format_number(totals[FIELD_REQUESTS])} requests",
                    spark_data=spark_30d_cost, spark_label="30d daily ▸",
                    classes="stat-box"),
            StatBox("TOKENS (7d)", format_tokens(sum(spark_7d_tokens)),
                    f"{format_tokens(sum(tokens_in_7d))} in / "
                    f"{format_tokens(sum(tokens_out_7d))} out",
                    spark_data=spark_7d_tokens, spark_label="7d tokens ▸",
                    classes="stat-box"),
            StatBox("CACHE HIT", format_cost(totals[FIELD_CACHE_SAVINGS]),
                    cache_eff,
                    spark_data=spark_7d_cache, spark_label="7d efficiency ▸",
                    classes="stat-box"),
            StatBox("BURN RATE", f"{format_cost(burn_rate)}/day",
                    "7-day rolling avg",
                    spark_data=spark_burn, spark_label="30d avg ▸",
                    classes="stat-box"),
            id="overview-panel",
        )

        return Vertical(stats_row, classes="chart-panel")

    # ── Cost timeline chart ──

    def _build_cost_chart(self) -> Widget:
        sorted_days = sorted(self._daily.keys())[-60:]
        dates = list(range(len(sorted_days)))
        costs = [self._daily[d][FIELD_COST] for d in sorted_days]
        total_cost = sum(costs)

        plot = PlotextPlot()

        def on_mount_chart(event=None):
            plt = self._init_plt(plot)
            plt.plot(dates, costs, marker="braille", color=(255, 153, 0))
            self._set_date_xticks(plt, sorted_days, dates)
            self._set_yticks(plt, costs, format_cost)
            plot.refresh()

        plot.call_after_refresh(on_mount_chart)
        subtitle = (
            f"{sorted_days[0]} → {sorted_days[-1]}  ◥  "
            f"Total: {format_cost(total_cost)}  ◥  "
            f"Avg: {format_cost(total_cost / len(costs))}/day"
            if sorted_days else ""
        )
        return self._chart_panel("DAILY COST ($)", plot, subtitle)

    # ── Tokens chart ──

    def _build_tokens_chart(self) -> Widget:
        sorted_days = sorted(self._daily.keys())[-30:]
        tokens_in = [self._daily[d][FIELD_TOKENS_IN] for d in sorted_days]
        tokens_out = [self._daily[d][FIELD_TOKENS_OUT] for d in sorted_days]
        cache_w = [self._daily[d][FIELD_CACHE_WRITES] for d in sorted_days]
        cache_r = [self._daily[d][FIELD_CACHE_READS] for d in sorted_days]

        plot = PlotextPlot()

        def on_mount_chart(event=None):
            plt = self._init_plt(plot)
            labels = [d[5:] for d in sorted_days]
            plt.multiple_bar(
                labels,
                [tokens_in, tokens_out, cache_w, cache_r],
                labels=["Input", "Output", "Cache Write", "Cache Read"],
                color=[
                    (255, 153, 0),
                    (204, 102, 153),
                    (153, 153, 204),
                    (204, 153, 204),
                ],
            )
            all_tokens = tokens_in + tokens_out + cache_w + cache_r
            self._set_yticks(plt, all_tokens, lambda v: format_tokens(int(v), compact=True))
            plot.refresh()

        plot.call_after_refresh(on_mount_chart)
        return self._chart_panel("TOKEN USAGE BY DAY", plot)

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

        plot = PlotextPlot()

        def on_mount_chart(event=None):
            plt = self._init_plt(plot)
            plt.plot(dates, savings, marker="braille", label="Savings ($)",
                     color=(153, 153, 204))
            plt.plot(dates, pcts, marker="braille", label="Efficiency (%)",
                     color=(255, 153, 0), yside="right")
            self._set_date_xticks(plt, sorted_days, dates)
            self._set_yticks(plt, savings, format_cost, yside="left")
            self._set_yticks(plt, pcts, lambda v: f"{v:.0f}%", yside="right")
            plot.refresh()

        plot.call_after_refresh(on_mount_chart)
        subtitle = (
            f"Total saved: {format_cost(total_saved)}  ◥  "
            f"Avg efficiency: {sum(pcts) / len(pcts):.0f}%"
            if pcts else ""
        )
        return self._chart_panel("CACHE PERFORMANCE", plot, subtitle)

    # ── Activity heatmap ──

    def _build_activity(self) -> Widget:
        reqs_by_date = {d: self._daily[d][FIELD_REQUESTS] for d in self._daily}
        grid, month_ticks, month_labels, grid_start, grid_end = self._build_weeks_grid(
            {d: float(v) for d, v in reqs_by_date.items()}
        )

        total_requests = sum(reqs_by_date.values())
        active_days = sum(1 for v in reqs_by_date.values() if v > 0)
        peak_day = max(reqs_by_date, key=reqs_by_date.get) if reqs_by_date else None
        peak_count = reqs_by_date[peak_day] if peak_day else 0

        plot = PlotextPlot()

        def on_mount_chart(event=None):
            plt = self._init_plt(plot)
            plt.matrix_plot(_grid_to_rgb(list(reversed(grid))))
            plt.yticks(list(range(7)), list(reversed(DAY_NAMES)))
            if month_ticks:
                plt.xticks(month_ticks, month_labels)
            plt.title("Requests per day")
            plot.refresh()

        plot.call_after_refresh(on_mount_chart)
        subtitle = (
            f"{active_days} active days  ◥  "
            f"Total: {total_requests:,}  ◥  "
            f"Peak: {peak_day[5:] if peak_day else '—'} ({peak_count:,})"
        )
        return self._chart_panel(
            "ACTIVITY HEATMAP — REQUESTS (13 weeks)", plot, subtitle,
        )

    # ── Calendar heatmap (GitHub-style) ──

    CALENDAR_WEEKS = 13

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

        plot = PlotextPlot()

        def on_mount_chart(event=None):
            plt = self._init_plt(plot)
            plt.matrix_plot(_grid_to_rgb(list(reversed(grid))))
            plt.yticks(list(range(7)),
                       list(reversed(DAY_NAMES)))
            if month_ticks:
                plt.xticks(month_ticks, month_labels)
            plt.title("Daily cost intensity")
            plot.refresh()

        plot.call_after_refresh(on_mount_chart)
        subtitle = (
            f"{grid_start.strftime('%Y-%m-%d')} → "
            f"{grid_end.strftime('%Y-%m-%d')}  ◥  "
            f"{active_days} active days  ◥  "
            f"Total: {format_cost(total_cost)}  ◥  "
            f"Peak: {format_cost(max_cost)}"
        )
        return self._chart_panel(
            "CALENDAR HEATMAP — DAILY COST (13 weeks)", plot, subtitle,
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

        plot = PlotextPlot()

        def on_mount_chart(event=None):
            plt = self._init_plt(plot)
            plt.matrix_plot(_grid_to_rgb(
                list(reversed(grid)),
                color_low=(80, 30, 0),
                color_high=(255, 140, 0),
            ))
            plt.yticks(list(range(7)), list(reversed(DAY_NAMES)))
            plt.xticks(
                [i for i in range(24) if i % 3 == 0],
                [str(i) for i in range(24) if i % 3 == 0],
            )
            plt.title("Cost per hour ($)")
            plot.refresh()

        plot.call_after_refresh(on_mount_chart)
        return self._chart_panel(
            "SPEND HEATMAP — COST BY HOUR × DAY", plot,
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
        for dt, entry in _iter_individual_entries(self._ledger, self._source_filter):
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

        plot = PlotextPlot()

        def on_mount_chart(event=None):
            plt = self._init_plt(plot)
            plt.plot(dates, cumulative, marker="braille", color=(255, 153, 0))
            self._set_date_xticks(plt, sorted_days, dates, max_ticks=8)
            self._set_yticks(plt, cumulative, format_cost)
            plot.refresh()

        plot.call_after_refresh(on_mount_chart)
        subtitle = (
            f"{sorted_days[0]} → {sorted_days[-1]}  ◥  "
            f"Total: {format_cost(cumulative[-1])}"
            if cumulative else ""
        )
        return self._chart_panel("CUMULATIVE COST", plot, subtitle)

    # ── OPS tab ──


    def _build_ops(self) -> Widget:
        now = datetime.now()

        entries = collect_entries(self._ledger, self._source_filter)
        self._ops_entries_cache = entries

        s = aggregate_today(entries, short_project, short_model)

        if s["first_dt"]:
            elapsed_hrs = max((now - s["first_dt"]).total_seconds() / 3600, 0.01)
            rate_per_hr = s["cost"] / elapsed_hrs
        else:
            rate_per_hr = 0

        potential = s["cost"] + s["savings"]
        cache_eff = (s["savings"] / potential * 100) if potential > 0 else 0

        med = percentile(s["costs"], 0.5)
        p95 = percentile(s["costs"], 0.95)
        mx_call = s["costs"][-1] if s["costs"] else 0

        max_log_cost = max((e.get(FIELD_COST, 0) for _, _, e in entries[:100]),
                           default=0)

        sorted_projects = sorted(s["project_cost"].items(),
                                 key=lambda x: x[1], reverse=True)
        today_cost = s["cost"]
        hour_cost = hourly_cost_today(entries)

        # ── Session stats: grouped bigger stats + spacious layout ──
        # Alternating cell accents for LCARS multi-color feel
        def stat_cell(label: str, value: str, detail: str = "",
                      accent: str = ""):
            cls = "ops-stat-cell"
            if accent:
                cls += f" ops-stat-cell-{accent}"
            return Vertical(
                Label(f" [#9999CC]{label}[/]", classes="ops-stat-cell-label", markup=True),
                Label(f" [#FF9900]{value}[/]", classes="ops-stat-cell-value", markup=True),
                Label(f" [dim]{detail}[/]", classes="ops-stat-cell-detail", markup=True),
                classes=cls,
            )

        session_row = Horizontal(
            stat_cell("CALLS", f"{s['count']:,}",
                      f"{s['subagent_count']:,} subagent"),
            stat_cell("COST", format_cost(today_cost),
                      f"{format_cost(rate_per_hr)}/hr", accent="alt"),
            stat_cell("CACHE", f"{cache_eff:.0f}%",
                      f"saved {format_cost(s['savings'])}", accent="accent"),
            stat_cell("TOKENS",
                      format_tokens(s['tokens_in'] + s['tokens_out']),
                      f"{format_tokens(s['tokens_in'])} in · "
                      f"{format_tokens(s['tokens_out'])} out", accent="alt"),
            stat_cell("PER-CALL", format_cost(med),
                      f"P95 {format_cost(p95)} · max {format_cost(mx_call)}"),
            classes="ops-stat-row",
        )

        # 24-cell hourly bar — always full-width, ghost empties
        hourly_spark = HourlyBar(hour_cost, classes="ops-hourly-spark")
        # Hour tick labels underneath (00 ... 06 ... 12 ... 18 ... 23)
        hourly_axis_children: list[Widget] = []
        for h in range(24):
            if h % 6 == 0 or h == 23:
                lbl = f"{h:02d}"
            else:
                lbl = " "
            hourly_axis_children.append(
                Static(f"[dim]{lbl}[/]", classes="hourly-axis", markup=True)
            )
        hourly_axis = Horizontal(*hourly_axis_children, classes="ops-hourly-axis")

        hourly_wrap = Vertical(hourly_spark, hourly_axis,
                               classes="ops-hourly-wrap")
        hourly_wrap.border_title = "◖ HOURLY ACTIVITY ◗"
        hourly_wrap.border_subtitle = (
            f"cost per hour · today {format_cost(today_cost)}"
        )
        session_panel = Vertical(
            session_row,
            hourly_wrap,
            classes="ops-panel ops-panel-session",
        )
        session_panel.border_title = "◖ SESSION STATS ◗"
        session_panel.border_subtitle = "today"

        stats_children: list[Widget] = [session_panel]

        # ── Projects / Models / Stops / Subagents — 4 side-by-side panels ──
        #
        # Unified row format per panel:
        #   LABEL · VALUE   ◖▓▓▓▓▓░░░░░◗  PCT%
        #   [fixed-width label column on the left]  [elastic bar]  [right tail]
        #
        # Bars use max-normalization (leader = 100% wide) so the visual contrast
        # between rows reflects *relative rank*, not absolute share. This keeps
        # smaller entries legible even when one value dominates.
        LABEL_W = 18

        def _esc(s: str) -> str:
            """Escape `[` so user-data can't inject Textual markup."""
            return s.replace("[", r"\[")

        def _panel_row(label: str, value_str: str, fraction: float,
                       fill: str) -> Horizontal:
            """One unified panel row: label+value column, elastic bar, pct tail."""
            safe_label = _esc(label[:LABEL_W])
            safe_value = _esc(value_str)
            return Horizontal(
                Label(
                    f" [#FFCC99]{safe_label:<{LABEL_W}}[/] "
                    f"[#FF9900]{safe_value:>7}[/] ",
                    classes="panel-row-label", markup=True,
                ),
                Static("[#CC6699]◖[/]", classes="bar-cap", markup=True),
                FluidBar(fraction, fill_color=fill, classes="fluid-bar"),
                Static("[#CC6699]◗[/]", classes="bar-cap", markup=True),
                classes="ops-labeled-bar",
            )

        def _build_panel(title: str, subtitle: str,
                         rows: List[Widget]) -> Vertical:
            panel = Vertical(*rows, classes="ops-panel ops-panel-third")
            panel.border_title = f"◖ {title} ◗"
            panel.border_subtitle = subtitle
            return panel

        side_by_side_children: list[Widget] = []

        # ── Projects panel ────────────────────────────────────────────
        if sorted_projects:
            top_projects = sorted_projects[:6]
            max_cost = max((c for _, c in top_projects), default=1) or 1
            proj_rows = [
                _panel_row(
                    label=proj,
                    value_str=format_cost(cost),
                    fraction=cost / max_cost,
                    fill="#FF9900",
                )
                for proj, cost in top_projects
            ]
            side_by_side_children.append(_build_panel(
                "ACTIVE PROJECTS",
                f"{len(sorted_projects)} total",
                proj_rows,
            ))

        # ── Model mix panel ───────────────────────────────────────────
        model_counts = s["model_counts"]
        if model_counts:
            total_calls = sum(model_counts.values()) or 1
            segments = sorted(model_counts.items(),
                              key=lambda x: x[1], reverse=True)
            max_count = segments[0][1]
            model_rows = [
                _panel_row(
                    label=m,
                    value_str=f"{c:,}",
                    fraction=c / max_count,
                    fill=model_color(m),
                )
                for m, c in segments
            ]
            side_by_side_children.append(_build_panel(
                "MODEL MIX",
                f"{total_calls:,} calls",
                model_rows,
            ))

        # ── Stop reasons panel ────────────────────────────────────────
        stop_counts = s["stop_counts"]
        if stop_counts:
            total_stops = sum(stop_counts.values()) or 1
            stop_color_map = {
                "end_turn": "#9999CC",
                "tool_use": "#FF9900",
                "max_tokens": "#CC6699",
                "stop_sequence": "#CC9966",
                "—": "#555566",
            }
            ordered = sorted(stop_counts.items(),
                             key=lambda x: x[1], reverse=True)
            max_c = ordered[0][1]
            stop_rows = [
                _panel_row(
                    label=sr,
                    value_str=f"{c:,}",
                    fraction=c / max_c,
                    fill=stop_color_map.get(sr, "#CC9966"),
                )
                for sr, c in ordered
            ]
            side_by_side_children.append(_build_panel(
                "STOP REASONS",
                f"{total_stops:,} turns",
                stop_rows,
            ))

        # ── Subagent types panel ──────────────────────────────────────
        subagent_types = s["subagent_type_counts"]
        if subagent_types:
            total_spawns = sum(subagent_types.values()) or 1
            top_types = subagent_types.most_common(6)
            max_c = top_types[0][1]
            type_rows: list[Widget] = [
                _panel_row(
                    label=(t.split(":")[-1] if ":" in t else t),
                    value_str=f"{c:,}",
                    fraction=c / max_c,
                    fill="#CC99CC",
                )
                for t, c in top_types
            ]
            if len(subagent_types) > 6:
                hidden = len(subagent_types) - 6
                hidden_n = sum(c for _, c in subagent_types.most_common()[6:])
                type_rows.append(Label(
                    f" [dim]+ {hidden} more · {hidden_n:,} calls[/]",
                    classes="panel-row-footer", markup=True,
                ))
            side_by_side_children.append(_build_panel(
                "SUBAGENT TYPES",
                f"{total_spawns:,} spawns · {len(subagent_types)} types",
                type_rows,
            ))

        if side_by_side_children:
            stats_children.append(Horizontal(
                *side_by_side_children, classes="ops-side-by-side",
            ))

        # ── Call log ──
        log_children: list[Widget] = [Label(
            f"   {'TIME':<8} {'MODEL':<12} {'IN':>5} {'OUT':>5} "
            f"{'CACHE':>11} {'COST':>7} {'·':<8} {'↳':<1} "
            f"{'TOOLS':<8} {'PROJECT':<14} PROMPT",
            classes="ops-log-header",
        )]

        row_count = min(100, len(entries))
        if self._selected_row >= row_count:
            self._selected_row = row_count - 1 if row_count else -1

        visible = entries[:row_count]
        # Anchor logic: prefer promptId (true turn boundary). Within a run of
        # entries sharing a promptId, only the newest is shown as anchor.
        # Fallback: for entries missing promptId (pre-rescan data), use the
        # old end_turn + session-head heuristic.
        seen_prompt_ids: set = set()
        seen_sessions_with_anchor: set = set()
        sessions_with_end_turn = set()
        for _, _, e in visible:
            if (e.get("stopReason") or "") == "end_turn":
                sess = e.get("session") or ""
                if sess:
                    sessions_with_end_turn.add(sess)

        # Subagent-cost attribution: which sessions were spawned from today's
        # entries (maps parent_session → total subagent cost).
        sub_rollup = subagent_cost_rollup(entries)

        for idx, (dt, entry_id, e) in enumerate(visible):
            if e.get("source") == "agent_spawn":
                continue  # rendered implicitly via rollup, skip direct row
            prompt_id = e.get("promptId") or ""
            session = e.get("session") or ""
            stop = e.get("stopReason") or ""

            if prompt_id:
                # True anchor: first time we see this promptId (newest-first scan)
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

            short_m = short_model(e.get("model"))
            color = model_color(short_m)
            tok_in = format_number(e.get(FIELD_TOKENS_IN, 0))
            tok_out = format_number(e.get(FIELD_TOKENS_OUT, 0))
            cr = e.get(FIELD_CACHE_READS, 0)
            cw = e.get(FIELD_CACHE_WRITES, 0)
            cache_str = f"{format_number(cr)}/{format_number(cw)}"
            cost_val = e.get(FIELD_COST, 0)
            # Attribute subagent spend to parent turn anchor (C rollup)
            spawn_cost = 0.0
            if is_anchor and session and session in sub_rollup:
                spawn_cost = sub_rollup[session]
            display_cost = cost_val + spawn_cost
            cost = format_cost(display_cost)
            bar = cost_bar(display_cost, max_log_cost)
            proj = short_project(e.get("project", ""))[:14]
            is_subagent = bool(e.get("isSubagent"))
            kind_marker = "[#9999CC]↳[/]" if is_subagent else " "
            time_str = dt.strftime("%H:%M:%S")
            tools_str = short_tools(e.get("tools") or [])[:8]
            if is_anchor:
                preview = row_preview_text(e)
                if spawn_cost > 0:
                    preview += f" [dim #9999CC](+{format_cost(spawn_cost)} subagents)[/]"
            else:
                preview = "[dim]  ⋮[/]"
            is_new = entry_id in self._new_entry_ids
            is_selected = idx == self._selected_row
            if is_selected:
                marker = "►"
            elif is_anchor:
                marker = "▶" if is_turn_end else "◆"
            elif is_new:
                marker = "★"
            else:
                marker = " "

            row_classes = "ops-log-row"
            if is_selected:
                row_classes += " ops-log-row-selected"
            elif is_new:
                row_classes += " ops-log-row-new"
            elif not is_anchor:
                row_classes += " ops-log-row-cont"
            elif is_subagent:
                row_classes += " ops-log-row-subagent"

            log_children.append(LogRow(
                f"{marker} {time_str:<8} [{color}]{short_m:<12}[/] "
                f"{tok_in:>5} {tok_out:>5} {cache_str:>11} {cost:>7} "
                f"{bar:<8} {kind_marker} {tools_str:<8} {proj:<14} {preview}",
                classes=row_classes,
                markup=True,
                row_index=idx,
            ))

        log_panel = Vertical(*log_children, classes="ops-panel ops-panel-log")
        log_panel.border_title = "◖ CALL LOG ◗"
        log_panel.border_subtitle = f"{row_count} most recent"
        stats_children.append(log_panel)

        return Vertical(*stats_children, classes="chart-panel")


if __name__ == "__main__":
    app = CostTrackerApp()
    app.run()
