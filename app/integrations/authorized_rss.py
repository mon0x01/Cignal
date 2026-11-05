import feedparser

from app.core.config import settings
from app.integrations.base import Connector, NormalizedSignal, parse_datetime_utc


class AuthorizedRSSFeeds(Connector):
    name = "Configured permitted RSS feeds"
    source_type = "authorized_rss"

    def configured(self) -> bool:
        return bool(settings.authorized_feeds)

    def fetch(self) -> list[NormalizedSignal]:
        output = []
        for feed_url in settings.authorized_feeds:
            parsed = feedparser.parse(feed_url, request_headers={"User-Agent": "Cignal-Disaster-Intelligence/0.1"})
            if getattr(parsed, "bozo", False) and not parsed.entries:
                continue
            for entry in parsed.entries:
                title = entry.get("title", "External public feed item")
                summary = entry.get("summary", title)
                output.append(NormalizedSignal(
                    source_type=self.source_type, source_name=parsed.feed.get("title", "Authorized RSS"),
                    source_record_id=entry.get("id") or entry.get("link") or title,
                    title=title[:180], description=summary[:4000], disaster_type="other", urgency="unknown",
                    source_url=entry.get("link"), provenance="live",
                    observed_at=parse_datetime_utc(entry.get("published_parsed") or entry.get("updated_parsed")),
                ))
        return output
