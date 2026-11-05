# Operations, retention and recovery

## Services

- `db`: PostgreSQL 16 with PostGIS.
- `redis`: RQ queue, rate limiting and event pub/sub.
- `api`: FastAPI routes and database/API health.
- `frontend`: NiceGUI UI and browser WebSocket sessions.
- `worker`: asynchronous AI triage, source ingestion, retention and backup jobs.
- `scheduler`: periodically queues ingestion and daily retention/backup tasks.
- `caddy`: optional reverse proxy, enabled with Compose profile `edge`.

## Health and logs

```bash
sudo docker compose ps
sudo docker compose logs --tail=200 api worker scheduler
curl -fsS http://localhost:8000/api/health
```

A degraded Redis status affects background jobs, realtime notifications and fail-closed rate-limited operations. Restore Redis availability before accepting public traffic.

## Evidence storage

Default: local filesystem at `EVIDENCE_DIR=/data/evidence`, shared between API and worker through the Docker named volume `evidence_data`. Optional S3-compatible storage can be enabled with `STORAGE_BACKEND=s3` and endpoint, bucket and credential values. Ensure the bucket exists and configure provider-side encryption/lifecycle policies. The bundled adapter writes server-side encryption metadata where the S3-compatible service supports it.

## Retention

`RETENTION_DAYS` defaults to 365 and is clamped to at least 30 days by the cleanup job. Once daily the worker:

- Deletes evidence files/rows attached to reports older than the cutoff.
- Removes the `submitted_by` account link from old reports while retaining the report record for historical analysis.
- Deletes old notifications and expired refresh tokens.

It deliberately does not delete incident history or audit logs. It does not redact personal details that a reporter may have typed into report text. Review legal/operational retention requirements before enabling this policy on real data. Disable with `RETENTION_ENABLED=false` if the policy does not fit the deployment.

## Backups

The scheduler generates one backup per day under `data/backups/cignal-backup-*`:

- `database.dump`: PostgreSQL custom-format dump.
- `evidence.tar.gz`: evidence files from the shared volume.
- `manifest.json`: timestamp, file count and version metadata.
- `SHA256SUMS.txt`: integrity checksums.

The worker container runs as UID 10001. Create the host folder and allow that UID to write it:

```bash
mkdir -p data/backups
sudo chown -R 10001:10001 data/backups
```

Run an immediate backup:

```bash
sudo ./scripts/backup.sh
```

Test a backup by restoring the database into a separate temporary database and checking the evidence archive:

```bash
sudo ./scripts/test_restore.sh data/backups/cignal-backup-YYYYMMDDTHHMMSSZ
```

Perform a full restore only after checking the backup and confirming that overwriting the current database/evidence is intended:

```bash
RESTORE_CONFIRM=YES sudo -E ./scripts/restore.sh data/backups/cignal-backup-YYYYMMDDTHHMMSSZ
```

A restore stops API/UI/worker/scheduler writers, restores the Postgres dump, replaces evidence files, then starts services. Test restores before a real incident and keep at least one copy on a separate device or storage provider. A backup stored on the same physical drive is not adequate protection against disk loss.

## Deployment modes

### Local-only

Default published ports bind to loopback: UI `127.0.0.1:8080`, API `127.0.0.1:8000`. The database and Redis ports are not published.

### Temporary public preview without a domain

Set `API_PUBLIC_URL=` (empty) in `.env`, start Caddy with `sudo docker compose --profile edge up -d caddy`, then run `cloudflared tunnel --url http://localhost:8081`. Browser API requests use the same-origin `/api` reverse-proxy route. The temporary hostname is public and ephemeral; protect it with Cloudflare's allowed-email option where supported, or do not share it.

### Stable public deployment

A stable domain or named tunnel configuration is outside the no-domain temporary setup. If using Caddy automatic HTTPS directly, set `CIGNAL_DOMAIN` to the DNS hostname and publish ports 80/443 in Compose instead of the loopback-only edge mapping. Configure firewall, DNS, TLS renewal, off-host backups and monitoring. Never switch the Caddy site to public HTTP only.

## Capacity

No 100–500-user load test was performed in this build environment. Benchmark with realistic simultaneous browser WebSocket sessions, API throughput, DB query patterns, upload size and ingestion/AI queue backlog. Keep the database and workers on the same private network and add CPU/RAM/IO headroom before opening public registrations. Rate-limit tuning should be revisited with actual traffic.


## Evidence integrity

The scheduler queues a full evidence SHA-256 and byte-count audit once per day when `EVIDENCE_INTEGRITY_AUDIT_ENABLED=true` (the default). Administrators can trigger an additional background scan through the API. See [Evidence integrity auditing](EVIDENCE_INTEGRITY.md) for endpoints, failure states and scaling trade-offs. Integrity failures are logged for human investigation; evidence is never modified automatically.
