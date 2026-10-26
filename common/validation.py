"""Schema validation for incoming messages. Returns (ok, error_string)."""
import math
import re
import time
from .constants import PROTOCOL_VERSION, MESSAGE_TYPES, MODULES, ZONE_IDS, NUM_PANELS

_REQUIRED = {
    "protocol_version": int, "msg_type": str, "message_id": str, "source": str,
    "destination": str, "timestamp": (int, float), "run_id": str, "payload": dict,
}
_PANEL_RE = re.compile(r"^P(\d{1,2})$")


def normalize_message(msg):
    """Normalize envelope fields across team schemas."""
    if not isinstance(msg, dict):
        return msg
    if "protocol_version" not in msg and "version" in msg:
        msg["protocol_version"] = msg["version"]
    if "msg_type" not in msg:
        if "type" in msg:
            msg["msg_type"] = msg["type"]
        elif "message_type" in msg:
            msg["msg_type"] = msg["message_type"]
    if "source" not in msg and "src" in msg:
        msg["source"] = msg["src"]
    if msg.get("source") in ("LAPTOP_A", "node_a", "laptop_a"):
        msg["source"] = "A"
    elif msg.get("source") in ("LAPTOP_B", "node_b", "laptop_b"):
        msg["source"] = "B"
    elif msg.get("source") in ("LAPTOP_C", "node_c", "laptop_c"):
        msg["source"] = "C"
    if "destination" not in msg:
        if "dst" in msg:
            msg["destination"] = msg["dst"]
        elif msg.get("message_type") or msg.get("source") in ("A", "LAPTOP_A", "B", "LAPTOP_B"):
            msg["destination"] = "C"
    if msg.get("destination") in ("LAPTOP_C", "node_c", "laptop_c"):
        msg["destination"] = "C"
    if "timestamp" not in msg:
        if "ts" in msg:
            msg["timestamp"] = msg["ts"]
        elif "sim_time" in msg or msg.get("message_type"):
            msg["timestamp"] = time.time()
    if "message_id" not in msg:
        if "msg_id" in msg:
            msg["message_id"] = msg["msg_id"]
        elif "id" in msg:
            msg["message_id"] = msg["id"]
        elif msg.get("message_type"):
            msg["message_id"] = f"lap_a_{time.time()}"
    if "run_id" not in msg and msg.get("message_type"):
        msg["run_id"] = "RUN_001"
    if "payload" not in msg:
        if msg.get("msg_type") == "TELEMETRY_UPDATE" and "snapshot" in msg:
            msg["payload"] = {"snapshot": msg["snapshot"]}
        elif msg.get("msg_type") == "LAPTOP_A_INIT":
            msg["payload"] = {k: v for k, v in msg.items() if k not in (
                "protocol_version", "msg_type", "message_id", "source", "destination", "timestamp", "run_id")}
        elif "snapshot" in msg:
            msg["payload"] = {"snapshot": msg["snapshot"]}
    return msg


def validate_message(msg, max_clock_skew_s=300.0, now=None):
    """Validate an already-decoded message. Returns (True, '') or (False, reason)."""
    now = time.time() if now is None else now
    if not isinstance(msg, dict):
        return False, "message is not a JSON object"
    normalize_message(msg)
    for k, t in _REQUIRED.items():
        if k not in msg:
            return False, f"missing field '{k}'"
        if isinstance(msg[k], bool) or not isinstance(msg[k], t):
            return False, f"field '{k}' has wrong type"
    if msg["protocol_version"] != PROTOCOL_VERSION:
        return False, (f"unsupported protocol_version {msg['protocol_version']} "
                       f"(expected {PROTOCOL_VERSION})")
    if msg["msg_type"] not in MESSAGE_TYPES:
        return False, f"unknown msg_type '{msg['msg_type']}'"
    if msg["source"] not in MODULES or msg["destination"] not in MODULES:
        return False, "source/destination must be one of A,B,C"
    ts = msg["timestamp"]
    if not math.isfinite(ts) or ts < 0:
        return False, "timestamp not finite/non-negative"
    if ts - now > max_clock_skew_s:
        return False, "timestamp too far in the future"
    if not msg["run_id"] or not msg["message_id"]:
        return False, "empty run_id or message_id"
    seq = msg.get("seq")
    if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 0):
        return False, "seq must be a non-negative int or null"
    z = msg.get("zone_id")
    if z is not None and z not in ZONE_IDS:
        return False, f"invalid zone_id '{z}'"
    p = msg.get("panel_id")
    if p is not None:
        m = _PANEL_RE.match(str(p))
        if not m or not (1 <= int(m.group(1)) <= NUM_PANELS):
            return False, f"invalid panel_id '{p}'"
    return True, ""
