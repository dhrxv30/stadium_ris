"""Common message envelope (JSON). Every message carries this metadata."""
import json
import time
import uuid
from .constants import PROTOCOL_VERSION


def make_message(msg_type, source, destination, run_id, payload=None, *,
                 seq=None, zone_id=None, panel_id=None, correlation_id=None,
                 timestamp=None, message_id=None) -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "msg_type": msg_type,
        "message_id": message_id or uuid.uuid4().hex,
        "source": source,
        "destination": destination,
        "timestamp": time.time() if timestamp is None else timestamp,  # epoch s (sender clock)
        "run_id": run_id,
        "seq": seq,                        # per-sender monotonically increasing counter
        "zone_id": zone_id,
        "panel_id": panel_id,
        "correlation_id": correlation_id,  # e.g. OOB command ID
        "payload": payload if payload is not None else {},
    }


def encode_message(msg: dict) -> bytes:
    return json.dumps(msg, separators=(",", ":")).encode("utf-8")


def decode_message(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))
