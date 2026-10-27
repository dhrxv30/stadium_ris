"""
metrics.py — Real-time Metrics Collection, Terminal Dashboard & CSV Export

Central registry for all Laptop A performance counters:
  • PDU lifecycle: generated → queued → transmitted → delivered / dropped
  • Per-zone breakdowns (Z1–Z5)
  • Latency histograms with percentile calculation
  • Rolling throughput (bits per second)
  • Queue depths, drop reasons
  • OOB and network-level counters
  • Strict accounting verification (generated == delivered + dropped + pending)

Integration points:
  → All Laptop A modules call increment / record_* methods.
  → snapshot() produces a complete dict for Laptop C telemetry export.
  → print_dashboard() renders a 1 Hz terminal TUI.
  → export_csv() writes final run results for post-analysis.
"""

import os
import csv
import time
import threading
from collections import defaultdict, deque
from typing import Dict, Tuple, Any, Optional


# ---------------------------------------------------------------------------
#  Latency Histogram
# ---------------------------------------------------------------------------

class LatencyHistogram:
    """Fixed-bucket histogram with rolling percentile window."""

    BUCKETS_MS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, float("inf")]

    def __init__(self, rolling_window: int = 10_000):
        self.counts = [0] * len(self.BUCKETS_MS)
        self.total: int = 0
        self.sum: float = 0.0
        self.min_val: float = float("inf")
        self.max_val: float = 0.0
        self._rolling = deque(maxlen=rolling_window)

    def record(self, value_ms: float) -> None:
        self.total += 1
        self.sum += value_ms
        if value_ms < self.min_val:
            self.min_val = value_ms
        if value_ms > self.max_val:
            self.max_val = value_ms
        self._rolling.append(value_ms)
        for i, edge in enumerate(self.BUCKETS_MS):
            if value_ms <= edge:
                self.counts[i] += 1
                break

    @property
    def mean(self) -> float:
        return self.sum / self.total if self.total else 0.0

    def percentile(self, p: float) -> float:
        if not self._rolling:
            return 0.0
        s = sorted(self._rolling)
        idx = min(int(len(s) * p / 100.0), len(s) - 1)
        return s[idx]

    def snapshot(self) -> dict:
        return {
            "count": self.total,
            "mean_ms": round(self.mean, 3),
            "min_ms": round(self.min_val, 3) if self.min_val != float("inf") else 0.0,
            "max_ms": round(self.max_val, 3),
            "p50_ms": round(self.percentile(50), 3),
            "p95_ms": round(self.percentile(95), 3),
            "p99_ms": round(self.percentile(99), 3),
        }

    def reset(self) -> None:
        self.counts = [0] * len(self.BUCKETS_MS)
        self.total = 0
        self.sum = 0.0
        self.min_val = float("inf")
        self.max_val = 0.0
        self._rolling.clear()


# ---------------------------------------------------------------------------
#  Metrics Registry
# ---------------------------------------------------------------------------

class MetricsRegistry:
    """
    Thread-safe, zone-aware metrics registry for the Laptop A BTS system.

    Usage:
        metrics = MetricsRegistry(num_zones=5)
        metrics.increment("pdu_generated")
        metrics.zone_increment(0, "pdu_generated")
        metrics.record_latency(0, 12.5)
        metrics.record_throughput(512)
        snap = metrics.snapshot()   # → full dict for telemetry
        metrics.print_dashboard()   # → terminal TUI
    """

    def __init__(
        self,
        num_zones: int = 5,
        pdu_event_sample_rate: int = 0,
        dashboard_interval_sec: float = 1.0,
    ):
        self.num_zones = num_zones
        self._lock = threading.Lock()
        self.pdu_event_sample_rate = max(0, pdu_event_sample_rate)
        self.dashboard_interval_sec = max(0.05, dashboard_interval_sec)

        # Global counters
        self._counters: Dict[str, int] = defaultdict(int)

        # Per-zone counters
        self._zone_counters: Dict[int, Dict[str, int]] = {
            z: defaultdict(int) for z in range(num_zones)
        }

        # Latency histograms — per zone + global
        self._zone_latency: Dict[int, LatencyHistogram] = {
            z: LatencyHistogram() for z in range(num_zones)
        }
        self._global_latency = LatencyHistogram()

        # Rolling throughput window — (wall_timestamp, bytes)
        self._throughput_window: deque = deque(maxlen=5000)

        # Queue depth snapshots
        self._queue_depths: Dict[str, int] = defaultdict(int)

        # Drop reason counters
        self._drop_reasons: Dict[str, int] = defaultdict(int)
        self._pdu_event_sequence = 0
        self._pdu_events = deque(maxlen=100_000)
        self._feedback_rows = deque(maxlen=100_000)
        self._pending_pdus: Optional[int] = None
        self._run_metadata: Dict[str, Any] = {}
        self._network_status = {
            "laptop_b_data_connected": False,
            "laptop_b_feedback_connected": False,
        }

        # Timing
        self._start_wall = time.time()
        self._last_dashboard_wall = 0.0

    # ----- atomic counter operations -----

    def increment(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] += value

    def zone_increment(self, zone_id: int, name: str, value: int = 1) -> None:
        with self._lock:
            self._zone_counters[zone_id][name] += value

    def get_counter(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, 0)

    def get_zone_counter(self, zone_id: int, name: str) -> int:
        with self._lock:
            return self._zone_counters[zone_id].get(name, 0)

    # ----- specialised recorders -----

    def record_latency(self, zone_id: int, latency_ms: float) -> None:
        with self._lock:
            self._zone_latency[zone_id].record(latency_ms)
            self._global_latency.record(latency_ms)

    def record_throughput(self, payload_bytes: int) -> None:
        with self._lock:
            self._throughput_window.append((time.time(), payload_bytes))
            self._counters["payload_bytes_delivered"] += payload_bytes

    def record_drop(self, reason: str) -> None:
        with self._lock:
            self._drop_reasons[reason] += 1
            self._counters["pdu_dropped"] += 1

    def set_queue_depth(self, queue_name: str, depth: int) -> None:
        with self._lock:
            self._queue_depths[queue_name] = depth

    def set_pending_pdus(self, pending: int) -> None:
        with self._lock:
            self._pending_pdus = max(0, pending)

    def record_drop_attempt(self, reason: str) -> None:
        """Record a failed transmission attempt without counting a terminal PDU drop."""
        with self._lock:
            self._drop_reasons[reason] += 1
            self._counters["pdu_attempt_failed"] += 1

    def set_network_status(self, data_connected: bool, feedback_connected: bool) -> None:
        with self._lock:
            self._network_status["laptop_b_data_connected"] = data_connected
            self._network_status["laptop_b_feedback_connected"] = feedback_connected

    def set_run_metadata(self, metadata: Dict[str, Any]) -> None:
        with self._lock:
            self._run_metadata = dict(metadata)

    def record_pdu_event(
        self,
        pdu: dict,
        event: str,
        *,
        sim_time: Optional[float] = None,
        result: Optional[Any] = None,
        drop_reason: str = "",
    ) -> None:
        """Store sampled PDU lifecycle events; a rate of zero disables the log."""
        if not self.pdu_event_sample_rate:
            return
        with self._lock:
            self._pdu_event_sequence += 1
            if (self._pdu_event_sequence - 1) % self.pdu_event_sample_rate:
                return
            if len(self._pdu_events) == self._pdu_events.maxlen:
                self._counters["pdu_events_truncated"] += 1
            self._pdu_events.append({
                "event": event,
                "sim_time_sec": sim_time,
                "pdu_id": pdu.get("pdu_id"),
                "src_device": pdu.get("src_device"),
                "dst_device": pdu.get("dst_device"),
                "zone": pdu.get("dst_zone", pdu.get("src_zone")),
                "direction": pdu.get("direction"),
                "payload_size_bytes": pdu.get("payload_size"),
                "priority": pdu.get("priority"),
                "seq_num": pdu.get("seq_num"),
                "status": pdu.get("status"),
                "sinr_db_simulated": getattr(result, "sinr_db", None),
                "per_simulated": getattr(result, "per", None),
                "latency_ms_simulated": getattr(result, "latency_ms", None),
                "drop_reason": drop_reason or getattr(result, "drop_reason", ""),
            })

    def record_feedback_message(self, message: dict) -> None:
        """Retain per-zone feedback reports for the run-level CSV export."""
        payload = message["payload"]
        with self._lock:
            if len(self._feedback_rows) == self._feedback_rows.maxlen:
                self._counters["feedback_csv_rows_truncated"] += 1
            self._feedback_rows.append({
                "zone": message["zone_id"],
                "seq": payload["seq"],
                "window_start_epoch_sec": payload["window_start"],
                "window_end_epoch_sec": payload["window_end"],
                "queue_occupancy_pdus": payload["queue_occupancy"],
                "queue_capacity_pdus": payload["queue_capacity"],
                "generated_pdus": payload["generated"],
                "delivered_pdus": payload["delivered"],
                "dropped_pdus": payload["dropped"],
                "mean_sinr_db_simulated": payload.get("mean_sinr_db"),
                "per_simulated": payload.get("per"),
                "traffic_rate_pps": payload.get("traffic_rate_pps"),
            })

    # ----- derived queries -----

    def _throughput_bps_unlocked(self) -> float:
        now = time.time()
        recent = [(t, b) for t, b in self._throughput_window if now - t <= 1.0]
        return sum(b for _, b in recent) * 8.0 if recent else 0.0

    def get_throughput_bps(self) -> float:
        with self._lock:
            return self._throughput_bps_unlocked()

    # ----- complete snapshot (for Laptop C telemetry) -----

    def snapshot(self) -> dict:
        """Return a complete, JSON-serialisable metrics snapshot."""
        with self._lock:
            elapsed = time.time() - self._start_wall

            gen = self._counters.get("pdu_generated", 0)
            delivered = self._counters.get("pdu_delivered", 0)
            dropped = self._counters.get("pdu_dropped", 0)
            pending = (
                self._pending_pdus
                if self._pending_pdus is not None
                else max(
                    self._counters.get("pdu_queued", 0)
                    - self._counters.get("pdu_dequeued", 0),
                    0,
                )
            )

            final_outcomes = delivered + dropped
            pdr = delivered / final_outcomes if final_outcomes > 0 else 0.0
            loss = dropped / final_outcomes if final_outcomes > 0 else 0.0

            zone_data = {}
            for z in range(self.num_zones):
                zc = self._zone_counters[z]
                z_gen = zc.get("pdu_generated", 0)
                z_del = zc.get("pdu_delivered", 0)
                z_drop = zc.get("pdu_dropped", 0)
                z_final_outcomes = z_del + z_drop
                z_attempt_failed = zc.get("pdu_attempt_failed", 0)
                z_acked = zc.get("pdu_acked", 0)
                z_nacked = zc.get("pdu_nacked", 0)
                z_retx = zc.get("pdu_retransmitted", 0)
                zone_data[f"Z{z + 1}"] = {
                    "generated": z_gen,
                    "delivered": z_del,
                    "dropped": z_drop,
                    "final_outcomes": z_final_outcomes,
                    "attempt_failed": z_attempt_failed,
                    "acked": z_acked,
                    "nacked": z_nacked,
                    "retransmitted": z_retx,
                    "pdr": (
                        round(z_del / z_final_outcomes, 4)
                        if z_final_outcomes > 0 else 0.0
                    ),
                    "latency": self._zone_latency[z].snapshot(),
                    "queue_ul": self._queue_depths.get(f"ul_z{z}", 0),
                    "queue_dl": self._queue_depths.get(f"dl_z{z}", 0),
                }

            return {
                "run": dict(self._run_metadata),
                "timestamp": time.time(),
                "elapsed_sec": round(elapsed, 2),
                "global": {
                    "pdu_generated": gen,
                    "pdu_delivered": delivered,
                    "pdu_dropped": dropped,
                    "pdu_final_outcomes": final_outcomes,
                    "pdu_acked": self._counters.get("pdu_acked", 0),
                    "pdu_nacked": self._counters.get("pdu_nacked", 0),
                    "pdu_attempt_failed": self._counters.get("pdu_attempt_failed", 0),
                    "pdu_events_truncated": self._counters.get("pdu_events_truncated", 0),
                    "feedback_csv_rows_truncated": self._counters.get("feedback_csv_rows_truncated", 0),
                    "pdu_retransmitted": self._counters.get("pdu_retransmitted", 0),
                    "pdu_pending": pending,
                    "pdr": round(pdr, 4),
                    "loss_ratio": round(loss, 4),
                    "payload_bytes_delivered": self._counters.get(
                        "payload_bytes_delivered", 0
                    ),
                    "throughput_bps": round(self._throughput_bps_unlocked()),
                    "latency": self._global_latency.snapshot(),
                    "drop_reasons": dict(self._drop_reasons),
                },
                "zones": zone_data,
                "oob": {
                    "feedback_reports_sent": self._counters.get("feedback_reports_sent", 0),
                    "feedback_windows_completed": self._counters.get("feedback_windows_completed", 0),
                    "feedback_zone_messages_sent": self._counters.get("feedback_zone_messages_sent", 0),
                    "feedback_reports_expired": self._counters.get("feedback_reports_expired", 0),
                    "ris_updates_received": self._counters.get("ris_updates_received", 0),
                    "scheduling_updates_received": self._counters.get("scheduling_updates_received", 0),
                    "control_updates_rejected": self._counters.get("control_updates_rejected", 0),
                    "supervisory_overrides": self._counters.get("supervisory_overrides", 0),
                },
                "network": {
                    "bytes_sent": self._counters.get("net_bytes_sent", 0),
                    "bytes_received": self._counters.get("net_bytes_received", 0),
                    "messages_sent": self._counters.get("net_msg_sent", 0),
                    "messages_received": self._counters.get("net_msg_received", 0),
                    "connection_errors": self._counters.get("net_conn_errors", 0),
                    **self._network_status,
                },
            }

    # ----- strict accounting verification -----

    def verify_accounting(self) -> Tuple[bool, str]:
        """
        Verify: generated == delivered + dropped + pending.
        Returns (is_balanced, human_readable_message).
        """
        with self._lock:
            gen = self._counters.get("pdu_generated", 0)
            delivered = self._counters.get("pdu_delivered", 0)
            dropped = self._counters.get("pdu_dropped", 0)
            pending = (
                self._pending_pdus
                if self._pending_pdus is not None
                else max(
                    self._counters.get("pdu_queued", 0)
                    - self._counters.get("pdu_dequeued", 0),
                    0,
                )
            )
        expected = delivered + dropped + pending
        ok = gen == expected
        msg = (
            f"Generated({gen}) {'==' if ok else '!='} "
            f"Delivered({delivered}) + Dropped({dropped}) + Pending({pending}) = {expected}"
        )
        return ok, msg

    # ----- terminal dashboard (1 Hz TUI) -----

    def print_dashboard(self, force: bool = False) -> None:
        """Print a compact, full-width terminal dashboard.  Rate-limited to 1 Hz."""
        now = time.time()
        if not force and (now - self._last_dashboard_wall) < self.dashboard_interval_sec:
            return
        self._last_dashboard_wall = now

        snap = self.snapshot()
        g = snap["global"]
        W = 90

        lines = [
            "",
            "=" * W,
            f"  LAPTOP A  —  BTS & Traffic Gateway  │  Elapsed: {snap['elapsed_sec']:.1f}s".ljust(W),
            "=" * W,
            f"  Generated: {g['pdu_generated']:>10,}  │  Delivered: {g['pdu_delivered']:>10,}  │"
            f"  Dropped: {g['pdu_dropped']:>10,}  │  Pending: {g['pdu_pending']:>10,}",
            f"  PDR: {g['pdr']:.4f}  │  Loss: {g['loss_ratio']:.4f}  │"
            f"  Throughput: {g['throughput_bps'] / 1e6:.2f} Mbps",
            f"  ACK={g['pdu_acked']:,}  │ NACK={g['pdu_nacked']:,}  │"
            f"Failed attempts={g['pdu_attempt_failed']:,}  │"
            f"  Retries queued: {g['pdu_retransmitted']:,}",
            f"  Latency — Mean: {g['latency']['mean_ms']:.1f} ms  │"
            f"  P95: {g['latency']['p95_ms']:.1f} ms  │  P99: {g['latency']['p99_ms']:.1f} ms",
            "-" * W,
            f"  {'Zone':<6} {'Gen':>9} {'Deliv':>9} {'Drop':>9}"
            f" {'PDR':>8} {'Lat(ms)':>9} {'Q_UL':>7} {'Q_DL':>7}",
            "-" * W,
        ]

        for z_name, z_data in snap["zones"].items():
            lines.append(
                f"  {z_name:<6} {z_data['generated']:>9,} {z_data['delivered']:>9,}"
                f" {z_data['dropped']:>9,} {z_data['pdr']:>8.4f}"
                f" {z_data['latency']['mean_ms']:>9.1f}"
                f" {z_data['queue_ul']:>7} {z_data['queue_dl']:>7}"
            )

        lines.append("-" * W)
        lines.append(
            f"  OOB  — Feedback: {snap['oob']['feedback_reports_sent']}  │"
            f"  Windows: {snap['oob']['feedback_windows_completed']}  │"
            f"  RIS Upd: {snap['oob']['ris_updates_received']}  │"
            f"  Sched Upd: {snap['oob']['scheduling_updates_received']}  │"
            f"  Rejected: {snap['oob']['control_updates_rejected']}  │"
            f"  Overrides: {snap['oob']['supervisory_overrides']}"
        )
        lines.append(
            f"  Net  — Sent: {snap['network']['bytes_sent'] / 1024:.1f} KB  │"
            f"  Recv: {snap['network']['bytes_received'] / 1024:.1f} KB  │"
            f"  Errors: {snap['network']['connection_errors']}"
        )
        lines.append(
            "  Laptop B — Feedback: "
            + ("CONNECTED" if snap["network"]["laptop_b_feedback_connected"] else "DISCONNECTED")
            + " | Data: "
            + ("CONNECTED" if snap["network"]["laptop_b_data_connected"] else "DISABLED")
        )

        balanced, acct = self.verify_accounting()
        tag = "✓" if balanced else "✗ MISMATCH"
        lines.append(f"  Acct — {tag}  {acct}")
        lines.append("=" * W)

        # Clear screen (Windows / POSIX)
        if os.name == "nt":
            os.system("cls")
        else:
            print("\033[2J\033[H", end="")
        print("\n".join(lines), flush=True)

    # ----- CSV export -----

    def export_csv(self, output_dir: str, run_id: str = "run") -> Tuple[str, str]:
        """Write run_summary.csv and zone_metrics.csv.  Returns (summary_path, zones_path)."""
        os.makedirs(output_dir, exist_ok=True)
        snap = self.snapshot()

        # --- summary ---
        summary_path = os.path.join(output_dir, f"{run_id}_summary.csv")
        with open(summary_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["metric", "value"])
            for k, v in snap["run"].items():
                w.writerow([f"run.{k}", v])
            for k, v in snap["global"].items():
                if isinstance(v, dict):
                    for sk, sv in v.items():
                        w.writerow([f"{k}.{sk}", sv])
                else:
                    w.writerow([k, v])
            for k, v in snap["oob"].items():
                w.writerow([f"oob.{k}", v])
            for k, v in snap["network"].items():
                w.writerow([f"net.{k}", v])

        # --- per-zone ---
        zones_path = os.path.join(output_dir, f"{run_id}_zones.csv")
        with open(zones_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([
                "zone", "generated_pdus", "delivered_pdus", "terminal_drops",
                "final_outcomes",
                "acked", "nacked", "failed_attempts", "retransmitted_pdus", "pdr",
                "mean_latency_ms", "p50_latency_ms", "p95_latency_ms", "p99_latency_ms",
                "queue_ul_pdus", "queue_dl_pdus",
            ])
            for z_name, zd in snap["zones"].items():
                lat = zd["latency"]
                w.writerow([
                    z_name, zd["generated"], zd["delivered"], zd["dropped"],
                    zd["final_outcomes"],
                    zd["acked"], zd["nacked"], zd["attempt_failed"],
                    zd["retransmitted"], zd["pdr"],
                    lat["mean_ms"], lat["p50_ms"], lat["p95_ms"], lat["p99_ms"],
                    zd["queue_ul"], zd["queue_dl"],
                ])

        pdu_events_path = os.path.join(output_dir, f"{run_id}_pdu_events.csv")
        with open(pdu_events_path, "w", newline="", encoding="utf-8") as fh:
            fields = [
                "event", "sim_time_sec", "pdu_id", "src_device", "dst_device",
                "zone", "direction", "payload_size_bytes", "priority", "seq_num",
                "status", "sinr_db_simulated", "per_simulated",
                "latency_ms_simulated", "drop_reason",
            ]
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            with self._lock:
                writer.writerows(self._pdu_events)

        feedback_path = os.path.join(output_dir, f"{run_id}_receiver_feedback.csv")
        with open(feedback_path, "w", newline="", encoding="utf-8") as fh:
            fields = [
                "zone", "seq", "window_start_epoch_sec", "window_end_epoch_sec",
                "queue_occupancy_pdus", "queue_capacity_pdus", "generated_pdus",
                "delivered_pdus", "dropped_pdus", "mean_sinr_db_simulated",
                "per_simulated", "traffic_rate_pps",
            ]
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            with self._lock:
                writer.writerows(self._feedback_rows)

        return summary_path, zones_path

    # ----- reset -----

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            for z in range(self.num_zones):
                self._zone_counters[z].clear()
                self._zone_latency[z].reset()
            self._global_latency.reset()
            self._throughput_window.clear()
            self._queue_depths.clear()
            self._drop_reasons.clear()
            self._pdu_event_sequence = 0
            self._pdu_events.clear()
            self._feedback_rows.clear()
            self._pending_pdus = None
            self._start_wall = time.time()
