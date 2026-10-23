"""
traffic_generator.py — Event-Driven PDU Generation Engine

Generates uplink Protocol Data Units (PDUs) for all 25,000 traffic sources
across 5 stadium zones using configurable traffic profiles.

Supported traffic profiles:
  • CBR    — Constant Bit Rate: fixed inter-arrival time.
  • POISSON — Poisson arrivals: exponentially distributed intervals.
  • BURST  — Periodic burst: concentrated packets over a short window.
  • CONGESTION — Sustained overload: rate exceeds zone capacity.

Each zone can run a different profile simultaneously.  A global rate
multiplier allows system-wide load scaling for stress testing.

PDU schema:
  Every generated PDU is a plain dict with all fields required by the
  master specification (§3.2):
    pdu_id, seq_num, src_device, dst_device, src_zone, dst_zone,
    direction, priority, service_class, payload_size, gen_time, status.

Integration points:
  → DevicePopulation provides zone-partitioned source/receiver lists.
  → PDUGateway enqueues the generated PDUs.
  → MetricsRegistry records generation counts.
  → Laptop B receives UL PDU batches via the data-plane TCP link.
  → Laptop C can override profiles via supervisory commands.
"""

import time
import math
import numpy as np
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    from device_population import DevicePopulation, ROLE_SOURCE, ROLE_RECEIVER
except ImportError:
    from laptop_a.device_population import DevicePopulation, ROLE_SOURCE, ROLE_RECEIVER


# ---------------------------------------------------------------------------
#  Traffic profiles
# ---------------------------------------------------------------------------

class ProfileType(str, Enum):
    CBR        = "cbr"
    POISSON    = "poisson"
    BURST      = "burst"
    CONGESTION = "congestion"


SERVICE_CLASSES = ["realtime", "streaming", "interactive", "background"]

# Priority weights for random assignment (higher number = more of that class)
DEFAULT_PRIORITY_WEIGHTS = {
    0: 0.05,   # critical  (5 %)
    1: 0.15,   # high      (15 %)
    2: 0.50,   # normal    (50 %)
    3: 0.30,   # best-effort (30 %)
}


@dataclass
class TrafficProfile:
    """
    Configurable traffic-generation profile for one zone.

    rate_pps:        Target packet rate (packets per second) for ALL sources
                     in the zone combined.
    profile_type:    One of ProfileType values.
    payload_min:     Minimum payload size (bytes).
    payload_max:     Maximum payload size (bytes).
    burst_duration:  Duration of each burst window (seconds) — BURST only.
    burst_interval:  Time between burst starts (seconds) — BURST only.
    burst_multiplier: Rate multiplier during burst — BURST only.
    """
    rate_pps: float = 5000.0
    profile_type: ProfileType = ProfileType.CBR
    payload_min: int = 64
    payload_max: int = 1500
    burst_duration: float = 0.5
    burst_interval: float = 5.0
    burst_multiplier: float = 10.0


# ---------------------------------------------------------------------------
#  Default profiles for quick scenario selection
# ---------------------------------------------------------------------------

PRESET_PROFILES: Dict[str, Dict[int, TrafficProfile]] = {
    "light": {z: TrafficProfile(rate_pps=1000, profile_type=ProfileType.POISSON) for z in range(5)},
    "normal": {z: TrafficProfile(rate_pps=5000, profile_type=ProfileType.CBR) for z in range(5)},
    "heavy": {z: TrafficProfile(rate_pps=15000, profile_type=ProfileType.CBR) for z in range(5)},
    "burst_z2": {
        **{z: TrafficProfile(rate_pps=5000, profile_type=ProfileType.CBR) for z in range(5)},
        1: TrafficProfile(rate_pps=5000, profile_type=ProfileType.BURST,
                          burst_duration=0.5, burst_interval=3.0, burst_multiplier=15.0),
    },
    "congestion_z4": {
        **{z: TrafficProfile(rate_pps=5000, profile_type=ProfileType.CBR) for z in range(5)},
        3: TrafficProfile(rate_pps=25000, profile_type=ProfileType.CONGESTION),
    },
    "uneven": {
        0: TrafficProfile(rate_pps=3000, profile_type=ProfileType.POISSON),
        1: TrafficProfile(rate_pps=12000, profile_type=ProfileType.CBR),
        2: TrafficProfile(rate_pps=5000, profile_type=ProfileType.CBR),
        3: TrafficProfile(rate_pps=20000, profile_type=ProfileType.CONGESTION),
        4: TrafficProfile(rate_pps=2000, profile_type=ProfileType.POISSON),
    },
}


# ---------------------------------------------------------------------------
#  Traffic Generator
# ---------------------------------------------------------------------------

class TrafficGenerator:
    """
    High-throughput, tick-driven traffic generator.

    Usage:
        gen = TrafficGenerator(population, profiles=PRESET_PROFILES["normal"])
        pdus = gen.generate_tick(sim_time=1.0, dt=0.1)

    Each call to generate_tick() returns a list of PDU dicts for one tick.
    """

    def __init__(
        self,
        population: DevicePopulation,
        profiles: Optional[Dict[int, TrafficProfile]] = None,
        rate_multiplier: float = 1.0,
        rng_seed: int = 123,
    ):
        self.population = population
        self.num_zones = population.num_zones
        self.profiles: Dict[int, TrafficProfile] = profiles or PRESET_PROFILES["normal"]
        self.rate_multiplier = rate_multiplier
        self.rng = np.random.default_rng(rng_seed)

        # Global PDU counter (monotonically increasing)
        self._pdu_counter: int = 0

        # Per-zone fractional accumulator for sub-packet ticks
        self._frac_accum: Dict[int, float] = {z: 0.0 for z in range(self.num_zones)}

        # Statistics
        self.stats_generated: int = 0
        self.stats_per_zone: Dict[int, int] = {z: 0 for z in range(self.num_zones)}

    # ----- profile management (runtime hot-swap) -----

    def set_zone_profile(self, zone_id: int, profile: TrafficProfile) -> None:
        """Hot-swap the traffic profile for a single zone."""
        self.profiles[zone_id] = profile

    def set_all_profiles(self, profiles: Dict[int, TrafficProfile]) -> None:
        self.profiles = profiles

    def set_rate_multiplier(self, multiplier: float) -> None:
        """Global rate scaling (1.0 = nominal, 2.0 = double load, etc.)."""
        self.rate_multiplier = max(0.0, multiplier)

    def load_preset(self, preset_name: str) -> None:
        """Load a named preset profile set."""
        if preset_name in PRESET_PROFILES:
            self.profiles = {z: p for z, p in PRESET_PROFILES[preset_name].items()}
            self._frac_accum = {z: 0.0 for z in range(self.num_zones)}
        else:
            raise ValueError(f"Unknown preset: {preset_name}. "
                             f"Available: {list(PRESET_PROFILES.keys())}")

    # ----- tick-level generation -----

    def generate_tick(self, sim_time: float, dt: float) -> List[dict]:
        """
        Generate all PDUs for one simulation tick.

        Args:
            sim_time: Current simulation time (seconds).
            dt:       Tick duration (seconds).

        Returns:
            List of PDU dicts.
        """
        all_pdus: List[dict] = []

        for z in range(self.num_zones):
            profile = self.profiles.get(z)
            if profile is None:
                continue

            effective_rate = self._effective_rate(profile, sim_time) * self.rate_multiplier
            n_float = effective_rate * dt + self._frac_accum[z]
            n_pdus = int(n_float)
            self._frac_accum[z] = n_float - n_pdus

            if n_pdus <= 0:
                continue

            pdus = self._generate_zone_pdus(z, n_pdus, profile, sim_time)
            all_pdus.extend(pdus)

        self.stats_generated += len(all_pdus)
        return all_pdus

    # ----- internal -----

    def _effective_rate(self, profile: TrafficProfile, sim_time: float) -> float:
        """Compute the instantaneous rate for a profile at the given sim_time."""
        base = profile.rate_pps

        if profile.profile_type == ProfileType.CBR:
            return base

        elif profile.profile_type == ProfileType.POISSON:
            return base   # Poisson — same mean rate; randomness is in source selection

        elif profile.profile_type == ProfileType.BURST:
            # Determine if we're inside a burst window
            cycle_pos = sim_time % profile.burst_interval
            if cycle_pos < profile.burst_duration:
                return base * profile.burst_multiplier
            return base * 0.2   # low inter-burst rate

        elif profile.profile_type == ProfileType.CONGESTION:
            return base   # sustained overload — let the gateway handle drops

        return base

    def _generate_zone_pdus(
        self,
        zone_id: int,
        count: int,
        profile: TrafficProfile,
        sim_time: float,
    ) -> List[dict]:
        """Generate `count` PDUs for a zone by randomly selecting sources and receivers."""
        sources = self.population.get_sources(zone_id)
        receivers = self.population.get_receivers(zone_id)

        if len(sources) == 0 or len(receivers) == 0:
            return []

        # Randomly select sources (with replacement for high counts)
        src_indices = self.rng.choice(sources, size=count, replace=True)
        dst_indices = self.rng.choice(receivers, size=count, replace=True)

        # Random payload sizes
        payload_sizes = self.rng.integers(
            profile.payload_min, profile.payload_max + 1, size=count
        )

        # Priority distribution
        priorities = self.rng.choice(
            list(DEFAULT_PRIORITY_WEIGHTS.keys()),
            size=count,
            p=list(DEFAULT_PRIORITY_WEIGHTS.values()),
        )

        # Service class (correlated with priority)
        svc_map = {0: "realtime", 1: "streaming", 2: "interactive", 3: "background"}

        pdus = []
        for i in range(count):
            src_dev = int(src_indices[i])
            dst_dev = int(dst_indices[i])
            prio = int(priorities[i])
            pdu_id = self._pdu_counter
            self._pdu_counter += 1
            seq = self.population.get_next_seq(src_dev)

            pdu = {
                "pdu_id": pdu_id,
                "seq_num": seq,
                "src_device": src_dev,
                "dst_device": dst_dev,
                "src_zone": zone_id,
                "dst_zone": zone_id,          # intra-zone by default
                "direction": "UL",
                "priority": prio,
                "service_class": svc_map.get(prio, "background"),
                "payload_size": int(payload_sizes[i]),
                "gen_time": sim_time,
                "status": "generated",
            }
            pdus.append(pdu)

        self.stats_per_zone[zone_id] = self.stats_per_zone.get(zone_id, 0) + count
        return pdus

    # ----- cross-zone traffic -----

    def generate_cross_zone_traffic(
        self, sim_time: float, fraction: float = 0.05
    ) -> List[dict]:
        """
        Optionally generate a fraction of traffic destined for OTHER zones
        (e.g., external sources or cross-zone mobility).
        """
        cross_pdus: List[dict] = []
        for z in range(self.num_zones):
            sources = self.population.get_sources(z)
            count = max(1, int(len(sources) * fraction))
            src_indices = self.rng.choice(sources, size=count, replace=True)

            other_zones = [oz for oz in range(self.num_zones) if oz != z]
            for src_dev in src_indices:
                dst_zone = int(self.rng.choice(other_zones))
                receivers = self.population.get_receivers(dst_zone)
                if len(receivers) == 0:
                    continue
                dst_dev = int(self.rng.choice(receivers))
                pdu_id = self._pdu_counter
                self._pdu_counter += 1

                pdu = {
                    "pdu_id": pdu_id,
                    "seq_num": self.population.get_next_seq(int(src_dev)),
                    "src_device": int(src_dev),
                    "dst_device": dst_dev,
                    "src_zone": z,
                    "dst_zone": dst_zone,
                    "direction": "UL",
                    "priority": 2,
                    "service_class": "interactive",
                    "payload_size": int(self.rng.integers(128, 1024)),
                    "gen_time": sim_time,
                    "status": "generated",
                }
                cross_pdus.append(pdu)

        self.stats_generated += len(cross_pdus)
        return cross_pdus

    # ----- statistics -----

    def get_stats(self) -> dict:
        return {
            "total_generated": self.stats_generated,
            "per_zone": dict(self.stats_per_zone),
            "rate_multiplier": self.rate_multiplier,
            "pdu_counter": self._pdu_counter,
            "profiles": {
                z: {
                    "type": p.profile_type.value,
                    "rate_pps": p.rate_pps,
                }
                for z, p in self.profiles.items()
            },
        }

    def reset_stats(self) -> None:
        self.stats_generated = 0
        self.stats_per_zone = {z: 0 for z in range(self.num_zones)}
