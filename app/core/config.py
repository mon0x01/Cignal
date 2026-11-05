from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    demo_mode: bool = False
    app_secret_key: str = "development-only-change-me-please"
    jwt_secret_key: str = "development-only-jwt-secret-change-me"
    jwt_access_minutes: int = 20
    jwt_refresh_days: int = 14
    cookie_secure: bool = False
    cors_origins: str = "http://localhost:8080"
    database_url: str = "sqlite:///./cignal-dev.db"
    redis_url: str = "redis://localhost:6379/0"
    api_internal_url: str = "http://localhost:8000"
    api_public_url: str = ""
    ui_public_url: str = "http://localhost:8080"
    evidence_dir: Path = Path("./data/evidence")
    max_upload_mb: int = 10
    retention_days: int = 365
    imd_api_base_url: str = ""
    imd_api_token: str = ""
    india_gov_api_base_url: str = ""
    india_gov_api_token: str = ""
    bhuvan_geojson_url: str = ""
    bhuvan_api_token: str = ""
    nasa_firms_map_key: str = ""
    nasa_eonet_enabled: bool = True
    sachet_cap_identifiers: str = ""
    open_meteo_enabled: bool = True
    gdacs_enabled: bool = True
    usgs_enabled: bool = True
    authorized_rss_feeds: str = ""
    social_api_base_url: str = ""
    social_api_token: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_claims_subject: str = "mailto:admin@example.invalid"
    storage_backend: str = "local"
    s3_endpoint_url: str = ""
    s3_bucket: str = "cignal-evidence"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"
    ingest_interval_minutes: int = 30
    ingest_source_interval_overrides: str = ""
    ingest_scheduler_tick_seconds: int = 30
    ingest_job_timeout_seconds: int = 1200
    ingest_job_lease_seconds: int = 1800
    ingest_retry_backoff_base_seconds: int = 60
    ingest_retry_backoff_max_seconds: int = 21600
    integration_retry_attempts: int = 3
    integration_retry_base_delay_seconds: float = 0.5
    integration_retry_max_delay_seconds: float = 8.0
    ai_enable_sklearn: bool = True
    ai_text_model_path: Path = Path("./data/models/disaster-text-v1.joblib")
    ai_vision_model_path: Path = Path("./data/models/disaster-vision-v1.joblib")
    ai_max_images_per_report: int = 8
    ai_enable_torchvision: bool = False
    ai_enable_pretrained_nlp: bool = False
    ai_nlp_model_id: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    event_pair_model_path: Path = Path("./data/models/event-pair-v1.joblib")
    continuous_event_discovery_enabled: bool = True
    continuous_event_discovery_quiet_seconds: int = 120
    continuous_event_discovery_window_hours: int = 72
    continuous_event_discovery_max_reports: int = 300
    retention_enabled: bool = True
    evidence_integrity_audit_enabled: bool = True
    mfa_required: bool = True
    rate_limit_fail_closed: bool = True
    backup_dir: Path = Path("./data/backups")
    backup_retention_count: int = 14

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def authorized_feeds(self) -> list[str]:
        return [item.strip() for item in self.authorized_rss_feeds.split(",") if item.strip()]

    @property
    def sachet_cap_identifier_list(self) -> list[str]:
        """Return unique configured SACHET identifiers without exposing them in logs."""
        values = []
        seen = set()
        for item in self.sachet_cap_identifiers.split(";"):
            identifier = item.strip()
            if not identifier or len(identifier) > 256 or any(ord(char) < 32 for char in identifier):
                continue
            if identifier not in seen:
                seen.add(identifier)
                values.append(identifier)
        return values[:50]

    @property
    def max_upload_bytes(self) -> int:
        return max(1, self.max_upload_mb) * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
