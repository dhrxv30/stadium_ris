# Stadium RIS — Laptop A, B and C

Software simulation of stadium traffic, RIS/OOB control, and a supervisor
dashboard. Channel, RIS, and OOB radio behavior are simulated; laptop messages
travel over TCP on a trusted LAN.

## Setup (each laptop)

From the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Set the LAN addresses in `config\network.yaml` on each laptop. Laptop C must
allow inbound TCP on ports 9101 and 9102. Laptop B must allow inbound TCP on
port 9002. Laptop A's TCP 9003 listener is for optional supervisory commands.

## Run all three laptops

Start C first, then B, then A, each in a separate PowerShell window at the
repository root. Laptop C adopts Laptop A's run ID; Laptop B waits for A's
first feedback report before sending telemetry so both streams use that run ID.
If you set `--run-id` on Laptop C, pass the same value to A and B.

**Laptop C** (configured with its local `bind_host`; add `--dashboard` to open
the optional graphical window, otherwise the live dashboard is shown in the
terminal):

```powershell
python -m laptop_c.main --config config\network.yaml --dashboard
```

**Laptop B**:

```powershell
python laptop_b\main.py --mode listen --duration 0
```

**Laptop A**:

```powershell
python laptop_a\main.py --mode networked --lap-b-ip 192.168.1.39 --duration 0
```

Replace the sample IP with your Laptop B LAN address.
Press **Ctrl+C** in each window to stop. Laptop C also supports a local mock
mode for testing without Laptop A or B:

```powershell
python -m laptop_c.main --mock --duration 6
```

Laptop C's optional Tkinter dashboard and 3D visualization are enabled with
`--dashboard` and `--visualize`.

## Laptop A standalone

```powershell
python laptop_a\main.py --mode standalone --duration 10
```

Use `--help` on each entry point for additional options. Laptop A exports
run-specific metrics under `results`; Laptop C writes its CSV reports there too.

## Important disclaimer

No physical RIS hardware, Sub-GHz radio link, or RF measurement is used. The
OOB control link is entirely simulated in software.
