import asyncio, time, unittest
from common.config import load_network_config
from common.framing import encode_frame
from common.messages import make_message, encode_message
from laptop_c.telemetry_collector import TelemetryCollector, CONNECTED, STALE, DISCONNECTED, NEVER


def cfg(**kw):
    c = load_network_config(None, {"laptop_c": {"telemetry_port_a": 0, "telemetry_port_b": 0}})
    c.update(kw)
    return c


def msg(src="A", seq=0, run="r1", mid=None, mtype="TELEMETRY_A", **kw):
    return make_message(mtype, src, "C", run, {"mock": True}, seq=seq, message_id=mid, **kw)


class TestProcessing(unittest.TestCase):
    def setUp(self):
        self.col = TelemetryCollector(cfg())

    def test_accept_and_run_id_adoption(self):
        self.assertTrue(self.col.process_message("A", msg(seq=0)))
        self.assertEqual(self.col.run_id, "r1")
        self.assertEqual(self.col.peers["A"].accepted, 1)

    def test_duplicate_rejected(self):
        self.col.process_message("A", msg(seq=0, mid="dup"))
        self.assertFalse(self.col.process_message("A", msg(seq=1, mid="dup")))
        self.assertEqual(self.col.peers["A"].rejected_duplicate, 1)

    def test_stale_seq_and_gap(self):
        for s in (0, 1):
            self.col.process_message("A", msg(seq=s))
        self.assertFalse(self.col.process_message("A", msg(seq=1)))
        self.assertEqual(self.col.peers["A"].rejected_stale_seq, 1)
        self.assertTrue(self.col.process_message("A", msg(seq=5)))
        self.assertEqual(self.col.peers["A"].missing_seq, 3)

    def test_run_id_mismatch(self):
        self.col.process_message("A", msg(run="r1"))
        self.assertFalse(self.col.process_message("B", msg(src="B", run="r2", mtype="TELEMETRY_B")))
        self.assertEqual(self.col.peers["B"].run_id_mismatch, 1)

    def test_wrong_source_for_port(self):
        self.assertFalse(self.col.process_message("A", msg(src="B")))
        self.assertEqual(self.col.peers["A"].rejected_invalid, 1)

    def test_malformed_bytes_do_not_crash(self):
        for raw in (b"\xff\xfe", b"not json", b"[1]", b"{}"):
            self.assertFalse(self.col.process_raw("A", raw))
        self.assertEqual(self.col.peers["A"].rejected_invalid, 4)

    def test_dedupe_window_bounded(self):
        c = TelemetryCollector(cfg(dedupe_window=3))
        for i in range(10):
            c.process_message("A", msg(seq=i, mid=f"m{i}"))
        self.assertLessEqual(len(c.peers["A"]._seen_set), 3)

    def test_status_transitions(self):
        p = self.col.peers["A"]
        self.assertEqual(p.status(5), NEVER)
        p.connected = p.ever_connected = True
        self.assertEqual(p.status(5), STALE)  # connected but silent
        self.col.process_message("A", msg())
        self.assertEqual(p.status(5), CONNECTED)
        self.assertEqual(p.status(5, now=time.monotonic() + 10), STALE)
        p.connected = False
        self.assertEqual(p.status(5), DISCONNECTED)


class TestTCP(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_tcp_with_disconnect_and_reconnect(self):
        col = TelemetryCollector(cfg())
        await col.start()
        try:
            r, w = await asyncio.open_connection("127.0.0.1", col.ports["A"])
            # two messages combined in one write + one fragmented
            f1 = encode_frame(encode_message(msg(seq=0)))
            f2 = encode_frame(encode_message(msg(seq=1)))
            f3 = encode_frame(encode_message(msg(seq=2)))
            w.write(f1 + f2); await w.drain()
            w.write(f3[:5]); await w.drain(); await asyncio.sleep(0.05)
            w.write(f3[5:]); await w.drain()
            w.write(encode_frame(b"garbage")); await w.drain()
            await asyncio.sleep(0.2)
            p = col.peers["A"]
            self.assertEqual(p.accepted, 3)
            self.assertEqual(p.rejected_invalid, 1)
            self.assertEqual(col.health()["A"], CONNECTED)
            self.assertEqual(col.health()["B"], NEVER)
            w.close(); await asyncio.sleep(0.2)
            self.assertEqual(col.health()["A"], DISCONNECTED)
            self.assertEqual(len(p.disconnect_intervals), 1)
            self.assertIsNone(p.disconnect_intervals[0][1])
            # reconnect closes the recorded interval
            r, w = await asyncio.open_connection("127.0.0.1", col.ports["A"])
            w.write(encode_frame(encode_message(msg(seq=3)))); await w.drain()
            await asyncio.sleep(0.2)
            self.assertEqual(col.health()["A"], CONNECTED)
            self.assertIsNotNone(p.disconnect_intervals[0][1])
            w.close()
        finally:
            await col.stop()

    async def test_oversize_frame_closes_connection_only(self):
        c = cfg(); c["limits"]["max_message_bytes"] = 2048
        col = TelemetryCollector(c)
        await col.start()
        try:
            r, w = await asyncio.open_connection("127.0.0.1", col.ports["B"])
            w.write(encode_frame(b"x" * 5000, max_bytes=10_000)); await w.drain()
            await asyncio.sleep(0.2)
            self.assertEqual(col.health()["B"], DISCONNECTED)
            # collector still serves A
            r2, w2 = await asyncio.open_connection("127.0.0.1", col.ports["A"])
            w2.write(encode_frame(encode_message(msg()))); await w2.drain()
            await asyncio.sleep(0.2)
            self.assertEqual(col.peers["A"].accepted, 1)
            w2.close()
        finally:
            await col.stop()


if __name__ == "__main__":
    unittest.main()
