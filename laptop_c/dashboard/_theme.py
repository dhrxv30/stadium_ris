"""
Design tokens and small custom widgets for the Stadium RIS dashboard.

Look: clean light surfaces, soft outlines, rounded cards, pill status chips
and an underlined tab bar (Google / Material-style).  Tk has no rounded
corners, so cards, chips and bars are drawn on tk.Canvas.

Standard library only.  Call init_fonts(root) once after tk.Tk() exists.
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Callable, List, Optional

# ── surfaces & text ──────────────────────────────────────────────────────────
BG         = "#f8f9fa"   # window background
SURFACE    = "#ffffff"   # cards, app bar
SURFACE2   = "#f1f3f4"   # inset areas, bar tracks
OUTLINE    = "#dadce0"   # card borders, dividers
TEXT       = "#202124"
TEXT_DIM   = "#5f6368"
TEXT_FAINT = "#80868b"

BLUE, GREEN, YELLOW = "#1a73e8", "#1e8e3e", "#f9ab00"
RED, ORANGE, GRAY, PURPLE = "#d93025", "#e8710a", "#9aa0a6", "#9334e6"

# tone -> (solid colour, tinted background, readable text colour on white)
TONES = {
    "green":  (GREEN,  "#e6f4ea", "#137333"),
    "yellow": (YELLOW, "#fef7e0", "#b06000"),
    "red":    (RED,    "#fce8e6", "#c5221f"),
    "orange": (ORANGE, "#fdeee0", "#b45309"),
    "blue":   (BLUE,   "#e8f0fe", "#1967d2"),
    "purple": (PURPLE, "#f3e8fd", "#7627bb"),
    "gray":   (GRAY,   "#f1f3f4", "#5f6368"),
}

ZONE_CLR = {"Z1": "#1a73e8", "Z2": "#1e8e3e", "Z3": "#f9ab00",
            "Z4": "#129eaf", "Z5": "#9334e6"}

# ── fonts ────────────────────────────────────────────────────────────────────
_FAMILY = "Helvetica"
_MONO = "Courier"


def init_fonts(root: tk.Misc) -> None:
    """Pick the best installed UI + monospace families (needs a Tk root)."""
    global _FAMILY, _MONO
    try:
        have = set(tkfont.families(root))
    except Exception:
        return
    for f in ("Google Sans", "Roboto", "Segoe UI", "SF Pro Text", "Helvetica Neue",
              "Inter", "Ubuntu", "Cantarell", "DejaVu Sans", "Arial"):
        if f in have:
            _FAMILY = f
            break
    for f in ("Roboto Mono", "Cascadia Mono", "Consolas", "SF Mono", "Menlo",
              "DejaVu Sans Mono", "Liberation Mono"):
        if f in have:
            _MONO = f
            break


def font(size: int = 10, weight: str = "normal") -> tuple:
    return (_FAMILY, size, weight)


def mono(size: int = 9) -> tuple:
    return (_MONO, size)


def blend(hex_colour: str, amount: float, base: str = "#ffffff") -> str:
    """Mix *hex_colour* into *base* (amount 0..1) – a fake-alpha tint."""
    def rgb(h: str):
        h = h.lstrip("#")
        return [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    c, b = rgb(hex_colour), rgb(base)
    return "#" + "".join(f"{round(b[i] + (c[i] - b[i]) * amount):02x}" for i in range(3))


def _round_pts(x0: float, y0: float, x1: float, y1: float, r: float) -> List[float]:
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
            x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]


# ── Card: rounded, outlined surface ──────────────────────────────────────────

class Card(tk.Canvas):
    """Rounded card.  Put content in ``card.body`` (a normal tk.Frame).

    expand=True lets the body follow the card's height (for content that
    should fill the card, e.g. a log).
    """

    def __init__(self, parent: tk.Misc, bg: str = BG, fill: str = SURFACE,
                 outline: str = OUTLINE, radius: int = 14,
                 pad: tuple = (16, 14), expand: bool = False) -> None:
        # Start tall: Tk never maps (or sends <Configure> for) a canvas window that
        # lies fully outside the canvas, so a tiny initial height would deadlock.
        super().__init__(parent, bg=bg, width=1, height=400,
                         highlightthickness=0, bd=0)
        self._fill, self._outline, self._r = fill, outline, radius
        self._px, self._py = pad
        self._expand = expand
        self.body = tk.Frame(self, bg=fill)
        self._win = self.create_window(self._px, self._py, window=self.body, anchor="nw")
        self.bind("<Configure>", self._on_canvas)
        self.body.bind("<Configure>", self._on_body)

    def _sync_height(self) -> None:
        h = self.body.winfo_reqheight() + 2 * self._py
        if int(float(self.cget("height"))) != h:
            self.configure(height=h)

    def _on_body(self, _e: tk.Event) -> None:
        self._sync_height()

    def _on_canvas(self, e: tk.Event) -> None:
        self._sync_height()
        opts = {"width": max(e.width - 2 * self._px, 1)}
        if self._expand:
            opts["height"] = max(e.height - 2 * self._py, 1)
        self.itemconfigure(self._win, **opts)
        self._draw(e.width, e.height)

    def _draw(self, w: int, h: int) -> None:
        self.delete("bg")
        self.create_polygon(_round_pts(1, 1, w - 1, h - 1, self._r), smooth=True,
                            fill=self._fill, outline=self._outline, tags="bg")
        self.tag_lower("bg")

    def set_fill(self, colour: str) -> None:
        """Change the card colour (and every child that matched the old one)."""
        if colour == self._fill:
            return
        old, self._fill = self._fill, colour
        self._recolour(self.body, old, colour)
        self._draw(self.winfo_width(), self.winfo_height())

    @classmethod
    def _recolour(cls, w: tk.Misc, old: str, new: str) -> None:
        try:
            if str(w.cget("bg")).lower() == old.lower():
                w.config(bg=new)
        except tk.TclError:
            pass
        for c in w.winfo_children():
            cls._recolour(c, old, new)


# ── Chip: pill-shaped status label (also used as a button) ───────────────────

class Chip(tk.Canvas):
    def __init__(self, parent: tk.Misc, text: str = "", tone: str = "gray",
                 bg: str = SURFACE, size: int = 9, weight: str = "bold",
                 dot: bool = False, padx: int = 10, pady: int = 4) -> None:
        super().__init__(parent, bg=bg, width=1, height=1, highlightthickness=0, bd=0)
        self._font = tkfont.Font(root=parent, family=_FAMILY, size=size, weight=weight)
        self._dot, self._padx, self._pady = dot, padx, pady
        self._state: Optional[tuple] = None
        self._tone = tone
        self.set(text, tone)

    def set(self, text: str, tone: Optional[str] = None) -> None:
        tone = tone or self._tone
        if self._state == (text, tone):
            return
        self._state, self._tone = (text, tone), tone
        self.delete("all")
        if not text:
            self.configure(width=1, height=1)
            return
        solid, tint, fg = TONES[tone]
        th = self._font.metrics("linespace")
        w = self._font.measure(text) + 2 * self._padx + (12 if self._dot else 0)
        h = th + 2 * self._pady
        self.configure(width=w, height=h)
        self.create_polygon(_round_pts(0, 0, w, h, h / 2), smooth=True, fill=tint, outline=tint)
        x = self._padx
        if self._dot:
            self.create_oval(x, h / 2 - 3, x + 6, h / 2 + 3, fill=solid, outline=solid)
            x += 12
        self.create_text(x, h / 2, text=text, anchor="w", fill=fg, font=self._font)


class PillButton(Chip):
    """Clickable chip."""

    def __init__(self, parent: tk.Misc, text: str, command: Callable[[], None],
                 tone: str = "blue", bg: str = SURFACE) -> None:
        super().__init__(parent, text, tone, bg=bg, padx=14, pady=5)
        self.configure(cursor="hand2")
        self.bind("<Button-1>", lambda _e: command())


# ── ProgressBar: thin rounded bar ────────────────────────────────────────────

class ProgressBar(tk.Canvas):
    def __init__(self, parent: tk.Misc, bg: str = SURFACE, height: int = 6,
                 track: str = SURFACE2) -> None:
        super().__init__(parent, bg=bg, width=1, height=height,
                         highlightthickness=0, bd=0)
        self._h, self._track = height, track
        self._frac: Optional[float] = None
        self._colour = GRAY
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, frac: Optional[float], colour: str) -> None:
        self._frac, self._colour = frac, colour
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self._h
        r = h / 2
        if w <= h:
            return
        self.create_line(r, r, w - r, r, width=h, capstyle="round", fill=self._track)
        if self._frac:
            x = w * min(max(self._frac, 0.0), 1.0)
            self.create_line(r, r, max(r + 0.01, x - r), r, width=h,
                             capstyle="round", fill=self._colour)


# ── TabBar: underlined tabs ──────────────────────────────────────────────────

class TabBar(tk.Frame):
    def __init__(self, parent: tk.Misc, labels: List[str],
                 on_select: Callable[[int], None], bg: str = SURFACE) -> None:
        super().__init__(parent, bg=bg)
        self._bg, self._on = bg, on_select
        self._cells: List[tuple] = []
        self._sel = -1
        for i, text in enumerate(labels):
            cell = tk.Frame(self, bg=bg, cursor="hand2")
            lbl = tk.Label(cell, text=text, bg=bg, fg=TEXT_DIM, font=font(10, "bold"),
                           padx=18, pady=11, cursor="hand2")
            bar = tk.Frame(cell, bg=bg, height=3)
            lbl.pack()
            bar.pack(fill="x", padx=10)
            cell.pack(side="left")
            for w in (cell, lbl):
                w.bind("<Button-1>", lambda _e, i=i: self.select(i))
            lbl.bind("<Enter>", lambda _e, i=i: self._hover(i, True))
            lbl.bind("<Leave>", lambda _e, i=i: self._hover(i, False))
            self._cells.append((lbl, bar))
        self.select(0)

    def _hover(self, i: int, on: bool) -> None:
        if i != self._sel:
            self._cells[i][0].config(fg=TEXT if on else TEXT_DIM)

    def select(self, i: int) -> None:
        self._sel = i
        for j, (lbl, bar) in enumerate(self._cells):
            on = j == i
            lbl.config(fg=BLUE if on else TEXT_DIM)
            bar.config(bg=BLUE if on else self._bg)
        self._on(i)