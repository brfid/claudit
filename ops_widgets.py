"""Textual widgets used by the claudit OPS dashboard.

These are generic, stateless presentation components. They know how to render
themselves given data; they do not know about the ledger, aggregation, or the
app shell.
"""

from typing import Dict, List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Click
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Label, Sparkline, Static

from claudit import (
    FIELD_CACHE_READS,
    FIELD_CACHE_SAVINGS,
    FIELD_CACHE_WRITES,
    FIELD_COST,
    FIELD_TOKENS_IN,
    FIELD_TOKENS_OUT,
)


# ── Static readout ────────────────────────────────────────────────────────

class StatBox(Static):
    """Single stat readout with optional sparkline below."""

    def __init__(self, label: str, value: str, detail: str = "",
                 spark_data: Optional[List[float]] = None,
                 spark_label: str = "", **kwargs):
        super().__init__(**kwargs)
        self._label = label
        self._value = value
        self._detail = detail
        self._spark_data = spark_data
        self._spark_label = spark_label

    def compose(self) -> ComposeResult:
        yield Label(self._label, classes="stat-label")
        yield Label(self._value, classes="stat-value")
        if self._detail:
            yield Label(self._detail, classes="stat-detail")
        if self._spark_data and any(v > 0 for v in self._spark_data):
            yield Sparkline(self._spark_data, summary_function=max)
            if self._spark_label:
                yield Label(self._spark_label, classes="spark-caption")


# ── Fluid-width bars ──────────────────────────────────────────────────────

class _FluidBarBase(Static):
    """Base for bars that redraw when their container width changes."""

    def _draw(self, width: int) -> str:
        raise NotImplementedError

    def on_resize(self, event) -> None:
        self.update(self._draw(event.size.width))

    def on_mount(self) -> None:
        if self.size.width > 0:
            self.update(self._draw(self.size.width))


class FluidBar(_FluidBarBase):
    """Proportional horizontal fill bar; `fraction` ∈ [0, 1].

    If `label` is given and the filled portion has enough room (≥ len+2), the
    label is drawn inside the fill in `label_color`. Otherwise the bar renders
    plain and the caller can place a label next to it externally.
    """

    def __init__(self, fraction: float, fill_color: str = "#FF9900",
                 empty_color: str = "#3a3a4a", label: str = "",
                 label_color: str = "#000000", **kwargs):
        super().__init__("", **kwargs)
        self._fraction = max(0.0, min(1.0, fraction))
        self._fill_color = fill_color
        self._empty_color = empty_color
        self._label = label
        self._label_color = label_color

    def _draw(self, width: int) -> str:
        if width <= 0:
            return ""
        fill = max(0, min(width, int(self._fraction * width)))
        empty = width - fill
        fill_block = "█" * fill
        if self._label and fill >= len(self._label) + 2:
            # Center the label inside the fill
            pad = (fill - len(self._label)) // 2
            fill_block = (
                "█" * pad
                + self._label
                + "█" * (fill - pad - len(self._label))
            )
            return (
                f"[{self._label_color} on {self._fill_color}]{fill_block}[/]"
                f"[{self._empty_color}]{'░' * empty}[/]"
            )
        return (
            f"[{self._fill_color}]{fill_block}[/]"
            f"[{self._empty_color}]{'░' * empty}[/]"
        )


class StackedBar(_FluidBarBase):
    """Horizontal bar split into colored segments by proportion.

    segments: list of (label, value, color). Zero-value segments are skipped.
    Each segment's label is drawn inside the segment when it fits (≥ len+2).
    """

    LABEL_COLOR = "#000000"

    def __init__(self, segments, show_labels: bool = True, **kwargs):
        super().__init__("", **kwargs)
        self._segments = [(lbl, v, c) for lbl, v, c in segments if v > 0]
        self._show_labels = show_labels

    def _draw(self, width: int) -> str:
        if width <= 0 or not self._segments:
            return ""
        total = sum(v for _, v, _ in self._segments) or 1
        widths = []
        remaining = width
        for i, (_, v, _) in enumerate(self._segments):
            if i == len(self._segments) - 1:
                widths.append(remaining)
            else:
                w = max(1, int(v / total * width))
                w = min(w, remaining - (len(self._segments) - i - 1))
                widths.append(w)
                remaining -= w

        out = []
        for (label, _, color), w in zip(self._segments, widths):
            if self._show_labels and label and w >= len(label) + 2:
                pad = (w - len(label)) // 2
                content = "█" * pad + label + "█" * (w - pad - len(label))
                out.append(
                    f"[{self.LABEL_COLOR} on {color}]{content}[/]"
                )
            else:
                out.append(f"[{color} on {color}]{'█' * w}[/]")
        return "".join(out)


# ── Hourly activity bar ───────────────────────────────────────────────────

class HourlyBar(Horizontal):
    """24 equal-width cells, each a block glyph scaled to its hour's value.

    Empty hours render a ghost `░` so the 24-hour frame is always visible.
    """

    BLOCKS = " ▁▂▃▄▅▆▇█"
    EMPTY_GLYPH = "░"
    EMPTY_COLOR = "#3a3a4a"
    FILL_COLOR = "#FF9900"

    def __init__(self, values: List[float], **kwargs):
        super().__init__(**kwargs)
        if len(values) != 24:
            raise ValueError(f"HourlyBar expects 24 values, got {len(values)}")
        self._values = values

    def compose(self) -> ComposeResult:
        mx = max(self._values) if any(self._values) else 1.0
        for hour, v in enumerate(self._values):
            if v <= 0:
                glyph, color = self.EMPTY_GLYPH, self.EMPTY_COLOR
            else:
                idx = max(1, int(v / mx * (len(self.BLOCKS) - 1)))
                glyph, color = self.BLOCKS[idx], self.FILL_COLOR
            classes = "hourly-cell"
            if hour % 6 == 0 and hour != 0:
                classes += " hourly-cell-mark"
            yield Static(f"[{color}]{glyph}[/]",
                         classes=classes, markup=True)


# ── Interactive log row ───────────────────────────────────────────────────

class LogRow(Label):
    """Label that posts a Clicked message carrying its row index."""

    class Clicked(Message):
        def __init__(self, row_index: int):
            super().__init__()
            self.row_index = row_index

    def __init__(self, *args, row_index: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.row_index = row_index

    def on_click(self, event: Click) -> None:
        self.post_message(self.Clicked(self.row_index))


# ── Detail modal ──────────────────────────────────────────────────────────

class EntryDetailScreen(ModalScreen):
    """Modal showing full details of a single ledger entry."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
        Binding("enter", "dismiss", "Close"),
    ]

    def __init__(self, dt, entry_id: str, entry: Dict):
        super().__init__()
        self._dt = dt
        self._entry_id = entry_id
        self._entry = entry

    def compose(self) -> ComposeResult:
        e = self._entry
        preview = e.get("promptPreview") or "—"
        lines = [
            f"[b]{self._entry_id}[/b]",
            "",
            f"Time:     {self._dt.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Source:   {e.get('source', '?')}",
            f"Model:    {e.get('model') or '—'}",
            f"Project:  {e.get('project') or '—'}",
            f"Session:  {e.get('session') or '—'}",
            f"Subagent: {'yes' if e.get('isSubagent') else 'no'}",
            f"Stop:     {e.get('stopReason') or '—'}",
            "",
            f"Tokens:   in={e.get(FIELD_TOKENS_IN, 0):,}  "
            f"out={e.get(FIELD_TOKENS_OUT, 0):,}  "
            f"cache_w={e.get(FIELD_CACHE_WRITES, 0):,}  "
            f"cache_r={e.get(FIELD_CACHE_READS, 0):,}",
            f"Cost:     ${e.get(FIELD_COST, 0):.4f}  "
            f"(saved ${e.get(FIELD_CACHE_SAVINGS, 0):.4f})",
            "",
            "[b]Prompt preview:[/b]",
            preview,
            "",
            "[dim]esc / q / enter to close[/dim]",
        ]
        yield Vertical(
            Static("\n".join(lines), id="entry-detail-body"),
            id="entry-detail-box",
        )
