"""Run reproducible baseline, scheduling-only, and adaptive RIS simulations."""

import argparse
import asyncio
import csv
import io
import time
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.constants import MSG_FEEDBACK_REPORT
from common.messages import make_message
from common.validation import load_config
from laptop_a.main import LaptopASystem
from laptop_b.controller import ControllerB
SCENARIOS = ("baseline", "scheduling_only", "adaptive_ris")


def _congestion_report(run_id: str, seq: int) -> dict:
    now = time.time()
    return make_message(
        MSG_FEEDBACK_REPORT,
        "A",
        "B",
        run_id,
        {
            "seq": seq,
            "window_start": now - 1.0,
            "window_end": now,
            "queue_occupancy": 950,
            "queue_capacity": 1000,
            "generated": 100,
            "delivered": 50,
            "dropped": 50,
            "mean_sinr_db": 4.0,
            "per": 0.2,
            "traffic_rate_pps": 5000.0,
        },
        zone_id="Z3",
    )


async def _apply_adaptive_control(system_a: LaptopASystem, config: dict, run_id: str):
    controller = ControllerB(config, run_id)
    controller.oob.params.delay_s = 0.001
    controller.oob.params.loss_probability = 0.0
    applied_updates = []

    async def apply_update(message: dict) -> bool:
        ack = system_a._apply_control_update(message)
        if ack["status"] != "applied":
            raise RuntimeError(f"Laptop A rejected {message['message_type']}: {ack}")
        applied_updates.append(message["message_type"])
        return True

    controller.closed_loop.control_update_sender = apply_update
    for seq in (1, 2):
        status, reason = controller.zones.process_feedback(
            _congestion_report(run_id, seq)
        )
        if status != "ACCEPTED":
            raise RuntimeError(f"Could not inject deterministic test feedback: {reason}")

    await controller.closed_loop._iterate()
    deadline = asyncio.get_running_loop().time() + 5.0
    target_panels = controller.panels.by_zone("Z3")
    while not target_panels or not all(panel.config_version for panel in target_panels):
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Adaptive RIS updates were not applied before timeout")
        await asyncio.sleep(0.01)
    if "SCHEDULING_UPDATE" not in applied_updates:
        raise RuntimeError("Laptop A did not apply the scheduling update")
    if "RIS_STATE_UPDATE" not in applied_updates:
        raise RuntimeError("Laptop A did not apply the RIS update")


async def _apply_scheduling_control(
    system_a: LaptopASystem, config: dict, run_id: str
):
    controller = ControllerB(config, run_id, mode="baseline")
    for seq in (1, 2):
        status, reason = controller.zones.process_feedback(
            _congestion_report(run_id, seq)
        )
        if status != "ACCEPTED":
            raise RuntimeError(f"Could not inject deterministic test feedback: {reason}")
    decisions = controller.scheduler.decide_all(controller.zones)
    weights = {decision.zone_id: decision.token_weight for decision in decisions}
    policy = (
        "weighted"
        if getattr(controller.scheduler, "NAME", "") == "CONGESTION_AWARE"
        else "round_robin"
    )
    ack = system_a._apply_control_update({
        "message_type": "SCHEDULING_UPDATE",
        "update_id": f"experiment-schedule-{run_id}",
        "zone_weights": weights,
        "schedule_policy": policy,
    })
    if ack["status"] != "applied":
        raise RuntimeError(f"Laptop A rejected scheduling update: {ack}")
def _run_scenario(
    scenario: str,
    seed: int,
    duration_s: float,
    packet_budget: int,
    rate_multiplier: float,
) -> dict:
    run_id = f"experiment-{scenario}-s{seed}"
    config = load_config(str(ROOT / "config"))
    config["network"]["run"]["seed"] = seed
    with tempfile.TemporaryDirectory() as export_dir:
        with redirect_stdout(io.StringIO()):
            system_a = LaptopASystem(
                mode="standalone",
                total_devices=5000,
                num_zones=5,
                packet_budget_per_tick=packet_budget,
                rate_multiplier=rate_multiplier,
                export_dir=export_dir,
                telemetry_port=0,
                show_dashboard=False,
                run_id=run_id,
                random_seed=seed,
            )
            try:
                if scenario == "scheduling_only":
                    asyncio.run(_apply_scheduling_control(system_a, config, run_id))
                elif scenario == "adaptive_ris":
                    asyncio.run(_apply_adaptive_control(system_a, config, run_id))

                ticks = max(1, round(duration_s * system_a.tick_rate_hz))
                simulated_duration_s = ticks * system_a.dt
                for _ in range(ticks):
                    system_a.step()
                balanced, accounting = system_a.metrics.verify_accounting()
                if not balanced:
                    raise RuntimeError(f"Accounting failed for {scenario}: {accounting}")
                snapshot = system_a.metrics.snapshot()
            finally:
                system_a.shutdown()

    global_metrics = snapshot["global"]
    result = {
        "scenario": scenario,
        "run_id": run_id,
        "seed": seed,
        "requested_duration_s": duration_s,
        "simulated_duration_s": simulated_duration_s,
        "packet_budget_per_tick": packet_budget,
        "rate_multiplier": rate_multiplier,
        "feedback_stimulus": "synthetic_congested_Z3",
        "generated": global_metrics["pdu_generated"],
        "delivered": global_metrics["pdu_delivered"],
        "dropped": global_metrics["pdu_dropped"],
        "pending": global_metrics["pdu_pending"],
        "final_outcomes": global_metrics["pdu_final_outcomes"],
        "pdr_final_outcomes": global_metrics["pdr"],
        "loss_ratio_final_outcomes": global_metrics["loss_ratio"],
        "mean_latency_ms": global_metrics["latency"]["mean_ms"],
        "payload_bytes_delivered": global_metrics["payload_bytes_delivered"],
        "goodput_bps_simulated": round(
            global_metrics["payload_bytes_delivered"] * 8 / simulated_duration_s
        ),
        "scheduling_updates_applied": snapshot["oob"]["scheduling_updates_received"],
        "ris_updates_applied": snapshot["oob"]["ris_updates_received"],
        "control_updates_applied": (
            snapshot["oob"]["scheduling_updates_received"]
            + snapshot["oob"]["ris_updates_received"]
        ),
    }
    for zone_id, zone_metrics in snapshot["zones"].items():
        for metric in ("generated", "delivered", "dropped", "final_outcomes", "pdr"):
            result[f"{zone_id.lower()}_{metric}"] = zone_metrics[metric]
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--duration", type=float, default=3.0,
                        help="simulated duration per scenario in seconds")
    parser.add_argument("--packet-budget", type=int, default=300,
                        help="A's per-tick service budget for each direction")
    parser.add_argument("--rate-multiplier", type=float, default=4.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "results" / "experiments" / "comparison.csv",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.duration <= 0 or args.packet_budget <= 0 or args.rate_multiplier < 0:
        raise SystemExit("duration and packet budget must be positive; rate multiplier non-negative")

    results = [
        _run_scenario(
            scenario,
            args.seed,
            args.duration,
            args.packet_budget,
            args.rate_multiplier,
        )
        for scenario in SCENARIOS
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    print(f"Wrote {len(results)} scenario summaries to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
