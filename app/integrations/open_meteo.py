import httpx

from app.core.config import settings
from app.integrations.base import Connector


class OpenMeteoWeather(Connector):
    name = "Open-Meteo forecast"
    source_type = "open_meteo"

    def configured(self) -> bool:
        return settings.open_meteo_enabled

    def fetch(self) -> list:
        return []  # Weather is exposed as contextual data, not converted into incident reports.

    def forecast(self, latitude: float, longitude: float) -> dict:
        response = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": latitude, "longitude": longitude, "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code", "daily": "precipitation_sum,wind_speed_10m_max", "timezone": "auto"},
            timeout=15,
            headers={"User-Agent": "Cignal-Disaster-Intelligence/0.1"},
        )
        response.raise_for_status()
        return response.json()
