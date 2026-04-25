#!/usr/bin/env python3
"""LCARS-themed TUI dashboard for AI Cost Tracker."""

import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Label, Sparkline, Static

from textual_plotext import PlotextPlot

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claudit import (
    FIELD_CACHE_READS,
    FIELD_CACHE_SAVINGS,
    FIELD_CACHE_WRITES,
    FIELD_COST,
    FIELD_REQUESTS,
    FIELD_TOKENS_IN,
    FIELD_TOKENS_OUT,
    aggregate_by_day,
    calculate_averages,
    calculate_totals,
    entry_local_dt,
    format_cost,
    format_number,
    format_tokens,
    get_ledger_path,
    init_field_dict,
    load_ledger,
    run_ingest,
)

TABS = ["OVERVIEW", "COST", "TOKENS", "CACHE", "ACTIVITY",
        "CALENDAR", "SPEND", "HISTOGRAM", "CUMULATIVE"]


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


def aggregate_hourly_heatmap(ledger: Dict, source_filter: Optional[str] = None
                             ) -> List[List[int]]:
    """Build 7×24 grid of request counts (rows=days Mon-Sun, cols=hours)."""
    grid = [[0] * 24 for _ in range(7)]
    for dt, _ in _iter_individual_entries(ledger, source_filter):
        grid[dt.weekday()][dt.hour] += 1
    return grid


def aggregate_hourly_cost_heatmap(ledger: Dict, source_filter: Optional[str] = None
                                  ) -> List[List[float]]:
    """Build 7×24 grid of cost (rows=days Mon-Sun, cols=hours)."""
    grid = [[0.0] * 24 for _ in range(7)]
    for dt, entry in _iter_individual_entries(ledger, source_filter):
        grid[dt.weekday()][dt.hour] += entry.get(FIELD_COST, 0)
    return grid


def aggregate_by_hour(ledger: Dict, source_filter: Optional[str] = None
                      ) -> Dict[int, Dict]:
    """Aggregate ledger entries by hour of day (0-23)."""
    hourly = defaultdict(init_field_dict)
    for dt, entry in _iter_individual_entries(ledger, source_filter):
        h = hourly[dt.hour]
        h[FIELD_COST] += entry.get(FIELD_COST, 0)
        h[FIELD_TOKENS_IN] += entry.get(FIELD_TOKENS_IN, 0)
        h[FIELD_TOKENS_OUT] += entry.get(FIELD_TOKENS_OUT, 0)
        h[FIELD_REQUESTS] += 1
    return dict(hourly)


# ── Stat box widget ──

class StatBox(Static):
    """Single stat readout in LCARS style."""

    def __init__(self, label: str, value: str, detail: str = "", **kwargs):
        super().__init__(**kwargs)
        self._label = label
        self._value = value
        self._detail = detail

    def compose(self) -> ComposeResult:
        yield Label(self._label, classes="stat-label")
        yield Label(self._value, classes="stat-value")
        if self._detail:
            yield Label(self._detail, classes="stat-detail")



# ── Heatmap (plotext matrix) ──

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ── Main app ──

class CostTrackerApp(App):
    CSS_PATH = Path(__file__).resolve().parent / "lcars.tcss"
    TITLE = "CLAUDIT"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("1", "tab('OVERVIEW')", "Overview"),
        ("2", "tab('COST')", "Cost"),
        ("3", "tab('TOKENS')", "Tokens"),
        ("4", "tab('CACHE')", "Cache"),
        ("5", "tab('ACTIVITY')", "Activity"),
        ("6", "tab('CALENDAR')", "Calendar"),
        ("7", "tab('SPEND')", "Spend Map"),
        ("8", "tab('HISTOGRAM')", "Histogram"),
        ("9", "tab('CUMULATIVE')", "Cumulative"),
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

    def _load_data(self):
        ledger_path = get_ledger_path(self._ledger_path_override)
        self._ledger = load_ledger(ledger_path)

        run_ingest(ledger_path, self._ledger, source=self._source_filter_arg,
                   no_ingest=self._no_ingest, force_ingest=self._force_ingest)

        source_map = {'claude-code': 'cc', 'cline': 'cline'}
        self._source_filter = None if self._source_filter_arg == "all" else source_map.get(self._source_filter_arg, self._source_filter_arg)

        self._daily = aggregate_by_day(self._ledger, source_filter=self._source_filter)

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield Static(datetime.now().strftime("%m·%d"), id="top-elbow")
            yield Static("CLAUDIT", id="top-title")
            yield Static("", id="top-bar-line")

        with Horizontal():
            with Vertical(id="sidebar"):
                for i, tab_name in enumerate(TABS):
                    btn = Button(tab_name, id=f"nav-{tab_name.lower()}",
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
        entry_count = len(self._ledger)
        day_count = len(self._daily)
        status = self.query_one("#bottom-status", Static)
        status.update(f"  {entry_count:,} entries · {day_count} active days  ")
        self._render_tab("OVERVIEW")

    def _activate_nav(self, tab_name: str) -> None:
        for btn in self.query(".nav-button"):
            btn.remove_class("active")
        self.query_one(f"#nav-{tab_name.lower()}", Button).add_class("active")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id.startswith("nav-"):
            tab_name = btn_id.replace("nav-", "").upper()
            self._activate_nav(tab_name)
            self._render_tab(tab_name)

    def action_tab(self, tab_name: str) -> None:
        self._activate_nav(tab_name)
        self._render_tab(tab_name)

    def _render_tab(self, tab_name: str) -> None:
        builders = {
            "OVERVIEW": self._build_overview,
            "COST": self._build_cost_chart,
            "TOKENS": self._build_tokens_chart,
            "CACHE": self._build_cache_chart,
            "ACTIVITY": self._build_activity,
            "CALENDAR": self._build_calendar_heatmap,
            "SPEND": self._build_spend_heatmap,
            "HISTOGRAM": self._build_cost_histogram,
            "CUMULATIVE": self._build_cumulative_chart,
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

    # ── Overview tab ──

    def _build_overview(self) -> Widget:
        sorted_days = sorted(self._daily.keys())
        last_30 = sorted_days[-30:] if len(sorted_days) > 30 else sorted_days
        data_30 = {d: self._daily[d] for d in last_30}
        totals = calculate_totals(data_30)
        avgs = calculate_averages(totals, len(last_30))

        today_str = datetime.now().strftime("%Y-%m-%d")
        today_data = self._daily.get(today_str, init_field_dict())

        potential = totals[FIELD_COST] + totals[FIELD_CACHE_SAVINGS]
        cache_pct = (
            f"{totals[FIELD_CACHE_SAVINGS] / potential * 100:.0f}% of potential cost"
            if potential > 0 else ""
        )

        # Week-over-week cost comparison
        today = datetime.now()
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

        # Peak day
        if sorted_days:
            peak_day = max(sorted_days[-30:], key=lambda d: self._daily[d][FIELD_COST])
            peak_cost = self._daily[peak_day][FIELD_COST]
            peak_detail = f"{peak_day[5:]} ({format_cost(peak_cost)})"
        else:
            peak_detail = ""

        stats_row = Horizontal(
            StatBox("TODAY", format_cost(today_data[FIELD_COST]),
                    f"{today_data[FIELD_REQUESTS]:,} requests", classes="stat-box"),
            StatBox("THIS WEEK", format_cost(this_week_cost),
                    wow_detail, classes="stat-box"),
            StatBox("30-DAY TOTAL", format_cost(totals[FIELD_COST]),
                    f"{format_number(totals[FIELD_REQUESTS])} requests", classes="stat-box"),
            StatBox("DAILY AVG", format_cost(avgs[FIELD_COST]),
                    f"{format_number(int(avgs[FIELD_REQUESTS]))} req/day", classes="stat-box"),
            StatBox("CACHE SAVINGS", format_cost(totals[FIELD_CACHE_SAVINGS]),
                    cache_pct, classes="stat-box"),
            StatBox("PEAK DAY", peak_detail,
                    f"{format_tokens(totals[FIELD_TOKENS_IN])} in / "
                    f"{format_tokens(totals[FIELD_TOKENS_OUT])} out",
                    classes="stat-box"),
            id="overview-panel",
        )

        children: list[Widget] = [stats_row]
        cost_data = [data_30.get(d, init_field_dict())[FIELD_COST] for d in last_30]
        if cost_data:
            children.append(Label("  COST TREND (30 days)", classes="chart-title"))
            children.append(Sparkline(cost_data, summary_function=max))

        return Vertical(*children, classes="chart-panel")

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
        return Vertical(
            Label("  DAILY COST ($)", classes="chart-title"),
            Label(
                f"  {sorted_days[0]} → {sorted_days[-1]}  |  "
                f"Total: {format_cost(total_cost)}  |  "
                f"Avg: {format_cost(total_cost / len(costs))}/day"
                if sorted_days else "",
                classes="chart-subtitle",
            ),
            plot,
            classes="chart-panel",
        )

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
        return Vertical(
            Label("  TOKEN USAGE BY DAY", classes="chart-title"),
            plot,
            classes="chart-panel",
        )

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
        return Vertical(
            Label("  CACHE PERFORMANCE", classes="chart-title"),
            Label(
                f"  Total saved: {format_cost(total_saved)}  |  "
                f"Avg efficiency: {sum(pcts) / len(pcts):.0f}%"
                if pcts else "",
                classes="chart-subtitle",
            ),
            plot,
            classes="chart-panel",
        )

    # ── Activity heatmap ──

    def _build_activity(self) -> Widget:
        grid = aggregate_hourly_heatmap(self._ledger, self._source_filter)
        hourly = aggregate_by_hour(self._ledger, self._source_filter)

        peak_hour = max(hourly, key=lambda h: hourly[h][FIELD_REQUESTS]) if hourly else 0
        peak_count = hourly[peak_hour][FIELD_REQUESTS] if hourly else 0
        total_requests = sum(sum(row) for row in grid)

        plot = PlotextPlot()

        def on_mount_chart(event=None):
            plt = self._init_plt(plot)
            # matrix_plot wants rows top-to-bottom; reverse so Mon is top
            plt.matrix_plot(list(reversed(grid)))
            plt.yticks(list(range(7)), list(reversed(DAY_NAMES)))
            plt.xticks(
                [i for i in range(24) if i % 3 == 0],
                [str(i) for i in range(24) if i % 3 == 0],
            )
            plt.title("Requests per hour")
            plot.refresh()

        plot.call_after_refresh(on_mount_chart)
        return Vertical(
            Label("  ACTIVITY HEATMAP — REQUESTS BY HOUR × DAY", classes="chart-title"),
            Label(
                f"  Peak: {peak_hour:02d}:00 ({peak_count:,} requests)  |  "
                f"All-time total: {total_requests:,}",
                classes="chart-subtitle",
            ),
            plot,
            classes="chart-panel",
        )

    # ── Calendar heatmap (GitHub-style) ──

    def _build_calendar_heatmap(self) -> Widget:
        sorted_days = sorted(self._daily.keys())
        if not sorted_days:
            return Vertical(Label("  No data", classes="chart-title"),
                            classes="chart-panel")

        last_90 = sorted_days[-90:]
        cost_by_date = {d: self._daily[d][FIELD_COST] for d in last_90}
        max_cost = max(cost_by_date.values()) if cost_by_date else 1

        first = datetime.strptime(last_90[0], "%Y-%m-%d")
        last = datetime.strptime(last_90[-1], "%Y-%m-%d")

        # Build week columns (Sun=top → Sat=bottom to match GitHub)
        # Each column is one ISO week; rows are weekdays Mon(0)→Sun(6)
        num_days = (last - first).days + 1
        start_weekday = first.weekday()  # Mon=0

        # Pad to start on Monday
        grid_start = first - timedelta(days=start_weekday)
        grid_end = last + timedelta(days=(6 - last.weekday()))
        total_days = (grid_end - grid_start).days + 1
        num_weeks = total_days // 7

        grid = [[0.0] * num_weeks for _ in range(7)]
        for w in range(num_weeks):
            for d in range(7):
                date = grid_start + timedelta(days=w * 7 + d)
                date_str = date.strftime("%Y-%m-%d")
                if date_str in cost_by_date:
                    grid[d][w] = cost_by_date[date_str]

        total_cost = sum(cost_by_date.values())
        active_days = len([v for v in cost_by_date.values() if v > 0])

        # Month labels on x-axis
        month_ticks = []
        month_labels = []
        for w in range(num_weeks):
            date = grid_start + timedelta(days=w * 7)
            if date.day <= 7:
                month_ticks.append(w)
                month_labels.append(date.strftime("%b"))

        plot = PlotextPlot()

        def on_mount_chart(event=None):
            plt = self._init_plt(plot)
            plt.matrix_plot(list(reversed(grid)))
            plt.yticks(list(range(7)),
                       list(reversed(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])))
            if month_ticks:
                plt.xticks(month_ticks, month_labels)
            plt.title("Daily cost intensity")
            plot.refresh()

        plot.call_after_refresh(on_mount_chart)
        return Vertical(
            Label("  CALENDAR HEATMAP — DAILY COST (90 days)", classes="chart-title"),
            Label(
                f"  {last_90[0]} → {last_90[-1]}  |  "
                f"{active_days} active days  |  "
                f"Total: {format_cost(total_cost)}  |  "
                f"Peak: {format_cost(max_cost)}",
                classes="chart-subtitle",
            ),
            plot,
            classes="chart-panel",
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
            plt.matrix_plot(list(reversed(grid)))
            plt.yticks(list(range(7)), list(reversed(DAY_NAMES)))
            plt.xticks(
                [i for i in range(24) if i % 3 == 0],
                [str(i) for i in range(24) if i % 3 == 0],
            )
            plt.title("Cost per hour ($)")
            plot.refresh()

        plot.call_after_refresh(on_mount_chart)
        return Vertical(
            Label("  SPEND HEATMAP — COST BY HOUR × DAY", classes="chart-title"),
            Label(
                f"  Peak: {peak_label}  |  "
                f"Total: {format_cost(total_cost)}",
                classes="chart-subtitle",
            ),
            plot,
            classes="chart-panel",
        )

    # ── Session cost histogram ──

    def _build_cost_histogram(self) -> Widget:
        costs = []
        for dt, entry in _iter_individual_entries(self._ledger, self._source_filter):
            c = entry.get(FIELD_COST, 0)
            if c > 0:
                costs.append(c)

        if not costs:
            return Vertical(Label("  No cost data", classes="chart-title"),
                            classes="chart-panel")

        costs.sort()
        median = costs[len(costs) // 2]
        p95 = costs[int(len(costs) * 0.95)]
        p99 = costs[int(len(costs) * 0.99)]
        mean = sum(costs) / len(costs)

        plot = PlotextPlot()

        def on_mount_chart(event=None):
            plt = self._init_plt(plot)
            bins = min(50, max(10, len(costs) // 100))
            plt.hist(costs, bins=bins, color=(255, 153, 0))
            plt.xlabel("Cost per API call ($)")
            plt.ylabel("Count")
            plot.refresh()

        plot.call_after_refresh(on_mount_chart)
        return Vertical(
            Label("  COST DISTRIBUTION — PER API CALL", classes="chart-title"),
            Label(
                f"  {len(costs):,} calls  |  "
                f"Median: {format_cost(median)}  |  "
                f"Mean: {format_cost(mean)}  |  "
                f"P95: {format_cost(p95)}  |  "
                f"P99: {format_cost(p99)}",
                classes="chart-subtitle",
            ),
            plot,
            classes="chart-panel",
        )

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

        children: list[Widget] = [
            Label("  CUMULATIVE COST", classes="chart-title"),
        ]
        if cumulative:
            children.append(Label(
                f"  {sorted_days[0]} → {sorted_days[-1]}  |  "
                f"Total: {format_cost(cumulative[-1])}",
                classes="chart-subtitle",
            ))
        children.append(plot)
        return Vertical(*children, classes="chart-panel")


if __name__ == "__main__":
    app = CostTrackerApp()
    app.run()
