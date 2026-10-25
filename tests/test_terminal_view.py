"""Unit tests for Stage C3: Terminal View and Filters."""
import unittest

from common.constants import ZONE_IDS
from laptop_c.supervisor import Supervisor
from laptop_c.terminal_view import TerminalView


class TestTerminalView(unittest.TestCase):
    def setUp(self):
        self.sup = Supervisor(run_id="run-term-test")
        # Populate simulated data
        self.sup.process_telemetry_a({
            "traffic_summary": {
                "uplink": {"generated": 100, "delivered": 95, "dropped": 5, "pending": 0},
                "downlink": {"generated": 100, "delivered": 96, "dropped": 4, "pending": 0},
            },
            "zones": {
                "Z1": {"queue_occupancy": 10, "queue_capacity": 500, "sinr_db": 22.0, "estimated_per": 0.01},
                "Z3": {"queue_occupancy": 80, "queue_capacity": 500, "sinr_db": 12.0, "estimated_per": 0.05},
            },
        }, ts=1729700000.0)

        self.sup.process_telemetry_b({
            "controller_status": {
                "last_decision": {
                    "decision": "RIS_RECONFIGURE",
                    "reason": "CONGESTION_Z3",
                    "zone_id": "Z3",
                    "panel_id": "P15",
                }
            },
            "zones": {
                "Z3": {"congestion_state": "CONGESTED", "last_ris_update": "CB_2"},
            },
            "ris_panels": [
                {"panel_id": "P15", "current_config": "CB_2", "config_version": 1, "command_status": "APPLIED"}
            ],
            "oob_metrics": {
                "tx_attempts": 5,
                "unique_commands": 4,
                "acknowledged": 4,
                "timeouts": 0,
            },
        }, ts=1729700000.0)

        # Add sample events
        self.sup.process_event({
            "event_type": "PDU_DELIVERY",
            "pdu_id": "PDU-001",
            "zone_id": "Z1",
            "details": "Batch delivered",
        }, ts=1729700001.0, source="A")

        self.sup.process_event({
            "event_type": "RIS_COMMAND",
            "command_id": "CMD-0004",
            "panel_id": "P15",
            "zone_id": "Z3",
            "details": "Reconfigured to CB_2",
        }, ts=1729700002.0, source="B")

    def test_render_to_string_structure(self):
        view = TerminalView(self.sup)
        output = view.render_to_string()

        # Check key sections exist
        self.assertIn("STADIUM RIS SUPERVISOR", output)
        self.assertIn("TRAFFIC & PDU ACCOUNTING", output)
        self.assertIn("OUT-OF-BAND (OOB) CONTROL LINK", output)
        self.assertIn("ZONE STATUS (Z1 - Z5)", output)
        self.assertIn("RIS PANELS (Total: 32)", output)
        self.assertIn("LATEST CONTROLLER DECISION", output)
        self.assertIn("LATEST EVENTS", output)

        # Verify values
        self.assertIn("95.5%", output)  # PDR
        self.assertIn("CONGESTED", output)
        self.assertIn("CB_2", output)

    def test_event_filtering_by_zone(self):
        # Filter for Z1 only
        view = TerminalView(self.sup, filter_zone="Z1")
        output = view.render_to_string()
        self.assertIn("PDU-001", output)
        self.assertNotIn("CMD-0004", output)

        # Filter for Z3 only
        view.set_filter_zone("Z3")
        output = view.render_to_string()
        self.assertNotIn("PDU-001", output)
        self.assertIn("CMD-0004", output)

    def test_event_filtering_by_cmd_and_pdu(self):
        view = TerminalView(self.sup, filter_cmd="CMD-0004")
        output = view.render_to_string()
        self.assertIn("CMD-0004", output)
        self.assertNotIn("PDU-001", output)

        view.clear_filters()
        view.set_filter_pdu("PDU-001")
        output = view.render_to_string()
        self.assertIn("PDU-001", output)
        self.assertNotIn("CMD-0004", output)

    def test_pause_resume(self):
        view = TerminalView(self.sup)
        self.assertFalse(view.is_paused)
        view.pause()
        self.assertTrue(view.is_paused)
        out = view.render_to_string()
        self.assertIn("[PAUSED]", out)
        view.resume()
        self.assertFalse(view.is_paused)

    def test_inspect_zone_and_panel(self):
        view = TerminalView(self.sup)
        z_info = view.inspect_zone("Z3")
        self.assertIn("ZONE Z3 DETAILED INSPECTION", z_info)
        self.assertIn("CONGESTED", z_info)
        self.assertIn("CB_2", z_info)

        p_info = view.inspect_panel("P15")
        self.assertIn("RIS PANEL P15 DETAILED INSPECTION", p_info)
        self.assertIn("CB_2", p_info)
        self.assertIn("APPLIED", p_info)

        err_z = view.inspect_zone("Z9")
        self.assertIn("Error", err_z)
        err_p = view.inspect_panel("P99")
        self.assertIn("Error", err_p)


if __name__ == "__main__":
    unittest.main()
