"""Unit tests for D1: HistoryBuffer, Supervisor.tick(), Supervisor.get_view().

Tests verify:
  - HistoryBuffer is bounded: oldest samples are evicted when maxlen is exceeded.
  - None values (N/A metrics) are stored as-is and never replaced with fabricated data.
  - tick() is gated: repeated calls within the interval do not produce duplicate samples.
  - tick() records correct None when metrics are unavailable (no telemetry ingested).
  - get_view() returns independent copies: mutating the view does not affect supervisor.
  - get_view() snapshot keys match snapshot() keys.
  - get_view() history structure has expected zone keys.
"""

import copy
import time
import unittest

from common.constants import ZONE_IDS
from laptop_c.supervisor import HistoryBuffer, Supervisor


class TestHistoryBuffer(unittest.TestCase):

    def test_empty_on_creation(self):
        buf = HistoryBuffer(maxlen=10)
        self.assertEqual(len(buf), 0)
        self.assertEqual(buf.as_list(), [])

    def test_append_and_retrieve(self):
        buf = HistoryBuffer(maxlen=10)
        buf.append(42.0, ts=1.0)
        buf.append(None, ts=2.0)
        items = buf.as_list()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0], (1.0, 42.0))
        self.assertEqual(items[1], (2.0, None))

    def test_bounded_oldest_evicted(self):
        maxlen = 5
        buf = HistoryBuffer(maxlen=maxlen)
        for i in range(10):
            buf.append(float(i), ts=float(i))
        self.assertEqual(len(buf), maxlen)
        values = [v for _, v in buf.as_list()]
        self.assertEqual(values, [5.0, 6.0, 7.0, 8.0, 9.0])

    def test_none_values_preserved(self):
        buf = HistoryBuffer(maxlen=10)
        for _ in range(5):
            buf.append(None, ts=time.monotonic())
        self.assertTrue(all(v is None for _, v in buf.as_list()))

    def test_as_list_is_a_copy(self):
        buf = HistoryBuffer(maxlen=10)
        buf.append(1.0, ts=0.0)
        lst = buf.as_list()
        lst.clear()
        self.assertEqual(len(buf), 1)

    def test_maxlen_one(self):
        buf = HistoryBuffer(maxlen=1)
        buf.append(1.0, ts=1.0)
        buf.append(2.0, ts=2.0)
        self.assertEqual(len(buf), 1)
        self.assertEqual(buf.as_list()[0][1], 2.0)

    def test_invalid_maxlen_raises(self):
        with self.assertRaises(ValueError):
            HistoryBuffer(maxlen=0)

    def test_default_ts_uses_monotonic(self):
        buf = HistoryBuffer(maxlen=5)
        before = time.monotonic()
        buf.append(7.0)
        after = time.monotonic()
        ts, v = buf.as_list()[0]
        self.assertGreaterEqual(ts, before)
        self.assertLessEqual(ts, after)
        self.assertEqual(v, 7.0)


class TestSupervisorTick(unittest.TestCase):

    def _fresh(self):
        return Supervisor(run_id="d1-tick-test", random_seed=0)

    def test_tick_with_no_telemetry_stores_none(self):
        sup = self._fresh()
        sup._last_tick_mono = 0.0
        result = sup.tick(min_interval_s=0.0)
        self.assertTrue(result)
        _, v = sup._history_pdr.as_list()[-1]
        self.assertIsNone(v)

    def test_tick_records_zone_none_queue_when_no_telemetry(self):
        sup = self._fresh()
        sup._last_tick_mono = 0.0
        sup.tick(min_interval_s=0.0)
        for zid in ZONE_IDS:
            _, v = sup._history_queue_pct[zid].as_list()[-1]
            self.assertIsNone(v)

    def test_tick_gated_by_interval(self):
        sup = self._fresh()
        sup._last_tick_mono = 0.0
        sup.tick(min_interval_s=60.0)
        before_len = len(sup._history_pdr)
        result2 = sup.tick(min_interval_s=60.0)
        self.assertFalse(result2)
        self.assertEqual(len(sup._history_pdr), before_len)

    def test_tick_records_real_pdr(self):
        sup = self._fresh()
        payload = {
            "traffic_summary": {
                "uplink":   {"generated": 100, "delivered": 90, "dropped": 10, "pending": 0, "timed_out": 0},
                "downlink": {"generated": 100, "delivered": 95, "dropped":  5, "pending": 0, "timed_out": 0},
            }
        }
        sup.process_telemetry_a(payload, ts=time.time())
        sup._last_tick_mono = 0.0
        sup.tick(min_interval_s=0.0)
        _, v = sup._history_pdr.as_list()[-1]
        self.assertIsNotNone(v)
        self.assertAlmostEqual(v, 92.5, places=1)

    def test_tick_records_oob_none_when_no_commands(self):
        sup = self._fresh()
        sup._last_tick_mono = 0.0
        sup.tick(min_interval_s=0.0)
        _, v = sup._history_oob_ack_ratio.as_list()[-1]
        self.assertIsNone(v)

    def test_history_bounded_via_tick(self):
        sup = self._fresh()
        maxlen = sup.HISTORY_MAXLEN
        for _ in range(maxlen + 50):
            sup._last_tick_mono = 0.0
            sup.tick(min_interval_s=0.0)
        self.assertEqual(len(sup._history_pdr), maxlen)
        for zid in ZONE_IDS:
            self.assertEqual(len(sup._history_queue_pct[zid]), maxlen)
            self.assertEqual(len(sup._history_sinr[zid]), maxlen)
        self.assertEqual(len(sup._history_oob_ack_ratio), maxlen)


class TestSupervisorGetView(unittest.TestCase):

    def _fresh(self):
        return Supervisor(run_id="d1-view-test", random_seed=7)

    def test_get_view_structure(self):
        sup = self._fresh()
        view = sup.get_view()
        self.assertIn("snapshot", view)
        self.assertIn("recent_events", view)
        self.assertIn("history", view)

    def test_get_view_snapshot_has_required_keys(self):
        sup = self._fresh()
        snap = sup.get_view()["snapshot"]
        for key in ("run_id", "random_seed", "elapsed_s", "peer_health",
                    "pdu_summary", "overall_metrics", "zones", "panels",
                    "oob", "latest_decision", "latest_event"):
            self.assertIn(key, snap)

    def test_get_view_history_zone_keys(self):
        sup = self._fresh()
        hist = sup.get_view()["history"]
        for key in ("pdr", "queue_pct", "sinr", "oob_ack_ratio"):
            self.assertIn(key, hist)
        for zid in ZONE_IDS:
            self.assertIn(zid, hist["queue_pct"])
            self.assertIn(zid, hist["sinr"])

    def test_get_view_returns_copy_not_live_ref(self):
        sup = self._fresh()
        view = sup.get_view()
        view["snapshot"]["run_id"] = "CORRUPTED"
        view["snapshot"]["peer_health"]["A"] = "FAKE"
        self.assertEqual(sup.run_id, "d1-view-test")
        self.assertNotEqual(sup.peer_health.get("A"), "FAKE")

    def test_get_view_recent_events_copy(self):
        sup = self._fresh()
        sup.process_event({"event_type": "TEST_EVT"}, ts=time.time(), source="A")
        view = sup.get_view()
        view["recent_events"].clear()
        self.assertEqual(len(sup.recent_events), 1)

    def test_get_view_na_in_snapshot_is_none_not_string(self):
        sup = self._fresh()
        snap = sup.get_view()["snapshot"]
        self.assertIsNone(snap["overall_metrics"]["pdr_pct"])
        self.assertIsNone(snap["overall_metrics"]["plr_pct"])
        for zid in ZONE_IDS:
            zd = snap["zones"][zid]
            self.assertIsNone(zd["sinr_db"])
            self.assertIsNone(zd["pdr_pct"])

    def test_get_view_history_none_preserved(self):
        sup = self._fresh()
        sup._last_tick_mono = 0.0
        sup.tick(min_interval_s=0.0)
        hist = sup.get_view()["history"]
        _, v = hist["pdr"][-1]
        self.assertIsNone(v)
        for zid in ZONE_IDS:
            _, vq = hist["queue_pct"][zid][-1]
            _, vs = hist["sinr"][zid][-1]
            self.assertIsNone(vq)
            self.assertIsNone(vs)


if __name__ == "__main__":
    unittest.main()
