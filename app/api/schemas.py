import re
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    totp_code: str | None = Field(default=None, min_length=6, max_length=32)


class SetupAdminRequest(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=14, max_length=128)


class ReportCreate(BaseModel):
    title: str = Field(min_length=5, max_length=180)
    description: str = Field(min_length=20, max_length=10000)
    disaster_type: str = Field(default="unknown", pattern="^(unknown|flood|earthquake|fire|cyclone|landslide|other)$")
    urgency: str = Field(default="unknown", pattern="^(unknown|low|moderate|high|critical)$")
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_text: str | None = Field(default=None, max_length=220)
    language: str = Field(default="en", pattern="^(en|hi|gu|other)$")
    reporter_name: str | None = Field(default=None, max_length=100)

    @field_validator("disaster_type", "urgency")
    @classmethod
    def normalize_enum_like(cls, value: str) -> str:
        return value.strip().lower()


class ReportFollowup(BaseModel):
    message: str = Field(min_length=20, max_length=3000)


class ReviewRequest(BaseModel):
    decision: str = Field(pattern="^(verify|reject|request_info|link)$")
    incident_id: str | None = None
    note: str | None = Field(default=None, max_length=1000)
    incident_title: str | None = Field(default=None, max_length=180)


class IncidentUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(verified|monitoring|resolved|closed)$")
    severity: str | None = Field(default=None, pattern="^(low|moderate|high|critical)$")
    summary: str | None = Field(default=None, max_length=5000)


class IncidentActivityCreate(BaseModel):
    activity_type: str = Field(
        pattern="^(observation|situation_update|resource_request|access_constraint|handover|note)$"
    )
    message: str = Field(min_length=10, max_length=3000)
    location_text: str | None = Field(default=None, max_length=220)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        value = value.strip()
        if "\x00" in value:
            raise ValueError("Activity message cannot contain a null byte")
        if len(value) < 10:
            raise ValueError("Activity message must contain at least 10 non-whitespace characters")
        return value

    @field_validator("location_text")
    @classmethod
    def normalize_location(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if "\x00" in value:
            raise ValueError("Location cannot contain a null byte")
        return value or None

    @model_validator(mode="after")
    def coordinates_must_be_paired(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        return self


class IncidentActionCreate(BaseModel):
    title: str = Field(min_length=5, max_length=180)
    description: str = Field(min_length=10, max_length=5000)
    action_type: str = Field(pattern="^(monitoring|logistics|communications|medical_support|infrastructure|public_warning|evacuation|resource_dispatch|medical_intervention|other)$")
    priority: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")
    assigned_to: str | None = Field(default=None, min_length=36, max_length=36)
    due_at: datetime | None = None

    @field_validator("title", "description")
    @classmethod
    def clean_text(cls, value: str) -> str:
        value = value.strip()
        if "\\x00" in value or not value:
            raise ValueError("Text must be non-empty and cannot contain null bytes")
        return value


class IncidentActionStatusUpdate(BaseModel):
    status: str = Field(pattern="^(approved|in_progress|blocked|completed|cancelled)$")
    expected_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=2000)


class IncidentActionApproval(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    note: str = Field(min_length=10, max_length=2000)




class PushSubscriptionRequest(BaseModel):
    subscription: dict


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=14, max_length=128)
    role: str = Field(pattern="^(admin|analyst|responder|citizen)$")


class UserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|analyst|responder|citizen)$")
    is_active: bool | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    display_name: str
    role: str
    is_active: bool
    mfa_enabled: bool


class EventDiscoveryRequest(BaseModel):
    window_hours: int = Field(default=72, ge=1, le=168)
    max_reports: int = Field(default=300, ge=2, le=500)
    minimum_pair_score: float = Field(default=0.62, ge=0.0, le=1.0)
    max_distance_km: float = Field(default=25.0, ge=0.1, le=500.0)


class EventHypothesisReview(BaseModel):
    status: str = Field(pattern="^(investigating|dismissed)$")
    note: str | None = Field(default=None, max_length=1000)


class EventHypothesisAssociationReview(BaseModel):
    decision: str = Field(pattern="^(confirm|reject)$")
    note: str | None = Field(default=None, max_length=1000)


class ResearchDatasetCreate(BaseModel):
    slug: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=3, max_length=160)
    description: str = Field(min_length=20, max_length=3000)
    task_type: str = Field(default="event_pair_classification", pattern="^event_pair_classification$")
    purpose: str = Field(min_length=20, max_length=2000)
    license_identifier: str = Field(min_length=2, max_length=120)
    license_url: str | None = Field(default=None, max_length=700)
    legal_basis: str = Field(pattern="^(explicit_consent|official_open_data|provider_terms|institutional_approval|internal_research_approval|synthetic_test)$")
    consent_scope: str = Field(pattern="^(explicit_consent|open_data_terms|official_public_task|institutional_approval|internal_authorized|synthetic_only)$")
    source_types: list[str] = Field(min_length=1, max_length=16)
    coordinate_precision: int = Field(default=2, ge=0, le=4)
    privacy_policy: str = Field(default="exclude_direct_identifiers", pattern="^exclude_direct_identifiers$")

    @field_validator("license_url")
    @classmethod
    def validate_license_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("license_url must be an absolute HTTP(S) URL")
        return value

    @field_validator("source_types")
    @classmethod
    def normalize_source_types(cls, values: list[str]) -> list[str]:
        normalized = sorted({value.strip().lower() for value in values if value.strip()})
        if not normalized or any(
            len(value) > 24 or re.fullmatch(r"[a-z0-9_.-]+", value) is None
            for value in normalized
        ):
            raise ValueError("source_types must contain valid source identifiers")
        return normalized


class EventPairLabelCreate(BaseModel):
    report_a_id: str = Field(min_length=36, max_length=36)
    report_b_id: str = Field(min_length=36, max_length=36)
    label: str = Field(pattern="^(same_event|different_event)$")
    split_group: str = Field(min_length=3, max_length=120)
    rationale: str = Field(min_length=10, max_length=1500)
    label_method: str = Field(default="analyst_review", pattern="^(analyst_review|official_dataset|external_annotation)$")

    @field_validator("split_group")
    @classmethod
    def reject_obvious_personal_identifiers_in_group(cls, value: str) -> str:
        patterns = (
            r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
            r"(?<!\w)(?:\+91[\s.-]?)?[6-9]\d{9}(?!\w)",
            r"(?<!\w)\+\d{1,3}[\s().-]?(?:\d[\s().-]?){7,12}\d(?!\w)",
        )
        if any(re.search(pattern, value) for pattern in patterns):
            raise ValueError("split_group must not contain obvious personal email or phone identifiers")
        return value

    @model_validator(mode="after")
    def reports_must_differ(self):
        if self.report_a_id == self.report_b_id:
            raise ValueError("A report cannot be paired with itself")
        if self.split_group.strip() != self.split_group:
            raise ValueError("split_group cannot contain leading or trailing whitespace")
        return self


class EventPairLabelReview(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    privacy_review: str = Field(pattern="^(passed|blocked)$")
    note: str | None = Field(default=None, max_length=1500)


class ResearchDatasetVersionCreate(BaseModel):
    minimum_records: int = Field(default=12, ge=12, le=10000)
    minimum_groups: int = Field(default=4, ge=4, le=2000)


class AcquisitionBackfillRequest(BaseModel):
    source_type: str = Field(pattern="^(usgs|nasa_eonet)$")
    start_at: datetime
    end_at: datetime
    window_days: int = Field(default=1, ge=1, le=14)
    max_records: int = Field(default=10000, ge=1, le=50000)

    @model_validator(mode="after")
    def validate_window(self):
        from datetime import UTC

        start = self.start_at.replace(tzinfo=UTC) if self.start_at.tzinfo is None else self.start_at.astimezone(UTC)
        end = self.end_at.replace(tzinfo=UTC) if self.end_at.tzinfo is None else self.end_at.astimezone(UTC)
        if start >= end:
            raise ValueError("start_at must be earlier than end_at")
        from datetime import timedelta
        if end - start > timedelta(days=366):
            raise ValueError("A single historical backfill is limited to 366 days")
        if self.source_type == "nasa_eonet" and self.window_days > 14:
            raise ValueError("EONET windows are limited to 14 days")
        return self


class PairCandidateMiningRequest(BaseModel):
    max_reports: int = Field(default=1200, ge=20, le=2000)
    window_hours: int = Field(default=168, ge=1, le=720)
    max_distance_km: float = Field(default=250.0, ge=1, le=2000)
    max_pairs_scored: int = Field(default=100000, ge=100, le=250000)
    top_candidates: int = Field(default=2000, ge=1, le=5000)
