import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from laptop_a.main import LaptopASystem
from laptop_a.device_population import DevicePopulation
from common.validation import validate_message


def test_shared_full_population_has_5000_senders_and_receivers_per_zone():
    population = DevicePopulation(total_devices=50_000, num_zones=5, rng_seed=42)

    assert population.total_devices == 50_000
    assert sum(len(population.get_sources(zone)) for zone in range(5)) == 25_000
    assert sum(len(population.get_receivers(zone)) for zone in range(5)) == 25_000
    for zone in range(5):
        assert len(population.get_sources(zone)) == 5_000
        assert len(population.get_receivers(zone)) == 5_000


def test_population_matches_requested_zone_totals():
    import tempfile

    with tempfile.TemporaryDirectory() as export_dir:
        system = LaptopASystem(
            mode="standalone",
            total_devices=10,
            num_zones=2,
            tick_rate_hz=10.0,
            export_dir=export_dir,
            show_dashboard=False,
        )
        try:
            assert system.total_devices == 10
            assert system.population.num_zones == 2
            assert len(system.population.get_sources(0)) == 2
            assert len(system.population.get_receivers(0)) == 3
            assert len(system.population.get_sources(1)) == 2
            assert len(system.population.get_receivers(1)) == 3
        finally:
            system.shutdown()


def test_retransmission_api_exists_and_works():
    import tempfile

    with tempfile.TemporaryDirectory() as export_dir:
        system = LaptopASystem(
            mode="standalone",
            total_devices=20,
            num_zones=5,
            tick_rate_hz=10.0,
            export_dir=export_dir,
            show_dashboard=False,
        )
        try:
            pdu = {"pdu_id": 42, "dst_zone": 0, "payload_size": 64, "retransmit_count": 0}
            accepted = system.pdu_gateway.retransmit_pdu(pdu)
            assert accepted is True
            assert pdu["status"] == "retransmit_queued"
            assert system.pdu_gateway.get_dl_depth(0) >= 1
        finally:
            system.shutdown()


def test_get_devices_command_uses_population_sample_and_does_not_crash():
    import tempfile

    class DummyClient:
        def sendall(self, payload):
            self.payload = payload

    with tempfile.TemporaryDirectory() as export_dir:
        system = LaptopASystem(
            mode="standalone",
            total_devices=20,
            num_zones=5,
            tick_rate_hz=10.0,
            export_dir=export_dir,
            show_dashboard=False,
        )
        try:
            msg = {"command": "GET_DEVICES", "sample_size": 5}
            system._handle_supervisory_command(DummyClient(), msg)
        finally:
            system.shutdown()


def test_feedback_messages_match_laptop_b_per_zone_protocol():
    import tempfile
    import time

    with tempfile.TemporaryDirectory() as export_dir:
        system = LaptopASystem(
            mode="standalone",
            total_devices=20,
            num_zones=5,
            tick_rate_hz=10.0,
            export_dir=export_dir,
            show_dashboard=False,
        )
        try:
            report = system.receiver_feedback.aggregate_report(system.sim_time)
            first = system._build_b_feedback_messages(report, time.time() + 0.1)
            second = system._build_b_feedback_messages(report, time.time() + 0.2)

            assert len(first) == 5
            for zone_id, message in enumerate(first, start=1):
                validate_message(message)
                assert message["type"] == "FEEDBACK_REPORT"
                assert message["zone_id"] == f"Z{zone_id}"
                assert message["payload"]["seq"] == 0
                assert message["payload"]["queue_capacity"] > 0
                assert message["payload"]["queue_occupancy"] <= message["payload"]["queue_capacity"]
            assert all(message["payload"]["seq"] == 1 for message in second)
            assert first[0]["msg_id"] != second[0]["msg_id"]
        finally:
            system.shutdown()


def test_sampled_pdu_and_feedback_csv_exports():
    import csv
    import tempfile

    from laptop_a.metrics import MetricsRegistry

    metrics = MetricsRegistry(num_zones=1, pdu_event_sample_rate=2)
    for pdu_id in range(1, 4):
        metrics.record_pdu_event(
            {"pdu_id": pdu_id, "src_zone": 0, "direction": "UL", "status": "generated"},
            "generated",
            sim_time=float(pdu_id),
        )
    metrics.record_feedback_message({
        "zone_id": "Z1",
        "payload": {
            "seq": 0,
            "window_start": 1.0,
            "window_end": 2.0,
            "queue_occupancy": 0,
            "queue_capacity": 50_000,
            "generated": 10,
            "delivered": 9,
            "dropped": 1,
            "mean_sinr_db": 12.5,
            "per": 0.1,
            "traffic_rate_pps": 10.0,
        },
    })

    with tempfile.TemporaryDirectory() as output_dir:
        metrics.export_csv(output_dir, "TEST")
        with open(os.path.join(output_dir, "TEST_pdu_events.csv"), newline="", encoding="utf-8") as fh:
            event_rows = list(csv.DictReader(fh))
        with open(os.path.join(output_dir, "TEST_receiver_feedback.csv"), newline="", encoding="utf-8") as fh:
            feedback_rows = list(csv.DictReader(fh))
        assert [row["pdu_id"] for row in event_rows] == ["1", "3"]
        assert feedback_rows[0]["mean_sinr_db_simulated"] == "12.5"


def test_retried_delivery_counts_as_one_packet_not_two_drops():
    import tempfile

    from laptop_a.receiver_model import DeliveryResult

    with tempfile.TemporaryDirectory() as export_dir:
        system = LaptopASystem(
            mode="standalone",
            total_devices=20,
            num_zones=5,
            tick_rate_hz=10.0,
            export_dir=export_dir,
            show_dashboard=False,
        )
        try:
            pdu = {
                "pdu_id": 101,
                "seq_num": 0,
                "src_device": 0,
                "dst_device": 2,
                "src_zone": 0,
                "dst_zone": 0,
                "direction": "UL",
                "payload_size": 64,
                "priority": 1,
                "gen_time": 0.0,
                "status": "generated",
            }
            system.traffic_gen.generate_tick = lambda sim_time, dt: []
            system.metrics.increment("pdu_generated")
            system.metrics.zone_increment(0, "pdu_generated")
            assert system.pdu_gateway.enqueue_uplink(pdu)

            system.receiver_model.evaluate_delivery = lambda pdu, sim_time: DeliveryResult(
                delivered=pdu.get("retransmit_count", 0) > 0,
                sinr_db=4.0,
                per=0.5,
                latency_ms=10.0,
                drop_reason="channel_loss" if pdu.get("retransmit_count", 0) == 0 else "",
            )

            system.step()
            assert system.metrics.get_counter("pdu_attempt_failed") == 1
            assert system.metrics.get_counter("pdu_dropped") == 0
            assert system.metrics.verify_accounting()[0]

            system.step()
            assert system.metrics.get_counter("pdu_delivered") == 1
            assert system.metrics.get_counter("pdu_dropped") == 0
            assert system.metrics.verify_accounting()[0]
            zone_report = system.receiver_feedback.get_zone_snapshot(0)
            assert zone_report["rx_pkts"] == 1
            assert zone_report["lost_pkts"] == 0
        finally:
            system.shutdown()


def test_uplink_queue_overflow_is_a_terminal_drop_in_accounting():
    import tempfile

    with tempfile.TemporaryDirectory() as export_dir:
        system = LaptopASystem(
            mode="standalone",
            total_devices=20,
            num_zones=5,
            tick_rate_hz=10.0,
            export_dir=export_dir,
            show_dashboard=False,
        )
        try:
            system.traffic_gen.generate_tick = lambda sim_time, dt: []
            pdu = {
                "pdu_id": 202,
                "src_zone": 0,
                "dst_zone": 0,
                "direction": "UL",
                "status": "generated",
            }
            system.metrics.increment("pdu_generated")
            system.metrics.zone_increment(0, "pdu_generated")
            system.pdu_gateway.ul_queues[0].max_size = 0
            assert not system.pdu_gateway.enqueue_uplink(pdu)

            system.step()
            assert system.metrics.get_counter("pdu_dropped") == 1
            assert system.metrics.get_counter("pdu_attempt_failed") == 0
            assert system.metrics.verify_accounting()[0]
        finally:
            system.shutdown()
