# Testing and verification

## Run the test suite

Run the tests inside the API container:

```bash
sudo docker compose exec api pytest -q
```

The local suite covers rule-based multilingual triage helpers, password/JWT/TOTP cryptographic helpers, MFA-secret encryption, local evidence-storage path containment, relational model separation, Alembic schema migrations, idempotency constraints, the evidence integrity audit, WebSocket event authorization, and an end-to-end API workflow. The workflow test exercises initial administrator setup, MFA enrollment, report submission and replay protection, human verification, incident creation, responder activity updates, timeline pagination, closed-incident restrictions, citizen access control, and citizen follow-up after an analyst requests more information. It uses SQLite and test doubles for Redis/feed parsing; it does not validate live Redis workers, NiceGUI browser rendering, or external provider behavior.

## Quality tools

```bash
sudo docker compose exec api ruff check app tests
sudo docker compose exec api bandit -r app
```

## Backup verification

```bash
sudo ./scripts/backup.sh
sudo ./scripts/test_restore.sh data/backups/cignal-backup-YYYYMMDDTHHMMSSZ
```

`test_restore.sh` restores the dump into a separate temporary database and validates the evidence archive and SHA-256 checksums. `restore.sh` is the destructive full restore operation and requires `RESTORE_CONFIRM=YES`.

## Known verification boundary for this archive

The build sandbox had no Docker daemon and no outbound package network. Python compilation, the SQLite/Alembic migration path, and all local unit/API workflow tests were executed. Docker image build, NiceGUI page startup, real Redis/RQ job execution, external provider connectivity, email/Web Push delivery, browser PWA behavior, restore against PostgreSQL, and real-world load tests remain to be run on the target Arch Linux machine. The ZIP does not claim these were validated in the build sandbox.


## Incident activity verification

The activity suite validates supported/unsupported activity types, nonblank message normalization, coordinate pairing and ranges, additive migration behavior on both fresh and pre-existing schemas, actor attribution snapshots, responder write permissions, citizen denial, timeline pagination, audit metadata, closed-incident immutability and realtime event authorization.
