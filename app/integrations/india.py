"""IMD adapter. IMD API portal registration/token access is required where endpoints are protected.

Set IMD_API_BASE_URL to the exact API endpoint granted to your account and IMD_API_TOKEN.
The adapter deliberately does not guess undocumented endpoint paths or scrape portal pages.
"""
import hashlib
import json

import httpx

from app.core.config import settings
from app.integrations.base import Connector, NormalizedSignal, parse_datetime_utc


class IMDConfiguredAPI(Connector):
    name = "India Meteorological Department API"
    source_type = "imd"

    def configured(self) -> bool:
        return bool(settings.imd_api_base_url and settings.imd_api_token)

    def fetch(self) -> list[NormalizedSignal]:
        if not self.configured():
            return []
        response = httpx.get(settings.imd_api_base_url, headers={"Authorization": f"Bearer {settings.imd_api_token}", "Accept": "application/json"}, timeout=20)
        response.raise_for_status()
        payload = response.json()
        rows = payload if isinstance(payload, list) else payload.get("alerts", payload.get("data", []))
        output = []
        for row in rows if isinstance(rows, list) else []:
            output.append(NormalizedSignal(
                source_type=self.source_type, source_name=self.name,
                source_record_id=str(row.get("id") or row.get("warning_id") or row.get("reference") or hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()),
                title=str(row.get("title") or row.get("event") or row.get("warning") or "IMD weather warning")[:180],
                description=str(row.get("description") or row.get("message") or row.get("areaDesc") or "Official IMD warning feed record")[:4000],
                disaster_type="cyclone" if "cyclone" in str(row).lower() else "flood" if "rain" in str(row).lower() else "other",
                urgency=str(row.get("severity") or "moderate").lower(),
                latitude=_optional_float(row.get("latitude")), longitude=_optional_float(row.get("longitude")),
                location_text=row.get("district") or row.get("areaDesc") or row.get("location"),
                source_url=row.get("url"), provenance="live",
                observed_at=parse_datetime_utc(row.get("observed_at") or row.get("issued_at") or row.get("timestamp")),
            ))
        return output


def _optional_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class IndiaGovernmentJSONAPI(Connector):
    """Adapter for an NDMA/state-authority API endpoint explicitly configured by the operator."""
    name = "India government / NDMA configured API"
    source_type = "india_gov"

    def configured(self) -> bool:
        return bool(settings.india_gov_api_base_url)

    def fetch(self) -> list[NormalizedSignal]:
        if not self.configured():
            return []
        headers = {"Accept": "application/json"}
        if settings.india_gov_api_token:
            headers["Authorization"] = f"Bearer {settings.india_gov_api_token}"
        response = httpx.get(settings.india_gov_api_base_url, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
        rows = payload if isinstance(payload, list) else payload.get("alerts", payload.get("data", []))
        output = []
        for row in rows if isinstance(rows, list) else []:
            record = json.dumps(row, sort_keys=True, ensure_ascii=False)
            output.append(NormalizedSignal(
                source_type=self.source_type, source_name=str(row.get("source") or self.name)[:100],
                source_record_id=str(row.get("id") or hashlib.sha256(record.encode()).hexdigest()),
                title=str(row.get("title") or row.get("event") or "India government disaster advisory")[:180],
                description=str(row.get("description") or row.get("message") or "Official government advisory record")[:4000],
                disaster_type=str(row.get("disaster_type") or "other")[:32].lower(),
                urgency=str(row.get("severity") or "unknown")[:16].lower(),
                latitude=_optional_float(row.get("latitude")), longitude=_optional_float(row.get("longitude")),
                location_text=row.get("district") or row.get("area") or row.get("location"),
                source_url=row.get("url"), provenance="live",
                observed_at=parse_datetime_utc(row.get("observed_at") or row.get("issued_at") or row.get("timestamp")),
            ))
        return output


class BhuvanGeoJSONFeed(Connector):
    """Optional configured GeoJSON layer; the operator supplies a permitted endpoint."""
    name = "Bhuvan / configured Indian geospatial GeoJSON"
    source_type = "bhuvan"

    def configured(self) -> bool:
        return bool(settings.bhuvan_geojson_url)

    def fetch(self) -> list[NormalizedSignal]:
        if not self.configured():
            return []
        headers = {"Accept": "application/geo+json, application/json"}
        if settings.bhuvan_api_token:
            headers["Authorization"] = f"Bearer {settings.bhuvan_api_token}"
        response = httpx.get(settings.bhuvan_geojson_url, headers=headers, timeout=25)
        response.raise_for_status()
        payload = response.json()
        output = []
        for feature in payload.get("features", []) if isinstance(payload, dict) else []:
            properties = feature.get("properties") or {}
            coordinates = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coordinates) < 2:
                continue
            lon, lat = _optional_float(coordinates[0]), _optional_float(coordinates[1])
            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            record_id = feature.get("id") or properties.get("id") or hashlib.sha256(json.dumps(feature, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            title = str(properties.get("title") or properties.get("name") or "Indian geospatial dataset feature")[:180]
            description = str(properties.get("description") or properties.get("class") or "Imported geospatial feature; interpret using the source dataset metadata.")[:4000]
            output.append(NormalizedSignal(
                source_type=self.source_type, source_name=self.name, source_record_id=str(record_id),
                title=title, description=description, disaster_type=str(properties.get("disaster_type") or "other")[:32].lower(),
                urgency="unknown", latitude=lat, longitude=lon,
                location_text=str(properties.get("location") or properties.get("district") or "")[:220] or None,
                source_url=settings.bhuvan_geojson_url, provenance="live",
            ))
        return output
