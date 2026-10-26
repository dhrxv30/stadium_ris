"""Unit and integration tests for Laptop C Supervisor State Model (Stage C2)."""
import asyncio
import time
import unittest

from common.constants import (
    LAPTOP_A,
    LAPTOP_B,
    LAPTOP_C,
    MSG_EVENT,
    MSG_TELEMETRY_A,
    MSG_TELEMETRY_B,
    ZONE_IDS,
)
from common.framing import encode_frame
from common.telemetry_messages import encode_message, make_message
from common.schemas import (
    make_default_device_population,
    make_default_panel_catalog,
    validate_event_payload,
    validate_telemetry_a_payload,
    validate_telemetry_b_payload,
)
from laptop_c.supervisor import Supervisor, fmt_val
from laptop_c.telemetry_collector import TelemetryCollector


class TestSupervisorState(unittest.TestCase):
    def test_default_supervisor_initialization(self):
        sup = Supervisor(run_id="run-test-init", random_seed=42)
        self.assertEqual(sup.run_id, "run-test-init")
        self.assertEqual(sup.random_seed, 42)

        # 50,000 devices across 5 zones
        self.assertEqual(sup.device_population["total"], 50000)
        self.assertEqual(sup.device_population["senders"], 25000)
        self.assertEqual(sup.device_population["receivers"], 25000)

        for zid in ZONE_IDS:
            zinfo = sup.device_population["per_zone"][zid]
            self.assertEqual(zinfo["senders"], 5000)
            self.assertEqual(zinfo["receivers"], 5000)
            self.assertEqual(zinfo["total"], 10000)

        # 5 zones initialized in supervisor
        self.assertEqual(len(sup.zones), 5)
        for zid in ZONE_IDS:
            z = sup.zones[zid]
            self.assertEqual(z.zone_id, zid)
            self.assertEqual(z.congestion_state, "NORMAL")
            self.assertIsNone(z.pdr)
            self.assertIsNone(z.plr)
            self.assertIsNone(z.sinr_db)
            self.assertEqual(fmt_val(z.sinr_db, unit="dB"), "N/A")
            self.assertEqual(fmt_val(z.pdr, fmt=".1f", unit="%"), "N/A")

        # 32 RIS panels catalog
        self.assertEqual(len(sup.panels), 32)
        for i in range(1, 33):
            pid = f"P{i}"
            p = sup.panels[pid]
            self.assertEqual(p.panel_id, pid)
            self.assertEqual(p.current_config, "CB_0")
            self.assertEqual(p.config_version, 0)
            self.assertEqual(p.command_status, "IDLE")
            self.assertEqual(p.health_state, "OK")

        # Check zone mapping distribution for panels
        self.assertEqual(sup.panels["P1"].zone_id, "Z1")
        self.assertEqual(sup.panels["P7"].zone_id, "Z1")
        self.assertEqual(sup.panels["P8"].zone_id, "Z2")
        self.assertEqual(sup.panels["P14"].zone_id, "Z2")
        self.assertEqual(sup.panels["P15"].zone_id, "Z3")
        self.assertEqual(sup.panels["P20"].zone_id, "Z3")
        self.assertEqual(sup.panels["P21"].zone_id, "Z4")
        self.assertEqual(sup.panels["P26"].zone_id, "Z4")
        self.assertEqual(sup.panels["P27"].zone_id, "Z5")
        self.assertEqual(sup.panels["P32"].zone_id, "Z5")

        # OOB initial state
        self.assertEqual(sup.oob.tx_attempts, 0)
        self.assertEqual(sup.oob.unique_commands, 0)
        self.assertIsNone(sup.oob.ack_ratio)
        self.assertEqual(fmt_val(sup.oob.ack_ratio, unit="%"), "N/A")

    def test_process_telemetry_a_structured(self):
        sup = Supervisor(run_id="run-a-test")
        payload = {
            "traffic_summary": {
                "uplink": {
                    "generated": 1000,
                    "delivered": 950,
                    "dropped": 30,
                    "pending": 20,
                    "timed_out": 0,
                },
                "downlink": {
                    "generated": 1000,
                    "delivered": 960,
                    "dropped": 20,
                    "pending": 20,
                    "timed_out": 0,
                },
                "ack_count": 1910,
                "nack_count": 50,
                "retry_count": 12,
            },
            "zones": {
                "Z1": {
                    "generated_ul": 200,
                    "delivered_ul": 190,
                    "dropped_ul": 6,
                    "pending_ul": 4,
                    "generated_dl": 200,
                    "delivered_dl": 192,
                    "dropped_dl": 4,
                    "pending_dl": 4,
                    "queue_occupancy": 15,
                    "queue_capacity": 500,
                    "sinr_db": 18.5,
                    "estimated_per": 0.015,
                    "mean_latency_ms": 16.2,
                    "p95_latency_ms": 31.0,
                    "throughput_bps": 2000000,
                }
            },
            "metrics": {
                "mean_latency_ms": 17.1,
                "p95_latency_ms": 33.4,
                "throughput_bps": 10000000,
            },
        }

        sup.process_telemetry_a(payload, ts=1729700000.0)

        # Aggregate checks
        self.assertEqual(sup.ul_generated, 1000)
        self.assertEqual(sup.ul_delivered, 950)
        self.assertEqual(sup.ul_dropped, 30)
        self.assertEqual(sup.dl_generated, 1000)
        self.assertEqual(sup.dl_delivered, 960)
        self.assertEqual(sup.dl_dropped, 20)

        self.assertEqual(sup.total_generated, 2000)
        self.assertEqual(sup.total_delivered, 1910)
        self.assertEqual(sup.total_dropped, 50)
        self.assertEqual(sup.total_pending, 40)

        # Delivered / (Delivered + Dropped) = 1910 / 1960 * 100% = 97.4489...%
        self.assertAlmostEqual(sup.pdr, 97.4489, places=2)
        # Dropped / (Delivered + Dropped) = 50 / 1960 * 100% = 2.5510...%
        self.assertAlmostEqual(sup.plr, 2.5510, places=2)

        # Zone 1 checks
        z1 = sup.zones["Z1"]
        self.assertEqual(z1.total_delivered, 382)
        self.assertEqual(z1.total_dropped, 10)
        self.assertAlmostEqual(z1.pdr, (382 / 392) * 100.0, places=2)
        self.assertEqual(z1.sinr_db, 18.5)
        self.assertEqual(z1.queue_occupancy, 15)
        self.assertEqual(z1.queue_capacity, 500)
        self.assertAlmostEqual(z1.queue_occupancy_pct, 3.0, places=1)

    def test_process_telemetry_b_structured(self):
        sup = Supervisor(run_id="run-b-test")
        payload = {
            "controller_status": {
                "active_policy": "congestion_aware",
                "last_decision": {
                    "decision": "RIS_RECONFIGURE",
                    "reason": "ZONE_CONGESTION_Z3",
                    "zone_id": "Z3",
                    "panel_id": "P16",
                    "config": "CB_4",
                    "timestamp": 1729700010.0,
                },
            },
            "zones": {
                "Z3": {
                    "congestion_state": "CONGESTED",
                    "queue_occupancy": 85,
                    "last_decision": "RIS_RECONFIGURE",
                    "last_ris_update": "CB_4",
                }
            },
            "ris_panels": [
                {
                    "panel_id": "P16",
                    "zone_id": "Z3",
                    "current_config": "CB_4",
                    "config_version": 2,
                    "command_status": "APPLIED",
                    "health_state": "OK",
                    "last_update_ts": 1729700010.0,
                }
            ],
            "oob_metrics": {
                "tx_attempts": 25,
                "unique_commands": 20,
                "acknowledged": 19,
                "negative_acknowledged": 0,
                "retries": 4,
                "timeouts": 1,
                "last_command_id": "CMD-0020",
                "last_command_status": "ACKED",
            },
        }

        sup.process_telemetry_b(payload, ts=1729700010.0)

        # Decision buffer
        self.assertEqual(len(sup.recent_decisions), 1)
        ld = sup.recent_decisions[-1]
        self.assertEqual(ld["decision"], "RIS_RECONFIGURE")
        self.assertEqual(ld["panel_id"], "P16")

        # Zone state updated
        z3 = sup.zones["Z3"]
        self.assertEqual(z3.congestion_state, "CONGESTED")
        self.assertEqual(z3.last_decision, "RIS_RECONFIGURE")
        self.assertEqual(z3.last_ris_update, "CB_4")

        # RIS panel updated
        p16 = sup.panels["P16"]
        self.assertEqual(p16.current_config, "CB_4")
        self.assertEqual(p16.config_version, 2)
        self.assertEqual(p16.command_status, "APPLIED")

        # OOB status
        self.assertEqual(sup.oob.unique_commands, 20)
        self.assertEqual(sup.oob.acknowledged, 19)
        self.assertEqual(sup.oob.timeouts, 1)
        self.assertAlmostEqual(sup.oob.ack_ratio, 95.0, places=1)

    def test_process_event(self):
        sup = Supervisor(run_id="run-evt-test")
        evt_payload = {
            "event_type": "CONGESTION_DETECTED",
            "event_id": "EVT-001",
            "zone_id": "Z2",
            "details": "Queue occupancy exceeded 80%",
        }
        sup.process_event(evt_payload, ts=1729700020.0, source="B", zone_id="Z2")
        self.assertEqual(len(sup.recent_events), 1)
        e = sup.recent_events[-1]
        self.assertEqual(e["event_type"], "CONGESTION_DETECTED")
        self.assertEqual(e["source"], "B")
        self.assertEqual(e["zone_id"], "Z2")
        self.assertEqual(e["details"], "Queue occupancy exceeded 80%")

    def test_fmt_val(self):
        self.assertEqual(fmt_val(None), "N/A")
        self.assertEqual(fmt_val(""), "N/A")
        self.assertEqual(fmt_val(12.3456, fmt=".1f", unit="dB"), "12.3dB")
        self.assertEqual(fmt_val(100, unit="ms"), "100ms")
        self.assertEqual(fmt_val("OK"), "OK")

    def test_schema_validators(self):
        ok, _ = validate_telemetry_a_payload({"traffic_summary": {"uplink": {}}})
        self.assertTrue(ok)
        ok, err = validate_telemetry_a_payload({"zones": {"Z9": {}}})
        self.assertFalse(ok)
        self.assertIn("Unknown zone_id", err)

        ok, _ = validate_telemetry_b_payload({"ris_panels": []})
        self.assertTrue(ok)
        ok, err = validate_telemetry_b_payload({"ris_panels": "invalid"})
        self.assertFalse(ok)

        ok, _ = validate_event_payload({"event_type": "PDU_DROP"})
        self.assertTrue(ok)
        ok, err = validate_event_payload({"missing_type": 1})
        self.assertFalse(ok)


class TestSupervisorIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_collector_to_supervisor_stream(self):
        cfg = {
            "laptop_c": {"bind_host": "127.0.0.1", "telemetry_port_a": 0, "telemetry_port_b": 0},
            "limits": {"max_message_bytes": 1048576, "read_timeout_s": 5.0},
            "stale_after_s": 2.0,
            "dedupe_window": 100,
            "max_clock_skew_s": 300.0,
        }
        sup = Supervisor(run_id="run-integ-1")
        col = TelemetryCollector(cfg, run_id="run-integ-1", on_message=sup.on_message)
        await col.start()

        try:
            # 1. Connect mock sender for Laptop A
            reader_a, writer_a = await asyncio.open_connection("127.0.0.1", col.ports[LAPTOP_A])
            msg_a = make_message(
                MSG_TELEMETRY_A,
                LAPTOP_A,
                LAPTOP_C,
                "run-integ-1",
                {
                    "traffic_summary": {
                        "uplink": {"generated": 500, "delivered": 485, "dropped": 10, "pending": 5},
                        "downlink": {"generated": 500, "delivered": 490, "dropped": 8, "pending": 2},
                    }
                },
                seq=0,
            )
            writer_a.write(encode_frame(encode_message(msg_a)))
            await writer_a.drain()

            # 2. Connect mock sender for Laptop B
            reader_b, writer_b = await asyncio.open_connection("127.0.0.1", col.ports[LAPTOP_B])
            msg_b = make_message(
                MSG_TELEMETRY_B,
                LAPTOP_B,
                LAPTOP_C,
                "run-integ-1",
                {
                    "oob_metrics": {
                        "tx_attempts": 10,
                        "unique_commands": 8,
                        "acknowledged": 8,
                        "timeouts": 0,
                    }
                },
                seq=0,
            )
            writer_b.write(encode_frame(encode_message(msg_b)))
            await writer_b.drain()

            # Give a brief moment for async socket read and processing
            await asyncio.sleep(0.1)

            # Check supervisor state
            self.assertEqual(sup.total_generated, 1000)
            self.assertEqual(sup.total_delivered, 975)
            self.assertEqual(sup.total_dropped, 18)
            self.assertEqual(sup.oob.acknowledged, 8)
            self.assertEqual(sup.oob.unique_commands, 8)

            writer_a.close()
            writer_b.close()
            await writer_a.wait_closed()
            await writer_b.wait_closed()
        finally:
            await col.stop()


if __name__ == "__main__":
    unittest.main()
