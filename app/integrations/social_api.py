"""Configurable adapter for officially permitted social-platform API providers.

No scraping, login automation, access-control bypass or undocumented endpoints are used.
The configured provider must return a JSON object with a `posts` list. Each item should
include id, text, url, created_at, latitude, longitude, and location where available.
"""
import httpx

from app.core.config import settings
from app.integrations.base import Connector, NormalizedSignal, parse_datetime_utc


class AuthorizedSocialAPI(Connector):
    name = "Authorized social-platform API adapter"
    source_type = "social_api"

    def configured(self) -> bool:
        return bool(settings.social_api_base_url and settings.social_api_token)

    def fetch(self) -> list[NormalizedSignal]:
        if not self.configured():
            return []
        response = httpx.get(
            settings.social_api_base_url,
            headers={"Authorization": f"Bearer {settings.social_api_token}", "Accept": "application/json"},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        posts = payload.get("posts", []) if isinstance(payload, dict) else []
        output = []
        for post in posts if isinstance(posts, list) else []:
            post_id = post.get("id")
            text = str(post.get("text") or post.get("content") or "")
            if not post_id or len(text.strip()) < 10:
                continue
            output.append(NormalizedSignal(
                source_type=self.source_type, source_name=str(post.get("provider") or self.name)[:100],
                source_record_id=str(post_id), title=text[:160], description=text[:4000],
                disaster_type="other", urgency="unknown",
                latitude=_optional_float(post.get("latitude")), longitude=_optional_float(post.get("longitude")),
                location_text=post.get("location"), source_url=post.get("url"), provenance="live",
                observed_at=parse_datetime_utc(post.get("created_at")),
            ))
        return output


def _optional_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
