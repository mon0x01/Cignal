from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(24), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pending_mfa_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mfa_recovery_hashes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BackgroundJobOutbox(Base):
    """Transactional requests to enqueue background work after the parent commit."""
    __tablename__ = "background_job_outbox"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    function_path: Mapped[str] = mapped_column(String(180), nullable=False)
    task_args: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    queue_name: Mapped[str] = mapped_column(String(80), nullable=False, default="cignal", server_default="cignal")
    job_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300, server_default="300")
    result_ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600, server_default="3600")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','publishing','enqueued','dead')",
            name="ck_background_job_outbox_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_background_job_outbox_attempts"),
        CheckConstraint("job_timeout_seconds >= 1", name="ck_background_job_outbox_timeout"),
        Index("ix_background_job_outbox_status_available", "status", "available_at"),
        Index("ix_background_job_outbox_lease", "status", "lease_expires_at"),
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tracking_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    submitted_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text)
    disaster_type: Mapped[str] = mapped_column(String(32), default="unknown")
    urgency: Mapped[str] = mapped_column(String(16), default="unknown")
    status: Mapped[str] = mapped_column(String(24), default="submitted", index=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="en")
    source_type: Mapped[str] = mapped_column(String(24), default="citizen")
    source_name: Mapped[str] = mapped_column(String(100), default="Citizen submission")
    source_record_id: Mapped[str | None] = mapped_column(String(180), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(700), nullable=True)
    provenance: Mapped[str] = mapped_column(String(20), default="submitted")
    source_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    source_metadata: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"), nullable=False)
    analysis_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    analyzed_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ai_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_urgency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_analysis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    duplicate_of: Mapped[str | None] = mapped_column(ForeignKey("reports.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("source_type", "source_record_id", name="uq_report_source_record"),
        UniqueConstraint("idempotency_key", name="uq_report_idempotency_key"),
        Index("ix_report_coordinates", "latitude", "longitude"),
        Index("ix_reports_updated_at", "updated_at"),
        Index(
            "ix_reports_analysis_pending", "created_at", "id",
            postgresql_where=text("analysis_revision > analyzed_revision"),
            sqlite_where=text("analysis_revision > analyzed_revision"),
        ),
    )


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_key: Mapped[str] = mapped_column(String(700))
    mime_type: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    image_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AcquisitionSourceState(Base):
    """Durable per-provider scheduling, lease and retry state."""
    __tablename__ = "acquisition_source_states"
    source_type: Mapped[str] = mapped_column(String(24), primary_key=True)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=1800, server_default="1800", nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_status: Mapped[str] = mapped_column(
        String(20), default="not_scheduled", server_default="not_scheduled", nullable=False
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_records_fetched: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_records_inserted: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_records_duplicate: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_records_rejected: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        Index("ix_acquisition_source_due", "next_run_at", "lease_expires_at"),
    )


class AcquisitionObservation(Base):
    """Immutable normalized provider observation version with replay provenance."""
    __tablename__ = "acquisition_observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(180), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(700), nullable=True)
    normalized_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    report_id: Mapped[str | None] = mapped_column(
        ForeignKey("reports.id", ondelete="SET NULL"), nullable=True, index=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    replay_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_replayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    __table_args__ = (
        UniqueConstraint("source_type", "source_record_id", "content_sha256", name="uq_acq_observation_version"),
        Index("ix_acq_observations_source_seen", "source_type", "last_seen_at"),
        Index("ix_acq_observations_observed", "observed_at"),
    )


class AcquisitionBackfillRun(Base):
    """Resumable bounded historical acquisition job with committed window cursor."""
    __tablename__ = "acquisition_backfill_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    cursor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    page_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    max_records: Mapped[int] = mapped_column(Integer, nullable=False, default=10000, server_default="10000")
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    records_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    records_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    records_duplicate: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    records_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    observations_captured: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    records_queued: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    analysis_queue_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    windows_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    __table_args__ = (
        Index("ix_acq_backfill_status_created", "status", "created_at"),
        Index("ix_acq_backfill_source_cursor", "source_type", "cursor_at"),
    )


class AcquisitionBackfillItem(Base):
    """Durable run-to-observation link and per-record analysis enqueue state."""
    __tablename__ = "acquisition_backfill_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    backfill_run_id: Mapped[str] = mapped_column(
        ForeignKey("acquisition_backfill_runs.id", ondelete="CASCADE"), nullable=False
    )
    observation_id: Mapped[str] = mapped_column(
        ForeignKey("acquisition_observations.id", ondelete="CASCADE"), nullable=False
    )
    report_id: Mapped[str | None] = mapped_column(
        ForeignKey("reports.id", ondelete="SET NULL"), nullable=True
    )
    analysis_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    queue_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("backfill_run_id", "observation_id", name="uq_acq_backfill_item_observation"),
        Index("ix_acq_backfill_items_run_status", "backfill_run_id", "analysis_status"),
    )


class PairCandidateMiningRun(Base):
    """Auditable active-learning candidate-generation run."""
    __tablename__ = "pair_candidate_mining_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("research_datasets.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running", index=True)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reports_considered: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    pairs_scored: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    candidates_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    candidates_existing: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class EventPairCandidate(Base):
    """Unlabelled, reviewable pair candidate; mining never auto-labels it."""
    __tablename__ = "event_pair_candidates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("research_datasets.id", ondelete="CASCADE"), nullable=False)
    mining_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("pair_candidate_mining_runs.id", ondelete="SET NULL"), nullable=True
    )
    report_a_id: Mapped[str] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), nullable=False)
    report_b_id: Mapped[str] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), nullable=False)
    pair_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    heuristic_score: Mapped[float] = mapped_column(Float, nullable=False)
    supervised_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    selection_score: Mapped[float] = mapped_column(Float, nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    candidate_reasons: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("dataset_id", "pair_fingerprint", name="uq_event_pair_candidate_fingerprint"),
        Index("ix_pair_candidates_review", "dataset_id", "status", "selection_score"),
    )


class ModelEvaluationRun(Base):
    """Immutable benchmark result metadata; does not store training data or claim calibration."""
    __tablename__ = "model_evaluation_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    model_version: Mapped[str] = mapped_column(String(120), nullable=False)
    dataset_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    group_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False)
    baseline_metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    comparison: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evaluation_policy: Mapped[str] = mapped_column(String(80), nullable=False)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)



class IntegrationRun(Base):
    """Persisted source-ingestion outcomes without provider secrets."""
    __tablename__ = "integration_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(24), index=True)
    source_name: Mapped[str] = mapped_column(String(100))
    trigger: Mapped[str] = mapped_column(String(16), default="scheduled")
    status: Mapped[str] = mapped_column(String(16), index=True)
    records_fetched: Mapped[int] = mapped_column(Integer, default=0)
    fetch_attempts: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    records_inserted: Mapped[int] = mapped_column(Integer, default=0)
    records_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    records_rejected: Mapped[int] = mapped_column(Integer, default=0)
    records_queued: Mapped[int] = mapped_column(Integer, default=0)
    queue_failures: Mapped[int] = mapped_column(Integer, default=0)
    rejection_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __table_args__ = (
        Index("ix_integration_runs_source_started", "source_type", "started_at"),
        Index("ix_integration_runs_status_started", "status", "started_at"),
    )


class EventHypothesis(Base):
    """An unverified event hypothesis assembled from independent reports."""
    __tablename__ = "event_hypotheses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True)
    anchor_report_id: Mapped[str | None] = mapped_column(
        ForeignKey("reports.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    title: Mapped[str] = mapped_column(String(180))
    summary: Mapped[str] = mapped_column(Text)
    disaster_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(20), default="candidate", index=True)
    centroid_latitude: Mapped[float] = mapped_column(Float)
    centroid_longitude: Mapped[float] = mapped_column(Float)
    location_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    report_count: Mapped[int] = mapped_column(Integer, default=0)
    distinct_source_count: Mapped[int] = mapped_column(Integer, default=0)
    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    scoring_version: Mapped[str] = mapped_column(String(32))
    explanation: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        Index("ix_event_hypotheses_status_last_observed", "status", "last_observed_at"),
    )


class EventHypothesisReport(Base):
    """Reviewable, reversible association between a report and an event hypothesis."""
    __tablename__ = "event_hypothesis_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    hypothesis_id: Mapped[str] = mapped_column(
        ForeignKey("event_hypotheses.id", ondelete="CASCADE"), index=True
    )
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), index=True)
    association_status: Mapped[str] = mapped_column(String(16), default="proposed", index=True)
    relationship_type: Mapped[str] = mapped_column(String(16), default="supports")
    pair_score: Mapped[float] = mapped_column(Float, default=0.0)
    component_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("hypothesis_id", "report_id", name="uq_event_hypothesis_report"),
        Index("ix_event_hypothesis_reports_review", "hypothesis_id", "association_status"),
    )


class EventDiscoveryRun(Base):
    """Operational record for analyst-triggered or continuous event discovery."""
    __tablename__ = "event_discovery_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requested_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    trigger_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="analyst", server_default="analyst")
    dedupe_key: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    reports_scanned: Mapped[int] = mapped_column(Integer, default=0)
    clusters_found: Mapped[int] = mapped_column(Integer, default=0)
    hypotheses_created: Mapped[int] = mapped_column(Integer, default=0)
    hypotheses_updated: Mapped[int] = mapped_column(Integer, default=0)
    clusters_skipped: Mapped[int] = mapped_column(Integer, default=0)
    result_summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    __table_args__ = (
        Index("ix_event_discovery_runs_status_created", "status", "created_at"),
    )


class ReportSemanticEmbedding(Base):
    """Content-addressed multilingual embedding cache for report text."""
    __tablename__ = "report_semantic_embeddings"
    report_id: Mapped[str] = mapped_column(
        ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True
    )
    model_version: Mapped[str] = mapped_column(String(100), primary_key=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        Index("ix_report_semantic_embeddings_model_hash", "model_version", "content_sha256"),
    )


class EventHypothesisSnapshot(Base):
    """Append-only state snapshot used to reconstruct event evolution."""
    __tablename__ = "event_hypothesis_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    hypothesis_id: Mapped[str] = mapped_column(
        ForeignKey("event_hypotheses.id", ondelete="CASCADE"), nullable=False
    )
    discovery_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("event_discovery_runs.id", ondelete="SET NULL"), nullable=True
    )
    snapshot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    change_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    report_count: Mapped[int] = mapped_column(Integer, nullable=False)
    distinct_source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    match_score: Mapped[float] = mapped_column(Float, nullable=False)
    centroid_latitude: Mapped[float] = mapped_column(Float, nullable=False)
    centroid_longitude: Mapped[float] = mapped_column(Float, nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snapshot_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    __table_args__ = (
        UniqueConstraint("hypothesis_id", "snapshot_key", name="uq_event_hypothesis_snapshot_key"),
        Index("ix_event_hypothesis_snapshots_timeline", "hypothesis_id", "created_at"),
    )


class SourceReliabilityProfile(Base):
    """Separate operational health from analyst-reviewed association quality."""
    __tablename__ = "source_reliability_profiles"
    source_origin: Mapped[str] = mapped_column(String(150), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    ingestion_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    partial_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reviewed_associations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confirmed_associations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_associations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    operational_success_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_acceptance_posterior_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_acceptance_wilson_lower: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_confidence: Mapped[str] = mapped_column(String(20), default="insufficient", nullable=False)
    method_version: Mapped[str] = mapped_column(String(40), nullable=False)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        Index("ix_source_reliability_type_updated", "source_type", "updated_at"),
    )


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    incident_code: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(180))
    summary: Mapped[str] = mapped_column(Text)
    disaster_type: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(24), default="verified", index=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_text: Mapped[str | None] = mapped_column(String(220), nullable=True)
    source_count: Mapped[int] = mapped_column(Integer, default=1)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    provenance: Mapped[str] = mapped_column(String(20), default="mixed")
    verified_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IncidentActivity(Base):
    """Append-only operational notes attached to a verified incident."""
    __tablename__ = "incident_activities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_display_name: Mapped[str] = mapped_column(String(120))
    actor_role: Mapped[str] = mapped_column(String(24))
    activity_type: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(Text)
    location_text: Mapped[str | None] = mapped_column(String(220), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    __table_args__ = (
        Index("ix_incident_activity_timeline", "incident_id", "created_at", "id"),
    )


class IncidentResponsePlan(Base):
    """Versioned snapshot of an operator-selected response coordination playbook."""
    __tablename__ = "incident_response_plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_key: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    template_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "incident_id", "idempotency_key", name="uq_incident_response_plans_idempotency"
        ),
        Index("ix_incident_response_plans_incident_created", "incident_id", "created_at"),
        Index(
            "uq_incident_response_plans_active_incident", "incident_id", unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        CheckConstraint("template_version >= 1", name="ck_response_plan_template_version"),
        CheckConstraint(
            "status IN ('active','completed','cancelled','needs_review')",
            name="ck_response_plan_status",
        ),
    )


class IncidentAction(Base):
    """Durable operational task with explicit human-approval gates."""
    __tablename__ = "incident_actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    response_plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("incident_response_plans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sequence_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal", index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed", index=True)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approval_status: Mapped[str] = mapped_column(String(16), nullable=False, default="not_required")
    assigned_to: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approval_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("response_plan_id", "sequence_order", name="uq_incident_actions_plan_sequence"),
        Index("ix_incident_actions_incident_status_priority", "incident_id", "status", "priority"),
        Index("ix_incident_actions_assignee_status", "assigned_to", "status"),
        CheckConstraint("version >= 1", name="ck_incident_actions_version_positive"),
        CheckConstraint(
            "action_type IN ('monitoring','logistics','communications','medical_support','infrastructure','public_warning','evacuation','resource_dispatch','medical_intervention','other')",
            name="ck_incident_actions_type",
        ),
        CheckConstraint("priority IN ('low','normal','high','urgent')", name="ck_incident_actions_priority"),
        CheckConstraint(
            "status IN ('proposed','approved','in_progress','blocked','completed','cancelled','rejected')",
            name="ck_incident_actions_status",
        ),
        CheckConstraint(
            "approval_status IN ('not_required','pending','approved','rejected')",
            name="ck_incident_actions_approval_status",
        ),
        CheckConstraint(
            "NOT approval_required OR status NOT IN ('approved','in_progress','blocked','completed') OR approval_status = 'approved'",
            name="ck_incident_actions_approval_gate",
        ),
    )


class IncidentActionEvent(Base):
    """Append-only audit trail for task creation, approvals and state transitions."""
    __tablename__ = "incident_action_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("incident_actions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        Index("ix_incident_action_events_timeline", "action_id", "created_at", "id"),
    )



class IncidentActionDependency(Base):
    """Immutable prerequisite edge between two tasks in one response plan."""
    __tablename__ = "incident_action_dependencies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("incident_response_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action_id: Mapped[str] = mapped_column(
        ForeignKey("incident_actions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    depends_on_action_id: Mapped[str] = mapped_column(
        ForeignKey("incident_actions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "plan_id", "action_id", "depends_on_action_id",
            name="uq_incident_action_dependencies_unique",
        ),
        CheckConstraint("action_id <> depends_on_action_id", name="ck_action_dependency_not_self"),
        Index("ix_incident_action_dependencies_plan_action", "plan_id", "action_id"),
    )


class ReportIncident(Base):
    __tablename__ = "report_incidents"
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), primary_key=True)
    relationship_type: Mapped[str] = mapped_column(String(24), default="supports")
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    resource_type: Mapped[str] = mapped_column(String(50))
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(180))
    body: Mapped[str] = mapped_column(String(500))
    category: Mapped[str] = mapped_column(String(32), default="general")
    target_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    subscription_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResearchDataset(Base):
    """Governance registry for curated research/training datasets."""
    __tablename__ = "research_datasets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[str] = mapped_column(String(48), nullable=False, default="event_pair_classification")
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    license_identifier: Mapped[str] = mapped_column(String(120), nullable=False)
    license_url: Mapped[str | None] = mapped_column(String(700), nullable=True)
    legal_basis: Mapped[str] = mapped_column(String(40), nullable=False)
    consent_scope: Mapped[str] = mapped_column(String(40), nullable=False)
    source_types: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    coordinate_precision: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    privacy_policy: Mapped[str] = mapped_column(String(48), nullable=False, default="exclude_direct_identifiers")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (
        Index("ix_research_datasets_status_updated", "status", "updated_at"),
    )


class EventPairLabel(Base):
    """Human-reviewed same-event/different-event label with source provenance."""
    __tablename__ = "event_pair_labels"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("research_datasets.id", ondelete="CASCADE"), nullable=False)
    report_a_id: Mapped[str] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), nullable=False)
    report_b_id: Mapped[str] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), nullable=False)
    pair_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(20), nullable=False)
    split_group: Mapped[str] = mapped_column(String(120), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    label_method: Mapped[str] = mapped_column(String(32), nullable=False, default="analyst_review")
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    privacy_review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("dataset_id", "pair_fingerprint", name="uq_event_pair_label_fingerprint"),
        Index("ix_event_pair_labels_dataset_review", "dataset_id", "review_status", "privacy_review_status"),
        Index("ix_event_pair_labels_dataset_group", "dataset_id", "split_group"),
    )


class ResearchDatasetVersion(Base):
    """Immutable, sealed snapshot of reviewed event-pair labels."""
    __tablename__ = "research_dataset_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("research_datasets.id", ondelete="CASCADE"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="sealed")
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    same_event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    different_event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    split_group_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    sealed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("dataset_id", "version_number", name="uq_research_dataset_version"),
        Index("ix_research_dataset_versions_dataset_id", "dataset_id"),
        Index("ix_research_dataset_versions_dataset_created", "dataset_id", "created_at"),
    )


class ResearchDatasetVersionItem(Base):
    """Frozen, privacy-screened JSONL row and provenance for a dataset version."""
    __tablename__ = "research_dataset_version_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("research_dataset_versions.id", ondelete="CASCADE"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    pair_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    split_group: Mapped[str] = mapped_column(String(120), nullable=False)
    label: Mapped[str] = mapped_column(String(20), nullable=False)
    record_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    provenance_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("version_id", "ordinal", name="uq_research_dataset_version_ordinal"),
        UniqueConstraint("version_id", "pair_fingerprint", name="uq_research_dataset_version_pair"),
        Index("ix_research_dataset_version_items_version_id", "version_id"),
        Index("ix_research_dataset_version_items_group", "version_id", "split_group"),
    )


class EventReplayRun(Base):
    """Reproducible, non-mutating historical event-correlation replay."""
    __tablename__ = "event_replay_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requested_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    engine_version: Mapped[str] = mapped_column(String(40), nullable=False)
    scoring_version: Mapped[str] = mapped_column(String(40), nullable=False)
    input_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    baseline_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','completed','failed')", name="ck_event_replay_runs_status"),
        CheckConstraint("input_count >= 0 AND baseline_count >= 0 AND result_count >= 0", name="ck_event_replay_runs_counts"),
        CheckConstraint("start_at < end_at", name="ck_event_replay_runs_window"),
        Index("ix_event_replay_runs_status_created", "status", "created_at"),
    )


class EventReplayInput(Base):
    """Frozen minimal report fields required to reproduce one replay input."""
    __tablename__ = "event_replay_inputs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("event_replay_runs.id", ondelete="CASCADE"), nullable=False
    )
    report_id: Mapped[str] = mapped_column(String(36), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint("run_id", "report_id", name="uq_event_replay_input_report"),
        Index("ix_event_replay_inputs_run_id", "run_id"),
    )


class EventReplayBaseline(Base):
    """Frozen event-hypothesis membership used as the replay comparison baseline."""
    __tablename__ = "event_replay_baselines"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("event_replay_runs.id", ondelete="CASCADE"), nullable=False
    )
    hypothesis_id: Mapped[str] = mapped_column(String(36), nullable=False)
    hypothesis_status: Mapped[str] = mapped_column(String(20), nullable=False)
    scoring_version: Mapped[str] = mapped_column(String(40), nullable=False)
    report_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    baseline_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint("run_id", "hypothesis_id", name="uq_event_replay_baseline_hypothesis"),
        Index("ix_event_replay_baselines_run_id", "run_id"),
    )


class EventReplayResult(Base):
    """One candidate cluster produced by a replay; never an operational incident."""
    __tablename__ = "event_replay_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("event_replay_runs.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_key: Mapped[str] = mapped_column(String(64), nullable=False)
    report_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    disaster_type: Mapped[str] = mapped_column(String(32), nullable=False)
    centroid_latitude: Mapped[float] = mapped_column(Float, nullable=False)
    centroid_longitude: Mapped[float] = mapped_column(Float, nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    report_count: Mapped[int] = mapped_column(Integer, nullable=False)
    distinct_source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    match_score: Mapped[float] = mapped_column(Float, nullable=False)
    minimum_pair_score: Mapped[float] = mapped_column(Float, nullable=False)
    comparison_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    # Preserve the matched identity even if the live hypothesis is later removed.
    matched_hypothesis_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    overlap_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    explanation: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        UniqueConstraint("run_id", "candidate_key", name="uq_event_replay_result_candidate"),
        UniqueConstraint("run_id", "ordinal", name="uq_event_replay_result_ordinal"),
        CheckConstraint("report_count >= 2 AND distinct_source_count >= 2", name="ck_event_replay_result_counts"),
        CheckConstraint("match_score >= 0 AND match_score <= 1", name="ck_event_replay_result_score"),
        CheckConstraint("overlap_score >= 0 AND overlap_score <= 1", name="ck_event_replay_result_overlap"),
        CheckConstraint(
            "comparison_kind IN ('new_candidate','unchanged','expanded','contracted','diverged')",
            name="ck_event_replay_result_comparison",
        ),
        Index("ix_event_replay_results_run_ordinal", "run_id", "ordinal"),
    )
