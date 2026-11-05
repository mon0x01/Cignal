"""Versioned source-governance metadata for Cignal's external data providers.

This catalog records how a connector is allowed to access and retain data. It is
not a legal opinion: operator-configured feeds still require review of the
specific provider terms and any source-level restrictions.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from app.integrations.base import sanitize_source_url

SOURCE_CATALOG_VERSION = "2026-11-05.1"


@dataclass(frozen=True)
class SourceDescriptor:
    source_type: str
    provider: str
    dataset: str
    data_scope: str
    access_mode: str
    license_status: str
    license_name: str
    license_url: str | None
    terms_url: str | None
    attribution: str
    retention_policy: str
    operational_note: str

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["catalog_version"] = SOURCE_CATALOG_VERSION
        return value


_SOURCE_CATALOG: dict[str, SourceDescriptor] = {
    "citizen": SourceDescriptor(
        source_type="citizen",
        provider="Cignal citizen reporting channel",
        dataset="User-submitted disaster observations and evidence references",
        data_scope="first-party citizen reports; submissions may be unverified or mistaken",
        access_mode="first_party_submission",
        license_status="consent_scope_review_required",
        license_name="User-submitted content; consent, privacy notice and retention scope must be explicit",
        license_url=None,
        terms_url=None,
        attribution="Submitting user where attribution is permitted; anonymity choices remain respected",
        retention_policy="purpose_limited_user_submitted_content",
        operational_note="Submission is a lead for human review, not a confirmed emergency or ground-truth label.",
    ),
    "usgs": SourceDescriptor(
        source_type="usgs",
        provider="U.S. Geological Survey",
        dataset="USGS Earthquake Hazards Program GeoJSON feeds",
        data_scope="earthquake event observations; not impact or damage assessments",
        access_mode="public_api",
        license_status="public_government_data",
        license_name="USGS data and information; verify third-party material separately",
        license_url="https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
        terms_url="https://www.usgs.gov/programs/earthquake-hazards/earthquakes",
        attribution="U.S. Geological Survey (USGS)",
        retention_policy="normalized_report_metadata",
        operational_note="Provider event time and reported magnitude are preserved as source observations.",
    ),
    "gdacs": SourceDescriptor(
        source_type="gdacs",
        provider="Global Disaster Alert and Coordination System",
        dataset="GDACS alerts and event metadata",
        data_scope="global multi-hazard alert metadata",
        access_mode="public_api_or_feed",
        license_status="attribution_required_terms_review",
        license_name="GDACS Terms of Use",
        license_url=None,
        terms_url="https://www.gdacs.org/documents/2025/GDACS_Terms_of_use_Mar_25.pdf",
        attribution="Global Disaster Alert and Coordination System (GDACS)",
        retention_policy="normalized_report_metadata",
        operational_note="GDACS requests source acknowledgement; alert level is not an impact assessment.",
    ),
    "nasa_eonet": SourceDescriptor(
        source_type="nasa_eonet",
        provider="NASA Earth Observatory",
        dataset="Earth Observatory Natural Event Tracker (EONET) v3",
        data_scope="curated near-real-time natural-event metadata and links to source imagery",
        access_mode="public_api",
        license_status="source_dependent_metadata",
        license_name="NASA EONET metadata; underlying linked sources may have separate terms",
        license_url=None,
        terms_url="https://eonet.gsfc.nasa.gov/docs/v3",
        attribution="NASA Earth Observatory Natural Event Tracker (EONET)",
        retention_policy="normalized_report_metadata",
        operational_note="EONET event boundaries and curation are source-defined; linked imagery is not copied.",
    ),
    "open_meteo": SourceDescriptor(
        source_type="open_meteo",
        provider="Open-Meteo",
        dataset="Weather forecast and current conditions API",
        data_scope="weather context; not a report or incident feed",
        access_mode="public_api",
        license_status="attribution_and_noncommercial_terms",
        license_name="CC BY 4.0 attribution; free-access terms apply",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        terms_url="https://open-meteo.com/en/terms",
        attribution="Weather data by Open-Meteo.com",
        retention_policy="context_only_no_incident_report",
        operational_note="Forecast values are contextual indicators, never evidence that an incident occurred.",
    ),
    "imd": SourceDescriptor(
        source_type="imd",
        provider="India Meteorological Department",
        dataset="IMD weather observations, forecasts and warnings APIs",
        data_scope="official India meteorological warnings and weather observations",
        access_mode="account_token_api",
        license_status="account_terms_review_required",
        license_name="IMD API account and dataset-specific terms",
        license_url=None,
        terms_url="https://api.imd.gov.in/public/",
        attribution="India Meteorological Department (IMD), Ministry of Earth Sciences, Government of India",
        retention_policy="operator_authorized_normalized_data",
        operational_note="Requires an IMD account/token and an endpoint enabled for that account.",
    ),
    "india_gov": SourceDescriptor(
        source_type="india_gov",
        provider="Configured Indian government authority",
        dataset="Operator-configured government disaster/advisory JSON endpoint",
        data_scope="authority-specific alerts and advisories",
        access_mode="configured_authenticated_endpoint",
        license_status="dataset_specific_license_review",
        license_name="Verify the individual dataset licence; data.gov.in datasets may use Government Open Data License - India",
        license_url="https://data.gov.in/government-open-data-license-india",
        terms_url="https://www.data.gov.in/",
        attribution="The publishing Government of India ministry, department or state authority",
        retention_policy="operator_authorized_normalized_data",
        operational_note="The operator must verify endpoint authorization, schema and dataset-specific terms.",
    ),
    "bhuvan": SourceDescriptor(
        source_type="bhuvan",
        provider="ISRO / Bhuvan or configured geospatial publisher",
        dataset="Operator-configured Indian geospatial GeoJSON layer",
        data_scope="geospatial features; semantics depend on the selected layer",
        access_mode="configured_endpoint",
        license_status="dataset_specific_license_review",
        license_name="Bhuvan layer and service-specific terms",
        license_url=None,
        terms_url="https://bhuvan.nrsc.gov.in/",
        attribution="The Bhuvan/ISRO dataset publisher identified by the operator",
        retention_policy="operator_authorized_normalized_data",
        operational_note="Do not infer disaster occurrence from a hazard layer or susceptibility map.",
    ),
    "nasa_firms": SourceDescriptor(
        source_type="nasa_firms",
        provider="NASA FIRMS",
        dataset="Fire Information for Resource Management System active fire detections",
        data_scope="satellite thermal anomaly / active-fire detections",
        access_mode="api_key",
        license_status="provider_terms_and_product_caveats",
        license_name="NASA FIRMS data use and product-specific terms",
        license_url=None,
        terms_url="https://firms.modaps.eosdis.nasa.gov/",
        attribution="NASA Fire Information for Resource Management System (FIRMS)",
        retention_policy="normalized_report_metadata",
        operational_note="A hotspot is a satellite detection, not a confirmed wildfire or ground-truth assessment.",
    ),
    "authorized_rss": SourceDescriptor(
        source_type="authorized_rss",
        provider="Operator-configured RSS publishers",
        dataset="Explicitly configured RSS/Atom feeds",
        data_scope="publisher-defined public or authorized feed items",
        access_mode="operator_configured_feed",
        license_status="operator_review_required",
        license_name="Publisher-specific feed terms and content rights",
        license_url=None,
        terms_url=None,
        attribution="The publisher identified by each configured feed",
        retention_policy="operator_authorized_normalized_data",
        operational_note="Only feeds the operator is permitted to access and process should be configured.",
    ),
    "social_api": SourceDescriptor(
        source_type="social_api",
        provider="Operator-configured social platform API",
        dataset="Authorized provider API records",
        data_scope="provider-authorized posts and metadata",
        access_mode="authorized_api_token",
        license_status="provider_terms_and_consent_review",
        license_name="Platform API terms and applicable content rights",
        license_url=None,
        terms_url=None,
        attribution="The platform/provider and original account as permitted by its terms",
        retention_policy="provider_terms_controlled_minimum_data",
        operational_note="No scraping, undocumented endpoints or access-control bypass is used.",
    ),
    "sachet_cap": SourceDescriptor(
        source_type="sachet_cap",
        provider="National Disaster Management Authority, Government of India",
        dataset="SACHET CAP-XML public alert feed",
        data_scope="official Common Alerting Protocol alert messages",
        access_mode="operator_configured_identifier_feed",
        license_status="official_feed_terms_review_required",
        license_name="SACHET/NDMA feed terms; operator must confirm its subscription/feed authorization",
        license_url=None,
        terms_url="https://sachet.ndma.gov.in/CapFeed",
        attribution="SACHET National Disaster Alert Portal, National Disaster Management Authority, Government of India",
        retention_policy="normalized_alert_metadata_and_30_day_etag_cache",
        operational_note="The NDMA integration guide requires ETag caching and correct HTTP 200/304 handling. Only Actual CAP messages are ingested.",
    ),
}


def source_descriptor(source_type: str) -> SourceDescriptor | None:
    return _SOURCE_CATALOG.get(str(source_type or "").strip().lower())


def source_descriptor_payload(source_type: str) -> dict[str, Any]:
    descriptor = source_descriptor(source_type)
    return descriptor.as_dict() if descriptor else {
        "source_type": str(source_type or "unknown")[:24],
        "catalog_version": SOURCE_CATALOG_VERSION,
        "license_status": "unregistered_source_review_required",
        "retention_policy": "do_not_expand_without_source_review",
    }


def _bounded_metadata(value: Any, *, depth: int = 0) -> Any:
    """Convert provider metadata into a small JSON-safe tree with strict bounds."""
    if depth >= 3:
        if isinstance(value, (dict, list, tuple)):
            return "[nested_metadata_truncated]"
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return str(value)[:200]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:24]:
            normalized_key = str(key)[:64]
            lowered_key = normalized_key.lower()
            if (
                lowered_key in {"token", "access_token", "api_key", "password", "secret", "authorization"}
                or lowered_key.endswith(("_token", "_key", "_secret", "_password", "_credential"))
            ):
                continue
            if lowered_key in {"url", "source_url", "href", "source"} and isinstance(item, str):
                safe_url = sanitize_source_url(item)
                if safe_url:
                    result[normalized_key] = safe_url
                continue
            result[normalized_key] = _bounded_metadata(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_bounded_metadata(item, depth=depth + 1) for item in value[:16]]
    return str(value)[:200]


def build_report_source_metadata(source_type: str, provider_metadata: dict | None = None) -> dict[str, Any]:
    """Bind catalog provenance to each stored source report without archiving raw payloads."""
    result = source_descriptor_payload(source_type)
    result["retrieved_at"] = datetime.now(UTC).isoformat()
    if provider_metadata:
        bounded = _bounded_metadata(provider_metadata)
        if isinstance(bounded, dict):
            result["provider_record"] = bounded
    return result
