"""Message schemas (protocol v1). Declarative; enforced by common/validation.py."""
from common.constants import (
    MSG_FEEDBACK_REPORT, MSG_HEARTBEAT,
    MSG_RIS_COMMAND, MSG_RIS_ACK,
    MSG_TELEMETRY_EVENT, MSG_CONTROLLER_STATE,
)

# Envelope fields present in every message.
ENVELOPE_REQUIRED = {
    "version": int,
    "type": str,
    "msg_id": str,
    "src": str,
    "dst": str,
    "ts": float,        # sender wall-clock, seconds since epoch
    "run_id": str,
    "payload": dict,
}
# Optional envelope fields (may be absent or null).
ENVELOPE_OPTIONAL = {"zone_id": str, "panel_id": str, "correlation_id": str}

# Payload schemas. "required" must be present and non-null.
# "optional" may be absent or null; null means "measurement unavailable" (shown as N/A).
PAYLOAD_SCHEMAS = {
    MSG_FEEDBACK_REPORT: {
        "requires_zone_id": True,
        "required": {
            "seq": int,              # per-zone, strictly increasing
            "window_start": float,   # measurement window, epoch seconds
            "window_end": float,
            "queue_occupancy": int,  # PDUs currently queued in the zone
            "queue_capacity": int,
            "generated": int,        # PDUs generated in window
            "delivered": int,        # PDUs with confirmed delivery in window
            "dropped": int,          # PDUs with a final loss outcome in window
        },
        "optional": {
            "mean_sinr_db": float,   # SIMULATED channel-model output
            "per": float,            # SIMULATED/approximated PER, 0..1
            "traffic_rate_pps": float,
        },
    },
    MSG_HEARTBEAT: {
        "requires_zone_id": False,
        "required": {"seq": int},
        "optional": {},
    },
    MSG_RIS_COMMAND: {
        # B → simulated panel endpoint; OOB link is software-simulated
        "requires_zone_id": True,
        "required": {
            "cmd_id": str,
            "target_panel_id": str,
            "codebook_index": int,
            "config_version": int,
            "created_ts": float,
            "expires_ts": float,
        },
        "optional": {
            "reason": str,
            "opt_id": str,
        },
    },
    MSG_RIS_ACK: {
        # Simulated panel → B; labelled as simulated ACK
        "requires_zone_id": True,
        "required": {
            "cmd_id": str,          # echoed from the command
            "ack_id": str,
            "panel_id": str,
            "status": str,          # "ACKED" | "REJECTED"
        },
        "optional": {
            "reason": str,
        },
    },
    MSG_TELEMETRY_EVENT: {
        # A or B → C: any event worth recording at the supervisor
        "requires_zone_id": False,
        "required": {
            "event_type": str,      # e.g. "PDU_DELIVERY", "CONGESTION_DETECTED", etc.
            "event_data": dict,
        },
        "optional": {
            "zone_id": str,
            "panel_id": str,
        },
    },
    MSG_CONTROLLER_STATE: {
        # B → C: periodic snapshot of controller state
        "requires_zone_id": False,
        "required": {
            "zones": dict,           # zone_id → zone state dict
            "oob_stats": dict,       # OOB counters
            "panel_count": int,
        },
        "optional": {
            "latest_decisions": dict,
            "latest_opt_results": dict,
        },
    },
}
