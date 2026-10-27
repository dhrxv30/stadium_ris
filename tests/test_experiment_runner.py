import csv

import pytest

from scripts.run_experiments import main


def test_experiment_runner_writes_reproducible_scenario_summaries(tmp_path):
    output = tmp_path / "comparison.csv"

    assert main([
        "--seed", "42",
        "--duration", "3",
        "--packet-budget", "300",
        "--rate-multiplier", "4",
        "--output", str(output),
    ]) == 0

    with output.open(newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))

    assert [row["scenario"] for row in rows] == [
        "baseline",
        "scheduling_only",
        "adaptive_ris",
    ]
    assert all(row["feedback_stimulus"] == "synthetic_congested_Z3" for row in rows)
    assert all(int(row["final_outcomes"]) > 0 for row in rows)
    assert all(
        float(row["pdr_final_outcomes"])
        == pytest.approx(
            int(row["delivered"]) / int(row["final_outcomes"]),
            abs=0.00005,
        )
        for row in rows
    )
    assert int(rows[0]["control_updates_applied"]) == 0
    assert int(rows[1]["scheduling_updates_applied"]) > 0
    assert int(rows[1]["ris_updates_applied"]) == 0
    assert int(rows[2]["scheduling_updates_applied"]) > 0
    assert int(rows[2]["ris_updates_applied"]) > 0
    assert "z3_pdr" in rows[2]
    assert int(rows[1]["z3_delivered"]) > int(rows[0]["z3_delivered"])
    assert float(rows[1]["z3_pdr"]) > float(rows[0]["z3_pdr"])
