"""
Stage D2–D5: Native Tkinter dashboard for the Stadium RIS Supervisor.

Architecture
------------
DashboardApp    – builds and owns all Tk widgets.  Must be created and
                  driven entirely within the dashboard thread.
DashboardThread – non-daemon thread that creates DashboardApp and runs
                  the Tk main-loop.  Exposes push_view() and stop().

Thread-safety contract
----------------------
* The asyncio main-loop calls push_view(view) which puts the view into a
  thread-safe queue.Queue.  No Tk objects are ever touched from asyncio.
* DashboardApp._refresh() is scheduled via root.after(500 ms) and drains
  the queue; all widget updates happen here, always on the Tk thread.
* If Tkinter is unavailable (ImportError, TclError, missing DISPLAY …),
  or the window is closed, or any exception occurs, DashboardThread catches
  it, prints "dashboard unavailable", and exits cleanly.  The supervisor
  keeps running in terminal mode.

Hard rules satisfied
--------------------
1. Off by default – only DashboardThread.start() activates it.
2. Tkinter only – no web server, no browser.
3. Read-only – supervisor internals never touched from Tk thread.
4. Graceful fallback on any Tk failure.
5. Refresh at 2 Hz from a deep-copied get_view() snapshot.
6. None → "N/A" via _na(); STALE data is greyed + labeled.
7. SINR and PER rows carry "¹ simulated model output"; mock=True → MOCK banner.

Visual design lives in _theme.py (tokens + rounded cards, pills, tabs, bars).
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, List, Optional

from common.constants import ZONE_IDS
from laptop_c.dashboard import _theme as T
from laptop_c.dashboard._theme import (
    BG, SURFACE, SURFACE2, OUTLINE, TEXT, TEXT_DIM, TEXT_FAINT, BLUE, ZONE_CLR,
    Card, Chip, PillButton, ProgressBar, TabBar, font, mono,
)

REFRESH_MS    = 500    # 2 Hz
MAX_EVT_ROWS  = 200    # bounded event log rows
MAX_OOB_HIST  = 10     # last N OOB commands kept
SHOW_OOB_HIST = 5      # …of which this many are visible

GAP = 12               # gap between cards
PAGE_PAD = 20          # page margin


# ── formatting helpers (never fabricate – None always → "N/A") ─────────────────

def _na(v: Any, fmt: str = "", unit: str = "") -> str:
    """Format a value or return 'N/A' when None/empty."""
    if v is None or v == "":
        return "N/A"
    if isinstance(v, float):
        return (f"{v:{fmt}}" if fmt else f"{v:.2f}") + unit
    if isinstance(v, int):
        return (f"{v:{fmt}}" if fmt else str(v)) + unit
    return str(v) + unit


# state → tone name (see _theme.TONES)

def _health_tone(state: str) -> str:
    s = (state or "").upper()
    return ("green"  if s == "CONNECTED"
            else "orange" if s == "STALE"
            else "gray")


def _congestion_tone(state: str) -> str:
    s = (state or "").upper()
    return "red" if s == "CONGESTED" else "yellow" if s == "ELEVATED" else "green"


def _panel_health_tone(h: str) -> str:
    h = (h or "").upper()
    return "green" if h == "OK" else "yellow" if h == "DEGRADED" else "red"


def _panel_status_tone(s: str) -> str:
    s = (s or "").upper()
    return ("green" if s == "APPLIED" else "yellow" if s == "PENDING"
            else "red" if s == "FAILED" else "gray")


def _fg(tone: str) -> str:
    """Text colour of a tone (readable on white)."""
    return T.TONES[tone][2]


def _solid(tone: str) -> str:
    """Saturated colour of a tone (dots, bars)."""
    return T.TONES[tone][0]


def _state_label(state: str) -> str:
    return (state or "UNKNOWN").replace("_", " ").capitalize()


# ── small layout helpers ──────────────────────────────────────────────────────

def _card_title(body: tk.Frame, title: str, note: str = "", bg: str = SURFACE) -> tk.Frame:
    row = tk.Frame(body, bg=bg)
    row.pack(fill="x", pady=(0, 10))
    tk.Label(row, text=title, bg=bg, fg=TEXT, font=font(12, "bold")).pack(side="left")
    if note:
        tk.Label(row, text=note, bg=bg, fg=TEXT_FAINT, font=font(9)).pack(side="left", padx=(8, 0))
    return row


def _divider(parent: tk.Misc, bg: str = OUTLINE) -> tk.Frame:
    return tk.Frame(parent, bg=bg, height=1)


# ── Main Dashboard Application ─────────────────────────────────────────────────

class DashboardApp:
    """All Tk widgets.  Must be created and driven from the dashboard thread only."""

    def __init__(self) -> None:
        self._root = tk.Tk()
        T.init_fonts(self._root)
        self._root.title("Stadium RIS Supervisor")
        self._root.configure(bg=BG)
        self._root.geometry("1200x840")
        self._root.minsize(1080, 720)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._alive = True
        self._stop_event = threading.Event()
        self._queue: "queue.Queue[Dict]" = queue.Queue(maxsize=6)
        self._last_view: Optional[Dict] = None

        # Filter StringVars (created on the Tk thread so they're safe)
        self._flt_zone  = tk.StringVar()
        self._flt_panel = tk.StringVar()
        self._flt_cmd   = tk.StringVar()
        self._flt_type  = tk.StringVar()
        self._log_paused = tk.BooleanVar(value=False)

        # OOB command history for "last commands" list
        self._oob_hist: List[str] = []
        self._mock_shown = False

        self._build_ui()
        self._root.after(REFRESH_MS, self._refresh)
        self._root.after(50, self._check_stop)

    # ── public thread-safe API ─────────────────────────────────────────────────

    def push_view(self, view: Dict) -> None:
        """Deliver a snapshot to the dashboard (called from asyncio thread)."""
        try:
            self._queue.put_nowait(view)
        except queue.Full:
            try:
                self._queue.get_nowait()      # drop oldest
                self._queue.put_nowait(view)
            except queue.Empty:
                pass

    def request_stop(self) -> None:
        """Thread-safe: ask the window to close."""
        self._alive = False
        self._stop_event.set()

    def is_alive(self) -> bool:
        return self._alive and not self._stop_event.is_set()

    # ── Tk internals ──────────────────────────────────────────────────────────

    def _check_stop(self) -> None:
        if not self._alive or self._stop_event.is_set():
            self._on_close()
            return
        try:
            self._root.after(50, self._check_stop)
        except Exception:
            pass

    def _on_close(self) -> None:
        self._alive = False
        self._stop_event.set()
        try:
            self._root.quit()
        except Exception:
            pass

    def cleanup(self) -> None:
        """Clean up Tk variables and destroy root window on the Tk thread."""
        self._alive = False
        self._stop_event.set()
        try:
            self._flt_zone = None
            self._flt_panel = None
            self._flt_cmd = None
            self._flt_type = None
            self._log_paused = None
        except Exception:
            pass
        try:
            self._root.destroy()
        except Exception:
            pass

    def _refresh(self) -> None:
        """Drain queue, update all panels, re-schedule at 2 Hz."""
        if not self._alive or self._stop_event.is_set():
            self._on_close()
            return
        view = None
        while True:
            try:
                view = self._queue.get_nowait()
            except queue.Empty:
                break
        if view is not None:
            self._last_view = view

        if self._last_view is not None:
            snap    = self._last_view["snapshot"]
            events  = self._last_view["recent_events"]
            history = self._last_view["history"]
            try:
                self._update_header(snap)
                self._update_traffic(snap)
                self._update_oob(snap)
                self._update_zones(snap)
                self._update_ris_grid(snap)
                self._update_events(events)
                self._update_charts(history)
            except Exception:
                pass   # never crash the supervisor

        try:
            self._root.after(REFRESH_MS, self._refresh)
        except Exception:
            self._alive = False

    def mainloop(self) -> None:
        """Enter Tk mainloop.  Blocks until the window is closed."""
        self._root.mainloop()

    # ── UI builder ────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """App bar, tab bar, and four pages."""
        self._style_ttk()
        self._build_header(self._root)
        _divider(self._root).pack(fill="x")

        self._pages: List[tk.Frame] = []
        # TabBar fires on_select once during __init__ (before pages exist);
        # _show_page tolerates that and we call it again after building.
        self._tabbar = TabBar(self._root, ["Overview", "RIS panels", "Trends", "Events"],
                              on_select=self._show_page)
        self._tabbar.pack(fill="x")
        _divider(self._root).pack(fill="x")

        host = tk.Frame(self._root, bg=BG)
        host.pack(fill="both", expand=True)
        for build in (self._build_overview, self._build_ris_tab,
                      self._build_trends_tab, self._build_events_tab):
            page = tk.Frame(host, bg=BG)
            self._pages.append(page)
            build(page)
        self._show_page(0)

    def _show_page(self, i: int) -> None:
        pages = getattr(self, "_pages", [])
        for j, p in enumerate(pages):
            if j == i:
                p.pack(fill="both", expand=True, padx=PAGE_PAD, pady=PAGE_PAD - 4)
            else:
                p.pack_forget()

    def _style_ttk(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        # slim scrollbar without arrows
        style.layout("Slim.Vertical.TScrollbar", [
            ("Vertical.Scrollbar.trough", {
                "sticky": "ns",
                "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})],
            })])
        style.configure("Slim.Vertical.TScrollbar", troughcolor=SURFACE,
                        background=OUTLINE, bordercolor=SURFACE,
                        lightcolor=OUTLINE, darkcolor=OUTLINE, gripcount=0, width=8)
        style.map("Slim.Vertical.TScrollbar", background=[("active", "#bdc1c6")])

    # ── App bar ───────────────────────────────────────────────────────────────

    def _build_header(self, parent: tk.Misc) -> None:
        bar = tk.Frame(parent, bg=SURFACE)
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=SURFACE)
        inner.pack(fill="x", padx=PAGE_PAD, pady=14)

        # brand mark
        logo = tk.Canvas(inner, width=36, height=36, bg=SURFACE, highlightthickness=0)
        logo.create_oval(1, 1, 35, 35, fill=BLUE, outline=BLUE)
        logo.create_text(18, 18, text="R", fill="white", font=font(14, "bold"))
        logo.pack(side="left", padx=(0, 12))

        titles = tk.Frame(inner, bg=SURFACE)
        titles.pack(side="left")
        trow = tk.Frame(titles, bg=SURFACE)
        trow.pack(anchor="w")
        tk.Label(trow, text="Stadium RIS Supervisor", bg=SURFACE, fg=TEXT,
                 font=font(15, "bold")).pack(side="left")
        self._chip_mock = Chip(trow, "Mock data", "orange", bg=SURFACE)   # packed on demand
        self._trow = trow

        meta = tk.Frame(titles, bg=SURFACE)
        meta.pack(anchor="w", pady=(2, 0))
        self._lbl_run_id  = tk.Label(meta, text="Run N/A",     bg=SURFACE, fg=TEXT_DIM, font=font(9))
        self._lbl_seed    = tk.Label(meta, text="Seed N/A",    bg=SURFACE, fg=TEXT_DIM, font=font(9))
        self._lbl_elapsed = tk.Label(meta, text="Elapsed 0s",  bg=SURFACE, fg=TEXT_DIM, font=font(9))
        self._lbl_run_id.pack(side="left")
        self._lbl_seed.pack(side="left", padx=(14, 0))
        self._lbl_elapsed.pack(side="left", padx=(14, 0))

        # peer status chips (A B C, left → right)
        self._badges: Dict[str, Chip] = {}
        for peer in ("C", "B", "A"):
            c = Chip(inner, f"Peer {peer}  Unknown", "gray", bg=SURFACE, dot=True)
            c.pack(side="right", padx=(6, 0))
            self._badges[peer] = c

    def _update_header(self, snap: Dict) -> None:
        is_mock = bool(snap.get("is_mock", False))
        if is_mock and not self._mock_shown:
            self._chip_mock.pack(side="left", padx=(12, 0))
            self._mock_shown = True
        elif not is_mock and self._mock_shown:
            self._chip_mock.pack_forget()
            self._mock_shown = False

        self._lbl_run_id.config(text=f"Run {snap.get('run_id') or 'N/A'}")
        self._lbl_seed.config(  text=f"Seed {snap.get('random_seed') or 'N/A'}")
        self._lbl_elapsed.config(text=f"Elapsed {_na(snap.get('elapsed_s'), '.0f', 's')}")

        ph = snap.get("peer_health", {})
        for peer, chip in self._badges.items():
            state = ph.get(peer, "UNKNOWN")
            chip.set(f"Peer {peer}  {_state_label(state)}", _health_tone(state))

    # ── Tab 1: Overview ───────────────────────────────────────────────────────

    def _build_overview(self, parent: tk.Frame) -> None:
        self._build_stat_tiles(parent)

        row2 = tk.Frame(parent, bg=BG)
        row2.pack(fill="x", pady=(GAP, 0))
        row2.columnconfigure(0, weight=3, uniform="r2")
        row2.columnconfigure(1, weight=2, uniform="r2")
        self._build_traffic(row2)
        self._build_oob(row2)

        self._build_zones(parent)

    # Headline numbers ───────────────────────────────────────────────────────

    def _build_stat_tiles(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x")
        specs = [("pdr", "Delivery rate"), ("plr", "Packet loss"),
                 ("thr", "Throughput"),    ("lat", "Mean latency")]
        subs: Dict[str, tk.Label] = {}
        vals: Dict[str, tk.Label] = {}
        for i, (key, title) in enumerate(specs):
            row.columnconfigure(i, weight=1, uniform="stat")
            card = Card(row, bg=BG)
            card.grid(row=0, column=i, sticky="nsew",
                      padx=(0 if i == 0 else GAP // 2, 0 if i == 3 else GAP // 2))
            tk.Label(card.body, text=title, bg=SURFACE, fg=TEXT_DIM,
                     font=font(10)).pack(anchor="w")
            v = tk.Label(card.body, text="N/A", bg=SURFACE, fg=TEXT, font=font(24, "bold"))
            v.pack(anchor="w", pady=(2, 0))
            s = tk.Label(card.body, text=" ", bg=SURFACE, fg=TEXT_FAINT, font=font(9))
            s.pack(anchor="w")
            vals[key], subs[key] = v, s
        self._lbl_pdr, self._lbl_plr = vals["pdr"], vals["plr"]
        self._lbl_thr, self._lbl_lat = vals["thr"], vals["lat"]
        self._lbl_lat_sub = subs["lat"]
        self._lbl_pdr_sub, self._lbl_plr_sub, self._lbl_thr_sub = subs["pdr"], subs["plr"], subs["thr"]

    # Traffic ────────────────────────────────────────────────────────────────

    def _build_traffic(self, parent: tk.Frame) -> None:
        card = Card(parent, bg=BG)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, GAP // 2))
        f = card.body
        _card_title(f, "Traffic", "PDU accounting")

        grid = tk.Frame(f, bg=SURFACE)
        grid.pack(fill="x")
        hdrs = ["Generated", "Delivered", "Dropped", "Pending", "Timed out"]
        grid.columnconfigure(0, minsize=84)
        for ci, h in enumerate(hdrs):
            grid.columnconfigure(ci + 1, weight=1)
            tk.Label(grid, text=h, bg=SURFACE, fg=TEXT_DIM, font=font(9),
                     anchor="e").grid(row=0, column=ci + 1, sticky="e", padx=(6, 0), pady=(0, 6))
        _divider(grid).grid(row=1, column=0, columnspan=6, sticky="ew")

        def _row(label: str, r: int) -> List[tk.Label]:
            tk.Label(grid, text=label, bg=SURFACE, fg=TEXT, font=font(10, "bold"),
                     anchor="w").grid(row=r, column=0, sticky="w", pady=7)
            cells = []
            for ci in range(5):
                v = tk.Label(grid, text="0", bg=SURFACE, fg=TEXT, font=font(11), anchor="e")
                v.grid(row=r, column=ci + 1, sticky="e", padx=(6, 0))
                cells.append(v)
            return cells

        self._ul_cells = _row("Uplink", 2)
        _divider(grid).grid(row=3, column=0, columnspan=6, sticky="ew")
        self._dl_cells = _row("Downlink", 4)

        self._lbl_ack = tk.Label(f, text="ACK –   NACK –   Retries –", bg=SURFACE,
                                 fg=TEXT_DIM, font=font(9))
        self._lbl_ack.pack(anchor="w", pady=(10, 0))

    def _update_traffic(self, snap: Dict) -> None:
        pdu = snap.get("pdu_summary", {})
        ul  = pdu.get("uplink",   {})
        dl  = pdu.get("downlink", {})
        om  = snap.get("overall_metrics", {})

        for ci, k in enumerate(["generated", "delivered", "dropped", "pending", "timed_out"]):
            self._ul_cells[ci].config(text=str(ul.get(k, 0)))
            self._dl_cells[ci].config(text=str(dl.get(k, 0)))

        self._lbl_pdr.config(text=_na(om.get("pdr_pct"), ".1f", "%"))
        self._lbl_plr.config(text=_na(om.get("plr_pct"), ".1f", "%"))
        self._lbl_ack.config(
            text=f"ACK {pdu.get('ack_count', 0)}     NACK {pdu.get('nack_count', 0)}"
                 f"     Retries {pdu.get('retry_count', 0)}"
        )
        self._lbl_lat.config(text=_na(om.get("mean_latency_ms"), ".1f", " ms"))
        self._lbl_lat_sub.config(text=f"p95 {_na(om.get('p95_latency_ms'), '.1f', ' ms')}")
        thr = om.get("throughput_bps")
        self._lbl_thr.config(text=f"{thr / 1e6:.2f} Mbps" if thr is not None else "N/A")

    # OOB ─────────────────────────────────────────────────────────────────────

    def _build_oob(self, parent: tk.Frame) -> None:
        card = Card(parent, bg=BG)
        card.grid(row=0, column=1, sticky="nsew", padx=(GAP // 2, 0))
        f = card.body
        _card_title(f, "Control link", "Out-of-band")

        mf = tk.Frame(f, bg=SURFACE)
        mf.pack(fill="x")
        mf.columnconfigure(1, weight=1, uniform="oob")
        mf.columnconfigure(3, weight=1, uniform="oob")

        def _m(lbl: str, r: int, c: int) -> tk.Label:
            tk.Label(mf, text=lbl, bg=SURFACE, fg=TEXT_DIM, font=font(9),
                     anchor="w").grid(row=r, column=c * 2, sticky="w",
                                      padx=(0 if c == 0 else 16, 8), pady=3)
            v = tk.Label(mf, text="0", bg=SURFACE, fg=TEXT, font=font(10, "bold"), anchor="w")
            v.grid(row=r, column=c * 2 + 1, sticky="w")
            return v

        self._oob_cmds  = _m("Commands",  0, 0)
        self._oob_acked = _m("Acknowledged", 0, 1)
        self._oob_retry = _m("Retried",   1, 0)
        self._oob_tmo   = _m("Timed out", 1, 1)
        self._oob_ratio = _m("ACK ratio", 2, 0)
        self._oob_last  = _m("Last",      2, 1)

        tk.Label(f, text="Recent commands", bg=SURFACE, fg=TEXT_DIM,
                 font=font(9)).pack(anchor="w", pady=(10, 4))
        self._oob_hist_txt = tk.Text(
            f, height=SHOW_OOB_HIST, bg=SURFACE2, fg=TEXT, font=mono(9),
            wrap="none", state="disabled", relief="flat", borderwidth=0,
            highlightthickness=0, padx=10, pady=6, cursor="arrow",
        )
        self._oob_hist_txt.pack(fill="x")

        self._oob_stale = Chip(f, "Last known values – peer B not connected", "orange",
                               bg=SURFACE, size=8)  # packed on demand

    def _update_oob(self, snap: Dict) -> None:
        oob = snap.get("oob", {})
        ph  = snap.get("peer_health", {})
        stale = ph.get("B", "") != "CONNECTED"

        cmds    = oob.get("unique_commands", 0)
        acked   = oob.get("acknowledged", 0)
        retried = oob.get("retries", 0)
        tmo     = oob.get("timeouts", 0)
        ratio   = oob.get("ack_ratio_pct")
        last_id = oob.get("last_command_id")
        last_st = oob.get("last_command_status")

        dim = TEXT_FAINT if stale else TEXT
        self._oob_cmds.config( text=str(cmds),    fg=dim)
        self._oob_acked.config(text=str(acked),   fg=dim)
        self._oob_retry.config(text=str(retried), fg=dim)
        self._oob_tmo.config(  text=str(tmo),
                               fg=(_fg("red") if tmo > 0 and not stale else dim))

        if ratio is not None:
            ratio_str = f"{ratio:.1f}%  ({acked}/{cmds})"
            rc = _fg("green" if ratio >= 90 else "yellow" if ratio >= 70 else "red")
        else:
            ratio_str, rc = "N/A", TEXT_DIM
        self._oob_ratio.config(text=ratio_str, fg=TEXT_FAINT if stale else rc)
        self._oob_last.config(text=f"{last_id or 'N/A'} ({last_st or 'N/A'})"[:22], fg=dim)

        # Update command history (only on new command IDs)
        if last_id and (not self._oob_hist or not self._oob_hist[0].startswith(last_id)):
            self._oob_hist.insert(0, f"{last_id:<12} {last_st or 'N/A'}")
            self._oob_hist = self._oob_hist[:MAX_OOB_HIST]

        t = self._oob_hist_txt
        t.config(state="normal", fg=TEXT_FAINT if stale else TEXT)
        t.delete("1.0", "end")
        for line in self._oob_hist[:SHOW_OOB_HIST]:
            t.insert("end", line + "\n")
        t.config(state="disabled")

        if stale:
            if not self._oob_stale.winfo_ismapped():
                self._oob_stale.pack(anchor="w", pady=(8, 0))
        else:
            self._oob_stale.pack_forget()

    # Zone cards ──────────────────────────────────────────────────────────────

    def _build_zones(self, parent: tk.Frame) -> None:
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", pady=(GAP + 6, 8))
        tk.Label(head, text="Zones", bg=BG, fg=TEXT, font=font(13, "bold")).pack(side="left")
        tk.Label(head, text="¹ simulated model output", bg=BG, fg=TEXT_FAINT,
                 font=font(9)).pack(side="left", padx=(10, 0))

        f = tk.Frame(parent, bg=BG)
        f.pack(fill="x")
        for i in range(len(ZONE_IDS)):
            f.columnconfigure(i, weight=1, uniform="zone")

        self._zone_cards: Dict[str, Dict] = {}
        for i, zid in enumerate(ZONE_IDS):
            self._zone_cards[zid] = self._build_zone_card(f, zid, i, len(ZONE_IDS))

    def _build_zone_card(self, parent: tk.Widget, zid: str, col: int, n: int) -> Dict:
        color = ZONE_CLR.get(zid, TEXT_DIM)
        card = Card(parent, bg=BG, pad=(14, 12))
        card.grid(row=0, column=col, sticky="nsew",
                  padx=(0 if col == 0 else 5, 0 if col == n - 1 else 5))
        cf = card.body

        top = tk.Frame(cf, bg=SURFACE)
        top.pack(fill="x")
        tk.Label(top, text="●", bg=SURFACE, fg=color, font=font(11)).pack(side="left")
        tk.Label(top, text=zid, bg=SURFACE, fg=TEXT, font=font(12, "bold")).pack(side="left", padx=(4, 0))
        cong = Chip(top, "Normal", "green", bg=SURFACE, size=8, padx=8, pady=2)
        cong.pack(side="right")

        w_pdr = tk.Label(cf, text="N/A", bg=SURFACE, fg=TEXT, font=font(22, "bold"))
        w_pdr.pack(anchor="w", pady=(10, 0))
        tk.Label(cf, text="Delivery rate", bg=SURFACE, fg=TEXT_DIM,
                 font=font(9)).pack(anchor="w")

        qrow = tk.Frame(cf, bg=SURFACE)
        qrow.pack(fill="x", pady=(10, 4))
        tk.Label(qrow, text="Queue", bg=SURFACE, fg=TEXT_DIM, font=font(9)).pack(side="left")
        w_queue = tk.Label(qrow, text="N/A", bg=SURFACE, fg=TEXT, font=font(9, "bold"))
        w_queue.pack(side="right")
        qbar = ProgressBar(cf, bg=SURFACE, height=6)
        qbar.pack(fill="x")

        kv = tk.Frame(cf, bg=SURFACE)
        kv.pack(fill="x", pady=(10, 0))
        kv.columnconfigure(1, weight=1)

        def _kv(label: str, r: int) -> tk.Label:
            tk.Label(kv, text=label, bg=SURFACE, fg=TEXT_DIM,
                     font=font(9), anchor="w").grid(row=r, column=0, sticky="w", pady=2)
            v = tk.Label(kv, text="N/A", bg=SURFACE, fg=TEXT, font=font(10), anchor="e")
            v.grid(row=r, column=1, sticky="e")
            return v

        w_gen  = _kv("Generated", 0)
        w_del  = _kv("Delivered", 1)
        w_drop = _kv("Dropped",   2)
        w_sinr = _kv("SINR ¹",    3)
        w_per  = _kv("PER ¹",     4)

        stale = Chip(cf, "Last known", "orange", bg=SURFACE, size=8, padx=8, pady=2)
        # packed on demand

        return dict(cong=cong, gen=w_gen, del_=w_del, drop=w_drop, pdr=w_pdr,
                    qbar=qbar, queue=w_queue, sinr=w_sinr, per=w_per, stale=stale)

    def _update_zones(self, snap: Dict) -> None:
        zones = snap.get("zones", {})
        ph    = snap.get("peer_health", {})
        stale = ph.get("A", "") != "CONNECTED"

        for zid, wmap in self._zone_cards.items():
            z = zones.get(zid, {})

            cong = z.get("congestion_state", "NORMAL")
            wmap["cong"].set(_state_label(cong), _congestion_tone(cong))

            dim = TEXT_FAINT if stale else TEXT
            wmap["gen"].config( text=str(z.get("total_generated", 0)), fg=dim)
            wmap["del_"].config(text=str(z.get("total_delivered",  0)), fg=dim)
            drop = z.get("total_dropped", 0)
            wmap["drop"].config(text=str(drop),
                                fg=TEXT_FAINT if stale else (_fg("red") if drop > 0 else TEXT))

            pdr = z.get("pdr_pct")
            if pdr is not None and not stale:
                pc = _fg("green" if pdr >= 95 else "yellow" if pdr >= 85 else "red")
            else:
                pc = TEXT_FAINT
            wmap["pdr"].config(text=_na(pdr, ".1f", "%"), fg=pc)

            q_occ = z.get("queue_occupancy")
            q_cap = z.get("queue_capacity")
            q_pct = z.get("queue_occupancy_pct")
            if q_pct is not None:
                tone = "gray" if stale else ("red" if q_pct > 80 else "yellow" if q_pct > 50 else "green")
                wmap["qbar"].set(min(q_pct / 100.0, 1.0), _solid(tone))
            else:
                wmap["qbar"].set(None, T.GRAY)
            wmap["queue"].config(text=f"{_na(q_occ)}/{_na(q_cap)}", fg=dim)
            wmap["sinr"].config(text=_na(z.get("sinr_db"), ".1f", " dB"), fg=dim)
            wmap["per"].config( text=_na(z.get("estimated_per"), ".3f"),  fg=dim)

            if stale:
                if not wmap["stale"].winfo_ismapped():
                    wmap["stale"].pack(anchor="w", pady=(10, 0))
            else:
                wmap["stale"].pack_forget()

    # ── Tab 2: RIS panels ─────────────────────────────────────────────────────

    def _build_ris_tab(self, parent: tk.Frame) -> None:
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", pady=(0, 10))
        tk.Label(head, text="RIS panels", bg=BG, fg=TEXT, font=font(13, "bold")).pack(side="left")
        tk.Label(head, text="32 passive tiles in an 8 × 4 grid", bg=BG, fg=TEXT_FAINT,
                 font=font(9)).pack(side="left", padx=(10, 0))

        # legend, right-aligned in the same row
        for text, tone in [("Failed", "red"), ("Pending", "yellow"),
                           ("Applied", "green"), ("Idle", "gray")]:
            Chip(head, text, tone, bg=BG, dot=True, size=8, padx=8, pady=2).pack(
                side="right", padx=(6, 0))

        grid = tk.Frame(parent, bg=BG)
        grid.pack(fill="both", expand=True)
        for i in range(8):
            grid.columnconfigure(i, weight=1, uniform="tile")

        self._panel_tiles: Dict[str, Dict] = {}
        for pidx in range(1, 33):
            pid = f"P{pidx}"
            r, c = (pidx - 1) // 8, (pidx - 1) % 8
            self._panel_tiles[pid] = self._build_panel_tile(grid, pid, r, c)

        tk.Label(parent, text="Tile fill turns red when a panel is offline.  "
                              "Health text shows OK, DEGRADED or OFFLINE.",
                 bg=BG, fg=TEXT_FAINT, font=font(9)).pack(anchor="w", pady=(8, 0))

    def _build_panel_tile(self, parent: tk.Widget, pid: str, row: int, col: int) -> Dict:
        card = Card(parent, bg=BG, radius=12, pad=(10, 8))
        card.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
        tf = card.body

        top = tk.Frame(tf, bg=SURFACE)
        top.pack(fill="x")
        tk.Label(top, text=pid, bg=SURFACE, fg=TEXT, font=font(10, "bold")).pack(side="left")
        stat_dot = tk.Label(top, text="●", bg=SURFACE, fg=T.GRAY, font=font(10))
        stat_dot.pack(side="right")

        zone_lbl = tk.Label(tf, text="Z?", bg=SURFACE, fg=TEXT_DIM, font=font(9, "bold"), anchor="w")
        zone_lbl.pack(fill="x")

        mid = tk.Frame(tf, bg=SURFACE)
        mid.pack(fill="x")
        cfg_lbl = tk.Label(mid, text="CB_0", bg=SURFACE, fg=TEXT_DIM, font=font(9))
        cfg_lbl.pack(side="left")
        ver_lbl = tk.Label(mid, text="v0", bg=SURFACE, fg=TEXT_FAINT, font=font(9))
        ver_lbl.pack(side="right")

        hlth_lbl = tk.Label(tf, text="OK", bg=SURFACE, fg=_fg("green"),
                            font=font(9, "bold"), anchor="w")
        hlth_lbl.pack(fill="x")

        return dict(card=card, zone=zone_lbl, cfg=cfg_lbl, ver=ver_lbl,
                    stat=stat_dot, hlth=hlth_lbl)

    def _update_ris_grid(self, snap: Dict) -> None:
        panels = snap.get("panels", {})
        ph     = snap.get("peer_health", {})
        stale  = ph.get("B", "") != "CONNECTED"

        for pid, wmap in self._panel_tiles.items():
            p    = panels.get(pid, {})
            zone = p.get("zone_id", "?")
            cfg  = p.get("current_config", "CB_0")
            ver  = p.get("config_version", 0)
            stat = p.get("command_status", "IDLE")
            hlth = p.get("health_state", "OK")

            stat_color = T.GRAY if stale else _solid(_panel_status_tone(stat))
            hlth_color = TEXT_FAINT if stale else _fg(_panel_health_tone(hlth))
            cfg_color  = (TEXT_FAINT if stale
                          else BLUE if cfg != "CB_0" else TEXT_DIM)

            wmap["card"].set_fill(T.TONES["red"][1] if hlth == "OFFLINE" else SURFACE)
            wmap["zone"].config(text=zone,        fg=ZONE_CLR.get(zone, TEXT_DIM))
            wmap["cfg"].config( text=cfg[:8],     fg=cfg_color)
            wmap["ver"].config( text=f"v{ver}",   fg=TEXT_FAINT)
            wmap["stat"].config(fg=stat_color)
            wmap["hlth"].config(text=hlth[:8].capitalize() if hlth != "OK" else "OK",
                                fg=hlth_color)

    # ── Tab 3: Trends ─────────────────────────────────────────────────────────

    def _build_trends_tab(self, parent: tk.Frame) -> None:
        from laptop_c.dashboard._charts import SparklineChart, MultiLineChart

        def _section(title: str, note: str = "") -> tk.Canvas:
            card = Card(parent, bg=BG)
            card.pack(fill="both", expand=True, pady=(0, GAP))
            _card_title(card.body, title, note)
            c = tk.Canvas(card.body, height=120, bg=SURFACE, highlightthickness=0)
            c.pack(fill="both", expand=True)
            return c

        c_pdr   = _section("Delivery rate", "history ≈5 min at 1 Hz")
        c_queue = _section("Queue occupancy by zone", "simulated model output")
        c_sinr  = _section("SINR by zone", "simulated model output")

        self._chart_pdr   = SparklineChart(c_pdr, color="#1e8e3e", unit="%",
                                           ymin=0.0, ymax=100.0)
        self._chart_queue = MultiLineChart(c_queue, unit="%", ymin=0.0, ymax=100.0)
        self._chart_sinr  = MultiLineChart(c_sinr,  unit=" dB")

    def _update_charts(self, history: Dict) -> None:
        try:
            self._chart_pdr.update(  history.get("pdr", []))
            self._chart_queue.update(history.get("queue_pct", {}))
            self._chart_sinr.update( history.get("sinr", {}))
        except Exception:
            pass

    # ── Tab 4: Event log ──────────────────────────────────────────────────────

    def _build_events_tab(self, parent: tk.Frame) -> None:
        """D4: filterable, pause-able event log with bounded rows."""
        card = Card(parent, bg=BG, expand=True, pad=(16, 14))
        card.pack(fill="both", expand=True)
        body = card.body

        flt = tk.Frame(body, bg=SURFACE)
        flt.pack(fill="x", pady=(0, 10))

        def _entry(label: str, var: tk.StringVar, width: int) -> None:
            tk.Label(flt, text=label, bg=SURFACE, fg=TEXT_DIM,
                     font=font(9)).pack(side="left", padx=(0, 5))
            tk.Entry(flt, textvariable=var, width=width, bg=SURFACE, fg=TEXT,
                     font=font(10), insertbackground=TEXT, relief="flat", bd=0,
                     highlightthickness=1, highlightbackground=OUTLINE,
                     highlightcolor=BLUE).pack(side="left", padx=(0, 14), ipady=4, ipadx=4)

        _entry("Zone",  self._flt_zone,  6)
        _entry("Panel", self._flt_panel, 6)
        _entry("Command", self._flt_cmd, 12)
        _entry("Type",  self._flt_type,  14)

        self._pause_btn = PillButton(flt, "Pause", self._toggle_pause, tone="gray")
        self._pause_btn.pack(side="right")

        _divider(body).pack(fill="x", pady=(0, 6))

        tf = tk.Frame(body, bg=SURFACE)
        tf.pack(fill="both", expand=True)

        ys = ttk.Scrollbar(tf, orient="vertical", style="Slim.Vertical.TScrollbar")
        ys.pack(side="right", fill="y")

        self._evt_txt = tk.Text(
            tf, bg=SURFACE, fg=TEXT_DIM, font=mono(9), height=12,
            wrap="none", state="disabled", relief="flat", borderwidth=0,
            highlightthickness=0, spacing1=2, spacing3=2,
            yscrollcommand=ys.set,
        )
        self._evt_txt.pack(side="left", fill="both", expand=True)
        ys.config(command=self._evt_txt.yview)

        self._evt_txt.tag_configure("error", foreground=_fg("red"))
        self._evt_txt.tag_configure("warn",  foreground=_fg("yellow"))
        self._evt_txt.tag_configure("ok",    foreground=_fg("green"))
        self._evt_txt.tag_configure("zone",  foreground=_fg("blue"))
        self._evt_txt.tag_configure("panel", foreground=_fg("purple"))
        self._evt_txt.tag_configure("mock",  foreground=_fg("orange"))
        self._evt_txt.tag_configure("dim",   foreground=TEXT_DIM)

    def _toggle_pause(self) -> None:
        paused = not self._log_paused.get()
        self._log_paused.set(paused)
        self._pause_btn.set("Resume" if paused else "Pause",
                            "yellow" if paused else "gray")

    def _update_events(self, events: List[Dict]) -> None:
        if self._log_paused.get():
            return

        fz = self._flt_zone.get().strip().upper()
        fp = self._flt_panel.get().strip().upper()
        fc = self._flt_cmd.get().strip().upper()
        ft = self._flt_type.get().strip().upper()

        filtered = [
            e for e in events
            if (not fz or (e.get("zone_id") or "").upper() == fz)
            and (not fp or (e.get("panel_id") or "").upper() == fp)
            and (not fc or fc in (e.get("command_id") or "").upper())
            and (not ft or ft in (e.get("event_type") or "").upper())
        ]
        to_show = filtered[-MAX_EVT_ROWS:]

        self._evt_txt.config(state="normal")
        self._evt_txt.delete("1.0", "end")
        for e in to_show:
            ts    = e.get("timestamp", 0)
            tstr  = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "??:??:??"
            etype = e.get("event_type", "EVENT")
            src   = e.get("source", "?")
            zid   = e.get("zone_id")
            pid   = e.get("panel_id")
            cid   = e.get("command_id")
            det   = e.get("details", "")

            line  = f"{tstr}  {src}: {etype}"
            if zid: line += f"  [{zid}]"
            if pid: line += f"  [{pid}]"
            if cid: line += f"  cmd={cid}"
            if det: line += f"  {det[:60]}"

            ety = etype.upper()
            tag = ("error" if any(k in ety for k in ("ERROR", "FAIL", "DROP", "NACK"))
                   else "warn"  if "CONGESTION" in ety
                   else "ok"    if any(k in ety for k in ("DELIVERY", "ACK", "APPLY"))
                   else "zone"  if zid
                   else "panel" if pid
                   else "dim")
            self._evt_txt.insert("end", line + "\n", tag)

        self._evt_txt.config(state="disabled")
        self._evt_txt.see("end")


# ── Thread wrapper ────────────────────────────────────────────────────────────

class DashboardThread(threading.Thread):
    """Owns a DashboardApp; safe to start/stop from asyncio code.

    On any failure (TclError, ImportError, missing display …) it prints
    "dashboard unavailable" and terminates without affecting the supervisor.
    """

    def __init__(self) -> None:
        super().__init__(name="dashboard-thread", daemon=True)
        self._app:   Optional[DashboardApp] = None
        self._ready  = threading.Event()
        self._error:  Optional[Exception]   = None

    def run(self) -> None:
        try:
            self._app = DashboardApp()
            try:
                self._app._root.after_idle(self._ready.set)
            except Exception:
                self._ready.set()
            self._app.mainloop()
        except Exception as exc:
            self._error = exc
            self._ready.set()
            print(f"dashboard unavailable: {exc}")
        finally:
            if self._app is not None:
                self._app.cleanup()
                self._app = None
            import gc
            gc.collect()

    def wait_ready(self, timeout: float = 6.0) -> bool:
        """Block until the window is shown (or failed). Returns True on success."""
        self._ready.wait(timeout=timeout)
        return self._error is None and self._app is not None

    def push_view(self, view: Dict) -> None:
        """Deliver a view dict to the dashboard (thread-safe)."""
        app = self._app
        if app is not None and app.is_alive():
            app.push_view(view)

    def stop(self) -> None:
        """Close the window and join the thread (safe to call from asyncio)."""
        if self._app is not None:
            self._app.request_stop()
        self._app = None
        if self.is_alive():
            self.join(timeout=5.0)
        import gc
        gc.collect()

    @property
    def is_dashboard_alive(self) -> bool:
        app = self._app
        return app is not None and app.is_alive()