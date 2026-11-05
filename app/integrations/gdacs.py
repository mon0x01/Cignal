import feedparser

from app.integrations.base import Connector, NormalizedSignal, parse_datetime_utc


class GDACSFeed(Connector):
    name = "GDACS RSS"
    source_type = "gdacs"
    url = "https://www.gdacs.org/xml/rss.xml"

    def configured(self) -> bool:
        return True

    def fetch(self) -> list[NormalizedSignal]:
        feed = feedparser.parse(self.url, request_headers={"User-Agent": "Cignal-Disaster-Intelligence/0.1"})
        if getattr(feed, "bozo", False) and not feed.entries:
            raise RuntimeError("GDACS feed could not be parsed")
        output = []
        for entry in feed.entries:
            text = f"{entry.get('title', '')} {entry.get('summary', '')}"
            kind = "cyclone" if any(w in text.lower() for w in ("cyclone", "storm")) else "flood" if "flood" in text.lower() else "earthquake" if "earthquake" in text.lower() else "other"
            output.append(NormalizedSignal(
                source_type=self.source_type, source_name=self.name,
                source_record_id=entry.get("id") or entry.get("link") or entry.get("title", "unknown"),
                title=(entry.get("title") or "GDACS alert")[:180],
                description=(entry.get("summary") or text)[:4000],
                disaster_type=kind, urgency="high" if "red" in text.lower() else "moderate",
                location_text=entry.get("gdacs_country") or entry.get("location"),
                source_url=entry.get("link"), provenance="live",
                observed_at=parse_datetime_utc(entry.get("published_parsed") or entry.get("updated_parsed")),
            ))
        return output
