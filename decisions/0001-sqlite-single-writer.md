# 0001 — SQLite as the single source of truth

- **Status:** accepted
- **Date:** 2026-10-20

## Context

Budget enforcement is a correctness property: two concurrent requests must not both observe
the same remaining headroom. The project must also be runnable by one person on one machine
with no external service, and the "zero-cost development" constraint rules out a managed
database for the default path.

Options considered:

1. **In-memory counters.** Fast, but a restart forgets spend and two processes cannot share
   a cap. Fails the core promise.
2. **SQLite file.** ACID, one file, no daemon, WAL gives concurrent readers. Writers
   serialize, which is exactly the property a budget gate wants.
3. **PostgreSQL.** Correct and concurrent, but adds an operational dependency that the
   target user does not have. Rejected for v1; the ledger interface is narrow enough to
   revisit.
4. **Redis with Lua.** Atomic and fast, but a second service, and durability depends on
   configuration most users get wrong.

## Decision

SQLite, one file per deployment, WAL journaling, `synchronous=FULL`, and one
`BEGIN IMMEDIATE` transaction per budget decision.

The write lock is taken at the start of the transaction rather than at first write, so the
read of committed spend and the insert of the reservation cannot be interleaved with another
writer. `busy_timeout` is 30 seconds; past that the gateway fails closed instead of spending
unaccounted.

## Consequences

- Throughput is bounded by one writer. Measured: ~196 req/s in-process on a developer
  machine; the HTTP harness saturates earlier on the same box. This is enough for the
  intended user and is documented rather than papered over.
- Multiple gateway processes can share the file and still enforce one cap. Their rate and
  concurrency limits do not combine (see ADR 0004).
- `synchronous=FULL` costs an fsync per commit. Commit count per request was reduced by
  pooling connections and by not opening a connection per transaction; `NORMAL` was rejected
  because losing recent settlements on power loss would understate spend.
- Schema changes require a migration story. The ledger refuses to open a newer
  `schema_version` rather than guessing, and v1 ships without automated migrations.
