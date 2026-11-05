import csv
import io
from datetime import UTC, datetime

import httpx

from app.core.config import settings
from app.integrations.base import Connector, NormalizedSignal


class NASAFirms(Connector):
    name = "NASA FIRMS active fire detections"
    source_type = "nasa_firms"

    def configured(self) -> bool:
        return bool(settings.nasa_firms_map_key)

    def fetch(self) -> list[NormalizedSignal]:
        if not self.configured():
            return []
        url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{settings.nasa_firms_map_key}/VIIRS_SNPP_NRT/world/1"
        response = httpx.get(url, timeout=30, headers={"User-Agent": "Cignal-Disaster-Intelligence/0.1"})
        response.raise_for_status()
        output = []
        for row in csv.DictReader(io.StringIO(response.text)):
            try:
                lat, lon = float(row["latitude"]), float(row["longitude"])
            except (KeyError, ValueError):
                continue
            confidence = row.get("confidence", "unknown")
            observed_at = None
            try:
                observed_at = datetime.strptime(
                    f"{row.get('acq_date', '')} {row.get('acq_time', '')}", "%Y-%m-%d %H%M"
                ).replace(tzinfo=UTC)
            except ValueError:
                pass
            output.append(NormalizedSignal(
                source_type=self.source_type, source_name=self.name,
                source_record_id=f"{row.get('acq_date','')}-{row.get('acq_time','')}-{lat:.4f}-{lon:.4f}",
                title="Satellite active-fire detection",
                description=f"NASA FIRMS VIIRS detection. Confidence reported as {confidence}. Satellite detection is not confirmation of an active incident or impact.",
                disaster_type="fire", urgency="moderate", latitude=lat, longitude=lon,
                location_text=f"{lat:.4f}, {lon:.4f}", provenance="live", observed_at=observed_at,
            ))
        return output
