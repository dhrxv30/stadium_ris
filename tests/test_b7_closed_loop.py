"""Integration test: full closed-loop feedback → schedule → optimize → OOB → evaluate (B7)."""
import asyncio
import time
import pytest

from laptop_b.zone_manager import (
    ZoneManager, CongestionPolicy, CONGESTION_CONGESTED,
)
from laptop_b.ris_channel import PanelRegistry, ChannelModelParams
from laptop_b.ris_optimizer import RISOptimizer, OptimizerPolicy
from laptop_b.oob_controller import OOBController, OOBChannelParams
from laptop_b.scheduler import CongestionScheduler, CongestionSchedulerPolicy
from laptop_b.closed_loop import ClosedLoopEngine, _DEFAULT_ZONE_CENTERS, _DEFAULT_NLOS
from common.constants import ZONE_IDS, MSG_FEEDBACK_REPORT
from common.messages import make_message
import numpy as np


def _build_all(loss_prob=0.0, cooldown_s=0.0, optimize_interval_s=0.05):
    """Build a fully wired closed-loop system for testing."""
    zone_ids = ZONE_IDS
    zones = ZoneManager(zone_ids, stale_after_s=30.0,
                        congestion_policy=CongestionPolicy(hysteresis_up=1, hysteresis_down=1))
    panels = PanelRegistry.build(zone_ids, [7, 7, 6, 6, 6], initial_codebook_index=0)
    scheduler = CongestionScheduler(CongestionSchedulerPolicy(cooldown_s=0.0))
    params = OOBChannelParams(delay_s=0.005, loss_probability=loss_prob,
                              retry_limit=2, timeout_s=0.5, validity_window_s=30.0)
    oob = OOBController(params=params, seed=42)
    optimizer = RISOptimizer(
        channel_params=ChannelModelParams(shadow_std_db=0.0),
        policy=OptimizerPolicy(cooldown_s=cooldown_s, min_sinr_gain_db=0.0, n_channel_samples=3),
        codebook_size=8,
        zone_centers=_DEFAULT_ZONE_CENTERS,
        zone_nlos_flags=_DEFAULT_NLOS,
        bs_pos=np.array([0.0, 0.0, 10.0]),
        seed=42,
    )
    engine = ClosedLoopEngine(
        zone_manager=zones,
        scheduler=scheduler,
        panel_registry=panels,
        optimizer=optimizer,
        oob_controller=oob,
        closed_loop_enabled=True,
        optimize_interval_s=optimize_interval_s,
    )
    return zones, panels, scheduler, oob, optimizer, engine


def _inject_congested_zone(zones, zone_id="Z3"):
    """Inject a congested feedback report into a zone."""
    payload = {
        "seq": 1,
        "window_start": time.time() - 1.0,
        "window_end": time.time(),
        "queue_occupancy": 950,
        "queue_capacity": 1000,
        "generated": 100,
        "delivered": 50,
        "dropped": 50,
        "mean_sinr_db": 4.0,  # poor SINR
        "per": 0.50,
        "traffic_rate_pps": 100.0,
    }
    msg = make_message(MSG_FEEDBACK_REPORT, "A", "B", "integration-run", payload, zone_id=zone_id)
    return zones.process_feedback(msg)


class TestB7ClosedLoop:

    @pytest.mark.asyncio
    async def test_congestion_triggers_optimization(self):
        """Congested zone should trigger the optimizer."""
        zones, panels, scheduler, oob, optimizer, engine = _build_all()
        status, _ = _inject_congested_zone(zones, "Z3")
        assert status == "ACCEPTED"
        assert "Z3" in zones.zones_needing_action()

        # Run one closed-loop iteration
        await engine._iterate()
        # Optimizer should have produced at least one result
        results = optimizer.latest_results()
        assert "Z3" in results


    @pytest.mark.asyncio
    async def test_zero_loss_oob_produces_ack(self):
        """With zero loss, if the optimizer decides APPLY, the OOB exchange must ACK.

        NO_CHANGE is a valid outcome if the simulated SINR gain is below threshold;
        in that case no OOB command should be issued (correct behaviour).
        """
        zones, panels, scheduler, oob, optimizer, engine = _build_all(loss_prob=0.0)
        _inject_congested_zone(zones, "Z3")

        await engine._iterate()
        await asyncio.sleep(0.5)

        results = optimizer.latest_results()
        assert "Z3" in results, "Optimizer must have evaluated Z3 when it was congested"
        r = results["Z3"]
        if r.decision == "APPLY":
            assert oob.stats["commands_issued"] >= 1
            assert oob.stats["commands_acked"] >= 1
        # If NO_CHANGE: correct outcome — no OOB command should be issued
        else:
            assert oob.stats["commands_issued"] == 0, \
                "NO_CHANGE decision must not produce an OOB command"


    @pytest.mark.asyncio
    async def test_post_update_evaluated(self):
        """After an ACK, the optimizer evaluates post-update channel quality."""
        zones, panels, scheduler, oob, optimizer, engine = _build_all(loss_prob=0.0)
        _inject_congested_zone(zones, "Z3")

        await engine._iterate()
        await asyncio.sleep(0.5)   # allow ACK to process

        results = optimizer.latest_results()
        if results.get("Z3") and results["Z3"].decision == "APPLY":
            r = results["Z3"]
            # post_sinr_db should be set after evaluation
            assert r.post_sinr_db is not None
            # improved flag should be True or False (not None) after post-evaluation
            assert r.improved is not None

    @pytest.mark.asyncio
    async def test_full_loss_does_not_apply_config(self):
        """With 100% OOB loss, no config should be applied to any panel."""
        zones, panels, scheduler, oob, optimizer, engine = _build_all(
            loss_prob=1.0, optimize_interval_s=0.05
        )
        # Store initial codebook indices
        initial_cbs = {pid: p.codebook_index for pid, p in panels.panels.items()}
        _inject_congested_zone(zones, "Z3")

        await engine._iterate()
        await asyncio.sleep(0.5)

        # With 100% loss, no panel should have changed codebook
        for pid, p in panels.panels.items():
            if p.zone_id == "Z3":
                assert p.codebook_index == initial_cbs[pid], \
                    f"Panel {pid} codebook changed despite 100% OOB loss"

    @pytest.mark.asyncio
    async def test_no_action_when_no_congestion(self):
        """Without congestion, closed-loop should not issue OOB commands."""
        zones, panels, scheduler, oob, optimizer, engine = _build_all()
        # Inject NORMAL zone traffic
        payload = {
            "seq": 1, "window_start": time.time() - 1, "window_end": time.time(),
            "queue_occupancy": 100, "queue_capacity": 1000,
            "generated": 100, "delivered": 99, "dropped": 1,
        }
        msg = make_message(MSG_FEEDBACK_REPORT, "A", "B", "r1", payload, zone_id="Z1")
        zones.process_feedback(msg)

        await engine._iterate()
        await asyncio.sleep(0.1)

        assert oob.stats["commands_issued"] == 0
