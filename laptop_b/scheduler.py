"""Congestion-aware scheduler for Laptop B (Stage B3).

Two scheduler modes:
  - BaselineScheduler:   fixed equal allocation across all zones; no feedback used.
  - CongestionScheduler: allocates tokens proportional to congestion severity;
                         applies hysteresis/cooldown to avoid rapid oscillation.

Both schedulers produce a SchedulerDecision per zone per call.
All policy parameters are configurable; no scheduler is assumed optimal.
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict

from laptop_b.zone_manager import (
    ZoneState, ZoneManager,
    CONGESTION_NORMAL, CONGESTION_MODERATE,
    CONGESTION_CONGESTED, CONGESTION_SEVERE, CONGESTION_UNKNOWN,
    NA,
)


# ---------------------------------------------------------------------------
# Decision record
# ---------------------------------------------------------------------------

@dataclass
class SchedulerDecision:
    zone_id: str
    ts: float
    scheduler_type: str       # "BASELINE" | "CONGESTION_AWARE"
    action: str               # "HOLD" | "REDUCE" | "BOOST" | "BOOST_SERVICE"
    token_weight: float       # relative allocation weight for this zone (1.0 = nominal)
    reason: str
    congestion_state: str
    queue_ratio: Optional[float]
    pdr: Optional[float]
    mean_sinr_db: Optional[float]


# ---------------------------------------------------------------------------
# Baseline scheduler
# ---------------------------------------------------------------------------

class BaselineScheduler:
    """Equal-weight, no-feedback allocation. Documented baseline for comparison."""
    NAME = "BASELINE"

    def decide(self, zone_id: str, zone: ZoneState) -> SchedulerDecision:
        return SchedulerDecision(
            zone_id=zone_id,
            ts=time.time(),
            scheduler_type=self.NAME,
            action="HOLD",
            token_weight=1.0,
            reason="baseline: equal allocation, no feedback used",
            congestion_state=zone.congestion_state,
            queue_ratio=zone.queue_ratio(),
            pdr=zone.pdr,
            mean_sinr_db=zone.mean_sinr_db,
        )


# ---------------------------------------------------------------------------
# Congestion-aware scheduler
# ---------------------------------------------------------------------------

@dataclass
class CongestionSchedulerPolicy:
    # Weights assigned to each congestion state
    weight_normal: float = 1.0
    weight_moderate: float = 1.25
    weight_congested: float = 1.60
    weight_severe: float = 2.00
    weight_unknown: float = 1.0
    severe_service_multiplier: float = 2.0

    # SINR bonus: if SINR is above sinr_good_threshold, reduce weight slightly
    sinr_good_threshold_db: float = 20.0
    sinr_good_factor: float = 0.90  # multiply weight by this if SINR is good

    # Cooldown: minimum seconds before re-deciding for the same zone
    cooldown_s: float = 3.0

    # Actions mapped to states
    action_map: Dict[str, str] = field(default_factory=lambda: {
        CONGESTION_NORMAL: "HOLD",
        CONGESTION_MODERATE: "BOOST",
        CONGESTION_CONGESTED: "BOOST",
        CONGESTION_SEVERE: "BOOST_SERVICE",
        CONGESTION_UNKNOWN: "HOLD",
    })


class CongestionScheduler:
    """Congestion-aware scheduler with per-zone cooldown hysteresis.

    Decisions are logged for every call regardless of whether the action changed.
    The cooldown prevents the same zone from being re-decided too rapidly.
    """
    NAME = "CONGESTION_AWARE"

    def __init__(self, policy: Optional[CongestionSchedulerPolicy] = None,
                 logger: Optional[logging.Logger] = None):
        self.policy = policy or CongestionSchedulerPolicy()
        self.log = logger or logging.getLogger("laptop_b.scheduler")
        self._last_decision_ts: Dict[str, float] = {}
        self._last_action: Dict[str, str] = {}
        self.decision_history: List[SchedulerDecision] = []
        self.max_history = 500  # keep last N decisions for reporting

    def decide(self, zone_id: str, zone: ZoneState) -> SchedulerDecision:
        """Produce a scheduling decision for a single zone.

        If the zone is in cooldown, returns the previous action without change.
        """
        p = self.policy
        now = time.time()
        last_ts = self._last_decision_ts.get(zone_id, 0.0)
        in_cooldown = (now - last_ts) < p.cooldown_s

        state = zone.congestion_state
        action = p.action_map.get(state, "HOLD")
        qr = zone.queue_ratio()
        pdr = zone.pdr
        sinr = zone.mean_sinr_db

        # Compute weight
        weight_map = {
            CONGESTION_NORMAL: p.weight_normal,
            CONGESTION_MODERATE: p.weight_moderate,
            CONGESTION_CONGESTED: p.weight_congested,
            CONGESTION_SEVERE: p.weight_severe,
            CONGESTION_UNKNOWN: p.weight_unknown,
        }
        weight = weight_map.get(state, 1.0)
        if sinr is not None and sinr >= p.sinr_good_threshold_db:
            weight *= p.sinr_good_factor

        if in_cooldown:
            prev_action = self._last_action.get(zone_id, "HOLD")
            reason = (f"cooldown ({now - last_ts:.1f}s < {p.cooldown_s}s); "
                      f"holding previous action={prev_action}")
            action = prev_action
        else:
            reason_parts = [f"state={state}"]
            if qr is not None:
                reason_parts.append(f"q_ratio={qr:.2f}")
            if pdr is not None:
                reason_parts.append(f"pdr={pdr:.2f}")
            if sinr is not None:
                reason_parts.append(f"sinr={sinr:.1f}dB")
            reason = "; ".join(reason_parts)
            self._last_decision_ts[zone_id] = now
            self._last_action[zone_id] = action

        dec = SchedulerDecision(
            zone_id=zone_id,
            ts=now,
            scheduler_type=self.NAME,
            action=action,
            token_weight=round(weight, 4),
            reason=reason,
            congestion_state=state,
            queue_ratio=qr,
            pdr=pdr,
            mean_sinr_db=sinr,
        )
        self._record(dec)
        self.log.debug("Scheduler decision %s: action=%s weight=%.2f | %s",
                       zone_id, action, weight, reason)
        return dec

    def decide_all(self, zone_manager: ZoneManager) -> List[SchedulerDecision]:
        """Decide for all zones; return list of decisions."""
        decisions = []
        for zid, zone in zone_manager.zones.items():
            decisions.append(self.decide(zid, zone))
        return decisions

    def _record(self, dec: SchedulerDecision):
        self.decision_history.append(dec)
        if len(self.decision_history) > self.max_history:
            self.decision_history.pop(0)

    def latest_decisions(self) -> Dict[str, SchedulerDecision]:
        """Return the most recent decision per zone."""
        result: Dict[str, SchedulerDecision] = {}
        for dec in reversed(self.decision_history):
            if dec.zone_id not in result:
                result[dec.zone_id] = dec
        return result

    def summary_line(self) -> str:
        latest = self.latest_decisions()
        parts = []
        for zid in sorted(latest):
            d = latest[zid]
            parts.append(f"{zid}={d.action}(w={d.token_weight:.2f})")
        return "Scheduler[" + ", ".join(parts) + "]" if parts else "Scheduler[no decisions yet]"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_scheduler(scheduler_type: str,
                   policy: Optional[CongestionSchedulerPolicy] = None,
                   logger: Optional[logging.Logger] = None):
    """Return a scheduler instance.  scheduler_type: 'baseline' | 'congestion_aware'"""
    if scheduler_type == "baseline":
        return BaselineScheduler()
    elif scheduler_type == "congestion_aware":
        return CongestionScheduler(policy=policy, logger=logger)
    else:
        raise ValueError(f"Unknown scheduler type: {scheduler_type!r}")
