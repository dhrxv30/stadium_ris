"""Laptop C Supervisor Entry Point - Stages C1 to D5.

Capabilities:
- Stage C1: Shared or per-peer TCP framed JSON telemetry listeners on Laptop C, with connection health and run-ID validation
- Stage C2: Unified supervisor state model (50k devices, 5 zones, 32 RIS panels, UL/DL PDUs, OOB stats)
- Stage C3: Live multi-pane terminal supervisor dashboard with event filtering and diagnostics
- Stage C4: Automated CSV performance reports export (results/<run_id>/*.csv)
- Stage C5: Optional native local 3D stadium catwalk & beam footprint visualization
- Stage D1: History buffers sampled ~1 Hz via sup.tick()
- Stage D2: Optional native Tkinter dashboard (--dashboard, off by default)

Usage:
  Live mode:      python -m laptop_c.main --config config/network.yaml
  Mock mode:      python -m laptop_c.main --mock --duration 6
  With dashboard: python -m laptop_c.main --mock --duration 30 --dashboard
  3D snapshot:    python -m laptop_c.main --mock --duration 6 --visualize
  With filters:   python -m laptop_c.main --mock --duration 6 --filter-zone Z3
"""
import argparse
import asyncio
import os
import signal
import sys
import time
import uuid

from common.config import ConfigError, load_network_config
from laptop_c.csv_exporter import CsvExporter
from laptop_c.mock_telemetry import MockPeer
from laptop_c.supervisor import Supervisor
from laptop_c.telemetry_collector import TelemetryCollector
from laptop_c.terminal_view import TerminalView
from laptop_c.visualization.stadium_3d import Stadium3DVisualizer


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Laptop C Supervisor - Full System (Stages C1 - C5)")
    p.add_argument("--config", default="config/network.yaml", help="path to network.yaml (default: config/network.yaml)")
    p.add_argument("--bind", default=None, help="override laptop_c.bind_host")
    p.add_argument("--port", type=int, default=None, help="override unified telemetry port for both A and B (default: 9003)")
    p.add_argument("--port-a", type=int, default=None, help="override telemetry port for A (default 9003)")
    p.add_argument("--port-b", type=int, default=None, help="override telemetry port for B (default 9003)")
    p.add_argument("--run-id", default=None, help="expected run ID (else adopted from first message)")
    p.add_argument("--duration", type=float, default=None, help="auto-exit after N seconds")
    p.add_argument("--summary-interval", type=float, default=1.0, help="refresh interval in seconds")
    p.add_argument("--mock", action="store_true", help="start in-process MOCK A and B senders (testing only)")
    p.add_argument("--verbosity", choices=["quiet", "normal", "debug"], default="normal")

    # Stage C3 Filter options
    p.add_argument("--filter-zone", default=None, help="filter event stream by zone (e.g. Z1..Z5)")
    p.add_argument("--filter-panel", default=None, help="filter event stream by panel (e.g. P1..P32)")
    p.add_argument("--filter-cmd", default=None, help="filter event stream by OOB command ID")
    p.add_argument("--filter-pdu", default=None, help="filter event stream by PDU ID")
    p.add_argument("--filter-type", default=None, help="filter event stream by event type")

    # Stage C4 CSV options
    p.add_argument("--csv-dir", default="results", help="base directory for CSV results")
    p.add_argument("--no-csv", action="store_true", help="disable CSV export")

    # Stage C5 Visualization option
    p.add_argument("--visualize", action="store_true", help="save a local 3D stadium visualization snapshot on exit")

    # Stage D2: Native dashboard – off by default (hard rule 1)
    # The core never imports the dashboard at module level (hard rule 4).
    p.add_argument(
        "--dashboard", action="store_true",
        help=(
            "open the native Tkinter supervisor dashboard window "
            "(requires a local display; gracefully falls back to terminal mode if unavailable)"
        ),
    )

    return p.parse_args(argv)


def build_overrides(a):
    ov = {"laptop_c": {}}
    if a.bind:
        ov["laptop_c"]["bind_host"] = a.bind
    if a.port is not None:
        ov["laptop_c"]["telemetry_port_a"] = a.port
        ov["laptop_c"]["telemetry_port_b"] = a.port
        ov["laptop_c"]["port"] = a.port
    if a.port_a is not None:
        ov["laptop_c"]["telemetry_port_a"] = a.port_a
    if a.port_b is not None:
        ov["laptop_c"]["telemetry_port_b"] = a.port_b
    return ov


async def amain(args):
    try:
        cfg = load_network_config(args.config, build_overrides(args))
    except ConfigError as e:
        print(f"[LAPTOP C] CONFIG ERROR: {e}", file=sys.stderr)
        return 2

    if args.mock:  # mock mode supports dynamic port assignment
        cfg["laptop_c"]["telemetry_port_a"] = args.port_a or 0
        cfg["laptop_c"]["telemetry_port_b"] = args.port_b or 0

    run_id = args.run_id or (f"run-{uuid.uuid4().hex[:6]}" if args.mock else None)
    t0_wall = time.time()
    t0_mono = time.monotonic()

    # Initialize Stage C2 Supervisor
    sup = Supervisor(run_id=run_id)

    # Initialize Stage C4 CSV Exporter
    exporter = None
    if not args.no_csv:
        export_run_id = run_id or f"run-{uuid.uuid4().hex[:6]}"
        exporter = CsvExporter(export_run_id, base_dir=args.csv_dir)

    # On message callback connecting Collector -> Supervisor -> CSV Exporter
    def handle_incoming_message(expected_source: str, msg: dict):
        sup.on_message(expected_source, msg)
        if exporter and msg.get("msg_type") == "EVENT":
            payload = msg.get("payload", {})
            payload["source"] = expected_source
            exporter.log_event(payload)

    # Initialize Stage C1 Telemetry Collector
    col = TelemetryCollector(cfg, run_id=run_id, on_message=handle_incoming_message)

    try:
        await col.start()
    except OSError as e:
        print(f"[LAPTOP C] cannot bind listener: {e}", file=sys.stderr)
        if exporter:
            exporter.close()
        return 3

    # Initialize Stage C3 Terminal View
    term_view = TerminalView(
        sup,
        filter_zone=args.filter_zone,
        filter_panel=args.filter_panel,
        filter_cmd=args.filter_cmd,
        filter_pdu=args.filter_pdu,
        filter_type=args.filter_type,
        verbosity=args.verbosity,
    )

    # Stage D2: Optional native Tkinter dashboard (hard rules 1 & 4)
    # Deferred import keeps the core independent of Tkinter.
    # Any failure -> prints "dashboard unavailable" and continues in terminal mode.
    dashboard_thread = None
    if getattr(args, "dashboard", False):
        try:
            from laptop_c.dashboard.app import DashboardThread  # noqa: PLC0415
            dashboard_thread = DashboardThread()
            dashboard_thread.start()
            if not dashboard_thread.wait_ready(timeout=6.0):
                print("dashboard unavailable: window did not open in time, "
                      "continuing in terminal mode")
                dashboard_thread = None
            else:
                print("[LAPTOP C] Native dashboard started (2 Hz refresh).")
        except Exception as _dash_exc:
            print(f"dashboard unavailable: {_dash_exc}")
            dashboard_thread = None

    mode_label = "MOCK TELEMETRY (synthetic test data)" if args.mock else "LIVE (waiting for Laptop A and B)"
    print("=" * 72)
    print("========== STADIUM RIS SUPERVISOR - LAPTOP C (STAGES C1-D5) ==========")
    print(f"Config:    {args.config or 'built-in defaults'} -> VALID")
    print(f"Mode:      {mode_label}")
    print(f"Listening: Port A={cfg['laptop_c']['bind_host']}:{col.ports['A']} | "
          f"Port B={cfg['laptop_c']['bind_host']}:{col.ports['B']}")
    if exporter:
        print(f"CSV Logs:  {exporter.output_dir}")
    if dashboard_thread:
        print("Dashboard: native Tkinter window open  [--dashboard]")
    print("Note:      TCP links between laptops are real; radio & OOB behaviour are SIMULATED.")
    print("Press Ctrl+C for clean shutdown.\n")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    tasks = []
    active_run_id = run_id or "N/A (pending first peer message)"
    if args.mock:
        for name, seed in (("A", 1), ("B", 2)):
            mp = MockPeer(name, "127.0.0.1", col.ports[name], active_run_id, seed=seed)
            tasks.append(asyncio.create_task(mp.run(args.duration, stop)))

    last_sample_t = time.monotonic()
    try:
        while not stop.is_set():
            now_mono = time.monotonic()
            if args.duration is not None and now_mono - t0_mono >= args.duration:
                break

            # Update peer health
            sup.update_peer_health(col.health())

            # D1: sample history at ~1 Hz (gated inside tick(); no I/O, no blocking)
            sup.tick()

            # Display Stage C3 Terminal Dashboard
            term_view.display()

            # D2: push a deep-copied view to the native dashboard (hard rule 5)
            if dashboard_thread is not None:
                if dashboard_thread.is_dashboard_alive:
                    dashboard_thread.push_view(sup.get_view())
                else:
                    print("dashboard unavailable: window closed, continuing in terminal mode")
                    dashboard_thread = None

            # Record Stage C4 periodic CSV metrics
            if exporter and (now_mono - last_sample_t >= args.summary_interval):
                exporter.sample_metrics(sup)
                exporter.log_ris_panel_snapshot(sup)
                last_sample_t = now_mono

            try:
                await asyncio.wait_for(stop.wait(), args.summary_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)

        # Update final health & print final dashboard
        sup.update_peer_health(col.health())
        print("\n\n" + "=" * 72)
        print("FINAL EXPERIMENT SUPERVISOR DASHBOARD")
        term_view.display()

        # Final Stage C4 CSV reconciliation export
        if exporter:
            exporter.sample_metrics(sup)
            exporter.log_ris_panel_snapshot(sup)
            exporter.export_run_summary(sup, t0_wall)
            exporter.close()
            print(f"[LAPTOP C] CSV performance reports exported to: {exporter.output_dir}")

        # Optional Stage C5 3D Visualization snapshot
        if args.visualize:
            vis = Stadium3DVisualizer(sup)
            if vis.is_available():
                snapshot_file = os.path.join(
                    exporter.output_dir if exporter else "results",
                    "stadium_3d.png"
                )
                if vis.save_snapshot(snapshot_file):
                    print(f"[LAPTOP C] 3D Stadium snapshot generated at: {snapshot_file}")
                else:
                    print("[LAPTOP C] Notice: Could not save 3D snapshot.")
            else:
                print("[LAPTOP C] Notice: matplotlib not available, skipped 3D rendering.")

        # D2: close dashboard window cleanly before exiting
        if dashboard_thread is not None:
            dashboard_thread.stop()

        await col.stop()
        print("[LAPTOP C] Shutdown complete. All sockets and file descriptors closed cleanly.")

    return 0


def main(argv=None):
    try:
        return asyncio.run(amain(parse_args(argv)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    rc = main()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(rc if rc is not None else 0)
