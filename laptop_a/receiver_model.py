"""
receiver_model.py — Simulated Wireless Channel & Delivery Evaluation

Models the radio environment inside the stadium, including:
  • Free-space path loss with distance-dependent attenuation.
  • Non-Line-of-Sight (NLoS) probabilistic obstruction.
  • Zone-specific extra attenuation (structural blockage).
  • Simplified Rayleigh fading (fast-fading fluctuation).
  • 32-panel RIS gain overlay — each panel adds constructive gain
    to its target zones when its phase is properly configured.
  • SINR-to-PER mapping via a sigmoid function.
  • Probabilistic packet delivery decision per PDU.

Integration points:
  → DevicePopulation provides receiver positions and zone geometry.
  → PDUGateway provides DL PDUs for delivery evaluation.
  → ReceiverFeedback records the delivery outcome.
  → Laptop B sends RIS_STATE_UPDATE messages to adjust panel phases.
  → Laptop C reads zone channel summaries for visualisation.
"""

import math
import numpy as np
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:
    from device_population import DevicePopulation
except ImportError:
    from laptop_a.device_population import DevicePopulation


# ---------------------------------------------------------------------------
#  Channel configuration defaults
# ---------------------------------------------------------------------------

@dataclass
class ChannelConfig:
    """Tunable parameters for the channel simulation."""
    # Path-loss model
    frequency_ghz: float = 3.5         # carrier frequency
    tx_power_dbm: float = 43.0         # BTS transmit power
    noise_floor_dbm: float = -95.0     # thermal noise + receiver NF
    # Fading
    fading_enabled: bool = True
    fading_std_db: float = 4.0         # Rayleigh fading std dev
    # NLoS
    nlos_extra_loss_db: float = 15.0   # additional loss when NLoS
    # RIS
    ris_max_gain_db: float = 12.0      # maximum gain from a single RIS panel
    # SINR → PER sigmoid
    sinr_threshold_db: float = 5.0     # PER = 0.5 at this SINR
    sigmoid_slope: float = 0.8         # steepness of the sigmoid
    # Delivery
    min_sinr_db: float = -5.0          # below this → guaranteed loss
    # Interference
    inter_zone_interference_db: float = 44.0  # loaded-stadium simulation baseline


# ---------------------------------------------------------------------------
#  Delivery Result
# ---------------------------------------------------------------------------

@dataclass
class DeliveryResult:
    """Outcome of evaluating one DL PDU delivery attempt."""
    delivered: bool
    sinr_db: float
    per: float
    latency_ms: float
    drop_reason: str            # "" if delivered, else reason string
    ris_gain_db: float = 0.0    # RIS contribution to SINR
    nlos: bool = False          # whether NLoS was applied
    fading_db: float = 0.0     # fading sample

    def to_dict(self) -> dict:
        return {
            "delivered": self.delivered,
            "sinr_db": round(self.sinr_db, 2),
            "per": round(self.per, 4),
            "latency_ms": round(self.latency_ms, 3),
            "drop_reason": self.drop_reason,
            "ris_gain_db": round(self.ris_gain_db, 2),
            "nlos": self.nlos,
            "fading_db": round(self.fading_db, 2),
        }


# ---------------------------------------------------------------------------
#  Receiver Model
# ---------------------------------------------------------------------------

class ReceiverModel:
    """
    Simulated wireless channel for the stadium BTS-to-receiver link.

    Usage:
        model = ReceiverModel(population)
        result = model.evaluate_delivery(pdu, sim_time)

    The model computes per-PDU SINR by combining:
        signal = tx_power - path_loss - nlos_loss - zone_atten + ris_gain + fading
        SINR   = signal - noise - interference
        PER    = sigmoid(SINR)
        delivered = random() > PER
    """

    def __init__(
        self,
        population: DevicePopulation,
        channel_config: Optional[ChannelConfig] = None,
        rng_seed: int = 777,
    ):
        self.pop = population
        self.cfg = channel_config or ChannelConfig()
        self.rng = np.random.default_rng(rng_seed)

        # RIS panel state — mutable, updated by Laptop B
        # panel_id → {"gain_db": float, "target_zones": [int], "phase": int, "active": bool}
        self.ris_state: Dict[int, dict] = {}
        self._init_ris_state()

        # Per-zone override SINR offsets (set by Laptop B scheduling decisions)
        self.zone_sinr_offset: Dict[int, float] = {z: 0.0 for z in range(population.num_zones)}

        # Propagation cache — distance from BTS to each device (precomputed)
        self._bts_distance: Optional[np.ndarray] = None
        self._recompute_distances()

    # ----- initialisation -----

    def _init_ris_state(self) -> None:
        """Initialise RIS state from the population's panel definitions."""
        for panel in self.pop.ris_panels:
            pid = panel["panel_id"]
            self.ris_state[pid] = {
                "gain_db": panel.get("gain_db", 0.0),
                "target_zones": list(panel.get("target_zones", [])),
                "phase": panel.get("current_phase", 0),
                "active": panel.get("active", True),
            }

    def _recompute_distances(self) -> None:
        """Compute Euclidean distance from BTS to every device."""
        dx = self.pop.pos_x - self.pop.bts_position[0]
        dy = self.pop.pos_y - self.pop.bts_position[1]
        dz = self.pop.pos_z - self.pop.bts_position[2]
        self._bts_distance = np.sqrt(dx * dx + dy * dy + dz * dz).astype(np.float32)
        # Clamp minimum distance to 1 m to avoid log(0)
        self._bts_distance = np.maximum(self._bts_distance, 1.0)

    # ----- RIS state management (integration with Laptop B) -----

    def _phase_to_gain_db(self, phase: int) -> float:
        """Convert a simulated codebook phase state into a per-panel gain estimate.

        This keeps the A-side channel model responsive to B's codebook decisions,
        while remaining a software simulation rather than RF calibration.
        """
        if phase is None:
            return 0.0
        phase = int(phase)
        if phase <= 3:
            return 0.0
        # Keep this contribution deliberately modest so the explicit zone-level
        # offset remains the primary control signal in the model.
        max_phase = 7  # default codebook range used by the RIS optimizer
        normalized = max(0.0, (phase - 3) / max(1, max_phase - 3))
        return min(self.cfg.ris_max_gain_db * 0.25 * normalized, 3.0)

    def apply_ris_update(self, update: dict) -> bool:
        """
        Apply a RIS state update received from Laptop B.

        Expected format:
            {
                "panels": [
                    {"panel_id": 0, "phase": 5, "gain_db": 8.2, "target_zones": [0,1,4]},
                    ...
                ]
            }

        Returns True if update was applied successfully.
        """
        panels = update.get("panels", [])
        if not panels:
            return False

        for p in panels:
            pid = p.get("panel_id")
            if pid is None or pid not in self.ris_state:
                continue
            if "phase" in p:
                self.ris_state[pid]["phase"] = p["phase"]
                if "gain_db" not in p:
                    self.ris_state[pid]["gain_db"] = self._phase_to_gain_db(p["phase"])
            if "gain_db" in p:
                self.ris_state[pid]["gain_db"] = min(p["gain_db"], self.cfg.ris_max_gain_db)
            if "target_zones" in p:
                self.ris_state[pid]["target_zones"] = list(p["target_zones"])
            if "active" in p:
                self.ris_state[pid]["active"] = p["active"]

        return True

    def get_ris_gain_for_zone(self, zone_id: int) -> float:
        """
        Aggregate RIS gain (dB) for a zone from all active panels targeting it.
        Uses power-sum (not simple dB addition) to model constructive combining.
        """
        linear_sum = 0.0
        for pid, state in self.ris_state.items():
            if state["active"] and zone_id in state["target_zones"]:
                linear_sum += 10 ** (state["gain_db"] / 10.0)

        if linear_sum <= 0:
            return 0.0
        combined_db = 10.0 * math.log10(linear_sum)
        return min(combined_db, self.cfg.ris_max_gain_db * 1.5)  # cap to avoid unrealistic gain

    # ----- channel computation -----

    def _path_loss_db(self, distance_m: float) -> float:
        """Free-space path loss (Friis) at the configured frequency."""
        if distance_m <= 0:
            distance_m = 1.0
        # FSPL = 20*log10(d) + 20*log10(f_GHz) + 32.44
        return 20.0 * math.log10(distance_m) + 20.0 * math.log10(self.cfg.frequency_ghz) + 32.44

    def compute_sinr(self, device_id: int) -> Tuple[float, float, bool, float]:
        """
        Compute SINR (dB) for a single device.

        Returns:
            (sinr_db, ris_gain_db, is_nlos, fading_sample_db)
        """
        zone = int(self.pop.zone_ids[device_id])
        geo = self.pop.zones[zone]
        dist = float(self._bts_distance[device_id])

        # Base path loss
        pl = self._path_loss_db(dist)

        # NLoS probabilistic check
        is_nlos = self.rng.random() < geo.nlos_base_prob
        nlos_loss = self.cfg.nlos_extra_loss_db if is_nlos else 0.0

        # Zone structural attenuation
        zone_atten = geo.extra_atten_db

        # RIS gain for this zone
        ris_gain = self.get_ris_gain_for_zone(zone)

        # Fading
        fading = 0.0
        if self.cfg.fading_enabled:
            fading = float(self.rng.normal(0, self.cfg.fading_std_db))

        # Received signal power
        rx_power = self.cfg.tx_power_dbm - pl - nlos_loss - zone_atten + ris_gain + fading

        # Interference (simplified: other zones contribute a flat interference floor)
        interference = self.cfg.noise_floor_dbm + self.cfg.inter_zone_interference_db

        # SINR
        sinr = rx_power - interference

        # Apply per-zone offset (from Laptop B scheduling decisions)
        sinr += self.zone_sinr_offset.get(zone, 0.0)

        return sinr, ris_gain, is_nlos, fading

    def sinr_to_per(self, sinr_db: float) -> float:
        """Map SINR (dB) to Packet Error Rate using a sigmoid."""
        # PER = 1 / (1 + exp(slope * (SINR - threshold)))
        exponent = self.cfg.sigmoid_slope * (sinr_db - self.cfg.sinr_threshold_db)
        exponent = max(-50, min(50, exponent))  # clamp to avoid overflow
        return 1.0 / (1.0 + math.exp(exponent))

    # ----- per-PDU delivery evaluation -----

    def evaluate_delivery(self, pdu: dict, sim_time: float) -> DeliveryResult:
        """
        Evaluate whether a downlink PDU is successfully received.

        Steps:
            1. Compute SINR at the destination device.
            2. Map SINR to PER.
            3. Random draw to decide delivery success.
            4. Compute latency (propagation + queueing estimate).

        Args:
            pdu:      A downlink PDU dict with "dst_device" and "gen_time".
            sim_time: Current simulation time.

        Returns:
            DeliveryResult with all channel details.
        """
        dst_device = pdu.get("dst_device", 0)
        gen_time = pdu.get("gen_time", sim_time)

        # Check if device is active
        if not self.pop.active[dst_device]:
            return DeliveryResult(
                delivered=False, sinr_db=0.0, per=1.0, latency_ms=0.0,
                drop_reason="device_inactive",
            )

        # Compute channel
        sinr, ris_gain, is_nlos, fading = self.compute_sinr(dst_device)

        # Hard cutoff
        if sinr < self.cfg.min_sinr_db:
            return DeliveryResult(
                delivered=False, sinr_db=sinr, per=1.0, latency_ms=0.0,
                drop_reason="sinr_below_min",
                ris_gain_db=ris_gain, nlos=is_nlos, fading_db=fading,
            )

        # PER
        per = self.sinr_to_per(sinr)

        # Delivery decision
        delivered = self.rng.random() > per

        # Latency model: base propagation + processing + queueing component
        prop_delay_ms = float(self._bts_distance[dst_device]) / 3e8 * 1e3  # speed of light
        processing_ms = self.rng.uniform(0.5, 2.0)
        queue_ms = (sim_time - gen_time) * 1000.0 if sim_time > gen_time else 0.0
        total_latency = prop_delay_ms + processing_ms + queue_ms

        # Additional NLoS delay
        if is_nlos:
            total_latency += self.rng.uniform(1.0, 5.0)

        drop_reason = "" if delivered else "per_failure"

        # Update device-level SINR/PER in the population
        self.pop.sinr_db[dst_device] = sinr
        self.pop.per[dst_device] = per

        return DeliveryResult(
            delivered=delivered,
            sinr_db=sinr,
            per=per,
            latency_ms=total_latency,
            drop_reason=drop_reason,
            ris_gain_db=ris_gain,
            nlos=is_nlos,
            fading_db=fading,
        )

    # ----- batch delivery (optimised for high throughput) -----

    def evaluate_batch(
        self, pdus: List[dict], sim_time: float
    ) -> List[DeliveryResult]:
        """Evaluate delivery for a batch of DL PDUs."""
        return [self.evaluate_delivery(pdu, sim_time) for pdu in pdus]

    # ----- zone-level channel summary (for feedback / telemetry) -----

    def get_zone_channel_summary(self) -> Dict[str, dict]:
        """
        Compute aggregate channel conditions per zone.
        Uses the current device-level SINR/PER stored in the population.
        """
        summary = {}
        for z in range(self.pop.num_zones):
            idx = self.pop.get_zone_devices(z)
            sinr_arr = self.pop.sinr_db[idx]
            per_arr = self.pop.per[idx]
            ris_gain = self.get_ris_gain_for_zone(z)

            summary[f"Z{z + 1}"] = {
                "mean_sinr_db": round(float(np.mean(sinr_arr)), 2),
                "min_sinr_db": round(float(np.min(sinr_arr)), 2),
                "max_sinr_db": round(float(np.max(sinr_arr)), 2),
                "mean_per": round(float(np.mean(per_arr)), 4),
                "ris_gain_db": round(ris_gain, 2),
                "nlos_prob": self.pop.zones[z].nlos_base_prob,
            }
        return summary

    def get_ris_summary(self) -> List[dict]:
        """Return current state of all 32 RIS panels (for Laptop C visualisation)."""
        return [
            {
                "panel_id": pid,
                "phase": state["phase"],
                "gain_db": round(state["gain_db"], 2),
                "target_zones": state["target_zones"],
                "active": state["active"],
            }
            for pid, state in sorted(self.ris_state.items())
        ]

    # ----- channel recalibration (called after mobility updates) -----

    def recalibrate_after_mobility(self) -> None:
        """Recompute BTS distances after device positions have changed."""
        self._recompute_distances()

    # ----- zone SINR offset (Laptop B control integration) -----

    def set_zone_sinr_offset(self, zone_id: int, offset_db: float) -> None:
        """
        Apply a per-zone simulated channel adjustment predicted by Laptop B's
        RIS model. This is a model input, not a physical measurement.
        """
        self.zone_sinr_offset[zone_id] = offset_db
