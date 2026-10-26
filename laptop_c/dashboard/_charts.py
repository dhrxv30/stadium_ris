"""
Stage D5: Pure-Tkinter Canvas trend charts for the Stadium RIS dashboard.

No external dependencies beyond the Python standard library.
Two chart types:
  SparklineChart  - single metric over time (e.g., global PDR %)
  MultiLineChart  - one line per zone (e.g., per-zone SINR or queue %)

None values in the series create visible gaps in the line ("N/A" periods).
Styled for the light theme in _theme.py.
"""
from __future__ import annotations

import tkinter as tk
from typing import Dict, List, Optional, Tuple

from laptop_c.dashboard import _theme as T

_BG       = T.SURFACE
_GRID     = "#e8eaed"
_TEXT_DIM = T.TEXT_DIM
_FAINT    = T.TEXT_FAINT

ZONE_PALETTE: Dict[str, str] = dict(T.ZONE_CLR)


class _BaseChart:
    """Shared canvas-drawing helpers.  Not instantiated directly."""

    # left, right, top, bottom padding inside canvas
    _PAD = (44, 24, 22, 22)

    def __init__(self, canvas: tk.Canvas) -> None:
        self.canvas = canvas
        canvas.configure(bg=_BG)

    def _clear(self) -> None:
        self.canvas.delete("all")

    def _dims(self) -> Tuple[int, int, int, int, int, int]:
        """(w, h, x0_plot, y0_plot, x1_plot, y1_plot)"""
        c = self.canvas
        w = c.winfo_width()  or int(c.cget("width"))
        h = c.winfo_height() or int(c.cget("height"))
        pl, pr, pt, pb = self._PAD
        return w, h, pl, pt, w - pr, h - pb

    def _draw_bg(self, w: int, h: int) -> None:
        self.canvas.create_rectangle(0, 0, w, h, fill=_BG, outline="")

    def _draw_axes(self, x0: int, y0: int, x1: int, y1: int,
                   ymin: float, ymax: float, unit: str = "", n: int = 4) -> None:
        """Horizontal grid lines, y-axis value labels, and a time hint."""
        for i in range(n + 1):
            frac = i / n
            y = y0 + (1.0 - frac) * (y1 - y0)
            self.canvas.create_line(x0, y, x1, y, fill=_GRID)
            val = ymin + frac * (ymax - ymin)
            self.canvas.create_text(
                x0 - 8, y, text=f"{val:.0f}{unit}",
                fill=_FAINT, anchor="e", font=T.font(8)
            )
        self.canvas.create_text(x0, y1 + 11, text="older", fill=_FAINT,
                                anchor="w", font=T.font(8))
        self.canvas.create_text(x1, y1 + 11, text="now", fill=_FAINT,
                                anchor="e", font=T.font(8))

    def _to_pixels(
        self,
        series: List[Tuple],
        x0: int, y0: int, x1: int, y1: int,
        ymin: float, ymax: float,
    ) -> List[Optional[Tuple[float, float]]]:
        """Map (ts, value_or_None) series to [(px,py), ...] with None for gaps."""
        n = len(series)
        if n == 0:
            return []
        span_y = (ymax - ymin) or 1.0
        span_x = (x1 - x0) or 1
        pts: List[Optional[Tuple[float, float]]] = []
        for i, (_ts, v) in enumerate(series):
            px = x0 + (i / (n - 1)) * span_x if n > 1 else x0
            if v is None:
                pts.append(None)
            else:
                frac = (v - ymin) / span_y
                py = y0 + (1.0 - frac) * (y1 - y0)
                pts.append((px, py))
        return pts

    def _draw_polyline(
        self,
        pts: List[Optional[Tuple[float, float]]],
        color: str,
        width: int = 2,
        area_to: Optional[float] = None,
    ) -> None:
        """Draw line segments, breaking on None values.

        If *area_to* (a y pixel) is given, a pale fill is drawn beneath each
        segment down to that baseline.
        """
        def flush(seg: List[float]) -> None:
            if len(seg) >= 4:
                if area_to is not None:
                    self.canvas.create_polygon(
                        seg + [seg[-2], area_to, seg[0], area_to],
                        fill=T.blend(color, 0.12), outline="")
                self.canvas.create_line(seg, fill=color, width=width, smooth=False,
                                        capstyle="round", joinstyle="round")

        seg: List[float] = []
        for pt in pts:
            if pt is None:
                flush(seg)
                seg = []
            else:
                seg.extend(pt)
        flush(seg)

    def _end_dot(self, pts: List[Optional[Tuple[float, float]]], color: str,
                 r: int = 3) -> None:
        last = next((p for p in reversed(pts) if p is not None), None)
        if last:
            x, y = last
            self.canvas.create_oval(x - r, y - r, x + r, y + r,
                                    fill=color, outline=_BG, width=2)

    @staticmethod
    def _auto_scale(series: List[Tuple]) -> Tuple[float, float]:
        vals = [v for _ts, v in series if v is not None]
        if not vals:
            return 0.0, 1.0
        lo, hi = min(vals), max(vals)
        if lo == hi:
            lo -= 0.5; hi += 0.5
        return lo, hi

    def _no_data(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self.canvas.create_text(
            (x0 + x1) // 2, (y0 + y1) // 2,
            text="N/A", fill=_FAINT, font=T.font(11)
        )


class SparklineChart(_BaseChart):
    """Single-metric trend line on a Canvas.

    Parameters
    ----------
    canvas   : the tk.Canvas to draw on
    label    : short title shown top-left
    color    : line colour (hex)
    unit     : appended to axis labels and current-value readout
    ymin/ymax: if given, fix the y-axis range; otherwise auto-scaled
    """

    def __init__(
        self,
        canvas: tk.Canvas,
        label: str = "",
        color: str = "#1a73e8",
        unit: str = "",
        ymin: Optional[float] = None,
        ymax: Optional[float] = None,
    ) -> None:
        super().__init__(canvas)
        self.label = label
        self.color = color
        self.unit  = unit
        self._fixed_ymin = ymin
        self._fixed_ymax = ymax

    def update(self, series: List[Tuple]) -> None:
        """Redraw the chart from *series* = [(ts, value_or_None), ...]."""
        self._clear()
        w, h, x0, y0, x1, y1 = self._dims()
        self._draw_bg(w, h)

        if self.label:
            self.canvas.create_text(
                x0, y0 - 12, text=self.label,
                fill=_TEXT_DIM, anchor="w", font=T.font(9, "bold")
            )

        vals = [v for _ts, v in series if v is not None]
        if not vals:
            self._no_data(x0, y0, x1, y1)
            return

        ymin = self._fixed_ymin if self._fixed_ymin is not None else min(vals)
        ymax = self._fixed_ymax if self._fixed_ymax is not None else max(vals)
        if ymin == ymax:
            ymin -= 0.5; ymax += 0.5

        self._draw_axes(x0, y0, x1, y1, ymin, ymax, self.unit)
        pts = self._to_pixels(series, x0, y0, x1, y1, ymin, ymax)
        self._draw_polyline(pts, self.color, width=2, area_to=y1)
        self._end_dot(pts, self.color)

        # Current value top-right
        self.canvas.create_text(
            x1, y0 - 12,
            text=f"{vals[-1]:.1f}{self.unit}",
            fill=self.color, anchor="e", font=T.font(10, "bold")
        )


class MultiLineChart(_BaseChart):
    """One coloured line per zone (Z1–Z5) on a single Canvas.

    Parameters
    ----------
    canvas   : the tk.Canvas to draw on
    label    : short title shown top-left
    unit     : appended to y-axis labels
    ymin/ymax: fix y-axis range; otherwise computed from all zones together
    """

    _ZONES = ("Z1", "Z2", "Z3", "Z4", "Z5")

    def __init__(
        self,
        canvas: tk.Canvas,
        label: str = "",
        unit: str = "",
        ymin: Optional[float] = None,
        ymax: Optional[float] = None,
    ) -> None:
        super().__init__(canvas)
        self.label = label
        self.unit  = unit
        self._fixed_ymin = ymin
        self._fixed_ymax = ymax

    def update(self, series_by_zone: Dict[str, List[Tuple]]) -> None:
        """Redraw. *series_by_zone* maps zone-id -> [(ts, value_or_None)]."""
        self._clear()
        w, h, x0, y0, x1, y1 = self._dims()
        self._draw_bg(w, h)

        if self.label:
            self.canvas.create_text(
                x0, y0 - 12, text=self.label,
                fill=_TEXT_DIM, anchor="w", font=T.font(9, "bold")
            )

        # Global scale
        all_vals: List[float] = []
        for zid in self._ZONES:
            all_vals.extend(v for _ts, v in series_by_zone.get(zid, []) if v is not None)

        if not all_vals:
            self._no_data(x0, y0, x1, y1)
            self._draw_legend(x1, y0)
            return

        ymin = self._fixed_ymin if self._fixed_ymin is not None else min(all_vals)
        ymax = self._fixed_ymax if self._fixed_ymax is not None else max(all_vals)
        if ymin == ymax:
            ymin -= 0.5; ymax += 0.5

        self._draw_axes(x0, y0, x1, y1, ymin, ymax, self.unit)

        for zid in self._ZONES:
            series = series_by_zone.get(zid, [])
            if not series:
                continue
            color = ZONE_PALETTE.get(zid, _TEXT_DIM)
            pts = self._to_pixels(series, x0, y0, x1, y1, ymin, ymax)
            self._draw_polyline(pts, color, width=2)
            self._end_dot(pts, color, r=3)

            # Zone label just right of the last known value
            last_v = next((v for _ts, v in reversed(series) if v is not None), None)
            if last_v is not None:
                span = (ymax - ymin) or 1.0
                py = y0 + (1.0 - (last_v - ymin) / span) * (y1 - y0)
                self.canvas.create_text(
                    x1 + 7, py, text=zid,
                    fill=color, anchor="w", font=T.font(8, "bold")
                )

        self._draw_legend(x1, y0)

    def _draw_legend(self, x1: int, y0: int) -> None:
        """Compact zone legend at top-right: coloured dot + zone id."""
        lx = x1
        for zid in reversed(self._ZONES):
            color = ZONE_PALETTE.get(zid, _TEXT_DIM)
            self.canvas.create_text(
                lx, y0 - 12, text=zid,
                fill=_TEXT_DIM, anchor="e", font=T.font(8, "bold")
            )
            self.canvas.create_oval(lx - 26, y0 - 15, lx - 20, y0 - 9,
                                    fill=color, outline=color)
            lx -= 38