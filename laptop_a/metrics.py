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

    def __init__(self, num_zones: int = 5):
        self.num_zones = num_zones
        self._lock = threading.Lock()

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

    def record_drop(self, reason: str) -> None:
        with self._lock:
            self._drop_reasons[reason] += 1
            self._counters["pdu_dropped"] += 1

    def set_queue_depth(self, queue_name: str, depth: int) -> None:
        with self._lock:
            self._queue_depths[queue_name] = depth

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
            queued = self._counters.get("pdu_queued", 0)
            dequeued = self._counters.get("pdu_dequeued", 0)
            pending = max(queued - dequeued, 0)

            pdr = delivered / gen if gen > 0 else 0.0
            loss = dropped / gen if gen > 0 else 0.0

            zone_data = {}
            for z in range(self.num_zones):
                zc = self._zone_counters[z]
                z_gen = zc.get("pdu_generated", 0)
                z_del = zc.get("pdu_delivered", 0)
                z_drop = zc.get("pdu_dropped", 0)
                z_retx = zc.get("pdu_retransmitted", 0)
                zone_data[f"Z{z + 1}"] = {
                    "generated": z_gen,
                    "delivered": z_del,
                    "dropped": z_drop,
                    "retransmitted": z_retx,
                    "pdr": round(z_del / z_gen, 4) if z_gen > 0 else 0.0,
                    "latency": self._zone_latency[z].snapshot(),
                    "queue_ul": self._queue_depths.get(f"ul_z{z}", 0),
                    "queue_dl": self._queue_depths.get(f"dl_z{z}", 0),
                }

            return {
                "timestamp": time.time(),
                "elapsed_sec": round(elapsed, 2),
                "global": {
                    "pdu_generated": gen,
                    "pdu_delivered": delivered,
                    "pdu_dropped": dropped,
                    "pdu_retransmitted": self._counters.get("pdu_retransmitted", 0),
                    "pdu_pending": pending,
                    "pdr": round(pdr, 4),
                    "loss_ratio": round(loss, 4),
                    "throughput_bps": round(self._throughput_bps_unlocked()),
                    "latency": self._global_latency.snapshot(),
                    "drop_reasons": dict(self._drop_reasons),
                },
                "zones": zone_data,
                "oob": {
                    "feedback_reports_sent": self._counters.get("feedback_sent", 0),
                    "ris_updates_received": self._counters.get("ris_updates_received", 0),
                    "supervisory_overrides": self._counters.get("supervisory_overrides", 0),
                },
                "network": {
                    "bytes_sent": self._counters.get("net_bytes_sent", 0),
                    "bytes_received": self._counters.get("net_bytes_received", 0),
                    "messages_sent": self._counters.get("net_msg_sent", 0),
                    "messages_received": self._counters.get("net_msg_received", 0),
                    "connection_errors": self._counters.get("net_conn_errors", 0),
                },
            }

    # ----- strict accounting verification -----

    def verify_accounting(self) -> Tuple[bool, str]:
        """
        Verify: generated == delivered + dropped + pending.
        Returns (is_balanced, human_readable_message).
        """
        gen = self.get_counter("pdu_generated")
        delivered = self.get_counter("pdu_delivered")
        dropped = self.get_counter("pdu_dropped")
        pending = max(self.get_counter("pdu_queued") - self.get_counter("pdu_dequeued"), 0)
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
        if not force and (now - self._last_dashboard_wall) < 0.95:
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
            f"  RIS Upd: {snap['oob']['ris_updates_received']}  │"
            f"  Overrides: {snap['oob']['supervisory_overrides']}"
        )
        lines.append(
            f"  Net  — Sent: {snap['network']['bytes_sent'] / 1024:.1f} KB  │"
            f"  Recv: {snap['network']['bytes_received'] / 1024:.1f} KB  │"
            f"  Errors: {snap['network']['connection_errors']}"
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
                "zone", "generated", "delivered", "dropped", "retransmitted",
                "pdr", "mean_lat_ms", "p50_lat_ms", "p95_lat_ms", "p99_lat_ms",
                "queue_ul", "queue_dl",
            ])
            for z_name, zd in snap["zones"].items():
                lat = zd["latency"]
                w.writerow([
                    z_name, zd["generated"], zd["delivered"], zd["dropped"],
                    zd["retransmitted"], zd["pdr"],
                    lat["mean_ms"], lat["p50_ms"], lat["p95_ms"], lat["p99_ms"],
                    zd["queue_ul"], zd["queue_dl"],
                ])

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
            self._start_wall = time.time()
