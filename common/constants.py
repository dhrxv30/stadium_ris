"""Shared constants. Every laptop must use the same PROTOCOL_VERSION."""

PROTOCOL_VERSION = 1
NODES = ("A", "B", "C")
ZONE_IDS = ["Z1", "Z2", "Z3", "Z4", "Z5"]
PANEL_COUNT = 32
DEFAULT_MAX_MESSAGE_BYTES = 1_048_576

# Message types
MSG_FEEDBACK_REPORT  = "FEEDBACK_REPORT"   # A → B  (aggregated zone report)
MSG_HEARTBEAT        = "HEARTBEAT"          # any → any (liveness)
MSG_RIS_COMMAND      = "RIS_COMMAND"        # B → simulated panel (OOB, B6)
MSG_RIS_ACK          = "RIS_ACK"            # simulated panel → B (OOB, B6)
MSG_TELEMETRY_EVENT  = "TELEMETRY_EVENT"    # A|B → C (supervisor telemetry, C1)
MSG_CONTROLLER_STATE = "CONTROLLER_STATE"   # B → C (zone/panel/OOB snapshot)

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
