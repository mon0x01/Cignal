# Evidence integrity auditing

Cignal stores a SHA-256 digest and byte count when evidence is accepted. The integrity audit re-reads each stored object and compares its current digest and size with that upload-time reference. It is a post-ingest integrity check; it does **not** establish who created an artifact, prove that its contents are truthful, or authenticate its source.

## Behavior

- Scans database rows in bounded batches, using a consistent upload-time cutoff.
- Supports the configured local and S3-compatible storage adapters.
- Detects missing objects, unreadable objects, invalid metadata, digest mismatches, and byte-count mismatches.
- Writes an audit entry for each failed evidence check and a summary entry for every completed scan.
- Does not modify, delete, quarantine, or automatically repair evidence.
- Keeps only a bounded sample of failed evidence IDs in the summary; per-object audit rows contain the expected and actual digest/size details.

## Scheduling and administration

Daily scheduling is enabled by default. Set `EVIDENCE_INTEGRITY_AUDIT_ENABLED=false` in `.env` to disable automatic daily scans. The scheduler uses a Redis lease to prevent duplicate daily enqueues. The scan itself runs on the background worker.

Administrators can also start a scan with `POST /api/admin/evidence-integrity-audits` and poll `GET /api/admin/evidence-integrity-audits/{run_id}`. Both endpoints require an authenticated administrator account with MFA completed.

A queued scan returns a `run_id` and RQ `job_id`. A completed or failed status is read from the audit log. If the worker process terminates before it can record a terminal state, the run remains `queued`; inspect the RQ worker logs before retrying.

## Operational trade-off

A full scan reads all stored evidence and therefore consumes disk or object-store I/O. It is appropriate for the current small installation and can be disabled as evidence volume grows. For multi-terabyte archives, replace the full daily scan with a rolling/incremental verification policy and periodic full audits.

Integrity failures are alerts for human investigation. Cignal never silently substitutes, deletes, or rewrites evidence.
