import json
import logging

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger(__name__)


def redis_client() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=2)


def publish_event(event_type: str, payload: dict) -> None:
    """Publish best-effort realtime events without breaking committed workflows."""
    client = None
    try:
        client = redis_client()
        client.publish("cignal:events", json.dumps({"type": event_type, "payload": payload}))
    except RedisError:
        logger.warning("Realtime event publish skipped; Redis is unavailable")
    except Exception:
        logger.exception("Realtime event publish failed")
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.debug("Redis client close failed", exc_info=True)


def enqueue_job(function_path: str, *args):
    try:
        from redis import Redis
        from rq import Queue
        connection = Redis.from_url(settings.redis_url)
        try:
            queue = Queue("cignal", connection=connection)
            if function_path == "app.worker.process_report" and args:
                # A report may legitimately be reanalyzed after new information/evidence.
                # Key jobs by content revision, not only by report ID.
                report_id = str(args[0])
                revision = int(args[1]) if len(args) > 1 else 1
                revision = max(1, revision)
                job_id = f"cignal-report-analysis-{report_id}-r{revision}"
                job_args = (report_id, revision)
                existing = queue.fetch_job(job_id)
                if existing is not None:
                    state = existing.get_status(refresh=True)
                    if state in {"queued", "started", "deferred", "scheduled", "finished"}:
                        return existing
                    existing.delete()
                try:
                    return queue.enqueue(
                        function_path, *job_args, job_id=job_id, job_timeout=300, result_ttl=3600
                    )
                except Exception:
                    # A concurrent scheduler/API request may have won the same job ID.
                    raced = queue.fetch_job(job_id)
                    if raced is not None and raced.get_status(refresh=True) in {
                        "queued", "started", "deferred", "scheduled", "finished"
                    }:
                        return raced
                    raise
            if function_path in {
                "app.worker.run_acquisition_backfill",
                "app.worker.mine_pair_candidates",
            } and args:
                run_id = str(args[0])
                prefix = "cignal-acquisition-backfill" if function_path.endswith("run_acquisition_backfill") else "cignal-pair-candidate-mining"
                job_id = f"{prefix}-{run_id}"
                existing = queue.fetch_job(job_id)
                if existing is not None:
                    state = existing.get_status(refresh=True)
                    if state in {"queued", "started", "deferred", "scheduled"}:
                        return existing
                    existing.delete()
                return queue.enqueue(
                    function_path, *args, job_id=job_id,
                    job_timeout=1800, result_ttl=86400,
                )
            if function_path == "app.worker.ingest_all_sources":
                # A global job and a source-scoped job have distinct deterministic IDs.
                # This prevents duplicate queue entries while allowing providers to
                # run independently without serializing the entire ingestion fleet.
                selected_source = str(args[1]).strip().lower() if len(args) > 1 and args[1] else None
                job_id = (
                    f"cignal-ingestion-source-{selected_source}"
                    if selected_source else "cignal-ingestion-all-sources"
                )
                existing = queue.fetch_job(job_id)
                if existing is not None:
                    state = existing.get_status(refresh=True)
                    if state in {"queued", "started", "deferred", "scheduled"}:
                        return existing
                    existing.delete()
                timeout = max(300, int(settings.ingest_job_timeout_seconds)) if selected_source else 900
                return queue.enqueue(
                    function_path, *args, job_id=job_id, job_timeout=timeout, result_ttl=3600
                )
            return queue.enqueue(function_path, *args, job_timeout=300, result_ttl=3600)
        finally:
            connection.close()
    except Exception:
        logger.exception("Unable to enqueue background job")
        return None
