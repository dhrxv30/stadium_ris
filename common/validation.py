"""Validation of incoming messages and of YAML configuration."""
import math
import re
from pathlib import Path

import yaml

from common.constants import (LOG_LEVELS, NODES, PROTOCOL_VERSION, ZONE_IDS,
                               MSG_RIS_COMMAND, MSG_RIS_ACK,
                               MSG_TELEMETRY_EVENT, MSG_CONTROLLER_STATE)
from common.schemas import ENVELOPE_OPTIONAL, ENVELOPE_REQUIRED, PAYLOAD_SCHEMAS

ZONE_RE = re.compile(r"^Z[1-5]$")
PANEL_RE = re.compile(r"^P(0[1-9]|[12]\d|3[0-2])$")


class ValidationError(ValueError):
    pass


class ConfigError(ValueError):
    pass


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _type_ok(value, expected):
    if expected is int:
        return _is_int(value)
    if expected is float:
        return _is_num(value)
    return isinstance(value, expected)


def validate_message(msg: dict, expected_version: int = PROTOCOL_VERSION) -> None:
    """Raise ValidationError describing the first problem found."""
    for key, typ in ENVELOPE_REQUIRED.items():
        if key not in msg:
            raise ValidationError(f"missing envelope field '{key}'")
        if not _type_ok(msg[key], typ):
            raise ValidationError(f"envelope field '{key}' must be {typ.__name__}")
    for key, typ in ENVELOPE_OPTIONAL.items():
        if msg.get(key) is not None and not _type_ok(msg[key], typ):
            raise ValidationError(f"envelope field '{key}' must be {typ.__name__}")

    if msg["version"] != expected_version:
        raise ValidationError(f"protocol version {msg['version']} != {expected_version}")
    if msg["src"] not in NODES or msg["dst"] not in NODES:
        raise ValidationError("src/dst must be one of A, B, C")
    if msg.get("zone_id") is not None and not ZONE_RE.match(msg["zone_id"]):
        raise ValidationError(f"invalid zone_id '{msg['zone_id']}'")
    if msg.get("panel_id") is not None and not PANEL_RE.match(msg["panel_id"]):
        raise ValidationError(f"invalid panel_id '{msg['panel_id']}'")

    schema = PAYLOAD_SCHEMAS.get(msg["type"])
    if schema is None:
        raise ValidationError(f"unknown message type '{msg['type']}'")
    if schema["requires_zone_id"] and msg.get("zone_id") is None:
        raise ValidationError(f"{msg['type']} requires envelope zone_id")

    payload = msg["payload"]
    for key, typ in schema["required"].items():
        if payload.get(key) is None:
            raise ValidationError(f"missing payload field '{key}'")
        if not _type_ok(payload[key], typ):
            raise ValidationError(f"payload field '{key}' must be {typ.__name__}")
    for key, typ in schema["optional"].items():
        if payload.get(key) is not None and not _type_ok(payload[key], typ):
            raise ValidationError(f"payload field '{key}' must be {typ.__name__}")

    _semantic_checks(msg["type"], payload)


def _semantic_checks(msg_type, p):
    if msg_type == "FEEDBACK_REPORT":
        if p["seq"] < 0:
            raise ValidationError("seq must be >= 0")
        if p["window_end"] < p["window_start"]:
            raise ValidationError("window_end < window_start")
        for k in ("queue_occupancy", "queue_capacity", "generated", "delivered", "dropped"):
            if p[k] < 0:
                raise ValidationError(f"{k} must be >= 0")
        if p["queue_capacity"] == 0 or p["queue_occupancy"] > p["queue_capacity"]:
            raise ValidationError("queue_occupancy must be <= queue_capacity (> 0)")
        if p.get("per") is not None and not 0.0 <= p["per"] <= 1.0:
            raise ValidationError("per must be within 0..1")
        if p.get("traffic_rate_pps") is not None and p["traffic_rate_pps"] < 0:
            raise ValidationError("traffic_rate_pps must be >= 0")
    elif msg_type == MSG_RIS_COMMAND:
        if p["codebook_index"] < 0:
            raise ValidationError("codebook_index must be >= 0")
        if p["config_version"] < 0:
            raise ValidationError("config_version must be >= 0")
        if p["expires_ts"] <= p["created_ts"]:
            raise ValidationError("expires_ts must be after created_ts")
    elif msg_type == MSG_RIS_ACK:
        if p["status"] not in ("ACKED", "REJECTED"):
            raise ValidationError("RIS_ACK status must be ACKED or REJECTED")
    # TELEMETRY_EVENT and CONTROLLER_STATE have flexible payloads; no extra checks here


# ----------------------------------------------------------------- configuration
def load_config(config_dir, overrides=None) -> dict:
    """Load network/stadium/ris YAML (and optional traffic.yaml), apply CLI overrides, validate."""
    config_dir = Path(config_dir)
    cfg = {}
    for name in ("network", "stadium", "ris"):
        path = config_dir / f"{name}.yaml"
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg[name] = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    # traffic.yaml is optional (used by Laptop A; B/C tolerate its absence)
    traffic_path = config_dir / "traffic.yaml"
    if traffic_path.is_file():
        try:
            with open(traffic_path, "r", encoding="utf-8") as fh:
                cfg["traffic"] = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{traffic_path}: invalid YAML: {exc}") from exc
    _apply_overrides(cfg, overrides or {})
    errors = _validate_config(cfg)
    if errors:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errors))
    return cfg


def _apply_overrides(cfg, ov):
    net = cfg["network"]
    if ov.get("host") is not None:
        net.setdefault("laptop_b", {})["host"] = ov["host"]
    if ov.get("port") is not None:
        net.setdefault("laptop_b", {})["port"] = ov["port"]
    run = net.setdefault("run", {})
    for key in ("seed", "duration_s", "log_level", "status_interval_s", "output_dir"):
        if ov.get(key) is not None:
            run[key] = ov[key]


def _validate_config(cfg) -> list:
    errs = []
    net, stad, ris = cfg["network"], cfg["stadium"], cfg["ris"]

    if net.get("protocol_version") != PROTOCOL_VERSION:
        errs.append(f"network.protocol_version must be {PROTOCOL_VERSION}")
    for node in ("laptop_a", "laptop_b", "laptop_c"):
        n = net.get(node)
        if not isinstance(n, dict) or not isinstance(n.get("host"), str):
            errs.append(f"network.{node}.host must be a string")
            continue
        if not _is_int(n.get("port")) or not 0 <= n["port"] <= 65535:
            errs.append(f"network.{node}.port must be an integer 0-65535 (0 = ephemeral, tests only)")
    for key in ("max_message_bytes",):
        if not _is_int(net.get(key)) or net[key] <= 0:
            errs.append(f"network.{key} must be a positive integer")
    for key in ("connect_timeout_s", "reconnect_interval_s", "idle_timeout_s"):
        if not _is_num(net.get(key)) or net[key] <= 0:
            errs.append(f"network.{key} must be a positive number")
    run = net.get("run", {})
    if not _is_int(run.get("seed")):
        errs.append("network.run.seed must be an integer")
    if not _is_num(run.get("status_interval_s")) or run["status_interval_s"] <= 0:
        errs.append("network.run.status_interval_s must be a positive number")
    if not _is_num(run.get("duration_s")) or run["duration_s"] < 0:
        errs.append("network.run.duration_s must be >= 0 (0 = run until stopped)")
    if run.get("log_level") not in LOG_LEVELS:
        errs.append(f"network.run.log_level must be one of {LOG_LEVELS}")
    if not isinstance(run.get("output_dir"), str):
        errs.append("network.run.output_dir must be a string")

    if stad.get("zones") != ZONE_IDS:
        errs.append(f"stadium.zones must be exactly {ZONE_IDS}")
    for key in ("senders_per_zone", "receivers_per_zone"):
        if not _is_int(stad.get(key)) or stad[key] <= 0:
            errs.append(f"stadium.{key} must be a positive integer")

    pc = ris.get("panel_count")
    zpc = ris.get("zone_panel_counts")
    if not _is_int(pc) or pc <= 0:
        errs.append("ris.panel_count must be a positive integer")
    if (not isinstance(zpc, list) or len(zpc) != len(ZONE_IDS)
            or not all(_is_int(x) and x >= 0 for x in zpc)):
        errs.append("ris.zone_panel_counts must be a list of 5 non-negative integers")
    elif _is_int(pc) and sum(zpc) != pc:
        errs.append(f"ris.zone_panel_counts sums to {sum(zpc)}, expected panel_count={pc}")
    cs = ris.get("codebook_size")
    if not _is_int(cs) or cs < 1:
        errs.append("ris.codebook_size must be an integer >= 1")
    ic = ris.get("initial_codebook_index")
    if not _is_int(ic) or (_is_int(cs) and not 0 <= ic < cs):
        errs.append("ris.initial_codebook_index must be within 0..codebook_size-1")
    if not _is_num(ris.get("stale_after_s")) or ris["stale_after_s"] <= 0:
        errs.append("ris.stale_after_s must be a positive number")
    return errs
