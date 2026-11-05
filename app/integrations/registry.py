from app.core.config import settings
from app.integrations.authorized_rss import AuthorizedRSSFeeds
from app.integrations.eonet import NASAEONETEvents
from app.integrations.firms import NASAFirms
from app.integrations.gdacs import GDACSFeed
from app.integrations.india import BhuvanGeoJSONFeed, IMDConfiguredAPI, IndiaGovernmentJSONAPI
from app.integrations.open_meteo import OpenMeteoWeather
from app.integrations.sachet_cap import SachetCAPFeed
from app.integrations.social_api import AuthorizedSocialAPI
from app.integrations.source_catalog import source_descriptor_payload
from app.integrations.usgs import USGSEarthquakes


def connectors():
    all_connectors = [
        USGSEarthquakes(), GDACSFeed(), OpenMeteoWeather(), IMDConfiguredAPI(),
        IndiaGovernmentJSONAPI(), BhuvanGeoJSONFeed(), NASAFirms(),
        AuthorizedRSSFeeds(), AuthorizedSocialAPI(), NASAEONETEvents(),
        *(SachetCAPFeed(identifier) for identifier in settings.sachet_cap_identifier_list),
    ]
    filtered = []
    for item in all_connectors:
        if item.source_type == "usgs" and not settings.usgs_enabled:
            continue
        if item.source_type == "gdacs" and not settings.gdacs_enabled:
            continue
        if item.source_type == "open_meteo" and not settings.open_meteo_enabled:
            continue
        if item.source_type == "nasa_eonet" and not settings.nasa_eonet_enabled:
            continue
        filtered.append(item)
    return filtered


def connector_statuses() -> list[dict]:
    result = []
    for connector in connectors():
        item = connector.status()
        item["mode"] = "ready" if item["configured"] else "credentials_or_configuration_required"
        item["note"] = {
            "usgs": "Public GeoJSON feed; event observations are not damage assessments.",
            "gdacs": "Public RSS feed; availability depends on the upstream service.",
            "open_meteo": "Free non-commercial use; attribution required. Forecast context, not an incident feed.",
            "imd": "Requires an IMD account/token and the exact endpoint enabled for that account.",
            "india_gov": "Configured NDMA/state-authority JSON endpoint; operator must provide an authorised endpoint and its exact field schema.",
            "bhuvan": "Configured Bhuvan/Indian geospatial GeoJSON endpoint; requires a permitted endpoint and dataset metadata.",
            "nasa_firms": "Requires free NASA FIRMS MAP_KEY registration.",
            "authorized_rss": "Only add feeds you are authorized to access and process.",
            "social_api": "Generic adapter for authorized provider APIs; no scraping or undocumented endpoints.",
            "nasa_eonet": "Curated near-real-time natural-event metadata; not an impact or severity assessment.",
            "sachet_cap": "Official CAP warning feed; configured identifier required; conditional ETag caching enabled.",
        }.get(item["source_type"], "")
        item["source_governance"] = source_descriptor_payload(item["source_type"])
        result.append(item)
    return result
