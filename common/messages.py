"""Laptop A/B message construction and JSON encoding."""

import json
import time
import uuid

from common.constants import DEFAULT_MAX_MESSAGE_BYTES, PROTOCOL_VERSION
from common.framing import encode_frame


class MessageError(ValueError):
    """Raised when raw bytes cannot be decoded into a JSON object."""


def make_message(
    msg_type,
    src,
    dst,
    run_id,
    payload,
    *,
    zone_id=None,
    panel_id=None,
    correlation_id=None,
    ts=None,
    msg_id=None,
    version=PROTOCOL_VERSION,
):
    message = {
        "version": version,
        "type": msg_type,
        "msg_id": msg_id or uuid.uuid4().hex,
        "src": src,
        "dst": dst,
        "ts": float(time.time() if ts is None else ts),
        "run_id": run_id,
        "payload": payload,
    }
    if zone_id is not None:
        message["zone_id"] = zone_id
    if panel_id is not None:
        message["panel_id"] = panel_id
    if correlation_id is not None:
        message["correlation_id"] = correlation_id
    return message


def encode_message(
    message: dict, max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
) -> bytes:
    """Serialize a complete framed message for a TCP writer."""
    raw = json.dumps(
        message, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return encode_frame(raw, max_bytes)


def encode_json_message(message: dict) -> bytes:
    """Serialize an unframed JSON payload for callers that frame separately."""
    return json.dumps(
        message, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def decode_message(raw: bytes) -> dict:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MessageError(f"not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise MessageError("top-level JSON value must be an object")
    return obj
