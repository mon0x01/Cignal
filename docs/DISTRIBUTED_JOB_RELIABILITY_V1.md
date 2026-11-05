# Distributed Job Reliability v1

## Purpose

Cignal's PostgreSQL outbox remains the source of truth for durable work requests. Phase 6 adds an execution lifecycle around RQ delivery so a Redis enqueue acknowledgement is no longer mistaken for successful work.

The guarantees are deliberately **at-least-once delivery with fenced, idempotent execution attempts**, not exactly-once side effects. Target functions still need their own domain-level idempotency for external or non-transactional side effects.

## Lifecycle

`pending -> publishing -> enqueued -> running -> completed`

Failure paths return to `pending` with bounded exponential backoff, or move to `dead` when the dispatch or execution budget is exhausted. An administrator can retry a dead-lettered row; that increments `execution_generation`, resets the current generation's counters, and preserves old attempt history.

A separate `background_job_execution_attempts` row is created for each execution attempt. It records worker identity, queue lane, RQ delivery ID, attempt generation/number, start/end time, lease deadline, failure class, and a SHA-256 digest of the result metadata. Task arguments and result bodies are not exposed by the admin summary.

## Worker fencing and crash recovery

Before a target function runs, the wrapper claims the outbox row under a database row lock and sets an execution lease. Because RQ uses forked workhorse processes, the wrapper disposes the inherited SQLAlchemy pool with `close=False` before it opens database connections in the child. On PostgreSQL, it also holds a session-level advisory lock derived from the logical outbox ID for the full target-function duration. A duplicate RQ delivery cannot execute the same logical job concurrently while that lock is held.

If a worker exits unexpectedly, its database connection is closed and PostgreSQL releases the advisory lock. The scheduler later recovers an expired execution lease, marks its attempt `abandoned`, and republishes within the remaining execution budget. The RQ delivery ID changes for each relay attempt; the logical outbox job ID remains stable. Migration 0021 backfills the Phase 5 logical RQ ID for both `enqueued` and interrupted `publishing` rows. The scheduler adopts an active legacy Redis delivery before the relay is allowed to publish a replacement. The relay acknowledgement is fenced by its lease token or, when a very fast worker has already cleared that token, by the exact persisted RQ delivery ID.

RQ is still an at-least-once queue. If a process is terminated after a domain transaction commits but before the wrapper records completion, the function may run again. Current report analysis and event replay functions have domain-level revision/status guards; future tasks must supply equivalent idempotency before being added to the outbox allowlist.

## Queue isolation

| Queue | Workload | Worker |
|---|---|---|
| `cignal-priority` | New/updated report analysis | Primary worker |
| `cignal` | Event discovery and default background work | Primary worker |
| `cignal-bulk` | Historical event replay | Bulk worker |

The primary worker checks the priority queue before the normal queue. The bulk worker is a separate Compose service so long-running replay work cannot occupy the report-analysis worker's only slot. Both worker services use the same image and persistent model/evidence mounts.

## Graceful shutdown

RQ's warm SIGTERM handling is retained. Compose allows a 65-minute stop grace period, covering the longest current scheduled backup timeout (60 minutes) plus a five-minute buffer; it also exceeds the maximum outbox job timeout plus its lease grace. A controlled deploy should therefore let an active job finish. A forced kill still relies on lease expiry and reconciliation.

Cignal disables RQ's embedded scheduler: the dedicated scheduler service owns dispatch, recovery, and timing in PostgreSQL. No task in the outbox currently uses RQ's delayed/deferred enqueue APIs.

## Recovery policy

- Dispatch failures use the existing bounded publish-attempt budget and exponential retry.
- Execution failures use `BACKGROUND_JOB_MAX_EXECUTION_ATTEMPTS` (default 8) and exponential retry capped at one hour.
- Expired execution leases are marked abandoned and retried or dead-lettered.
- Enqueued rows are reconciled against their last RQ delivery. Queued work is left alone; finished legacy deliveries are marked completed; failed/missing deliveries are retried; a started delivery beyond its hard timeout is treated as orphaned.
- A deterministic delivery ID is generated for each relay attempt, while the logical `job_id` remains stable across retries.
- PostgreSQL advisory locks fence overlapping executions if an expired worker resumes while a replacement is starting.

## Operations and observability

Administrators can inspect `/api/admin/background-jobs/outbox`. It now reports counts by state and queue, plus bounded recent fields including dispatch attempts, execution attempts/generation, last start/completion time, RQ delivery ID, and error classes. It never returns task arguments or target-function results.

Dead-letter retries remain available at `/api/admin/background-jobs/outbox/{job_id}/retry`; each manual retry starts a new execution generation while preserving the previous attempt ledger.

## Configuration

- `BACKGROUND_JOB_MAX_EXECUTION_ATTEMPTS=8`
- `BACKGROUND_JOB_DISPATCH_MAX_ATTEMPTS=12`
- `BACKGROUND_JOB_LEASE_GRACE_SECONDS=120`
- `BACKGROUND_JOB_RECONCILE_GRACE_SECONDS=120`

These are operational budgets, not guarantees that external systems will complete work. Monitor outbox age, dead-letter counts, queue depth, worker restarts, and persistent DB/Redis health before increasing workload.

## Safety boundaries

The wrapper allowlist is explicit. Do not allow arbitrary import paths or caller-provided function names. Public warnings, evacuation decisions, dispatch authorization, and medical intervention remain human-controlled and are not worker side effects.

## Delivery fencing and duplicate execution

Each RQ delivery is bound to the exact `last_rq_job_id` stored by the outbox relay. A worker whose RQ job ID is stale is rejected before it can create an execution attempt. Failed enqueue deliveries, failed execution attempts, expired execution leases and failed/missing queue reconciliation retire the prior RQ ID before the row becomes pending, so a manually requeued old RQ job cannot steal a later retry.

The PostgreSQL advisory execution fence is held for the complete target call. If a delivery cannot acquire that fence, the wrapper raises a retryable error so RQ records a failed delivery for scheduler reconciliation. It must not return success: a successful RQ status would otherwise be mistaken for completed domain work even though this process never ran the target function.
