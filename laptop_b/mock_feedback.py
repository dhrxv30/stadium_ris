"""Mock Laptop A: sends a deterministic mix of valid and invalid messages to B over real TCP.

The VALUES are synthetic test data (seeded RNG); they are not measurements.
"""
import asyncio
import random
import time
from collections import Counter

from common.constants import DEFAULT_MAX_MESSAGE_BYTES, MSG_FEEDBACK_REPORT, MSG_HEARTBEAT
from common.framing import encode_frame
from common.messages import encode_message, make_message


def _report(rng, run_id, zone_id, seq, now):
    gen = rng.randint(100, 500)
    dropped = rng.randint(0, gen // 4)
    payload = {
        "seq": seq, "window_start": now - 1.0, "window_end": now,
        "queue_occupancy": rng.randint(0, 1000), "queue_capacity": 1000,
        "generated": gen, "delivered": gen - dropped, "dropped": dropped,
        "mean_sinr_db": round(rng.uniform(5, 25), 2),
        "per": round(dropped / gen, 4),
        "traffic_rate_pps": float(gen),
    }
    return make_message(MSG_FEEDBACK_REPORT, "A", "B", run_id, payload, zone_id=zone_id)


async def run_mock_a(host, port, run_id, zone_ids, reports_per_zone=3, seed=42,
                     max_bytes=DEFAULT_MAX_MESSAGE_BYTES) -> Counter:
    """Returns the Counter of outcomes B is EXPECTED to report (for self-checking)."""
    rng = random.Random(seed)
    exp = Counter()
    seq = {z: 0 for z in zone_ids}
    reader, writer = await asyncio.open_connection(host, port)

    async def send_bytes(b):
        writer.write(b)
        await writer.drain()

    last = None
    for _ in range(reports_per_zone):
        for z in zone_ids:
            seq[z] += 1
            last = _report(rng, run_id, z, seq[z], time.time())
            await send_bytes(encode_message(last, max_bytes))
            exp["accepted"] += 1

    z0 = zone_ids[0]
    # 1. heartbeat
    await send_bytes(encode_message(make_message(MSG_HEARTBEAT, "A", "B", run_id, {"seq": 1}), max_bytes))
    exp["heartbeats"] += 1
    # 2. fragmented valid message (exercises TCP framing)
    seq[z0] += 1
    frame = encode_message(_report(rng, run_id, z0, seq[z0], time.time()), max_bytes)
    await send_bytes(frame[:3]); await asyncio.sleep(0.02)
    await send_bytes(frame[3:20]); await asyncio.sleep(0.02)
    await send_bytes(frame[20:])
    exp["accepted"] += 1
    # 3. duplicate (same msg_id resent)
    dup = _report(rng, run_id, z0, seq[z0] + 1, time.time())
    await send_bytes(encode_message(dup, max_bytes))
    exp["accepted"] += 1
    await send_bytes(encode_message(dup, max_bytes))
    exp["rejected_duplicate"] += 1
    seq[z0] += 1
    # 4. stale (window_end 1000 s in the past)
    old = _report(rng, run_id, z0, seq[z0] + 1, time.time() - 1000)
    await send_bytes(encode_message(old, max_bytes))
    exp["rejected_stale"] += 1
    # 5. malformed: bad JSON, missing payload field, wrong protocol version
    await send_bytes(encode_frame(b"{not json", max_bytes))
    bad = _report(rng, run_id, z0, seq[z0] + 1, time.time())
    del bad["payload"]["delivered"]
    await send_bytes(encode_message(bad, max_bytes))
    wrong_ver = _report(rng, run_id, z0, seq[z0] + 1, time.time())
    wrong_ver["version"] = 99
    await send_bytes(encode_message(wrong_ver, max_bytes))
    exp["malformed"] += 3

    exp["frames"] = sum(exp[k] for k in ("accepted", "heartbeats", "rejected_duplicate",
                                         "rejected_stale", "malformed"))
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return exp
