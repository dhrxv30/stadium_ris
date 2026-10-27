"""Tests for Stage B2: Zone manager congestion classification and hysteresis."""
import time
import pytest

from laptop_b.zone_manager import (
    ZoneManager, CongestionPolicy, ZoneState,
    ACCEPTED, REJ_DUPLICATE, REJ_STALE, REJ_UNKNOWN_ZONE,
    CONGESTION_NORMAL, CONGESTION_MODERATE, CONGESTION_CONGESTED,
    CONGESTION_SEVERE, CONGESTION_UNKNOWN,
)
from common.messages import make_message
from common.constants import MSG_FEEDBACK_REPORT, ZONE_IDS


def _make_feedback(zone_id, seq, queue_occupancy=100, queue_capacity=1000,
                   generated=200, delivered=190, dropped=10,
                   sinr_db=15.0, per=0.05, run_id="test-run"):
    payload = {
        "seq": seq,
        "window_start": time.time() - 1.0,
        "window_end": time.time(),
        "queue_occupancy": queue_occupancy,
        "queue_capacity": queue_capacity,
        "generated": generated,
        "delivered": delivered,
        "dropped": dropped,
        "mean_sinr_db": sinr_db,
        "per": per,
        "traffic_rate_pps": float(generated),
    }
    return make_message(MSG_FEEDBACK_REPORT, "A", "B", run_id, payload, zone_id=zone_id)


def _make_manager():
    return ZoneManager(ZONE_IDS, stale_after_s=5.0)


class TestB2ZoneManager:

    def test_accepts_valid_report(self):
        zm = _make_manager()
        msg = _make_feedback("Z1", seq=1)
        status, reason = zm.process_feedback(msg)
        assert status == ACCEPTED
        assert zm.zones["Z1"].reports_accepted == 1
        assert zm.zones["Z1"].last_seq == 1

    def test_rejects_unknown_zone(self):
        zm = _make_manager()
        msg = _make_feedback("Z1", seq=1)
        msg["zone_id"] = "Z9"
        status, reason = zm.process_feedback(msg)
        assert status == REJ_UNKNOWN_ZONE

    def test_rejects_duplicate_msg_id(self):
        zm = _make_manager()
        msg = _make_feedback("Z1", seq=1)
        zm.process_feedback(msg)
        status, reason = zm.process_feedback(msg)   # same msg_id
        assert status == REJ_DUPLICATE

    def test_rejects_stale_window_end(self):
        zm = _make_manager()
        msg = _make_feedback("Z1", seq=1)
        msg["payload"]["window_end"] = time.time() - 100.0   # old
        msg["payload"]["window_start"] = time.time() - 101.0
        status, reason = zm.process_feedback(msg)
        assert status == REJ_STALE

    def test_rejects_decreasing_seq(self):
        zm = _make_manager()
        zm.process_feedback(_make_feedback("Z1", seq=5))
        status, reason = zm.process_feedback(_make_feedback("Z1", seq=3))
        assert status == REJ_STALE

    def test_cumulative_counters(self):
        zm = _make_manager()
        for i in range(1, 4):
            msg = _make_feedback("Z2", seq=i, generated=100, delivered=90, dropped=10)
            zm.process_feedback(msg)
        z = zm.zones["Z2"]
        assert z.total_generated == 300
        assert z.total_delivered == 270
        assert z.total_dropped == 30
        assert z.cumulative_pdr() == pytest.approx(270 / 300)

    def test_pdr_derived_correctly(self):
        zm = _make_manager()
        msg = _make_feedback("Z3", seq=1, delivered=80, dropped=20)
        zm.process_feedback(msg)
        assert zm.zones["Z3"].pdr == pytest.approx(80 / 100)

    def test_pdr_excludes_generated_but_pending_packets(self):
        zm = _make_manager()
        msg = _make_feedback(
            "Z4",
            seq=1,
            generated=100,
            delivered=45,
            dropped=5,
        )

        zm.process_feedback(msg)

        assert zm.zones["Z4"].pdr == pytest.approx(45 / 50)

    def test_history_appended(self):
        zm = _make_manager()
        for i in range(1, 4):
            zm.process_feedback(_make_feedback("Z1", seq=i))
        assert len(zm.zones["Z1"].history) == 3

    def test_history_max_capped(self):
        zm = ZoneManager(ZONE_IDS, stale_after_s=5.0, max_history=5)
        for i in range(1, 10):
            zm.process_feedback(_make_feedback("Z1", seq=i))
        assert len(zm.zones["Z1"].history) <= 5

    def test_global_counters(self):
        zm = _make_manager()
        for i in range(1, 4):
            zm.process_feedback(_make_feedback("Z1", seq=i))
        assert zm.total_accepted == 3
        assert zm.total_feedback_received == 3


class TestB2CongestionClassification:

    def _classify_with_queue_ratio(self, qr, pdr=None):
        """Helper: create a zone with given queue ratio and classify it."""
        policy = CongestionPolicy(hysteresis_up=1, hysteresis_down=1)
        zm = ZoneManager(["Z1"], stale_after_s=5.0, congestion_policy=policy)
        qo = int(qr * 1000)
        delivered = int(pdr * 100) if pdr is not None else 100
        payload = {
            "seq": 1, "window_start": time.time() - 1, "window_end": time.time(),
            "queue_occupancy": qo, "queue_capacity": 1000,
            "generated": 100, "delivered": delivered,
            "dropped": 100 - delivered,
        }
        from common.messages import make_message
        from common.constants import MSG_FEEDBACK_REPORT
        msg = make_message(MSG_FEEDBACK_REPORT, "A", "B", "r1", payload, zone_id="Z1")
        zm.process_feedback(msg)
        return zm.zones["Z1"].congestion_state

    def test_normal_queue(self):
        assert self._classify_with_queue_ratio(0.20) == CONGESTION_NORMAL

    def test_moderate_queue(self):
        assert self._classify_with_queue_ratio(0.55) == CONGESTION_MODERATE

    def test_congested_queue(self):
        assert self._classify_with_queue_ratio(0.80) == CONGESTION_CONGESTED

    def test_severe_queue(self):
        assert self._classify_with_queue_ratio(0.95) == CONGESTION_SEVERE

    def test_severe_from_poor_pdr(self):
        """PDR below severe floor should force SEVERE regardless of queue."""
        state = self._classify_with_queue_ratio(0.20, pdr=0.40)
        assert state == CONGESTION_SEVERE

    def test_hysteresis_prevents_immediate_transition(self):
        """With hysteresis_up=2, one sample in CONGESTED should not yet transition."""
        policy = CongestionPolicy(hysteresis_up=2, hysteresis_down=3)
        zm = ZoneManager(["Z1"], stale_after_s=5.0, congestion_policy=policy)
        # First send NORMAL report to establish state
        for i, (qo, exp) in enumerate([
            (100, CONGESTION_NORMAL),    # seq 1: normal, state stays UNKNOWN→NORMAL
        ], start=1):
            payload = {
                "seq": i, "window_start": time.time() - 1, "window_end": time.time(),
                "queue_occupancy": qo, "queue_capacity": 1000,
                "generated": 100, "delivered": 90, "dropped": 10,
            }
            from common.messages import make_message
            from common.constants import MSG_FEEDBACK_REPORT
            msg = make_message(MSG_FEEDBACK_REPORT, "A", "B", "r1", payload, zone_id="Z1")
            zm.process_feedback(msg)

        # Now send one CONGESTED report — should NOT yet be CONGESTED (need 2)
        payload = {
            "seq": 2, "window_start": time.time() - 1, "window_end": time.time(),
            "queue_occupancy": 850, "queue_capacity": 1000,
            "generated": 100, "delivered": 90, "dropped": 10,
        }
        from common.messages import make_message
        from common.constants import MSG_FEEDBACK_REPORT
        msg = make_message(MSG_FEEDBACK_REPORT, "A", "B", "r1", payload, zone_id="Z1")
        zm.process_feedback(msg)
        # After 1 sample of CONGESTED, state should still be NORMAL
        assert zm.zones["Z1"].congestion_state != CONGESTION_CONGESTED

    def test_zones_needing_action(self):
        policy = CongestionPolicy(hysteresis_up=1, hysteresis_down=1)
        zm = ZoneManager(ZONE_IDS, stale_after_s=5.0, congestion_policy=policy)
        # Make Z3 congested
        payload = {
            "seq": 1, "window_start": time.time() - 1, "window_end": time.time(),
            "queue_occupancy": 900, "queue_capacity": 1000,
            "generated": 100, "delivered": 90, "dropped": 10,
        }
        from common.messages import make_message
        from common.constants import MSG_FEEDBACK_REPORT
        msg = make_message(MSG_FEEDBACK_REPORT, "A", "B", "r1", payload, zone_id="Z3")
        zm.process_feedback(msg)
        assert "Z3" in zm.zones_needing_action()

    def test_small_pdr_loss_escalates_to_actionable_congestion(self):
        zm = _make_manager()
        for seq, delivered, dropped in (
            (1, 994, 6),
            (2, 994, 6),
            (3, 979, 21),
            (4, 979, 21),
        ):
            msg = _make_feedback(
                "Z3",
                seq=seq,
                queue_occupancy=100,
                queue_capacity=1000,
                generated=1000,
                delivered=delivered,
                dropped=dropped,
            )
            assert zm.process_feedback(msg)[0] == ACCEPTED

            if seq == 2:
                assert zm.zones["Z3"].congestion_state == CONGESTION_MODERATE
                assert "Z3" not in zm.zones_needing_action()

        assert zm.zones["Z3"].congestion_state == CONGESTION_CONGESTED
        assert "Z3" in zm.zones_needing_action()
