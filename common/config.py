"""YAML configuration loading with validation and safe defaults."""
import copy
import yaml

DEFAULTS = {
    "protocol_version": 1,
    "laptop_c": {"bind_host": "127.0.0.1", "telemetry_port_a": 9101, "telemetry_port_b": 9102},
    "laptop_a": {"host": "127.0.0.1"},
    "laptop_b": {"host": "127.0.0.1"},
    "limits": {"max_message_bytes": 1_048_576, "read_timeout_s": 30.0},
    "stale_after_s": 5.0,
    "dedupe_window": 10000,
    "max_clock_skew_s": 300.0,
}


class ConfigError(ValueError):
    pass


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def validate_network_config(cfg):
    errs = []
    from .constants import PROTOCOL_VERSION
    if cfg["protocol_version"] != PROTOCOL_VERSION:
        errs.append(f"protocol_version {cfg['protocol_version']} != {PROTOCOL_VERSION}")
    for key in ("telemetry_port_a", "telemetry_port_b"):
        p = cfg["laptop_c"].get(key)
        if not isinstance(p, int) or not (0 <= p <= 65535):
            errs.append(f"laptop_c.{key} must be int 0-65535 (got {p!r})")
    if cfg["limits"]["max_message_bytes"] < 1024:
        errs.append("limits.max_message_bytes must be >= 1024")
    for key in ("stale_after_s", "max_clock_skew_s"):
        if not isinstance(cfg[key], (int, float)) or cfg[key] <= 0:
            errs.append(f"{key} must be > 0")
    if cfg["limits"]["read_timeout_s"] <= cfg["stale_after_s"]:
        errs.append("limits.read_timeout_s should exceed stale_after_s")
    if cfg["dedupe_window"] < 1:
        errs.append("dedupe_window must be >= 1")
    if errs:
        raise ConfigError("; ".join(errs))
    return cfg


def load_network_config(path=None, overrides=None):
    """Load YAML (if path given), merge onto defaults and CLI overrides, validate."""
    data = {}
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            raise ConfigError(f"config file not found: {path}")
        except yaml.YAMLError as e:
            raise ConfigError(f"invalid YAML in {path}: {e}")
        if not isinstance(data, dict):
            raise ConfigError("top-level YAML must be a mapping")

    # Map team schema to internal config format
    if "laptop_c" in data and isinstance(data["laptop_c"], dict):
        lc = data["laptop_c"]
        if "port" in lc and "telemetry_port_a" not in lc:
            lc["telemetry_port_a"] = lc["port"]
            lc["telemetry_port_b"] = lc["port"]
        if "host" in lc and "bind_host" not in lc:
            lc["bind_host"] = lc["host"]
    if "max_message_bytes" in data:
        data.setdefault("limits", {})["max_message_bytes"] = data["max_message_bytes"]
    if "idle_timeout_s" in data:
        data.setdefault("limits", {})["read_timeout_s"] = data["idle_timeout_s"]

    cfg = _merge(DEFAULTS, data)
    cfg = _merge(cfg, overrides)
    return validate_network_config(cfg)
