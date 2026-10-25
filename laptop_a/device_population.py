"""
device_population.py — Vectorised 50,000-Endpoint Stadium Device Manager

Represents the full logical device population using NumPy arrays for
memory efficiency and fast bulk operations.

Stadium layout:
  • 5 zones (Z1–Z5), each containing 10,000 endpoints.
  • Each zone has 5,000 traffic sources and 5,000 intended receivers.
  • Zone geometry models a stadium with distinct seating sections.

Integration points:
  → TrafficGenerator reads source device lists per zone.
  → ReceiverModel reads receiver positions, channel state, and mobility.
  → ReceiverFeedback uses device-to-zone mapping for aggregation.
  → Laptop B can query zone summaries via telemetry.
  → Laptop C uses zone geometry for 3D visualisation.
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
#  Zone geometry — models physical stadium sections
# ---------------------------------------------------------------------------

ZONE_NAMES = ["Z1_North", "Z2_East", "Z3_South", "Z4_West", "Z5_VIP"]

@dataclass
class ZoneGeometry:
    """Physical properties of one stadium zone."""
    zone_id: int
    name: str
    center_x: float          # metres — stadium-local coordinate
    center_y: float
    center_z: float          # elevation above pitch
    radius: float            # approximate zone extent in metres
    nlos_base_prob: float    # base probability of NLoS (0–1)
    extra_atten_db: float    # additional zone-specific attenuation (dB)

    def contains(self, x: float, y: float) -> bool:
        return ((x - self.center_x) ** 2 + (y - self.center_y) ** 2) <= self.radius ** 2


DEFAULT_ZONE_GEOMETRIES: List[ZoneGeometry] = [
    ZoneGeometry(0, "Z1_North",    0,  100, 15, 50, 0.30, 2.0),
    ZoneGeometry(1, "Z2_East",   100,    0, 20, 50, 0.40, 3.0),
    ZoneGeometry(2, "Z3_South",    0, -100, 15, 50, 0.30, 2.5),
    ZoneGeometry(3, "Z4_West",  -100,    0, 20, 50, 0.50, 4.0),
    ZoneGeometry(4, "Z5_VIP",     0,    0,  5, 30, 0.10, 0.5),
]

# BTS is positioned at the stadium centre, elevated
DEFAULT_BTS_POSITION = (0.0, 0.0, 40.0)

# Default 32 RIS panel positions (distributed along the stadium catwalk)
def _default_ris_panels(num_panels: int = 32) -> List[dict]:
    """Generate default RIS panel positions evenly around the catwalk ring."""
    panels = []
    catwalk_radius = 110.0   # metres from centre
    catwalk_z = 30.0         # elevation
    for i in range(num_panels):
        angle = 2.0 * np.pi * i / num_panels
        x = catwalk_radius * np.cos(angle)
        y = catwalk_radius * np.sin(angle)
        # Assign nearest two zones based on angle
        zone_angles = [np.pi / 2, 0, -np.pi / 2, np.pi]  # N, E, S, W
        dists = [abs(((angle - za + np.pi) % (2 * np.pi)) - np.pi) for za in zone_angles]
        sorted_zones = sorted(range(4), key=lambda zi: dists[zi])
        target_zones = sorted_zones[:2] + [4]  # Always include VIP zone
        panels.append({
            "panel_id": i,
            "position": (round(x, 2), round(y, 2), catwalk_z),
            "target_zones": target_zones,
            "current_phase": 0,       # codebook index (0 = neutral)
            "gain_db": 0.0,           # current effective gain
            "active": True,
        })
    return panels


# ---------------------------------------------------------------------------
#  Device Population
# ---------------------------------------------------------------------------

# Roles
ROLE_SOURCE = 0
ROLE_RECEIVER = 1

class DevicePopulation:
    """
    Vectorised device population manager.

    All per-device state is stored in contiguous NumPy arrays for
    cache-friendly bulk operations and minimal memory footprint.

    Args:
        total_devices:   Total logical endpoints (default 50,000).
        num_zones:       Number of stadium zones (default 5).
        devices_per_zone: Devices per zone (default total_devices // num_zones).
        zone_geometries: List[ZoneGeometry] — physical zone descriptions.
        bts_position:    (x, y, z) BTS location.
        ris_panels:      List[dict] — RIS panel definitions.
        rng_seed:        Reproducible randomness.
    """

    def __init__(
        self,
        total_devices: int = 50_000,
        num_zones: int = 5,
        devices_per_zone: Optional[int] = None,
        zone_geometries: Optional[List[ZoneGeometry]] = None,
        bts_position: Tuple[float, float, float] = DEFAULT_BTS_POSITION,
        ris_panels: Optional[List[dict]] = None,
        rng_seed: int = 42,
    ):
        self.num_zones = num_zones
        self.total_devices = total_devices
        self.devices_per_zone = devices_per_zone or (total_devices // num_zones)
        assert self.devices_per_zone * num_zones == total_devices, \
            "total_devices must be evenly divisible by num_zones"
        self.sources_per_zone = self.devices_per_zone // 2
        self.receivers_per_zone = self.devices_per_zone - self.sources_per_zone

        self.zones = zone_geometries or DEFAULT_ZONE_GEOMETRIES[:num_zones]
        self.bts_position = np.array(bts_position, dtype=np.float64)
        self.ris_panels = ris_panels or _default_ris_panels()
        self.rng = np.random.default_rng(rng_seed)

        # --- per-device arrays ---
        self.zone_ids    = np.empty(total_devices, dtype=np.int32)
        self.roles       = np.empty(total_devices, dtype=np.int8)
        self.pos_x       = np.empty(total_devices, dtype=np.float32)
        self.pos_y       = np.empty(total_devices, dtype=np.float32)
        self.pos_z       = np.empty(total_devices, dtype=np.float32)
        self.sinr_db     = np.full(total_devices, 20.0, dtype=np.float32)
        self.per         = np.full(total_devices, 0.01, dtype=np.float32)
        self.active      = np.ones(total_devices, dtype=np.bool_)
        self.seq_counters = np.zeros(total_devices, dtype=np.int64)

        # Mobility velocity (metres / second)
        self.vel_x = np.zeros(total_devices, dtype=np.float32)
        self.vel_y = np.zeros(total_devices, dtype=np.float32)
        self.mobility_enabled = False
        self.max_speed_mps = 1.5   # default walking speed

        self._init_devices()

        # Pre-computed index arrays for fast zone queries
        self._zone_source_idx: Dict[int, np.ndarray] = {}
        self._zone_receiver_idx: Dict[int, np.ndarray] = {}
        self._rebuild_zone_indices()

    # ----- initialisation -----

    def _init_devices(self) -> None:
        """Assign zones, roles, and initial positions."""
        for z in range(self.num_zones):
            start = z * self.devices_per_zone
            mid = start + self.sources_per_zone
            end = start + self.devices_per_zone
            geo = self.zones[z]

            self.zone_ids[start:end] = z

            # First half are sources, second half are receivers
            self.roles[start:mid] = ROLE_SOURCE
            self.roles[mid:end] = ROLE_RECEIVER

            # Scatter within zone radius
            n = self.devices_per_zone
            angles = self.rng.uniform(0, 2 * np.pi, n)
            radii = geo.radius * np.sqrt(self.rng.uniform(0, 1, n))
            self.pos_x[start:end] = geo.center_x + radii * np.cos(angles)
            self.pos_y[start:end] = geo.center_y + radii * np.sin(angles)
            self.pos_z[start:end] = geo.center_z + self.rng.uniform(-2, 2, n)

    def _rebuild_zone_indices(self) -> None:
        for z in range(self.num_zones):
            mask_z = self.zone_ids == z
            self._zone_source_idx[z] = np.where(mask_z & (self.roles == ROLE_SOURCE))[0]
            self._zone_receiver_idx[z] = np.where(mask_z & (self.roles == ROLE_RECEIVER))[0]

    # ----- zone / role queries -----

    def get_sources(self, zone_id: int) -> np.ndarray:
        """Return device indices of all sources in a zone."""
        return self._zone_source_idx[zone_id]

    def get_receivers(self, zone_id: int) -> np.ndarray:
        """Return device indices of all receivers in a zone."""
        return self._zone_receiver_idx[zone_id]

    def get_zone_devices(self, zone_id: int) -> np.ndarray:
        return np.where(self.zone_ids == zone_id)[0]

    def get_device_zone(self, device_id: int) -> int:
        return int(self.zone_ids[device_id])

    def get_device_role(self, device_id: int) -> int:
        return int(self.roles[device_id])

    def get_device_position(self, device_id: int) -> Tuple[float, float, float]:
        return (
            float(self.pos_x[device_id]),
            float(self.pos_y[device_id]),
            float(self.pos_z[device_id]),
        )

    def get_next_seq(self, device_id: int) -> int:
        """Atomically increment and return the next sequence number for a device."""
        seq = int(self.seq_counters[device_id])
        self.seq_counters[device_id] = seq + 1
        return seq

    # ----- bulk state updates (used by ReceiverModel & Laptop B RIS updates) -----

    def update_zone_sinr(self, zone_id: int, sinr_values: np.ndarray) -> None:
        """Overwrite SINR for all devices in a zone."""
        idx = self.get_zone_devices(zone_id)
        self.sinr_db[idx] = sinr_values[:len(idx)]

    def update_zone_per(self, zone_id: int, per_values: np.ndarray) -> None:
        idx = self.get_zone_devices(zone_id)
        self.per[idx] = per_values[:len(idx)]

    def set_zone_channel(self, zone_id: int, sinr_db: float, per: float) -> None:
        """Set uniform SINR and PER for every device in a zone."""
        idx = self.get_zone_devices(zone_id)
        self.sinr_db[idx] = sinr_db
        self.per[idx] = per

    # ----- mobility -----

    def enable_mobility(self, max_speed_mps: float = 1.5) -> None:
        """Enable random-walk mobility for all devices."""
        self.mobility_enabled = True
        self.max_speed_mps = max_speed_mps
        # Assign random initial velocities
        n = self.total_devices
        speed = self.rng.uniform(0, max_speed_mps, n).astype(np.float32)
        angle = self.rng.uniform(0, 2 * np.pi, n).astype(np.float32)
        self.vel_x = speed * np.cos(angle)
        self.vel_y = speed * np.sin(angle)

    def apply_mobility(self, dt: float) -> None:
        """
        Advance device positions by dt seconds.
        Devices that leave their zone are reflected back.
        """
        if not self.mobility_enabled:
            return

        self.pos_x += self.vel_x * dt
        self.pos_y += self.vel_y * dt

        # Reflect devices that drift outside their zone
        for z in range(self.num_zones):
            geo = self.zones[z]
            idx = self.get_zone_devices(z)
            dx = self.pos_x[idx] - geo.center_x
            dy = self.pos_y[idx] - geo.center_y
            dist_sq = dx * dx + dy * dy
            outside = dist_sq > geo.radius ** 2
            if np.any(outside):
                oi = idx[outside]
                self.vel_x[oi] *= -1
                self.vel_y[oi] *= -1
                self.pos_x[oi] -= self.vel_x[oi] * dt * 2
                self.pos_y[oi] -= self.vel_y[oi] * dt * 2

        # Periodically randomise velocity direction
        change = self.rng.random(self.total_devices) < (dt * 0.5)
        if np.any(change):
            ci = np.where(change)[0]
            angle = self.rng.uniform(0, 2 * np.pi, len(ci)).astype(np.float32)
            speed = np.sqrt(self.vel_x[ci] ** 2 + self.vel_y[ci] ** 2)
            self.vel_x[ci] = speed * np.cos(angle)
            self.vel_y[ci] = speed * np.sin(angle)

    # ----- summary (for telemetry / Laptop C) -----

    def get_zone_summary(self) -> Dict[str, dict]:
        """Return a per-zone summary dict suitable for telemetry."""
        summary = {}
        for z in range(self.num_zones):
            idx = self.get_zone_devices(z)
            src_count = int(np.sum(self.roles[idx] == ROLE_SOURCE))
            rcv_count = int(np.sum(self.roles[idx] == ROLE_RECEIVER))
            summary[f"Z{z + 1}"] = {
                "zone_name": self.zones[z].name,
                "total_devices": len(idx),
                "sources": src_count,
                "receivers": rcv_count,
                "active": int(np.sum(self.active[idx])),
                "mean_sinr_db": round(float(np.mean(self.sinr_db[idx])), 2),
                "mean_per": round(float(np.mean(self.per[idx])), 4),
            }
        return summary

    def get_full_config(self) -> dict:
        """Return a serialisable configuration dict (for Laptop C visualisation)."""
        return {
            "total_devices": self.total_devices,
            "num_zones": self.num_zones,
            "devices_per_zone": self.devices_per_zone,
            "sources_per_zone": self.sources_per_zone,
            "receivers_per_zone": self.receivers_per_zone,
            "bts_position": self.bts_position.tolist(),
            "ris_panels": self.ris_panels,
            "zones": [
                {
                    "zone_id": g.zone_id,
                    "name": g.name,
                    "center": (g.center_x, g.center_y, g.center_z),
                    "radius": g.radius,
                    "nlos_base_prob": g.nlos_base_prob,
                    "extra_atten_db": g.extra_atten_db,
                }
                for g in self.zones
            ],
            "mobility_enabled": self.mobility_enabled,
        }

    def memory_usage_bytes(self) -> int:
        """Approximate memory footprint of the device arrays."""
        total = 0
        for arr in [
            self.zone_ids, self.roles, self.pos_x, self.pos_y, self.pos_z,
            self.sinr_db, self.per, self.active, self.seq_counters,
            self.vel_x, self.vel_y,
        ]:
            total += arr.nbytes
        return total

    def __repr__(self) -> str:
        mb = self.memory_usage_bytes() / (1024 * 1024)
        return (
            f"DevicePopulation(devices={self.total_devices}, zones={self.num_zones}, "
            f"memory={mb:.1f} MB)"
        )
