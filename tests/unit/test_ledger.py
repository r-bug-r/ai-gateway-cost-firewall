"""Ledger tests: atomic reservation, settlement, release, retention, reconciliation."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cost_firewall.ledger import (
    BudgetPolicy,
    BudgetSnapshot,
    BudgetUnavailable,
    Ledger,
    ReservationDenied,
    utc_day_start_ms,
    utc_month_start_ms,
)

PROJECT = BudgetPolicy(
    project_id="alpha",
    name="Alpha",
    monthly_budget_micros=1_000,
    daily_budget_micros=None,
    per_request_max_micros=400,
)


def make_ledger(tmp_path: Path) -> Ledger:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.sync_projects([PROJECT])
    return ledger


def reserve(
    ledger: Ledger, request_id: str, micros: int, *, ttl: int = 60, now: int | None = None
) -> BudgetSnapshot:
    return ledger.reserve(
        project=PROJECT,
        request_id=request_id,
        route_model="model-a",
        reserved_micros=micros,
        ttl_seconds=ttl,
        idempotent=True,
        now=now,
    )


def test_reserve_then_settle_reduces_remaining_budget(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    reserve(ledger, "r1", 300)
    snapshot = ledger.snapshot(PROJECT)
    assert snapshot.month_reserved_micros == 300
    assert snapshot.month_settled_micros == 0
    assert snapshot.month_remaining_micros == 700

    ledger.settle(request_id="r1", cost_micros=250)
    snapshot = ledger.snapshot(PROJECT)
    assert snapshot.month_reserved_micros == 0
    assert snapshot.month_settled_micros == 250
    assert snapshot.month_remaining_micros == 750


def test_reservation_denied_when_monthly_budget_would_be_exceeded(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    reserve(ledger, "r1", 700)
    with pytest.raises(ReservationDenied) as excinfo:
        reserve(ledger, "r2", 400)
    assert excinfo.value.reason == "monthly_budget_exhausted"
    assert ledger.request_row("r2") is None


def test_reservation_exactly_at_budget_is_allowed(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    reserve(ledger, "r1", 1000)
    assert ledger.snapshot(PROJECT).month_remaining_micros == 0
    with pytest.raises(ReservationDenied):
        reserve(ledger, "r2", 1)


def test_daily_budget_blocks_even_when_monthly_headroom_exists(tmp_path: Path) -> None:
    policy = BudgetPolicy(
        project_id="alpha",
        name="Alpha",
        monthly_budget_micros=10_000,
        daily_budget_micros=500,
        per_request_max_micros=400,
    )
    ledger = Ledger(tmp_path / "daily.db")
    ledger.sync_projects([policy])
    ledger.reserve(
        project=policy,
        request_id="r1",
        route_model="model-a",
        reserved_micros=400,
        ttl_seconds=60,
        idempotent=True,
    )
    with pytest.raises(ReservationDenied) as excinfo:
        ledger.reserve(
            project=policy,
            request_id="r2",
            route_model="model-a",
            reserved_micros=200,
            ttl_seconds=60,
            idempotent=True,
        )
    assert excinfo.value.reason == "daily_budget_exhausted"


def test_expired_reservation_releases_headroom_without_a_background_job(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    now = 1_700_000_000_000
    reserve(ledger, "r1", 900, ttl=10, now=now)
    with pytest.raises(ReservationDenied):
        reserve(ledger, "r2", 500, now=now + 1)
    # After the TTL the abandoned reservation no longer consumes headroom.
    reserve(ledger, "r2", 500, now=now + 11_000)


def test_release_does_not_charge_and_allows_reuse(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    reserve(ledger, "r1", 900)
    ledger.release(request_id="r1", error_code="upstream_timeout")
    row = ledger.request_row("r1")
    assert row is not None
    assert row.status == "released"
    assert ledger.snapshot(PROJECT).month_settled_micros == 0
    reserve(ledger, "r2", 900)


def test_fail_marks_request_without_charge(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    reserve(ledger, "r1", 300)
    ledger.fail(request_id="r1", error_code="upstream_permanent")
    row = ledger.request_row("r1")
    assert row is not None and row.status == "failed"
    assert ledger.snapshot(PROJECT).month_settled_micros == 0


def test_settle_twice_is_rejected(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    reserve(ledger, "r1", 300)
    ledger.settle(request_id="r1", cost_micros=100)
    with pytest.raises(RuntimeError):
        ledger.settle(request_id="r1", cost_micros=100)


def test_concurrent_reservations_never_overspend(tmp_path: Path) -> None:
    """Two processes' worth of ledger handles racing for the same budget."""
    ledger_path = tmp_path / "race.db"
    Ledger(ledger_path).sync_projects([PROJECT])
    grants: list[str] = []
    denials: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(12)

    def worker(index: int) -> None:
        ledger = Ledger(ledger_path)
        barrier.wait()
        try:
            ledger.reserve(
                project=PROJECT,
                request_id=f"r{index}",
                route_model="model-a",
                reserved_micros=100,
                ttl_seconds=60,
                idempotent=True,
            )
            with lock:
                grants.append(f"r{index}")
        except ReservationDenied as exc:
            with lock:
                denials.append(exc.reason)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(worker, range(12)))

    # 1000 micro budget / 100 per request = at most 10 grants out of 12 attempts.
    assert len(grants) == 10
    assert denials == ["monthly_budget_exhausted"] * 2
    ledger = Ledger(ledger_path)
    snapshot = ledger.snapshot(PROJECT)
    assert snapshot.month_reserved_micros == 1000
    assert snapshot.month_committed_micros <= PROJECT.monthly_budget_micros


def test_usage_report_reconciles_settled_request_with_attempt(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    reserve(ledger, "r1", 400)
    ledger.record_attempt(
        request_id="r1",
        attempt_number=1,
        provider="primary",
        provider_model="upstream-a",
        status="success",
        error_class=None,
        http_status=200,
        input_tokens=10,
        output_tokens=20,
        cost_micros=120,
        latency_ms=5,
    )
    ledger.settle(request_id="r1", cost_micros=120)
    report = ledger.usage_report(project_id="alpha")
    assert report["reconciled"] is True
    assert report["settled_micros"] == 120
    assert report["attempts"] == 1
    assert report["input_tokens"] == 10
    assert report["output_tokens"] == 20


def test_usage_report_flags_mismatched_settlement(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    reserve(ledger, "r1", 400)
    ledger.record_attempt(
        request_id="r1",
        attempt_number=1,
        provider="primary",
        provider_model="upstream-a",
        status="success",
        error_class=None,
        http_status=200,
        input_tokens=10,
        output_tokens=20,
        cost_micros=120,
        latency_ms=5,
    )
    ledger.settle(request_id="r1", cost_micros=999)
    report = ledger.usage_report(project_id="alpha")
    assert report["reconciled"] is False
    assert report["mismatched_request_ids"] == ["r1"]


def test_retention_purge_removes_old_rows_and_keeps_recent(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    old = 1_600_000_000_000
    reserve(ledger, "old", 100, now=old)
    ledger.settle(request_id="old", cost_micros=50, now=old)
    reserve(ledger, "new", 100)
    ledger.settle(request_id="new", cost_micros=50)

    cutoff = old + 1000
    deleted = ledger.purge_older_than(cutoff_ms=cutoff)
    assert deleted["requests"] == 1
    assert ledger.request_row("old") is None
    assert ledger.request_row("new") is not None


def test_audit_events_are_persisted(tmp_path: Path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.audit(event="budget_fail_open", detail="forwarded", project_id="alpha")
    with ledger.connection() as conn:
        rows = conn.execute("SELECT event, detail FROM audit_events").fetchall()
    assert [tuple(row) for row in rows] == [("budget_fail_open", "forwarded")]


def test_sqlite_failure_raises_budget_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any storage error must surface as `BudgetUnavailable`, never as headroom."""
    ledger = make_ledger(tmp_path)

    def broken_checkout() -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ledger, "_checkout", broken_checkout)
    with pytest.raises(BudgetUnavailable):
        reserve(ledger, "r1", 100)


def test_month_and_day_boundaries_are_utc() -> None:
    # 2026-10-20T12:00:00Z
    now = 1_792_497_600_000
    assert utc_day_start_ms(now) == 1_792_454_400_000
    assert utc_month_start_ms(now) == 1_790_812_800_000
    # A late-UTC-day timestamp still belongs to the same UTC day, not a local one.
    assert utc_day_start_ms(1_792_454_400_000 + 3_600_000) == 1_792_454_400_000
