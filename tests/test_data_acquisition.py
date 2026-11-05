from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.integrations.eonet import NASAEONETEvents, _geometry_point
from app.integrations.sachet_cap import SachetCAPFeed
from app.integrations.source_catalog import (
    SOURCE_CATALOG_VERSION,
    build_report_source_metadata,
    source_descriptor_payload,
)

CAP_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>NDMA-123</identifier><sender>ndma@example.gov.in</sender>
  <sent>2026-11-05T03:00:00+00:00</sent><status>Actual</status>
  <msgType>Alert</msgType><scope>Public</scope>
  <info><language>en-IN</language><category>Met</category><event>Heavy Rainfall</event>
    <urgency>Immediate</urgency><severity>Severe</severity><certainty>Likely</certainty>
    <headline>Heavy rainfall warning</headline><description>Heavy rainfall expected in the district.</description>
    <instruction>Move away from low-lying areas.</instruction><expires>2026-11-05T12:00:00+00:00</expires>
    <area><areaDesc>District A</areaDesc><circle>23.0225,72.5714 10</circle></area>
  </info>
</alert>'''


class _MemoryCache:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        self.ttls[key] = ex
        return True


class _Response:
    def __init__(self, status_code, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def json(self):
        return json.loads(self.content.decode("utf-8"))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_source_catalog_is_explicit_and_records_dataset_terms_review_state():
    sachet = source_descriptor_payload("sachet_cap")
    eonet = source_descriptor_payload("nasa_eonet")
    assert sachet["catalog_version"] == SOURCE_CATALOG_VERSION
    assert sachet["license_status"] == "official_feed_terms_review_required"
    assert "ETag" in sachet["operational_note"]
    assert eonet["license_status"] == "source_dependent_metadata"
    assert "underlying linked sources" in eonet["license_name"]


def test_source_metadata_bounds_provider_fields_and_drops_secrets():
    result = build_report_source_metadata("nasa_eonet", {
        "event_id": "E-1",
        "api_key": "must-not-persist",
        "sources": [{"url": "https://example.org/feed?access_token=secret&item=7"}],
        "large": "x" * 1000,
    })
    provider = result["provider_record"]
    assert provider["event_id"] == "E-1"
    assert "api_key" not in provider
    assert provider["large"] == "x" * 500
    assert provider["sources"][0]["url"] == "https://example.org/feed?item=7"


def test_source_metadata_keeps_finite_numeric_values_and_drops_nonfinite_values():
    result = build_report_source_metadata("nasa_eonet", {
        "magnitude": 2.1,
        "invalid_numeric": float("nan"),
    })
    assert result["provider_record"]["magnitude"] == 2.1
    assert result["provider_record"]["invalid_numeric"] is None


def test_sachet_identifier_list_is_unique_bounded_and_semicolon_delimited():
    settings = Settings(sachet_cap_identifiers="feed-a; feed-b;feed-a;;" + ("x" * 257))
    assert settings.sachet_cap_identifier_list == ["feed-a", "feed-b"]


def test_eonet_maps_point_and_polygon_without_inventing_severity(monkeypatch):
    import app.integrations.eonet as module

    monkeypatch.setattr(module.settings, "nasa_eonet_enabled", True)
    payload = {"events": [
        {
            "id": "EONET_1", "title": "Wildfire in Region A", "description": "Thermal event.",
            "categories": [{"id": "wildfires", "title": "Wildfires"}],
            "sources": [{"id": "source-1", "title": "Publisher", "source": "https://example.org/event"}],
            "geometry": [{"date": "2026-11-05T02:00:00Z", "type": "Point", "coordinates": [72.5, 23.0]}],
            "magnitudeValue": 2.1, "magnitudeUnit": "index",
        },
        {
            "id": "EONET_2", "title": "Flood event", "categories": None, "sources": None,
            "geometry": [{"date": "2026-11-05T01:00:00Z", "type": "Polygon", "coordinates": [[[72, 22], [73, 22], [73, 23], [72, 23], [72, 22]]]}],
        },
    ]}
    monkeypatch.setattr(module.httpx, "get", lambda *args, **kwargs: _Response(200, json.dumps(payload).encode()))
    signals = NASAEONETEvents().fetch()
    assert len(signals) == 2
    assert signals[0].disaster_type == "fire"
    assert signals[0].urgency == "unknown"
    assert (signals[0].latitude, signals[0].longitude) == (23.0, 72.5)
    assert signals[0].source_metadata["sources"][0]["id"] == "source-1"
    assert signals[1].source_metadata["geometry_type"] == "Polygon"
    assert signals[1].latitude is not None and signals[1].longitude is not None
    assert len(signals[0].source_record_id.split(":")) == 2


def test_eonet_source_link_change_creates_a_new_record_revision(monkeypatch):
    import app.integrations.eonet as module

    monkeypatch.setattr(module.settings, "nasa_eonet_enabled", True)
    event = {
        "id": "EONET_REVISION", "title": "Flood event", "description": "Curated event.",
        "categories": [{"id": "floods", "title": "Floods"}],
        "geometry": [{"date": "2026-11-05T01:00:00Z", "type": "Point", "coordinates": [72.5, 23.0]}],
    }
    first_event = {**event, "sources": [{"id": "a", "title": "Source A", "source": "https://a.example/"}]}
    second_event = {**event, "sources": [{"id": "b", "title": "Source B", "source": "https://b.example/"}]}
    payloads = iter([{"events": [first_event]}, {"events": [second_event]}])
    monkeypatch.setattr(module.httpx, "get", lambda *args, **kwargs: _Response(200, json.dumps(next(payloads)).encode()))
    first = NASAEONETEvents().fetch()[0]
    second = NASAEONETEvents().fetch()[0]
    assert first.source_record_id != second.source_record_id


def test_sachet_parses_actual_cap_and_preserves_source_semantics():
    connector = SachetCAPFeed("feed-secret", cache=_MemoryCache())
    signals = connector._parse_alerts(CAP_XML, cache_state="modified_200")
    assert len(signals) == 1
    signal = signals[0]
    assert signal.source_type == "sachet_cap"
    assert signal.title == "Heavy rainfall warning"
    assert signal.disaster_type == "other"  # no flood inference from rain-only wording
    assert signal.urgency == "critical"
    assert signal.observed_at == datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
    assert signal.location_text == "District A"
    assert signal.source_metadata["cap_certainty"] == "Likely"
    assert signal.source_metadata["location_representation"] == "circle_center_approximation"
    assert "feed-secret" not in connector.name


def test_sachet_ignores_non_actual_and_ack_messages():
    xml = CAP_XML.replace(b"<status>Actual</status>", b"<status>Exercise</status>")
    assert SachetCAPFeed("id", cache=_MemoryCache())._parse_alerts(xml, cache_state="test") == []
    xml = CAP_XML.replace(b"<msgType>Alert</msgType>", b"<msgType>Ack</msgType>")
    assert SachetCAPFeed("id", cache=_MemoryCache())._parse_alerts(xml, cache_state="test") == []


def test_sachet_rejects_doctype_even_after_initial_xml_prefix():
    xml = b"<?xml version='1.0'?><!--" + (b"x" * 5000) + b"--><!DOCTYPE alert><alert/>"
    with pytest.raises(RuntimeError, match="document type or entity"):
        SachetCAPFeed("id", cache=_MemoryCache())._parse_alerts(xml, cache_state="test")


def test_sachet_rejects_utf16_doctype_declaration():
    xml = "<?xml version='1.0' encoding='UTF-16'?><!DOCTYPE alert><alert/>".encode("utf-16")
    with pytest.raises(RuntimeError, match="document type or entity"):
        SachetCAPFeed("id", cache=_MemoryCache())._parse_alerts(xml, cache_state="test")


def test_sachet_uses_etag_and_cached_body_on_http_304(monkeypatch):
    import app.integrations.sachet_cap as module

    cache = _MemoryCache()
    responses = iter([
        _Response(200, CAP_XML, {"ETag": 'W/"v1"'}),
        _Response(304),
    ])
    observed_headers = []

    def fake_get(*args, **kwargs):
        observed_headers.append(kwargs.get("headers", {}))
        return next(responses)

    monkeypatch.setattr(module.httpx, "get", fake_get)
    connector = SachetCAPFeed("opaque-id", cache=cache)
    first = connector.fetch()
    second = connector.fetch()
    assert len(first) == len(second) == 1
    assert first[0].source_metadata["cache_state"] == "modified_200"
    assert second[0].source_metadata["cache_state"] == "not_modified_cached"
    assert "If-None-Match" not in observed_headers[0]
    assert observed_headers[1]["If-None-Match"] == 'W/"v1"'
    assert all(ttl == 30 * 24 * 60 * 60 for ttl in cache.ttls.values())


def test_sachet_recovers_when_etag_exists_but_cached_body_was_evicted(monkeypatch):
    import app.integrations.sachet_cap as module

    cache = _MemoryCache()
    cache.values["cignal:sachet:etag:placeholder"] = b"old"
    # Seed real keys by deriving them exactly as the connector does.
    import hashlib
    digest = hashlib.sha256(b"opaque-id").hexdigest()
    cache.values[f"cignal:sachet:etag:{digest}"] = b'"old"'
    responses = iter([_Response(304), _Response(200, CAP_XML, {"ETag": '"new"'})])
    headers = []

    def fake_get(*args, **kwargs):
        headers.append(kwargs.get("headers", {}))
        return next(responses)

    monkeypatch.setattr(module.httpx, "get", fake_get)
    signals = SachetCAPFeed("opaque-id", cache=cache).fetch()
    assert len(signals) == 1
    assert "If-None-Match" in headers[0]
    assert "If-None-Match" not in headers[1]


def test_eonet_polygon_centroid_handles_antimeridian():
    lat, lon, kind = _geometry_point({
        "type": "Polygon",
        "coordinates": [[[179, 10], [-179, 10], [-179, 12], [179, 12], [179, 10]]],
    })
    assert kind == "Polygon"
    assert lat == pytest.approx(11.0)
    assert abs(abs(lon) - 180) < 1.0
