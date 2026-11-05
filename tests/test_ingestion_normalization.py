from __future__ import annotations

import hashlib

import pytest

from app.integrations.base import (
    normalize_location_text,
    normalize_source_record_id,
    sanitize_source_url,
)


def test_source_record_id_keeps_short_values_unchanged():
    assert normalize_source_record_id("usgs-event-123") == "usgs-event-123"


def test_source_record_id_uses_deterministic_digest_when_long():
    raw = "source-event-" + ("region-segment-" * 24)
    normalized = normalize_source_record_id(raw)
    assert len(normalized) == 180
    assert normalized.endswith(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32])
    assert normalized == normalize_source_record_id(raw)


def test_long_source_record_ids_with_same_prefix_do_not_collide():
    prefix = "same-provider-prefix-" * 10
    first = normalize_source_record_id(prefix + "A" * 200)
    second = normalize_source_record_id(prefix + "B" * 200)
    assert first != second
    assert len(first) == len(second) == 180


def test_source_record_id_requires_value_or_fallback():
    with pytest.raises(ValueError, match="source record ID"):
        normalize_source_record_id(None)
    fallback = normalize_source_record_id(None, fallback_material="source|title|23.0|72.0")
    assert len(fallback) == 64


def test_location_normalization_collapses_whitespace_and_bounds_size():
    assert normalize_location_text("  Ahmedabad\n  Gujarat  ") == "Ahmedabad Gujarat"
    assert normalize_location_text("  ") is None
    assert normalize_location_text("x" * 3000) == "x" * 2000


def test_ingestion_preserves_long_location_and_deduplicates_long_source_id(monkeypatch):
    import sys
    import types

    if "rq" not in sys.modules:
        rq_module = types.ModuleType("rq")
        rq_module.Queue = object
        rq_module.Worker = object
        sys.modules["rq"] = rq_module

    from app import worker
    from app.db.models import Report
    from app.db.session import SessionLocal, initialize_database
    from app.integrations import registry
    from app.integrations.base import NormalizedSignal

    initialize_database()
    raw_id = "external-record-" + ("very-long-provider-segment-" * 12)
    long_location = "District administrative location detail " * 18
    signal = NormalizedSignal(
        source_type="qa-feed",
        source_name="Quality Assurance Feed",
        source_record_id=raw_id,
        title="Flood warning near the river",
        description="External feed reports rising water levels.",
        disaster_type="flood",
        urgency="high",
        latitude=23.0225,
        longitude=72.5714,
        location_text=long_location,
        source_url="https://example.invalid/event/1?event=55&api_key=private",
    )

    class FakeConnector:
        name = "Quality Assurance Feed"
        source_type = "qa-feed"

        def configured(self):
            return True

        def fetch(self):
            return [signal]

    monkeypatch.setattr(registry, "connectors", lambda: [FakeConnector()])
    monkeypatch.setattr(worker, "enqueue_job", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(worker, "publish_event", lambda *_args, **_kwargs: None)

    first = worker.ingest_all_sources()
    second = worker.ingest_all_sources()
    assert first["inserted"] == 1
    assert second["inserted"] == 0

    with SessionLocal() as db:
        rows = db.query(Report).filter_by(source_type="qa-feed").all()
        assert len(rows) == 1
        assert rows[0].location_text == normalize_location_text(long_location)
        assert len(rows[0].source_record_id) == 180
        assert rows[0].source_url == "https://example.invalid/event/1?event=55"


def test_source_url_removes_userinfo_and_credential_query_parameters():
    value = (
        "https://operator:password@example.org/events/42?event=42&api_key=private"
        "&access_token=secret&lang=en#private-fragment"
    )
    assert sanitize_source_url(value) == "https://example.org/events/42?event=42&lang=en"
    assert sanitize_source_url("file:///etc/passwd") is None
    assert sanitize_source_url("https://example.org/" + ("x" * 800)) is None


def test_bhuvan_adapter_preserves_long_provider_id_for_central_normalization(monkeypatch):
    from app.core.config import settings
    from app.integrations.india import BhuvanGeoJSONFeed

    raw_id = "bhuvan-record-" + ("long-segment-" * 30)
    monkeypatch.setattr(settings, "bhuvan_geojson_url", "https://example.org/layer.geojson")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"features": [{
                "id": raw_id,
                "geometry": {"coordinates": [72.5, 23.0]},
                "properties": {"title": "Flood observation"},
            }]}

    from app.integrations import india
    monkeypatch.setattr(india.httpx, "get", lambda *_args, **_kwargs: FakeResponse())
    signal = BhuvanGeoJSONFeed().fetch()[0]
    assert signal.source_record_id == raw_id
    assert len(normalize_source_record_id(signal.source_record_id)) == 180


def test_authorized_social_adapter_preserves_long_provider_id(monkeypatch):
    from app.core.config import settings
    from app.integrations import social_api
    from app.integrations.social_api import AuthorizedSocialAPI

    raw_id = "social-event-" + ("provider-segment-" * 24)
    monkeypatch.setattr(settings, "social_api_base_url", "https://example.org/api/posts")
    monkeypatch.setattr(settings, "social_api_token", "test-token")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"posts": [{"id": raw_id, "text": "Flood conditions reported near the river."}]}

    monkeypatch.setattr(social_api.httpx, "get", lambda *_args, **_kwargs: FakeResponse())
    signal = AuthorizedSocialAPI().fetch()[0]
    assert signal.source_record_id == raw_id
    assert len(normalize_source_record_id(signal.source_record_id)) == 180


def test_worker_normalization_accepts_epoch_provider_timestamps():
    from types import SimpleNamespace

    from app import worker
    from app.integrations.base import NormalizedSignal

    epoch_ms = 1_793_696_700_000
    signal = NormalizedSignal(
        source_type="qa-epoch", source_name="QA", source_record_id="event-epoch",
        title="Flood observation", description="Rising water level.",
        disaster_type="flood", urgency="high", observed_at=epoch_ms,
    )
    connector = SimpleNamespace(source_type="qa-epoch", name="QA")
    normalized = worker._normalize_ingested_signal(signal, connector)
    assert normalized["source_observed_at"].tzinfo is not None
    assert normalized["source_observed_at"].year == 2026
