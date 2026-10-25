"""Mock telemetry generators for Laptop A and B (Stage C2).

Used ONLY to test Laptop C in standalone mode before real Laptop A and B connect.
Every payload explicitly carries "mock": true so mock test data can never be
confused with live measurements. Values are synthetic simulation test inputs.
"""
import asyncio
import random
import time
from common.constants import (
    LAPTOP_A,
    LAPTOP_B,
    LAPTOP_C,
    MSG_EVENT,
    MSG_HEARTBEAT,
    MSG_HELLO,
    MSG_TELEMETRY_A,
    MSG_TELEMETRY_B,
    NUM_PANELS,
    ZONE_IDS,
)
from common.framing import encode_frame
from common.messages import encode_message, make_message
from common.schemas import make_default_device_population, make_default_panel_catalog


class MockPeer:
    def __init__(self, name, host, port, run_id, interval_s=0.5, seed=1):
        assert name in (LAPTOP_A, LAPTOP_B)
        self.name, self.host, self.port = name, host, port
        self.run_id, self.interval_s = run_id, interval_s
        self.rng = random.Random(seed)
        self.seq = 0
        self.sent = 0

        # Stateful counters for realistic simulation progression
        self.ul_gen = 0
        self.ul_del = 0
        self.ul_drop = 0
        self.dl_gen = 0
        self.dl_del = 0
        self.dl_drop = 0
        self.oob_cmds = 0
        self.oob_acks = 0
        self.oob_timeouts = 0

        # RIS catalog state
        self.panels = make_default_panel_catalog()
        self.device_pop = make_default_device_population(50000, 5)

    def _msg(self, mtype, payload, zone=None, panel=None, corr=None):
        m = make_message(
            mtype,
            self.name,
            LAPTOP_C,
            self.run_id,
            payload,
            seq=self.seq,
            zone_id=zone,
            panel_id=panel,
            correlation_id=corr,
        )
        self.seq += 1
        return m

    async def _send(self, writer, msg):
        writer.write(encode_frame(encode_message(msg)))
        await writer.drain()
        self.sent += 1

    def _generate_telemetry_a(self) -> dict:
        """Synthesize rich Laptop A PDU, queue, and link telemetry."""
        new_ul = self.rng.randint(50, 150)
        new_dl = self.rng.randint(50, 150)
        self.ul_gen += new_ul
        self.dl_gen += new_dl

        # 95-98% delivery, small drop rate
        del_ul = int(new_ul * self.rng.uniform(0.93, 0.98))
        del_dl = int(new_dl * self.rng.uniform(0.93, 0.98))
        drop_ul = new_ul - del_ul
        drop_dl = new_dl - del_dl

        self.ul_del += del_ul
        self.dl_del += del_dl
        self.ul_drop += drop_ul
        self.dl_drop += drop_dl

        zones_data = {}
        for zid in ZONE_IDS:
            q_occ = self.rng.randint(5, 45)
            sinr = round(self.rng.uniform(12.0, 26.0), 1)
            per = round(self.rng.uniform(0.005, 0.04), 3)
            zones_data[zid] = {
                "generated_ul": self.ul_gen // 5,
                "delivered_ul": self.ul_del // 5,
                "dropped_ul": self.ul_drop // 5,
                "pending_ul": self.rng.randint(2, 10),
                "generated_dl": self.dl_gen // 5,
                "delivered_dl": self.dl_del // 5,
                "dropped_dl": self.dl_drop // 5,
                "pending_dl": self.rng.randint(2, 10),
                "queue_occupancy": q_occ,
                "queue_capacity": 500,
                "sinr_db": sinr,
                "estimated_per": per,
                "mean_latency_ms": round(self.rng.uniform(12.0, 22.0), 1),
                "p95_latency_ms": round(self.rng.uniform(28.0, 48.0), 1),
                "throughput_bps": self.rng.randint(1_000_000, 2_500_000),
            }

        return {
            "mock": True,
            "device_population": self.device_pop,
            "traffic_summary": {
                "uplink": {
                    "generated": self.ul_gen,
                    "delivered": self.ul_del,
                    "dropped": self.ul_drop,
                    "pending": self.rng.randint(10, 50),
                    "timed_out": 0,
                },
                "downlink": {
                    "generated": self.dl_gen,
                    "delivered": self.dl_del,
                    "dropped": self.dl_drop,
                    "pending": self.rng.randint(10, 50),
                    "timed_out": 0,
                },
                "ack_count": self.ul_del + self.dl_del,
                "nack_count": self.ul_drop + self.dl_drop,
                "retry_count": self.rng.randint(5, 20),
            },
            "zones": zones_data,
            "metrics": {
                "mean_latency_ms": round(self.rng.uniform(14.0, 20.0), 1),
                "p95_latency_ms": round(self.rng.uniform(32.0, 44.0), 1),
                "throughput_bps": round(self.rng.uniform(6_000_000, 10_000_000)),
            },
        }

    def _generate_telemetry_b(self) -> dict:
        """Synthesize rich Laptop B controller decisions, RIS panels, and OOB telemetry."""
        target_zone = self.rng.choice(ZONE_IDS)
        target_panel_idx = self.rng.randint(1, NUM_PANELS)
        target_panel_id = f"P{target_panel_idx}"

        self.oob_cmds += 1
        if self.rng.random() > 0.1:
            self.oob_acks += 1
            cmd_status = "ACKED"
        else:
            self.oob_timeouts += 1
            cmd_status = "TIMEOUT"

        cmd_id = f"CMD-{self.oob_cmds:04d}"

        # Update affected panel
        for p in self.panels:
            if p["panel_id"] == target_panel_id:
                p["current_config"] = f"CB_{self.rng.randint(1, 8)}"
                p["config_version"] += 1
                p["command_status"] = "APPLIED" if cmd_status == "ACKED" else "FAILED"
                p["last_update_ts"] = time.time()
                break

        zones_data = {}
        for zid in ZONE_IDS:
            cong = "NORMAL"
            if zid == target_zone and self.rng.random() > 0.5:
                cong = self.rng.choice(["ELEVATED", "CONGESTED"])
            zones_data[zid] = {
                "congestion_state": cong,
                "queue_occupancy": self.rng.randint(10, 80),
                "queue_delay_ms": round(self.rng.uniform(3.0, 15.0), 1),
                "traffic_rate_bps": self.rng.randint(1_200_000, 3_000_000),
                "sinr_db": round(self.rng.uniform(14.0, 25.0), 1),
                "pdr": round(self.rng.uniform(94.0, 99.0), 1),
                "per": round(self.rng.uniform(0.01, 0.05), 3),
                "last_decision": "RIS_RECONFIGURE" if zid == target_zone else "NO_CHANGE",
                "last_ris_update": f"CB_{self.rng.randint(0, 4)}",
            }

        return {
            "mock": True,
            "controller_status": {
                "active_policy": "congestion_aware",
                "last_decision": {
                    "decision": "RIS_RECONFIGURE",
                    "reason": f"CONGESTION_MITIGATION_{target_zone}",
                    "zone_id": target_zone,
                    "panel_id": target_panel_id,
                    "config": f"CB_{self.rng.randint(1, 8)}",
                    "timestamp": time.time(),
                },
            },
            "zones": zones_data,
            "ris_panels": self.panels,
            "oob_metrics": {
                "tx_attempts": self.oob_cmds + self.rng.randint(0, 3),
                "unique_commands": self.oob_cmds,
                "acknowledged": self.oob_acks,
                "negative_acknowledged": 0,
                "retries": self.rng.randint(0, 2),
                "timeouts": self.oob_timeouts,
                "ack_ratio": round((self.oob_acks / self.oob_cmds) * 100.0, 1),
                "last_command_id": cmd_id,
                "last_command_status": cmd_status,
                "last_command_ts": time.time(),
                "last_ack_ts": time.time() if cmd_status == "ACKED" else None,
            },
        }

    async def run(self, duration_s=None, stop_event=None):
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            await self._send(writer, self._msg(MSG_HELLO, {"mock": True, "role": self.name}))
            t = 0.0
            step = 0
            while (duration_s is None or t < duration_s) and not (stop_event and stop_event.is_set()):
                if self.name == LAPTOP_A:
                    payload = self._generate_telemetry_a()
                    await self._send(writer, self._msg(MSG_TELEMETRY_A, payload))
                    # Occasionally emit PDU delivery / drop event
                    if step % 3 == 0:
                        evt = {
                            "mock": True,
                            "event_type": "PDU_DELIVERY",
                            "event_id": f"PDU-{self.rng.randint(1000, 9999)}",
                            "zone_id": self.rng.choice(ZONE_IDS),
                            "details": "Simulated batch delivery confirmed",
                        }
                        await self._send(writer, self._msg(MSG_EVENT, evt))
                else:
                    payload = self._generate_telemetry_b()
                    await self._send(writer, self._msg(MSG_TELEMETRY_B, payload))
                    # Occasionally emit RIS command event
                    if step % 2 == 0:
                        evt = {
                            "mock": True,
                            "event_type": "RIS_COMMAND",
                            "event_id": f"EVT-CMD-{self.oob_cmds}",
                            "command_id": f"CMD-{self.oob_cmds:04d}",
                            "panel_id": f"P{self.rng.randint(1, NUM_PANELS)}",
                            "zone_id": self.rng.choice(ZONE_IDS),
                            "details": "Adaptive phase codebook dispatched via OOB",
                        }
                        await self._send(writer, self._msg(MSG_EVENT, evt))

                await self._send(writer, self._msg(MSG_HEARTBEAT, {"mock": True}))
                await asyncio.sleep(self.interval_s)
                t += self.interval_s
                step += 1
        finally:
            writer.close()
