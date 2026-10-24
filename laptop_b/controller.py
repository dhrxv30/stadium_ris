"""ControllerB: Full B2–B8 TCP listener, message handling, scheduler,
RIS optimizer, OOB controller, closed-loop engine, and reporting for Laptop B.

Real network: A → B messages travel over TCP on the LAN.
Simulated: zones, channel values, the OOB radio link, and RIS configurations
           are all software models.  Nothing is claimed as a physical measurement.
"""
import asyncio
import logging
import time
from collections import Counter
from pathlib import Path

import numpy as np

from common.constants import (MSG_FEEDBACK_REPORT, MSG_HEARTBEAT, PROTOCOL_VERSION)
from common.framing import FrameDecoder, FramingError
from common.messages import MessageError, decode_message
from common.validation import ValidationError, validate_message

from laptop_b.zone_manager import ZoneManager, CongestionPolicy
from laptop_b.ris_channel import PanelRegistry, ChannelModelParams
from laptop_b.ris_optimizer import RISOptimizer, OptimizerPolicy
from laptop_b.oob_controller import OOBController, OOBChannelParams
from laptop_b.scheduler import CongestionScheduler, CongestionSchedulerPolicy, make_scheduler
from laptop_b.closed_loop import ClosedLoopEngine, _DEFAULT_ZONE_CENTERS, _DEFAULT_NLOS
from laptop_b.report_writer import ReportWriter
from laptop_b.zone_manager import ACCEPTED, NA

NA_STR = "N/A"


class ControllerB:
    def __init__(self, cfg, run_id, clock=time.time, logger=None, mode="listen"):
        self.cfg = cfg
        self.run_id = run_id
        self.clock = clock
        self.mode = mode
        self.log = logger or logging.getLogger("laptop_b")

        ris = cfg["ris"]
        net = cfg["network"]
        stad = cfg["stadium"]

        # --- Zone manager (B2) ---
        self.zones = ZoneManager(
            zone_ids=stad["zones"],
            stale_after_s=ris["stale_after_s"],
            clock=clock,
            congestion_policy=CongestionPolicy(),
        )

        # --- Panel registry (B4) ---
        self.panels = PanelRegistry.build(
            zone_ids=stad["zones"],
            zone_panel_counts=ris["zone_panel_counts"],
            initial_codebook_index=ris["initial_codebook_index"],
        )

        # --- Scheduler (B3) ---
        sched_type = cfg.get("ris", {}).get("scheduler", "congestion_aware")
        self.scheduler = make_scheduler(sched_type, logger=self.log)

        # --- Channel model + Optimizer (B4/B5) ---
        channel_params = ChannelModelParams(
            freq_hz=cfg.get("ris", {}).get("freq_hz", 3.5e9),
            path_loss_exp=cfg.get("ris", {}).get("path_loss_exp", 2.5),
            ris_elements_per_panel=cfg.get("ris", {}).get("ris_elements_per_panel", 64),
        )
        self.optimizer = RISOptimizer(
            channel_params=channel_params,
            policy=OptimizerPolicy(
                cooldown_s=cfg.get("ris", {}).get("optimizer_cooldown_s", 5.0),
            ),
            codebook_size=ris["codebook_size"],
            zone_centers=_DEFAULT_ZONE_CENTERS,
            zone_nlos_flags=_DEFAULT_NLOS,
            bs_pos=np.array([0.0, 0.0, 10.0]),
            seed=net["run"]["seed"],
            logger=self.log,
        )

        # --- OOB controller (B6) ---
        oob_params = OOBChannelParams(
            delay_s=cfg.get("ris", {}).get("oob_delay_s", 0.05),
            loss_probability=cfg.get("ris", {}).get("oob_loss_prob", 0.05),
            retry_limit=cfg.get("ris", {}).get("oob_retry_limit", 3),
            timeout_s=cfg.get("ris", {}).get("oob_timeout_s", 2.0),
        )
        self.oob = OOBController(
            params=oob_params,
            seed=net["run"]["seed"],
            logger=self.log,
            on_acked=self._on_oob_acked,
            on_failed=self._on_oob_failed,
        )

        # --- Closed-loop engine (B7) ---
        self.closed_loop = ClosedLoopEngine(
            zone_manager=self.zones,
            scheduler=self.scheduler,
            panel_registry=self.panels,
            optimizer=self.optimizer,
            oob_controller=self.oob,
            logger=self.log,
            closed_loop_enabled=(mode != "baseline"),
            optimize_interval_s=cfg.get("ris", {}).get("optimizer_interval_s", 5.0),
        )

        # --- Networking ---
        self.max_bytes = net["max_message_bytes"]
        self.idle_timeout = net["idle_timeout_s"]
        self.stats = Counter()
        self.expected_run_id = None
        self._conns = {}
        self._server = None
        self._start_ts = clock()

    # ---------------------------------------------------------------- OOB callbacks

    def _on_oob_acked(self, cmd):
        self.log.info("[OOB ACK] cmd=%s panel=%s codebook=%d (simulated)",
                      cmd.cmd_id, cmd.target_panel_id, cmd.codebook_index)

    def _on_oob_failed(self, cmd):
        self.log.warning("[OOB FAIL] cmd=%s panel=%s after %d retries",
                         cmd.cmd_id, cmd.target_panel_id, cmd.retry_count)

    # ---------------------------------------------------------------- lifecycle

    async def start(self, host, port) -> int:
        self._server = await asyncio.start_server(self._handle_client, host, port)
        actual = self._server.sockets[0].getsockname()[1]
        self.log.info("Laptop B listening on %s:%d (TCP, length-prefixed JSON)", host, actual)
        self.closed_loop.start()
        return actual

    async def stop(self):
        await self.closed_loop.stop()
        if self._server is not None:
            self._server.close()
            for w in list(self._conns):
                w.close()
            await self._server.wait_closed()
            self._server = None
        self.log.info("Laptop B listener stopped")

    # ---------------------------------------------------------------- connection handling

    async def _handle_client(self, reader, writer):
        peer = writer.get_extra_info("peername")
        self._conns[writer] = None
        self.stats["connections_total"] += 1
        self.log.info("connection opened from %s", peer)
        decoder = FrameDecoder(self.max_bytes)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(65536), self.idle_timeout)
                except asyncio.TimeoutError:
                    self.log.warning("idle timeout, closing %s", peer)
                    break
                if not data:
                    break
                try:
                    frames = decoder.feed(data)
                except FramingError as exc:
                    self.stats["framing_errors"] += 1
                    self.log.error("framing error from %s: %s; closing connection", peer, exc)
                    break
                for raw in frames:
                    src = self.handle_frame(raw)
                    if src:
                        self._conns[writer] = src
        except (ConnectionError, OSError) as exc:
            self.log.warning("connection error from %s: %s", peer, exc)
        finally:
            self._conns.pop(writer, None)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            self.log.info("connection closed from %s", peer)

    # ---------------------------------------------------------------- message handling

    def handle_frame(self, raw: bytes):
        """Process one frame. Never raises. Returns sender label if valid."""
        self.stats["frames_received"] += 1
        try:
            msg = decode_message(raw)
            validate_message(msg, PROTOCOL_VERSION)
        except (MessageError, ValidationError) as exc:
            self.stats["malformed"] += 1
            self.log.warning("malformed message rejected: %s", exc)
            return None

        if msg["dst"] != "B":
            self.stats["malformed"] += 1
            self.log.warning("message addressed to %s received by B; rejected", msg["dst"])
            return None

        if self.expected_run_id is None:
            self.expected_run_id = msg["run_id"]
            self.log.info("adopted run_id %s from %s", msg["run_id"], msg["src"])
        elif msg["run_id"] != self.expected_run_id:
            self.stats["rejected"] += 1
            self.stats["rejected_run_id"] += 1
            self.log.warning("run_id %s != expected %s; rejected", msg["run_id"], self.expected_run_id)
            return msg["src"]

        if msg["type"] == MSG_HEARTBEAT:
            self.stats["heartbeats"] += 1
        elif msg["type"] == MSG_FEEDBACK_REPORT:
            status, reason = self.zones.process_feedback(msg)
            self.stats["feedback_received"] += 1
            if status == ACCEPTED:
                self.stats["feedback_accepted"] += 1
                zone_id = msg.get("zone_id")
                self.log.debug("%s seq=%s accepted", zone_id, msg["payload"]["seq"])
            else:
                self.stats["rejected"] += 1
                key = status.lower()
                self.stats[key] += 1
                self.log.warning("%s feedback %s: %s", msg.get("zone_id"), status, reason)

        return msg["src"]

    # ---------------------------------------------------------------- CSV export (B8)

    def write_reports(self, run_dir: Path):
        """Write all B8 CSV reports to run_dir/."""
        writer = ReportWriter(run_dir, self.run_id)
        elapsed = self.clock() - self._start_ts
        sched = self.scheduler if isinstance(self.scheduler, CongestionScheduler) else None
        if sched is None:
            # baseline scheduler has no history; wrap minimally
            class _FakeSched:
                NAME = "BASELINE"
                decision_history = []
            sched = _FakeSched()
        writer.write_all(
            elapsed_s=elapsed,
            ctrl_stats=dict(self.stats),
            zone_manager=self.zones,
            panel_registry=self.panels,
            oob_controller=self.oob,
            scheduler=sched,
            optimizer=self.optimizer,
        )
        self.log.info("CSV reports written to %s", run_dir)

    # ---------------------------------------------------------------- reporting

    def link_status(self, node):
        return "CONNECTED" if node in self._conns.values() else "DISCONNECTED"

    def startup_summary(self) -> str:
        lines = [
            "=" * 60,
            f"LAPTOP B - RIS / OOB CONTROLLER   run={self.run_id}   mode={self.mode}",
            "NOTE: RIS panels, channel model, and OOB link are SIMULATED in software.",
            f"Zones: {len(self.zones.zones)}   RIS panels: {self.panels.total} (virtual, passive)",
            f"Scheduler: {getattr(self.scheduler, 'NAME', 'UNKNOWN')}",
            f"OOB: delay={self.oob.params.delay_s}s  loss={self.oob.params.loss_probability:.0%}  "
            f"retries={self.oob.params.retry_limit}",
            "-" * 60,
        ]
        for zid in self.zones.zones:
            ids = [p.panel_id for p in self.panels.by_zone(zid)]
            nlos = "NLoS" if _DEFAULT_NLOS.get(zid) else "LoS"
            lines.append(f"  {zid}: {len(ids):2d} panels [{ids[0]}..{ids[-1]}]  {nlos}")
        lines.append("=" * 60)
        return "\n".join(lines)

    def status_summary(self) -> str:
        s = self.stats
        elapsed = self.clock() - self._start_ts
        zone_lines = self.zones.zone_summary_lines()
        oob_line = self.oob.stats_summary()
        cl_lines = self.closed_loop.summary_lines()
        sched_line = (self.scheduler.summary_line()
                      if hasattr(self.scheduler, "summary_line")
                      else f"Scheduler[{getattr(self.scheduler,'NAME','?')}]")

        return "\n".join([
            f"[LAPTOP B] RUN={self.run_id} ELAPSED={elapsed:.1f}s",
            f"Laptop A link={self.link_status('A')} | Laptop C link=N/A (telemetry: C-stage)",
            f"Feedback received={s['feedback_received']} accepted={s['feedback_accepted']} | "
            f"Stale/rejected={s['rejected']} | Malformed={s['malformed']} | "
            f"Framing errors={s['framing_errors']}",
            "ZONES:",
            *zone_lines,
            f"RIS panels: total={self.panels.total} | active configs={self.panels.active_configs} | "
            f"pending commands={self.panels.pending_commands}",
            oob_line,
            sched_line,
            "CLOSED-LOOP (simulated):",
            *cl_lines,
        ])
