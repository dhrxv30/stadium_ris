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


def validate_message(msg, max_clock_skew_s=300.0, now=None):
    """Validate an already-decoded message. Returns (True, '') or (False, reason)."""
    now = time.time() if now is None else now
    if not isinstance(msg, dict):
        return False, "message is not a JSON object"
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
