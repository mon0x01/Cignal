"""NDMA SACHET CAP-XML connector with mandatory ETag client-side caching."""
from __future__ import annotations

import hashlib
import logging
import xml.etree.ElementTree as ET

import httpx

from app.core.config import settings
from app.integrations.base import Connector, NormalizedSignal, parse_datetime_utc

logger = logging.getLogger("cignal.integrations.sachet")

SACHET_CAP_URL = "https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile"
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_XML_BYTES = 2 * 1024 * 1024
MAX_CAP_ALERTS = 500


class SachetCAPFeed(Connector):
    source_type = "sachet_cap"

    def __init__(self, identifier: str, *, cache=None):
        self.identifier = str(identifier or "").strip()
        digest = hashlib.sha256(self.identifier.encode("utf-8")).hexdigest()[:8]
        self.name = f"SACHET CAP XML feed ({digest})"
        self._cache_override = cache

    def configured(self) -> bool:
        return bool(self.identifier)

    def _cache_client(self):
        if self._cache_override is not None:
            return self._cache_override
        try:
            from redis import Redis
            return Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
        except Exception:
            return None

    def fetch(self) -> list[NormalizedSignal]:
        if not self.configured():
            return []
        xml_content, cache_state = self._fetch_cached_xml()
        return self._parse_alerts(xml_content, cache_state=cache_state)

    def _fetch_cached_xml(self) -> tuple[bytes, str]:
        identifier_hash = hashlib.sha256(self.identifier.encode("utf-8")).hexdigest()
        etag_key = f"cignal:sachet:etag:{identifier_hash}"
        body_key = f"cignal:sachet:body:{identifier_hash}"
        cache = self._cache_client()
        etag = cached_body = None
        if cache is not None:
            try:
                etag = cache.get(etag_key)
                cached_body = cache.get(body_key)
            except Exception as exc:
                logger.warning("SACHET cache read unavailable (%s); fetching without ETag", type(exc).__name__)
                etag = cached_body = None
        headers = {"Accept": "application/xml, text/xml", "User-Agent": "Cignal-Disaster-Intelligence/0.1"}
        if etag:
            headers["If-None-Match"] = _as_text(etag)
        response = httpx.get(
            SACHET_CAP_URL,
            params={"identifier": self.identifier},
            headers=headers,
            timeout=25,
        )
        if response.status_code == 304:
            if cached_body:
                body = _as_bytes(cached_body)
                if len(body) > MAX_XML_BYTES:
                    raise RuntimeError("Cached SACHET CAP XML exceeds the configured size limit")
                return body, "not_modified_cached"
            # Redis may have evicted the body while retaining the ETag. Recover with a
            # one-time unconditional request rather than losing the feed on HTTP 304.
            response = httpx.get(
                SACHET_CAP_URL,
                params={"identifier": self.identifier},
                headers={key: value for key, value in headers.items() if key != "If-None-Match"},
                timeout=25,
            )
            if response.status_code == 304:
                raise RuntimeError("SACHET returned HTTP 304 without a cached XML body")
        response.raise_for_status()
        body = bytes(response.content)
        if not body or len(body) > MAX_XML_BYTES:
            raise RuntimeError("SACHET CAP XML is empty or exceeds the configured size limit")
        new_etag = response.headers.get("ETag")
        if cache is not None and new_etag:
            try:
                # Store body and ETag together from the same 200 response. If either
                # write fails, the next request safely falls back to an unconditional GET.
                cache.set(body_key, body, ex=CACHE_TTL_SECONDS)
                cache.set(etag_key, new_etag, ex=CACHE_TTL_SECONDS)
            except Exception as exc:
                logger.warning("SACHET cache write unavailable (%s)", type(exc).__name__)
        return body, "modified_200"

    def _parse_alerts(self, xml_content: bytes, *, cache_state: str) -> list[NormalizedSignal]:
        # CAP feeds are normally UTF-8, but scan after stripping NUL bytes so a
        # UTF-16/UTF-32 DTD declaration cannot bypass the entity-expansion guard.
        upper = xml_content.replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise RuntimeError("SACHET CAP XML must not contain a document type or entity declaration")
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as exc:
            raise RuntimeError("SACHET returned malformed CAP XML") from exc
        alert_nodes = [node for node in root.iter() if _local_name(node.tag) == "alert"]
        output: list[NormalizedSignal] = []
        for alert in alert_nodes[:MAX_CAP_ALERTS]:
            status = _child_text(alert, "status") or "Unknown"
            if status.strip().lower() != "actual":
                continue
            message_type = (_child_text(alert, "msgType") or "Alert").strip()
            if message_type.lower() in {"ack", "error"}:
                continue
            sender = (_child_text(alert, "sender") or "unknown-sender").strip()[:100]
            alert_identifier = (_child_text(alert, "identifier") or "").strip()
            sent_value = _child_text(alert, "sent")
            sent_at = parse_datetime_utc(sent_value)
            if not alert_identifier or not sent_at:
                continue
            info = _preferred_info(alert)
            if info is None:
                continue
            event = (_child_text(info, "event") or "Disaster advisory").strip()
            headline = (_child_text(info, "headline") or event).strip()
            description = (_child_text(info, "description") or headline).strip()
            instruction = (_child_text(info, "instruction") or "").strip()
            if instruction:
                description = f"{description}\n\nOfficial instruction: {instruction}"
            urgency_value = (_child_text(info, "urgency") or "Unknown").strip()
            severity_value = (_child_text(info, "severity") or "Unknown").strip()
            certainty_value = (_child_text(info, "certainty") or "Unknown").strip()
            expires_value = _child_text(info, "expires")
            category_value = _child_text(info, "category")
            area = _first_child(info, "area")
            location_text, latitude, longitude, location_representation = _area_location(area)
            source_url = _child_text(info, "web")
            if message_type.lower() == "cancel":
                title = f"CANCELLED: {headline}"
                urgency = "low"
            else:
                title = headline
                urgency = _cap_urgency(urgency_value, severity_value)
            source_record_id = f"{sender}:{alert_identifier}:{sent_at.isoformat()}"
            metadata = {
                "cap_sender": sender,
                "cap_identifier": alert_identifier[:160],
                "cap_references": (_child_text(alert, "references") or "")[:500],
                "cap_status": status,
                "cap_message_type": message_type,
                "cap_severity": severity_value,
                "cap_urgency": urgency_value,
                "cap_certainty": certainty_value,
                "cap_expires": expires_value,
                "cap_category": category_value,
                "cache_state": cache_state,
                "location_representation": location_representation,
                "cancelled_message": message_type.lower() == "cancel",
            }
            output.append(NormalizedSignal(
                source_type=self.source_type,
                source_name=self.name,
                source_record_id=source_record_id,
                title=title[:180],
                description=description[:4000],
                disaster_type=_cap_disaster_type(event, category_value),
                urgency=urgency,
                latitude=latitude,
                longitude=longitude,
                location_text=location_text,
                source_url=source_url,
                provenance="live",
                observed_at=sent_at,
                source_metadata=metadata,
            ))
        return output


def _as_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _as_bytes(value) -> bytes:
    return value if isinstance(value, bytes) else str(value).encode("utf-8")


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _first_child(parent: ET.Element | None, name: str) -> ET.Element | None:
    if parent is None:
        return None
    return next((child for child in parent if _local_name(child.tag) == name), None)


def _child_text(parent: ET.Element | None, name: str) -> str | None:
    child = _first_child(parent, name)
    return " ".join("".join(child.itertext()).split()) if child is not None else None


def _preferred_info(alert: ET.Element) -> ET.Element | None:
    infos = [child for child in alert if _local_name(child.tag) == "info"]
    if not infos:
        return None
    return next(
        (item for item in infos if (_child_text(item, "language") or "").lower().startswith("en")),
        infos[0],
    )


def _area_location(area: ET.Element | None) -> tuple[str | None, float | None, float | None, str]:
    if area is None:
        return None, None, None, "unavailable"
    area_desc = _child_text(area, "areaDesc")
    for polygon_node in (child for child in area if _local_name(child.tag) == "polygon"):
        points = _parse_cap_polygon("".join(polygon_node.itertext()))
        if len(points) >= 3:
            lat, lon = _mean_lat_lon(points)
            return area_desc, lat, lon, "polygon_mean_point_approximation"
    for circle_node in (child for child in area if _local_name(child.tag) == "circle"):
        text = "".join(circle_node.itertext()).strip().split()
        if not text:
            continue
        point = _parse_cap_coordinate(text[0])
        if point:
            return area_desc, point[0], point[1], "circle_center_approximation"
    return area_desc, None, None, "area_description_only"


def _parse_cap_polygon(value: str) -> list[tuple[float, float]]:
    result = []
    for token in value.replace("\n", " ").split():
        point = _parse_cap_coordinate(token)
        if point:
            result.append(point)
    return result


def _parse_cap_coordinate(value: str) -> tuple[float, float] | None:
    parts = value.split(",")
    if len(parts) < 2:
        return None
    try:
        latitude, longitude = float(parts[0]), float(parts[1])
    except (TypeError, ValueError, OverflowError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def _mean_lat_lon(points: list[tuple[float, float]]) -> tuple[float, float]:
    # A mean point is intentionally labelled approximate; it is not a polygon centroid.
    latitude = sum(point[0] for point in points) / len(points)
    anchor = points[0][1]
    longitudes = []
    for _, longitude in points:
        while longitude - anchor > 180:
            longitude -= 360
        while longitude - anchor < -180:
            longitude += 360
        longitudes.append(longitude)
    longitude = sum(longitudes) / len(longitudes)
    return latitude, ((longitude + 180) % 360) - 180


def _cap_urgency(urgency: str, severity: str) -> str:
    urgency = urgency.lower()
    severity = severity.lower()
    if urgency == "immediate" or severity == "extreme":
        return "critical"
    if urgency == "expected" or severity == "severe":
        return "high"
    if urgency == "future" or severity == "moderate":
        return "moderate"
    if urgency == "past" or severity == "minor":
        return "low"
    return "unknown"


def _cap_disaster_type(event: str, category: str | None) -> str:
    text = f"{event} {category or ''}".lower()
    rules = (
        (("flood",), "flood"),
        (("earthquake", "seismic"), "earthquake"),
        (("cyclone", "hurricane", "typhoon", "tropical storm"), "cyclone"),
        (("landslide", "mudslide"), "landslide"),
        (("wildfire", "forest fire", "bushfire"), "fire"),
        (("tsunami",), "tsunami"),
        (("heatwave", "heat wave"), "heatwave"),
        (("drought",), "drought"),
    )
    for terms, disaster_type in rules:
        if any(term in text for term in terms):
            return disaster_type
    return "other"
