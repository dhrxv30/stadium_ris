# Implementation Status

## Implemented and locally verified

- Laptop A's logical endpoint population, traffic generation, bounded queues,
  delivery/channel simulation, feedback aggregation, metrics, and CSV export.
- Laptop B's feedback ingestion, congestion scheduler, finite RIS codebook
  search, simulated OOB retry/ACK lifecycle, and controller CSV reports.
- A↔B TCP feedback and acknowledged control updates for scheduling and grouped
  RIS state; A applies updates to subsequent simulated queue/channel behavior.
- Laptop C's telemetry ingestion, supervisor state, dashboard modes, and CSV
  exports.
- Reproducible local comparison command for baseline, scheduling-only, and
  adaptive RIS scenarios (see README).
- Unit and local TCP integration tests for A/B control updates and optimizer
  decisions.

## Partial or simulation-only

- RIS propagation, steering, SINR/PER, packet delivery, and OOB radio
  acknowledgements are modeled in software. The beam response and the
  codebook-derived A-side SINR adjustment are simplified, not calibrated to
  measured stadium RF data.
- The experiment runner injects a clearly labeled synthetic congested-Z3
  feedback fixture to exercise the control paths; packet metrics in the output
  are produced by Laptop A's simulator.
- C telemetry and CSV have been validated locally. A concurrent three-laptop
  physical-network test and a graphical dashboard acceptance run have not been
  performed in this environment.

## Not claimed / future work

- Physical RIS programming, Sub-GHz radio transport, real RF calibration, and
  field validation.
- A statistically powered study across multiple seeds, parameter sweeps, and
  hardware profiles. The comparison command is a reproducible smoke-level
  experiment, not a publication-grade performance evaluation.
- Large-scale performance certification on the full 50,000-endpoint workload
  across separate physical laptops.

## Metric definitions

PDR is `delivered / (delivered + terminal_dropped)` and loss ratio is
`terminal_dropped / (delivered + terminal_dropped)`. Packets still queued at
the end of a run are reported as pending and excluded from both rates.
Retransmission attempts are not terminal packet outcomes.
