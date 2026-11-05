from datetime import datetime

import httpx

from app.integrations.base import Connector, NormalizedSignal, utc_from_millis


class USGSEarthquakes(Connector):
    name = "USGS Earthquake GeoJSON"
    source_type = "usgs"
    url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"

    def configured(self) -> bool:
        return True

    def fetch_window(
        self, start_at: datetime, end_at: datetime, *, offset: int = 1, limit: int = 500
    ) -> list[NormalizedSignal]:
        """Fetch one bounded historical page from the USGS FDSN event service."""
        if start_at >= end_at:
            raise ValueError("start_at must be earlier than end_at")
        if not 1 <= offset <= 20_000 or not 1 <= limit <= 20_000:
            raise ValueError("USGS offset and limit must be within 1..20000")
        response = httpx.get(
            "https://earthquake.usgs.gov/fdsnws/event/1/query",
            params={
                "format": "geojson", "starttime": start_at.isoformat(),
                "endtime": end_at.isoformat(), "orderby": "time-asc",
                "limit": limit, "offset": offset,
            },
            timeout=30,
            headers={"Accept": "application/geo+json", "User-Agent": "Cignal-Disaster-Intelligence/0.1"},
        )
        response.raise_for_status()
        payload = response.json()
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list):
            raise RuntimeError("USGS historical query returned an invalid GeoJSON feature collection")
        if len(features) > limit:
            raise RuntimeError("USGS historical page exceeded requested limit")
        signals: list[NormalizedSignal] = []
        for feature in features:
            if not isinstance(feature, dict):
                continue
            props = feature.get("properties") or {}
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            event_id = str(feature.get("id") or "").strip()
            if not event_id or len(coords) < 2:
                continue
            try:
                magnitude = float(props.get("mag") or 0)
                longitude, latitude = float(coords[0]), float(coords[1])
            except (TypeError, ValueError, OverflowError):
                continue
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                continue
            severity = "critical" if magnitude >= 6.5 else "high" if magnitude >= 5.5 else "moderate" if magnitude >= 4 else "low"
            signals.append(NormalizedSignal(
                source_type=self.source_type, source_name=self.name,
                source_record_id=event_id,
                title=f"Earthquake M{magnitude:.1f}: {props.get('place') or 'Location unavailable'}",
                description=(
                    f"USGS earthquake event. Magnitude {magnitude:.1f}. "
                    f"Status: {props.get('status', 'unknown')}. This is a scientific event feed, not an impact assessment."
                ),
                disaster_type="earthquake", urgency=severity,
                latitude=latitude, longitude=longitude, location_text=props.get("place"),
                source_url=props.get("url"), observed_at=utc_from_millis(props.get("time", 0)),
                source_metadata={
                    "catalog": "USGS FDSN Event Web Service",
                    "magnitude": magnitude,
                    "magnitude_type": props.get("magType"),
                    "event_status": props.get("status"),
                    "review_status": props.get("status"),
                    "historical_backfill": True,
                },
            ))
        return signals

    def fetch(self) -> list[NormalizedSignal]:
        response = httpx.get(self.url, timeout=20, headers={"User-Agent": "Cignal-Disaster-Intelligence/0.1 (+open-source)"})
        response.raise_for_status()
        signals = []
        for feature in response.json().get("features", []):
            props = feature.get("properties", {})
            coords = feature.get("geometry", {}).get("coordinates", [])
            if len(coords) < 2:
                continue
            magnitude = float(props.get("mag") or 0)
            severity = "critical" if magnitude >= 6.5 else "high" if magnitude >= 5.5 else "moderate" if magnitude >= 4 else "low"
            signals.append(NormalizedSignal(
                source_type=self.source_type, source_name=self.name,
                source_record_id=str(feature.get("id")),
                title=f"Earthquake M{magnitude:.1f}: {props.get('place') or 'Location unavailable'}",
                description=f"USGS earthquake event. Magnitude {magnitude:.1f}. Status: {props.get('status', 'unknown')}. This is a scientific event feed, not an impact assessment.",
                disaster_type="earthquake", urgency=severity,
                latitude=float(coords[1]), longitude=float(coords[0]), location_text=props.get("place"),
                source_url=props.get("url"), observed_at=utc_from_millis(props.get("time", 0)),
            ))
        return signals
