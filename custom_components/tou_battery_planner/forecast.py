"""PV forecast sources: built-in Open-Meteo and external sensors."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_PAST_DAYS = 14
REQUEST_TIMEOUT = 20


@dataclass(frozen=True)
class ShortwaveForecast:
    """Daily shortwave radiation sums (MJ/m2) keyed by local date."""

    by_day: dict[date, float]


def parse_open_meteo(payload: dict[str, Any]) -> ShortwaveForecast:
    """Parse an Open-Meteo forecast response (daily shortwave_radiation_sum)."""
    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    values = daily.get("shortwave_radiation_sum") or []
    out: dict[date, float] = {}
    for t, v in zip(times, values, strict=False):
        if v is None:
            continue
        try:
            out[date.fromisoformat(t)] = float(v)
        except (TypeError, ValueError):
            continue
    return ShortwaveForecast(out)


def build_open_meteo_params(lat: float, lon: float, tz: str, past_days: int) -> dict[str, Any]:
    return {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "daily": "shortwave_radiation_sum",
        "timezone": tz,
        "past_days": past_days,
        "forecast_days": 3,
    }


async def async_fetch_open_meteo(
    hass: HomeAssistant,
    lat: float,
    lon: float,
    tz: str,
    past_days: int = DEFAULT_PAST_DAYS,
) -> ShortwaveForecast | None:
    """Fetch the daily shortwave forecast via HA's shared aiohttp session."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            OPEN_METEO_URL,
            params=build_open_meteo_params(lat, lon, tz, past_days),
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            return parse_open_meteo(await resp.json())
    except Exception as err:  # noqa: BLE001 - network/JSON errors all mean "no forecast"
        _LOGGER.warning("Open-Meteo request failed: %s", err)
        return None


def pv_kwh_from_shortwave(shortwave_mj: float | None, k: float) -> float | None:
    """Convert a daily shortwave sum to PV kWh using calibration factor k."""
    if shortwave_mj is None:
        return None
    return max(0.0, shortwave_mj * k)


def read_sensor_kwh(state: Any, attribute: str | None) -> float | None:
    """Read a kWh number from an entity state (or one of its attributes).

    ``state`` is a Home Assistant State or None. Returns None when unavailable.
    """
    if state is None:
        return None
    raw: Any = state.attributes.get(attribute) if attribute else state.state
    if raw in (None, "unknown", "unavailable", ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    unit = str(state.attributes.get("unit_of_measurement", "")).lower()
    if not attribute and unit == "wh":
        value /= 1000
    elif not attribute and unit == "mwh":
        value *= 1000
    return value if value >= 0 else None
