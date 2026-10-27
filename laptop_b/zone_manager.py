"""Five-zone state tracking, congestion classification and telemetry history (Stage B2).

Every measurement stored here is either:
  - Received from Laptop A (a simulated/approximated value, labelled explicitly)
  - Computed from received counters (e.g. PDR from delivered/dropped counts)
Nothing is fabricated. Missing measurements are stored as None and rendered as N/A.

Congestion state machine (documented thresholds, configurable via CongestionPolicy):
  NORMAL      queue_ratio < low_threshold
  MODERATE    low_threshold <= queue_ratio < high_threshold
  CONGESTED   queue_ratio >= high_threshold  OR  pdr < pdr_floor
  SEVERE      queue_ratio >= severe_threshold  OR  pdr < severe_pdr_floor
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, List, Dict

NA = "N/A"

# Feedback acceptance codes
ACCEPTED = "ACCEPTED"
REJ_DUPLICATE = "REJECTED_DUPLICATE"
REJ_STALE = "REJECTED_STALE"
REJ_UNKNOWN_ZONE = "REJECTED_UNKNOWN_ZONE"

# Congestion states (ordered by severity)
CONGESTION_UNKNOWN = "UNKNOWN"
CONGESTION_NORMAL = "NORMAL"
CONGESTION_MODERATE = "MODERATE"
CONGESTION_CONGESTED = "CONGESTED"
CONGESTION_SEVERE = "SEVERE"


@dataclass
class CongestionPolicy:
    """Documented thresholds for queue-based and PDR-based congestion classification."""
    # Queue occupancy ratio thresholds (0..1)
    normal_max: float = 0.40        # <= this => NORMAL
    moderate_max: float = 0.70      # <= this => MODERATE
    severe_min: float = 0.90        # >= this => SEVERE

    # PDR (packet delivery ratio) thresholds (0..1), applied when PDR is available
    pdr_floor_moderate: float = 0.995  # PDR < this forces at least MODERATE
    pdr_floor_congested: float = 0.98  # PDR < this forces at least CONGESTED
    pdr_floor_severe: float = 0.95     # PDR < this forces SEVERE

    # Hysteresis: number of consecutive samples confirming a state before transitioning up/down
    hysteresis_up: int = 2     # samples needed to move to a worse state
    hysteresis_down: int = 3   # samples needed to move to a better state


@dataclass
class ZoneSnapshot:
    """One measurement window recorded in per-zone history."""
    ts: float                          # window_end timestamp
    seq: int
    queue_occupancy: int
    queue_capacity: int
    generated: int
    delivered: int
    dropped: int
    traffic_rate_pps: Optional[float]
    mean_sinr_db: Optional[float]      # SIMULATED channel-model output from Laptop A
    per: Optional[float]               # SIMULATED/approximated PER
    pdr: Optional[float]               # derived: delivered / (delivered + dropped)
    congestion_state: str = CONGESTION_UNKNOWN


@dataclass
class ZoneState:
    zone_id: str

    # --- live metrics (updated on each accepted report) ---
    reports_accepted: int = 0
    last_seq: Optional[int] = None
    last_report_ts: Optional[float] = None       # window_end of last accepted report

    queue_occupancy: Optional[int] = None
    queue_capacity: Optional[int] = None
    traffic_rate_pps: Optional[float] = None
    mean_sinr_db: Optional[float] = None         # SIMULATED (from Laptop A channel model)
    per: Optional[float] = None                  # SIMULATED/approximated
    pdr: Optional[float] = None                  # delivered / (delivered + dropped)

    # --- cumulative counters across all accepted windows ---
    total_generated: int = 0
    total_delivered: int = 0
    total_dropped: int = 0

    # --- congestion state ---
    congestion_state: str = CONGESTION_UNKNOWN
    _pending_state: Optional[str] = field(default=None, repr=False)
    _pending_count: int = field(default=0, repr=False)

    # --- decision tracking (populated by Stage B3+) ---
    last_decision: str = NA
    last_decision_ts: Optional[float] = None
    last_ris_update: str = NA
    last_ris_update_ts: Optional[float] = None

    # --- history (sliding window, max_history entries) ---
    history: List[ZoneSnapshot] = field(default_factory=list, repr=False)
    max_history: int = field(default=60, repr=False)  # ~60 one-second windows

    def queue_ratio(self) -> Optional[float]:
        if self.queue_occupancy is None or not self.queue_capacity:
            return None
        return self.queue_occupancy / self.queue_capacity

    def cumulative_pdr(self) -> Optional[float]:
        total = self.total_delivered + self.total_dropped
        return self.total_delivered / total if total > 0 else None

    def brief(self) -> str:
        q = NA if self.queue_occupancy is None else f"{self.queue_occupancy}/{self.queue_capacity}"
        sinr = NA if self.mean_sinr_db is None else f"{self.mean_sinr_db:.1f}dB"
        pdr_s = NA if self.pdr is None else f"{self.pdr*100:.1f}%"
        return (f"rpt={self.reports_accepted} q={q} sinr={sinr} "
                f"pdr={pdr_s} state={self.congestion_state}")

    def _append_history(self, snap: ZoneSnapshot):
        self.history.append(snap)
        if len(self.history) > self.max_history:
            self.history.pop(0)

    def apply_congestion_policy(self, policy: CongestionPolicy) -> str:
        """Classify current state; apply hysteresis; return new state."""
        new_state = self._classify(policy)
        if new_state == self.congestion_state:
            self._pending_state = None
            self._pending_count = 0
            return self.congestion_state

        if new_state != self._pending_state:
            self._pending_state = new_state
            self._pending_count = 1
        else:
            self._pending_count += 1

        _SEVERITY = {
            CONGESTION_UNKNOWN: 0,
            CONGESTION_NORMAL: 1,
            CONGESTION_MODERATE: 2,
            CONGESTION_CONGESTED: 3,
            CONGESTION_SEVERE: 4,
        }
        current_sev = _SEVERITY.get(self.congestion_state, 0)
        new_sev = _SEVERITY.get(new_state, 0)
        threshold = policy.hysteresis_up if new_sev > current_sev else policy.hysteresis_down

        if self._pending_count >= threshold:
            self.congestion_state = new_state
            self._pending_state = None
            self._pending_count = 0

        return self.congestion_state

    def _classify(self, policy: CongestionPolicy) -> str:
        """Pure classification: queue ratio and PDR (no hysteresis)."""
        qr = self.queue_ratio()
        pdr = self.pdr

        # PDR-based floor
        if pdr is not None:
            if pdr < policy.pdr_floor_severe:
                return CONGESTION_SEVERE
            if pdr < policy.pdr_floor_congested:
                return CONGESTION_CONGESTED
            if pdr < policy.pdr_floor_moderate:
                return CONGESTION_MODERATE

        # Queue-ratio based
        if qr is None:
            return CONGESTION_UNKNOWN
        if qr >= policy.severe_min:
            return CONGESTION_SEVERE
        if qr >= policy.moderate_max:
            return CONGESTION_CONGESTED
        if qr >= policy.normal_max:
            return CONGESTION_MODERATE
        return CONGESTION_NORMAL


class ZoneManager:
    """Manages five independently tracked zones with feedback acceptance rules."""

    def __init__(self, zone_ids: List[str], stale_after_s: float,
                 clock=time.time, seen_capacity: int = 50_000,
                 congestion_policy: Optional[CongestionPolicy] = None,
                 max_history: int = 60):
        self.zones: Dict[str, ZoneState] = {
            z: ZoneState(z, max_history=max_history) for z in zone_ids
        }
        self.stale_after_s = stale_after_s
        self.clock = clock
        self.policy = congestion_policy or CongestionPolicy()
        self._seen: set = set()
        self._seen_order: deque = deque()
        self._seen_capacity = seen_capacity
        # Global counters across all zones
        self.total_feedback_received: int = 0
        self.total_accepted: int = 0
        self.total_rejected: int = 0

    def _remember(self, msg_id: str):
        self._seen.add(msg_id)
        self._seen_order.append(msg_id)
        if len(self._seen_order) > self._seen_capacity:
            self._seen.discard(self._seen_order.popleft())

    def process_feedback(self, msg: dict):
        """Process a schema-validated FEEDBACK_REPORT message.

        Returns (status_code: str, reason: str).
        The caller is responsible for incrementing its own counters.
        """
        self.total_feedback_received += 1
        zone = self.zones.get(msg.get("zone_id"))
        if zone is None:
            self.total_rejected += 1
            return REJ_UNKNOWN_ZONE, f"zone {msg.get('zone_id')} not configured"

        msg_id = msg["msg_id"]
        if msg_id in self._seen:
            self.total_rejected += 1
            return REJ_DUPLICATE, "msg_id already processed"
        self._remember(msg_id)

        p = msg["payload"]
        if zone.last_seq is not None:
            if p["seq"] == zone.last_seq:
                self.total_rejected += 1
                return REJ_DUPLICATE, f"seq {p['seq']} already accepted"
            if p["seq"] < zone.last_seq:
                self.total_rejected += 1
                return REJ_STALE, f"seq {p['seq']} < last accepted {zone.last_seq}"

        age = self.clock() - p["window_end"]
        if age > self.stale_after_s:
            self.total_rejected += 1
            return REJ_STALE, f"window_end is {age:.1f}s old (> {self.stale_after_s}s)"

        # --- accept and update zone state ---
        zone.last_seq = p["seq"]
        zone.last_report_ts = p["window_end"]
        zone.queue_occupancy = p["queue_occupancy"]
        zone.queue_capacity = p["queue_capacity"]
        zone.traffic_rate_pps = p.get("traffic_rate_pps")
        zone.mean_sinr_db = p.get("mean_sinr_db")
        zone.per = p.get("per")

        final = p["delivered"] + p["dropped"]
        zone.pdr = (p["delivered"] / final) if final > 0 else None

        # cumulative counters
        zone.total_generated += p["generated"]
        zone.total_delivered += p["delivered"]
        zone.total_dropped += p["dropped"]

        # congestion classification with hysteresis
        zone.apply_congestion_policy(self.policy)

        # record snapshot in history
        snap = ZoneSnapshot(
            ts=p["window_end"],
            seq=p["seq"],
            queue_occupancy=p["queue_occupancy"],
            queue_capacity=p["queue_capacity"],
            generated=p["generated"],
            delivered=p["delivered"],
            dropped=p["dropped"],
            traffic_rate_pps=p.get("traffic_rate_pps"),
            mean_sinr_db=p.get("mean_sinr_db"),
            per=p.get("per"),
            pdr=zone.pdr,
            congestion_state=zone.congestion_state,
        )
        zone._append_history(snap)
        zone.reports_accepted += 1

        self.total_accepted += 1
        return ACCEPTED, "ok"

    def zone_summary_lines(self) -> List[str]:
        """Return one formatted line per zone for terminal display."""
        lines = []
        for zid, z in self.zones.items():
            lines.append(f"  {zid}: {z.brief()}")
        return lines

    def all_congestion_states(self) -> Dict[str, str]:
        return {zid: z.congestion_state for zid, z in self.zones.items()}

    def zones_needing_action(self) -> List[str]:
        """Return zone IDs in CONGESTED or SEVERE state."""
        return [
            zid for zid, z in self.zones.items()
            if z.congestion_state in (CONGESTION_CONGESTED, CONGESTION_SEVERE)
        ]
