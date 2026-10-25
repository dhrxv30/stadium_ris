"""Unit tests for Stage C4: CSV Exporter and Performance Reports."""
import csv
import os
import shutil
import tempfile
import unittest

from laptop_c.csv_exporter import CsvExporter
from laptop_c.supervisor import Supervisor


class TestCsvExporter(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.run_id = "test-run-csv-1"
        self.exporter = CsvExporter(self.run_id, base_dir=self.test_dir)
        self.sup = Supervisor(run_id=self.run_id)

    def tearDown(self):
        self.exporter.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_file_initialization_and_headers(self):
        expected_files = [
            "system_metrics.csv",
            "zone_metrics.csv",
            "pdu_events.csv",
            "oob_events.csv",
            "ris_panel_states.csv",
        ]
        for fname in expected_files:
            fpath = os.path.join(self.exporter.output_dir, fname)
            self.assertTrue(os.path.exists(fpath), f"Missing file: {fname}")
            with open(fpath, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader)
                self.assertGreater(len(header), 2)
                self.assertIn("run_id", header)

    def test_sample_metrics_and_zone_writing(self):
        # Update supervisor with known data
        self.sup.process_telemetry_a({
            "traffic_summary": {
                "uplink": {"generated": 500, "delivered": 480, "dropped": 20, "pending": 0},
                "downlink": {"generated": 500, "delivered": 490, "dropped": 10, "pending": 0},
            },
            "zones": {
                "Z1": {"queue_occupancy": 15, "queue_capacity": 500, "sinr_db": 19.5},
            },
        }, ts=1729700000.0)

        self.exporter.sample_metrics(self.sup)

        # Verify system_metrics.csv has a data row
        p_sys = os.path.join(self.exporter.output_dir, "system_metrics.csv")
        with open(p_sys, "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
            self.assertEqual(len(rows), 2)  # header + 1 sample row
            data_row = rows[1]
            self.assertEqual(data_row[2], self.run_id)  # run_id
            self.assertEqual(data_row[20], "1000")       # total_generated
            self.assertEqual(data_row[21], "970")        # total_delivered

        # Verify zone_metrics.csv has 5 rows (one per zone)
        p_zone = os.path.join(self.exporter.output_dir, "zone_metrics.csv")
        with open(p_zone, "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
            self.assertEqual(len(rows), 6)  # header + 5 zones
            z1_row = rows[1]
            self.assertEqual(z1_row[4], "Z1")

    def test_log_discrete_events(self):
        # PDU event
        self.exporter.log_event({
            "event_type": "PDU_DELIVERY",
            "event_id": "EVT-PDU-1",
            "pdu_id": "PDU-99",
            "zone_id": "Z2",
            "details": "Delivered successfully",
        })
        p_pdu = os.path.join(self.exporter.output_dir, "pdu_events.csv")
        with open(p_pdu, "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1][3], "EVT-PDU-1")
            self.assertEqual(rows[1][6], "PDU-99")

        # OOB event
        self.exporter.log_event({
            "event_type": "RIS_COMMAND",
            "event_id": "EVT-CMD-1",
            "command_id": "CMD-001",
            "panel_id": "P10",
            "zone_id": "Z2",
            "details": "Dispatched codebook update",
        })
        p_oob = os.path.join(self.exporter.output_dir, "oob_events.csv")
        with open(p_oob, "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1][4], "CMD-001")
            self.assertEqual(rows[1][5], "P10")

    def test_log_ris_panel_snapshot(self):
        self.exporter.log_ris_panel_snapshot(self.sup)
        p_ris = os.path.join(self.exporter.output_dir, "ris_panel_states.csv")
        with open(p_ris, "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
            self.assertEqual(len(rows), 33)  # header + 32 panels
            self.assertEqual(rows[1][3], "P1")
            self.assertEqual(rows[32][3], "P32")

    def test_export_run_summary(self):
        start_time = 1729700000.0
        self.exporter.export_run_summary(self.sup, start_wall_time=start_time)
        p_sum = os.path.join(self.exporter.output_dir, "run_summary.csv")
        self.assertTrue(os.path.exists(p_sum))
        with open(p_sum, "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
            self.assertEqual(len(rows), 2)  # header + summary row
            self.assertEqual(rows[1][0], self.run_id)
            self.assertEqual(rows[1][4], "50000")  # total_devices


if __name__ == "__main__":
    unittest.main()
