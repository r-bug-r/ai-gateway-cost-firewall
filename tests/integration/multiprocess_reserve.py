"""Helper process for `test_multiprocess_budget.py`.

Not collected by pytest (no `test_` prefix). Each process opens its own
`Ledger` against the shared file, so the SQLite write lock is the only thing
serializing the budget decision — exactly the deployment shape where several
gateway processes share one database.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cost_firewall.ledger import BudgetPolicy, Ledger, ReservationDenied

POLICY = BudgetPolicy(
    project_id="alpha",
    name="Alpha",
    monthly_budget_micros=1_000,
    daily_budget_micros=None,
    per_request_max_micros=100,
)


def main(argv: list[str]) -> int:
    db_path, request_id, reserved = argv[1], argv[2], int(argv[3])
    ledger = Ledger(db_path)
    try:
        _ = ledger.reserve(
            project=POLICY,
            request_id=request_id,
            route_model="model-a",
            reserved_micros=reserved,
            ttl_seconds=60,
            idempotent=True,
        )
    except ReservationDenied as exc:
        print(f"DENY {exc.reason}")
        return 1
    finally:
        ledger.close()
    print("GRANT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
