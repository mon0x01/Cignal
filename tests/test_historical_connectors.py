from datetime import UTC, datetime

import pytest

from app.integrations.eonet import NASAEONETEvents
from app.integrations.usgs import USGSEarthquakes


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.content = b"{}"

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_usgs_historical_fetch_uses_bounded_fdsn_query(monkeypatch):
    captured = {}

    def fake_get(url, *, params, timeout, headers):
        captured.update(url=url, params=params, timeout=timeout, headers=headers)
        return FakeResponse({
            "type": "FeatureCollection",
            "features": [{
                "id": "us7000history",
                "properties": {
                    "mag": 5.1, "place": "Test region", "status": "reviewed",
                    "time": 1767225600000, "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000history",
                    "magType": "mb",
                },
                "geometry": {"type": "Point", "coordinates": [72.5, 23.0, 10.0]},
            }],
        })

    monkeypatch.setattr("app.integrations.usgs.httpx.get", fake_get)
    connector = USGSEarthquakes()
    result = connector.fetch_window(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC),
        offset=1, limit=100,
    )
    assert captured["url"].endswith("/fdsnws/event/1/query")
    assert captured["params"]["format"] == "geojson"
    assert captured["params"]["orderby"] == "time-asc"
    assert captured["params"]["starttime"].startswith("2026-01-01")
    assert captured["params"]["endtime"].startswith("2026-01-02")
    assert captured["params"]["limit"] == 100
    assert len(result) == 1
    assert result[0].source_record_id == "us7000history"
    assert result[0].disaster_type == "earthquake"
    assert result[0].latitude == 23.0


def test_eonet_historical_fetch_uses_inclusive_date_range_and_marks_provenance(monkeypatch):
    captured = {}

    def fake_get(url, *, params, timeout, headers):
        captured.update(url=url, params=params)
        return FakeResponse({
            "events": [{
                "id": "EONET_123",
                "title": "Flooding in test region",
                "description": "A curated event description",
                "link": "https://eonet.gsfc.nasa.gov/api/v3/events/EONET_123",
                "categories": [{"id": "floods", "title": "Floods"}],
                "sources": [], "closed": None,
                "geometry": [{"date": "2026-01-01T00:00:00Z", "type": "Point", "coordinates": [72.5, 23.0]}],
            }],
        })

    monkeypatch.setattr("app.integrations.eonet.httpx.get", fake_get)
    result = NASAEONETEvents().fetch_window(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC),
    )
    assert captured["params"] == {"status": "all", "limit": 2000, "start": "2026-01-01", "end": "2026-01-03"}
    assert len(result) == 1
    assert result[0].provenance == "historical"
    assert result[0].disaster_type == "flood"
    assert result[0].latitude == 23.0


def test_eonet_provider_edits_keep_stable_event_identity_and_change_payload_revision(monkeypatch):
    payloads = [
        {
            "events": [{
                "id": "EONET_STABLE_1", "title": "Flooding in test region",
                "description": description, "link": "https://eonet.gsfc.nasa.gov/events/stable",
                "categories": [{"id": "floods", "title": "Floods"}],
                "sources": [], "closed": None,
                "geometry": [{"date": "2026-01-01T00:00:00Z", "type": "Point", "coordinates": [72.5, 23.0]}],
            }]
        }
        for description in ("Initial curated record", "Updated curated record with new context")
    ]

    def fake_get(url, *, params, timeout, headers):
        return FakeResponse(payloads.pop(0))

    monkeypatch.setattr("app.integrations.eonet.httpx.get", fake_get)
    connector = NASAEONETEvents()
    first = connector.fetch_window(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 23, tzinfo=UTC))[0]
    second = connector.fetch_window(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 23, tzinfo=UTC))[0]

    assert first.source_record_id == second.source_record_id == "EONET_STABLE_1"
    assert first.source_metadata["provider_payload_revision"] != second.source_metadata["provider_payload_revision"]


def test_eonet_rejects_more_than_fourteen_inclusive_calendar_dates():
    connector = NASAEONETEvents()
    with pytest.raises(ValueError, match="14 inclusive dates"):
        connector.fetch_window(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 15, 23, 59, tzinfo=UTC),
        )
