"""SQLite usage/cost ledger with atomic budget reservation.

Invariants enforced here (not in the HTTP layer):

* A reservation is granted only if `settled + reserved + requested` stays within
  every configured budget, evaluated inside one `BEGIN IMMEDIATE` transaction so
  two concurrent processes cannot both see the same headroom.
* A settled request row is immutable except for settlement of its own attempt
  and `released_at`, which is write-once.
* Reservations expire after a TTL. Expired reservations stop consuming headroom
  (their effective state is computed from `expires_at`, not from a background job),
  so a crashed process cannot permanently block a budget.
* Costs are integer micro-USD. No float arithmetic touches persisted balances.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    monthly_budget_micros INTEGER NOT NULL CHECK (monthly_budget_micros > 0),
    daily_budget_micros INTEGER CHECK (daily_budget_micros IS NULL OR daily_budget_micros > 0),
    per_request_max_micros INTEGER NOT NULL CHECK (per_request_max_micros > 0),
    created_at TEXT NOT NULL
);

-- One row per client request. `status` transitions:
--   reserved -> settled | released | failed
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    route_model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reserved', 'settled', 'released', 'failed')),
    idempotent INTEGER NOT NULL,
    reserved_micros INTEGER NOT NULL CHECK (reserved_micros >= 0),
    settled_micros INTEGER NOT NULL DEFAULT 0 CHECK (settled_micros >= 0),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER,
    released_at_ms INTEGER,
    error_code TEXT,
    decision TEXT
);

CREATE INDEX IF NOT EXISTS requests_project_status
    ON requests (project_id, status, created_at_ms);

-- One row per provider attempt. Retries and fallbacks create separate rows so a
-- failed attempt is never indistinguishable from a billable success.
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES requests(request_id),
    attempt_number INTEGER NOT NULL,
    provider TEXT NOT NULL,
    provider_model TEXT NOT NULL,
    status TEXT NOT NULL,
    error_class TEXT,
    http_status INTEGER,
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cost_micros INTEGER NOT NULL DEFAULT 0 CHECK (cost_micros >= 0),
    latency_ms INTEGER NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
    provider_request_id TEXT,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (request_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS attempts_request ON attempts (request_id);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ms INTEGER NOT NULL,
    request_id TEXT,
    project_id TEXT,
    event TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS audit_events_at ON audit_events (at_ms);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_day_start_ms(now: int) -> int:
    day = datetime.fromtimestamp(now / 1000, tz=UTC).date()
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return int(start.timestamp() * 1000)


def utc_month_start_ms(now: int) -> int:
    day = datetime.fromtimestamp(now / 1000, tz=UTC).date()
    start = datetime(day.year, day.month, 1, tzinfo=UTC)
    return int(start.timestamp() * 1000)


@dataclass(frozen=True)
class BudgetPolicy:
    """The budget fields the ledger needs. Built from validated config."""

    project_id: str
    name: str
    monthly_budget_micros: int
    daily_budget_micros: int | None
    per_request_max_micros: int


@dataclass(frozen=True)
class BudgetSnapshot:
    project_id: str
    monthly_budget_micros: int
    daily_budget_micros: int | None
    month_settled_micros: int
    month_reserved_micros: int
    day_settled_micros: int
    day_reserved_micros: int

    @property
    def month_committed_micros(self) -> int:
        return self.month_settled_micros + self.month_reserved_micros

    @property
    def day_committed_micros(self) -> int:
        return self.day_settled_micros + self.day_reserved_micros

    @property
    def month_remaining_micros(self) -> int:
        return max(0, self.monthly_budget_micros - self.month_committed_micros)

    @property
    def day_remaining_micros(self) -> int | None:
        if self.daily_budget_micros is None:
            return None
        return max(0, self.daily_budget_micros - self.day_committed_micros)


class BudgetUnavailable(Exception):
    """The ledger could not prove headroom. Callers decide fail-open vs fail-closed."""


class ReservationDenied(Exception):
    def __init__(self, reason: str, snapshot: BudgetSnapshot, requested_micros: int):
        super().__init__(reason)
        self.reason = reason
        self.snapshot = snapshot
        self.requested_micros = requested_micros


@dataclass(frozen=True)
class RequestRow:
    request_id: str
    project_id: str
    route_model: str
    status: str
    reserved_micros: int
    settled_micros: int
    attempts: int
    created_at_ms: int
    expires_at_ms: int
    completed_at_ms: int | None
    error_code: str | None


@dataclass(frozen=True)
class AttemptRow:
    attempt_id: str
    request_id: str
    attempt_number: int
    provider: str
    provider_model: str
    status: str
    error_class: str | None
    http_status: int | None
    input_tokens: int
    output_tokens: int
    cost_micros: int
    latency_ms: int
    created_at_ms: int


class Ledger:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._is_memory = self.path == ":memory:"
        if not self._is_memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # A shared in-memory connection must be serialized: two threads issuing
        # BEGIN on one connection would interleave transactions.
        self._memory_lock = threading.RLock()
        self._memory_conn: sqlite3.Connection | None = self._connect() if self._is_memory else None
        # Connections are expensive to open (each `PRAGMA journal_mode` write takes a
        # file lock), so idle ones are reused. The pool is only a cache: a checkout
        # that finds it empty opens a new connection instead of blocking, which keeps
        # nested/parallel callers from deadlocking on a small pool.
        self._pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue()
        self._pool_limit = 16
        self._closed = False
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # `journal_mode` is persisted in the database file, so it is set once in
        # `_initialize`; repeating it per connection would serialize on a file lock.
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _checkout(self) -> sqlite3.Connection:
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            return self._connect()

    def _checkin(self, conn: sqlite3.Connection) -> None:
        if self._closed or conn.in_transaction or self._pool.qsize() >= self._pool_limit:
            conn.close()
            return
        self._pool.put_nowait(conn)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self._memory_conn is not None:
            with self._memory_lock:
                yield self._memory_conn
            return
        conn = self._checkout()
        try:
            yield conn
        finally:
            self._checkin(conn)

    def close(self) -> None:
        """Close pooled connections. Safe to call twice."""
        self._closed = True
        while True:
            try:
                self._pool.get_nowait().close()
            except queue.Empty:
                break
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        with suppress(Exception):
            self.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            else:
                if conn.in_transaction:
                    conn.execute("COMMIT")

    def _initialize(self) -> None:
        # `executescript` issues an implicit COMMIT, so schema creation runs outside
        # the explicit transaction helper; the DDL itself is idempotent.
        with self.connection() as conn:
            if not self._is_memory:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Ledger schema version {row['value']} is not supported "
                    f"(expected {SCHEMA_VERSION})"
                )

    # ------------------------------------------------------------------ projects

    def sync_projects(self, projects: list[BudgetPolicy]) -> None:
        """Insert/update configured projects. Budgets are authoritative from config."""
        with self._write_transaction() as conn:
            for project in projects:
                conn.execute(
                    """
                    INSERT INTO projects (
                        id, name, monthly_budget_micros, daily_budget_micros,
                        per_request_max_micros, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        monthly_budget_micros = excluded.monthly_budget_micros,
                        daily_budget_micros = excluded.daily_budget_micros,
                        per_request_max_micros = excluded.per_request_max_micros
                    """,
                    (
                        project.project_id,
                        project.name,
                        project.monthly_budget_micros,
                        project.daily_budget_micros,
                        project.per_request_max_micros,
                        datetime.now(tz=UTC).isoformat(timespec="seconds"),
                    ),
                )

    # -------------------------------------------------------------------- budget

    def _budget_totals(
        self, conn: sqlite3.Connection, project_id: str, now: int
    ) -> tuple[int, int, int, int]:
        month_start = utc_month_start_ms(now)
        day_start = utc_day_start_ms(now)

        def totals(since: int) -> tuple[int, int]:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'settled'
                        THEN settled_micros ELSE 0 END), 0) AS settled,
                    COALESCE(SUM(CASE WHEN status = 'reserved' AND expires_at_ms > ?
                        THEN reserved_micros ELSE 0 END), 0) AS reserved
                FROM requests
                WHERE project_id = ? AND created_at_ms >= ?
                """,
                (now, project_id, since),
            ).fetchone()
            return int(row["settled"]), int(row["reserved"])

        month_settled, month_reserved = totals(month_start)
        day_settled, day_reserved = totals(day_start)
        return month_settled, month_reserved, day_settled, day_reserved

    def snapshot(self, project: BudgetPolicy, *, now: int | None = None) -> BudgetSnapshot:
        timestamp = now if now is not None else now_ms()
        with self.connection() as conn:
            month_settled, month_reserved, day_settled, day_reserved = self._budget_totals(
                conn, project.project_id, timestamp
            )
        return BudgetSnapshot(
            project_id=project.project_id,
            monthly_budget_micros=project.monthly_budget_micros,
            daily_budget_micros=project.daily_budget_micros,
            month_settled_micros=month_settled,
            month_reserved_micros=month_reserved,
            day_settled_micros=day_settled,
            day_reserved_micros=day_reserved,
        )

    def reserve(
        self,
        *,
        project: BudgetPolicy,
        request_id: str,
        route_model: str,
        reserved_micros: int,
        ttl_seconds: int,
        idempotent: bool,
        decision: str | None = None,
        now: int | None = None,
    ) -> BudgetSnapshot:
        """Atomically reserve headroom for one request.

        Raises `ReservationDenied` when any budget would be exceeded and
        `BudgetUnavailable` when the ledger itself cannot be read/written.
        """
        timestamp = now if now is not None else now_ms()
        try:
            with self._write_transaction() as conn:
                month_settled, month_reserved, day_settled, day_reserved = self._budget_totals(
                    conn, project.project_id, timestamp
                )
                monthly_budget = project.monthly_budget_micros
                daily_budget = project.daily_budget_micros
                snap = BudgetSnapshot(
                    project_id=project.project_id,
                    monthly_budget_micros=monthly_budget,
                    daily_budget_micros=daily_budget,
                    month_settled_micros=month_settled,
                    month_reserved_micros=month_reserved,
                    day_settled_micros=day_settled,
                    day_reserved_micros=day_reserved,
                )
                reason = self._denial_reason(snap, reserved_micros)
                if reason is not None:
                    # The context manager rolls back on any exception.
                    raise ReservationDenied(reason, snap, reserved_micros)
                conn.execute(
                    """
                    INSERT INTO requests (
                        request_id, project_id, route_model, status, idempotent,
                        reserved_micros, attempts, created_at_ms, expires_at_ms, decision
                    ) VALUES (?, ?, ?, 'reserved', ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        request_id,
                        project.project_id,
                        route_model,
                        1 if idempotent else 0,
                        reserved_micros,
                        timestamp,
                        timestamp + ttl_seconds * 1000,
                        decision,
                    ),
                )
                return snap
        except sqlite3.Error as exc:
            raise BudgetUnavailable(str(exc)) from exc

    @staticmethod
    def _denial_reason(snapshot: BudgetSnapshot, requested_micros: int) -> str | None:
        if snapshot.month_committed_micros + requested_micros > snapshot.monthly_budget_micros:
            return "monthly_budget_exhausted"
        daily = snapshot.daily_budget_micros
        if daily is not None and snapshot.day_committed_micros + requested_micros > daily:
            return "daily_budget_exhausted"
        return None

    # ------------------------------------------------------------------ attempts

    def record_attempt(
        self,
        *,
        request_id: str,
        attempt_number: int,
        provider: str,
        provider_model: str,
        status: str,
        error_class: str | None,
        http_status: int | None,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
        latency_ms: int,
        provider_request_id: str | None = None,
        now: int | None = None,
    ) -> None:
        timestamp = now if now is not None else now_ms()
        with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO attempts (
                    attempt_id, request_id, attempt_number, provider, provider_model,
                    status, error_class, http_status, input_tokens, output_tokens,
                    cost_micros, latency_ms, provider_request_id, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    request_id,
                    attempt_number,
                    provider,
                    provider_model,
                    status,
                    error_class,
                    http_status,
                    input_tokens,
                    output_tokens,
                    cost_micros,
                    latency_ms,
                    provider_request_id,
                    timestamp,
                ),
            )
            conn.execute(
                "UPDATE requests SET attempts = MAX(attempts, ?) WHERE request_id = ?",
                (attempt_number, request_id),
            )

    def settle(
        self,
        *,
        request_id: str,
        cost_micros: int,
        now: int | None = None,
    ) -> None:
        """Settle a reservation at the provider-evidenced cost."""
        timestamp = now if now is not None else now_ms()
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT status FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Unknown request {request_id}")
            if row["status"] != "reserved":
                raise RuntimeError(f"Request {request_id} is already {row['status']}")
            conn.execute(
                """
                UPDATE requests
                SET status = 'settled', settled_micros = ?, completed_at_ms = ?
                WHERE request_id = ?
                """,
                (cost_micros, timestamp, request_id),
            )

    def fail(self, *, request_id: str, error_code: str, now: int | None = None) -> None:
        """Close a reservation with no charge (provider failed before billing evidence)."""
        timestamp = now if now is not None else now_ms()
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT status FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Unknown request {request_id}")
            if row["status"] != "reserved":
                return
            conn.execute(
                """
                UPDATE requests
                SET status = 'failed', error_code = ?, completed_at_ms = ?
                WHERE request_id = ?
                """,
                (error_code, timestamp, request_id),
            )

    def release(self, *, request_id: str, error_code: str, now: int | None = None) -> None:
        """Release a reservation after an ambiguous outcome (e.g. timeout)."""
        timestamp = now if now is not None else now_ms()
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT status FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Unknown request {request_id}")
            if row["status"] != "reserved":
                return
            conn.execute(
                """
                UPDATE requests
                SET status = 'released', error_code = ?, released_at_ms = ?
                WHERE request_id = ?
                """,
                (error_code, timestamp, request_id),
            )

    # -------------------------------------------------------------------- audits

    @staticmethod
    def _parse_decision(raw: str | None) -> dict[str, Any] | None:
        """Decode the stored routing plan; never raise on a malformed row."""
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def record_rejection(
        self,
        *,
        project_id: str,
        request_id: str,
        route_model: str,
        reason: str,
        now: int | None = None,
    ) -> None:
        """Persist a refused request as a zero-cost terminal row.

        Rejections happen before a reservation exists, so this is the only place a
        block becomes visible in the ledger. `decision = 'blocked'` distinguishes it
        from a request that was admitted and later failed.
        """
        timestamp = now if now is not None else now_ms()
        with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO requests (
                    request_id, project_id, route_model, status, idempotent,
                    reserved_micros, settled_micros, attempts,
                    created_at_ms, expires_at_ms, completed_at_ms, error_code, decision
                ) VALUES (?, ?, ?, 'failed', 0, 0, 0, 0, ?, ?, ?, ?, 'blocked')
                """,
                (
                    request_id,
                    project_id,
                    route_model,
                    timestamp,
                    timestamp,
                    timestamp,
                    reason,
                ),
            )
            # The reason is also an audit event, so a refusal explains itself in the
            # request detail view without the reader needing to decode `error_code`.
            conn.execute(
                """
                INSERT INTO audit_events (at_ms, request_id, project_id, event, detail)
                VALUES (?, ?, ?, 'request_rejected', ?)
                """,
                (timestamp, request_id, project_id, f"Refused before forwarding: {reason}"),
            )

    def audit(
        self,
        *,
        event: str,
        detail: str,
        project_id: str | None = None,
        request_id: str | None = None,
        now: int | None = None,
    ) -> None:
        timestamp = now if now is not None else now_ms()
        with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (at_ms, request_id, project_id, event, detail)
                VALUES (?, ?, ?, ?, ?)
                """,
                (timestamp, request_id, project_id, event, detail),
            )

    # ------------------------------------------------------------------ queries

    def request_row(self, request_id: str) -> RequestRow | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        return RequestRow(
            request_id=row["request_id"],
            project_id=row["project_id"],
            route_model=row["route_model"],
            status=row["status"],
            reserved_micros=row["reserved_micros"],
            settled_micros=row["settled_micros"],
            attempts=row["attempts"],
            created_at_ms=row["created_at_ms"],
            expires_at_ms=row["expires_at_ms"],
            completed_at_ms=row["completed_at_ms"],
            error_code=row["error_code"],
        )

    def recent_requests(
        self, *, project_id: str, limit: int = 50, since_ms: int | None = None
    ) -> list[dict[str, Any]]:
        """Newest-first request metadata for one project.

        Deliberately excludes prompt and completion text: this backs the operator
        request list, which must not leak content even when retention is enabled.
        """
        clauses = ["r.project_id = ?"]
        params: list[Any] = [project_id]
        if since_ms is not None:
            clauses.append("r.created_at_ms >= ?")
            params.append(since_ms)
        params.append(limit)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT r.request_id, r.route_model, r.status, r.reserved_micros,
                       r.settled_micros, r.attempts, r.created_at_ms, r.completed_at_ms,
                       r.error_code, r.decision,
                       (SELECT a.provider FROM attempts a
                         WHERE a.request_id = r.request_id
                         ORDER BY a.attempt_number DESC LIMIT 1) AS last_provider
                FROM requests r
                WHERE {" AND ".join(clauses)}
                ORDER BY r.created_at_ms DESC, r.rowid DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "request_id": row["request_id"],
                "route_model": row["route_model"],
                "status": row["status"],
                "reserved_micros": row["reserved_micros"],
                "settled_micros": row["settled_micros"],
                "attempts": row["attempts"],
                "created_at_ms": row["created_at_ms"],
                "completed_at_ms": row["completed_at_ms"],
                "error_code": row["error_code"],
                # `outcome` is the one-word verdict an operator scans for. A failure
                # with no attempt never reached a provider, so it is a refusal, not
                # a provider error; the two must not look alike in the list.
                "outcome": (
                    "blocked"
                    if row["status"] == "failed" and row["attempts"] == 0
                    else row["status"]
                ),
                "decision": self._parse_decision(row["decision"]),
                "last_provider": row["last_provider"],
            }
            for row in rows
        ]

    def audit_for(self, request_id: str) -> list[dict[str, Any]]:
        """Audit events attached to one request, oldest first.

        Detail strings are produced by the gateway and never contain prompt text.
        """
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT at_ms, event, detail FROM audit_events WHERE request_id = ? "
                "ORDER BY at_ms, rowid",
                (request_id,),
            ).fetchall()
        return [
            {"at_ms": row["at_ms"], "event": row["event"], "detail": row["detail"]} for row in rows
        ]

    def provider_health(self, *, project_id: str, since_ms: int) -> list[dict[str, Any]]:
        """Per-provider attempt outcomes observed inside the window.

        Health here means "what this gateway observed", not a synthetic probe: a
        provider with no attempts is reported with zeroes and the UI says so.
        """
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT a.provider,
                       COUNT(*) AS attempts,
                       SUM(CASE WHEN a.status = 'success' THEN 1 ELSE 0 END) AS successes,
                       SUM(CASE WHEN a.status <> 'success' THEN 1 ELSE 0 END) AS failures,
                       MAX(a.created_at_ms) AS last_seen_ms
                FROM attempts a
                JOIN requests r ON r.request_id = a.request_id
                WHERE r.project_id = ? AND a.created_at_ms >= ?
                GROUP BY a.provider
                ORDER BY a.provider
                """,
                (project_id, since_ms),
            ).fetchall()
            last_errors = {
                row["provider"]: row["error_class"]
                for row in conn.execute(
                    """
                    SELECT a.provider, a.error_class
                    FROM attempts a
                    JOIN requests r ON r.request_id = a.request_id
                    WHERE r.project_id = ? AND a.error_class IS NOT NULL
                    ORDER BY a.created_at_ms
                    """,
                    (project_id,),
                ).fetchall()
            }
        return [
            {
                "provider": row["provider"],
                "attempts": row["attempts"],
                "successes": row["successes"],
                "failures": row["failures"],
                "last_seen_ms": row["last_seen_ms"],
                "last_error_class": last_errors.get(row["provider"]),
            }
            for row in rows
        ]

    def attempts_for(self, request_id: str) -> list[AttemptRow]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM attempts WHERE request_id = ? ORDER BY attempt_number",
                (request_id,),
            ).fetchall()
        return [
            AttemptRow(
                attempt_id=row["attempt_id"],
                request_id=row["request_id"],
                attempt_number=row["attempt_number"],
                provider=row["provider"],
                provider_model=row["provider_model"],
                status=row["status"],
                error_class=row["error_class"],
                http_status=row["http_status"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cost_micros=row["cost_micros"],
                latency_ms=row["latency_ms"],
                created_at_ms=row["created_at_ms"],
            )
            for row in rows
        ]

    def usage_report(
        self, *, project_id: str | None = None, since_ms: int | None = None
    ) -> dict[str, Any]:
        """Aggregate settled usage and per-request reconciliation.

        `reconciled` is true only when every settled request's charged amount equals
        the sum of its successful attempt costs.
        """
        clauses = ["1 = 1"]
        params: list[Any] = []
        if project_id is not None:
            clauses.append("r.project_id = ?")
            params.append(project_id)
        if since_ms is not None:
            clauses.append("r.created_at_ms >= ?")
            params.append(since_ms)
        where = " AND ".join(clauses)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    r.request_id, r.project_id, r.status, r.settled_micros, r.attempts,
                    COALESCE(SUM(a.cost_micros), 0) AS attempt_cost_micros,
                    COUNT(a.attempt_id) AS attempt_rows,
                    COALESCE(SUM(CASE WHEN a.status = 'success' THEN 1 ELSE 0 END), 0)
                        AS success_attempts
                FROM requests r
                LEFT JOIN attempts a ON a.request_id = r.request_id
                WHERE {where}
                GROUP BY r.request_id
                ORDER BY r.created_at_ms
                """,
                params,
            ).fetchall()
            totals = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS requests,
                    COALESCE(SUM(r.settled_micros), 0) AS settled_micros,
                    COALESCE(SUM(CASE WHEN r.status = 'settled' THEN 1 ELSE 0 END), 0)
                        AS settled_requests,
                    COALESCE(SUM(CASE WHEN r.status = 'failed' THEN 1 ELSE 0 END), 0)
                        AS failed_requests,
                    COALESCE(SUM(CASE WHEN r.status = 'released' THEN 1 ELSE 0 END), 0)
                        AS released_requests,
                    COALESCE(SUM(r.reserved_micros), 0) AS reserved_micros
                FROM requests r
                WHERE {where}
                """,
                params,
            ).fetchone()
            token_totals = conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(a.input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(a.output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(CASE WHEN a.status = 'success' THEN a.latency_ms ELSE 0 END), 0)
                        AS success_latency_ms,
                    COALESCE(SUM(CASE WHEN a.status = 'success' THEN 1 ELSE 0 END), 0)
                        AS success_attempts,
                    COUNT(a.attempt_id) AS attempt_rows
                FROM requests r
                LEFT JOIN attempts a ON a.request_id = r.request_id
                WHERE {where}
                """,
                params,
            ).fetchone()

        mismatches = [
            row["request_id"]
            for row in rows
            if row["status"] == "settled"
            and int(row["settled_micros"]) != int(row["attempt_cost_micros"])
        ]
        settled = int(totals["settled_requests"])
        success_attempts = int(token_totals["success_attempts"])
        return {
            "project_id": project_id,
            "since_ms": since_ms,
            "requests": int(totals["requests"]),
            "settled_requests": settled,
            "failed_requests": int(totals["failed_requests"]),
            "released_requests": int(totals["released_requests"]),
            "attempts": int(token_totals["attempt_rows"]),
            "successful_attempts": success_attempts,
            "settled_micros": int(totals["settled_micros"]),
            "input_tokens": int(token_totals["input_tokens"]),
            "output_tokens": int(token_totals["output_tokens"]),
            "avg_latency_ms": (
                round(int(token_totals["success_latency_ms"]) / success_attempts)
                if success_attempts
                else 0
            ),
            "reconciled": not mismatches,
            "mismatched_request_ids": mismatches,
        }

    def request_trace(self, request_id: str) -> dict[str, Any] | None:
        request = self.request_row(request_id)
        if request is None:
            return None
        attempts = self.attempts_for(request_id)
        return {
            "request_id": request.request_id,
            "project_id": request.project_id,
            "route_model": request.route_model,
            "status": request.status,
            "error_code": request.error_code,
            "outcome": (
                "blocked" if request.status == "failed" and not attempts else request.status
            ),
            "reserved_micros": request.reserved_micros,
            "settled_micros": request.settled_micros,
            "created_at_ms": request.created_at_ms,
            "completed_at_ms": request.completed_at_ms,
            "audit": self.audit_for(request_id),
            "attempts": [
                {
                    "attempt": attempt.attempt_number,
                    "provider": attempt.provider,
                    "provider_model": attempt.provider_model,
                    "status": attempt.status,
                    "error_class": attempt.error_class,
                    "http_status": attempt.http_status,
                    "input_tokens": attempt.input_tokens,
                    "output_tokens": attempt.output_tokens,
                    "cost_micros": attempt.cost_micros,
                    "latency_ms": attempt.latency_ms,
                }
                for attempt in attempts
            ],
        }

    def purge_older_than(self, *, cutoff_ms: int) -> dict[str, int]:
        """Delete request/attempt/audit rows older than `cutoff_ms`.

        Attempts are deleted first so no orphan reference can outlive its request.
        """
        with self._write_transaction() as conn:
            attempt_ids = conn.execute(
                "SELECT request_id FROM requests WHERE created_at_ms < ?", (cutoff_ms,)
            ).fetchall()
            ids = [row["request_id"] for row in attempt_ids]
            attempts = 0
            requests = 0
            for chunk_start in range(0, len(ids), 500):
                chunk = ids[chunk_start : chunk_start + 500]
                placeholders = ",".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"DELETE FROM attempts WHERE request_id IN ({placeholders})", chunk
                )
                attempts += cursor.rowcount
                cursor = conn.execute(
                    f"DELETE FROM requests WHERE request_id IN ({placeholders})", chunk
                )
                requests += cursor.rowcount
            audit = conn.execute("DELETE FROM audit_events WHERE at_ms < ?", (cutoff_ms,)).rowcount
        return {"requests": requests, "attempts": attempts, "audit_events": audit}

    def retention_cutoff_ms(self, *, retention_days: int, now: int | None = None) -> int:
        timestamp = now if now is not None else now_ms()
        delta = timedelta(days=retention_days)
        return int((datetime.fromtimestamp(timestamp / 1000, tz=UTC) - delta).timestamp() * 1000)
