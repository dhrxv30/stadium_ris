"""32 virtual PASSIVE RIS panels. State only (B1); channel effects arrive in Stage B4.

A panel is a controller-managed passive surface, not a transmitter and not a router.
"""
from dataclasses import dataclass
from typing import Optional

PENDING_STATES = {"PENDING", "SENT", "RETRYING"}


@dataclass
class PanelState:
    panel_id: str
    zone_id: str
    codebook_index: Optional[int]
    config_version: int = 0
    last_update_ts: Optional[float] = None
    command_status: str = "NONE"            # NONE | PENDING | SENT | RETRYING | ACKED | FAILED
    last_command_id: Optional[str] = None
    health: str = "OK"                      # simulated health state


class PanelRegistry:
    def __init__(self, panels):
        self.panels = {p.panel_id: p for p in panels}

    @classmethod
    def build(cls, zone_ids, zone_panel_counts, initial_codebook_index):
        panels, n = [], 0
        for zone_id, count in zip(zone_ids, zone_panel_counts):
            for _ in range(count):
                n += 1
                panels.append(PanelState(f"P{n:02d}", zone_id, initial_codebook_index))
        return cls(panels)

    def by_zone(self, zone_id):
        return [p for p in self.panels.values() if p.zone_id == zone_id]

    @property
    def total(self):
        return len(self.panels)

    @property
    def active_configs(self):
        return sum(1 for p in self.panels.values() if p.codebook_index is not None)

    @property
    def pending_commands(self):
        return sum(1 for p in self.panels.values() if p.command_status in PENDING_STATES)
