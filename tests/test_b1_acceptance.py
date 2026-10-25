"""Stage B1 acceptance: B starts, accepts valid feedback, reports malformed input without crashing."""
import asyncio
import copy
from pathlib import Path

import pytest

from common.constants import MSG_FEEDBACK_REPORT
from common.messages import make_message
from common.validation import ConfigError, load_config
from laptop_b.controller import ControllerB
from laptop_b.mock_feedback import run_mock_a
from laptop_b.ris_panels import PanelRegistry
from laptop_b.zone_manager import ACCEPTED, REJ_DUPLICATE, REJ_STALE, ZoneManager

CFG_DIR = Path(__file__).resolve().parent.parent / "config"


def test_config_loads_and_overrides():
    cfg = load_config(CFG_DIR, {"port": 12345, "seed": 7})
    assert cfg["network"]["laptop_b"]["port"] == 12345
    assert cfg["network"]["run"]["seed"] == 7


def test_invalid_config_reports_clear_errors():
    with pytest.raises(ConfigError) as e:
        load_config(CFG_DIR, {"port": 99999, "log_level": "LOUD"})
    assert "port" in str(e.value) and "log_level" in str(e.value)


def test_missing_config_dir():
    with pytest.raises(ConfigError):
        load_config(CFG_DIR / "nope")


def test_32_panels_over_5_zones():
    cfg = load_config(CFG_DIR)
    reg = PanelRegistry.build(cfg["stadium"]["zones"], cfg["ris"]["zone_panel_counts"], 0)
    assert reg.total == 32 and reg.active_configs == 32 and reg.pending_commands == 0
    assert len({p.panel_id for p in reg.panels.values()}) == 32
    assert sum(len(reg.by_zone(z)) for z in cfg["stadium"]["zones"]) == 32
    assert all(p.config_version == 0 and p.health == "OK" for p in reg.panels.values())


def _msg(seq, we, zone="Z1", msg_id=None):
    p = {"seq": seq, "window_start": we - 1, "window_end": we, "queue_occupancy": 5,
         "queue_capacity": 10, "generated": 10, "delivered": 8, "dropped": 2}
    return make_message(MSG_FEEDBACK_REPORT, "A", "B", "r", p, zone_id=zone, msg_id=msg_id)


def test_zone_manager_accept_duplicate_stale_and_na():
    now = [1000.0]
    zm = ZoneManager(["Z1", "Z2"], stale_after_s=5.0, clock=lambda: now[0])
    z = zm.zones["Z1"]
    assert z.mean_sinr_db is None and z.queue_occupancy is None          # N/A until reported
    m1 = _msg(1, 1000.0)
    assert zm.process_feedback(m1)[0] == ACCEPTED
    assert z.pdr == pytest.approx(0.8) and z.mean_sinr_db is None        # missing metric stays None
    assert zm.process_feedback(copy.deepcopy(m1))[0] == REJ_DUPLICATE    # same msg_id
    assert zm.process_feedback(_msg(1, 1000.0))[0] == REJ_DUPLICATE      # same seq, new msg_id
    assert zm.process_feedback(_msg(0, 1000.0))[0] == REJ_STALE          # older seq
    assert zm.process_feedback(_msg(2, 900.0))[0] == REJ_STALE           # window too old
    assert zm.process_feedback(_msg(2, 1000.0))[0] == ACCEPTED
    assert zm.zones["Z2"].reports_accepted == 0                          # zones independent


def test_end_to_end_mock_over_real_tcp():
    cfg = load_config(CFG_DIR)

    async def scenario():
        ctrl = ControllerB(cfg, "test-run")
        port = await ctrl.start("127.0.0.1", 0)
        exp = await run_mock_a("127.0.0.1", port, "test-run", cfg["stadium"]["zones"], 3, 42)
        for _ in range(100):
            if ctrl.stats["frames_received"] >= exp["frames"]:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.05)
        await ctrl.stop()
        return ctrl, exp

    ctrl, exp = asyncio.run(scenario())
    s = ctrl.stats
    assert s["feedback_accepted"] == exp["accepted"] == 5 * 3 + 2
    assert s["heartbeats"] == 1
    assert s["rejected_duplicate"] == 1
    assert s["rejected_stale"] == 1
    assert s["malformed"] == 3
    assert s["frames_received"] == exp["frames"]
    assert all(z.reports_accepted >= 3 for z in ctrl.zones.zones.values())
    assert "Laptop A link=" in ctrl.status_summary()


def test_oversized_frame_closes_connection_but_b_survives():
    cfg = load_config(CFG_DIR)

    async def scenario():
        ctrl = ControllerB(cfg, "t")
        port = await ctrl.start("127.0.0.1", 0)
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write((cfg["network"]["max_message_bytes"] + 1).to_bytes(4, "big"))
        await w.drain()
        assert await asyncio.wait_for(r.read(10), 2) == b""       # B closed this connection
        w.close()
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)  # still accepting
        w2.close()
        await asyncio.sleep(0.05)
        await ctrl.stop()
        return ctrl

    ctrl = asyncio.run(scenario())
    assert ctrl.stats["framing_errors"] == 1
