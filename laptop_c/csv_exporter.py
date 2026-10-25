"""CSV and Event Recording for Laptop C (Stage C4).

Maintains and exports 6 structured CSV files per experiment run:
1. system_metrics.csv: Time-series system aggregate metrics (sampled)
2. zone_metrics.csv: Time-series per-zone metrics (sampled across Z1-Z5)
3. pdu_events.csv: Discrete PDU lifecycle events (delivery, drop, queue overflow)
4. oob_events.csv: Discrete OOB control commands and acknowledgements
5. ris_panel_states.csv: State transitions of 32 virtual passive RIS panels
6. run_summary.csv: Final experiment reconciliation report on shutdown
"""
import csv
import os
import time
from typing import Any, Dict, List, Optional

from common.constants import NUM_PANELS, ZONE_IDS
from laptop_c.supervisor import Supervisor


class CsvExporter:
    """Manages CSV metric and event streams for Laptop C experiments."""

    def __init__(self, run_id: str, base_dir: str = "results"):
        self.run_id = run_id
        self.output_dir = os.path.join(base_dir, run_id)
        os.makedirs(self.output_dir, exist_ok=True)

        self._files: Dict[str, Any] = {}
        self._writers: Dict[str, csv.writer] = {}
        self._init_csv_files()

    def _init_csv_files(self):
        """Initialize all 6 CSV files with schema headers."""
        # 1. system_metrics.csv
        p_sys = os.path.join(self.output_dir, "system_metrics.csv")
        f_sys = open(p_sys, "w", newline="", encoding="utf-8")
        w_sys = csv.writer(f_sys)
        w_sys.writerow([
            "timestamp_iso",
            "timestamp_epoch_s",
            "run_id",
            "elapsed_s",
            "peer_a_status",
            "peer_b_status",
            "peer_c_status",
            "total_devices",
            "total_senders",
            "total_receivers",
            "ul_generated",
            "ul_delivered",
            "ul_dropped",
            "ul_pending",
            "ul_timed_out",
            "dl_generated",
            "dl_delivered",
            "dl_dropped",
            "dl_pending",
            "dl_timed_out",
            "total_generated",
            "total_delivered",
            "total_dropped",
            "total_pending",
            "ack_count",
            "nack_count",
            "retry_count",
            "pdr_pct",
            "plr_pct",
            "mean_latency_ms",
            "p95_latency_ms",
            "throughput_bps",
            "oob_tx_attempts",
            "oob_unique_commands",
            "oob_acknowledged",
            "oob_timeouts",
            "oob_ack_ratio_pct",
        ])
        self._files["system"] = f_sys
        self._writers["system"] = w_sys

        # 2. zone_metrics.csv
        p_zone = os.path.join(self.output_dir, "zone_metrics.csv")
        f_zone = open(p_zone, "w", newline="", encoding="utf-8")
        w_zone = csv.writer(f_zone)
        w_zone.writerow([
            "timestamp_iso",
            "timestamp_epoch_s",
            "run_id",
            "elapsed_s",
            "zone_id",
            "senders",
            "receivers",
            "generated_ul",
            "delivered_ul",
            "dropped_ul",
            "pending_ul",
            "generated_dl",
            "delivered_dl",
            "dropped_dl",
            "pending_dl",
            "total_generated",
            "total_delivered",
            "total_dropped",
            "pdr_pct",
            "plr_pct",
            "queue_occupancy",
            "queue_capacity",
            "queue_occupancy_pct",
            "sinr_db",
            "estimated_per",
            "mean_latency_ms",
            "p95_latency_ms",
            "throughput_bps",
            "congestion_state",
            "last_decision",
            "last_ris_update",
        ])
        self._files["zone"] = f_zone
        self._writers["zone"] = w_zone

        # 3. pdu_events.csv
        p_pdu = os.path.join(self.output_dir, "pdu_events.csv")
        f_pdu = open(p_pdu, "w", newline="", encoding="utf-8")
        w_pdu = csv.writer(f_pdu)
        w_pdu.writerow([
            "timestamp_iso",
            "timestamp_epoch_s",
            "run_id",
            "event_id",
            "source",
            "event_type",
            "pdu_id",
            "zone_id",
            "details",
        ])
        self._files["pdu"] = f_pdu
        self._writers["pdu"] = w_pdu

        # 4. oob_events.csv
        p_oob = os.path.join(self.output_dir, "oob_events.csv")
        f_oob = open(p_oob, "w", newline="", encoding="utf-8")
        w_oob = csv.writer(f_oob)
        w_oob.writerow([
            "timestamp_iso",
            "timestamp_epoch_s",
            "run_id",
            "event_id",
            "command_id",
            "target_panel_id",
            "target_zone_id",
            "status",
            "details",
        ])
        self._files["oob"] = f_oob
        self._writers["oob"] = w_oob

        # 5. ris_panel_states.csv
        p_ris = os.path.join(self.output_dir, "ris_panel_states.csv")
        f_ris = open(p_ris, "w", newline="", encoding="utf-8")
        w_ris = csv.writer(f_ris)
        w_ris.writerow([
            "timestamp_iso",
            "timestamp_epoch_s",
            "run_id",
            "panel_id",
            "zone_id",
            "current_config",
            "config_version",
            "command_status",
            "health_state",
            "last_update_ts",
        ])
        self._files["ris"] = f_ris
        self._writers["ris"] = w_ris

        # Flush initial headers
        for f in self._files.values():
            f.flush()

    def sample_metrics(self, supervisor: Supervisor):
        """Sample periodic system and zone time-series rows from supervisor state."""
        now_ts = time.time()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now_ts))
        snap = supervisor.snapshot()
        pdu = snap["pdu_summary"]
        oob = snap["oob"]
        pop = snap["device_population"]
        h = snap["peer_health"]
        m = snap["overall_metrics"]

        # Write to system_metrics.csv
        self._writers["system"].writerow([
            now_iso,
            round(now_ts, 3),
            self.run_id,
            round(snap["elapsed_s"], 2),
            h.get("A", "UNKNOWN"),
            h.get("B", "UNKNOWN"),
            h.get("C", "CONNECTED"),
            pop["total"],
            pop["senders"],
            pop["receivers"],
            pdu["uplink"]["generated"],
            pdu["uplink"]["delivered"],
            pdu["uplink"]["dropped"],
            pdu["uplink"]["pending"],
            pdu["uplink"]["timed_out"],
            pdu["downlink"]["generated"],
            pdu["downlink"]["delivered"],
            pdu["downlink"]["dropped"],
            pdu["downlink"]["pending"],
            pdu["downlink"]["timed_out"],
            pdu["total_generated"],
            pdu["total_delivered"],
            pdu["total_dropped"],
            pdu["total_pending"],
            pdu["ack_count"],
            pdu["nack_count"],
            pdu["retry_count"],
            round(pdu["pdr_pct"], 2) if pdu["pdr_pct"] is not None else "",
            round(pdu["plr_pct"], 2) if pdu["plr_pct"] is not None else "",
            m["mean_latency_ms"] if m["mean_latency_ms"] is not None else "",
            m["p95_latency_ms"] if m["p95_latency_ms"] is not None else "",
            m["throughput_bps"] if m["throughput_bps"] is not None else "",
            oob["tx_attempts"],
            oob["unique_commands"],
            oob["acknowledged"],
            oob["timeouts"],
            round(oob["ack_ratio_pct"], 2) if oob["ack_ratio_pct"] is not None else "",
        ])
        self._files["system"].flush()

        # Write to zone_metrics.csv
        for zid in ZONE_IDS:
            z = supervisor.zones[zid]
            self._writers["zone"].writerow([
                now_iso,
                round(now_ts, 3),
                self.run_id,
                round(snap["elapsed_s"], 2),
                zid,
                z.senders,
                z.receivers,
                z.generated_ul,
                z.delivered_ul,
                z.dropped_ul,
                z.pending_ul,
                z.generated_dl,
                z.delivered_dl,
                z.dropped_dl,
                z.pending_dl,
                z.total_generated,
                z.total_delivered,
                z.total_dropped,
                round(z.pdr, 2) if z.pdr is not None else "",
                round(z.plr, 2) if z.plr is not None else "",
                z.queue_occupancy if z.queue_occupancy is not None else "",
                z.queue_capacity if z.queue_capacity is not None else "",
                round(z.queue_occupancy_pct, 2) if z.queue_occupancy_pct is not None else "",
                round(z.sinr_db, 2) if z.sinr_db is not None else "",
                round(z.estimated_per, 4) if z.estimated_per is not None else "",
                round(z.mean_latency_ms, 2) if z.mean_latency_ms is not None else "",
                round(z.p95_latency_ms, 2) if z.p95_latency_ms is not None else "",
                round(z.throughput_bps, 0) if z.throughput_bps is not None else "",
                z.congestion_state,
                z.last_decision or "",
                z.last_ris_update or "",
            ])
        self._files["zone"].flush()

    def log_event(self, evt: dict):
        """Log a discrete event into pdu_events.csv or oob_events.csv."""
        now_ts = evt.get("timestamp", time.time())
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now_ts))
        etype = str(evt.get("event_type", "EVENT"))

        if "PDU" in etype:
            self._writers["pdu"].writerow([
                now_iso,
                round(now_ts, 3),
                self.run_id,
                evt.get("event_id", ""),
                evt.get("source", ""),
                etype,
                evt.get("pdu_id", ""),
                evt.get("zone_id", ""),
                evt.get("details", ""),
            ])
            self._files["pdu"].flush()
        elif "RIS" in etype or "OOB" in etype or "CMD" in etype:
            self._writers["oob"].writerow([
                now_iso,
                round(now_ts, 3),
                self.run_id,
                evt.get("event_id", ""),
                evt.get("command_id", ""),
                evt.get("panel_id", ""),
                evt.get("zone_id", ""),
                etype,
                evt.get("details", ""),
            ])
            self._files["oob"].flush()

    def log_ris_panel_snapshot(self, supervisor: Supervisor):
        """Record all 32 RIS panel configurations to ris_panel_states.csv."""
        now_ts = time.time()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now_ts))
        for p in supervisor.panels.values():
            self._writers["ris"].writerow([
                now_iso,
                round(now_ts, 3),
                self.run_id,
                p.panel_id,
                p.zone_id,
                p.current_config,
                p.config_version,
                p.command_status,
                p.health_state,
                round(p.last_update_ts, 3) if p.last_update_ts else "",
            ])
        self._files["ris"].flush()

    def export_run_summary(self, supervisor: Supervisor, start_wall_time: float):
        """Write the final run_summary.csv reconciling overall experiment performance."""
        end_wall_time = time.time()
        start_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(start_wall_time))
        end_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(end_wall_time))
        duration = round(end_wall_time - start_wall_time, 2)

        pdu = supervisor.get_pdu_summary()
        oob = supervisor.get_oob_summary()
        pop = supervisor.device_population

        p_sum = os.path.join(self.output_dir, "run_summary.csv")
        with open(p_sum, "w", newline="", encoding="utf-8") as f_sum:
            w = csv.writer(f_sum)
            w.writerow([
                "run_id",
                "start_time_iso",
                "end_time_iso",
                "total_duration_s",
                "total_devices",
                "total_senders",
                "total_receivers",
                "ul_generated",
                "ul_delivered",
                "ul_dropped",
                "dl_generated",
                "dl_delivered",
                "dl_dropped",
                "total_generated",
                "total_delivered",
                "total_dropped",
                "final_pdr_pct",
                "final_plr_pct",
                "mean_latency_ms",
                "p95_latency_ms",
                "throughput_bps",
                "oob_unique_commands",
                "oob_acknowledged",
                "oob_timeouts",
                "final_oob_ack_ratio_pct",
            ])
            w.writerow([
                self.run_id,
                start_iso,
                end_iso,
                duration,
                pop["total"],
                pop["senders"],
                pop["receivers"],
                pdu["uplink"]["generated"],
                pdu["uplink"]["delivered"],
                pdu["uplink"]["dropped"],
                pdu["downlink"]["generated"],
                pdu["downlink"]["delivered"],
                pdu["downlink"]["dropped"],
                pdu["total_generated"],
                pdu["total_delivered"],
                pdu["total_dropped"],
                round(pdu["pdr_pct"], 2) if pdu["pdr_pct"] is not None else "",
                round(pdu["plr_pct"], 2) if pdu["plr_pct"] is not None else "",
                supervisor.overall_mean_latency_ms if supervisor.overall_mean_latency_ms is not None else "",
                supervisor.overall_p95_latency_ms if supervisor.overall_p95_latency_ms is not None else "",
                supervisor.overall_throughput_bps if supervisor.overall_throughput_bps is not None else "",
                oob["unique_commands"],
                oob["acknowledged"],
                oob["timeouts"],
                round(oob["ack_ratio_pct"], 2) if oob["ack_ratio_pct"] is not None else "",
            ])

    def close(self):
        """Flush and close all open CSV file handles."""
        for name, f in self._files.items():
            try:
                f.flush()
                f.close()
            except Exception:
                pass
        self._files.clear()
        self._writers.clear()
