"""NASA EONET v3 connector for curated natural-event metadata."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

import httpx

from app.core.config import settings
from app.integrations.base import Connector, NormalizedSignal, parse_datetime_utc

MAX_EONET_RESPONSE_BYTES = 5 * 1024 * 1024


class NASAEONETEvents(Connector):
    name = "NASA EONET v3 natural events"
    source_type = "nasa_eonet"
    url = "https://eonet.gsfc.nasa.gov/api/v3/events"

    def configured(self) -> bool:
        return bool(settings.nasa_eonet_enabled)

    def fetch(self) -> list[NormalizedSignal]:
        if not self.configured():
            return []
        return self._fetch({"status": "open", "limit": 200, "days": 30})

    def fetch_window(self, start_at: datetime, end_at: datetime) -> list[NormalizedSignal]:
        """Fetch a bounded inclusive EONET date interval (one provider page)."""
        if not self.configured():
            return []
        if start_at >= end_at:
            raise ValueError("start_at must be earlier than end_at")
        inclusive_days = (end_at.date() - start_at.date()).days + 1
        if inclusive_days > 14:
            raise ValueError("EONET historical windows are limited to 14 inclusive dates")
        return self._fetch({
            "status": "all", "limit": 2000,
            "start": start_at.date().isoformat(), "end": end_at.date().isoformat(),
        })

    def _fetch(self, params: dict) -> list[NormalizedSignal]:
        response = httpx.get(
            self.url,
            params=params,
            timeout=25,
            headers={"Accept": "application/json", "User-Agent": "Cignal-Disaster-Intelligence/0.1"},
        )
        response.raise_for_status()
        if len(response.content) > MAX_EONET_RESPONSE_BYTES:
            raise RuntimeError("NASA EONET response exceeds the configured size limit")
        payload = response.json()
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list):
            raise RuntimeError("NASA EONET returned an invalid events response")

        output: list[NormalizedSignal] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("id") or "").strip()
            title = " ".join(str(event.get("title") or "").split())
            if not event_id or not title:
                continue
            raw_categories = event.get("categories")
            raw_sources = event.get("sources")
            categories = [
                {"id": str(item.get("id") or "")[:80], "title": str(item.get("title") or "")[:120]}
                for item in (raw_categories if isinstance(raw_categories, list) else [])
                if isinstance(item, dict)
            ][:12]
            sources = [
                {
                    "id": str(item.get("id") or "")[:80],
                    "title": str(item.get("title") or "")[:120],
                    "url": str(item.get("source") or "")[:500],
                }
                for item in (raw_sources if isinstance(raw_sources, list) else [])
                if isinstance(item, dict)
            ][:12]
            geometry = _latest_geometry(event.get("geometry"))
            latitude, longitude, geometry_type = _geometry_point(geometry)
            observed_at = parse_datetime_utc(geometry.get("date")) if geometry else None
            description = " ".join(str(event.get("description") or "").split())
            if not description:
                description = "NASA EONET curated natural-event metadata. Consult the linked source records for event-specific context."
            fingerprint_material = json.dumps(
                {
                    "title": title,
                    "description": description,
                    "observed_at": observed_at.isoformat() if observed_at else None,
                    "geometry": geometry,
                    "categories": categories,
                    "sources": sources,
                    "closed": event.get("closed"),
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            revision = hashlib.sha256(fingerprint_material.encode("utf-8")).hexdigest()[:16]
            # Live polling keeps the established versioned source key so a changed
            # curated event payload becomes a distinct report revision. Historical
            # backfills use the stable provider event identity: the immutable
            # observation ledger captures content revisions without fragmenting the
            # historical event into multiple reports.
            source_record_id = (
                event_id[:140]
                if params.get("status") == "all"
                else f"{event_id[:140]}:{revision}"
            )
            category_names = [item["title"] for item in categories if item["title"]]
            disaster_type = _event_disaster_type(category_names, title)
            source_url = event.get("link") or f"https://eonet.gsfc.nasa.gov/api/v3/events/{event_id}"
            magnitude = event.get("magnitudeValue")
            magnitude_unit = event.get("magnitudeUnit")
            output.append(NormalizedSignal(
                source_type=self.source_type,
                source_name=self.name,
                source_record_id=source_record_id,
                title=title[:180],
                description=description[:4000],
                disaster_type=disaster_type,
                urgency="unknown",  # EONET curation is not an impact/severity assessment.
                latitude=latitude,
                longitude=longitude,
                location_text="; ".join(category_names[:4]) or None,
                source_url=source_url,
                provenance="historical" if params.get("status") == "all" else "live",
                observed_at=observed_at,
                source_metadata={
                    "eonet_event_id": event_id[:140],
                    "provider_payload_revision": revision,
                    "event_closed_at": str(event.get("closed") or "")[:50] or None,
                    "categories": categories,
                    "sources": sources,
                    "geometry_type": geometry_type,
                    "magnitude_value": magnitude if isinstance(magnitude, (int, float)) else None,
                    "magnitude_unit": str(magnitude_unit or "")[:40] or None,
                    "location_representation": "polygon_centroid" if geometry_type == "Polygon" else "point" if geometry_type == "Point" else "unavailable",
                },
            ))
        return output


def _latest_geometry(items) -> dict | None:
    if not isinstance(items, list):
        return None
    candidates = [item for item in items if isinstance(item, dict) and item.get("type") in {"Point", "Polygon"}]
    if not candidates:
        return None
    return max(candidates, key=lambda item: str(item.get("date") or ""))


def _geometry_point(geometry: dict | None) -> tuple[float | None, float | None, str | None]:
    if not geometry:
        return None, None, None
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Point":
        point = _valid_lon_lat(coordinates)
        if point:
            return point[1], point[0], "Point"
        return None, None, "Point"
    if geometry_type == "Polygon" and isinstance(coordinates, list) and coordinates:
        ring = coordinates[0]
        points = [_valid_lon_lat(point) for point in ring if isinstance(point, (list, tuple))]
        valid_points = [point for point in points if point]
        if len(valid_points) >= 3:
            latitude, longitude = _polygon_centroid(valid_points)
            if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                return latitude, longitude, "Polygon"
        return None, None, "Polygon"
    return None, None, str(geometry_type or "unknown")[:40]


def _valid_lon_lat(value) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        longitude, latitude = float(value[0]), float(value[1])
    except (TypeError, ValueError, OverflowError):
        return None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None
    return longitude, latitude


def _polygon_centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Return a planar centroid after unwrapping longitudes across the dateline."""
    anchor = points[0][0]
    unwrapped = []
    for longitude, latitude in points:
        while longitude - anchor > 180:
            longitude -= 360
        while longitude - anchor < -180:
            longitude += 360
        unwrapped.append((longitude, latitude))
    if unwrapped[0] != unwrapped[-1]:
        unwrapped.append(unwrapped[0])
    twice_area = 0.0
    centroid_x = 0.0
    centroid_y = 0.0
    for (x1, y1), (x2, y2) in zip(unwrapped, unwrapped[1:], strict=False):
        cross = x1 * y2 - x2 * y1
        twice_area += cross
        centroid_x += (x1 + x2) * cross
        centroid_y += (y1 + y2) * cross
    if abs(twice_area) < 1e-12:
        longitude = sum(point[0] for point in unwrapped[:-1]) / max(1, len(unwrapped) - 1)
        latitude = sum(point[1] for point in unwrapped[:-1]) / max(1, len(unwrapped) - 1)
    else:
        longitude = centroid_x / (3 * twice_area)
        latitude = centroid_y / (3 * twice_area)
    longitude = ((longitude + 180) % 360) - 180
    return latitude, longitude


def _event_disaster_type(categories: list[str], title: str) -> str:
    text = " ".join([*categories, title]).lower()
    rules = (
        (("wildfire", "wild fires", "fire"), "fire"),
        (("flood",), "flood"),
        (("earthquake",), "earthquake"),
        (("landslide",), "landslide"),
        (("tropical cyclone", "hurricane", "typhoon", "cyclone", "severe storm"), "cyclone"),
    )
    for terms, disaster_type in rules:
        if any(term in text for term in terms):
            return disaster_type
    return "other"
