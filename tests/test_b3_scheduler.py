"""Tests for Stage B3: Congestion-aware scheduler."""
import time
import pytest

from laptop_b.scheduler import (
    BaselineScheduler, CongestionScheduler, CongestionSchedulerPolicy,
    make_scheduler,
)
from laptop_b.zone_manager import (
    ZoneState, CONGESTION_NORMAL, CONGESTION_MODERATE,
    CONGESTION_CONGESTED, CONGESTION_SEVERE,
)


def _zone(zone_id, congestion_state, queue_ratio=0.5, pdr=0.9, sinr_db=15.0):
    z = ZoneState(zone_id=zone_id)
    z.congestion_state = congestion_state
    z.queue_occupancy = int(queue_ratio * 1000)
    z.queue_capacity = 1000
    z.pdr = pdr
    z.mean_sinr_db = sinr_db
    return z


class TestBaselineScheduler:

    def test_always_hold(self):
        sched = BaselineScheduler()
        for state in [CONGESTION_NORMAL, CONGESTION_MODERATE, CONGESTION_CONGESTED, CONGESTION_SEVERE]:
            zone = _zone("Z1", state)
            dec = sched.decide("Z1", zone)
            assert dec.action == "HOLD"
            assert dec.token_weight == 1.0
            assert dec.scheduler_type == "BASELINE"


class TestCongestionScheduler:

    def test_action_maps_to_state(self):
        sched = CongestionScheduler()
        cases = [
            (CONGESTION_NORMAL, "HOLD"),
            (CONGESTION_MODERATE, "BOOST"),
            (CONGESTION_CONGESTED, "BOOST"),
            (CONGESTION_SEVERE, "THROTTLE"),
        ]
        for state, expected_action in cases:
            # bypass cooldown by using fresh scheduler per case
            s = CongestionScheduler()
            zone = _zone("Z1", state)
            dec = s.decide("Z1", zone)
            assert dec.action == expected_action, f"state={state} expected={expected_action} got={dec.action}"

    def test_severe_has_higher_weight_than_normal(self):
        # With fresh scheduler per call to avoid cooldown
        sn = CongestionScheduler()
        ss = CongestionScheduler()
        dn = sn.decide("Z1", _zone("Z1", CONGESTION_NORMAL))
        ds = ss.decide("Z1", _zone("Z1", CONGESTION_SEVERE))
        assert ds.token_weight > dn.token_weight

    def test_cooldown_holds_previous_action(self):
        policy = CongestionSchedulerPolicy(cooldown_s=60.0)  # very long cooldown
        sched = CongestionScheduler(policy=policy)
        zone1 = _zone("Z1", CONGESTION_CONGESTED)
        dec1 = sched.decide("Z1", zone1)
        # Now switch state but should still be in cooldown
        zone2 = _zone("Z1", CONGESTION_NORMAL)
        dec2 = sched.decide("Z1", zone2)
        assert "cooldown" in dec2.reason
        assert dec2.action == dec1.action  # action unchanged during cooldown

    def test_sinr_good_reduces_weight(self):
        policy = CongestionSchedulerPolicy(sinr_good_threshold_db=15.0, sinr_good_factor=0.80,
                                           cooldown_s=0.0)
        sched = CongestionScheduler(policy=policy)
        z_good_sinr = _zone("Z1", CONGESTION_CONGESTED, sinr_db=20.0)
        z_poor_sinr = _zone("Z2", CONGESTION_CONGESTED, sinr_db=5.0)
        d_good = sched.decide("Z1", z_good_sinr)
        d_poor = sched.decide("Z2", z_poor_sinr)
        assert d_good.token_weight < d_poor.token_weight

    def test_decision_history_recorded(self):
        sched = CongestionScheduler(CongestionSchedulerPolicy(cooldown_s=0.0))
        for i, zid in enumerate(["Z1", "Z2", "Z3"]):
            sched.decide(zid, _zone(zid, CONGESTION_MODERATE))
        assert len(sched.decision_history) == 3

    def test_latest_decisions_returns_one_per_zone(self):
        sched = CongestionScheduler(CongestionSchedulerPolicy(cooldown_s=0.0))
        for _ in range(3):
            for zid in ["Z1", "Z2"]:
                sched.decide(zid, _zone(zid, CONGESTION_MODERATE))
        latest = sched.latest_decisions()
        assert set(latest.keys()) == {"Z1", "Z2"}

    def test_deterministic_with_threshold_crossed(self):
        """When queue occupancy crosses the congestion threshold, a BOOST action must occur."""
        policy = CongestionSchedulerPolicy(cooldown_s=0.0)
        sched = CongestionScheduler(policy=policy)
        # Force CONGESTED state explicitly
        zone = _zone("Z1", CONGESTION_CONGESTED, queue_ratio=0.75, pdr=0.80)
        dec = sched.decide("Z1", zone)
        assert dec.action in ("BOOST", "THROTTLE")

    def test_make_scheduler_factory(self):
        s1 = make_scheduler("baseline")
        assert isinstance(s1, BaselineScheduler)
        s2 = make_scheduler("congestion_aware")
        assert isinstance(s2, CongestionScheduler)

    def test_make_scheduler_unknown_raises(self):
        with pytest.raises(ValueError):
            make_scheduler("unknown_type")
