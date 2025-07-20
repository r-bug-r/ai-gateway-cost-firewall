"""Cross-process budget enforcement.

The unit test in `tests/unit/test_ledger.py` races threads that each hold their
own connection. This one races separate *processes*, which is how a deployment
with more than one gateway worker actually behaves: the only shared state is
the SQLite file, so `BEGIN IMMEDIATE` is the entire correctness argument.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cost_firewall.ledger import BudgetPolicy, Ledger

ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name("multiprocess_reserve.py")
PROJECT = BudgetPolicy(
    project_id="alpha",
    name="Alpha",
    monthly_budget_micros=1_000,
    daily_budget_micros=None,
    per_request_max_micros=100,
)


def test_separate_processes_cannot_overspend_one_budget(tmp_path: Path) -> None:
    db_path = tmp_path / "shared.db"
    Ledger(db_path).sync_projects([PROJECT])

    workers = [
        subprocess.Popen(
            [sys.executable, str(WORKER), str(db_path), f"proc-{index}", "100"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(14)
    ]
    outputs: list[str] = []
    for worker in workers:
        stdout, stderr = worker.communicate(timeout=120)
        assert stderr == "", stderr
        outputs.append(stdout.strip())

    grants = [line for line in outputs if line == "GRANT"]
    denials = [line for line in outputs if line.startswith("DENY")]

    # 1000 micro of budget, 100 micro per reservation: exactly ten may win.
    assert len(grants) == 10, outputs
    assert len(denials) == 4, outputs
    assert set(denials) == {"DENY monthly_budget_exhausted"}

    ledger = Ledger(db_path)
    try:
        snapshot = ledger.snapshot(PROJECT)
        report = ledger.usage_report(project_id="alpha")
    finally:
        ledger.close()

    assert snapshot.month_reserved_micros == 1_000
    assert snapshot.month_committed_micros == 1_000
    assert snapshot.month_committed_micros <= PROJECT.monthly_budget_micros
    assert report["requests"] == 10
    assert report["reconciled"] is True
