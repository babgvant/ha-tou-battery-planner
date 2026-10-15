"""Tests for forecast parsing (fixtures only, no network)."""

from datetime import date
from types import SimpleNamespace

from custom_components.tou_battery_planner.forecast import (
    build_open_meteo_params,
    parse_open_meteo,
    pv_kwh_from_shortwave,
    read_sensor_kwh,
)

FIXTURE = {
    "daily": {
        "time": ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"],
        "shortwave_radiation_sum": [14.2, None, 5.5, 16.0],
    }
}


def test_parse_skips_null_days():
    fc = parse_open_meteo(FIXTURE)
    assert fc.by_day == {
        date(2026, 10, 13): 14.2,
        date(2026, 10, 15): 5.5,
        date(2026, 10, 16): 16.0,
    }


def test_parse_handles_empty():
    assert parse_open_meteo({}).by_day == {}


def test_params_use_past_days_forecast_endpoint():
    p = build_open_meteo_params(42.05, -87.69, "America/Chicago", 14)
    assert p["past_days"] == 14 and p["daily"] == "shortwave_radiation_sum"


def test_pv_conversion():
    assert pv_kwh_from_shortwave(10.0, 6.0) == 60.0
    assert pv_kwh_from_shortwave(None, 6.0) is None


def _state(value, **attrs):
    return SimpleNamespace(state=value, attributes=attrs)


def test_read_sensor_variants():
    assert read_sensor_kwh(None, None) is None
    assert read_sensor_kwh(_state("unavailable"), None) is None
    assert read_sensor_kwh(_state("42.5", unit_of_measurement="kWh"), None) == 42.5
    assert read_sensor_kwh(_state("42500", unit_of_measurement="Wh"), None) == 42.5
    assert read_sensor_kwh(_state("on", estimate=31.0), "estimate") == 31.0
    assert read_sensor_kwh(_state("on"), "missing") is None
