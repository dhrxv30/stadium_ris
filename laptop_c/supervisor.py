"""Laptop C Supervisor - Stage C2 + D1: Unified Supervisor State Model with History Buffer.

Maintains unified system state aggregated from Laptop A, Laptop B, and local C:
- Process and connection health (A, B, C)
- 50,000 logical device population across 5 stadium zones
- Directional PDU accounting (generated, delivered, dropped, pending, timed-out, ACK/NACK, retries)
- Five-zone condition metrics (queue occupancy/capacity, SINR, PER, latency, throughput, congestion)
- 32 virtual passive RIS panels (panel ID, zone, current config, version, update status, health)
- OOB control link status (TX attempts, unique commands, ACKs, NACKs, retries, timeouts, ACK ratio)
- Latest controller decisions and event log
- Experiment run ID, elapsed time, and random seed
- Rigorous formatting: unsupplied or incalculable values return "N/A".

D1 additions:
- HistoryBuffer: bounded, timestamped series for PDR, per-zone queue occupancy,
  per-zone SINR, and OOB ack ratio (~1 sample/s).
- Supervisor.tick(): call from the main loop ~1/s; samples into history; never
  blocks asyncio.
- Supervisor.get_view(): returns {snapshot, recent_events, history} as a deep
  copy so the dashboard never holds references to live internals.
"""
import collections
import copy
import time
from typing import Any, Dict, List, Optional, Union

from common.constants import (
    LAPTOP_A,
    LAPTOP_B,
    LAPTOP_C,
    MSG_EVENT,
    MSG_TELEMETRY_A,
    MSG_TELEMETRY_B,
    NUM_PANELS,
    NUM_ZONES,
    ZONE_IDS,
)
from common.schemas import make_default_device_population, make_default_panel_catalog


def safe_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    """Safely convert value to float, returning default if None or invalid."""
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def safe_int(v: Any, default: Optional[int] = None) -> Optional[int]:
    """Safely convert value to int, returning default if None or invalid."""
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def fmt_val(val: Any, fmt: Optional[str] = None, unit: str = "") -> str:
    """Format any metric, returning 'N/A' if None, empty, or uncomputable."""
    if val is None or val == "":
        return "N/A"
    if isinstance(val, float):
        if fmt:
            return f"{val:{fmt}}{unit}"
        return f"{val:.2f}{unit}"
    if isinstance(val, int):
        if fmt:
            return f"{val:{fmt}}{unit}"
        return f"{val}{unit}"
    return f"{val}{unit}"


class ZoneState:
    """Tracks state and metrics for one of the five stadium zones (Z1 - Z5)."""

    def __init__(self, zone_id: str, senders: int = 5000, receivers: int = 5000):
        self.zone_id = zone_id
        self.senders = senders
        self.receivers = receivers

        # Traffic & PDU counters
        self.generated_ul = 0
        self.delivered_ul = 0
        self.dropped_ul = 0
        self.pending_ul = 0
        self.timed_out_ul = 0

        self.generated_dl = 0
        self.delivered_dl = 0
        self.dropped_dl = 0
        self.pending_dl = 0
        self.timed_out_dl = 0

        # Queue metrics
        self.queue_occupancy: Optional[int] = None
        self.queue_capacity: Optional[int] = None

        # Wireless link conditions (simulated)
        self.sinr_db: Optional[float] = None
        self.estimated_per: Optional[float] = None
        self.mean_latency_ms: Optional[float] = None
        self.p95_latency_ms: Optional[float] = None
        self.throughput_bps: Optional[float] = None

        # Controller / RIS state
        self.congestion_state = "NORMAL"  # NORMAL, ELEVATED, CONGESTED
        self.service_multiplier: Optional[float] = None
        self.last_decision: Optional[str] = None
        self.last_ris_update: Optional[str] = None
        self.last_telemetry_ts: Optional[float] = None

    @property
    def total_generated(self) -> int:
        return self.generated_ul + self.generated_dl

    @property
    def total_delivered(self) -> int:
        return self.delivered_ul + self.delivered_dl

    @property
    def total_dropped(self) -> int:
        return self.dropped_ul + self.dropped_dl

    @property
    def total_pending(self) -> int:
        return self.pending_ul + self.pending_dl

    @property
    def pdr(self) -> Optional[float]:
        """Packet Delivery Ratio: delivered / (delivered + dropped) * 100%."""
        final_outcomes = self.total_delivered + self.total_dropped
        if final_outcomes > 0:
            return (self.total_delivered / final_outcomes) * 100.0
        return None

    @property
    def plr(self) -> Optional[float]:
        """Packet Loss Ratio: dropped / (delivered + dropped) * 100%."""
        final_outcomes = self.total_delivered + self.total_dropped
        if final_outcomes > 0:
            return (self.total_dropped / final_outcomes) * 100.0
        return None

    @property
    def queue_occupancy_pct(self) -> Optional[float]:
        if self.queue_capacity and self.queue_capacity > 0 and self.queue_occupancy is not None:
            return (self.queue_occupancy / self.queue_capacity) * 100.0
        return None

    def update_from_a(self, data: dict, ts: float):
        """Update metrics reported by Laptop A."""
        self.last_telemetry_ts = ts
        if "senders" in data:
            self.senders = safe_int(data["senders"], self.senders)
        if "receivers" in data:
            self.receivers = safe_int(data["receivers"], self.receivers)

        # UL/DL counters
        for k in ("generated_ul", "delivered_ul", "dropped_ul", "pending_ul", "timed_out_ul",
                  "generated_dl", "delivered_dl", "dropped_dl", "pending_dl", "timed_out_dl"):
            if k in data and data[k] is not None:
                setattr(self, k, safe_int(data[k], getattr(self, k)))

        # Queue and link metrics
        if "queue_occupancy" in data and data["queue_occupancy"] is not None:
            self.queue_occupancy = safe_int(data["queue_occupancy"])
        elif "queue" in data and data["queue"] is not None:  # fallback for simple mock payloads
            self.queue_occupancy = safe_int(data["queue"])
        elif "queue_ul" in data or "queue_dl" in data:
            self.queue_occupancy = safe_int(data.get("queue_ul", 0)) + safe_int(data.get("queue_dl", 0))
            if not self.queue_capacity:
                self.queue_capacity = 1000

        if "queue_capacity" in data and data["queue_capacity"] is not None:
            self.queue_capacity = safe_int(data["queue_capacity"])
        if "service_multiplier" in data and data["service_multiplier"] is not None:
            self.service_multiplier = safe_float(
                data["service_multiplier"], self.service_multiplier
            )

        if "generated" in data and "generated_dl" not in data:
            self.generated_dl = safe_int(data["generated"])
        if "delivered" in data and "delivered_dl" not in data:
            self.delivered_dl = safe_int(data["delivered"])
        if "dropped" in data and "dropped_dl" not in data:
            self.dropped_dl = safe_int(data["dropped"])

        if "latency" in data and isinstance(data["latency"], dict):
            self.mean_latency_ms = safe_float(data["latency"].get("mean_ms"), self.mean_latency_ms)
            self.p95_latency_ms = safe_float(data["latency"].get("p95_ms"), self.p95_latency_ms)

        if "sinr_db" in data and data["sinr_db"] is not None:
            self.sinr_db = safe_float(data["sinr_db"])
        if "estimated_per" in data and data["estimated_per"] is not None:
            self.estimated_per = safe_float(data["estimated_per"])
        elif "per" in data and data["per"] is not None:
            self.estimated_per = safe_float(data["per"])
        if "mean_latency_ms" in data and data["mean_latency_ms"] is not None:
            self.mean_latency_ms = safe_float(data["mean_latency_ms"])
        if "p95_latency_ms" in data and data["p95_latency_ms"] is not None:
            self.p95_latency_ms = safe_float(data["p95_latency_ms"])
        if "throughput_bps" in data and data["throughput_bps"] is not None:
            self.throughput_bps = safe_float(data["throughput_bps"])

    def update_from_b(self, data: dict, ts: float):
        """Update controller decisions and congestion state reported by Laptop B."""
        self.last_telemetry_ts = ts
        if "congestion_state" in data and data["congestion_state"] is not None:
            self.congestion_state = str(data["congestion_state"])
        if "last_decision" in data and data["last_decision"] is not None:
            self.last_decision = str(data["last_decision"])
        if "last_ris_update" in data and data["last_ris_update"] is not None:
            self.last_ris_update = str(data["last_ris_update"])
        if "sinr_db" in data and data["sinr_db"] is not None and self.sinr_db is None:
            self.sinr_db = safe_float(data["sinr_db"])
        if "per" in data and data["per"] is not None and self.estimated_per is None:
            self.estimated_per = safe_float(data["per"])
        if "queue_occupancy" in data and data["queue_occupancy"] is not None and self.queue_occupancy is None:
            self.queue_occupancy = safe_int(data["queue_occupancy"])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "senders": self.senders,
            "receivers": self.receivers,
            "generated_ul": self.generated_ul,
            "delivered_ul": self.delivered_ul,
            "dropped_ul": self.dropped_ul,
            "pending_ul": self.pending_ul,
            "timed_out_ul": self.timed_out_ul,
            "generated_dl": self.generated_dl,
            "delivered_dl": self.delivered_dl,
            "dropped_dl": self.dropped_dl,
            "pending_dl": self.pending_dl,
            "timed_out_dl": self.timed_out_dl,
            "total_generated": self.total_generated,
            "total_delivered": self.total_delivered,
            "total_dropped": self.total_dropped,
            "total_pending": self.total_pending,
            "pdr_pct": self.pdr,
            "plr_pct": self.plr,
            "queue_occupancy": self.queue_occupancy,
            "queue_capacity": self.queue_capacity,
            "queue_occupancy_pct": self.queue_occupancy_pct,
            "service_multiplier": self.service_multiplier,
            "sinr_db": self.sinr_db,
            "estimated_per": self.estimated_per,
            "mean_latency_ms": self.mean_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "throughput_bps": self.throughput_bps,
            "congestion_state": self.congestion_state,
            "last_decision": self.last_decision,
            "last_ris_update": self.last_ris_update,
            "last_telemetry_ts": self.last_telemetry_ts,
        }


class PanelState:
    """Tracks state for one of the 32 virtual passive RIS panels."""

    def __init__(
        self,
        panel_id: str,
        zone_id: str,
        current_config: str = "CB_0",
        config_version: int = 0,
        health_state: str = "OK",
    ):
        self.panel_id = panel_id
        self.zone_id = zone_id
        self.current_config = current_config
        self.config_version = config_version
        self.last_update_ts: Optional[float] = None
        self.command_status = "IDLE"  # IDLE, PENDING, APPLIED, FAILED
        self.health_state = health_state  # OK, DEGRADED, OFFLINE

    def update(self, data: dict, ts: float):
        if "current_config" in data and data["current_config"] is not None:
            self.current_config = str(data["current_config"])
        if "config_version" in data and data["config_version"] is not None:
            self.config_version = safe_int(data["config_version"], self.config_version)
        if "command_status" in data and data["command_status"] is not None:
            self.command_status = str(data["command_status"])
        if "health_state" in data and data["health_state"] is not None:
            self.health_state = str(data["health_state"])
        if "zone_id" in data and data["zone_id"] is not None:
            self.zone_id = str(data["zone_id"])
        self.last_update_ts = safe_float(data.get("last_update_ts"), ts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "panel_id": self.panel_id,
            "zone_id": self.zone_id,
            "current_config": self.current_config,
            "config_version": self.config_version,
            "last_update_ts": self.last_update_ts,
            "command_status": self.command_status,
            "health_state": self.health_state,
        }


class OOBState:
    """Tracks logically separate Out-of-Band (OOB) control link metrics."""

    def __init__(self):
        self.tx_attempts = 0
        self.unique_commands = 0
        self.acknowledged = 0
        self.negative_acknowledged = 0
        self.retries = 0
        self.timeouts = 0
        self.last_command_id: Optional[str] = None
        self.last_command_status: Optional[str] = None
        self.last_command_ts: Optional[float] = None
        self.last_ack_ts: Optional[float] = None

    @property
    def ack_ratio(self) -> Optional[float]:
        """OOB command acknowledgement ratio: acknowledged / unique_commands * 100%."""
        if self.unique_commands > 0:
            return (self.acknowledged / self.unique_commands) * 100.0
        return None

    def update(self, data: dict, ts: float):
        for k in ("tx_attempts", "unique_commands", "acknowledged", "negative_acknowledged",
                  "retries", "timeouts"):
            if k in data and data[k] is not None:
                setattr(self, k, safe_int(data[k], getattr(self, k)))
        if "acknowledged" not in data:
            acknowledged = data.get(
                "acknowledged_commands", data.get("commands_acked")
            )
            if acknowledged is not None:
                self.acknowledged = safe_int(acknowledged, self.acknowledged)
        if "last_command_id" in data and data["last_command_id"] is not None:
            self.last_command_id = str(data["last_command_id"])
        if "last_command_status" in data and data["last_command_status"] is not None:
            self.last_command_status = str(data["last_command_status"])
        if "last_command_ts" in data and data["last_command_ts"] is not None:
            self.last_command_ts = safe_float(data["last_command_ts"])
        if "last_ack_ts" in data and data["last_ack_ts"] is not None:
            self.last_ack_ts = safe_float(data["last_ack_ts"])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tx_attempts": self.tx_attempts,
            "unique_commands": self.unique_commands,
            "acknowledged": self.acknowledged,
            "negative_acknowledged": self.negative_acknowledged,
            "retries": self.retries,
            "timeouts": self.timeouts,
            "ack_ratio_pct": self.ack_ratio,
            "last_command_id": self.last_command_id,
            "last_command_status": self.last_command_status,
            "last_command_ts": self.last_command_ts,
            "last_ack_ts": self.last_ack_ts,
        }


# ---------------------------------------------------------------------------
# D1: Bounded history buffer
# ---------------------------------------------------------------------------

class HistoryBuffer:
    """Bounded, timestamped ring-buffer for a single scalar metric.

    Stores up to ``maxlen`` (timestamp, value_or_None) tuples.
    ``value`` is always float or None — never a formatted string.
    Thread-safe only via the GIL (same-process, no locks needed).
    """

    def __init__(self, maxlen: int = 300):
        """Create a buffer that holds at most *maxlen* samples."""
        if maxlen < 1:
            raise ValueError(f"maxlen must be >= 1, got {maxlen}")
        self._buf: collections.deque = collections.deque(maxlen=maxlen)
        self.maxlen = maxlen

    def append(self, value: Optional[float], ts: Optional[float] = None) -> None:
        """Append one sample.  *ts* defaults to ``time.monotonic()``."""
        self._buf.append((ts if ts is not None else time.monotonic(), value))

    def as_list(self) -> List[tuple]:
        """Return a plain list of (ts, value_or_None) tuples (oldest first)."""
        return list(self._buf)

    def __len__(self) -> int:
        return len(self._buf)

    def __repr__(self) -> str:  # pragma: no cover
        return f"HistoryBuffer(maxlen={self.maxlen}, len={len(self._buf)})"


class Supervisor:
    """Unified Supervisor state model for Laptop C."""

    # Maximum number of 1-Hz history samples to retain (~5 min at 1 Hz)
    HISTORY_MAXLEN: int = 300
    ROLLING_LOSS_WINDOW_S: float = 10.0

    def __init__(self, run_id: Optional[str] = None, random_seed: Optional[Union[int, str]] = None):
        self.run_id = run_id
        self.random_seed = random_seed
        self.start_wall_time = time.time()
        self.start_mono_time = time.monotonic()

        # Peer health tracking
        self.peer_health: Dict[str, str] = {
            LAPTOP_A: "NEVER_CONNECTED",
            LAPTOP_B: "NEVER_CONNECTED",
            LAPTOP_C: "CONNECTED",
        }

        # Device population (default 50,000 devices across 5 zones)
        self.device_population = make_default_device_population()

        # Five stadium zones
        self.zones: Dict[str, ZoneState] = {}
        for zid in ZONE_IDS:
            zinfo = self.device_population["per_zone"][zid]
            self.zones[zid] = ZoneState(zid, senders=zinfo["senders"], receivers=zinfo["receivers"])

        # 32 Virtual passive RIS panels
        self.panels: Dict[str, PanelState] = {}
        for pdata in make_default_panel_catalog():
            pid = pdata["panel_id"]
            self.panels[pid] = PanelState(
                panel_id=pid,
                zone_id=pdata["zone_id"],
                current_config=pdata["current_config"],
                config_version=pdata["config_version"],
                health_state=pdata["health_state"],
            )

        # System-level PDU aggregates
        self.ul_generated = 0
        self.ul_delivered = 0
        self.ul_dropped = 0
        self.ul_pending = 0
        self.ul_timed_out = 0

        self.dl_generated = 0
        self.dl_delivered = 0
        self.dl_dropped = 0
        self.dl_pending = 0
        self.dl_timed_out = 0

        self.ack_count = 0
        self.nack_count = 0
        self.retry_count = 0

        # Overall wireless performance metrics
        self.overall_mean_latency_ms: Optional[float] = None
        self.overall_p95_latency_ms: Optional[float] = None
        self.overall_throughput_bps: Optional[float] = None
        self._loss_samples = collections.deque(maxlen=120)
        self._rolling_plr_pct: Optional[float] = None

        # OOB link model
        self.oob = OOBState()

        # Recent decisions & events buffers (bounded)
        self.recent_decisions = collections.deque(maxlen=100)
        self.recent_events = collections.deque(maxlen=500)

        # D1: History buffers — sampled ~1/s by tick()
        self._history_pdr: HistoryBuffer = HistoryBuffer(self.HISTORY_MAXLEN)
        self._history_rolling_plr: HistoryBuffer = HistoryBuffer(self.HISTORY_MAXLEN)
        # Per-zone queue occupancy pct  {zone_id -> HistoryBuffer}
        self._history_queue_pct: Dict[str, HistoryBuffer] = {
            zid: HistoryBuffer(self.HISTORY_MAXLEN) for zid in self.zones
        }
        # Per-zone SINR  {zone_id -> HistoryBuffer}
        self._history_sinr: Dict[str, HistoryBuffer] = {
            zid: HistoryBuffer(self.HISTORY_MAXLEN) for zid in self.zones
        }
        self._history_oob_ack_ratio: HistoryBuffer = HistoryBuffer(self.HISTORY_MAXLEN)

        # Track last tick monotonic time to gate ~1 Hz sampling
        self._last_tick_mono: float = 0.0

        # D2/D6: True once any mock-flagged payload is received (for dashboard banner)
        self._is_mock: bool = False

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.start_mono_time

    @property
    def total_generated(self) -> int:
        return self.ul_generated + self.dl_generated

    @property
    def total_delivered(self) -> int:
        return self.ul_delivered + self.dl_delivered

    @property
    def total_dropped(self) -> int:
        return self.ul_dropped + self.dl_dropped

    @property
    def total_pending(self) -> int:
        return self.ul_pending + self.dl_pending

    @property
    def total_timed_out(self) -> int:
        return self.ul_timed_out + self.dl_timed_out

    @property
    def pdr(self) -> Optional[float]:
        """Overall Packet Delivery Ratio: delivered / (delivered + dropped) * 100%."""
        final_outcomes = self.total_delivered + self.total_dropped
        if final_outcomes > 0:
            return (self.total_delivered / final_outcomes) * 100.0
        return None

    @property
    def plr(self) -> Optional[float]:
        """Overall Packet Loss Ratio: dropped / (delivered + dropped) * 100%."""
        final_outcomes = self.total_delivered + self.total_dropped
        if final_outcomes > 0:
            return (self.total_dropped / final_outcomes) * 100.0
        return None

    # ---------- Telemetry ingestion & event handling ----------
    def on_message(self, expected_source: str, msg: dict):
        """Standard callback to connect to TelemetryCollector.on_message."""
        mtype = msg.get("msg_type")
        payload = msg.get("payload", {})
        ts = safe_float(msg.get("timestamp"), time.time())
        run_id = msg.get("run_id")

        if self.run_id is None and run_id:
            self.run_id = run_id

        from common.constants import (
            MSG_TELEMETRY_A, MSG_TELEMETRY_B, MSG_EVENT,
            MSG_FEEDBACK_REPORT, MSG_RIS_COMMAND, MSG_RIS_ACK,
            MSG_TELEMETRY_EVENT, MSG_CONTROLLER_STATE, MSG_TELEMETRY_UPDATE
        )
        if mtype in (MSG_TELEMETRY_A, MSG_FEEDBACK_REPORT, MSG_TELEMETRY_UPDATE):
            self.process_telemetry_a(payload, ts, msg.get("zone_id"))
        elif mtype in (MSG_TELEMETRY_B, MSG_CONTROLLER_STATE):
            self.process_telemetry_b(payload, ts, msg.get("zone_id"), msg.get("panel_id"))
        elif mtype in (MSG_EVENT, MSG_TELEMETRY_EVENT, MSG_RIS_COMMAND, MSG_RIS_ACK):
            self.process_event(payload, ts, expected_source, msg.get("zone_id"), msg.get("panel_id"))

    def process_telemetry_a(self, payload: dict, ts: float, zone_id: Optional[str] = None):
        """Process telemetry sent by Laptop A."""
        previous_delivered = self.total_delivered
        previous_dropped = self.total_dropped
        if payload.get("mock"):
            self._is_mock = True
        # 1. Device population update if reported
        if "device_population" in payload and isinstance(payload["device_population"], dict):
            dp = payload["device_population"]
            if "total" in dp:
                self.device_population["total"] = safe_int(dp["total"], self.device_population["total"])
            if "senders" in dp:
                self.device_population["senders"] = safe_int(dp["senders"], self.device_population["senders"])
            if "receivers" in dp:
                self.device_population["receivers"] = safe_int(dp["receivers"], self.device_population["receivers"])
            if "per_zone" in dp and isinstance(dp["per_zone"], dict):
                for zid, zinfo in dp["per_zone"].items():
                    if zid in self.zones and isinstance(zinfo, dict):
                        self.zones[zid].senders = safe_int(zinfo.get("senders"), self.zones[zid].senders)
                        self.zones[zid].receivers = safe_int(zinfo.get("receivers"), self.zones[zid].receivers)

        # 2. Traffic summary update
        if "snapshot" in payload and isinstance(payload["snapshot"], dict):
            snap = payload["snapshot"]
            if "global" in snap and isinstance(snap["global"], dict):
                g = snap["global"]
                self.dl_generated = safe_int(g.get("pdu_generated"), self.dl_generated)
                self.dl_delivered = safe_int(g.get("pdu_delivered"), self.dl_delivered)
                self.dl_dropped = safe_int(g.get("pdu_dropped"), self.dl_dropped)
                self.dl_pending = safe_int(g.get("pdu_pending"), self.dl_pending)
                if "throughput_bps" in g:
                    self.overall_throughput_bps = safe_float(g["throughput_bps"])
                if "latency" in g and isinstance(g["latency"], dict):
                    self.overall_mean_latency_ms = safe_float(g["latency"].get("mean_ms"))
                    self.overall_p95_latency_ms = safe_float(g["latency"].get("p95_ms"))
            if "zones" in snap and isinstance(snap["zones"], dict):
                for zid, zdata in snap["zones"].items():
                    if zid in self.zones and isinstance(zdata, dict):
                        self.zones[zid].update_from_a(zdata, ts)
            if "oob" in snap and isinstance(snap["oob"], dict):
                o = snap["oob"]
                self.oob.total_tx_attempts = safe_int(o.get("feedback_reports_sent"), self.oob.total_tx_attempts)
                self.oob.acked = safe_int(o.get("ris_updates_received"), self.oob.acked)
        elif "traffic_summary" in payload and isinstance(payload["traffic_summary"], dict):
            tsum = payload["traffic_summary"]
            if "uplink" in tsum and isinstance(tsum["uplink"], dict):
                ul = tsum["uplink"]
                self.ul_generated = safe_int(ul.get("generated"), self.ul_generated)
                self.ul_delivered = safe_int(ul.get("delivered"), self.ul_delivered)
                self.ul_dropped = safe_int(ul.get("dropped"), self.ul_dropped)
                self.ul_pending = safe_int(ul.get("pending"), self.ul_pending)
                self.ul_timed_out = safe_int(ul.get("timed_out"), self.ul_timed_out)
            if "downlink" in tsum and isinstance(tsum["downlink"], dict):
                dl = tsum["downlink"]
                self.dl_generated = safe_int(dl.get("generated"), self.dl_generated)
                self.dl_delivered = safe_int(dl.get("delivered"), self.dl_delivered)
                self.dl_dropped = safe_int(dl.get("dropped"), self.dl_dropped)
                self.dl_pending = safe_int(dl.get("pending"), self.dl_pending)
                self.dl_timed_out = safe_int(dl.get("timed_out"), self.dl_timed_out)
            if "ack_count" in tsum:
                self.ack_count = safe_int(tsum["ack_count"], self.ack_count)
            if "nack_count" in tsum:
                self.nack_count = safe_int(tsum["nack_count"], self.nack_count)
            if "retry_count" in tsum:
                self.retry_count = safe_int(tsum["retry_count"], self.retry_count)
        else:
            # Fallback for flat/mock telemetry
            if "generated" in payload:
                self.ul_generated += safe_int(payload["generated"], 0)
            if "delivered" in payload:
                self.ul_delivered += safe_int(payload["delivered"], 0)
            if "dropped" in payload:
                self.ul_dropped += safe_int(payload["dropped"], 0)
            if "pending" in payload:
                self.ul_pending = safe_int(payload["pending"], 0)

        # 3. Zones telemetry
        if "zones" in payload and isinstance(payload["zones"], dict):
            for zid, zdata in payload["zones"].items():
                if zid in self.zones and isinstance(zdata, dict):
                    self.zones[zid].update_from_a(zdata, ts)
        elif zone_id and zone_id in self.zones:
            self.zones[zone_id].update_from_a(payload, ts)

        # 4. Overall metrics
        if "metrics" in payload and isinstance(payload["metrics"], dict):
            m = payload["metrics"]
            if "mean_latency_ms" in m:
                self.overall_mean_latency_ms = safe_float(m["mean_latency_ms"])
            if "p95_latency_ms" in m:
                self.overall_p95_latency_ms = safe_float(m["p95_latency_ms"])
            if "throughput_bps" in m:
                self.overall_throughput_bps = safe_float(m["throughput_bps"])

        has_traffic_counters = (
            isinstance(payload.get("traffic_summary"), dict)
            or (
                isinstance(payload.get("snapshot"), dict)
                and isinstance(payload["snapshot"].get("global"), dict)
            )
            or any(key in payload for key in ("delivered", "dropped"))
        )
        if has_traffic_counters:
            self._record_loss_sample(
                previous_delivered,
                previous_dropped,
                self.total_delivered,
                self.total_dropped,
            )

    def _record_loss_sample(
        self,
        previous_delivered: int,
        previous_dropped: int,
        delivered: int,
        dropped: int,
    ) -> None:
        """Track terminal outcome counter deltas for a recent loss rate."""
        now = time.monotonic()
        if delivered < previous_delivered or dropped < previous_dropped:
            self._loss_samples.clear()
            delivered_delta, dropped_delta = delivered, dropped
        else:
            delivered_delta = delivered - previous_delivered
            dropped_delta = dropped - previous_dropped

        self._loss_samples.append((now, delivered_delta, dropped_delta))
        self._refresh_rolling_plr(now)

    def _refresh_rolling_plr(self, now: float) -> None:
        cutoff = now - self.ROLLING_LOSS_WINDOW_S
        while self._loss_samples and self._loss_samples[0][0] < cutoff:
            self._loss_samples.popleft()

        recent_delivered = sum(sample[1] for sample in self._loss_samples)
        recent_dropped = sum(sample[2] for sample in self._loss_samples)
        final_outcomes = recent_delivered + recent_dropped
        self._rolling_plr_pct = (
            recent_dropped / final_outcomes * 100.0 if final_outcomes else None
        )

    def process_telemetry_b(
        self,
        payload: dict,
        ts: float,
        zone_id: Optional[str] = None,
        panel_id: Optional[str] = None,
    ):
        """Process controller and RIS telemetry sent by Laptop B."""
        if payload.get("mock"):
            self._is_mock = True
        # 1. Controller decisions & status
        if "controller_status" in payload and isinstance(payload["controller_status"], dict):
            cstat = payload["controller_status"]
            if "last_decision" in cstat and isinstance(cstat["last_decision"], dict):
                d = cstat["last_decision"]
                self.recent_decisions.append({
                    "timestamp": safe_float(d.get("timestamp"), ts),
                    "decision": str(d.get("decision", "UNKNOWN")),
                    "reason": str(d.get("reason", "N/A")),
                    "zone_id": d.get("zone_id"),
                    "panel_id": d.get("panel_id"),
                    "config": d.get("config"),
                })
        elif "decision" in payload:  # fallback for flat/mock payload
            self.recent_decisions.append({
                "timestamp": ts,
                "decision": str(payload.get("decision", "UNKNOWN")),
                "reason": str(payload.get("reason", "N/A")),
                "zone_id": zone_id or payload.get("zone"),
                "panel_id": panel_id or payload.get("panel"),
                "config": payload.get("config"),
            })

        # 2. Zones state from controller view
        if "zones" in payload and isinstance(payload["zones"], dict):
            for zid, zdata in payload["zones"].items():
                if zid in self.zones and isinstance(zdata, dict):
                    self.zones[zid].update_from_b(zdata, ts)
        elif zone_id and zone_id in self.zones:
            self.zones[zone_id].update_from_b(payload, ts)

        # 3. 32 RIS Panels state
        if "ris_panels" in payload and isinstance(payload["ris_panels"], list):
            for pdata in payload["ris_panels"]:
                if isinstance(pdata, dict) and "panel_id" in pdata:
                    pid = str(pdata["panel_id"])
                    if pid in self.panels:
                        self.panels[pid].update(pdata, ts)

        # 4. OOB link metrics
        if "oob_metrics" in payload and isinstance(payload["oob_metrics"], dict):
            self.oob.update(payload["oob_metrics"], ts)

    def process_event(
        self,
        payload: dict,
        ts: float,
        source: str,
        zone_id: Optional[str] = None,
        panel_id: Optional[str] = None,
    ):
        """Process discrete system events."""
        event_record = {
            "timestamp": ts,
            "source": source,
            "event_type": str(payload.get("event_type", "UNKNOWN")),
            "event_id": payload.get("event_id"),
            "zone_id": zone_id or payload.get("zone_id"),
            "panel_id": panel_id or payload.get("panel_id"),
            "command_id": payload.get("command_id"),
            "pdu_id": payload.get("pdu_id"),
            "details": str(payload.get("details", "")),
        }
        self.recent_events.append(event_record)

    def update_peer_health(self, peer_health_map: Dict[str, str]):
        """Update peer health statuses from TelemetryCollector."""
        for p, st in peer_health_map.items():
            self.peer_health[p] = st

    # ---------- Queries and Snapshots ----------
    def get_zone(self, zone_id: str) -> Optional[ZoneState]:
        return self.zones.get(zone_id)

    def get_panel(self, panel_id: str) -> Optional[PanelState]:
        return self.panels.get(panel_id)

    def get_pdu_summary(self) -> Dict[str, Any]:
        return {
            "uplink": {
                "generated": self.ul_generated,
                "delivered": self.ul_delivered,
                "dropped": self.ul_dropped,
                "pending": self.ul_pending,
                "timed_out": self.ul_timed_out,
            },
            "downlink": {
                "generated": self.dl_generated,
                "delivered": self.dl_delivered,
                "dropped": self.dl_dropped,
                "pending": self.dl_pending,
                "timed_out": self.dl_timed_out,
            },
            "total_generated": self.total_generated,
            "total_delivered": self.total_delivered,
            "total_dropped": self.total_dropped,
            "total_pending": self.total_pending,
            "total_timed_out": self.total_timed_out,
            "ack_count": self.ack_count,
            "nack_count": self.nack_count,
            "retry_count": self.retry_count,
            "pdr_pct": self.pdr,
            "plr_pct": self.plr,
        }

    def get_oob_summary(self) -> Dict[str, Any]:
        return self.oob.to_dict()

    def snapshot(self) -> Dict[str, Any]:
        """Return a complete dictionary snapshot of the current supervisor state."""
        return {
            "run_id": self.run_id,
            "random_seed": self.random_seed,
            "elapsed_s": self.elapsed_s,
            "is_mock": self._is_mock,
            "peer_health": dict(self.peer_health),
            "device_population": dict(self.device_population),
            "pdu_summary": self.get_pdu_summary(),
            "overall_metrics": {
                "pdr_pct": self.pdr,
                "plr_pct": self.plr,
                "rolling_plr_pct": self._rolling_plr_pct,
                "mean_latency_ms": self.overall_mean_latency_ms,
                "p95_latency_ms": self.overall_p95_latency_ms,
                "throughput_bps": self.overall_throughput_bps,
            },
            "zones": {zid: z.to_dict() for zid, z in self.zones.items()},
            "panels": {pid: p.to_dict() for pid, p in self.panels.items()},
            "oob": self.get_oob_summary(),
            "latest_decision": self.recent_decisions[-1] if self.recent_decisions else None,
            "latest_event": self.recent_events[-1] if self.recent_events else None,
        }

    # ------------------------------------------------------------------
    # D1: History sampling and get_view()
    # ------------------------------------------------------------------

    def tick(self, min_interval_s: float = 0.8) -> bool:
        """Sample all history metrics at most once per *min_interval_s* seconds.

        Call this from the main asyncio loop (e.g., every ~1 s).  It is
        intentionally cheap: one monotonic clock read, one branch, then plain
        attribute reads — no I/O, no locks, no copies.

        Returns True if a sample was recorded, False if the interval has not
        elapsed yet.
        """
        now = time.monotonic()
        if now - self._last_tick_mono < min_interval_s:
            return False
        self._last_tick_mono = now

        ts = now  # use monotonic as the x-axis for all trend charts

        # Global PDR (None when no packets have been finalised)
        self._history_pdr.append(self.pdr, ts)
        self._refresh_rolling_plr(time.monotonic())
        self._history_rolling_plr.append(self._rolling_plr_pct, ts)

        # Per-zone queue occupancy pct and SINR
        for zid, z in self.zones.items():
            self._history_queue_pct[zid].append(z.queue_occupancy_pct, ts)
            self._history_sinr[zid].append(z.sinr_db, ts)

        # OOB ack ratio
        self._history_oob_ack_ratio.append(self.oob.ack_ratio, ts)

        return True

    def _build_history_view(self) -> Dict[str, Any]:
        """Return a plain-dict copy of all history buffers (safe to hand to dashboard)."""
        return {
            "pdr": self._history_pdr.as_list(),
            "rolling_plr": self._history_rolling_plr.as_list(),
            "queue_pct": {
                zid: buf.as_list() for zid, buf in self._history_queue_pct.items()
            },
            "sinr": {
                zid: buf.as_list() for zid, buf in self._history_sinr.items()
            },
            "oob_ack_ratio": self._history_oob_ack_ratio.as_list(),
        }

    def get_view(self) -> Dict[str, Any]:
        """Return a fully copied, read-only view suitable for the dashboard thread.

        The dashboard MUST use this method — never access supervisor internals
        directly.  The returned dict contains:

        * ``snapshot``      — deep-copied result of self.snapshot()
        * ``recent_events`` — plain list (copy) of the last 500 events
        * ``history``       — plain-dict copy of all history buffers

        N/A contract: any metric that is None in the live state will be None
        in the returned view.  The dashboard is responsible for rendering
        None as "N/A" and never fabricating values.
        """
        snap = copy.deepcopy(self.snapshot())
        events = list(self.recent_events)   # bounded deque -> list copy
        history = self._build_history_view()
        return {
            "snapshot": snap,
            "recent_events": events,
            "history": history,
        }
