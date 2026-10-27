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

Set the LAN addresses in `config\network.yaml` on each laptop. The current
shared telemetry design uses Laptop C's unified listener on port 9003 for both
A and B telemetry streams; if you override the ports, keep the same semantics.
Laptop B must allow inbound TCP on port 9002 for its feedback/control listener.
Laptop A's TCP 9003 listener is for optional supervisory commands.

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

Replace the sample IP with your Laptop B LAN address. In networked mode, A
sends per-zone feedback to B over the control connection. B returns scheduling
and, when its optimizer selects a change, RIS updates; A validates, applies,
and acknowledges them. Severe zones receive a separate 2× weighted dequeue
service allowance from A (up to twice the normal batch budget if every zone is
severe), in addition to their scheduling weight. This raises processing work
and CPU usage; it does not improve channel/PER failures by itself. The zone
cards show A's currently applied service multiplier. Scheduling weights affect
A's queue service allocation, and the B model's predicted RIS SINR adjustment
is applied to A's simulated channel for subsequent packets. The OOB delay/loss
model and the A-B command transport are software simulation/application traffic
over TCP; no physical radio or RIS hardware is involved. B's status reports
count applied/rejected control updates, and A's dashboard reports received
scheduling/RIS updates. Laptop C's graphical dashboard shows both run-wide
packet loss and a 10-second rolling loss rate derived from new
delivered/dropped counter outcomes. The rolling rate can move up or down as
recent traffic changes; it is N/A until there are outcomes in the window. The
cumulative dropped-packet count never decreases.

Press **Ctrl+C** in each window to stop. Laptop C also supports a local mock
mode for testing without Laptop A or B:

```powershell
python -m laptop_c.main --mock --duration 6
```

Laptop C's optional Tkinter dashboard and 3D visualization are enabled with
`--dashboard` and `--visualize`.

## Reproducible simulator comparison

Run the same seeded Laptop A workload as a static baseline, scheduling-only
control, and adaptive RIS control:

```powershell
python scripts\run_experiments.py --seed 42 --duration 3 --packet-budget 300 --rate-multiplier 4 --output results\experiments\comparison.csv
```

The runner uses Laptop A's simulated packet/channel results. It injects a
deterministic synthetic congested-Z3 feedback fixture to exercise the B
scheduler and RIS optimizer; this is control-path validation, not observed
network congestion. The CSV records generated, delivered, terminal drops,
pending packets, final-outcome PDR/loss, latency, throughput, and applied
control-update counts, including per-zone results. Defaults intentionally
overload A's service budget to make queue loss and scheduling trade-offs
visible. Pending packets are excluded from PDR/loss denominators. Increase
`--duration` for a longer run; comparisons are still software simulation and
are not hardware or RF measurements.
The experiment's `goodput_bps_simulated` is delivered payload bytes divided by
simulated duration; Laptop A's live dashboard `throughput_bps` remains a
wall-clock rolling measurement.

The optimizer evaluates repeatable receiver samples over the same synthetic
zone geometry used by Laptop A. Zone updates are applied to every panel in the
zone only after all simulated panel acknowledgements and A's application ACK.

For the control-cycle diagram and an honest implemented/partial/unimplemented
feature list, see [docs/architecture.md](docs/architecture.md) and
[docs/implementation-status.md](docs/implementation-status.md).

## Validation

```powershell
python -m pytest -q
```

## Laptop A standalone

```powershell
python laptop_a\main.py --mode standalone --duration 10
```

Laptop A's default packet budget is 3,000 packets per tick in each direction
(about 30,000 packets/second at the default 10 Hz tick rate), enough for the
`normal` preset's 25,000 packets/second aggregate input. Increase or lower it
with `--packet-budget`; higher settings require more CPU.

The default simulated channel includes loaded-stadium interference so
retransmissions and occasional terminal packet drops are visible. Laptop B
classifies PDR below 99.5% as moderate and below 98% as congested; after two
consecutive reports it boosts scheduling for moderate zones and runs RIS
optimization for congested zones. These are tunable simulation defaults, not
measured RF performance. Short runs can still show 100% PDR before a terminal
drop occurs.

Use `--help` on each entry point for additional options. Laptop A exports
run-specific metrics under `results`; Laptop C writes its CSV reports there too.

## Important disclaimer

No physical RIS hardware, Sub-GHz radio link, or RF measurement is used. The
OOB control link is entirely simulated in software.
