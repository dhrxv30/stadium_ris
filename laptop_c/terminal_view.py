"""Terminal supervisor interface for Laptop C (Stage C3).

Provides:
- Compact, continuously refreshed multi-pane terminal dashboard
- Five-zone status table with queue occupancy, SINR, PER, and congestion state
- 32-panel virtual passive RIS status grid
- Filterable event stream (filter by zone, panel, command ID, PDU ID, event type)
- Interactive commands (pause/resume, verbosity, snapshot, zone inspection)
"""
import io
import sys
import time
from typing import Any, Dict, List, Optional

from common.constants import NUM_PANELS, ZONE_IDS
from laptop_c.supervisor import Supervisor, fmt_val


class TerminalView:
    """Renders the Stage C3 supervisor dashboard to the terminal."""

    def __init__(
        self,
        supervisor: Supervisor,
        filter_zone: Optional[str] = None,
        filter_panel: Optional[str] = None,
        filter_cmd: Optional[str] = None,
        filter_pdu: Optional[str] = None,
        filter_type: Optional[str] = None,
        verbosity: str = "normal",  # quiet, normal, debug
        max_events: int = 6,
    ):
        self.sup = supervisor
        self.filter_zone = filter_zone
        self.filter_panel = filter_panel
        self.filter_cmd = filter_cmd
        self.filter_pdu = filter_pdu
        self.filter_type = filter_type
        self.verbosity = verbosity
        self.max_events = max_events
        self.is_paused = False

    def pause(self):
        self.is_paused = True

    def resume(self):
        self.is_paused = False

    def set_filter_zone(self, zone_id: Optional[str]):
        self.filter_zone = zone_id

    def set_filter_panel(self, panel_id: Optional[str]):
        self.filter_panel = panel_id

    def set_filter_cmd(self, cmd_id: Optional[str]):
        self.filter_cmd = cmd_id

    def set_filter_pdu(self, pdu_id: Optional[str]):
        self.filter_pdu = pdu_id

    def set_verbosity(self, level: str):
        if level in ("quiet", "normal", "debug"):
            self.verbosity = level

    def clear_filters(self):
        self.filter_zone = None
        self.filter_panel = None
        self.filter_cmd = None
        self.filter_pdu = None
        self.filter_type = None

    def _matches_filters(self, evt: dict) -> bool:
        """Check if an event matches the active filtering criteria."""
        if self.filter_zone and evt.get("zone_id") != self.filter_zone:
            return False
        if self.filter_panel and evt.get("panel_id") != self.filter_panel:
            return False
        if self.filter_cmd and evt.get("command_id") != self.filter_cmd:
            return False
        if self.filter_pdu and evt.get("pdu_id") != self.filter_pdu:
            return False
        if self.filter_type and evt.get("event_type") != self.filter_type:
            return False
        return True

    def render_to_string(self) -> str:
        """Render the complete terminal supervisor dashboard into a formatted string."""
        buf = io.StringIO()
        pdu = self.sup.get_pdu_summary()
        oob = self.sup.get_oob_summary()
        pop = self.sup.device_population
        h = self.sup.peer_health

        status_flag = " [PAUSED]" if self.is_paused else ""
        buf.write("=" * 72 + "\n")
        buf.write(f"========== STADIUM RIS SUPERVISOR{status_flag} ==========\n")
        buf.write(f"RUN: {self.sup.run_id or 'N/A':<30} ELAPSED: {self.sup.elapsed_s:.1f}s\n")
        buf.write(f"Laptop A: {h.get('A', 'UNKNOWN'):<18} Laptop B: {h.get('B', 'UNKNOWN'):<18} Laptop C: {h.get('C', 'CONNECTED')}\n")
        buf.write(f"Logical devices: {pop['total']} (Senders: {pop['senders']} | Receivers: {pop['receivers']}) | Zones: 5 | RIS Panels: 32\n")
        buf.write("-" * 72 + "\n")

        # PDU Accounting
        buf.write("TRAFFIC & PDU ACCOUNTING\n")
        buf.write(
            f"  Uplink:   gen={pdu['uplink']['generated']:<6} del={pdu['uplink']['delivered']:<6} "
            f"drop={pdu['uplink']['dropped']:<5} pend={pdu['uplink']['pending']:<5} tmo={pdu['uplink']['timed_out']}\n"
        )
        buf.write(
            f"  Downlink: gen={pdu['downlink']['generated']:<6} del={pdu['downlink']['delivered']:<6} "
            f"drop={pdu['downlink']['dropped']:<5} pend={pdu['downlink']['pending']:<5} tmo={pdu['downlink']['timed_out']}\n"
        )
        buf.write(
            f"  Total:    PDR={fmt_val(pdu['pdr_pct'], fmt='.1f', unit='%'):<8} PLR={fmt_val(pdu['plr_pct'], fmt='.1f', unit='%'):<8} "
            f"ACK={pdu['ack_count']:<6} NACK={pdu['nack_count']:<5} Retries={pdu['retry_count']}\n"
        )
        buf.write(
            f"  Quality:  Mean Lat={fmt_val(self.sup.overall_mean_latency_ms, fmt='.1f', unit='ms'):<10} "
            f"P95 Lat={fmt_val(self.sup.overall_p95_latency_ms, fmt='.1f', unit='ms'):<10} "
            f"Throughput={fmt_val(self.sup.overall_throughput_bps, fmt='.0f', unit=' bps')}\n"
        )

        # OOB Link Status
        buf.write("-" * 72 + "\n")
        buf.write("OUT-OF-BAND (OOB) CONTROL LINK\n")
        buf.write(
            f"  TX Attempts: {oob['tx_attempts']:<6} Unique Cmds: {oob['unique_commands']:<6} "
            f"ACK: {oob['acknowledged']:<5} NACK: {oob['negative_acknowledged']:<5} Retries: {oob['retries']:<5} Tmo: {oob['timeouts']}\n"
        )
        buf.write(
            f"  ACK Ratio:   {fmt_val(oob['ack_ratio_pct'], fmt='.1f', unit='%'):<10} "
            f"Last Cmd: {oob['last_command_id'] or 'N/A'} ({oob['last_command_status'] or 'N/A'})\n"
        )

        # Zone Status
        buf.write("-" * 72 + "\n")
        buf.write("ZONE STATUS (Z1 - Z5)\n")
        buf.write(f"  {'Zone':<6} {'State':<11} {'Queue':<10} {'Cap%':<8} {'SINR':<9} {'Est PER':<10} {'PDR%':<8} {'Last Update':<10}\n")
        for zid in ZONE_IDS:
            z = self.sup.zones[zid]
            q_str = f"{fmt_val(z.queue_occupancy)}/{fmt_val(z.queue_capacity)}"
            cap_str = fmt_val(z.queue_occupancy_pct, fmt=".1f", unit="%")
            sinr_str = fmt_val(z.sinr_db, fmt=".1f", unit="dB")
            per_str = fmt_val(z.estimated_per, fmt=".3f")
            pdr_str = fmt_val(z.pdr, fmt=".1f", unit="%")
            upd_str = z.last_ris_update or "None"
            buf.write(f"  {zid:<6} {z.congestion_state:<11} {q_str:<10} {cap_str:<8} {sinr_str:<9} {per_str:<10} {pdr_str:<8} {upd_str:<10}\n")

        # RIS Panels Grid Summary
        buf.write("-" * 72 + "\n")
        reconfigured = [p for p in self.sup.panels.values() if p.current_config != "CB_0"]
        pending = [p for p in self.sup.panels.values() if p.command_status == "PENDING"]
        degraded = [p for p in self.sup.panels.values() if p.health_state != "OK"]
        buf.write(
            f"RIS PANELS (Total: 32) | Active Reconfigured: {len(reconfigured)} | Pending: {len(pending)} | Faults: {len(degraded)}\n"
        )
        if reconfigured and self.verbosity in ("normal", "debug"):
            sample = reconfigured[:6]
            rec_strs = [f"{p.panel_id}({p.zone_id}:{p.current_config}:v{p.config_version})" for p in sample]
            buf.write("  Active: " + ", ".join(rec_strs) + ("..." if len(reconfigured) > 6 else "") + "\n")

        # Latest Decisions
        if self.sup.recent_decisions:
            ld = self.sup.recent_decisions[-1]
            buf.write(
                f"LATEST CONTROLLER DECISION: {ld['decision']} | Zone: {ld.get('zone_id', 'N/A')} | "
                f"Panel: {ld.get('panel_id', 'N/A')} | Reason: {ld.get('reason', 'N/A')}\n"
            )

        # Filtered Event Stream
        buf.write("-" * 72 + "\n")
        filter_desc = []
        if self.filter_zone: filter_desc.append(f"Zone={self.filter_zone}")
        if self.filter_panel: filter_desc.append(f"Panel={self.filter_panel}")
        if self.filter_cmd: filter_desc.append(f"Cmd={self.filter_cmd}")
        if self.filter_pdu: filter_desc.append(f"PDU={self.filter_pdu}")
        if self.filter_type: filter_desc.append(f"Type={self.filter_type}")
        f_str = f" [Filters: {', '.join(filter_desc)}]" if filter_desc else " [All Events]"

        buf.write(f"LATEST EVENTS{f_str}\n")
        matched_events = [e for e in self.sup.recent_events if self._matches_filters(e)]
        if not matched_events:
            buf.write("  (No recent matching events)\n")
        else:
            for e in list(matched_events)[-self.max_events:]:
                t_str = time.strftime("%H:%M:%S", time.localtime(e.get("timestamp", time.time())))
                z_str = f"[{e['zone_id']}] " if e.get("zone_id") else ""
                p_str = f"[{e['panel_id']}] " if e.get("panel_id") else ""
                c_str = f"cmd={e['command_id']} " if e.get("command_id") else ""
                pdu_str = f"pdu={e['pdu_id']} " if e.get("pdu_id") else ""
                buf.write(f"  [{t_str}] {e.get('source', '?')}: {e.get('event_type', 'EVENT')} {z_str}{p_str}{c_str}{pdu_str}{e.get('details', '')}\n")

        buf.write("=" * 72 + "\n")
        return buf.getvalue()

    def display(self):
        """Print the rendered dashboard to stdout."""
        if self.is_paused:
            return
        output = self.render_to_string()
        sys.stdout.write(output)
        sys.stdout.flush()

    def inspect_zone(self, zone_id: str) -> str:
        """Return detailed diagnostic string for a single zone."""
        z = self.sup.get_zone(zone_id)
        if not z:
            return f"Error: Zone '{zone_id}' not found. Available: {', '.join(ZONE_IDS)}"
        out = [
            f"=== ZONE {zone_id} DETAILED INSPECTION ===",
            f"Population:      Senders={z.senders}, Receivers={z.receivers}",
            f"Congestion:      {z.congestion_state}",
            f"Traffic UL:      gen={z.generated_ul}, del={z.delivered_ul}, drop={z.dropped_ul}, pend={z.pending_ul}",
            f"Traffic DL:      gen={z.generated_dl}, del={z.delivered_dl}, drop={z.dropped_dl}, pend={z.pending_dl}",
            f"PDR:             {fmt_val(z.pdr, fmt='.2f', unit='%')}",
            f"PLR:             {fmt_val(z.plr, fmt='.2f', unit='%')}",
            f"Queue:           Occupancy={fmt_val(z.queue_occupancy)} / Capacity={fmt_val(z.queue_capacity)} ({fmt_val(z.queue_occupancy_pct, fmt='.1f', unit='%')})",
            f"Link Conditions: SINR={fmt_val(z.sinr_db, fmt='.1f', unit='dB')}, PER={fmt_val(z.estimated_per, fmt='.4f')}",
            f"Latency:         Mean={fmt_val(z.mean_latency_ms, fmt='.1f', unit='ms')}, P95={fmt_val(z.p95_latency_ms, fmt='.1f', unit='ms')}",
            f"Throughput:      {fmt_val(z.throughput_bps, fmt='.0f', unit=' bps')}",
            f"Last Controller: Decision='{z.last_decision or 'None'}', Applied RIS='{z.last_ris_update or 'None'}'",
        ]
        return "\n".join(out)

    def inspect_panel(self, panel_id: str) -> str:
        """Return detailed diagnostic string for a single RIS panel."""
        p = self.sup.get_panel(panel_id)
        if not p:
            return f"Error: Panel '{panel_id}' not found. Range: P1 to P{NUM_PANELS}"
        out = [
            f"=== RIS PANEL {panel_id} DETAILED INSPECTION ===",
            f"Associated Zone:       {p.zone_id}",
            f"Current Configuration: {p.current_config}",
            f"Configuration Version: {p.config_version}",
            f"Command Status:        {p.command_status}",
            f"Simulated Health:      {p.health_state}",
            f"Last Update Wall Time: {time.ctime(p.last_update_ts) if p.last_update_ts else 'N/A'}",
        ]
        return "\n".join(out)
