import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from laptop_a.main import LaptopASystem


def test_population_matches_requested_zone_totals():
    system = LaptopASystem(mode="standalone", total_devices=10, num_zones=2, tick_rate_hz=10.0)
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
    system = LaptopASystem(mode="standalone", total_devices=20, num_zones=5, tick_rate_hz=10.0)
    try:
        pdu = {"pdu_id": 42, "dst_zone": 0, "payload_size": 64, "retransmit_count": 0}
        accepted = system.pdu_gateway.retransmit_pdu(pdu)
        assert accepted is True
        assert pdu["status"] == "retransmit_queued"
        assert system.pdu_gateway.get_dl_depth(0) >= 1
    finally:
        system.shutdown()


def test_get_devices_command_uses_population_sample_and_does_not_crash():
    system = LaptopASystem(mode="standalone", total_devices=20, num_zones=5, tick_rate_hz=10.0)
    try:
        msg = {"command": "GET_DEVICES", "sample_size": 5}
        client = type("DummyClient", (), {})()
        system._handle_supervisory_command(client, msg)
    finally:
        system.shutdown()
