"""
main.py — BTS, Traffic Generation & User-Data Gateway Runtime (Laptop A)

Entry point and orchestrator for Laptop A in the 3-Laptop Closed-Loop
OOB-Controlled RIS Network for High-Density Stadium Traffic Management.

Architecture:
  • DevicePopulation  — 50,000 vectorised logical endpoints across 5 stadium zones.
  • TrafficGenerator  — Event-driven uplink PDU generation engine with presets.
  • PDUGateway        — Bounded UL & DL per-zone queues with priority scheduling.
  • ReceiverModel     — 3.5 GHz stadium wireless channel with 32 RIS panels, fading & NLoS.
  • ReceiverFeedback  — Aggregated OOB feedback report generator.
  • MetricsRegistry   — Real-time accounting, terminal TUI dashboard & CSV export.

Communication Interfaces:
  1. Data Plane (TCP):
     - Streams uplink PDUs to Laptop B.
     - Receives scheduled downlink PDUs from Laptop B.
  2. OOB Control Plane (TCP/UDP Sub-GHz simulation):
     - Sends periodic RECEIVER_FEEDBACK_REPORT to Laptop B.
     - Receives RIS_STATE_UPDATE & SCHEDULING_UPDATE from Laptop B.
     - Sends ACKs for control commands.
  3. Supervisory & Telemetry Plane (TCP):
     - Streams live telemetry to Laptop C (Central Supervisor & 3D Visualizer).
     - Receives supervisory commands (profile changes, rate scaling, pause/resume).
  4. Standalone / Loopback Mode:
     - Automatically loops traffic internally when running in standalone mode,
       allowing full closed-loop testing and verification without external laptops.
"""

import sys
import os
import time
import json
import socket
import select
import signal
import threading
import argparse
import numpy as np
from typing import Dict, List, Optional, Tuple, Any

# Ensure current directory is in python path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from device_population import DevicePopulation, DEFAULT_ZONE_GEOMETRIES
from traffic_generator import TrafficGenerator, TrafficProfile, ProfileType, PRESET_PROFILES
from pdu_gateway import PDUGateway, DropPolicy, SchedulePolicy
from receiver_model import ReceiverModel, ChannelConfig, DeliveryResult
from receiver_feedback import ReceiverFeedback
from metrics import MetricsRegistry


# ---------------------------------------------------------------------------
#  Framed JSON Socket Helper (Length-Prefixed Framing)
# ---------------------------------------------------------------------------

def send_framed_json(sock: socket.socket, data: Any) -> bool:
    """Send length-prefixed JSON object over socket: [4 bytes big-endian length][UTF-8 payload]."""
    try:
        payload = json.dumps(data).encode("utf-8")
        header = len(payload).to_bytes(4, byteorder="big")
        sock.sendall(header + payload)
        return True
    except Exception:
        return False


def read_framed_json(sock: socket.socket, buffer: bytearray) -> List[Any]:
    """Read length-prefixed JSON objects from socket and append remaining bytes to buffer."""
    messages = []
    try:
        chunk = sock.recv(65536)
        if not chunk:
            return messages
        buffer.extend(chunk)
        while len(buffer) >= 4:
            msg_len = int.from_bytes(buffer[:4], byteorder="big")
            if len(buffer) < 4 + msg_len:
                break
            msg_bytes = buffer[4:4 + msg_len]
            del buffer[:4 + msg_len]
            try:
                msg = json.loads(msg_bytes.decode("utf-8"))
                messages.append(msg)
            except json.JSONDecodeError:
                pass
    except (BlockingIOError, socket.error):
        pass
    return messages


# ---------------------------------------------------------------------------
#  Laptop A Orchestrator & Node Controller
# ---------------------------------------------------------------------------

class LaptopASystem:
    """
    Complete BTS, Traffic Generator and User-Data Gateway node.
    """

    def __init__(
        self,
        mode: str = "standalone",
        total_devices: int = 50_000,
        num_zones: int = 5,
        tick_rate_hz: float = 10.0,
        preset_name: str = "normal",
        rate_multiplier: float = 1.0,
        feedback_interval_sec: float = 1.0,
        lap_b_ip: str = "127.0.0.1",
        lap_b_data_port: int = 9001,
        lap_b_oob_port: int = 9002,
        telemetry_port: int = 9003,
        enable_mobility: bool = False,
        export_dir: str = "./results",
        show_dashboard: bool = True,
        run_id: str = "RUN_001",
    ):
        self.mode = mode
        self.num_zones = num_zones
        self.total_devices = total_devices
        self.tick_rate_hz = tick_rate_hz
        self.dt = 1.0 / tick_rate_hz
        self.feedback_interval_sec = feedback_interval_sec
        self.lap_b_ip = lap_b_ip
        self.lap_b_data_port = lap_b_data_port
        self.lap_b_oob_port = lap_b_oob_port
        self.telemetry_port = telemetry_port
        self.export_dir = export_dir
        self.show_dashboard = show_dashboard
        self.run_id = run_id

        # State flags
        self.running = False
        self.paused = False
        self.sim_time = 0.0
        self.tick_counter = 0

        # --- 1. Core Model Components ---
        print(f"[Init] Creating 50,000-device stadium population across {num_zones} zones...")
        self.population = DevicePopulation(
            total_devices=total_devices,
            num_zones=num_zones,
            rng_seed=42,
        )
        if enable_mobility:
            self.population.enable_mobility(max_speed_mps=1.5)

        print("[Init] Initialising metrics registry and accounting...")
        self.metrics = MetricsRegistry(num_zones=num_zones)

        print(f"[Init] Initialising traffic generator with preset '{preset_name}'...")
        initial_profiles = PRESET_PROFILES.get(preset_name, PRESET_PROFILES["normal"])
        self.traffic_gen = TrafficGenerator(
            population=self.population,
            profiles=initial_profiles,
            rate_multiplier=rate_multiplier,
            rng_seed=123,
        )

        print("[Init] Initialising per-zone bounded PDU queues with priority scheduling...")
        self.pdu_gateway = PDUGateway(
            num_zones=num_zones,
            ul_queue_size=25_000,
            dl_queue_size=25_000,
            drop_policy=DropPolicy.TAIL_DROP,
            schedule_policy=SchedulePolicy.ROUND_ROBIN,
            metrics=self.metrics,
        )

        print("[Init] Initialising 3.5 GHz stadium channel model with 32 RIS panels...")
        self.receiver_model = ReceiverModel(
            population=self.population,
            channel_config=ChannelConfig(),
            rng_seed=777,
        )

        print("[Init] Initialising aggregated receiver feedback engine...")
        self.receiver_feedback = ReceiverFeedback(
            num_zones=num_zones,
            window_duration=feedback_interval_sec,
            run_id=run_id,
        )

        # --- 2. Network Sockets & Clients ---
        self.telemetry_server: Optional[socket.socket] = None
        self.telemetry_clients: List[socket.socket] = []
        self.telemetry_buffers: Dict[socket.socket, bytearray] = {}

        self.data_sock: Optional[socket.socket] = None
        self.data_buffer = bytearray()
        self.data_connected = False

        self.oob_sock: Optional[socket.socket] = None
        self.oob_buffer = bytearray()
        self.oob_connected = False

        self._last_reconnect_attempt = 0.0
        self._last_telemetry_emit = 0.0

        if self.mode == "networked":
            self._setup_network_clients()
        self._setup_telemetry_server()

    # -----------------------------------------------------------------------
    #  Network Setup & Sockets
    # -----------------------------------------------------------------------

    def _setup_telemetry_server(self) -> None:
        """Start non-blocking TCP server for Laptop C supervisory & monitoring."""
        try:
            self.telemetry_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.telemetry_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.telemetry_server.bind(("0.0.0.0", self.telemetry_port))
            self.telemetry_server.listen(5)
            self.telemetry_server.setblocking(False)
            print(f"[Net] Telemetry & Supervisory server listening on 0.0.0.0:{self.telemetry_port}")
        except Exception as e:
            print(f"[Net] Warning: Could not bind telemetry port {self.telemetry_port}: {e}")
            self.telemetry_server = None

    def _setup_network_clients(self) -> None:
        """Attempt non-blocking connections to Laptop B (Data & OOB)."""
        now = time.time()
        if now - self._last_reconnect_attempt < 3.0:
            return
        self._last_reconnect_attempt = now

        # Data Plane Client
        if not self.data_connected:
            try:
                if self.data_sock:
                    try:
                        self.data_sock.close()
                    except Exception:
                        pass
                self.data_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.data_sock.settimeout(0.2)
                self.data_sock.connect((self.lap_b_ip, self.lap_b_data_port))
                self.data_sock.setblocking(False)
                self.data_connected = True
                print(f"[Net] Connected to Laptop B Data-Plane at {self.lap_b_ip}:{self.lap_b_data_port}")
            except Exception:
                self.data_connected = False
                self.metrics.increment("net_conn_errors")

        # OOB Control Client
        if not self.oob_connected:
            try:
                if self.oob_sock:
                    try:
                        self.oob_sock.close()
                    except Exception:
                        pass
                self.oob_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.oob_sock.settimeout(0.2)
                self.oob_sock.connect((self.lap_b_ip, self.lap_b_oob_port))
                self.oob_sock.setblocking(False)
                self.oob_connected = True
                print(f"[Net] Connected to Laptop B OOB Control at {self.lap_b_ip}:{self.lap_b_oob_port}")
            except Exception:
                self.oob_connected = False
                self.metrics.increment("net_conn_errors")

    def _poll_telemetry_clients(self) -> None:
        """Accept new connections and process supervisory commands from Laptop C."""
        if not self.telemetry_server:
            return

        try:
            client, addr = self.telemetry_server.accept()
            client.setblocking(False)
            self.telemetry_clients.append(client)
            self.telemetry_buffers[client] = bytearray()
            print(f"[Net] Laptop C supervisor connected from {addr[0]}:{addr[1]}")
            # Immediately send initial stadium geometry & RIS setup
            init_msg = {
                "message_type": "LAPTOP_A_INIT",
                "timestamp": time.time(),
                "total_devices": self.total_devices,
                "num_zones": self.num_zones,
                "zones": [
                    {
                        "zone_id": z.zone_id,
                        "name": z.name,
                        "center": [z.center_x, z.center_y, z.center_z],
                        "radius": z.radius,
                    }
                    for z in self.population.zones
                ],
                "bts_position": list(self.population.bts_position),
                "ris_panels": self.population.ris_panels,
            }
            send_framed_json(client, init_msg)
        except (BlockingIOError, socket.error):
            pass

        # Read commands from connected clients
        dead_clients = []
        for client in self.telemetry_clients:
            buf = self.telemetry_buffers.get(client, bytearray())
            try:
                msgs = read_framed_json(client, buf)
                for msg in msgs:
                    self._handle_supervisory_command(client, msg)
            except Exception:
                dead_clients.append(client)

        for dc in dead_clients:
            if dc in self.telemetry_clients:
                self.telemetry_clients.remove(dc)
            if dc in self.telemetry_buffers:
                del self.telemetry_buffers[dc]
            try:
                dc.close()
            except Exception:
                pass

    def _handle_supervisory_command(self, client: socket.socket, msg: dict) -> None:
        """Execute supervisory control command from Laptop C."""
        cmd = msg.get("command") or msg.get("cmd") or msg.get("message_type")
        cmd = str(cmd).upper()
        self.metrics.increment("supervisory_overrides")

        response = {"status": "ok", "command": cmd, "timestamp": time.time()}

        if cmd == "SET_TRAFFIC_PRESET":
            preset = msg.get("preset", "normal")
            try:
                self.traffic_gen.load_preset(preset)
                response["details"] = f"Preset set to {preset}"
            except Exception as e:
                response["status"] = "error"
                response["details"] = str(e)

        elif cmd == "SET_RATE_MULTIPLIER":
            mult = float(msg.get("multiplier", 1.0))
            self.traffic_gen.set_rate_multiplier(mult)
            response["details"] = f"Rate multiplier set to {mult}"

        elif cmd == "SET_ZONE_PROFILE":
            zone = int(msg.get("zone", 0))
            rate = float(msg.get("rate_pps", 5000.0))
            ptype = ProfileType(msg.get("profile_type", "cbr"))
            prof = TrafficProfile(rate_pps=rate, profile_type=ptype)
            self.traffic_gen.set_zone_profile(zone, prof)
            response["details"] = f"Zone {zone} profile updated: {rate} pps, {ptype.value}"

        elif cmd == "ENABLE_MOBILITY":
            spd = float(msg.get("speed", 1.5))
            self.population.enable_mobility(max_speed_mps=spd)
            response["details"] = f"Mobility enabled (max speed: {spd} m/s)"

        elif cmd == "DISABLE_MOBILITY":
            self.population.mobility_enabled = False
            response["details"] = "Mobility disabled"

        elif cmd == "PAUSE":
            self.paused = True
            response["details"] = "Simulation paused"

        elif cmd == "RESUME":
            self.paused = False
            response["details"] = "Simulation resumed"

        elif cmd == "RESET_METRICS":
            self.metrics.reset()
            self.traffic_gen.reset_stats()
            response["details"] = "Metrics reset"

        elif cmd == "GET_DEVICES":
            # Return a representative sample of device coordinates for 3D rendering
            zone = msg.get("zone")
            sample_size = min(int(msg.get("sample_size", 500)), 5000)
            if zone is not None:
                idx = self.population.get_zone_devices(int(zone))
            else:
                idx = np.arange(self.total_devices)
            if len(idx) > sample_size:
                idx = np.random.choice(idx, size=sample_size, replace=False)
            response["devices"] = [
                {
                    "id": int(i),
                    "zone": int(self.population.zone_ids[i]),
                    "role": "SRC" if self.population.roles[i] == 0 else "RCV",
                    "pos": [
                        round(float(self.population.pos_x[i]), 2),
                        round(float(self.population.pos_y[i]), 2),
                        round(float(self.population.pos_z[i]), 2),
                    ],
                    "sinr_db": round(float(self.population.sinr_db[i]), 2),
                }
                for i in idx
            ]

        elif cmd == "GET_SNAPSHOT":
            response["snapshot"] = self.metrics.snapshot()

        elif cmd == "EXPORT_CSV":
            out_dir = msg.get("export_dir", self.export_dir)
            sum_f, zn_f = self.metrics.export_csv(out_dir, self.run_id)
            response["summary_file"] = sum_f
            response["zones_file"] = zn_f

        send_framed_json(client, response)

    def _poll_laptop_b_messages(self) -> None:
        """Poll incoming messages from Laptop B data and OOB sockets."""
        if not self.data_connected and not self.oob_connected:
            return

        # 1. Incoming DL PDUs from Laptop B Data-plane
        if self.data_connected and self.data_sock:
            try:
                msgs = read_framed_json(self.data_sock, self.data_buffer)
                for msg in msgs:
                    self.metrics.increment("net_msg_received")
                    # Check if batch of PDUs or single PDU
                    if isinstance(msg, dict) and msg.get("message_type") == "DL_PDU_BATCH":
                        pdus = msg.get("pdus", [])
                        for p in pdus:
                            self.pdu_gateway.enqueue_downlink(p)
                    elif isinstance(msg, list):
                        for p in msg:
                            if isinstance(p, dict):
                                self.pdu_gateway.enqueue_downlink(p)
            except Exception:
                self.data_connected = False

        # 2. Incoming Control & RIS Updates from Laptop B OOB Control Plane
        if self.oob_connected and self.oob_sock:
            try:
                msgs = read_framed_json(self.oob_sock, self.oob_buffer)
                for msg in msgs:
                    self.metrics.increment("net_msg_received")
                    mtype = msg.get("message_type", "")

                    if mtype == "RIS_STATE_UPDATE":
                        # Update RIS configuration in ReceiverModel
                        self.receiver_model.apply_ris_update(msg)
                        self.metrics.increment("ris_updates_received")
                        # Send ACK
                        ack = {
                            "message_type": "ACK_RIS_UPDATE",
                            "status": "applied",
                            "update_id": msg.get("update_id"),
                            "timestamp": time.time(),
                        }
                        send_framed_json(self.oob_sock, ack)

                    elif mtype == "SCHEDULING_UPDATE":
                        # Apply new zone weights or policies
                        weights = msg.get("zone_weights")
                        if weights:
                            self.pdu_gateway.zone_weights = self.pdu_gateway._normalise(
                                {int(k.replace("Z", "")) - 1 if isinstance(k, str) and k.startswith("Z") else int(k): float(v)
                                 for k, v in weights.items()}
                            )
                        pol = msg.get("schedule_policy")
                        if pol:
                            try:
                                self.pdu_gateway.schedule_policy = SchedulePolicy(pol)
                            except Exception:
                                pass
                        ack = {
                            "message_type": "ACK_SCHEDULING",
                            "status": "applied",
                            "timestamp": time.time(),
                        }
                        send_framed_json(self.oob_sock, ack)
            except Exception:
                self.oob_connected = False

    # -----------------------------------------------------------------------
    #  Main Simulation Tick
    # -----------------------------------------------------------------------

    def step(self) -> None:
        """Advance the simulation by one tick."""
        if self.paused:
            return

        self.tick_counter += 1
        self.sim_time += self.dt

        # 1. Advance device mobility if enabled
        if self.population.mobility_enabled:
            self.population.apply_mobility(self.dt)

        # 2. Generate uplink PDUs for this tick
        new_pdus = self.traffic_gen.generate_tick(self.sim_time, self.dt)
        self.metrics.increment("pdu_generated", len(new_pdus))
        for p in new_pdus:
            z = p.get("src_zone", 0)
            self.metrics.zone_increment(z, "pdu_generated")
            self.pdu_gateway.enqueue_uplink(p)

        # 3. Process Uplink Queues
        ul_batch = self.pdu_gateway.dequeue_uplink_batch(total_count=1000)

        if self.mode == "networked" and self.data_connected and self.data_sock:
            # Transmit UL PDUs to Laptop B
            if ul_batch:
                batch_msg = {
                    "message_type": "UL_PDU_BATCH",
                    "timestamp": self.sim_time,
                    "count": len(ul_batch),
                    "pdus": ul_batch,
                }
                send_framed_json(self.data_sock, batch_msg)
                self.metrics.increment("net_msg_sent")
                self.metrics.increment("net_bytes_sent", len(json.dumps(batch_msg)))
        else:
            # Standalone / Loopback mode:
            # Simulate BTS forwarding UL PDUs directly into corresponding DL queues
            for pdu in ul_batch:
                # Mark as DL and enqueue to destination zone
                pdu["direction"] = "DL"
                self.pdu_gateway.enqueue_downlink(pdu)

        # 4. Process Downlink Queues (Delivery Evaluation)
        dl_batch = self.pdu_gateway.dequeue_downlink_batch(total_count=1000)

        for pdu in dl_batch:
            res: DeliveryResult = self.receiver_model.evaluate_delivery(pdu, self.sim_time)
            dst_zone = pdu.get("dst_zone", 0)
            payload_bytes = pdu.get("payload_size", 512)

            # Record in OOB ReceiverFeedback accumulator
            self.receiver_feedback.record_delivery(pdu, res, self.sim_time)

            if res.delivered:
                self.metrics.increment("pdu_delivered")
                self.metrics.zone_increment(dst_zone, "pdu_delivered")
                self.metrics.record_latency(dst_zone, res.latency_ms)
                self.metrics.record_throughput(payload_bytes)
            else:
                self.metrics.increment("pdu_dropped")
                self.metrics.zone_increment(dst_zone, "pdu_dropped")
                self.metrics.record_drop(res.drop_reason or "channel_loss")

                # Handle retransmission if eligible
                if pdu.get("retransmit_count", 0) < self.pdu_gateway.max_retransmissions:
                    self.pdu_gateway.retransmit_pdu(pdu)

        # 5. Check OOB Feedback Window & Send Report
        if self.receiver_feedback.is_window_complete(self.sim_time):
            report = self.receiver_feedback.aggregate_report(self.sim_time)
            self.metrics.increment("feedback_sent")

            if self.mode == "networked" and self.oob_connected and self.oob_sock:
                send_framed_json(self.oob_sock, report)
                self.metrics.increment("net_msg_sent")
                self.metrics.increment("net_bytes_sent", len(json.dumps(report)))
            elif self.mode == "standalone":
                # Closed loop demonstration in standalone mode:
                # Adapt RIS panels dynamically toward zones with highest loss!
                self._standalone_adaptive_closed_loop(report)

            self.receiver_feedback.reset_window(self.sim_time)

        # 6. Update Queue Depths for Metrics
        self.pdu_gateway.update_metrics_queue_depths()

        # 7. Telemetry Broadcast to Laptop C (rate-limited to 2 Hz)
        now = time.time()
        if (now - self._last_telemetry_emit) >= 0.5:
            self._last_telemetry_emit = now
            if self.telemetry_clients:
                snap = self.metrics.snapshot()
                telemetry_msg = {
                    "message_type": "TELEMETRY_UPDATE",
                    "source": "LAPTOP_A",
                    "sim_time": round(self.sim_time, 2),
                    "snapshot": snap,
                }
                for c in list(self.telemetry_clients):
                    try:
                        send_framed_json(c, telemetry_msg)
                    except Exception:
                        pass

        # 8. Network Maintenance (Polling & Auto-reconnect)
        if self.mode == "networked":
            self._setup_network_clients()
            self._poll_laptop_b_messages()
        self._poll_telemetry_clients()

        # 9. Terminal TUI Dashboard
        if self.show_dashboard:
            self.metrics.print_dashboard()

    def _standalone_adaptive_closed_loop(self, report: dict) -> None:
        """
        Standalone closed loop controller:
        Inspects feedback report, finds the zone with poorest PDR or highest loss,
        and reconfigures RIS panels and scheduling weights adaptively.
        """
        zones = report.get("payload", {}).get("zones", {})
        if not zones:
            return

        # Find zone with lowest PDR
        worst_zone = 0
        min_pdr = 1.0
        for i in range(self.num_zones):
            z_key = f"Z{i + 1}"
            pdr = zones.get(z_key, {}).get("pdr", 1.0)
            if pdr < min_pdr:
                min_pdr = pdr
                worst_zone = i

        # If significant packet loss is observed, boost RIS gain for that zone
        if min_pdr < 0.90:
            # Shift 8 RIS panels to constructively assist the congested zone
            panel_updates = []
            for pid in range(worst_zone * 6, min(len(self.receiver_model.ris_state), (worst_zone + 1) * 6 + 2)):
                panel_updates.append({
                    "panel_id": pid,
                    "target_zones": [worst_zone, 4],
                    "gain_db": 10.5,
                    "phase": (pid * 3) % 8,
                    "active": True,
                })
            self.receiver_model.apply_ris_update({"panels": panel_updates})
            self.metrics.increment("ris_updates_received")

            # Adjust scheduling weights slightly to prioritize congested zone
            weights = {z: 0.15 for z in range(self.num_zones)}
            weights[worst_zone] = 0.40
            self.pdu_gateway.zone_weights = self.pdu_gateway._normalise(weights)

    # -----------------------------------------------------------------------
    #  Lifecycle & Execution Loop
    # -----------------------------------------------------------------------

    def run(self, duration_sec: float = 0.0) -> None:
        """Run the simulation loop until stopped or duration expires."""
        self.running = True
        start_wall = time.time()
        print(f"\n[Run] Laptop A simulation started in {self.mode.upper()} mode at {self.tick_rate_hz} Hz...")
        print("[Run] Press Ctrl+C at any time to gracefully stop and export results.\n")

        try:
            while self.running:
                loop_start = time.time()

                self.step()

                # Check duration limit
                if duration_sec > 0 and (time.time() - start_wall) >= duration_sec:
                    print(f"\n[Run] Target duration of {duration_sec}s reached.")
                    break

                # Precise tick rate pacing
                elapsed = time.time() - loop_start
                sleep_time = self.dt - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\n[Run] User interrupted execution (Ctrl+C).")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Gracefully terminate sockets, verify accounting, and export results."""
        self.running = False
        print("\n" + "=" * 80)
        print("  LAPTOP A SHUTDOWN & ACCOUNTING VERIFICATION")
        print("=" * 80)

        # Print final dashboard
        if self.show_dashboard:
            self.metrics.print_dashboard(force=True)

        # Verification of strict accounting
        balanced, msg = self.metrics.verify_accounting()
        print(f"\nAccounting Check: {'[PASSED]' if balanced else '[MISMATCH]'}")
        print(f"  → {msg}")

        # CSV Export
        sum_f, zn_f = self.metrics.export_csv(self.export_dir, self.run_id)
        print(f"\nRun results saved to:")
        print(f"  • Summary: {sum_f}")
        print(f"  • Per-Zone: {zn_f}")

        # Close sockets
        if self.data_sock:
            try:
                self.data_sock.close()
            except Exception:
                pass
        if self.oob_sock:
            try:
                self.oob_sock.close()
            except Exception:
                pass
        for c in self.telemetry_clients:
            try:
                c.close()
            except Exception:
                pass
        if self.telemetry_server:
            try:
                self.telemetry_server.close()
            except Exception:
                pass

        print("\n[Done] Laptop A shutdown complete.\n")


# ---------------------------------------------------------------------------
#  CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Laptop A — BTS, Traffic Generation & User-Data Gateway Runtime"
    )
    parser.add_argument(
        "--mode",
        choices=["standalone", "networked"],
        default="standalone",
        help="Operation mode: 'standalone' (self-contained loopback) or 'networked' (connects to Laptop B & C). Default: standalone",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Run duration in seconds (0 = run indefinitely). Default: 0",
    )
    parser.add_argument(
        "--tick-rate",
        type=float,
        default=10.0,
        help="Simulation tick rate in Hz. Default: 10.0",
    )
    parser.add_argument(
        "--preset",
        choices=list(PRESET_PROFILES.keys()),
        default="normal",
        help=f"Initial traffic preset: {list(PRESET_PROFILES.keys())}. Default: normal",
    )
    parser.add_argument(
        "--rate-mult",
        type=float,
        default=1.0,
        help="Global traffic rate multiplier. Default: 1.0",
    )
    parser.add_argument(
        "--feedback-interval",
        type=float,
        default=1.0,
        help="OOB feedback report window interval in seconds. Default: 1.0",
    )
    parser.add_argument(
        "--lap-b-ip",
        type=str,
        default="127.0.0.1",
        help="IP address of Laptop B. Default: 127.0.0.1",
    )
    parser.add_argument(
        "--lap-b-data-port",
        type=int,
        default=9001,
        help="TCP port for Laptop B user-data plane. Default: 9001",
    )
    parser.add_argument(
        "--lap-b-oob-port",
        type=int,
        default=9002,
        help="TCP port for Laptop B OOB control plane. Default: 9002",
    )
    parser.add_argument(
        "--telemetry-port",
        type=int,
        default=9003,
        help="TCP port to listen for Laptop C supervisory connections. Default: 9003",
    )
    parser.add_argument(
        "--mobility",
        action="store_true",
        help="Enable device mobility (random walk inside zone).",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Disable the 1 Hz terminal TUI dashboard.",
    )
    parser.add_argument(
        "--export-dir",
        type=str,
        default="./results",
        help="Output directory for CSV metrics export. Default: ./results",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default="RUN_001",
        help="Run identifier tag for logs and export files. Default: RUN_001",
    )

    args = parser.parse_args()

    system = LaptopASystem(
        mode=args.mode,
        total_devices=50_000,
        num_zones=5,
        tick_rate_hz=args.tick_rate,
        preset_name=args.preset,
        rate_multiplier=args.rate_mult,
        feedback_interval_sec=args.feedback_interval,
        lap_b_ip=args.lap_b_ip,
        lap_b_data_port=args.lap_b_data_port,
        lap_b_oob_port=args.lap_b_oob_port,
        telemetry_port=args.telemetry_port,
        enable_mobility=args.mobility,
        export_dir=args.export_dir,
        show_dashboard=not args.no_dashboard,
        run_id=args.run_id,
    )

    # Register OS signals
    def handle_signal(sig, frame):
        system.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    system.run(duration_sec=args.duration)


if __name__ == "__main__":
    main()
