"""Laptop C telemetry envelope helpers."""

import json
import time
import uuid

from common.constants import PROTOCOL_VERSION


def make_message(
    msg_type,
    source,
    destination,
    run_id,
    payload=None,
    *,
    seq=None,
    zone_id=None,
    panel_id=None,
    correlation_id=None,
    timestamp=None,
    message_id=None,
):
    return {
        "protocol_version": PROTOCOL_VERSION,
        "msg_type": msg_type,
        "message_id": message_id or uuid.uuid4().hex,
        "source": source,
        "destination": destination,
        "timestamp": time.time() if timestamp is None else timestamp,
        "run_id": run_id,
        "seq": seq,
        "zone_id": zone_id,
        "panel_id": panel_id,
        "correlation_id": correlation_id,
        "payload": payload if payload is not None else {},
    }


def encode_message(message):
    return json.dumps(
        message, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
