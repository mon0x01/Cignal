import logging
import time

from redis import Redis
from rq import Queue

from app.core.config import settings
from app.db.models import AcquisitionBackfillRun, PairCandidateMiningRun
from app.db.session import SessionLocal
from app.integrations.registry import connectors
from app.services.acquisition_schedule import claim_due_sources, release_source_claim
from app.services.event_discovery_schedule import schedule_continuous_event_discovery
from app.services.job_outbox import dispatch_pending_jobs
from app.services.scheduler_jobs import enqueue_daily_evidence_integrity_audit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cignal.scheduler")


def _schedule_due_source_jobs(redis, queue) -> int:
    """Claim due providers independently and enqueue one deterministic job each."""
    scheduled = 0
    with SessionLocal() as db:
        claims = claim_due_sources(db, connectors())
    for claim in claims:
        source_type = claim["source_type"]
        token = claim["lease_token"]
        job_id = f"cignal-ingestion-source-{source_type}"
        try:
            existing = queue.fetch_job(job_id)
            if existing is not None:
                status = existing.get_status(refresh=True)
                if status in {"queued", "started", "deferred", "scheduled"}:
                    with SessionLocal() as db:
                        release_source_claim(db, source_type, token, delay_seconds=60)
                    continue
                existing.delete()
            queue.enqueue(
                "app.worker.ingest_all_sources",
                "scheduled",
                source_type,
                token,
                job_id=job_id,
                job_timeout=max(300, int(claim["job_timeout_seconds"])),
                result_ttl=3600,
            )
            scheduled += 1
            logger.info("Scheduled source acquisition: %s", source_type)
        except Exception:
            with SessionLocal() as db:
                release_source_claim(db, source_type, token, delay_seconds=60)
            logger.exception("Unable to schedule source acquisition: %s", source_type)
    return scheduled


def _recover_acquisition_backfills(queue) -> int:
    """Re-enqueue queued or lease-expired historical backfills after worker loss."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    with SessionLocal() as db:
        runs = (
            db.query(AcquisitionBackfillRun)
            .filter(
                (AcquisitionBackfillRun.status == "queued")
                | (
                    (AcquisitionBackfillRun.status == "running")
                    & (
                        AcquisitionBackfillRun.lease_expires_at.is_(None)
                        | (AcquisitionBackfillRun.lease_expires_at < now)
                    )
                )
            )
            .order_by(AcquisitionBackfillRun.created_at.asc())
            .limit(10)
            .all()
        )
        run_ids = [run.id for run in runs]
    scheduled = 0
    for run_id in run_ids:
        job_id = f"cignal-acquisition-backfill-{run_id}"
        try:
            existing = queue.fetch_job(job_id)
            if existing is not None:
                state = existing.get_status(refresh=True)
                if state in {"queued", "started", "deferred", "scheduled"}:
                    continue
                existing.delete()
            queue.enqueue(
                "app.worker.run_acquisition_backfill", run_id,
                job_id=job_id, job_timeout=1800, result_ttl=86400,
            )
            scheduled += 1
        except Exception:
            logger.exception("Unable to recover acquisition backfill %s", run_id)
    return scheduled


def _recover_pair_candidate_mining(queue) -> int:
    """Re-enqueue queued or lease-expired mining jobs after worker loss."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    with SessionLocal() as db:
        runs = (
            db.query(PairCandidateMiningRun)
            .filter(
                (PairCandidateMiningRun.status == "queued")
                | (
                    (PairCandidateMiningRun.status == "running")
                    & (
                        PairCandidateMiningRun.lease_expires_at.is_(None)
                        | (PairCandidateMiningRun.lease_expires_at < now)
                    )
                )
            )
            .order_by(PairCandidateMiningRun.created_at.asc())
            .limit(5)
            .all()
        )
        run_ids = [run.id for run in runs]
    scheduled = 0
    for run_id in run_ids:
        job_id = f"cignal-pair-candidate-mining-{run_id}"
        try:
            existing = queue.fetch_job(job_id)
            if existing is not None:
                state = existing.get_status(refresh=True)
                if state in {"queued", "started", "deferred", "scheduled"}:
                    continue
                existing.delete()
            queue.enqueue(
                "app.worker.mine_pair_candidates", run_id,
                job_id=job_id, job_timeout=1800, result_ttl=86400,
            )
            scheduled += 1
        except Exception:
            logger.exception("Unable to recover pair candidate mining run %s", run_id)
    return scheduled


def main():
    redis = Redis.from_url(settings.redis_url)
    queue = Queue("cignal", connection=redis)
    tick = max(15, min(300, int(settings.ingest_scheduler_tick_seconds)))
    logger.info("Per-source acquisition scheduler active; tick=%s seconds", tick)
    while True:
        tick_lock = None
        try:
            tick_lock = redis.lock(
                "cignal:scheduler:source-acquisition",
                timeout=tick - 2,
                blocking_timeout=0,
            )
            if tick_lock.acquire(blocking=False):
                try:
                    with SessionLocal() as discovery_db:
                        scheduled_discovery = schedule_continuous_event_discovery(discovery_db)
                    if scheduled_discovery:
                        logger.info("Scheduled continuous event discovery run=%s", scheduled_discovery["run_id"])
                except Exception:
                    logger.exception("Continuous event-discovery scheduling cycle failed")
                try:
                    outbox_result = dispatch_pending_jobs(queue, batch_size=100)
                    if outbox_result["claimed"]:
                        logger.info("Durable job outbox dispatch: %s", outbox_result)
                except Exception:
                    # An outbox relay failure must not block independent acquisition,
                    # retention, integrity-audit or backup scheduling.
                    logger.exception("Durable job outbox dispatch cycle failed")
                scheduled = _schedule_due_source_jobs(redis, queue)
                recovered_backfills = _recover_acquisition_backfills(queue)
                recovered_mining = _recover_pair_candidate_mining(queue)
                if scheduled:
                    logger.info("Scheduled %s due source(s)", scheduled)
                if recovered_backfills:
                    logger.info("Recovered %s historical acquisition backfill(s)", recovered_backfills)
                if recovered_mining:
                    logger.info("Recovered %s pair-candidate mining run(s)", recovered_mining)
            if settings.retention_enabled:
                retention_lock = redis.lock("cignal:scheduler:retention", timeout=23 * 60 * 60, blocking_timeout=1)
                if retention_lock.acquire(blocking=False):
                    queue.enqueue("app.worker.apply_retention_policy", job_timeout=1800, result_ttl=3600)
                    logger.info("Scheduled data-retention job")
            integrity_run_id = enqueue_daily_evidence_integrity_audit(
                redis, queue, enabled=settings.evidence_integrity_audit_enabled
            )
            if integrity_run_id:
                logger.info("Scheduled daily evidence integrity audit %s", integrity_run_id)
            backup_lock = redis.lock("cignal:scheduler:backup", timeout=23 * 60 * 60, blocking_timeout=1)
            if backup_lock.acquire(blocking=False):
                queue.enqueue("app.worker.run_scheduled_backup", job_timeout=3600, result_ttl=86400)
                logger.info("Scheduled daily backup job")
        except Exception:
            logger.exception("Scheduling cycle failed")
        finally:
            if tick_lock is not None:
                try:
                    if tick_lock.owned():
                        tick_lock.release()
                except Exception:
                    logger.debug("Source scheduling tick lease cleanup failed", exc_info=True)
        time.sleep(tick)


if __name__ == "__main__":
    main()
