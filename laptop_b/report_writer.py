"""CSV and terminal report writer for Laptop B (Stage B8).

Writes:
  - oob_events.csv
  - ris_panel_states.csv
  - controller_decisions.csv
  - zone_state_history.csv
  - run_summary.csv

All exported values are computed from actual application state counters.
No fabricated or placeholder values are inserted.
"""

import csv
import os
import time
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from laptop_b.zone_manager import ZoneManager
    from laptop_b.ris_channel import PanelRegistry
    from laptop_b.oob_controller import OOBController
    from laptop_b.scheduler import CongestionScheduler


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def _open_csv(path: Path, headers: List[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w", newline="", encoding="utf-8")
    w = csv.DictWriter(f, fieldnames=headers)
    w.writeheader()
    return f, w


def _na(v, fmt=None):
    """Return formatted value or empty string for None."""
    if v is None:
        return ""
    return fmt % v if fmt else str(v)


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

class ReportWriter:
    """Writes Laptop B CSV reports to run_dir/."""

    OOB_HEADERS = [
        "ts", "cmd_id", "panel_id", "zone_id", "codebook_index",
        "config_version", "status", "retry_count", "ack_id",
        "created_ts", "sent_ts", "acked_ts", "failed_ts",
        "opt_id", "reason", "last_error",
    ]
    PANEL_HEADERS = [
        "ts", "panel_id", "zone_id", "codebook_index", "config_version",
        "command_status", "last_command_id", "health",
        "last_sinr_db", "last_per",
    ]
    DECISION_HEADERS = [
        "ts", "zone_id", "scheduler_type", "action", "token_weight",
        "congestion_state", "queue_ratio", "pdr", "mean_sinr_db", "reason",
    ]
    ZONE_HISTORY_HEADERS = [
        "ts", "zone_id", "seq", "queue_occupancy", "queue_capacity",
        "generated", "delivered", "dropped",
        "traffic_rate_pps", "mean_sinr_db", "per", "pdr", "congestion_state",
    ]
    RUN_SUMMARY_HEADERS = [
        "run_id", "elapsed_s", "feedback_received", "feedback_accepted",
        "rejected_duplicate", "rejected_stale", "malformed",
        "oob_tx_attempts", "oob_acked", "oob_failed", "oob_retries",
        "opt_no_change", "opt_apply", "total_zones", "total_panels",
        "scheduler_type", "notes",
    ]

    def __init__(self, run_dir: Path, run_id: str):
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

    # ---------- OOB events (append on each call) ----------

    def write_oob_events(self, oob_controller: "OOBController"):
        path = self.run_dir / "oob_events.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.OOB_HEADERS)
            w.writeheader()
            for cmd in oob_controller.all_commands():
                w.writerow({
                    "ts": _na(cmd.created_ts, "%.3f"),
                    "cmd_id": cmd.cmd_id,
                    "panel_id": cmd.target_panel_id,
                    "zone_id": cmd.zone_id,
                    "codebook_index": cmd.codebook_index,
                    "config_version": cmd.config_version,
                    "status": cmd.status.value,
                    "retry_count": cmd.retry_count,
                    "ack_id": cmd.ack_id or "",
                    "created_ts": _na(cmd.created_ts, "%.3f"),
                    "sent_ts": _na(cmd.sent_ts, "%.3f"),
                    "acked_ts": _na(cmd.acked_ts, "%.3f"),
                    "failed_ts": _na(cmd.failed_ts, "%.3f"),
                    "opt_id": cmd.opt_id or "",
                    "reason": cmd.reason,
                    "last_error": cmd.last_error or "",
                })

    # ---------- Panel states snapshot ----------

    def write_panel_states(self, panel_registry: "PanelRegistry"):
        path = self.run_dir / "ris_panel_states.csv"
        now = time.time()
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.PANEL_HEADERS)
            w.writeheader()
            for pid, p in panel_registry.panels.items():
                w.writerow({
                    "ts": f"{now:.3f}",
                    "panel_id": pid,
                    "zone_id": p.zone_id,
                    "codebook_index": _na(p.codebook_index),
                    "config_version": p.config_version,
                    "command_status": p.command_status,
                    "last_command_id": p.last_command_id or "",
                    "health": p.health,
                    "last_sinr_db": _na(p.last_sinr_db, "%.2f"),
                    "last_per": _na(p.last_per, "%.4f"),
                })

    # ---------- Scheduler decisions ----------

    def write_decisions(self, scheduler: "CongestionScheduler"):
        path = self.run_dir / "controller_decisions.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.DECISION_HEADERS)
            w.writeheader()
            for dec in scheduler.decision_history:
                w.writerow({
                    "ts": f"{dec.ts:.3f}",
                    "zone_id": dec.zone_id,
                    "scheduler_type": dec.scheduler_type,
                    "action": dec.action,
                    "token_weight": f"{dec.token_weight:.4f}",
                    "congestion_state": dec.congestion_state,
                    "queue_ratio": _na(dec.queue_ratio, "%.4f"),
                    "pdr": _na(dec.pdr, "%.4f"),
                    "mean_sinr_db": _na(dec.mean_sinr_db, "%.2f"),
                    "reason": dec.reason,
                })

    # ---------- Zone history ----------

    def write_zone_history(self, zone_manager: "ZoneManager"):
        path = self.run_dir / "zone_state_history.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.ZONE_HISTORY_HEADERS)
            w.writeheader()
            for zid, zone in zone_manager.zones.items():
                for snap in zone.history:
                    w.writerow({
                        "ts": f"{snap.ts:.3f}",
                        "zone_id": zid,
                        "seq": snap.seq,
                        "queue_occupancy": snap.queue_occupancy,
                        "queue_capacity": snap.queue_capacity,
                        "generated": snap.generated,
                        "delivered": snap.delivered,
                        "dropped": snap.dropped,
                        "traffic_rate_pps": _na(snap.traffic_rate_pps, "%.2f"),
                        "mean_sinr_db": _na(snap.mean_sinr_db, "%.2f"),
                        "per": _na(snap.per, "%.4f"),
                        "pdr": _na(snap.pdr, "%.4f"),
                        "congestion_state": snap.congestion_state,
                    })

    # ---------- Run summary ----------

    def write_run_summary(self, elapsed_s: float, ctrl_stats: dict,
                          zone_manager: "ZoneManager",
                          panel_registry: "PanelRegistry",
                          oob_controller: "OOBController",
                          scheduler_type: str,
                          optimizer_history: list):
        path = self.run_dir / "run_summary.csv"
        opt_apply = sum(1 for r in optimizer_history if r.decision == "APPLY")
        opt_no_change = sum(1 for r in optimizer_history if r.decision == "NO_CHANGE")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.RUN_SUMMARY_HEADERS)
            w.writeheader()
            w.writerow({
                "run_id": self.run_id,
                "elapsed_s": f"{elapsed_s:.1f}",
                "feedback_received": ctrl_stats.get("feedback_received", 0),
                "feedback_accepted": ctrl_stats.get("feedback_accepted", 0),
                "rejected_duplicate": ctrl_stats.get("rejected_duplicate", 0),
                "rejected_stale": ctrl_stats.get("rejected_stale", 0),
                "malformed": ctrl_stats.get("malformed", 0),
                "oob_tx_attempts": oob_controller.stats["tx_attempts"],
                "oob_acked": oob_controller.stats["commands_acked"],
                "oob_failed": oob_controller.stats["commands_failed"],
                "oob_retries": oob_controller.stats["retries"],
                "opt_no_change": opt_no_change,
                "opt_apply": opt_apply,
                "total_zones": len(zone_manager.zones),
                "total_panels": panel_registry.total,
                "scheduler_type": scheduler_type,
                "notes": ("All RIS/channel values are SIMULATED. "
                          "OOB link is software simulation, not physical Sub-GHz radio."),
            })

    def write_all(self, elapsed_s, ctrl_stats, zone_manager,
                  panel_registry, oob_controller, scheduler, optimizer):
        """Write all CSV reports at once (e.g. at shutdown)."""
        self.write_oob_events(oob_controller)
        self.write_panel_states(panel_registry)
        self.write_decisions(scheduler)
        self.write_zone_history(zone_manager)
        self.write_run_summary(
            elapsed_s, ctrl_stats, zone_manager, panel_registry,
            oob_controller, scheduler.NAME, optimizer.history,
        )
