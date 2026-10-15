"""Learning math: hourly load profile and PV calibration factor.

The math functions are pure. ``async_hourly_means`` is the thin recorder adapter.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# Typical residential shape (fractions of a day, sums to 1.0).
DEFAULT_SHAPE: list[float] = [
    0.025,
    0.022,
    0.020,
    0.020,
    0.021,
    0.025,
    0.035,
    0.050,
    0.050,
    0.040,
    0.036,
    0.036,
    0.036,
    0.036,
    0.037,
    0.040,
    0.048,
    0.062,
    0.072,
    0.072,
    0.062,
    0.050,
    0.039,
    0.030,
]
_total = sum(DEFAULT_SHAPE)
DEFAULT_SHAPE = [v / _total for v in DEFAULT_SHAPE]

MIN_PROFILE_DAYS = 7
WEEKDAY_SPLIT_DAYS = 28
K_MIN, K_MAX, K_DEFAULT = 3.0, 9.0, 6.0
K_MIN_VALID_DAYS = 5
K_WINDOW_DAYS = 14
MAX_MISSING_HOURS = 2
MIN_SHORTWAVE = 1.0


@dataclass(frozen=True)
class LoadProfile:
    """24 hourly kWh values (EV excluded)."""

    hourly: list[float]
    provisional: bool
    days_of_data: int
    weekend: list[float] | None = None  # only set when a weekday/weekend split exists

    @property
    def daily_kwh(self) -> float:
        return sum(self.hourly)

    def for_date(self, day: date) -> list[float]:
        if self.weekend is not None and day.weekday() >= 5:
            return self.weekend
        return self.hourly


def flat_profile(daily_kwh: float) -> list[float]:
    """Spread a flat daily total over the default residential shape."""
    return [daily_kwh * s for s in DEFAULT_SHAPE]


def build_load_profile(
    samples: Iterable[tuple[datetime, float]],
    flat_daily_kwh: float,
) -> LoadProfile:
    """Build the hour-of-day median profile from (local hour start, kWh) samples.

    Hours without samples are filled from the flat fallback. Fewer than
    MIN_PROFILE_DAYS distinct days -> the flat fallback, marked provisional.
    """
    by_hour: dict[int, list[float]] = defaultdict(list)
    by_hour_we: dict[int, list[float]] = defaultdict(list)
    by_hour_wd: dict[int, list[float]] = defaultdict(list)
    days: set[date] = set()
    for ts, kwh in samples:
        days.add(ts.date())
        by_hour[ts.hour].append(kwh)
        (by_hour_we if ts.weekday() >= 5 else by_hour_wd)[ts.hour].append(kwh)
    fallback = flat_profile(flat_daily_kwh)
    if len(days) < MIN_PROFILE_DAYS:
        return LoadProfile(fallback, True, len(days))

    def med(src: dict[int, list[float]]) -> list[float]:
        return [statistics.median(src[h]) if src.get(h) else fallback[h] for h in range(24)]

    hourly = med(by_hour)
    if len(days) >= WEEKDAY_SPLIT_DAYS and by_hour_we and by_hour_wd:
        return LoadProfile(med(by_hour_wd), False, len(days), weekend=med(by_hour_we))
    return LoadProfile(hourly, False, len(days))


def daily_totals(
    samples: Iterable[tuple[datetime, float]],
    max_missing_hours: int = MAX_MISSING_HOURS,
    exclude_day: date | None = None,
) -> dict[date, float]:
    """Sum hourly kWh into per-day totals, dropping days with too many missing hours."""
    hours: dict[date, dict[int, float]] = defaultdict(dict)
    for ts, kwh in samples:
        hours[ts.date()][ts.hour] = kwh
    out: dict[date, float] = {}
    for day, hmap in hours.items():
        if day == exclude_day:
            continue
        if 24 - len(hmap) > max_missing_hours:
            continue
        out[day] = sum(hmap.values())
    return out


def calibration_factor(
    pv_by_day: dict[date, float],
    shortwave_by_day: dict[date, float],
    window: int = K_WINDOW_DAYS,
) -> tuple[float, list[tuple[date, float]]]:
    """Rolling-median k (kWh per MJ/m2) clamped to [K_MIN, K_MAX].

    Returns (k, valid (day, ratio) pairs used, newest last). Default K_DEFAULT
    until K_MIN_VALID_DAYS valid days exist.
    """
    ratios: list[tuple[date, float]] = []
    for day in sorted(pv_by_day):
        sw = shortwave_by_day.get(day)
        if sw is None or sw < MIN_SHORTWAVE:
            continue
        ratios.append((day, pv_by_day[day] / sw))
    recent = ratios[-window:]
    if len(recent) < K_MIN_VALID_DAYS:
        return K_DEFAULT, recent
    k = statistics.median(r for _, r in recent)
    return max(K_MIN, min(K_MAX, k)), recent


def mean_absolute_error(pairs: list[tuple[float, float]]) -> float | None:
    """MAE of (forecast, actual) pairs, None if empty."""
    if not pairs:
        return None
    return sum(abs(f - a) for f, a in pairs) / len(pairs)


async def async_hourly_means(
    hass: HomeAssistant,
    statistic_ids: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, list[tuple[datetime, float]]]:
    """Fetch hourly mean statistics from the recorder as (local hour start, mean)."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period
    from homeassistant.util import dt as dt_util

    if not statistic_ids:
        return {}
    raw = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        set(statistic_ids),
        "hour",
        None,
        {"mean"},
    )
    out: dict[str, list[tuple[datetime, float]]] = {}
    for sid, rows in raw.items():
        series: list[tuple[datetime, float]] = []
        for row in rows:
            mean = row.get("mean")
            if mean is None:
                continue
            ts = row["start"]
            if isinstance(ts, (int, float)):
                ts = dt_util.utc_from_timestamp(ts)
            series.append((dt_util.as_local(ts), float(mean)))
        out[sid] = series
    return out


def combine_load(
    load: list[tuple[datetime, float]],
    evs: list[list[tuple[datetime, float]]],
    load_to_kw: float,
    ev_to_kw: list[float],
) -> list[tuple[datetime, float]]:
    """Hourly kWh = load - sum(EV), clamped at 0 (hourly mean kW x 1 h)."""
    ev_maps = [{ts: v * f for ts, v in series} for series, f in zip(evs, ev_to_kw, strict=False)]
    out = []
    for ts, v in load:
        kwh = v * load_to_kw - sum(m.get(ts, 0.0) for m in ev_maps)
        out.append((ts, max(0.0, kwh)))
    return out
