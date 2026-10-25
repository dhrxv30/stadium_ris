# Stadium RIS — Laptop B: RIS / OOB Controller

Adaptive Closed-Loop OOB-Controlled RIS for High-Density Stadium Hotspot Offloading.
**This README covers Laptop B only.** Laptop A and Laptop C are built on separate machines.

---

## Implemented Stages

| Stage | Description | Status |
|-------|-------------|--------|
| B1 | TCP listener, message framing, zone/panel registry, mock feedback, config/validation | ✅ Complete |
| B2 | Zone manager: congestion state machine, hysteresis, history, cumulative counters | ✅ Complete |
| B3 | Baseline and congestion-aware schedulers, cooldown hysteresis, decision history | ✅ Complete |
| B4 | RIS channel model (FSPL, NLoS, shadowing, RIS array gain, PER sigmoid), panel geometry | ✅ Complete |
| B5 | RIS optimizer: codebook search, objective function, pre/post SINR comparison | ✅ Complete |
| B6 | OOB command/ACK protocol: configurable delay/loss/retry, duplicate protection | ✅ Complete |
| B7 | Closed-loop engine: feedback→schedule→optimize→OOB→evaluate cycle | ✅ Complete |
| B8 | CSV reports (5 files), terminal summaries | ✅ Complete |

---

## Important Disclaimer

> **All RIS/channel values are SIMULATED in software.**
> No physical RIS hardware, no real Sub-GHz radio link, and no real RF measurements are used.
> The OOB control link (conceptually Sub-GHz 868/915 MHz) is entirely a software simulation.
> The same laptop Wi-Fi LAN carries the actual application messages.

---

## Requirements

- Python 3.10+
- Dependencies: `PyYAML>=6.0`, `numpy>=1.24`, `pytest>=7.0`, `pytest-asyncio>=0.23`

## Setup

```bash
# Create virtual environment (from repo root)
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

## Configuration

Edit `config/network.yaml` to set the LAN IP addresses:
```yaml
laptop_a: {host: "192.168.1.10", port: 9001}
laptop_b: {host: "0.0.0.0",      port: 9002}   # B listens on all interfaces
laptop_c: {host: "192.168.1.30", port: 9003}
```

Edit `config/ris.yaml` for OOB and channel simulation parameters.

### Windows Firewall

Allow inbound TCP on port 9002 (Laptop B's listen port) in Windows Defender Firewall
for the trusted local network only. Do not expose to the public internet.

```powershell
New-NetFirewallRule -DisplayName "Stadium RIS - Laptop B" `
  -Direction Inbound -Protocol TCP -LocalPort 9002 `
  -Action Allow -Profile Private
```

## Running Laptop B

### Standalone mock mode (no other laptops required)

```bash
python laptop_b/main.py --mode mock --mock-reports 5 --log-level INFO
```

### Listen mode (waiting for real Laptop A)

```bash
python laptop_b/main.py --mode listen --duration 60
```

### Baseline scheduler (no adaptive RIS; Scenario 1/2)

```bash
python laptop_b/main.py --mode listen --duration 60
# Then in config/ris.yaml set: scheduler: baseline
```

### Options

```
--mode {listen,mock,baseline}   Operation mode (default: listen)
--duration N                     Run duration in seconds (0 = until Ctrl+C)
--mock-reports N                 Feedback reports per zone in mock mode (default: 3)
--log-level {DEBUG,INFO,...}
--no-csv                         Skip CSV export at shutdown
--run-id RUN_ID                  Override auto-generated run ID
```

## Running Tests

```bash
# All tests (84 tests as of B8)
python -m pytest tests/ -v

# Stage-specific tests
python -m pytest tests/test_b1_acceptance.py -v
python -m pytest tests/test_b2_zone_manager.py -v
python -m pytest tests/test_b3_scheduler.py -v
python -m pytest tests/test_b4_ris_channel.py -v
python -m pytest tests/test_b6_oob_controller.py -v
python -m pytest tests/test_b7_closed_loop.py -v
```

## CSV Output Files

Written to `results/<run-id>/` at shutdown:

| File | Contents |
|------|----------|
| `oob_events.csv` | All OOB commands: status, retry count, ACK timing |
| `ris_panel_states.csv` | Snapshot of all 32 panel states at shutdown |
| `controller_decisions.csv` | Per-zone scheduler decisions with input measurements |
| `zone_state_history.csv` | Per-window zone metrics (queue, SINR, PER, PDR, congestion state) |
| `run_summary.csv` | Single-row experiment summary with all key counters |

All SINR, PER, and channel values in CSV files are labelled as **SIMULATED**.

## Terminal Output

Periodic status (configurable via `status_interval_s`):

```
[LAPTOP B] RUN=... ELAPSED=...s
Laptop A link=CONNECTED | Laptop C link=N/A (telemetry: C-stage)
Feedback received=... accepted=... | Stale/rejected=... | Malformed=...
ZONES:
  Z1: rpt=... q=.../1000 sinr=...dB pdr=...% state=CONGESTED
  ...
RIS panels: total=32 | active configs=32 | pending commands=...
OOB TX_attempts=... ACK=... retries=... timeouts=...
CLOSED-LOOP (simulated):
  Z3: opt=APPLY cb=3 SINR 4.2dB→7.8dB (improved) [abc12345]
```

## Module Structure

```
laptop_b/
  main.py             Entry point (stages B1–B8)
  controller.py       Main ControllerB class wiring all modules
  zone_manager.py     Five-zone state machine + congestion classification (B2)
  scheduler.py        Baseline + congestion-aware scheduler (B3)
  ris_channel.py      Channel model + PanelRegistry with geometry (B4)
  ris_optimizer.py    Codebook search + objective function (B5)
  oob_controller.py   OOB command/ACK/retry protocol (B6)
  closed_loop.py      Feedback → schedule → OOB → evaluate engine (B7)
  report_writer.py    CSV report writer (B8)
  mock_feedback.py    Mock Laptop A for standalone testing
```

## Known Limitations

1. **Channel model**: Simplified; no multi-path fading or real antenna patterns.
2. **OOB link**: Software simulation only; no physical Sub-GHz radio.
3. **Laptop C telemetry**: Not yet connected (requires C1 implementation).
4. **Optimizer**: Finite codebook search; no global optimality guarantee.
5. **RIS panels**: All panels in a zone share one OOB command per iteration (scalable to all panels in future stages).
6. **Scheduler**: Token weights are computed but not yet connected to actual traffic shaping (requires Laptop A integration).

## Next Stage

**I1 — Protocol agreement and integration with Laptop A and C.**
Start Laptop B in `--mode listen`, then start Laptop A on its machine.
Verify that feedback reports arrive and the closed-loop runs.
