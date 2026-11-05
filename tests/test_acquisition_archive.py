from datetime import UTC, datetime

from app.db.models import AcquisitionObservation
from app.db.session import SessionLocal, initialize_database
from app.services.acquisition_archive import capture_observation


def _record(title="Earthquake M4.2 near Ahmedabad"):
    return {
        "source_type": "usgs",
        "source_name": "USGS Earthquake GeoJSON",
        "source_record_id": "us7000test",
        "title": title,
        "description": "Scientific earthquake observation; not an impact assessment.",
        "disaster_type": "earthquake",
        "urgency": "moderate",
        "latitude": 23.0,
        "longitude": 72.5,
        "location_text": "Ahmedabad region",
        "source_url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000test",
        "provenance": "historical",
        "source_observed_at": datetime(2026, 1, 1, tzinfo=UTC),
        "source_metadata": {"magnitude": 4.2, "historical_backfill": True},
    }


def _reset():
    initialize_database()
    with SessionLocal() as db:
        db.query(AcquisitionObservation).delete()
        db.commit()


def test_repeated_observation_capture_is_content_addressed_and_idempotent():
    _reset()
    with SessionLocal() as db:
        first = capture_observation(db, _record())
        db.commit()
        observation_id = first.id
        assert first.seen_count == 1

        repeated = capture_observation(db, _record())
        db.commit()
        assert repeated.id == observation_id
        assert repeated.seen_count == 2
        assert db.query(AcquisitionObservation).count() == 1


def test_provider_payload_revision_creates_immutable_observation_version():
    _reset()
    with SessionLocal() as db:
        first = capture_observation(db, _record())
        first_hash = first.content_sha256
        db.commit()

        revised = capture_observation(db, _record("Earthquake M4.3 near Ahmedabad"))
        db.commit()
        assert revised.id != first.id
        assert revised.content_sha256 != first_hash
        rows = db.query(AcquisitionObservation).order_by(AcquisitionObservation.first_seen_at).all()
        assert len(rows) == 2
        assert rows[0].normalized_payload["title"] == "Earthquake M4.2 near Ahmedabad"
        assert rows[1].normalized_payload["title"] == "Earthquake M4.3 near Ahmedabad"


def test_live_and_historical_eonet_records_share_one_report_identity_but_keep_observation_revisions():
    from types import SimpleNamespace

    from app.worker import _normalize_ingested_signal

    class EONETConnector:
        source_type = "nasa_eonet"
        name = "NASA EONET v3 natural events"

    live = SimpleNamespace(
        source_type="nasa_eonet", source_record_id="EONET_42:revision-a",
        title="Flooding in test region", description="Curated event record.",
        disaster_type="flood", urgency="unknown", latitude=23.0, longitude=72.5,
        location_text="Floods", source_url="https://eonet.gsfc.nasa.gov/events/EONET_42",
        provenance="live", observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_metadata={"eonet_event_id": "EONET_42", "provider_payload_revision": "revision-a"},
    )
    historical = SimpleNamespace(
        **{**live.__dict__, "source_record_id": "EONET_42", "provenance": "historical",
           "source_metadata": {"eonet_event_id": "EONET_42", "provider_payload_revision": "revision-b"}}
    )
    normalized_live = _normalize_ingested_signal(live, EONETConnector())
    normalized_historical = _normalize_ingested_signal(historical, EONETConnector())

    assert normalized_live["source_record_id"] == "EONET_42"
    assert normalized_historical["source_record_id"] == "EONET_42"
    _reset()
    with SessionLocal() as db:
        first = capture_observation(db, normalized_live)
        db.commit()
        second = capture_observation(db, normalized_historical)
        db.commit()
        assert first.id != second.id
        assert first.content_sha256 != second.content_sha256
        assert db.query(AcquisitionObservation).count() == 2


def test_live_and_historical_mode_do_not_create_duplicate_content_versions():
    _reset()
    live = _record()
    live["provenance"] = "live"
    with SessionLocal() as db:
        first = capture_observation(db, live)
        db.commit()
        historical = _record()
        historical["provenance"] = "historical"
        repeated = capture_observation(db, historical)
        db.commit()
        assert repeated.id == first.id
        assert repeated.seen_count == 2
        assert db.query(AcquisitionObservation).count() == 1
