"""RIS channel and panel model (Stage B4).

THIS IS A SIMULATION MODEL — not a physical RIS deployment and not real RF measurements.

Channel model assumptions (documented):
  - Free-space path loss (FSPL) for direct link and RIS-assisted link.
  - RIS-assisted received power adds coherently to direct path
    using a simplified phase-array gain: G_ris ∝ N² · cos²(θ) where θ is the
    angular error between the RIS steering direction and the receiver direction,
    multiplied by the normalized square-array beam response for steered candidates.
    N = number of reflecting elements per panel (configurable; default 64).
  - Shadowing: log-normal, σ = shadow_std_db (configurable).
  - NLoS: additional loss of nlos_extra_db (configurable per zone).
  - Additive thermal noise power P_noise_dbm = -174 + 10·log10(BW_Hz) + noise_figure_db.
  - SINR = P_rx_dbm - P_noise_dbm  (interference treated as additional noise floor).
  - PER approximation: sigmoid function of SINR:
      PER ≈ 1 / (1 + exp(k_slope · (sinr_db - sinr_threshold_db)))
    k_slope and sinr_threshold_db are configurable.
  - No multi-path fading in Stage B4; may be extended later.

Reference geometry:
  - BS (Laptop A) is at the origin (0, 0, 0) in metres.
  - Receivers and RIS panels are at configurable (x, y, z) coordinates.
  - Distances are Euclidean in 3D.
"""

import math
import random
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple

import numpy as np

NA = "N/A"


# ---------------------------------------------------------------------------
# Channel model parameters
# ---------------------------------------------------------------------------

@dataclass
class ChannelModelParams:
    """All parameters are documented simulation parameters, not hardware measurements."""
    # Frequency
    freq_hz: float = 3.5e9            # 3.5 GHz (Sub-6 GHz example)

    # Path-loss exponent for direct link
    path_loss_exp: float = 2.5         # free-space = 2.0; indoor = 2.5–4.0

    # RIS
    ris_elements_per_panel: int = 64   # N reflecting elements per panel
    ris_reflection_efficiency: float = 0.7  # η, 0 < η ≤ 1

    # Noise
    bandwidth_hz: float = 20e6        # 20 MHz
    noise_figure_db: float = 7.0      # receiver noise figure

    # Shadowing
    shadow_std_db: float = 4.0        # log-normal shadowing σ

    # NLoS extra loss (per zone, applied when zone is marked NLoS)
    nlos_extra_db: float = 10.0

    # PER approximation (sigmoid)
    per_sinr_threshold_db: float = 5.0   # SINR at which PER ≈ 0.5
    per_k_slope: float = 0.5            # steepness; larger => sharper transition

    # Transmit power
    tx_power_dbm: float = 30.0          # BTS transmit power

    # Antenna gains
    tx_gain_db: float = 8.0
    rx_gain_db: float = 0.0


def _fspl_db(distance_m: float, freq_hz: float, path_loss_exp: float = 2.0) -> float:
    """Free-space path loss in dB. Extended with configurable exponent."""
    if distance_m < 0.1:
        distance_m = 0.1  # avoid log(0)
    wavelength = 3e8 / freq_hz
    # FSPL(dB) = 20·log10(4π·d/λ) for exp=2; generalised to exp·10·log10(d/d_ref) + FSPL(d_ref)
    fspl_ref = 20 * math.log10(4 * math.pi / wavelength)
    return fspl_ref + path_loss_exp * 10 * math.log10(distance_m)


def _noise_power_dbm(bandwidth_hz: float, noise_figure_db: float) -> float:
    """Thermal noise + noise figure in dBm."""
    k_boltzmann = 1.380649e-23
    T_kelvin = 290.0
    noise_W = k_boltzmann * T_kelvin * bandwidth_hz
    noise_dbm = 10 * math.log10(noise_W) + 30  # +30: W → mW
    return noise_dbm + noise_figure_db


@dataclass
class LinkResult:
    """Output of one channel evaluation. All values are simulation model outputs."""
    direct_rx_dbm: float          # received power on direct path (simulation)
    ris_rx_dbm: Optional[float]   # additional power from RIS-assisted path (simulation)
    total_rx_dbm: float           # combined received power (simulation)
    noise_dbm: float              # noise floor (simulation)
    sinr_db: float                # SINR (simulation)
    per: float                    # PER approximation (simulation, sigmoid model)
    is_nlos: bool
    shadow_db: float              # shadowing draw used in this evaluation (simulation)


# ---------------------------------------------------------------------------
# Panel geometry and state
# ---------------------------------------------------------------------------

@dataclass
class PanelGeometry:
    """Physical geometry of a virtual RIS panel (simulation coordinates, metres)."""
    panel_id: str
    zone_id: str
    pos: np.ndarray     # (x, y, z) in metres
    normal: np.ndarray  # unit normal vector of the panel surface


@dataclass
class PanelState:
    """Runtime state of one virtual passive RIS panel."""
    panel_id: str
    zone_id: str
    codebook_index: Optional[int]   # current phase state index
    config_version: int = 0
    last_update_ts: Optional[float] = None
    command_status: str = "NONE"    # NONE|PENDING|SENT|RETRYING|ACKED|FAILED
    last_command_id: Optional[str] = None
    health: str = "OK"              # simulated health state
    # Channel evaluation results (last evaluated)
    last_sinr_db: Optional[float] = None
    last_per: Optional[float] = None


PENDING_STATES = {"PENDING", "SENT", "RETRYING"}


class PanelRegistry:
    """Registry of 32 virtual passive RIS panels with geometry and state."""

    def __init__(self, panels: List[PanelState],
                 geometries: Optional[Dict[str, PanelGeometry]] = None):
        self.panels: Dict[str, PanelState] = {p.panel_id: p for p in panels}
        self.geometries: Dict[str, PanelGeometry] = geometries or {}

    @classmethod
    def build(cls, zone_ids: List[str], zone_panel_counts: List[int],
              initial_codebook_index: int,
              stadium_radius_m: float = 110.0):
        """Build zone-aligned catwalk groups matching Laptop A's virtual panels."""
        panels, geometries, n = [], {}, 0
        zone_angles = [math.pi / 2, 0.0, -math.pi / 2, math.pi]
        for zone_index, (zone_id, count) in enumerate(zip(zone_ids, zone_panel_counts)):
            if zone_index < len(zone_angles):
                angles = np.linspace(
                    zone_angles[zone_index] - math.pi / 8,
                    zone_angles[zone_index] + math.pi / 8,
                    count,
                )
            else:
                angles = np.linspace(0.0, 2.0 * math.pi, count, endpoint=False)
            for angle in angles:
                n += 1
                pid = f"P{n:02d}"
                x = stadium_radius_m * math.cos(angle)
                y = stadium_radius_m * math.sin(angle)
                z = 30.0
                pos = np.array([x, y, z])
                # Normal points inward toward stadium centre
                normal = np.array([-math.cos(angle), -math.sin(angle), 0.0])
                panels.append(PanelState(pid, zone_id, initial_codebook_index))
                geometries[pid] = PanelGeometry(pid, zone_id, pos, normal)
        return cls(panels, geometries)

    def by_zone(self, zone_id: str) -> List[PanelState]:
        return [p for p in self.panels.values() if p.zone_id == zone_id]

    @property
    def total(self) -> int:
        return len(self.panels)

    @property
    def active_configs(self) -> int:
        return sum(1 for p in self.panels.values() if p.codebook_index is not None)

    @property
    def pending_commands(self) -> int:
        return sum(1 for p in self.panels.values() if p.command_status in PENDING_STATES)

    def apply_config(self, panel_id: str, codebook_index: int,
                     command_id: str, clock=time.time) -> bool:
        """Apply a new codebook configuration to a panel. Returns True if applied."""
        p = self.panels.get(panel_id)
        if p is None or p.health != "OK":
            return False
        p.codebook_index = codebook_index
        p.config_version += 1
        p.last_update_ts = clock()
        p.command_status = "ACKED"
        p.last_command_id = command_id
        return True


# ---------------------------------------------------------------------------
# Channel evaluator
# ---------------------------------------------------------------------------

class ChannelEvaluator:
    """Evaluates simulated link quality for a receiver in a given zone.

    This is a SIMULATION MODEL. All outputs are labelled as simulated.
    No physical RIS hardware or actual radio measurements are used.
    """

    def __init__(self, params: Optional[ChannelModelParams] = None, seed: int = 42):
        self.params = params or ChannelModelParams()
        self._rng = random.Random(seed)
        self._noise_dbm = _noise_power_dbm(
            self.params.bandwidth_hz, self.params.noise_figure_db
        )

    def evaluate(self,
                 receiver_pos: np.ndarray,
                 bs_pos: np.ndarray,
                 active_panels: List[Tuple[PanelState, PanelGeometry]],
                 codebook_angles: Optional[List[float]] = None,
                 is_nlos: bool = False,
                 shadow_draw: Optional[float] = None) -> LinkResult:
        """Evaluate link quality for one receiver position (SIMULATED).

        Parameters
        ----------
        receiver_pos      : 3D position of the simulated receiver (metres).
        bs_pos            : 3D position of the base station (metres).
        active_panels     : List of (PanelState, PanelGeometry) in the zone.
        codebook_angles   : Per-panel steering angle offset (radians). None = bore-sight.
        is_nlos           : Whether this zone has NLoS obstruction.
        shadow_draw       : If given, use this value for shadowing (reproducible tests).
        """
        p = self.params
        # --- direct path ---
        d_direct = float(np.linalg.norm(receiver_pos - bs_pos))
        pl_direct = _fspl_db(d_direct, p.freq_hz, p.path_loss_exp)
        shadow = (shadow_draw if shadow_draw is not None
                  else self._rng.gauss(0, p.shadow_std_db))
        nlos_loss = p.nlos_extra_db if is_nlos else 0.0
        # Shadowing is a dB offset: positive values attenuate the link, negative
        # values improve it. The previous abs(shadow) formulation always degraded
        # the direct path, which was inconsistent with the stated log-normal model.
        direct_rx_dbm = (p.tx_power_dbm + p.tx_gain_db + p.rx_gain_db
                         - pl_direct - shadow - nlos_loss)

        # --- RIS-assisted path ---
        ris_power_linear = 0.0
        if active_panels:
            for i, (panel_state, geom) in enumerate(active_panels):
                if panel_state.codebook_index is None:
                    continue
                # BS → panel
                d_bs_ris = float(np.linalg.norm(geom.pos - bs_pos))
                pl_bs_ris = _fspl_db(d_bs_ris, p.freq_hz, 2.0)  # free-space to RIS

                # Panel → receiver
                d_ris_rx = float(np.linalg.norm(receiver_pos - geom.pos))
                pl_ris_rx = _fspl_db(d_ris_rx, p.freq_hz, 2.0)

                # Angular gain: dot product of panel normal and (receiver - panel) direction
                v_to_rx = (receiver_pos - geom.pos)
                dist_to_rx = float(np.linalg.norm(v_to_rx))
                if dist_to_rx > 0:
                    v_to_rx_unit = v_to_rx / dist_to_rx
                else:
                    v_to_rx_unit = geom.normal

                cos_theta = float(np.dot(geom.normal, v_to_rx_unit))
                cos_theta = max(0.0, cos_theta)  # no back-lobe in this model

                # Codebook steering offset: steering angle shifts the peak
                if codebook_angles is not None and i < len(codebook_angles):
                    steer_rad = codebook_angles[i]
                    target_angle = math.acos(cos_theta)
                    beam_error = abs(target_angle - steer_rad)
                    cos_theta = max(0.0, math.cos(beam_error))
                    elements_per_side = max(1, round(math.sqrt(p.ris_elements_per_panel)))
                    half_spacing_phase = 0.5 * math.pi * math.sin(beam_error)
                    denominator = elements_per_side * math.sin(half_spacing_phase)
                    if abs(denominator) < 1e-12:
                        array_amplitude = 1.0
                    else:
                        array_amplitude = (
                            math.sin(elements_per_side * half_spacing_phase)
                            / denominator
                        )
                    array_power_factor = min(1.0, max(0.0, array_amplitude ** 4))
                else:
                    array_power_factor = 1.0

                N = p.ris_elements_per_panel
                ris_array_gain = (
                    p.ris_reflection_efficiency
                    * (N ** 2)
                    * (cos_theta ** 2)
                    * array_power_factor
                )
                if ris_array_gain <= 0:
                    continue

                total_pl_db = pl_bs_ris + pl_ris_rx - 10 * math.log10(ris_array_gain)
                ris_path_rx_dbm = (p.tx_power_dbm + p.tx_gain_db + p.rx_gain_db - total_pl_db)
                ris_power_linear += 10 ** (ris_path_rx_dbm / 10)

        direct_linear = 10 ** (direct_rx_dbm / 10)
        total_linear = direct_linear + ris_power_linear
        total_rx_dbm = 10 * math.log10(max(total_linear, 1e-30))
        ris_rx_dbm = (10 * math.log10(max(ris_power_linear, 1e-30))
                      if ris_power_linear > 0 else None)

        sinr_db = total_rx_dbm - self._noise_dbm

        # PER approximation (sigmoid; documented simulation model)
        per = 1.0 / (1.0 + math.exp(p.per_k_slope * (sinr_db - p.per_sinr_threshold_db)))

        return LinkResult(
            direct_rx_dbm=round(direct_rx_dbm, 2),
            ris_rx_dbm=round(ris_rx_dbm, 2) if ris_rx_dbm is not None else None,
            total_rx_dbm=round(total_rx_dbm, 2),
            noise_dbm=round(self._noise_dbm, 2),
            sinr_db=round(sinr_db, 2),
            per=round(min(max(per, 0.0), 1.0), 4),
            is_nlos=is_nlos,
            shadow_db=round(shadow, 2),
        )

    def evaluate_zone_mean(self,
                           zone_id: str,
                           panel_registry: PanelRegistry,
                           zone_center_pos: np.ndarray,
                           bs_pos: np.ndarray,
                           is_nlos: bool = False,
                           n_samples: int = 5) -> LinkResult:
        """Evaluate mean link quality for a zone by sampling n receiver positions.

        Receiver positions are uniformly sampled within 20 m of zone_center_pos.
        All results are SIMULATED.
        """
        panels = panel_registry.by_zone(zone_id)
        geoms = [panel_registry.geometries[p.panel_id] for p in panels
                 if p.panel_id in panel_registry.geometries]
        active = list(zip(panels, geoms))

        results = []
        for _ in range(n_samples):
            offset = np.array([
                self._rng.uniform(-20, 20),
                self._rng.uniform(-20, 20),
                0.0,
            ])
            rx_pos = zone_center_pos + offset
            results.append(self.evaluate(rx_pos, bs_pos, active, is_nlos=is_nlos))

        # Average in linear domain for power, then convert
        mean_sinr = float(np.mean([r.sinr_db for r in results]))
        mean_per = float(np.mean([r.per for r in results]))
        mean_rx_dbm = float(np.mean([r.total_rx_dbm for r in results]))

        return LinkResult(
            direct_rx_dbm=round(float(np.mean([r.direct_rx_dbm for r in results])), 2),
            ris_rx_dbm=round(float(np.mean([r.ris_rx_dbm for r in results
                                             if r.ris_rx_dbm is not None])), 2)
                       if any(r.ris_rx_dbm is not None for r in results) else None,
            total_rx_dbm=round(mean_rx_dbm, 2),
            noise_dbm=round(self._noise_dbm, 2),
            sinr_db=round(mean_sinr, 2),
            per=round(mean_per, 4),
            is_nlos=is_nlos,
            shadow_db=0.0,
        )
