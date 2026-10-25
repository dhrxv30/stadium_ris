"""Closed-loop decision engine for Laptop B (Stage B7).

Orchestrates the full feedback → schedule → optimize → OOB-command → evaluate cycle.
All channel evaluations and OOB exchanges are SIMULATED IN SOFTWARE.
"""

import asyncio
import logging
import time
import numpy as np
from typing import Optional, List, Dict

from laptop_b.zone_manager import ZoneManager, CONGESTION_CONGESTED, CONGESTION_SEVERE
from laptop_b.scheduler import CongestionScheduler, SchedulerDecision
from laptop_b.ris_channel import PanelRegistry, ChannelModelParams
from laptop_b.ris_optimizer import RISOptimizer, OptResult, OptimizerPolicy
from laptop_b.oob_controller import OOBController, OOBChannelParams, OOBCommand


# Default stadium geometry (synthetic; configurable via constructor)
_DEFAULT_ZONE_CENTERS = {
    "Z1": np.array([ 40.0,   0.0, 0.0]),
    "Z2": np.array([  0.0,  40.0, 0.0]),
    "Z3": np.array([-40.0,   0.0, 0.0]),
    "Z4": np.array([  0.0, -40.0, 0.0]),
    "Z5": np.array([  0.0,   0.0, 0.0]),   # centre field
}

_DEFAULT_NLOS = {
    "Z1": False,
    "Z2": False,
    "Z3": True,    # example: Z3 has NLoS obstruction by default
    "Z4": False,
    "Z5": False,
}


class ClosedLoopEngine:
    """Implements the 12-step closed-loop workflow from the master spec.

    Step 1-3:  Laptop A generates traffic, estimates conditions (handled by A)
    Step 4:    Laptop A sends feedback to B  ← handled by ControllerB._handle_client
    Step 5-6:  B evaluates zones and runs scheduler + optimizer  ← THIS class
    Step 7-8:  B sends OOB command through simulated channel      ← THIS class
    Step 9-10: RIS model applies config; B receives simulated ACK ← THIS class
    Step 11:   Subsequent traffic evaluated with new channel      ← THIS class
    Step 12:   Laptop C records decisions (via telemetry)        ← telemetry publishing
    """

    def __init__(self,
                 zone_manager: ZoneManager,
                 scheduler: CongestionScheduler,
                 panel_registry: PanelRegistry,
                 optimizer: RISOptimizer,
                 oob_controller: OOBController,
                 logger: Optional[logging.Logger] = None,
                 closed_loop_enabled: bool = True,
                 optimize_interval_s: float = 5.0):
        self.zones = zone_manager
        self.scheduler = scheduler
        self.panels = panel_registry
        self.optimizer = optimizer
        self.oob = oob_controller
        self.log = logger or logging.getLogger("laptop_b.closed_loop")
        self.closed_loop_enabled = closed_loop_enabled
        self.optimize_interval_s = optimize_interval_s

        self._last_optimize_ts: Dict[str, float] = {}
        self._pending_opt_results: Dict[str, OptResult] = {}  # cmd_id -> OptResult
        self._loop_task: Optional[asyncio.Task] = None

    def start(self):
        """Start the background closed-loop task."""
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        """Periodic closed-loop iteration."""
        while True:
            try:
                await asyncio.sleep(self.optimize_interval_s)
                await self._iterate()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.log.error("Closed-loop iteration error: %s", exc, exc_info=True)

    async def _iterate(self):
        """One closed-loop iteration across all zones."""
        if not self.closed_loop_enabled:
            return

        # Step 5: Evaluate zones; identify zones needing action
        zones_to_act = self.zones.zones_needing_action()
        all_decisions = self.scheduler.decide_all(self.zones)

        for dec in all_decisions:
            zone = self.zones.zones.get(dec.zone_id)
            if zone:
                zone.last_decision = f"{dec.action}(w={dec.token_weight:.2f})"
                zone.last_decision_ts = dec.ts

        if not zones_to_act:
            self.log.debug("Closed-loop iteration: no congested zones")
            return

        self.log.info("Closed-loop: congested zones %s; running optimizer", zones_to_act)

        # Step 6: Optimizer selects RIS codebook
        for zone_id in zones_to_act:
            zone = self.zones.zones[zone_id]
            current_sinr = zone.mean_sinr_db  # SIMULATED value from Laptop A feedback

            opt_result = self.optimizer.optimize_zone(
                zone_id, self.panels, current_sinr_db=current_sinr
            )
            if opt_result is None or opt_result.decision != "APPLY":
                continue

            # Step 7: Issue OOB commands for panels in this zone
            zone_panels = self.panels.by_zone(zone_id)
            for panel in zone_panels[:1]:   # issue to first panel per zone (scalable)
                cmd = self.oob.issue_command(
                    panel_id=panel.panel_id,
                    zone_id=zone_id,
                    codebook_index=opt_result.selected_codebook,
                    config_version=panel.config_version + 1,
                    reason=opt_result.reason,
                    opt_id=opt_result.opt_id,
                )
                if cmd is None:
                    continue  # duplicate protection triggered

                # Steps 7-10: Transmit via simulated OOB channel
                asyncio.create_task(
                    self._drive_command(cmd, opt_result, zone_id)
                )

    async def _drive_command(self, cmd: OOBCommand, opt_result: OptResult, zone_id: str):
        """Send command, await simulated ACK, apply panel config, evaluate post-update."""
        success = await self.oob.transmit_and_await_ack(cmd)

        if success:
            # Step 9: Apply configuration to the simulated RIS panel
            applied = self.panels.apply_config(
                cmd.target_panel_id, cmd.codebook_index, cmd.cmd_id
            )
            if applied:
                self.log.info(
                    "Panel %s updated to codebook=%d (simulated, step 9)",
                    cmd.target_panel_id, cmd.codebook_index,
                )
                zone = self.zones.zones.get(zone_id)
                if zone:
                    zone.last_ris_update = (f"cb={cmd.codebook_index} "
                                            f"cmd={cmd.cmd_id[:8]}")
                    zone.last_ris_update_ts = time.time()

                # Step 11: Evaluate post-update channel quality (simulated)
                self.optimizer.evaluate_post_update(zone_id, opt_result, self.panels)
            else:
                self.log.warning("Panel %s apply_config returned False (health?)",
                                 cmd.target_panel_id)
        else:
            self.log.warning(
                "OOB command %s FAILED for panel %s; no configuration change applied",
                cmd.cmd_id, cmd.target_panel_id,
            )
            opt_result.improved = False

    def summary_lines(self) -> List[str]:
        """Return recent closed-loop event lines for terminal display."""
        lines = []
        latest = self.optimizer.latest_results()
        for zid in sorted(latest):
            r = latest[zid]
            imp = ("improved" if r.improved else
                   "degraded" if r.improved is False else
                   "not yet evaluated")
            pre = f"{r.pre_sinr_db:.1f}dB" if r.pre_sinr_db is not None else "N/A"
            post = f"{r.post_sinr_db:.1f}dB" if r.post_sinr_db is not None else "N/A"
            lines.append(
                f"  {zid}: opt={r.decision} cb={r.selected_codebook} "
                f"SINR {pre}→{post} ({imp}) [{r.opt_id[:8]}]"
            )
        return lines if lines else ["  (no optimization results yet)"]
