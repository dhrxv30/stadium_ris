"""
receiver_feedback.py — Aggregated Receiver Feedback & OOB Report Generator

Collects per-device delivery outcomes (ACK/NACK, SINR, PER, latency) and
compresses them into periodic zone-level health reports for the OOB
control channel to Laptop B.

Scalability design:
  • 50,000 receivers are NOT reported individually every window.
  • Per-zone accumulators aggregate metrics in O(1) per event.
  • A configurable set of "tracked devices" can produce individual reports
    for deep inspection without flooding the OOB link.

Report format:
  The aggregated report is a JSON-serialisable dict matching the master
  prompt's OOB RECEIVER_FEEDBACK_REPORT schema (§5.1):
    - Per-zone: rx_pkts, lost_pkts, sinr_db, per, mean_latency_ms,
      jitter_ms, demand_estimate, queue_depth.
    - Optional per-device reports for tracked devices.
    - Window metadata: window_id, duration, start/end timestamps, run_id.

Integration points:
  → ReceiverModel calls record_delivery() after each PDU evaluation.
  → main.py calls aggregate_report() at the configured feedback interval
    and sends the result to Laptop B via the OOB TCP link.
  → MetricsRegistry is updated in parallel for dashboard display.
  → Laptop C can request the latest report via the supervisory link.
"""

import time
import math
from collections import defaultdict
from typing import Dict, List, Optional, Set

try:
    from receiver_model import DeliveryResult
except ImportError:
    from laptop_a.receiver_model import DeliveryResult


# ---------------------------------------------------------------------------
#  Per-zone accumulator
# ---------------------------------------------------------------------------

class ZoneAccumulator:
    """
    Lightweight running accumulator for one zone within one feedback window.
    All operations are O(1) — no per-device storage for non-tracked devices.
    """

    __slots__ = [
        "rx_count", "loss_count", "total_count",
        "sinr_sum", "sinr_sq_sum", "sinr_min", "sinr_max",
        "per_sum",
        "latency_sum", "latency_sq_sum", "latency_min", "latency_max",
        "payload_bytes",
        "nlos_count",
        "ris_gain_sum",
    ]

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.rx_count = 0
        self.loss_count = 0
        self.total_count = 0
        self.sinr_sum = 0.0
        self.sinr_sq_sum = 0.0
        self.sinr_min = float("inf")
        self.sinr_max = float("-inf")
        self.per_sum = 0.0
        self.latency_sum = 0.0
        self.latency_sq_sum = 0.0
        self.latency_min = float("inf")
        self.latency_max = 0.0
        self.payload_bytes = 0
        self.nlos_count = 0
        self.ris_gain_sum = 0.0

    def record(self, result: DeliveryResult, payload_size: int = 0) -> None:
        """Record one delivery outcome."""
        self.total_count += 1
        self.sinr_sum += result.sinr_db
        self.sinr_sq_sum += result.sinr_db ** 2
        if result.sinr_db < self.sinr_min:
            self.sinr_min = result.sinr_db
        if result.sinr_db > self.sinr_max:
            self.sinr_max = result.sinr_db
        self.per_sum += result.per
        self.ris_gain_sum += result.ris_gain_db
        if result.nlos:
            self.nlos_count += 1

        if result.delivered:
            self.rx_count += 1
            self.latency_sum += result.latency_ms
            self.latency_sq_sum += result.latency_ms ** 2
            if result.latency_ms < self.latency_min:
                self.latency_min = result.latency_ms
            if result.latency_ms > self.latency_max:
                self.latency_max = result.latency_ms
            self.payload_bytes += payload_size
        else:
            self.loss_count += 1

    def snapshot(self) -> dict:
        """Return a summary dict for this zone."""
        n = self.total_count or 1
        rx = self.rx_count or 1

        mean_sinr = self.sinr_sum / n
        sinr_var = max(0, self.sinr_sq_sum / n - mean_sinr ** 2)

        mean_lat = self.latency_sum / rx if self.rx_count else 0.0
        lat_var = max(0, self.latency_sq_sum / rx - mean_lat ** 2) if self.rx_count else 0.0

        return {
            "rx_pkts": self.rx_count,
            "lost_pkts": self.loss_count,
            "total_pkts": self.total_count,
            "pdr": round(self.rx_count / n, 4),
            "sinr_db": round(mean_sinr, 2),
            "sinr_std_db": round(math.sqrt(sinr_var), 2),
            "sinr_min_db": round(self.sinr_min, 2) if self.sinr_min != float("inf") else 0.0,
            "sinr_max_db": round(self.sinr_max, 2) if self.sinr_max != float("-inf") else 0.0,
            "per": round(self.per_sum / n, 4),
            "mean_latency_ms": round(mean_lat, 3),
            "jitter_ms": round(math.sqrt(lat_var), 3),
            "latency_min_ms": round(self.latency_min, 3) if self.latency_min != float("inf") else 0.0,
            "latency_max_ms": round(self.latency_max, 3),
            "throughput_bytes": self.payload_bytes,
            "nlos_fraction": round(self.nlos_count / n, 3),
            "mean_ris_gain_db": round(self.ris_gain_sum / n, 2),
        }


# ---------------------------------------------------------------------------
#  Receiver Feedback Aggregator
# ---------------------------------------------------------------------------

class ReceiverFeedback:
    """
    Aggregates per-device delivery events into periodic zone-level OOB reports.

    Usage:
        fb = ReceiverFeedback(num_zones=5, window_duration=1.0)
        fb.record_delivery(pdu, result, sim_time)
        ...
        if fb.is_window_complete(sim_time):
            report = fb.aggregate_report(sim_time)
            send_to_laptop_b(report)
            fb.reset_window(sim_time)

    Args:
        num_zones:        Number of stadium zones.
        window_duration:  Feedback aggregation window in seconds.
        run_id:           Run identifier for report tagging.
    """

    def __init__(
        self,
        num_zones: int = 5,
        window_duration: float = 1.0,
        run_id: str = "RUN_DEFAULT",
    ):
        self.num_zones = num_zones
        self.window_duration = window_duration
        self.run_id = run_id

        # Per-zone accumulators
        self._accumulators: Dict[int, ZoneAccumulator] = {
            z: ZoneAccumulator() for z in range(num_zones)
        }

        # Window timing
        self._window_start: float = 0.0
        self._window_id: int = 0

        # Individual device tracking (for selected deep inspection)
        self._tracked_devices: Set[int] = set()
        self._individual_reports: List[dict] = []
        self._max_individual: int = 100   # cap per window

    # ----- event recording -----

    def record_delivery(
        self, pdu: dict, result: DeliveryResult, sim_time: float
    ) -> None:
        """
        Record the delivery outcome for one DL PDU.

        Args:
            pdu:       The downlink PDU dict.
            result:    DeliveryResult from ReceiverModel.
            sim_time:  Current simulation time.
        """
        zone = pdu.get("dst_zone", 0)
        payload = pdu.get("payload_size", 0)
        self._accumulators[zone].record(result, payload)

        # Individual tracking for selected devices
        dst = pdu.get("dst_device", -1)
        if dst in self._tracked_devices and len(self._individual_reports) < self._max_individual:
            self._individual_reports.append({
                "device_id": dst,
                "pdu_id": pdu.get("pdu_id"),
                "seq_num": pdu.get("seq_num"),
                "zone": zone,
                "delivered": result.delivered,
                "sinr_db": round(result.sinr_db, 2),
                "per": round(result.per, 4),
                "latency_ms": round(result.latency_ms, 3),
                "ris_gain_db": round(result.ris_gain_db, 2),
                "nlos": result.nlos,
                "timestamp": sim_time,
            })

    # ----- window management -----

    def is_window_complete(self, sim_time: float) -> bool:
        """Check if the current feedback window has expired."""
        return (sim_time - self._window_start) >= self.window_duration

    def reset_window(self, sim_time: float) -> None:
        """Reset all accumulators and start a new window."""
        for acc in self._accumulators.values():
            acc.reset()
        self._individual_reports.clear()
        self._window_start = sim_time
        self._window_id += 1

    def set_window_duration(self, duration: float) -> None:
        """Adjust the feedback interval (e.g., from Laptop B or C override)."""
        self.window_duration = max(0.05, duration)

    # ----- aggregated report generation -----

    def aggregate_report(self, sim_time: float) -> dict:
        """
        Generate a complete OOB feedback report for Laptop B.

        The report follows the master prompt RECEIVER_FEEDBACK_REPORT schema.
        """
        zone_metrics = {}
        for z in range(self.num_zones):
            zone_metrics[f"Z{z + 1}"] = self._accumulators[z].snapshot()

        report = {
            "protocol_version": "1.0",
            "message_type": "RECEIVER_FEEDBACK_REPORT",
            "message_id": f"fb-{self.run_id}-{self._window_id:06d}",
            "timestamp": time.time(),
            "source": "LAPTOP_A",
            "payload": {
                "run_id": self.run_id,
                "window_id": self._window_id,
                "window_start": self._window_start,
                "window_end": sim_time,
                "window_duration_sec": round(sim_time - self._window_start, 3),
                "zone_metrics": zone_metrics,
                "individual_reports": list(self._individual_reports),
                "individual_count": len(self._individual_reports),
            },
        }
        return report

    # ----- device tracking management -----

    def set_tracked_devices(self, device_ids: List[int]) -> None:
        """
        Set which device IDs receive individual per-PDU feedback reports.
        Used for deep inspection of specific receivers.
        """
        self._tracked_devices = set(device_ids)

    def add_tracked_device(self, device_id: int) -> None:
        self._tracked_devices.add(device_id)

    def clear_tracked_devices(self) -> None:
        self._tracked_devices.clear()

    def set_max_individual_reports(self, max_count: int) -> None:
        self._max_individual = max(1, max_count)

    # ----- quick zone status (for dashboard) -----

    def get_zone_snapshot(self, zone_id: int) -> dict:
        """Return the current (in-progress) accumulator snapshot for one zone."""
        return self._accumulators[zone_id].snapshot()

    def get_all_zones_snapshot(self) -> Dict[str, dict]:
        """Current accumulator state for all zones (not a finalised report)."""
        return {f"Z{z + 1}": self._accumulators[z].snapshot() for z in range(self.num_zones)}

    # ----- statistics -----

    def get_stats(self) -> dict:
        return {
            "window_id": self._window_id,
            "window_duration": self.window_duration,
            "tracked_devices": len(self._tracked_devices),
            "individual_reports_in_window": len(self._individual_reports),
        }
