"""Local 3D Stadium Visualization for Laptop C (Stage C5).

Uses matplotlib.mplot3d for maintainable, native, local (non-browser) rendering:
- 3D stadium bowl geometry and 5 labelled seating zones (Z1 - Z5)
- Catwalk roof perimeter ring at 25m elevation
- 32 virtual passive RIS panels (P1 - P32) positioned along catwalk
- Color-coded panel configuration states (baseline CB_0 vs. adaptive codebooks)
- Zone congestion heatmap (NORMAL = green, ELEVATED = yellow, CONGESTED = red)
- Simulated directional beam rays and target footprints
- Clear visual disclaimer: SIMULATED PROPAGATION (Software Prototype, Non-Physical RF)
"""
import math
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    import matplotlib
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d import Axes3D
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

from common.constants import NUM_PANELS, ZONE_IDS
from laptop_c.supervisor import Supervisor


ZONE_CENTERS = {
    "Z1": (0.0, 50.0, 5.0),    # North Stand
    "Z2": (65.0, 0.0, 5.0),    # East Stand
    "Z3": (0.0, -50.0, 5.0),   # South Stand
    "Z4": (-65.0, 0.0, 5.0),   # West Stand
    "Z5": (0.0, 0.0, 0.0),     # Pitch / Field Area
}

CONGESTION_COLORS = {
    "NORMAL": "#2ecc71",     # green
    "ELEVATED": "#f39c12",   # amber/orange
    "CONGESTED": "#e74c3c",  # red
    "UNKNOWN": "#95a5a6",    # gray
}


def calculate_panel_3d_coordinates(num_panels: int = 32, rx: float = 75.0, ry: float = 55.0, z: float = 25.0) -> Dict[str, tuple]:
    """Calculate 3D catwalk catwalk coordinates for the 32 virtual passive RIS panels."""
    coords = {}
    for i in range(1, num_panels + 1):
        angle = 2.0 * math.pi * (i - 1) / num_panels
        x = rx * math.cos(angle)
        y = ry * math.sin(angle)
        coords[f"P{i}"] = (x, y, z)
    return coords


class Stadium3DVisualizer:
    """Renders a local 3D stadium visualization using Matplotlib."""

    def __init__(self, supervisor: Supervisor):
        self.sup = supervisor
        self.panel_coords = calculate_panel_3d_coordinates(NUM_PANELS)

    def is_available(self) -> bool:
        return HAS_MATPLOTLIB

    def render_figure(self) -> Optional[Any]:
        """Generate and render the 3D stadium plot figure."""
        if not HAS_MATPLOTLIB:
            return None

        fig = plt.figure(figsize=(12, 9), facecolor="#1e1e24")
        ax = fig.add_subplot(111, projection="3d", facecolor="#1e1e24")

        # Set title and simulation notice
        fig.suptitle(
            "STADIUM RIS SUPERVISOR — 3D TOPOLOGY & BEAM COVERAGE",
            fontsize=14, fontweight="bold", color="#ecf0f1", y=0.96
        )
        ax.set_title(
            "[SIMULATED PROPAGATION MODEL — Virtual Passive RIS, Software Prototype]",
            fontsize=10, color="#95a5a6", pad=12
        )

        # 1. Pitch / Center Field (Zone Z5)
        px = [-45, 45, 45, -45]
        py = [-30, -30, 30, 30]
        pz = [0, 0, 0, 0]
        z5_cong = self.sup.zones["Z5"].congestion_state
        z5_col = CONGESTION_COLORS.get(z5_cong, "#2ecc71")
        pitch_poly = Poly3DCollection([list(zip(px, py, pz))], alpha=0.35, facecolor=z5_col, edgecolor="#27ae60")
        ax.add_collection3d(pitch_poly)
        ax.text(0, 0, 1, f"Z5 (Pitch)\n{z5_cong}", color="#ecf0f1", fontsize=9, ha="center", weight="bold")

        # 2. Seating Stands (Zones Z1 - Z4)
        stand_polys = {
            "Z1": ([(-55, 32, 2), (55, 32, 2), (65, 60, 10), (-65, 60, 10)], "Z1 (North)"),
            "Z2": ([(47, -35, 2), (47, 35, 2), (78, 45, 10), (78, -45, 10)], "Z2 (East)"),
            "Z3": ([(-55, -32, 2), (55, -32, 2), (65, -60, 10), (-65, -60, 10)], "Z3 (South)"),
            "Z4": ([(-47, -35, 2), (-47, 35, 2), (-78, 45, 10), (-78, -45, 10)], "Z4 (West)"),
        }
        for zid, (vertices, label) in stand_polys.items():
            z_cong = self.sup.zones[zid].congestion_state
            z_col = CONGESTION_COLORS.get(z_cong, "#2ecc71")
            poly = Poly3DCollection([vertices], alpha=0.4, facecolor=z_col, edgecolor="#7f8c8d")
            ax.add_collection3d(poly)
            cx, cy, cz = ZONE_CENTERS[zid]
            ax.text(cx, cy, cz + 2, f"{label}\n[{z_cong}]", color="#ecf0f1", fontsize=9, ha="center", weight="bold")

        # 3. Catwalk Ring (Roof Level at Z = 25m)
        theta = [2.0 * math.pi * j / 100 for j in range(101)]
        cx = [75.0 * math.cos(t) for t in theta]
        cy = [55.0 * math.sin(t) for t in theta]
        cz = [25.0] * len(theta)
        ax.plot(cx, cy, cz, color="#7f8c8d", linestyle="--", linewidth=1.2, label="Catwalk Ring (25m)")

        # 4. 32 Virtual Passive RIS Panels
        xs_base, ys_base, zs_base = [], [], []
        xs_act, ys_act, zs_act = [], [], []
        xs_pend, ys_pend, zs_pend = [], [], []

        for pid, (x, y, z) in self.panel_coords.items():
            panel = self.sup.panels[pid]
            if panel.command_status == "PENDING":
                xs_pend.append(x); ys_pend.append(y); zs_pend.append(z)
            elif panel.current_config != "CB_0":
                xs_act.append(x); ys_act.append(y); zs_act.append(z)
                # Draw simulated beam ray to target zone
                target_zone = panel.zone_id
                if target_zone in ZONE_CENTERS:
                    tx, ty, tz = ZONE_CENTERS[target_zone]
                    ax.plot([x, tx], [y, ty], [z, tz], color="#3498db", alpha=0.6, linewidth=1.5, linestyle=":")
            else:
                xs_base.append(x); ys_base.append(y); zs_base.append(z)

            # Label subset of panels for readability
            p_num = int(pid[1:])
            if p_num % 4 == 1 or panel.current_config != "CB_0":
                ax.text(x * 1.05, y * 1.05, z + 1, pid, color="#bdc3c7", fontsize=7, ha="center")

        if xs_base:
            ax.scatter(xs_base, ys_base, zs_base, c="#34495e", s=45, marker="s", edgecolors="#95a5a6", label="Baseline (CB_0)")
        if xs_act:
            ax.scatter(xs_act, ys_act, zs_act, c="#2ecc71", s=70, marker="^", edgecolors="#ffffff", label="Reconfigured (CB_Active)")
        if xs_pend:
            ax.scatter(xs_pend, ys_pend, zs_pend, c="#f1c40f", s=60, marker="o", edgecolors="#e67e22", label="OOB Command Pending")

        # Set axes labels and styling
        ax.set_xlabel("X (meters, East-West)", color="#bdc3c7", labelpad=8)
        ax.set_ylabel("Y (meters, North-South)", color="#bdc3c7", labelpad=8)
        ax.set_zlabel("Z (meters, Elevation)", color="#bdc3c7", labelpad=8)
        ax.set_xlim(-90, 90)
        ax.set_ylim(-70, 70)
        ax.set_zlim(0, 32)
        ax.tick_params(colors="#7f8c8d", labelsize=8)
        ax.grid(color="#34495e", linestyle=":", alpha=0.5)

        # Pane backgrounds
        ax.xaxis.set_pane_color((0.12, 0.12, 0.15, 1.0))
        ax.yaxis.set_pane_color((0.12, 0.12, 0.15, 1.0))
        ax.zaxis.set_pane_color((0.10, 0.10, 0.13, 1.0))

        ax.legend(loc="upper right", facecolor="#2c3e50", edgecolor="#7f8c8d", labelcolor="#ecf0f1", fontsize=8)
        ax.view_init(elev=32, azim=-55)

        return fig

    def save_snapshot(self, output_path: str) -> bool:
        """Render and save 3D visualization snapshot to a PNG file."""
        if not HAS_MATPLOTLIB:
            return False
        fig = self.render_figure()
        if not fig:
            return False
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        fig.savefig(output_path, dpi=150, facecolor=fig.get_facecolor(), bbox_inches="tight")
        plt.close(fig)
        return True


def main():
    """Standalone CLI launcher for 3D visualization testing."""
    if not HAS_MATPLOTLIB:
        print("[VISUALIZATION] matplotlib is not available; install matplotlib to enable 3D rendering.")
        sys.exit(1)

    sup = Supervisor(run_id="vis-test-001")
    # Simulate a reconfigured panel and congested zone for demonstration
    sup.zones["Z3"].congestion_state = "CONGESTED"
    sup.zones["Z1"].congestion_state = "ELEVATED"
    sup.panels["P16"].current_config = "CB_3"
    sup.panels["P16"].command_status = "APPLIED"
    sup.panels["P4"].current_config = "CB_2"
    sup.panels["P4"].command_status = "APPLIED"

    vis = Stadium3DVisualizer(sup)
    snapshot_path = "results/vis_demo/stadium_3d.png"
    ok = vis.save_snapshot(snapshot_path)
    if ok:
        print(f"[VISUALIZATION] Snapshot successfully rendered and saved to: {snapshot_path}")
    else:
        print("[VISUALIZATION] Failed to render figure.")


if __name__ == "__main__":
    main()
