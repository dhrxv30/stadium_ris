"""Message schemas and validation for Laptop A and B payloads.

Defines the payload structure for:
- MSG_TELEMETRY_A: Laptop A traffic generation, queue, and wireless link measurements
- MSG_TELEMETRY_B: Laptop B controller decisions, zone congestion, RIS panel states, OOB statistics
- MSG_EVENT: Discrete system events (PDU outcomes, congestion changes, RIS commands)
"""
from typing import Any, Dict, List, Optional, Tuple
from common.constants import ZONE_IDS, NUM_PANELS


def make_default_device_population(total: int = 50_000, num_zones: int = 5) -> Dict[str, Any]:
    """Default stadium device population: 50,000 devices across 5 zones."""
    senders_per_zone = (total // 2) // num_zones
    receivers_per_zone = (total // 2) // num_zones
    per_zone = {}
    for z in ZONE_IDS:
        per_zone[z] = {
            "senders": senders_per_zone,
            "receivers": receivers_per_zone,
            "total": senders_per_zone + receivers_per_zone,
        }
    return {
        "total": total,
        "senders": total // 2,
        "receivers": total // 2,
        "per_zone": per_zone,
    }


def make_default_panel_catalog() -> List[Dict[str, Any]]:
    """Initialize catalog of 32 virtual passive RIS panels mapped to 5 zones.
    
    Distribution:
    - Z1: P1 - P7  (7 panels)
    - Z2: P8 - P14 (7 panels)
    - Z3: P15 - P20 (6 panels)
    - Z4: P21 - P26 (6 panels)
    - Z5: P27 - P32 (6 panels)
    Total = 32 panels.
    """
    panels = []
    zone_distribution = [
        ("Z1", 1, 7),
        ("Z2", 8, 14),
        ("Z3", 15, 20),
        ("Z4", 21, 26),
        ("Z5", 27, 32),
    ]
    for zone, start_idx, end_idx in zone_distribution:
        for idx in range(start_idx, end_idx + 1):
            panels.append({
                "panel_id": f"P{idx}",
                "zone_id": zone,
                "current_config": "CB_0",  # default baseline codebook configuration
                "config_version": 0,
                "last_update_ts": None,
                "command_status": "IDLE",
                "health_state": "OK",
            })
    return panels


def validate_telemetry_a_payload(payload: dict) -> Tuple[bool, str]:
    """Validate incoming payload for MSG_TELEMETRY_A."""
    if not isinstance(payload, dict):
        return False, "Payload must be a dictionary"
    
    # Optional device_population check
    if "device_population" in payload:
        dp = payload["device_population"]
        if not isinstance(dp, dict):
            return False, "device_population must be a dictionary"
        if "total" in dp and not isinstance(dp["total"], (int, float)):
            return False, "device_population.total must be numeric"

    # Optional traffic_summary check
    if "traffic_summary" in payload:
        ts = payload["traffic_summary"]
        if not isinstance(ts, dict):
            return False, "traffic_summary must be a dictionary"
        for dir_key in ("uplink", "downlink"):
            if dir_key in ts and not isinstance(ts[dir_key], dict):
                return False, f"traffic_summary.{dir_key} must be a dictionary"

    # Optional zones check
    if "zones" in payload:
        zdict = payload["zones"]
        if not isinstance(zdict, dict):
            return False, "zones must be a dictionary keyed by zone_id"
        for zid in zdict:
            if zid not in ZONE_IDS:
                return False, f"Unknown zone_id '{zid}' in zones telemetry"

    return True, ""


def validate_telemetry_b_payload(payload: dict) -> Tuple[bool, str]:
    """Validate incoming payload for MSG_TELEMETRY_B."""
    if not isinstance(payload, dict):
        return False, "Payload must be a dictionary"

    if "zones" in payload:
        zdict = payload["zones"]
        if not isinstance(zdict, dict):
            return False, "zones must be a dictionary keyed by zone_id"
        for zid in zdict:
            if zid not in ZONE_IDS:
                return False, f"Unknown zone_id '{zid}' in zones telemetry"

    if "ris_panels" in payload:
        rlist = payload["ris_panels"]
        if not isinstance(rlist, list):
            return False, "ris_panels must be a list of panel dictionaries"

    if "oob_metrics" in payload:
        oob = payload["oob_metrics"]
        if not isinstance(oob, dict):
            return False, "oob_metrics must be a dictionary"

    return True, ""


def validate_event_payload(payload: dict) -> Tuple[bool, str]:
    """Validate incoming payload for MSG_EVENT."""
    if not isinstance(payload, dict):
        return False, "Payload must be a dictionary"
    if "event_type" not in payload:
        return False, "Payload missing 'event_type'"
    return True, ""
