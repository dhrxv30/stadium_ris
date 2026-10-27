import asyncio
import json
import tempfile
import time
from pathlib import Path

import pytest

from common.constants import MSG_FEEDBACK_REPORT, MSG_HEARTBEAT
from common.framing import encode_frame, read_frame
from common.messages import encode_message, make_message
from common.validation import load_config
from laptop_a.main import LaptopASystem
from laptop_a.pdu_gateway import SchedulePolicy
from laptop_b.controller import ControllerB


def test_ris_phase_update_changes_model_gain_for_zone():
    with tempfile.TemporaryDirectory() as export_dir:
        system_a = LaptopASystem(
            mode="standalone",
            total_devices=100,
            num_zones=5,
            telemetry_port=0,
            export_dir=export_dir,
            show_dashboard=False,
        )
        try:
            baseline_gain = system_a.receiver_model.get_ris_gain_for_zone(2)
            ok = system_a._apply_control_update({
                "message_type": "RIS_STATE_UPDATE",
                "update_id": "phase-gain-test",
                "cmd_id": "phase-gain-test",
                "config_version": 1,
                "zone_id": "Z3",
                "codebook_index": 7,
                "zone_sinr_offset_db": 0.0,
                "panels": [{
                    "panel_id": 0,
                    "config_version": 1,
                    "phase": 7,
                    "target_zones": [2],
                    "active": True,
                }],
            })
            assert ok["status"] == "applied"
            assert system_a.receiver_model.ris_state[0]["gain_db"] > 0.0
            after_gain = system_a.receiver_model.get_ris_gain_for_zone(2)
            assert after_gain > baseline_gain
        finally:
            system_a.shutdown()


def test_laptop_b_updates_are_applied_and_acknowledged_by_laptop_a():
    asyncio.run(_exercise_control_update_round_trip())


async def _exercise_control_update_round_trip():
    with tempfile.TemporaryDirectory() as export_dir:
        system_a = LaptopASystem(
            mode="standalone",
            total_devices=100,
            num_zones=5,
            telemetry_port=0,
            export_dir=export_dir,
            show_dashboard=False,
        )
        config_dir = str(Path(__file__).resolve().parents[1] / "config")
        controller_b = ControllerB(load_config(config_dir), "integration-run")
        writer = None
        try:
            port = await controller_b.start("127.0.0.1", 0)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            heartbeat = make_message(
                MSG_HEARTBEAT,
                "A",
                "B",
                "integration-run",
                {"seq": 0},
            )
            writer.write(encode_message(heartbeat))
            await writer.drain()
            deadline = asyncio.get_running_loop().time() + 2.0
            while not any(source == "A" for source in controller_b._conns.values()):
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("Laptop B did not register Laptop A")
                await asyncio.sleep(0.01)

            async def apply_next_update():
                raw = await read_frame(reader)
                update = json.loads(raw.decode("utf-8"))
                ack = system_a._apply_control_update(update)
                writer.write(encode_frame(json.dumps(ack).encode("utf-8")))
                await writer.drain()
                return update, ack

            for update in (
                {
                    "message_type": "SCHEDULING_UPDATE",
                    "update_id": "schedule-1",
                    "zone_weights": {
                        "Z1": 2.0, "Z2": 1.0, "Z3": 1.0, "Z4": 1.0, "Z5": 1.0,
                    },
                    "schedule_policy": "weighted",
                },
                {
                    "message_type": "RIS_STATE_UPDATE",
                    "update_id": "ris-command-1",
                    "cmd_id": "command-1",
                    "config_version": 1,
                    "zone_id": "Z3",
                    "codebook_index": 2,
                    "zone_sinr_offset_db": 3.5,
                    "panels": [{
                        "panel_id": 0,
                        "target_zones": [2],
                        "active": True,
                    }],
                },
            ):
                sender = asyncio.create_task(
                    controller_b.send_control_update(update)
                )
                received, ack = await asyncio.wait_for(apply_next_update(), 2.0)
                assert received["update_id"] == update["update_id"]
                assert ack["status"] == "applied"
                assert await asyncio.wait_for(sender, 2.0)

            assert system_a.pdu_gateway.schedule_policy == SchedulePolicy.WEIGHTED
            assert system_a.receiver_model.ris_state[0]["phase"] == 2
            assert system_a.receiver_model.zone_sinr_offset[2] == 3.5
            assert controller_b.stats["control_updates_applied"] == 2
            assert controller_b.stats["ris_updates_applied"] == 1
            assert controller_b.stats["scheduling_updates_applied"] == 1
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            await controller_b.stop()
            system_a.shutdown()


def test_congested_feedback_drives_optimizer_and_applies_zone_update_to_laptop_a():
    asyncio.run(_exercise_congestion_to_applied_ris_update())


async def _exercise_congestion_to_applied_ris_update():
    with tempfile.TemporaryDirectory() as export_dir:
        system_a = LaptopASystem(
            mode="standalone",
            total_devices=100,
            num_zones=5,
            telemetry_port=0,
            export_dir=export_dir,
            show_dashboard=False,
            run_id="closed-loop-run",
        )
        system_a.receiver_model.cfg.fading_enabled = False
        system_a.population.zones[2].nlos_base_prob = 0.0

        config_dir = str(Path(__file__).resolve().parents[1] / "config")
        controller_b = ControllerB(load_config(config_dir), "closed-loop-run")
        controller_b.oob.params.delay_s = 0.001
        controller_b.oob.params.loss_probability = 0.0
        writer = None
        ack_task = None
        try:
            port = await controller_b.start("127.0.0.1", 0)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            async def apply_control_updates():
                while True:
                    raw = await read_frame(reader)
                    update = json.loads(raw.decode("utf-8"))
                    ack = system_a._apply_control_update(update)
                    writer.write(encode_frame(json.dumps(ack).encode("utf-8")))
                    await writer.drain()

            ack_task = asyncio.create_task(apply_control_updates())
            for message in (
                make_message(
                    MSG_HEARTBEAT,
                    "A",
                    "B",
                    "closed-loop-run",
                    {"seq": 0},
                ),
                *[
                    make_message(
                        MSG_FEEDBACK_REPORT,
                        "A",
                        "B",
                        "closed-loop-run",
                        {
                            "seq": seq,
                            "window_start": time.time() - 1.0,
                            "window_end": time.time(),
                            "queue_occupancy": 950,
                            "queue_capacity": 1000,
                            "generated": 100,
                            "delivered": 50,
                            "dropped": 50,
                            "mean_sinr_db": 4.0,
                            "per": 0.5,
                            "traffic_rate_pps": 100.0,
                        },
                        zone_id="Z3",
                    )
                    for seq in (1, 2)
                ],
            ):
                writer.write(encode_message(message))
            await writer.drain()

            deadline = asyncio.get_running_loop().time() + 2.0
            while controller_b.stats["feedback_accepted"] < 1:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("Laptop B did not accept congested feedback")
                await asyncio.sleep(0.01)

            await controller_b.closed_loop._iterate()
            while controller_b.stats["ris_updates_applied"] < 1:
                if asyncio.get_running_loop().time() >= deadline + 2.0:
                    raise AssertionError("Laptop A did not acknowledge the RIS update")
                await asyncio.sleep(0.01)

            result = controller_b.optimizer.latest_results()["Z3"]
            assert controller_b.zones.zones["Z3"].congestion_state == "SEVERE"
            assert result.decision == "APPLY"
            assert result.observed_sinr_db == 4.0
            assert system_a.metrics.snapshot()["oob"]["ris_updates_received"] == 1
            assert system_a.metrics.snapshot()["oob"]["scheduling_updates_received"] == 1
            assert system_a.receiver_model.zone_sinr_offset[2] == pytest.approx(
                result.predicted_sinr_gain_db
            )

            applied_panels = controller_b.panels.by_zone("Z3")
            assert len(applied_panels) == 6
            assert all(panel.config_version == 1 for panel in applied_panels)
            assert all(
                system_a.receiver_model.ris_state[index]["phase"]
                == result.selected_codebook
                for index in range(14, 20)
            )

            receiver_id = system_a.population.get_receivers(2)[0]
            system_a.receiver_model.set_zone_sinr_offset(2, 0.0)
            before = system_a.receiver_model.compute_sinr(receiver_id)[0]
            system_a.receiver_model.set_zone_sinr_offset(
                2, result.predicted_sinr_gain_db
            )
            after = system_a.receiver_model.compute_sinr(receiver_id)[0]
            assert after - before == pytest.approx(result.predicted_sinr_gain_db)
        finally:
            if ack_task is not None:
                ack_task.cancel()
                try:
                    await ack_task
                except asyncio.CancelledError:
                    pass
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            await controller_b.stop()
            system_a.shutdown()


def test_default_channel_feedback_drives_laptop_b_scheduling():
    asyncio.run(_exercise_default_channel_feedback_drives_scheduling())


async def _exercise_default_channel_feedback_drives_scheduling():
    with tempfile.TemporaryDirectory() as export_dir:
        system_a = LaptopASystem(
            mode="standalone",
            total_devices=100,
            num_zones=5,
            telemetry_port=0,
            export_dir=export_dir,
            show_dashboard=False,
            run_id="default-loss-run",
        )
        controller_b = ControllerB(
            load_config(str(Path(__file__).resolve().parents[1] / "config")),
            "default-loss-run",
        )
        writer = None
        ack_task = None
        try:
            port = await controller_b.start("127.0.0.1", 0)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            async def apply_control_updates():
                while True:
                    raw = await read_frame(reader)
                    update = json.loads(raw.decode("utf-8"))
                    ack = system_a._apply_control_update(update)
                    writer.write(encode_frame(json.dumps(ack).encode("utf-8")))
                    await writer.drain()

            ack_task = asyncio.create_task(apply_control_updates())
            heartbeat = make_message(
                MSG_HEARTBEAT,
                "A",
                "B",
                "default-loss-run",
                {"seq": 0},
            )
            writer.write(encode_message(heartbeat))
            for _ in range(2):
                for _ in range(15):
                    system_a.step()
                report = system_a.receiver_feedback.aggregate_report(system_a.sim_time)
                for message in system_a._build_b_feedback_messages(report, time.time()):
                    writer.write(encode_message(message))
            await writer.drain()

            deadline = asyncio.get_running_loop().time() + 3.0
            while controller_b.stats["feedback_accepted"] < 10:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("Laptop B did not accept A's simulated feedback")
                await asyncio.sleep(0.01)

            assert system_a.metrics.snapshot()["global"]["pdu_dropped"] > 0
            assert controller_b.zones.zones["Z4"].congestion_state == "CONGESTED"
            await controller_b.closed_loop._iterate()
            while controller_b.stats["scheduling_updates_applied"] < 1:
                if asyncio.get_running_loop().time() >= deadline + 2.0:
                    raise AssertionError("Laptop B did not apply a scheduling response")
                await asyncio.sleep(0.01)

            assert system_a.metrics.snapshot()["oob"]["scheduling_updates_received"] == 1
            assert controller_b.zones.zones["Z4"].last_decision.startswith("BOOST")
            assert "Z4" in controller_b.optimizer.latest_results()
        finally:
            if ack_task is not None:
                ack_task.cancel()
                try:
                    await ack_task
                except asyncio.CancelledError:
                    pass
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            await controller_b.stop()
            system_a.shutdown()
