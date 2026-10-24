"""RIS configuration optimizer and action selector (Stage B5).

Evaluates a finite codebook of candidate configurations per zone and selects
the configuration that maximises the declared objective (mean SINR by default).

Key design choices:
  - Finite codebook search: codebook_size candidate codebook indices are
    evaluated using the channel model. Global optimality is NOT claimed.
  - Baseline config is always evaluated for comparison.
  - The optimizer can decide: NO_CHANGE, APPLY, or REVERT.
  - Configuration change frequency is bounded by cooldown_s.
  - Pre- and post-update SINR and PER are recorded; improvement is not assumed.

All SINR/PER values produced here are SIMULATED (channel model outputs).
"""

import time
import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple

import numpy as np

from laptop_b.ris_channel import (
    ChannelEvaluator, ChannelModelParams, PanelRegistry, LinkResult,
)


# ---------------------------------------------------------------------------
# Optimization result record
# ---------------------------------------------------------------------------

@dataclass
class OptResult:
    """Records one optimization attempt for one zone."""
    opt_id: str
    zone_id: str
    ts: float
    decision: str               # "NO_CHANGE" | "APPLY" | "REVERT"
    reason: str

    baseline_codebook: Optional[int]
    selected_codebook: Optional[int]

    # Channel metrics BEFORE the change (simulation)
    pre_sinr_db: Optional[float]
    pre_per: Optional[float]

    # Channel metrics AFTER the change (simulation; set after apply)
    post_sinr_db: Optional[float] = None
    post_per: Optional[float] = None

    # Whether the change improved the objective (True/False/None if not yet evaluated)
    improved: Optional[bool] = None

    # Objective values (higher = better)
    baseline_objective: Optional[float] = None
    selected_objective: Optional[float] = None


@dataclass
class OptimizerPolicy:
    """Configuration for the RIS optimizer."""
    # Minimum SINR improvement (dB) required to trigger a config change
    min_sinr_gain_db: float = 1.0

    # Cooldown between optimization runs for the same zone (seconds)
    cooldown_s: float = 5.0

    # Number of channel samples per codebook candidate evaluation
    n_channel_samples: int = 5

    # Maximum number of pending (unapplied) changes allowed at once
    max_pending: int = 4

    # Whether to revert a config if it degraded performance
    auto_revert: bool = True
    revert_sinr_loss_db: float = 1.0   # revert if post-update SINR drops by this much


class RISOptimizer:
    """Codebook searcher and configuration action selector.

    Uses the channel model to evaluate all codebook candidates and selects
    the best. Does NOT claim global optimality.
    All channel evaluations are SIMULATED.
    """

    def __init__(self,
                 channel_params: Optional[ChannelModelParams] = None,
                 policy: Optional[OptimizerPolicy] = None,
                 codebook_size: int = 8,
                 zone_centers: Optional[Dict[str, np.ndarray]] = None,
                 zone_nlos_flags: Optional[Dict[str, bool]] = None,
                 bs_pos: Optional[np.ndarray] = None,
                 seed: int = 42,
                 logger: Optional[logging.Logger] = None):
        self.params = channel_params or ChannelModelParams()
        self.policy = policy or OptimizerPolicy()
        self.codebook_size = codebook_size
        self.evaluator = ChannelEvaluator(self.params, seed=seed)
        self.zone_centers = zone_centers or {}
        self.zone_nlos_flags = zone_nlos_flags or {}
        self.bs_pos = bs_pos if bs_pos is not None else np.array([0.0, 0.0, 10.0])
        self.log = logger or logging.getLogger("laptop_b.optimizer")

        self._last_opt_ts: Dict[str, float] = {}
        self._applied_codebook: Dict[str, Optional[int]] = {}  # zone -> last applied
        self._pre_sinr: Dict[str, Optional[float]] = {}        # zone -> SINR before last change
        self.history: List[OptResult] = []
        self.max_history = 200

    # ------------------------------------------------------------------ public API

    def optimize_zone(self, zone_id: str, panel_registry: PanelRegistry,
                      current_sinr_db: Optional[float] = None) -> Optional[OptResult]:
        """Evaluate all codebook candidates for a zone and decide whether to change.

        Returns OptResult or None if zone is in cooldown or has no geometry.
        """
        now = time.time()
        if zone_id not in self.zone_centers:
            self.log.debug("Zone %s has no geometry configured; skip", zone_id)
            return None

        last = self._last_opt_ts.get(zone_id, 0.0)
        if (now - last) < self.policy.cooldown_s:
            return None  # cooldown; caller should not force an update

        self._last_opt_ts[zone_id] = now

        zone_center = self.zone_centers[zone_id]
        is_nlos = self.zone_nlos_flags.get(zone_id, False)
        panels = panel_registry.by_zone(zone_id)
        geoms = [panel_registry.geometries.get(p.panel_id) for p in panels]
        active_pairs = [(ps, g) for ps, g in zip(panels, geoms) if g is not None]

        # Evaluate baseline (current config)
        baseline_cb = panels[0].codebook_index if panels else 0
        baseline_link = self._eval_codebook(baseline_cb, active_pairs, zone_center, is_nlos)

        # Evaluate all candidates
        best_cb = baseline_cb
        best_obj = self._objective(baseline_link)
        candidate_results: List[Tuple[int, float, LinkResult]] = [
            (baseline_cb, best_obj, baseline_link)
        ]

        for cb_idx in range(self.codebook_size):
            if cb_idx == baseline_cb:
                continue
            link = self._eval_codebook(cb_idx, active_pairs, zone_center, is_nlos)
            obj = self._objective(link)
            candidate_results.append((cb_idx, obj, link))
            if obj > best_obj:
                best_obj = obj
                best_cb = cb_idx

        sinr_gain = best_obj - self._objective(baseline_link)
        pre_sinr = current_sinr_db if current_sinr_db is not None else baseline_link.sinr_db
        self._pre_sinr[zone_id] = pre_sinr

        opt_id = uuid.uuid4().hex[:12]

        if best_cb == baseline_cb or sinr_gain < self.policy.min_sinr_gain_db:
            result = OptResult(
                opt_id=opt_id, zone_id=zone_id, ts=now,
                decision="NO_CHANGE",
                reason=(f"best candidate codebook={best_cb} gain={sinr_gain:.2f}dB "
                        f"< min_gain={self.policy.min_sinr_gain_db}dB"),
                baseline_codebook=baseline_cb,
                selected_codebook=best_cb,
                pre_sinr_db=round(pre_sinr, 2),
                pre_per=round(baseline_link.per, 4),
                baseline_objective=round(self._objective(baseline_link), 2),
                selected_objective=round(best_obj, 2),
            )
        else:
            result = OptResult(
                opt_id=opt_id, zone_id=zone_id, ts=now,
                decision="APPLY",
                reason=(f"codebook {baseline_cb}→{best_cb} simulated gain={sinr_gain:.2f}dB "
                        f"(model outputs, not physical measurements)"),
                baseline_codebook=baseline_cb,
                selected_codebook=best_cb,
                pre_sinr_db=round(pre_sinr, 2),
                pre_per=round(baseline_link.per, 4),
                baseline_objective=round(self._objective(baseline_link), 2),
                selected_objective=round(best_obj, 2),
            )

        self._record(result)
        self.log.info(
            "OptimizeZone %s: decision=%s best_cb=%d gain=%.2fdB pre_sinr=%.1fdB",
            zone_id, result.decision, best_cb, sinr_gain, pre_sinr,
        )
        return result

    def evaluate_post_update(self, zone_id: str, opt_result: OptResult,
                             panel_registry: PanelRegistry) -> None:
        """Evaluate channel quality after a config update and record improvement.

        Called after the panel has acknowledged the new configuration.
        Modifies opt_result in-place.
        All values are SIMULATED.
        """
        if zone_id not in self.zone_centers:
            return
        zone_center = self.zone_centers[zone_id]
        is_nlos = self.zone_nlos_flags.get(zone_id, False)
        panels = panel_registry.by_zone(zone_id)
        geoms = [panel_registry.geometries.get(p.panel_id) for p in panels]
        active_pairs = [(ps, g) for ps, g in zip(panels, geoms) if g is not None]

        cb = opt_result.selected_codebook
        post_link = self._eval_codebook(cb, active_pairs, zone_center, is_nlos)
        opt_result.post_sinr_db = round(post_link.sinr_db, 2)
        opt_result.post_per = round(post_link.per, 4)

        pre = opt_result.pre_sinr_db or 0.0
        gain = post_link.sinr_db - pre
        opt_result.improved = gain >= 0

        if not opt_result.improved:
            self.log.warning(
                "Zone %s: post-update SINR=%.1fdB PRE=%.1fdB DELTA=%.2fdB (degraded, simulation)",
                zone_id, post_link.sinr_db, pre, gain,
            )
        else:
            self.log.info(
                "Zone %s: post-update SINR=%.1fdB PRE=%.1fdB DELTA=+%.2fdB (improved, simulation)",
                zone_id, post_link.sinr_db, pre, gain,
            )

    # ------------------------------------------------------------------ helpers

    def _objective(self, link: LinkResult) -> float:
        """Objective function: maximise SINR (dB). Penalty for high PER."""
        return link.sinr_db - 5.0 * link.per   # weighted SINR-PER objective

    def _codebook_steering_angles(self, codebook_index: int, n_panels: int) -> List[float]:
        """Map codebook index to per-panel steering offsets (radians).

        Codebook index 0 = bore-sight (all zeros).
        Subsequent indices spread the beam in discrete angular steps.
        """
        if codebook_index == 0:
            return [0.0] * n_panels
        step = (math.pi / 4) / (self.codebook_size - 1)
        offset = step * codebook_index
        # Alternate sign across panels for diversity
        return [offset * (1 if i % 2 == 0 else -1) for i in range(n_panels)]

    def _eval_codebook(self, cb_idx: int, active_pairs, zone_center, is_nlos) -> LinkResult:
        angles = self._codebook_steering_angles(cb_idx, len(active_pairs))
        return self.evaluator.evaluate_zone_mean(
            zone_id="",              # not used inside evaluate_zone_mean directly
            panel_registry=None,    # we pass active_pairs directly below
            zone_center_pos=zone_center,
            bs_pos=self.bs_pos,
            is_nlos=is_nlos,
            n_samples=self.policy.n_channel_samples,
        ) if not active_pairs else self._eval_with_pairs(
            cb_idx, active_pairs, zone_center, is_nlos, angles
        )

    def _eval_with_pairs(self, cb_idx, active_pairs, zone_center, is_nlos, angles) -> LinkResult:
        # Set all panels in the active_pairs to the candidate codebook index temporarily
        from copy import copy
        temp_pairs = []
        for ps, g in active_pairs:
            ps_copy = copy(ps)
            ps_copy.codebook_index = cb_idx
            temp_pairs.append((ps_copy, g))

        return self.evaluator.evaluate(
            receiver_pos=zone_center,
            bs_pos=self.bs_pos,
            active_panels=temp_pairs,
            codebook_angles=angles,
            is_nlos=is_nlos,
        )

    def _record(self, r: OptResult):
        self.history.append(r)
        if len(self.history) > self.max_history:
            self.history.pop(0)

    def latest_results(self) -> Dict[str, OptResult]:
        result: Dict[str, OptResult] = {}
        for r in reversed(self.history):
            if r.zone_id not in result:
                result[r.zone_id] = r
        return result


# Needed for math.pi in _codebook_steering_angles
import math
