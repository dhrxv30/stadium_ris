# Stadium RIS — Laptop C (Supervisor, Monitoring & Stadium Visualization)

**Adaptive Closed-Loop Out-of-Band (OOB) Controlled Reconfigurable Intelligent Surface (RIS) for High-Density Stadium Hotspot Offloading**

Laptop C serves as the central supervisor and telemetry collector across the 3-laptop network.

---

## 1. Quickstart & Installation

Python 3.10+ (tested on Python 3.12 64-bit on Windows). Dependencies: `PyYAML>=6.0.1`, `matplotlib>=3.9.0` (for optional native 3D visualization).

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

---

## 2. Running Laptop C

Laptop C must be started **first**, prior to starting Laptop A and Laptop B.

### Live Network Mode (Standard Deployment)
Listens on all interfaces for Laptop A (port 9101) and Laptop B (port 9102):
```powershell
python -m laptop_c.main --config config/network.yaml
```

### Standalone / Mock Simulation Mode
Runs with synthetic, self-contained Laptop A and Laptop B senders (for local testing without physical laptops):
```powershell
# 6-second test run with full terminal dashboard and CSV exports
python -m laptop_c.main --mock --duration 6

# With automatic local 3D stadium visualization snapshot on exit:
python -m laptop_c.main --mock --duration 6 --visualize

# With event stream filtering (e.g. inspecting Zone 3 only):
python -m laptop_c.main --mock --duration 6 --filter-zone Z3
```

---

## 3. Running Unit and Integration Tests

Run the full automated test suite (40 unit and integration tests covering framing, message validation, TCP sockets, supervisor state, terminal view, and CSV exporter):
```powershell
python -m unittest discover -s tests -t . -v
```

---

## 4. Network Configuration & Ports

*   **Telemetry Port A (TCP)**: `9101` (Receives Laptop A PDU generation, queue load, and receiver telemetry)
*   **Telemetry Port B (TCP)**: `9102` (Receives Laptop B controller decisions, 32 RIS panel states, and OOB link telemetry)
*   **Framing**: 4-byte big-endian unsigned length prefix + UTF-8 encoded JSON payload.

### Windows Firewall Configuration (Administrator PowerShell on Private/Trusted LAN)
```powershell
New-NetFirewallRule -DisplayName "RIS-C telemetry" -Direction Inbound -Protocol TCP -LocalPort 9101,9102 -Profile Private -Action Allow
```

Find your Laptop C LAN IP address using `ipconfig` (Windows) and configure it in `config/network.yaml` so Laptop A and B know where to connect.

---

## 5. Completed Stages for Laptop C

| Stage | Status | Description |
| :--- | :--- | :--- |
| **Stage C1** | **COMPLETED** | Dual framed TCP listeners on ports 9101 & 9102, schema validation, clock skew protection, run-ID tracking, sequence gap / duplicate rejection, per-peer health monitoring (`NEVER_CONNECTED`, `CONNECTED`, `STALE`, `DISCONNECTED`), clean shutdown. |
| **Stage C2** | **COMPLETED** | Unified Supervisor State Model (`laptop_c/supervisor.py`). Tracks 50,000 devices (25k senders, 25k receivers across 5 zones), directional PDU accounting (UL/DL generated, delivered, dropped, pending), 5 stadium zones (queue occupancy, capacity, SINR, PER, congestion), 32 virtual passive RIS panels catalog, OOB link reliability (unique cmds, ACKs, timeouts, ACK ratio), safe `"N/A"` handling. |
| **Stage C3** | **COMPLETED** | Multi-pane continuously refreshed terminal supervisor dashboard (`laptop_c/terminal_view.py`). Zone status table, 32-panel catwalk grid, and filterable live event stream (`--filter-zone`, `--filter-panel`, `--filter-cmd`, `--filter-pdu`, `--filter-type`). |
| **Stage C4** | **COMPLETED** | Automated CSV performance recording (`laptop_c/csv_exporter.py`). Exports 6 structured CSV reports per run under `results/<run_id>/`: `system_metrics.csv`, `zone_metrics.csv`, `pdu_events.csv`, `oob_events.csv`, `ris_panel_states.csv`, and `run_summary.csv`. |
| **Stage C5** | **COMPLETED** | Native local 3D stadium visualization (`laptop_c/visualization/stadium_3d.py`). Plots stadium geometry, 5 seating zones, 25m catwalk elevation ring, 32 virtual passive RIS panels, active beam coverage footprints, and zone congestion heatmaps. Can be saved as high-res PNG or run interactively. |

---

## 6. Output Artifacts and Results

Each experiment produces an isolated folder under `results/<run_id>/`:
1.  `system_metrics.csv`: Time-series sampled system-wide PDU, latency, throughput, and OOB performance.
2.  `zone_metrics.csv`: Time-series sampled metrics for each zone Z1 through Z5.
3.  `pdu_events.csv`: Event trace of discrete PDU deliveries, drops, and timeouts.
4.  `oob_events.csv`: Event trace of discrete Sub-GHz OOB commands, acknowledgements, and retries.
5.  `ris_panel_states.csv`: State change log and snapshots of all 32 virtual passive RIS panels.
6.  `run_summary.csv`: Final reconciliation report summarizing device population, total PDR/PLR, throughput, and OOB success rates.
7.  `stadium_3d.png` (optional with `--visualize`): 3D topological view of the stadium and beam coverage footprints.


---

## Stage D — Native Tkinter Dashboard

### Starting the dashboard

```bash
# Without dashboard (default — terminal mode only, exactly as before)
python -m laptop_c.main --mock --duration 30

# With dashboard (requires a local display)
python -m laptop_c.main --mock --duration 30 --dashboard
```

The dashboard is **off by default** (hard rule 1).  
If Tkinter is unavailable or the window crashes, the supervisor prints  
`dashboard unavailable` and continues in terminal mode automatically.

---

### What each panel shows

#### Tab 1 — Overview

| Section | Metric | Notes |
|---------|--------|-------|
| **Header** | Run ID, Seed, Elapsed | Peer badges A / B / C colour-coded: **green** = CONNECTED, **amber** = STALE, **grey** = DISCONNECTED/NEVER |
| | MOCK banner | Shown in orange when any ingested payload has `mock=true` |
| **Traffic** | UL & DL counters (GEN / DEL / DROP / PEND / TMO) | Row per direction |
| | PDR %, PLR % | Colour-coded green ≥95 / amber ≥85 / red <85 |
| | ACK / NACK / Retry counts | |
| | Latency mean & P95, Throughput Mbps | |
| **OOB** | Commands, ACK'd, Retried, Timed out | Greyed + "⚠ STALE" when peer B is not CONNECTED |
| | ACK ratio | Shown with denominator e.g. `95.0% (19/20)` |
| | Last 10 commands | Newest first |
| **Zone cards** (Z1–Z5) | Congestion badge | NORMAL / ELEVATED / CONGESTED, colour-coded |
| | GEN / DEL / DROP, PDR % | |
| | Queue occupancy bar | Fills red when >80 %, amber >50 % |
| | SINR, PER | Labelled **¹ simulated model output** |
| | STALE label | Shown when peer A is not CONNECTED |

#### Tab 2 — RIS Panels

32 tiles in an 8 × 4 grid.  
Each tile shows: panel ID, zone (colour-coded), config ID, version, status dot (● APPLIED / PENDING / FAILED / IDLE), health (OK / DEGRADED / OFFLINE).  
Tiles dim when peer B is not CONNECTED.

#### Tab 3 — Trends

Three Canvas sparklines fed from the D1 history buffer (~5 min at 1 Hz):

| Chart | Y-axis | Label |
|-------|--------|-------|
| Global PDR % | 0 – 100 | Fixed range |
| Per-zone Queue occupancy % | 0 – 100 | One coloured line per zone Z1–Z5 |
| Per-zone SINR dB | Auto | Labelled **simulated model output** |

None values create visible gaps in the line (shown as breaks, not fabricated).

#### Tab 4 — Events

- Live scrolling event log (newest at bottom, max 200 rows).  
- **Filters** (type to narrow): Zone · Panel · Cmd ID · Event type.  
- **⏸ Pause / ▶ Resume** button suspends log updates without stopping the supervisor.  
- Colour-coded by event class: red = ERROR/FAIL/DROP, amber = CONGESTION, green = DELIVERY/ACK.

---

### Design constraints & limitations

| Rule | Implementation |
|------|---------------|
| Off by default | `--dashboard` flag required |
| Terminal only | Tkinter (stdlib) — no web server, no browser tab |
| Read-only | Dashboard thread reads deep-copied snapshots; never writes supervisor state |
| Graceful fallback | Any Tk failure → `dashboard unavailable` → terminal mode continues |
| No blocking | 2 Hz refresh on the Tk thread; asyncio loop is never touched |
| No fabrication | `None` → `"N/A"` via `_na()`; STALE data greyed, not hidden |
| Simulated labels | SINR and PER rows carry "¹ simulated model output" |

**Refresh rate:** 2 Hz (500 ms `root.after` loop).  
**History depth:** 300 samples at 1 Hz ≈ 5 minutes of trend data.  
**Thread model:** One non-daemon `DashboardThread`; asyncio passes data via `queue.Queue`.  

