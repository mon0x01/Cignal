import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


@dataclass
class NormalizedSignal:
    source_type: str
    source_name: str
    source_record_id: str
    title: str
    description: str
    disaster_type: str
    urgency: str
    latitude: float | None = None
    longitude: float | None = None
    location_text: str | None = None
    source_url: str | None = None
    provenance: str = "live"
    observed_at: datetime | None = None
    source_metadata: dict | None = None


class Connector(ABC):
    name: str
    source_type: str

    @abstractmethod
    def configured(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def fetch(self) -> list[NormalizedSignal]:
        raise NotImplementedError

    def status(self) -> dict:
        return {"name": self.name, "source_type": self.source_type, "configured": self.configured()}


def utc_from_millis(value: int | float) -> datetime:
    return datetime.fromtimestamp(float(value) / 1000, tz=UTC)


def parse_datetime_utc(value) -> datetime | None:
    """Parse common provider timestamp shapes into UTC without guessing invalid values."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            timestamp = float(value)
            if not timestamp:
                return None
            return datetime.fromtimestamp(timestamp / 1000 if abs(timestamp) >= 100_000_000_000 else timestamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, (tuple, list)) and len(value) >= 6:
        try:
            return datetime(*[int(item) for item in value[:6]], tzinfo=UTC)
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


def normalize_source_record_id(value: str | None, *, fallback_material: str = "") -> str:
    """Return a deterministic, database-safe source ID without truncation collisions."""
    raw = str(value or "").strip()
    if not raw:
        if not fallback_material:
            raise ValueError("A source record ID or fallback identity is required")
        raw = hashlib.sha256(fallback_material.encode("utf-8")).hexdigest()
    if len(raw) <= 180:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{raw[:147]}:{digest}"


def normalize_location_text(value: str | None, *, max_length: int = 2000) -> str | None:
    """Normalize external place descriptions while preserving useful source detail."""
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized[:max_length] or None


_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "accesstoken",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "authtoken",
    "client_secret",
    "credential",
    "key",
    "password",
    "secret",
    "sig",
    "signature",
    "subscription_key",
    "token",
    "x_amz_credential",
    "x_amz_signature",
    "x_amz_security_token",
}


def sanitize_source_url(value: str | None, *, max_length: int = 700) -> str | None:
    """Strip URL userinfo and credential-bearing query parameters before persistence."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        safe_query = []
        for key, item in parse_qsl(parsed.query, keep_blank_values=True):
            normalized_key = key.strip().lower().replace("-", "_")
            sensitive_suffix = (
                "_key", "_token", "_secret", "_password", "_signature",
                "_credential", "_auth", "_sig",
            )
            if normalized_key in _SENSITIVE_QUERY_KEYS or normalized_key.endswith(sensitive_suffix):
                continue
            safe_query.append((key, item))
        cleaned = urlunsplit((parsed.scheme.lower(), host, parsed.path, urlencode(safe_query), ""))
    except (TypeError, ValueError):
        return None
    return cleaned if len(cleaned) <= max_length else None
