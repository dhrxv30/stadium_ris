"""Laptop B entry point (Stage B1–B8).

  python laptop_b/main.py --mode listen          # wait for real Laptop A
  python laptop_b/main.py --mode mock            # standalone: built-in mock Laptop A over loopback
  python laptop_b/main.py --mode baseline        # listen, baseline scheduler (no adaptive RIS)
"""
import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.constants import MSG_TELEMETRY_B
from common.framing import encode_frame
from common.logging_utils import setup_logging          # noqa: E402
from common.messages import encode_json_message
from common.telemetry_messages import make_message as make_telemetry_message
from common.validation import ConfigError, load_config  # noqa: E402
from laptop_b.controller import ControllerB             # noqa: E402
from laptop_b.mock_feedback import run_mock_a           # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Laptop B: RIS / OOB controller (Stage B1–B8)")
    p.add_argument("--mode", choices=["listen", "mock", "baseline"], default="listen",
                   help="listen=real A; mock=built-in mock A; baseline=no adaptive RIS")
    p.add_argument("--config-dir", default=str(ROOT / "config"))
    p.add_argument("--host", help="bind address override")
    p.add_argument("--port", type=int, help="listen port override")
    p.add_argument("--lap-c-ip", help="Laptop C reachable LAN IP override")
    p.add_argument("--seed", type=int)
    p.add_argument("--duration", type=float, dest="duration_s",
                   help="seconds to run (0 = until Ctrl+C)")
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--status-interval", type=float, dest="status_interval_s")
    p.add_argument("--run-id")
    p.add_argument("--mock-reports", type=int, default=3,
                   help="mock: feedback reports per zone")
    p.add_argument("--no-csv", action="store_true", help="skip CSV export at end")
    return p.parse_args(argv)


async def status_loop(ctrl, interval):
    while True:
        await asyncio.sleep(interval)
        print(ctrl.status_summary(), flush=True)


async def publish_telemetry_to_c(ctrl, network, stop):
    """Reconnect to Laptop C and publish the latest controller state."""
    host = network["laptop_c"]["host"]
    port = network["laptop_c"].get("telemetry_port_b") or network["laptop_c"].get("port", 9003)
    max_bytes = network["max_message_bytes"]
    interval = 0.5
    seq = 0

    while not stop.is_set():
        telemetry_run_id = ctrl.expected_run_id
        if telemetry_run_id is None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            continue

        writer = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=network["connect_timeout_s"],
            )
            ctrl.telemetry_c_connected = True
            ctrl.log.info("Laptop C telemetry connected at %s:%d", host, port)
            while not stop.is_set():
                message = make_telemetry_message(
                    MSG_TELEMETRY_B,
                    "B",
                    "C",
                    telemetry_run_id,
                    ctrl.telemetry_payload(),
                    seq=seq,
                )
                writer.write(encode_frame(
                    encode_json_message(message), max_bytes
                ))
                await writer.drain()
                seq += 1
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        except (OSError, asyncio.TimeoutError) as exc:
            ctrl.telemetry_c_connected = False
            ctrl.log.warning("Laptop C telemetry unavailable: %r", exc)
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=network["reconnect_interval_s"]
                )
            except asyncio.TimeoutError:
                pass
        finally:
            ctrl.telemetry_c_connected = False
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass


async def amain(args) -> int:
    overrides = {k: getattr(args, k) for k in
                 ("host", "port", "seed", "duration_s", "log_level", "status_interval_s")}
    try:
        cfg = load_config(args.config_dir, overrides)
    except ConfigError as exc:
        print(f"[LAPTOP B] CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    if args.lap_c_ip:
        cfg["network"]["laptop_c"]["host"] = args.lap_c_ip
    print("[LAPTOP B] configuration validated OK")

    run = cfg["network"]["run"]
    run_id = args.run_id or time.strftime("run-%Y%m%d-%H%M%S") + f"-s{run['seed']}"
    run_dir = ROOT / run["output_dir"] / run_id
    log = setup_logging("laptop_b", run["log_level"], run_dir)
    ctrl = ControllerB(cfg, run_id, logger=log, mode=args.mode)
    print(ctrl.startup_summary())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: Ctrl+C handled via KeyboardInterrupt

    net_b = cfg["network"]["laptop_b"]
    host = "127.0.0.1" if args.mode == "mock" and not args.host else (args.host or net_b.get("bind_host") or "0.0.0.0")
    listen_port = (args.port or 0) if args.mode == "mock" else net_b["port"]
    port = await ctrl.start(host, listen_port)
    status_task = asyncio.create_task(status_loop(ctrl, run["status_interval_s"]))
    telemetry_task = None
    if args.mode != "mock":
        telemetry_task = asyncio.create_task(
            publish_telemetry_to_c(ctrl, cfg["network"], stop)
        )
    exit_code = 0

    try:
        if args.mode == "mock":
            exp = await run_mock_a("127.0.0.1", port, run_id, cfg["stadium"]["zones"],
                                   args.mock_reports, run["seed"], cfg["network"]["max_message_bytes"])
            for _ in range(100):
                if ctrl.stats["frames_received"] >= exp["frames"]:
                    break
                await asyncio.sleep(0.05)
            print("\n[MOCK] expected vs actual (B self-check)")
            checks = {
                "accepted": ctrl.stats["feedback_accepted"],
                "heartbeats": ctrl.stats["heartbeats"],
                "rejected_duplicate": ctrl.stats["rejected_duplicate"],
                "rejected_stale": ctrl.stats["rejected_stale"],
                "malformed": ctrl.stats["malformed"],
            }
            for k, actual in checks.items():
                ok = actual == exp[k]
                exit_code |= 0 if ok else 1
                print(f"  {k:22s} expected={exp[k]:3d} actual={actual:3d} {'OK' if ok else 'MISMATCH'}")
        else:
            dur = run["duration_s"]
            try:
                await asyncio.wait_for(stop.wait(), timeout=dur if dur > 0 else None)
            except asyncio.TimeoutError:
                pass
    except KeyboardInterrupt:
        pass
    finally:
        status_task.cancel()
        if telemetry_task:
            telemetry_task.cancel()
            try:
                await telemetry_task
            except asyncio.CancelledError:
                pass
        await ctrl.stop()
        print("\n[LAPTOP B] FINAL SUMMARY")
        print(ctrl.status_summary())
        if not args.no_csv:
            ctrl.write_reports(run_dir)
            print(f"[LAPTOP B] CSV reports written to: {run_dir}")
        print(f"[LAPTOP B] logs: {run_dir}")
    return exit_code


def main(argv=None):
    try:
        return asyncio.run(amain(parse_args(argv)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
