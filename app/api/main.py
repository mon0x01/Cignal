from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from redis.exceptions import RedisError
from sqlalchemy import and_, case, func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.api.dependencies import get_current_user, require_roles
from app.api.incident_operations import router as incident_operations_router
from app.api.response_plans import router as response_plans_router
from app.api.schemas import (
    AcquisitionBackfillRequest,
    EventDiscoveryRequest,
    EventHypothesisAssociationReview,
    EventHypothesisReview,
    EventPairLabelCreate,
    EventPairLabelReview,
    IncidentActivityCreate,
    IncidentUpdate,
    LoginRequest,
    PairCandidateMiningRequest,
    PushSubscriptionRequest,
    ReportCreate,
    ReportFollowup,
    ResearchDatasetCreate,
    ResearchDatasetVersionCreate,
    ReviewRequest,
    SetupAdminRequest,
    UserCreate,
    UserUpdate,
)
from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    decrypt_mfa_secret,
    encrypt_mfa_secret,
    hash_password,
    hash_token,
    new_totp_secret,
    provisioning_uri,
    verify_password,
    verify_totp,
)
from app.db.models import (
    AcquisitionBackfillRun,
    AcquisitionObservation,
    AuditLog,
    BackgroundJobOutbox,
    EventDiscoveryRun,
    EventHypothesis,
    EventHypothesisReport,
    EventHypothesisSnapshot,
    EventPairCandidate,
    EventPairLabel,
    Evidence,
    Incident,
    IncidentActivity,
    IncidentResponsePlan,
    IntegrationRun,
    ModelEvaluationRun,
    Notification,
    PairCandidateMiningRun,
    PushSubscription,
    RefreshToken,
    Report,
    ReportIncident,
    ResearchDataset,
    ResearchDatasetVersion,
    ResearchDatasetVersionItem,
    SourceReliabilityProfile,
    User,
    utcnow,
)
from app.db.session import SessionLocal, get_db, initialize_database
from app.integrations.open_meteo import OpenMeteoWeather
from app.integrations.registry import connector_statuses
from app.integrations.source_catalog import build_report_source_metadata
from app.services.acquisition_schedule import source_schedule_status
from app.services.analysis_jobs import advance_report_analysis_revision
from app.services.audit import record_audit
from app.services.demo import seed_demo_data
from app.services.events import enqueue_job, publish_event, redis_client
from app.services.incident_review import review_report as apply_report_review
from app.services.integration_runs import connector_health, integration_run_dict
from app.services.job_outbox import (
    enqueue_event_discovery,
    enqueue_report_analysis,
    summarize_outbox,
)
from app.services.research_datasets import (
    ResearchDatasetError,
    pair_fingerprint,
    prepare_dataset_snapshot,
    report_content_fingerprint,
    report_source_metadata_fingerprint,
    serialize_dataset_items,
)
from app.services.source_reliability import (
    rebuild_source_reliability_profiles,
    source_reliability_dict,
)
from app.services.websocket_auth import event_is_visible_to_user
from app.storage.factory import get_storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("cignal.api")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.app_env == "production":
        weak_markers = ("replace-with", "development-only", "change-me", "example")
        secrets_to_check = (settings.jwt_secret_key, settings.app_secret_key)
        if any(len(value) < 32 or any(marker in value.lower() for marker in weak_markers) for value in secrets_to_check):
            raise RuntimeError("Production requires unique, randomly generated APP_SECRET_KEY and JWT_SECRET_KEY values")
        if settings.jwt_secret_key == settings.app_secret_key:
            raise RuntimeError("APP_SECRET_KEY and JWT_SECRET_KEY must be different")
        if settings.demo_mode:
            raise RuntimeError("DEMO_MODE must be false in production")
        if not settings.mfa_required or not settings.rate_limit_fail_closed:
            raise RuntimeError("Production requires MFA_REQUIRED=true and RATE_LIMIT_FAIL_CLOSED=true")
    initialize_database()
    with SessionLocal() as db:
        seed_demo_data(db)
    settings.evidence_dir.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="Cignal Disaster Intelligence API",
    version="0.1.0",
    description="Human-in-the-loop disaster intelligence and decision support. AI outputs are triage hints, not emergency declarations.",
    docs_url="/api/docs" if settings.app_env != "production" else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if settings.app_env != "production" else None,
    lifespan=lifespan,
)
app.include_router(incident_operations_router)
app.include_router(response_plans_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(self)"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/auth") else "private, max-age=0"
    return response




def _rate_limit(key: str, limit: int, seconds: int) -> None:
    client = redis_client()
    try:
        value = client.incr(key)
        if value == 1:
            client.expire(key, seconds)
        if value > limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
    except RedisError as exc:
        logger.error("Rate limiter unavailable; refusing protected request")
        if settings.rate_limit_fail_closed:
            raise HTTPException(status_code=503, detail="Security rate limiter is temporarily unavailable") from exc
    finally:
        client.close()


def _token_for_user(user: User) -> dict:
    return {"access_token": create_access_token(user.id, user.role), "token_type": "bearer", "expires_in": settings.jwt_access_minutes * 60}


def _issue_refresh(db: Session, user: User) -> str:
    token = create_refresh_token()
    db.add(RefreshToken(
        id=str(uuid4()), user_id=user.id, token_hash=hash_token(token),
        expires_at=datetime.now(UTC) + timedelta(days=settings.jwt_refresh_days),
    ))
    return token


def _user_dict(user: User) -> dict:
    return {
        "id": user.id, "email": user.email, "display_name": user.display_name,
        "role": user.role, "is_active": user.is_active, "mfa_enabled": user.mfa_enabled,
    }


def _optional_user(authorization: str | None, db: Session) -> User | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        claims = decode_token(authorization.split(" ", 1)[1])
        if claims.get("type") != "access":
            return None
        user = db.get(User, claims["sub"])
        if user and user.is_active and settings.mfa_required and not user.mfa_enabled:
            raise HTTPException(status_code=403, detail="MFA setup is required before submitting while signed in.")
        return user
    except HTTPException:
        raise
    except Exception:
        return None


def _report_out(report: Report) -> dict:
    return {
        "id": report.id, "tracking_code": report.tracking_code, "title": report.title,
        "description": report.description, "is_anonymous": report.submitted_by is None,
        "disaster_type": report.disaster_type,
        "urgency": report.urgency, "status": report.status,
        "latitude": report.latitude, "longitude": report.longitude, "location_text": report.location_text,
        "language": report.language, "source_type": report.source_type, "source_name": report.source_name,
        "source_url": report.source_url, "provenance": report.provenance,
        "source_observed_at": report.source_observed_at.isoformat() if report.source_observed_at else None,
        "source_metadata": report.source_metadata or {},
        "ai_category": report.ai_category, "ai_urgency": report.ai_urgency,
        "ai_confidence": report.ai_confidence, "ai_summary": report.ai_summary,
        "duplicate_of": report.duplicate_of, "created_at": report.created_at.isoformat(),
    }


def _incident_out(incident: Incident) -> dict:
    return {
        "id": incident.id, "incident_code": incident.incident_code, "title": incident.title,
        "summary": incident.summary, "disaster_type": incident.disaster_type,
        "severity": incident.severity, "status": incident.status,
        "latitude": incident.latitude, "longitude": incident.longitude,
        "location_text": incident.location_text, "source_count": incident.source_count,
        "confidence": incident.confidence, "provenance": incident.provenance,
        "verified_at": incident.verified_at.isoformat() if incident.verified_at else None,
        "created_at": incident.created_at.isoformat(),
    }


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    redis_ok = False
    client = redis_client()
    try:
        client.ping()
        redis_ok = True
    except Exception:
        pass
    finally:
        client.close()
    return {"status": "ok", "database": "ok", "redis": "ok" if redis_ok else "degraded", "version": "0.1.0"}


@app.get("/api/setup/status")
def setup_status(db: Session = Depends(get_db)):
    exists = db.query(User.id).first() is not None
    return {"setup_required": not exists, "demo_mode": settings.demo_mode}


@app.post("/api/setup/admin", status_code=201)
def setup_admin(payload: SetupAdminRequest, request: Request, db: Session = Depends(get_db)):
    _rate_limit(f"setup:{request.client.host if request.client else 'unknown'}", 5, 3600)
    if db.query(User.id).first():
        raise HTTPException(status_code=409, detail="Initial setup has already been completed")
    if payload.password.lower() in {"password12345678", "changemeplease123"}:
        raise HTTPException(status_code=400, detail="Choose a non-default password")
    user = User(
        id=str(uuid4()), email=payload.email.lower(), display_name=payload.display_name.strip(),
        password_hash=hash_password(payload.password), role="admin", is_active=True,
    )
    db.add(user)
    db.flush()
    refresh = _issue_refresh(db, user)
    record_audit(db, "setup.admin_created", "user", user.id, user.id, request)
    db.commit()
    result = _token_for_user(user)
    result.update({"refresh_token": refresh, "user": _user_dict(user), "mfa_setup_required": True})
    return result


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    email = payload.email.lower()
    _rate_limit(f"login:{request.client.host if request.client else 'unknown'}:{email}", 8, 300)
    user = db.query(User).filter(func.lower(User.email) == email).first()
    now = datetime.now(UTC)
    if not user or not verify_password(payload.password, user.password_hash):
        if user:
            user.failed_login_count += 1
            if user.failed_login_count >= 5:
                user.locked_until = now + timedelta(minutes=15)
            db.commit()
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.locked_until and user.locked_until.replace(tzinfo=UTC) > now:
        raise HTTPException(status_code=423, detail="Account temporarily locked after repeated failed sign-in attempts")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")
    if user.mfa_enabled:
        code = payload.totp_code or ""
        valid = bool(user.mfa_secret and verify_totp(decrypt_mfa_secret(user.mfa_secret), code))
        if not valid and user.mfa_recovery_hashes:
            code_hash = hash_token(code)
            if code_hash in user.mfa_recovery_hashes:
                user.mfa_recovery_hashes.remove(code_hash)
                valid = True
        if not valid:
            user.failed_login_count += 1
            if user.failed_login_count >= 5:
                user.locked_until = now + timedelta(minutes=15)
            db.commit()
            raise HTTPException(status_code=401, detail="A valid authenticator or recovery code is required")
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    result = _token_for_user(user)
    refresh = _issue_refresh(db, user)
    record_audit(db, "auth.login", "user", user.id, user.id, request)
    db.commit()
    result.update({"refresh_token": refresh, "user": _user_dict(user), "mfa_setup_required": settings.mfa_required and not user.mfa_enabled})
    return result


@app.post("/api/auth/refresh")
def refresh_access(payload: dict, db: Session = Depends(get_db)):
    token = str(payload.get("refresh_token") or "")
    stored = db.query(RefreshToken).filter_by(token_hash=hash_token(token), revoked_at=None).first()
    if not stored or stored.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC):
        raise HTTPException(status_code=401, detail="Refresh token is invalid or expired")
    user = db.get(User, stored.user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Account unavailable")
    stored.revoked_at = utcnow()
    replacement = _issue_refresh(db, user)
    result = _token_for_user(user)
    result["refresh_token"] = replacement
    db.commit()
    return result


@app.post("/api/auth/logout")
def logout(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    token = str(payload.get("refresh_token") or "")
    stored = db.query(RefreshToken).filter_by(token_hash=hash_token(token), user_id=user.id, revoked_at=None).first()
    if stored:
        stored.revoked_at = utcnow()
        db.commit()
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: User = Depends(get_current_user)):
    return _user_dict(user)


@app.post("/api/auth/mfa/start")
def mfa_start(payload: dict | None = Body(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.mfa_enabled and not (user.mfa_secret and verify_totp(decrypt_mfa_secret(user.mfa_secret), str((payload or {}).get("current_code") or ""))):
        raise HTTPException(status_code=401, detail="Current authenticator code is required to replace MFA")
    secret = new_totp_secret()
    user.pending_mfa_secret = encrypt_mfa_secret(secret)
    db.commit()
    return {"secret": secret, "provisioning_uri": provisioning_uri(secret, user.email), "issuer": "Cignal"}


@app.post("/api/auth/mfa/verify")
def mfa_verify(payload: dict, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _rate_limit(f"mfa-verify:{user.id}:{request.client.host if request.client else 'unknown'}", 8, 300)
    if user.locked_until and user.locked_until.replace(tzinfo=UTC) > datetime.now(UTC):
        raise HTTPException(status_code=423, detail="Account temporarily locked after repeated failed MFA attempts")
    secret = decrypt_mfa_secret(user.pending_mfa_secret) if user.pending_mfa_secret else None
    if not secret or not verify_totp(secret, str(payload.get("code") or "")):
        user.failed_login_count += 1
        if user.failed_login_count >= 5:
            user.locked_until = datetime.now(UTC) + timedelta(minutes=15)
        db.commit()
        raise HTTPException(status_code=400, detail="Authenticator code is invalid")
    recovery_codes = [secrets.token_urlsafe(8) for _ in range(8)]
    user.mfa_secret = encrypt_mfa_secret(secret)
    user.pending_mfa_secret = None
    user.mfa_enabled = True
    user.failed_login_count = 0
    user.locked_until = None
    user.mfa_recovery_hashes = [hash_token(code) for code in recovery_codes]
    record_audit(db, "auth.mfa_enabled", "user", user.id, user.id)
    db.commit()
    publish_event("security.mfa_enabled", {"user_id": user.id})
    return {"ok": True, "recovery_codes": recovery_codes, "message": "Store these recovery codes offline. They are shown only once."}


@app.post("/api/reports", status_code=201)
def create_report(
    payload: ReportCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    ip = request.client.host if request.client else "unknown"
    _rate_limit(f"report:{ip}", 20, 3600)
    user = _optional_user(authorization, db)
    if idempotency_key:
        existing = db.query(Report).filter_by(idempotency_key=idempotency_key[:80]).first()
        if existing:
            if (existing.submitted_by or None) != (user.id if user else None):
                raise HTTPException(status_code=409, detail="Idempotency key is already associated with another submission")
            return _report_out(existing)
    report = Report(
        id=str(uuid4()), tracking_code=f"CIG-R-{secrets.token_hex(12).upper()}",
        submitted_by=user.id if user else None, title=payload.title.strip(), description=payload.description.strip(),
        disaster_type=payload.disaster_type, urgency=payload.urgency, status="submitted",
        latitude=payload.latitude, longitude=payload.longitude, location_text=payload.location_text,
        language=payload.language, source_type="citizen", source_name="Citizen submission",
        provenance="submitted", source_metadata=build_report_source_metadata("citizen"),
        idempotency_key=idempotency_key[:80] if idempotency_key else None,
    )
    db.add(report)
    record_audit(db, "report.submitted", "report", report.id, user.id if user else None, request)
    enqueue_report_analysis(db, report.id, report.analysis_revision or 1)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            existing = db.query(Report).filter_by(idempotency_key=idempotency_key[:80]).first()
            if existing:
                if (existing.submitted_by or None) != (user.id if user else None):
                    raise HTTPException(status_code=409, detail="Idempotency key is already associated with another submission") from None
                return _report_out(existing)
        raise HTTPException(status_code=409, detail="Duplicate report source record") from None
    publish_event("report.submitted", {"report_id": report.id, "tracking_code": report.tracking_code})
    return _report_out(report)


@app.get("/api/reports")
def list_reports(
    status_filter: str | None = None,
    limit: int = 100,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Report)
    if user.role == "citizen":
        query = query.filter(Report.submitted_by == user.id)
    elif user.role == "responder":
        raise HTTPException(status_code=403, detail="Responders use the verified incident view")
    if status_filter:
        query = query.filter(Report.status == status_filter)
    return [_report_out(row) for row in query.order_by(Report.created_at.desc()).limit(min(max(limit, 1), 250)).all()]


@app.get("/api/reports/search")
def search_reports(
    q: str | None = Query(default=None, max_length=120),
    status_filter: str | None = Query(default=None, max_length=24),
    source_type: str | None = Query(default=None, max_length=24),
    disaster_type: str | None = Query(default=None, max_length=32),
    date_from: date | None = None,
    date_to: date | None = None,
    sort: str = "newest",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Search reports with server-side filtering and stable pagination.

    Admins and analysts can search all submissions, including anonymous reports.
    Citizens can search only their own reports. Responders use verified incidents.
    """
    query = db.query(Report)
    if user.role == "citizen":
        query = query.filter(Report.submitted_by == user.id)
    elif user.role == "responder":
        raise HTTPException(status_code=403, detail="Responders use the verified incident view")

    if q and q.strip():
        term = q.strip()[:120]
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        query = query.filter(or_(
            Report.tracking_code.ilike(pattern, escape="\\"),
            Report.title.ilike(pattern, escape="\\"),
            Report.description.ilike(pattern, escape="\\"),
            Report.location_text.ilike(pattern, escape="\\"),
            Report.source_name.ilike(pattern, escape="\\"),
            Report.source_record_id.ilike(pattern, escape="\\"),
        ))
    if status_filter:
        allowed_statuses = {"submitted", "analyzed", "needs_info", "verified", "rejected"}
        if status_filter not in allowed_statuses:
            raise HTTPException(status_code=422, detail="Unsupported report status filter")
        query = query.filter(Report.status == status_filter)
    if source_type:
        query = query.filter(Report.source_type == source_type)
    if disaster_type:
        query = query.filter(Report.disaster_type == disaster_type)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    if date_from:
        start = datetime.combine(date_from, time.min, tzinfo=UTC)
        query = query.filter(Report.created_at >= start)
    if date_to:
        end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=UTC)
        query = query.filter(Report.created_at < end)

    if sort not in {"newest", "oldest", "urgency"}:
        raise HTTPException(status_code=422, detail="sort must be newest, oldest, or urgency")

    total = query.order_by(None).count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(page, total_pages)

    if sort == "oldest":
        ordering = (Report.created_at.asc(), Report.id.asc())
    elif sort == "urgency":
        urgency_rank = case(
            (Report.urgency == "critical", 5),
            (Report.urgency == "high", 4),
            (Report.urgency == "moderate", 3),
            (Report.urgency == "low", 2),
            else_=1,
        )
        ordering = (urgency_rank.desc(), Report.created_at.desc(), Report.id.desc())
    else:
        ordering = (Report.created_at.desc(), Report.id.desc())

    rows = (
        query.order_by(*ordering)
        .offset((current_page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [_report_out(row) for row in rows],
        "total": total,
        "page": current_page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_previous": current_page > 1,
        "has_next": current_page < total_pages,
    }


@app.get("/api/reports/{report_id}")
def get_report(report_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    report = db.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if user.role == "citizen" and report.submitted_by != user.id:
        raise HTTPException(status_code=403, detail="You can only view your own reports")
    if user.role == "responder":
        raise HTTPException(status_code=403, detail="Responders use verified incidents")
    evidence = db.query(Evidence).filter_by(report_id=report.id).all()
    result = _report_out(report)
    result["evidence"] = [{"id": item.id, "filename": item.original_filename, "mime_type": item.mime_type, "size_bytes": item.size_bytes} for item in evidence]
    if user.role in {"admin", "analyst"}:
        result["ai_analysis_available"] = report.ai_analysis is not None
    return result


@app.get("/api/reports/{report_id}/analysis")
def get_report_analysis(
    report_id: str,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    report = db.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.ai_analysis is None:
        raise HTTPException(status_code=404, detail="AI analysis is not available for this report yet")
    return {"report_id": report.id, "analysis": report.ai_analysis}


@app.post("/api/reports/{report_id}/followup")
def add_report_followup(
    report_id: str,
    payload: ReportFollowup,
    request: Request,
    user: User = Depends(require_roles("citizen")),
    db: Session = Depends(get_db),
):
    report = db.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.submitted_by != user.id:
        raise HTTPException(status_code=403, detail="You can only add information to your own report")
    if report.status != "needs_info":
        raise HTTPException(status_code=409, detail="This report is not awaiting additional information")
    ip = request.client.host if request.client else "unknown"
    _rate_limit(f"followup:{user.id}:{ip}", 10, 3600)
    addition = f"\n\nCitizen follow-up ({utcnow().isoformat()}):\n{payload.message.strip()}"
    if len(report.description) + len(addition) > 10000:
        raise HTTPException(status_code=413, detail="The report has reached its description limit")
    report.description += addition
    report.status = "submitted"
    report.updated_at = utcnow()
    analysis_revision = advance_report_analysis_revision(db, report)
    record_audit(db, "report.followup_added", "report", report.id, user.id, request)
    enqueue_report_analysis(db, report.id, analysis_revision)
    db.commit()
    publish_event("report.followup_added", {"report_id": report.id})
    return _report_out(report)


@app.post("/api/reports/{report_id}/evidence", status_code=201)
async def upload_evidence(
    report_id: str,
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    report = db.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if user.role == "citizen" and report.submitted_by != user.id:
        raise HTTPException(status_code=403, detail="You can only attach evidence to your own report")
    if user.role == "responder":
        raise HTTPException(status_code=403, detail="Responders cannot modify report evidence")
    ip = request.client.host if request.client else "unknown"
    _rate_limit(f"evidence:{user.id}:{ip}", 30, 3600)
    allowed = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=415, detail="Only JPEG, PNG, WebP and PDF evidence is accepted")
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Evidence exceeds the configured upload size limit")
    if not content:
        raise HTTPException(status_code=400, detail="Empty evidence file")
    image_metadata = {}
    if file.content_type.startswith("image/"):
        from io import BytesIO

        from PIL import Image, ImageOps

        from app.intelligence.vision import ImageEvidenceError, extract_image_metadata
        try:
            image_metadata = extract_image_metadata(content)
            image = ImageOps.exif_transpose(Image.open(BytesIO(content))).convert("RGB")
            output = BytesIO()
            output_format = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[file.content_type]
            save_options = {"quality": 88, "optimize": True} if output_format in {"JPEG", "WEBP"} else {"optimize": True}
            image.save(output, format=output_format, **save_options)
            content = output.getvalue()
            image_metadata.update({"width": image.width, "height": image.height, "format": output_format, "exif_stripped": True})
        except (ImageEvidenceError, OSError, ValueError):
            raise HTTPException(status_code=400, detail="Image content failed validation") from None
    elif not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="PDF signature validation failed")
    digest = hashlib.sha256(content).hexdigest()
    safe_suffix = "." + ("jpg" if file.content_type == "image/jpeg" else file.content_type.split("/")[-1])
    key = f"{report_id}/{uuid4().hex}{safe_suffix}"
    try:
        stored_key = get_storage().save(key, content, file.content_type)
    except Exception as exc:
        logger.exception("Evidence storage failed")
        raise HTTPException(status_code=503, detail="Evidence storage is unavailable") from exc
    original_filename = Path(file.filename or "evidence").name.replace('"', "_").replace("\r", "_").replace("\n", "_")[:255]
    record = Evidence(
        id=str(uuid4()), report_id=report_id,
        original_filename=original_filename, stored_key=stored_key,
        mime_type=file.content_type, sha256=digest, size_bytes=len(content), image_metadata=image_metadata,
    )
    db.add(record)
    # Any evidence type changes the report's multimodal context and creates a new generation.
    analysis_revision = advance_report_analysis_revision(db, report)
    record_audit(db, "evidence.uploaded", "evidence", record.id, user.id)
    enqueue_report_analysis(db, report.id, analysis_revision)
    db.commit()
    return {"id": record.id, "filename": record.original_filename, "mime_type": record.mime_type, "size_bytes": record.size_bytes, "sha256": digest}


@app.get("/api/evidence/{evidence_id}")
def download_evidence(evidence_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    evidence = db.get(Evidence, evidence_id)
    if not evidence:
        raise HTTPException(status_code=404, detail="Evidence not found")
    report = db.get(Report, evidence.report_id)
    if user.role == "citizen" and report.submitted_by != user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this evidence")
    if user.role == "responder":
        raise HTTPException(status_code=403, detail="Responders cannot access unreviewed evidence")
    content = get_storage().read(evidence.stored_key)
    safe_filename = Path(evidence.original_filename).name.replace('"', "_").replace("\r", "_").replace("\n", "_")[:255]
    return Response(content=content, media_type=evidence.mime_type, headers={"Content-Disposition": f'attachment; filename="{safe_filename}"', "X-Content-Type-Options": "nosniff"})


@app.post("/api/admin/evidence-integrity-audits", status_code=202)
def start_evidence_integrity_audit(
    request: Request,
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    """Queue a full evidence integrity scan. Only administrators can trigger it."""
    _rate_limit(f"evidence-integrity-audit:{user.id}", 2, 3600)
    run_id = str(uuid4())
    job = enqueue_job("app.worker.run_evidence_integrity_audit", run_id)
    if job is None:
        raise HTTPException(status_code=503, detail="Evidence integrity audit queue is unavailable")
    record_audit(
        db,
        "evidence.integrity_audit_queued",
        "evidence_integrity_audit",
        run_id,
        user.id,
        request,
        {"run_id": run_id, "job_id": str(job.id)},
    )
    db.commit()
    return {"run_id": run_id, "job_id": str(job.id), "status": "queued"}


@app.get("/api/admin/evidence-integrity-audits/{run_id}")
def get_evidence_integrity_audit(
    run_id: str,
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    """Return queued, completed, or failed state for a known integrity scan."""
    try:
        normalized_run_id = str(UUID(run_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid audit run ID") from exc

    rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "evidence_integrity_audit",
            AuditLog.resource_id == normalized_run_id,
            AuditLog.action.in_([
                "evidence.integrity_audit_queued",
                "evidence.integrity_audit_completed",
                "evidence.integrity_audit_failed",
            ]),
        )
        .order_by(AuditLog.created_at.asc())
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Evidence integrity audit not found")

    result = {"run_id": normalized_run_id, "status": "queued", "job_id": None, "summary": None, "error_type": None}
    for row in rows:
        if row.action == "evidence.integrity_audit_queued":
            result["job_id"] = row.details.get("job_id")
        elif row.action == "evidence.integrity_audit_completed":
            result["status"] = "completed"
            result["summary"] = row.details
        elif row.action == "evidence.integrity_audit_failed":
            result["status"] = "failed"
            result["error_type"] = row.details.get("error_type")
    return result


@app.post("/api/reports/{report_id}/review")
def review_report(
    report_id: str,
    payload: ReviewRequest,
    request: Request,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    report, incident = apply_report_review(
        db,
        report_id,
        decision=payload.decision,
        reviewer=user,
        request=request,
        incident_id=payload.incident_id,
        incident_title=payload.incident_title,
        note=payload.note,
    )
    return {
        "ok": True,
        "report": _report_out(report),
        "incident": _incident_out(incident) if incident else None,
    }


@app.get("/api/incidents")
def list_incidents(
    status_filter: str | None = None,
    severity: str | None = None,
    limit: int = 100,
    user: User = Depends(require_roles("admin", "analyst", "responder")),
    db: Session = Depends(get_db),
):
    query = db.query(Incident)
    if status_filter:
        query = query.filter(Incident.status == status_filter)
    if severity:
        query = query.filter(Incident.severity == severity)
    return [_incident_out(row) for row in query.order_by(Incident.updated_at.desc()).limit(min(max(limit, 1), 250)).all()]


@app.get("/api/incidents/nearby")
def nearby_incidents(latitude: float, longitude: float, radius_km: float = 25, user: User = Depends(require_roles("admin", "analyst", "responder")), db: Session = Depends(get_db)):
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and 0.1 <= radius_km <= 500):
        raise HTTPException(status_code=422, detail="Invalid center or radius")
    if db.bind and db.bind.dialect.name == "postgresql":
        rows = db.query(Incident).filter(text(
            "location_geom IS NOT NULL AND ST_DWithin(location_geom::geography, "
            "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :radius_m)"
        )).params(lon=longitude, lat=latitude, radius_m=radius_km * 1000).limit(250).all()
    else:
        from app.intelligence.analyzer import haversine_km
        rows = [row for row in db.query(Incident).filter(Incident.latitude.is_not(None)).all()
                if haversine_km(latitude, longitude, row.latitude, row.longitude) <= radius_km]
    return [_incident_out(row) for row in rows]


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str, user: User = Depends(require_roles("admin", "analyst", "responder")), db: Session = Depends(get_db)):
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    links = db.query(ReportIncident).filter_by(incident_id=incident.id).order_by(ReportIncident.linked_at.asc()).all()
    report_links = []
    for link in links:
        report = db.get(Report, link.report_id)
        if not report:
            continue
        report_links.append({
            "relationship_type": link.relationship_type,
            "linked_at": link.linked_at.isoformat(),
            "report": _report_out(report),
            "evidence_count": db.query(func.count(Evidence.id)).filter(Evidence.report_id == report.id).scalar() or 0,
        })
    return {
        **_incident_out(incident),
        # Keep the original reports field for clients already using the API.
        "reports": [item["report"] for item in report_links],
        "report_links": report_links,
    }


def _incident_activity_out(activity: IncidentActivity) -> dict:
    return {
        "id": activity.id,
        "incident_id": activity.incident_id,
        "actor_display_name": activity.actor_display_name,
        "actor_role": activity.actor_role,
        "activity_type": activity.activity_type,
        "message": activity.message,
        "location_text": activity.location_text,
        "latitude": activity.latitude,
        "longitude": activity.longitude,
        "created_at": activity.created_at.isoformat(),
    }


@app.get("/api/incidents/{incident_id}/activity")
def list_incident_activity(
    incident_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    before_created_at: datetime | None = Query(default=None),
    before_id: str | None = Query(default=None, min_length=36, max_length=36),
    user: User = Depends(require_roles("admin", "analyst", "responder")),
    db: Session = Depends(get_db),
):
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    if (before_created_at is None) != (before_id is None):
        raise HTTPException(status_code=422, detail="Both before_created_at and before_id are required")
    if before_created_at is not None and offset:
        raise HTTPException(status_code=422, detail="Cursor pagination cannot be combined with offset")

    query = db.query(IncidentActivity).filter(IncidentActivity.incident_id == incident_id)
    total = query.count()
    if before_created_at is not None and before_id is not None:
        query = query.filter(or_(
            IncidentActivity.created_at < before_created_at,
            and_(IncidentActivity.created_at == before_created_at, IncidentActivity.id < before_id),
        ))
    fetched = query.order_by(
        IncidentActivity.created_at.desc(), IncidentActivity.id.desc()
    ).offset(offset).limit(limit + 1).all()
    has_more = len(fetched) > limit
    rows = fetched[:limit]
    next_cursor = None
    if has_more and rows:
        next_cursor = {
            "before_created_at": rows[-1].created_at.isoformat(),
            "before_id": rows[-1].id,
        }
    return {
        "items": [_incident_activity_out(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


@app.post("/api/incidents/{incident_id}/activity", status_code=201)
def add_incident_activity(
    incident_id: str,
    payload: IncidentActivityCreate,
    request: Request,
    user: User = Depends(require_roles("admin", "analyst", "responder")),
    db: Session = Depends(get_db),
):
    _rate_limit(f"incident-activity:{user.id}", 30, 60)
    incident = db.query(Incident).filter(Incident.id == incident_id).with_for_update().first()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    if incident.status == "closed":
        raise HTTPException(status_code=409, detail="Closed incidents cannot accept new activity")

    activity = IncidentActivity(
        id=str(uuid4()),
        incident_id=incident.id,
        actor_user_id=user.id,
        actor_display_name=user.display_name,
        actor_role=user.role,
        activity_type=payload.activity_type,
        message=payload.message,
        location_text=payload.location_text,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )
    db.add(activity)
    incident.updated_at = utcnow()
    record_audit(
        db,
        "incident.activity_added",
        "incident",
        incident.id,
        user.id,
        request,
        {
            "activity_id": activity.id,
            "activity_type": activity.activity_type,
            "has_location": bool(activity.location_text or activity.latitude is not None),
        },
    )
    db.commit()
    db.refresh(activity)
    publish_event(
        "incident.activity_added",
        {
            "incident_id": incident.id,
            "activity_id": activity.id,
            "activity_type": activity.activity_type,
        },
    )
    return _incident_activity_out(activity)


@app.patch("/api/incidents/{incident_id}")
def update_incident(incident_id: str, payload: IncidentUpdate, request: Request, user: User = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db)):
    incident = db.query(Incident).filter(Incident.id == incident_id).with_for_update().first()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    allowed_transitions = {
        "verified": {"monitoring", "resolved", "closed"},
        "monitoring": {"resolved", "closed"},
        "resolved": {"closed"},
        "closed": set(),
    }
    changes = {}
    if payload.status in {"resolved", "closed"} and payload.status != incident.status:
        open_plan = db.query(IncidentResponsePlan).filter(
            IncidentResponsePlan.incident_id == incident.id,
            IncidentResponsePlan.status.in_(("active", "needs_review")),
        ).first()
        if open_plan is not None:
            raise HTTPException(
                status_code=409,
                detail={"message": "Resolve or cancel the active response plan before changing incident lifecycle.",
                        "response_plan_id": open_plan.id, "response_plan_status": open_plan.status},
            )
    if payload.status is not None and payload.status != incident.status:
        if payload.status not in allowed_transitions.get(incident.status, set()):
            raise HTTPException(
                status_code=409,
                detail=f"Invalid incident transition: {incident.status} -> {payload.status}",
            )
        changes["status"] = {"from": incident.status, "to": payload.status}
        incident.status = payload.status
        if payload.status in {"resolved", "closed"} and incident.resolved_at is None:
            incident.resolved_at = utcnow()

    if incident.status == "closed" and any(value is not None for value in (payload.severity, payload.summary)):
        raise HTTPException(status_code=409, detail="Closed incidents are immutable")

    for field in ("severity", "summary"):
        value = getattr(payload, field)
        if value is not None and value != getattr(incident, field):
            changes[field] = {"from": getattr(incident, field), "to": value}
            setattr(incident, field, value)

    if not changes:
        return _incident_out(incident)

    incident.updated_at = utcnow()
    record_audit(db, "incident.updated", "incident", incident.id, user.id, request, changes)
    db.commit()
    publish_event("incident.updated", {"incident_id": incident.id, "changes": changes})
    return _incident_out(incident)


@app.get("/api/dashboard/summary")
def dashboard_summary(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role == "citizen":
        reports = db.query(Report).filter(Report.submitted_by == user.id)
        return {"my_reports": reports.count(), "pending": reports.filter(Report.status.in_(["submitted", "analyzed", "needs_info"])).count(), "verified": reports.filter_by(status="verified").count()}
    return {
        "active_incidents": db.query(Incident).filter(Incident.status.in_(["verified", "monitoring"])).count(),
        "critical_incidents": db.query(Incident).filter(Incident.severity == "critical", Incident.status.in_(["verified", "monitoring"])).count(),
        "pending_reports": db.query(Report).filter(Report.status.in_(["submitted", "analyzed", "needs_info"])).count(),
        "reports_24h": db.query(Report).filter(Report.created_at >= datetime.now(UTC) - timedelta(hours=24)).count(),
        "verified_incidents": db.query(Incident).filter(Incident.status == "verified").count(),
        "sources_live": sum(1 for item in connector_statuses() if item["configured"]),
    }


@app.post("/api/import/historical/csv")
async def import_historical_csv(
    file: UploadFile = File(...),
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    """Import a CSV with title, description and optional coordinates/category/date columns."""
    filename = Path(file.filename or "historical.csv").name
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=415, detail="Only CSV files are accepted")
    raw = await file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="CSV exceeds 5 MB")
    try:
        text_content = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text_content))
        headers = {str(item).strip().lower() for item in (reader.fieldnames or [])}
        if not {"title", "description"}.issubset(headers):
            raise ValueError("CSV needs title and description columns")
        inserted = 0
        skipped = 0
        imported_ids = []
        for line_number, row in enumerate(reader, start=2):
            if inserted >= 5000:
                break
            normalized = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k}
            title = normalized.get("title", "")[:180]
            description = normalized.get("description", "")[:10000]
            if len(title) < 5 or len(description) < 20:
                skipped += 1
                continue
            raw_key = hashlib.sha256(f"{filename}:{line_number}:{title}:{description}".encode()).hexdigest()
            if db.query(Report.id).filter_by(source_type="historical", source_record_id=raw_key).first():
                skipped += 1
                continue
            try:
                lat = float(normalized["latitude"]) if normalized.get("latitude") else None
                lon = float(normalized["longitude"]) if normalized.get("longitude") else None
                if lat is not None and not -90 <= lat <= 90:
                    raise ValueError("Latitude is outside the valid range")
                if lon is not None and not -180 <= lon <= 180:
                    raise ValueError("Longitude is outside the valid range")
            except ValueError:
                skipped += 1
                continue
            report = Report(
                id=str(uuid4()), tracking_code=f"CIG-H-{secrets.token_hex(4).upper()}",
                title=title, description=description, disaster_type=normalized.get("disaster_type", "unknown")[:32].lower(),
                urgency=normalized.get("urgency", "unknown")[:16].lower(), status="submitted",
                latitude=lat, longitude=lon, location_text=normalized.get("location_text", "")[:220] or None,
                language=normalized.get("language", "en")[:8], source_type="historical", source_name=filename[:100],
                source_record_id=raw_key, source_url=normalized.get("source_url", "")[:700] or None, provenance="historical",
            )
            db.add(report)
            db.flush()
            imported_ids.append(report.id)
            inserted += 1
        record_audit(db, "historical.csv_imported", "dataset", None, user.id, None, {"filename": filename, "inserted": inserted, "skipped": skipped})
        for imported_id in imported_ids:
            enqueue_report_analysis(db, imported_id, 1)
        db.commit()
        return {"ok": True, "inserted": inserted, "skipped": skipped, "limit": 5000, "provenance": "historical"}
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded") from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await file.close()


@app.get("/api/admin/background-jobs/outbox")
def background_job_outbox_status(
    limit: int = Query(default=25, ge=1, le=100),
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    """Expose bounded outbox health metadata to administrators."""
    return summarize_outbox(db, recent_limit=limit)


@app.post("/api/admin/background-jobs/outbox/{job_id}/retry")
def retry_dead_background_job(
    job_id: str,
    request: Request,
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    """Release a dead-lettered job for another delivery attempt."""
    row = db.query(BackgroundJobOutbox).filter_by(job_id=job_id).with_for_update().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Background job not found")
    if row.status != "dead":
        raise HTTPException(status_code=409, detail="Only dead-lettered jobs can be retried")
    row.status = "pending"
    row.attempts = 0
    row.available_at = utcnow()
    row.lease_token = None
    row.lease_expires_at = None
    row.last_error_code = None
    row.updated_at = utcnow()
    record_audit(
        db, "background_job_outbox.retried", "background_job_outbox", row.id,
        user.id, request, {"job_id": row.job_id},
    )
    db.commit()
    return {"ok": True, "job_id": row.job_id, "status": row.status}


@app.get("/api/analytics/summary")
def analytics_summary(user: User = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db)):
    by_type = db.query(Incident.disaster_type, func.count(Incident.id)).group_by(Incident.disaster_type).all()
    by_severity = db.query(Incident.severity, func.count(Incident.id)).group_by(Incident.severity).all()
    by_status = db.query(Report.status, func.count(Report.id)).group_by(Report.status).all()
    return {
        "incidents_by_type": [{"name": name, "value": count} for name, count in by_type],
        "incidents_by_severity": [{"name": name, "value": count} for name, count in by_severity],
        "reports_by_status": [{"name": name, "value": count} for name, count in by_status],
        "data_note": "Counts describe records stored in Cignal. They are not a forecast or a measure of total disaster impact.",
    }


@app.get("/api/weather/forecast")
def weather_forecast(
    latitude: float, longitude: float,
    user: User = Depends(require_roles("admin", "analyst", "responder")),
):
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise HTTPException(status_code=422, detail="Coordinates are outside valid ranges")
    try:
        forecast = OpenMeteoWeather().forecast(latitude, longitude)
        return {"ok": True, "provenance": "live", "provider": "Open-Meteo", "attribution": "Open-Meteo; CC BY 4.0; non-commercial usage terms apply", "data": forecast}
    except Exception as exc:
        logger.warning("Weather provider unavailable: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Weather provider is currently unavailable") from exc


@app.get("/api/integrations")
def integrations_status(user: User = Depends(require_roles("admin", "analyst"))):
    return connector_statuses()


@app.get("/api/integrations/health")
def integrations_health(user: User = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db)):
    """Return configured sources together with their latest persisted outcomes."""
    return connector_health(db, connector_statuses())


@app.get("/api/integrations/schedules")
def integration_schedules(
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    """Return durable per-source polling cadence, lease and retry state."""
    return source_schedule_status(db, connector_statuses())


@app.post("/api/integrations/ingest/{source_type}")
def trigger_source_ingestion(
    source_type: str,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    """Queue one configured provider without triggering unrelated feeds."""
    normalized = source_type.strip().lower()
    connector = next((item for item in connector_statuses() if item["source_type"] == normalized), None)
    if connector is None:
        raise HTTPException(status_code=404, detail="Integration source is not registered")
    if not connector.get("configured"):
        raise HTTPException(status_code=409, detail="Integration source is not configured")
    job = enqueue_job("app.worker.ingest_all_sources", "manual", normalized, None)
    record_audit(db, "integrations.source_ingestion_requested", "integration", normalized, user.id)
    db.commit()
    return {
        "queued": bool(job),
        "source_type": normalized,
        "job_id": job.id if job else None,
        "message": "Source ingestion queued" if job else "Queue unavailable; check worker and Redis",
    }


@app.get("/api/integrations/runs")
def integration_runs(
    source_type: str | None = Query(default=None, max_length=24),
    status_filter: str | None = Query(default=None, pattern="^(running|succeeded|partial|failed|empty)$"),
    limit: int = Query(default=25, ge=1, le=100),
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    query = db.query(IntegrationRun)
    if source_type:
        query = query.filter(IntegrationRun.source_type == source_type.strip().lower())
    if status_filter:
        query = query.filter(IntegrationRun.status == status_filter)
    rows = query.order_by(IntegrationRun.started_at.desc(), IntegrationRun.id.desc()).limit(limit).all()
    return [integration_run_dict(row) for row in rows]


@app.post("/api/integrations/ingest")
def trigger_ingestion(user: User = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db)):
    job = enqueue_job("app.worker.ingest_all_sources", "manual")
    record_audit(db, "integrations.ingestion_requested", "integration", None, user.id)
    db.commit()
    return {"queued": bool(job), "job_id": job.id if job else None, "message": "Ingestion queued" if job else "Queue unavailable; check worker and Redis"}



def _event_discovery_out(run: EventDiscoveryRun) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "trigger_kind": run.trigger_kind,
        "parameters": run.parameters,
        "reports_scanned": run.reports_scanned,
        "clusters_found": run.clusters_found,
        "hypotheses_created": run.hypotheses_created,
        "hypotheses_updated": run.hypotheses_updated,
        "clusters_skipped": run.clusters_skipped,
        "result_summary": run.result_summary or {},
        "failure_code": run.failure_code,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "duration_ms": run.duration_ms,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _event_hypothesis_out(hypothesis: EventHypothesis) -> dict:
    return {
        "id": hypothesis.id,
        "event_key": hypothesis.event_key,
        "title": hypothesis.title,
        "summary": hypothesis.summary,
        "disaster_type": hypothesis.disaster_type,
        "status": hypothesis.status,
        "centroid": {
            "latitude": hypothesis.centroid_latitude,
            "longitude": hypothesis.centroid_longitude,
        },
        "location_text": hypothesis.location_text,
        "first_observed_at": hypothesis.first_observed_at.isoformat(),
        "last_observed_at": hypothesis.last_observed_at.isoformat(),
        "report_count": hypothesis.report_count,
        "distinct_source_count": hypothesis.distinct_source_count,
        "match_score": hypothesis.match_score,
        "score_interpretation": "heuristic_match_score_not_probability",
        "scoring_version": hypothesis.scoring_version,
        "explanation": hypothesis.explanation,
        "reviewed_by": hypothesis.reviewed_by,
        "reviewed_at": hypothesis.reviewed_at.isoformat() if hypothesis.reviewed_at else None,
        "created_at": hypothesis.created_at.isoformat(),
        "updated_at": hypothesis.updated_at.isoformat(),
    }


@app.post("/api/intelligence/event-discoveries", status_code=202)
def request_event_discovery(
    payload: EventDiscoveryRequest,
    request: Request,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    """Queue bounded cross-source event discovery; no incident is created or verified."""
    _rate_limit(f"event-discovery:{user.id}", 5, 300)
    parameters = payload.model_dump()
    run = EventDiscoveryRun(
        id=str(uuid4()),
        requested_by=user.id,
        trigger_kind="analyst",
        status="queued",
        parameters=parameters,
        created_at=utcnow(),
    )
    db.add(run)
    # Persist run + dispatch intent atomically. Redis downtime delays delivery but
    # does not turn a valid analyst request into a lost/failed request.
    job = enqueue_event_discovery(db, run.id, parameters)
    record_audit(
        db, "event_discovery.queued", "event_discovery_run", run.id,
        user.id, request, {"job_id": job.job_id, "parameters": parameters, "dispatch": "durable_outbox"},
    )
    db.commit()
    return {**_event_discovery_out(run), "job_id": job.job_id, "dispatch_status": job.status}


@app.get("/api/intelligence/event-discoveries")
def list_event_discoveries(
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(EventDiscoveryRun)
        .order_by(EventDiscoveryRun.created_at.desc(), EventDiscoveryRun.id.desc())
        .limit(limit)
        .all()
    )
    return [_event_discovery_out(row) for row in rows]


@app.get("/api/intelligence/event-discoveries/{run_id}")
def get_event_discovery(
    run_id: str,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_id = str(UUID(run_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid discovery run ID") from exc
    run = db.get(EventDiscoveryRun, normalized_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Event discovery run not found")
    return _event_discovery_out(run)


@app.get("/api/intelligence/event-hypotheses")
def list_event_hypotheses(
    status_filter: str | None = Query(default=None, pattern="^(candidate|investigating|dismissed)$"),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    query = db.query(EventHypothesis)
    if status_filter:
        query = query.filter(EventHypothesis.status == status_filter)
    rows = query.order_by(
        EventHypothesis.last_observed_at.desc(), EventHypothesis.id.desc()
    ).limit(limit).all()
    return [_event_hypothesis_out(row) for row in rows]


@app.get("/api/intelligence/event-hypotheses/{hypothesis_id}")
def get_event_hypothesis(
    hypothesis_id: str,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_id = str(UUID(hypothesis_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid event hypothesis ID") from exc
    hypothesis = db.get(EventHypothesis, normalized_id)
    if hypothesis is None:
        raise HTTPException(status_code=404, detail="Event hypothesis not found")
    rows = (
        db.query(EventHypothesisReport, Report)
        .join(Report, Report.id == EventHypothesisReport.report_id)
        .filter(EventHypothesisReport.hypothesis_id == normalized_id)
        .order_by(Report.source_observed_at.asc().nullslast(), Report.created_at.asc())
        .all()
    )
    result = _event_hypothesis_out(hypothesis)
    result["reports"] = [
        {
            "report_id": association.report_id,
            "tracking_code": report.tracking_code,
            "title": report.title,
            "disaster_type": report.disaster_type,
            "source_type": report.source_type,
            "source_name": report.source_name,
            "source_observed_at": report.source_observed_at.isoformat() if report.source_observed_at else None,
            "source_metadata": report.source_metadata or {},
            "created_at": report.created_at.isoformat(),
            "association_status": association.association_status,
            "relationship_type": association.relationship_type,
            "pair_score": association.pair_score,
            "component_scores": association.component_scores,
            "reasons": association.reasons,
            "reviewed_by": association.reviewed_by,
            "reviewed_at": association.reviewed_at.isoformat() if association.reviewed_at else None,
        }
        for association, report in rows
    ]
    return result


@app.patch("/api/intelligence/event-hypotheses/{hypothesis_id}/review")
def review_event_hypothesis(
    hypothesis_id: str,
    payload: EventHypothesisReview,
    request: Request,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_id = str(UUID(hypothesis_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid event hypothesis ID") from exc
    hypothesis = db.get(EventHypothesis, normalized_id)
    if hypothesis is None:
        raise HTTPException(status_code=404, detail="Event hypothesis not found")
    if hypothesis.status == "dismissed" and payload.status != "dismissed":
        raise HTTPException(status_code=409, detail="Dismissed hypotheses cannot be reopened automatically")
    previous = hypothesis.status
    hypothesis.status = payload.status
    hypothesis.reviewed_by = user.id
    hypothesis.reviewed_at = utcnow()
    from app.intelligence.event_hypotheses import record_hypothesis_review_snapshot

    record_hypothesis_review_snapshot(db, hypothesis, change_kind="analyst_status_reviewed")
    record_audit(
        db, "event_hypothesis.reviewed", "event_hypothesis", hypothesis.id,
        user.id, request,
        {"previous_status": previous, "status": payload.status, "note": payload.note},
    )
    db.commit()
    db.refresh(hypothesis)
    return _event_hypothesis_out(hypothesis)


@app.patch("/api/intelligence/event-hypotheses/{hypothesis_id}/reports/{report_id}")
def review_event_hypothesis_report(
    hypothesis_id: str,
    report_id: str,
    payload: EventHypothesisAssociationReview,
    request: Request,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_hypothesis_id = str(UUID(hypothesis_id))
        normalized_report_id = str(UUID(report_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid hypothesis or report ID") from exc
    association = db.query(EventHypothesisReport).filter_by(
        hypothesis_id=normalized_hypothesis_id, report_id=normalized_report_id
    ).one_or_none()
    if association is None:
        raise HTTPException(status_code=404, detail="Hypothesis/report association not found")
    previous = association.association_status
    association.association_status = "confirmed" if payload.decision == "confirm" else "rejected"
    association.reviewed_by = user.id
    association.reviewed_at = utcnow()
    from app.intelligence.event_hypotheses import refresh_hypothesis_metrics_from_associations

    hypothesis = db.get(EventHypothesis, normalized_hypothesis_id)
    refresh_hypothesis_metrics_from_associations(db, hypothesis)
    from app.intelligence.event_hypotheses import record_hypothesis_review_snapshot

    record_hypothesis_review_snapshot(
        db, hypothesis, change_kind="analyst_association_reviewed"
    )
    reliability_refresh = rebuild_source_reliability_profiles(db)
    record_audit(
        db, "event_hypothesis.report_association_reviewed", "event_hypothesis",
        normalized_hypothesis_id, user.id, request,
        {
            "report_id": normalized_report_id,
            "previous_status": previous,
            "association_status": association.association_status,
            "source_profiles_refreshed": reliability_refresh["profiles_refreshed"],
            "note": payload.note,
        },
    )
    db.commit()
    return {
        "hypothesis_id": normalized_hypothesis_id,
        "report_id": normalized_report_id,
        "association_status": association.association_status,
        "source_profiles_refreshed": reliability_refresh["profiles_refreshed"],
        "reviewed_by": user.id,
        "reviewed_at": association.reviewed_at.isoformat(),
    }


@app.get("/api/intelligence/event-hypotheses/{hypothesis_id}/timeline")
def get_event_hypothesis_timeline(
    hypothesis_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_id = str(UUID(hypothesis_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid event hypothesis ID") from exc
    if db.get(EventHypothesis, normalized_id) is None:
        raise HTTPException(status_code=404, detail="Event hypothesis not found")
    rows = (
        db.query(EventHypothesisSnapshot)
        .filter(EventHypothesisSnapshot.hypothesis_id == normalized_id)
        .order_by(EventHypothesisSnapshot.created_at.desc(), EventHypothesisSnapshot.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": row.id,
            "change_kind": row.change_kind,
            "report_count": row.report_count,
            "distinct_source_count": row.distinct_source_count,
            "match_score": row.match_score,
            "score_interpretation": "heuristic_match_score_not_probability",
            "centroid": {
                "latitude": row.centroid_latitude,
                "longitude": row.centroid_longitude,
            },
            "first_observed_at": row.first_observed_at.isoformat(),
            "last_observed_at": row.last_observed_at.isoformat(),
            "snapshot_data": row.snapshot_data,
            "discovery_run_id": row.discovery_run_id,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@app.get("/api/intelligence/semantic-status")
def get_event_semantic_status(
    user: User = Depends(require_roles("admin", "analyst")),
):
    from app.intelligence.event_pair_model import event_pair_model_status
    from app.intelligence.semantic import semantic_model_status

    return {
        "multilingual_embeddings": semantic_model_status(),
        "supervised_event_pair_model": event_pair_model_status(),
    }


@app.get("/api/intelligence/source-reliability")
def list_source_reliability(
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(SourceReliabilityProfile)
        .order_by(SourceReliabilityProfile.updated_at.desc(), SourceReliabilityProfile.source_origin.asc())
        .limit(limit)
        .all()
    )
    return [source_reliability_dict(row) for row in rows]


@app.post("/api/intelligence/source-reliability/refresh")
def refresh_source_reliability(
    request: Request,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    _rate_limit(f"source-reliability-refresh:{user.id}", 5, 300)
    result = rebuild_source_reliability_profiles(db)
    record_audit(
        db,
        "source_reliability.refreshed",
        "source_reliability",
        None,
        user.id,
        request,
        result,
    )
    db.commit()
    return result




def _acquisition_backfill_out(run: AcquisitionBackfillRun) -> dict:
    return {
        "id": run.id,
        "source_type": run.source_type,
        "start_at": run.start_at.isoformat(),
        "end_at": run.end_at.isoformat(),
        "cursor_at": run.cursor_at.isoformat(),
        "page_offset": run.page_offset,
        "window_days": run.window_days,
        "max_records": run.max_records,
        "status": run.status,
        "records_fetched": run.records_fetched,
        "records_inserted": run.records_inserted,
        "records_duplicate": run.records_duplicate,
        "records_rejected": run.records_rejected,
        "observations_captured": run.observations_captured,
        "records_queued": run.records_queued,
        "analysis_queue_failures": run.analysis_queue_failures,
        "windows_completed": run.windows_completed,
        "last_error_code": run.last_error_code,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


@app.post("/api/integrations/backfills", status_code=202)
def create_acquisition_backfill(
    payload: AcquisitionBackfillRequest,
    request: Request,
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    from datetime import UTC

    from app.integrations.registry import connectors

    connector = next((item for item in connectors() if item.source_type == payload.source_type), None)
    if connector is None or not connector.configured():
        raise HTTPException(status_code=409, detail="Historical source is not configured")
    if not callable(getattr(connector, "fetch_window", None)):
        raise HTTPException(status_code=422, detail="Historical backfill is not supported for this source")
    start = payload.start_at.replace(tzinfo=UTC) if payload.start_at.tzinfo is None else payload.start_at.astimezone(UTC)
    end = payload.end_at.replace(tzinfo=UTC) if payload.end_at.tzinfo is None else payload.end_at.astimezone(UTC)
    run = AcquisitionBackfillRun(
        id=str(uuid4()), source_type=payload.source_type, start_at=start, end_at=end,
        window_days=payload.window_days, cursor_at=start, page_offset=1,
        max_records=payload.max_records, status="queued", requested_by=user.id,
        created_at=utcnow(), updated_at=utcnow(),
    )
    db.add(run)
    db.flush()
    record_audit(
        db, "acquisition.backfill.created", "acquisition_backfill", run.id, user.id, request,
        {"source_type": run.source_type, "start_at": start.isoformat(), "end_at": end.isoformat(),
         "window_days": run.window_days, "max_records": run.max_records},
    )
    db.commit()
    try:
        job = enqueue_job("app.worker.run_acquisition_backfill", run.id)
    except Exception as exc:
        logger.warning("Backfill job enqueue failed (%s)", type(exc).__name__)
        job = None
    if job is None:
        run.status = "queue_failed"
        run.last_error_code = "queue_unavailable"
        db.commit()
        raise HTTPException(status_code=503, detail={"message": "Backfill saved but queue unavailable", "run": _acquisition_backfill_out(run)})
    return {**_acquisition_backfill_out(run), "job_id": job.id}


@app.get("/api/integrations/backfills")
def list_acquisition_backfills(
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    runs = db.query(AcquisitionBackfillRun).order_by(AcquisitionBackfillRun.created_at.desc()).limit(limit).all()
    return [_acquisition_backfill_out(run) for run in runs]


@app.get("/api/integrations/backfills/{run_id}")
def get_acquisition_backfill(
    run_id: str,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_id = str(UUID(run_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid backfill run ID") from exc
    run = db.get(AcquisitionBackfillRun, normalized_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backfill run not found")
    return _acquisition_backfill_out(run)


@app.post("/api/integrations/backfills/{run_id}/resume", status_code=202)
def resume_acquisition_backfill(
    run_id: str,
    request: Request,
    additional_records: int = Query(default=10000, ge=1, le=50000),
    user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    try:
        normalized_id = str(UUID(run_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid backfill run ID") from exc
    run = db.query(AcquisitionBackfillRun).filter_by(id=normalized_id).with_for_update().one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Backfill run not found")
    if run.status not in {"failed", "partial", "limit_reached", "queue_failed"}:
        raise HTTPException(status_code=409, detail="Only failed or incomplete backfills can be resumed")
    run.max_records = min(200000, int(run.max_records) + additional_records)
    run.status = "queued"
    run.last_error_code = None
    run.completed_at = None
    db.commit()
    job = enqueue_job("app.worker.run_acquisition_backfill", run.id)
    if job is None:
        run.status = "queue_failed"
        run.last_error_code = "queue_unavailable"
        db.commit()
        raise HTTPException(status_code=503, detail="Queue unavailable; backfill remains resumable")
    return {**_acquisition_backfill_out(run), "job_id": job.id}


@app.get("/api/integrations/observations")
def list_acquisition_observations(
    source_type: str | None = Query(default=None, max_length=24),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    query = db.query(AcquisitionObservation)
    if source_type:
        query = query.filter(AcquisitionObservation.source_type == source_type.strip().lower())
    rows = query.order_by(AcquisitionObservation.last_seen_at.desc()).limit(limit).all()
    return [{
        "id": row.id, "source_type": row.source_type, "source_record_id": row.source_record_id,
        "content_sha256": row.content_sha256, "observed_at": row.observed_at.isoformat() if row.observed_at else None,
        "source_url": row.source_url, "report_id": row.report_id, "seen_count": row.seen_count,
        "first_seen_at": row.first_seen_at.isoformat(), "last_seen_at": row.last_seen_at.isoformat(),
        "replay_attempts": row.replay_attempts, "last_error_code": row.last_error_code,
    } for row in rows]


@app.post("/api/integrations/observations/{observation_id}/replay", status_code=202)
def replay_acquisition_observation(
    observation_id: str,
    request: Request,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_id = str(UUID(observation_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid observation ID") from exc
    observation = db.get(AcquisitionObservation, normalized_id)
    if observation is None:
        raise HTTPException(status_code=404, detail="Observation not found")
    job = enqueue_job("app.worker.replay_acquisition_observation", observation.id)
    if job is None:
        raise HTTPException(status_code=503, detail="Analysis queue unavailable")
    record_audit(db, "acquisition.observation.replay_requested", "acquisition_observation", observation.id, user.id, request)
    db.commit()
    return {"queued": True, "observation_id": observation.id, "job_id": job.id}


@app.post("/api/research/datasets/{dataset_id}/pair-candidates/mine", status_code=202)
def start_pair_candidate_mining(
    dataset_id: str,
    payload: PairCandidateMiningRequest,
    request: Request,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_dataset_id = str(UUID(dataset_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid dataset ID") from exc
    dataset = db.get(ResearchDataset, normalized_dataset_id)
    if dataset is None or dataset.status != "active":
        raise HTTPException(status_code=404, detail="Active research dataset not found")
    run = PairCandidateMiningRun(
        id=str(uuid4()), dataset_id=dataset.id, status="queued",
        parameters=payload.model_dump(), created_by=user.id, created_at=utcnow(),
    )
    db.add(run)
    db.flush()
    record_audit(db, "research.pair_candidate_mining.requested", "pair_candidate_mining_run", run.id, user.id, request, payload.model_dump())
    db.commit()
    job = enqueue_job("app.worker.mine_pair_candidates", run.id)
    if job is None:
        run.status = "queue_failed"
        run.failure_code = "queue_unavailable"
        db.commit()
        raise HTTPException(status_code=503, detail={"message": "Mining run saved but queue unavailable", "run_id": run.id})
    return {"run_id": run.id, "status": run.status, "job_id": job.id}


@app.get("/api/research/datasets/{dataset_id}/pair-candidates")
def list_pair_candidates(
    dataset_id: str,
    status: str = Query(default="pending", pattern="^(pending|labelled|dismissed|all)$"),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_dataset_id = str(UUID(dataset_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid dataset ID") from exc
    query = db.query(EventPairCandidate).filter_by(dataset_id=normalized_dataset_id)
    if status != "all":
        query = query.filter(EventPairCandidate.status == status)
    rows = query.order_by(EventPairCandidate.selection_score.desc(), EventPairCandidate.created_at.desc()).limit(limit).all()
    report_ids = {value for row in rows for value in (row.report_a_id, row.report_b_id)}
    reports = {item.id: item for item in db.query(Report).filter(Report.id.in_(report_ids)).all()} if report_ids else {}
    return [{
        "id": row.id, "pair_fingerprint": row.pair_fingerprint, "status": row.status,
        "heuristic_score": row.heuristic_score, "supervised_score": row.supervised_score,
        "selection_score": row.selection_score, "candidate_reasons": row.candidate_reasons,
        "report_a": {"id": reports[row.report_a_id].id, "title": reports[row.report_a_id].title,
                     "description": reports[row.report_a_id].description[:1000],
                     "source_type": reports[row.report_a_id].source_type,
                     "observed_at": reports[row.report_a_id].source_observed_at.isoformat() if reports[row.report_a_id].source_observed_at else None},
        "report_b": {"id": reports[row.report_b_id].id, "title": reports[row.report_b_id].title,
                     "description": reports[row.report_b_id].description[:1000],
                     "source_type": reports[row.report_b_id].source_type,
                     "observed_at": reports[row.report_b_id].source_observed_at.isoformat() if reports[row.report_b_id].source_observed_at else None},
        "created_at": row.created_at.isoformat(),
    } for row in rows if row.report_a_id in reports and row.report_b_id in reports]


@app.get("/api/research/datasets/{dataset_id}/pair-candidates/runs/{run_id}")
def get_pair_candidate_mining_run(
    dataset_id: str,
    run_id: str,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        dataset_key, run_key = str(UUID(dataset_id)), str(UUID(run_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid dataset or run ID") from exc
    run = db.query(PairCandidateMiningRun).filter_by(id=run_key, dataset_id=dataset_key).one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Mining run not found")
    return {
        "id": run.id, "status": run.status, "parameters": run.parameters,
        "reports_considered": run.reports_considered, "pairs_scored": run.pairs_scored,
        "candidates_created": run.candidates_created, "candidates_existing": run.candidates_existing,
        "failure_code": run.failure_code, "created_at": run.created_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


@app.post("/api/research/datasets/{dataset_id}/pair-candidates/runs/{run_id}/resume", status_code=202)
def resume_pair_candidate_mining(
    dataset_id: str,
    run_id: str,
    request: Request,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        dataset_key, run_key = str(UUID(dataset_id)), str(UUID(run_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid dataset or run ID") from exc
    run = db.query(PairCandidateMiningRun).filter_by(
        id=run_key, dataset_id=dataset_key,
    ).with_for_update().one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Mining run not found")
    if run.status not in {"failed", "queue_failed"}:
        raise HTTPException(status_code=409, detail="Only failed or queue-failed mining runs can be resumed")
    run.status = "queued"
    run.failure_code = None
    run.completed_at = None
    run.updated_at = utcnow()
    record_audit(db, "research.pair_candidate_mining.resumed", "pair_candidate_mining_run", run.id, user.id, request)
    db.commit()
    job = enqueue_job("app.worker.mine_pair_candidates", run.id)
    if job is None:
        run.status = "queue_failed"
        run.failure_code = "queue_unavailable"
        run.updated_at = utcnow()
        db.commit()
        raise HTTPException(status_code=503, detail="Mining run remains saved and can be resumed")
    return {"run_id": run.id, "status": run.status, "job_id": job.id}


@app.post("/api/research/datasets/{dataset_id}/pair-candidates/{candidate_id}/dismiss")
def dismiss_pair_candidate(
    dataset_id: str,
    candidate_id: str,
    request: Request,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        dataset_key, candidate_key = str(UUID(dataset_id)), str(UUID(candidate_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid dataset or candidate ID") from exc
    candidate = db.query(EventPairCandidate).filter_by(
        id=candidate_key, dataset_id=dataset_key,
    ).with_for_update().one_or_none()
    if candidate is None:
        raise HTTPException(status_code=404, detail="Pair candidate not found")
    if candidate.status != "pending":
        raise HTTPException(status_code=409, detail="Candidate is no longer pending review")
    candidate.status = "dismissed"
    candidate.updated_at = utcnow()
    record_audit(db, "research.pair_candidate.dismissed", "event_pair_candidate", candidate.id, user.id, request)
    db.commit()
    return {"id": candidate.id, "status": candidate.status}


@app.get("/api/research/model-evaluations")
def list_model_evaluation_runs(
    task_type: str | None = Query(default=None, max_length=48),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    query = db.query(ModelEvaluationRun)
    if task_type:
        query = query.filter(ModelEvaluationRun.task_type == task_type)
    rows = query.order_by(ModelEvaluationRun.created_at.desc()).limit(limit).all()
    return [{
        "id": row.id, "task_type": row.task_type, "model_name": row.model_name,
        "model_version": row.model_version, "dataset_sha256": row.dataset_sha256,
        "artifact_sha256": row.artifact_sha256, "sample_count": row.sample_count,
        "group_count": row.group_count, "metrics": row.metrics,
        "baseline_metrics": row.baseline_metrics, "comparison": row.comparison,
        "evaluation_policy": row.evaluation_policy, "created_at": row.created_at.isoformat(),
    } for row in rows]


def _research_dataset_out(dataset: ResearchDataset, db: Session) -> dict:
    label_query = db.query(EventPairLabel).filter(EventPairLabel.dataset_id == dataset.id)
    version_query = db.query(ResearchDatasetVersion).filter(
        ResearchDatasetVersion.dataset_id == dataset.id
    )
    return {
        "id": dataset.id,
        "slug": dataset.slug,
        "name": dataset.name,
        "description": dataset.description,
        "task_type": dataset.task_type,
        "purpose": dataset.purpose,
        "license_identifier": dataset.license_identifier,
        "license_url": dataset.license_url,
        "legal_basis": dataset.legal_basis,
        "consent_scope": dataset.consent_scope,
        "source_types": dataset.source_types,
        "coordinate_precision": dataset.coordinate_precision,
        "privacy_policy": dataset.privacy_policy,
        "status": dataset.status,
        "labels_total": label_query.count(),
        "labels_pending": label_query.filter(EventPairLabel.review_status == "pending").count(),
        "labels_approved": label_query.filter(
            EventPairLabel.review_status == "approved",
            EventPairLabel.privacy_review_status == "passed",
        ).count(),
        "versions_total": version_query.count(),
        "created_at": dataset.created_at.isoformat(),
        "updated_at": dataset.updated_at.isoformat(),
    }


def _dataset_version_out(version: ResearchDatasetVersion) -> dict:
    return {
        "id": version.id,
        "dataset_id": version.dataset_id,
        "version_number": version.version_number,
        "state": version.state,
        "schema_version": version.schema_version,
        "content_sha256": version.content_sha256,
        "record_count": version.record_count,
        "same_event_count": version.same_event_count,
        "different_event_count": version.different_event_count,
        "split_group_count": version.split_group_count,
        "manifest": version.manifest,
        "created_at": version.created_at.isoformat(),
        "sealed_at": version.sealed_at.isoformat(),
    }


@app.post("/api/research/datasets", status_code=201)
def create_research_dataset(
    payload: ResearchDatasetCreate,
    request: Request,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    _rate_limit(f"research-dataset-create:{user.id}", 10, 300)
    if db.query(ResearchDataset).filter_by(slug=payload.slug).first():
        raise HTTPException(status_code=409, detail="Dataset slug already exists")
    if payload.license_identifier.strip().lower() in {"unknown", "none", "unspecified"}:
        raise HTTPException(status_code=422, detail="A specific dataset license or terms identifier is required")
    dataset = ResearchDataset(
        id=str(uuid4()),
        slug=payload.slug,
        name=payload.name.strip(),
        description=payload.description.strip(),
        task_type=payload.task_type,
        purpose=payload.purpose.strip(),
        license_identifier=payload.license_identifier.strip(),
        license_url=payload.license_url.strip() if payload.license_url else None,
        legal_basis=payload.legal_basis,
        consent_scope=payload.consent_scope,
        source_types=payload.source_types,
        coordinate_precision=payload.coordinate_precision,
        privacy_policy=payload.privacy_policy,
        status="active",
        created_by=user.id,
    )
    db.add(dataset)
    db.flush()
    record_audit(
        db, "research_dataset.created", "research_dataset", dataset.id, user.id, request,
        {"slug": dataset.slug, "legal_basis": dataset.legal_basis,
            "license_identifier": dataset.license_identifier, "source_types": dataset.source_types},
    )
    db.commit()
    db.refresh(dataset)
    return _research_dataset_out(dataset, db)


@app.get("/api/research/datasets")
def list_research_datasets(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=100000),
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(ResearchDataset)
        .order_by(ResearchDataset.updated_at.desc(), ResearchDataset.slug.asc())
        .offset(offset).limit(limit).all()
    )
    return [_research_dataset_out(row, db) for row in rows]


@app.get("/api/research/datasets/{dataset_id}")
def get_research_dataset(
    dataset_id: str,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_id = str(UUID(dataset_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid dataset ID") from exc
    dataset = db.get(ResearchDataset, normalized_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    result = _research_dataset_out(dataset, db)
    result["versions"] = [
        _dataset_version_out(row)
        for row in db.query(ResearchDatasetVersion)
        .filter_by(dataset_id=dataset.id)
        .order_by(ResearchDatasetVersion.version_number.desc()).all()
    ]
    return result


@app.post("/api/research/datasets/{dataset_id}/pair-labels", status_code=201)
def create_event_pair_label(
    dataset_id: str,
    payload: EventPairLabelCreate,
    request: Request,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_dataset_id = str(UUID(dataset_id))
        report_ids = sorted((str(UUID(payload.report_a_id)), str(UUID(payload.report_b_id))))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid dataset or report ID") from exc
    dataset = db.get(ResearchDataset, normalized_dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    if dataset.status != "active":
        raise HTTPException(status_code=409, detail="Dataset is not accepting new labels")
    reports = db.query(Report).filter(Report.id.in_(report_ids)).with_for_update().all()
    by_id = {row.id: row for row in reports}
    if len(by_id) != 2:
        raise HTTPException(status_code=404, detail="One or both reports were not found")
    report_a, report_b = by_id[report_ids[0]], by_id[report_ids[1]]
    allowed_sources = set(dataset.source_types or [])
    if "*" not in allowed_sources and not {report_a.source_type, report_b.source_type}.issubset(allowed_sources):
        raise HTTPException(status_code=422, detail="Report source is outside this dataset's registered source scope")
    fingerprint = pair_fingerprint(*report_ids)
    if db.query(EventPairLabel).filter_by(
        dataset_id=dataset.id, pair_fingerprint=fingerprint
    ).first():
        raise HTTPException(status_code=409, detail="This report pair is already labelled in the dataset")
    provenance = {
        "report_a": {
            "report_id": report_a.id,
            "content_sha256": report_content_fingerprint(report_a),
            "source_metadata_sha256": report_source_metadata_fingerprint(report_a),
            "source_type": report_a.source_type,
            "source_name": report_a.source_name,
            "source_observed_at": report_a.source_observed_at.isoformat() if report_a.source_observed_at else None,
            "created_at": report_a.created_at.isoformat() if report_a.created_at else None,
        },
        "report_b": {
            "report_id": report_b.id,
            "content_sha256": report_content_fingerprint(report_b),
            "source_metadata_sha256": report_source_metadata_fingerprint(report_b),
            "source_type": report_b.source_type,
            "source_name": report_b.source_name,
            "source_observed_at": report_b.source_observed_at.isoformat() if report_b.source_observed_at else None,
            "created_at": report_b.created_at.isoformat() if report_b.created_at else None,
        },
    }
    label = EventPairLabel(
        id=str(uuid4()), dataset_id=dataset.id, report_a_id=report_a.id, report_b_id=report_b.id,
        pair_fingerprint=fingerprint, label=payload.label, split_group=payload.split_group,
        rationale=payload.rationale.strip(), label_method=payload.label_method,
        review_status="pending", privacy_review_status="pending",
        provenance_snapshot=provenance, created_by=user.id,
    )
    db.add(label)
    candidate = db.query(EventPairCandidate).filter_by(
        dataset_id=dataset.id, pair_fingerprint=fingerprint, status="pending"
    ).first()
    if candidate is not None:
        candidate.status = "labelled"
        candidate.updated_at = utcnow()
    record_audit(
        db, "event_pair_label.created", "event_pair_label", label.id, user.id, request,
        {"dataset_id": dataset.id, "pair_fingerprint": fingerprint, "label": label.label,
            "split_group": label.split_group, "label_method": label.label_method},
    )
    db.commit()
    return {
        "id": label.id, "dataset_id": dataset.id, "report_a_id": report_a.id,
        "report_b_id": report_b.id, "label": label.label, "split_group": label.split_group,
        "review_status": label.review_status, "privacy_review_status": label.privacy_review_status,
        "created_at": label.created_at.isoformat(),
    }


@app.patch("/api/research/datasets/{dataset_id}/pair-labels/{label_id}/review")
def review_event_pair_label(
    dataset_id: str,
    label_id: str,
    payload: EventPairLabelReview,
    request: Request,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_dataset_id = str(UUID(dataset_id))
        normalized_label_id = str(UUID(label_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid dataset or label ID") from exc
    label = db.query(EventPairLabel).filter_by(
        id=normalized_label_id, dataset_id=normalized_dataset_id
    ).first()
    if label is None:
        raise HTTPException(status_code=404, detail="Pair label not found")
    if payload.decision == "approve" and payload.privacy_review != "passed":
        raise HTTPException(status_code=422, detail="A label cannot be approved without a passed privacy review")
    label.review_status = "approved" if payload.decision == "approve" else "rejected"
    label.privacy_review_status = payload.privacy_review
    label.review_note = payload.note.strip() if payload.note else None
    label.reviewed_by = user.id
    label.reviewed_at = utcnow()
    record_audit(
        db, "event_pair_label.reviewed", "event_pair_label", label.id, user.id, request,
        {"decision": payload.decision, "privacy_review": payload.privacy_review,
            "dataset_id": label.dataset_id},
    )
    db.commit()
    return {
        "id": label.id, "review_status": label.review_status,
        "privacy_review_status": label.privacy_review_status,
        "reviewed_by": label.reviewed_by, "reviewed_at": label.reviewed_at.isoformat(),
    }


@app.post("/api/research/datasets/{dataset_id}/versions", status_code=201)
def create_research_dataset_version(
    dataset_id: str,
    payload: ResearchDatasetVersionCreate,
    request: Request,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_dataset_id = str(UUID(dataset_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid dataset ID") from exc
    dataset = (
        db.query(ResearchDataset)
        .filter(ResearchDataset.id == normalized_dataset_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    if dataset.status != "active":
        raise HTTPException(status_code=409, detail="Dataset is not active")
    report_a = aliased(Report)
    report_b = aliased(Report)
    labelled_rows = (
        db.query(EventPairLabel, report_a, report_b)
        .join(report_a, EventPairLabel.report_a_id == report_a.id)
        .join(report_b, EventPairLabel.report_b_id == report_b.id)
        .filter(
            EventPairLabel.dataset_id == dataset.id,
            EventPairLabel.review_status == "approved",
            EventPairLabel.privacy_review_status == "passed",
        )
        .order_by(EventPairLabel.split_group.asc(), EventPairLabel.pair_fingerprint.asc())
        .with_for_update()
        .all()
    )
    try:
        snapshot = prepare_dataset_snapshot(
            labelled_rows,
            minimum_records=payload.minimum_records,
            minimum_groups=payload.minimum_groups,
            coordinate_precision=dataset.coordinate_precision,
        )
    except ResearchDatasetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    current_version = db.query(func.max(ResearchDatasetVersion.version_number)).filter_by(
        dataset_id=dataset.id
    ).scalar() or 0
    version = ResearchDatasetVersion(
        id=str(uuid4()), dataset_id=dataset.id, version_number=current_version + 1,
        state="sealed", schema_version=snapshot["schema_version"],
        content_sha256=snapshot["content_sha256"], record_count=snapshot["record_count"],
        same_event_count=snapshot["same_event_count"],
        different_event_count=snapshot["different_event_count"],
        split_group_count=snapshot["split_group_count"], created_by=user.id,
        manifest={},
    )
    created_at = datetime.now(UTC)
    version.manifest = {
        "dataset": {"id": dataset.id, "slug": dataset.slug, "name": dataset.name,
            "task_type": dataset.task_type, "purpose": dataset.purpose},
        "governance": {"license_identifier": dataset.license_identifier,
            "license_url": dataset.license_url, "legal_basis": dataset.legal_basis,
            "consent_scope": dataset.consent_scope, "privacy_policy": dataset.privacy_policy,
            "source_types": dataset.source_types,
            "coordinate_precision": dataset.coordinate_precision},
        "version": version.version_number,
        "schema_version": snapshot["schema_version"],
        "created_at": created_at.isoformat(),
        "record_count": snapshot["record_count"],
        "same_event_count": snapshot["same_event_count"],
        "different_event_count": snapshot["different_event_count"],
        "split_group_count": snapshot["split_group_count"],
        "positive_group_count": snapshot["positive_group_count"],
        "negative_group_count": snapshot["negative_group_count"],
        "mixed_group_count": snapshot["mixed_group_count"],
        "split_groups": snapshot["split_groups"],
        "source_type_counts": snapshot["source_type_counts"],
        "source_license_status_counts": snapshot["source_license_status_counts"],
        "source_catalog_versions": snapshot["source_catalog_versions"],
        "label_method_counts": snapshot["label_method_counts"],
        "redaction_counts": snapshot["redaction_counts"],
        "automated_privacy_screen": "email_and_phone_pattern_redaction_only_not_exhaustive",
        "human_privacy_review_required": True,
        "payload_bytes": snapshot["payload_bytes"],
        "content_sha256": snapshot["content_sha256"],
        "score_interpretation": "labels_are_operator_adjudications_not_automatically_verified_truth",
    }
    db.add(version)
    db.flush()
    for ordinal, (record, provenance) in enumerate(
        zip(snapshot["records"], snapshot["provenance"], strict=True), start=1
    ):
        db.add(ResearchDatasetVersionItem(
            id=str(uuid4()), version_id=version.id, ordinal=ordinal,
            pair_fingerprint=record["pair_fingerprint"], split_group=record["split_group"],
            label="same_event" if record["same_event"] == 1 else "different_event",
            record_json=record, provenance_json=provenance,
        ))
    record_audit(
        db, "research_dataset.version_sealed", "research_dataset_version", version.id,
        user.id, request,
        {"dataset_id": dataset.id, "version_number": version.version_number,
            "record_count": version.record_count, "content_sha256": version.content_sha256},
    )
    db.commit()
    db.refresh(version)
    return _dataset_version_out(version)


@app.get("/api/research/datasets/{dataset_id}/versions")
def list_research_dataset_versions(
    dataset_id: str,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_id = str(UUID(dataset_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid dataset ID") from exc
    if db.get(ResearchDataset, normalized_id) is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    versions = (
        db.query(ResearchDatasetVersion).filter_by(dataset_id=normalized_id)
        .order_by(ResearchDatasetVersion.version_number.desc()).all()
    )
    return [_dataset_version_out(row) for row in versions]


@app.get("/api/research/datasets/{dataset_id}/versions/{version_id}/export")
def export_research_dataset_version(
    dataset_id: str,
    version_id: str,
    user: User = Depends(require_roles("admin", "analyst")),
    db: Session = Depends(get_db),
):
    try:
        normalized_dataset_id = str(UUID(dataset_id))
        normalized_version_id = str(UUID(version_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid dataset or version ID") from exc
    dataset = db.get(ResearchDataset, normalized_dataset_id)
    version = db.query(ResearchDatasetVersion).filter_by(
        id=normalized_version_id, dataset_id=normalized_dataset_id
    ).first()
    if dataset is None or version is None:
        raise HTTPException(status_code=404, detail="Dataset version not found")
    if version.state != "sealed":
        raise HTTPException(status_code=409, detail="Only sealed dataset versions can be exported")
    items = (
        db.query(ResearchDatasetVersionItem).filter_by(version_id=version.id)
        .order_by(ResearchDatasetVersionItem.ordinal.asc()).all()
    )
    expected_ordinals = list(range(1, version.record_count + 1))
    actual_ordinals = [item.ordinal for item in items]
    if len(items) != version.record_count or actual_ordinals != expected_ordinals:
        logger.error("Research dataset snapshot row-count/ordinal mismatch: %s", version.id)
        raise HTTPException(status_code=409, detail="Dataset snapshot structure check failed")
    body = serialize_dataset_items(items)
    digest = hashlib.sha256(body).hexdigest()
    if digest != version.content_sha256:
        logger.error("Research dataset snapshot hash mismatch: %s", version.id)
        raise HTTPException(status_code=409, detail="Dataset snapshot integrity check failed")
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{dataset.slug}-v{version.version_number}.jsonl"',
            "X-Content-SHA256": digest,
        },
    )


@app.get("/api/notifications")
def list_notifications(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(Notification).filter_by(user_id=user.id).order_by(Notification.created_at.desc()).limit(50).all()
    return [{"id": row.id, "title": row.title, "body": row.body, "category": row.category, "target_url": row.target_url, "is_read": row.is_read, "created_at": row.created_at.isoformat()} for row in rows]


@app.post("/api/notifications/{notification_id}/read")
def mark_notification_read(notification_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(Notification).filter_by(id=notification_id, user_id=user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    row.is_read = True
    db.commit()
    return {"ok": True}


@app.get("/api/push/public-key")
def push_public_key(user: User = Depends(get_current_user)):
    return {"public_key": settings.vapid_public_key, "enabled": bool(settings.vapid_public_key and settings.vapid_private_key)}


@app.post("/api/push/subscribe")
def subscribe_push(payload: PushSubscriptionRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    endpoint = str(payload.subscription.get("endpoint") or "")
    keys = payload.subscription.get("keys")
    if not endpoint.startswith("https://") or not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(status_code=422, detail="Invalid Web Push subscription")
    existing = db.query(PushSubscription).filter_by(endpoint=endpoint).first()
    if existing:
        existing.user_id = user.id
        existing.subscription_json = payload.subscription
    else:
        db.add(PushSubscription(id=str(uuid4()), user_id=user.id, endpoint=endpoint, subscription_json=payload.subscription))
    db.commit()
    return {"ok": True}


@app.delete("/api/push/subscribe")
def unsubscribe_push(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    endpoint = str(payload.get("endpoint") or "")
    row = db.query(PushSubscription).filter_by(endpoint=endpoint, user_id=user.id).first()
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True}


@app.get("/api/users")
def list_users(user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    return [_user_dict(row) for row in db.query(User).order_by(User.created_at.desc()).all()]


@app.post("/api/users", status_code=201)
def create_user(payload: UserCreate, request: Request, user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    email = payload.email.lower()
    if db.query(User).filter_by(email=email).first():
        raise HTTPException(status_code=409, detail="Email is already registered")
    new_user = User(id=str(uuid4()), email=email, display_name=payload.display_name, password_hash=hash_password(payload.password), role=payload.role)
    db.add(new_user)
    record_audit(db, "user.created", "user", new_user.id, user.id, request, {"role": payload.role})
    db.commit()
    return _user_dict(new_user)


@app.patch("/api/users/{user_id}")
def update_user(user_id: str, payload: UserUpdate, request: Request, admin: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == admin.id and payload.is_active is False:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
    if payload.role is not None:
        target.role = payload.role
    if payload.is_active is not None:
        target.is_active = payload.is_active
        if not target.is_active:
            db.query(RefreshToken).filter_by(user_id=target.id, revoked_at=None).update({"revoked_at": utcnow()})
    record_audit(db, "user.updated", "user", target.id, admin.id, request, {"role": target.role, "is_active": target.is_active})
    db.commit()
    return _user_dict(target)


@app.post("/api/users/{user_id}/mfa/reset")
def reset_user_mfa(user_id: str, request: Request, admin: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="Use your recovery codes or account recovery procedure for your own MFA")
    target.mfa_enabled = False
    target.mfa_secret = None
    target.pending_mfa_secret = None
    target.mfa_recovery_hashes = None
    db.query(RefreshToken).filter_by(user_id=target.id, revoked_at=None).update({"revoked_at": utcnow()})
    record_audit(db, "user.mfa_reset", "user", target.id, admin.id, request)
    db.commit()
    return {"ok": True, "message": "MFA reset; the user must enroll again on next sign-in."}


@app.get("/api/audit")
def audit_logs(limit: int = 100, user: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    rows = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(max(limit, 1), 250)).all()
    return [{"id": row.id, "actor_user_id": row.actor_user_id, "action": row.action, "resource_type": row.resource_type, "resource_id": row.resource_id, "ip_address": row.ip_address, "details": row.details, "created_at": row.created_at.isoformat()} for row in rows]


@app.websocket("/api/ws/incidents")
async def incident_websocket(websocket: WebSocket, token: str):
    try:
        claims = decode_token(token)
        if claims.get("type") != "access":
            raise ValueError("Wrong token type")
        with SessionLocal() as db:
            user = db.get(User, claims["sub"])
            if not user or not user.is_active or (settings.mfa_required and not user.mfa_enabled):
                await websocket.close(code=4403)
                return
    except Exception:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    client = redis_client()
    pubsub = client.pubsub()
    pubsub.subscribe("cignal:events")
    try:
        while True:
            message = await asyncio.to_thread(pubsub.get_message, True, 1.0)
            if message and message.get("type") == "message":
                import json
                event = json.loads(message["data"])
                if event_is_visible_to_user(user.role, user.id, event):
                    await websocket.send_json(event)
            await asyncio.sleep(0.2)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Incident websocket disconnected unexpectedly")
        try:
            await websocket.close(code=1011, reason="Event stream unavailable")
        except RuntimeError:
            pass
    finally:
        pubsub.close()
        client.close()
