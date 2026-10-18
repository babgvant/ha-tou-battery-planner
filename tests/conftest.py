"""Shared fixtures."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.tou_battery_planner.const import (
    CONF_CAPACITY,
    CONF_CHARGE_FLAGS,
    CONF_EV_SENSORS,
    CONF_FLOOR_SOC,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_SOURCE,
    CONF_LOAD_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_PRICE_UNIT,
    CONF_PV_SENSOR,
    CONF_SLOT_COUNT,
    CONF_SLOT_ROLES,
    CONF_SLOT_STARTS,
    CONF_SOC_ENTITIES,
    CONF_SOC_SENSOR,
    CONF_TIME_ENTITIES,
    DOMAIN,
    FORECAST_SENSOR,
    TUNABLE_DEFAULTS,
)

SLOT_ENTITIES = [f"number.sa_tou_{i}_battery_soc" for i in range(1, 7)]

ENTRY_DATA: dict[str, Any] = {
    **TUNABLE_DEFAULTS,
    CONF_SLOT_COUNT: 6,
    CONF_SOC_ENTITIES: SLOT_ENTITIES,
    CONF_TIME_ENTITIES: [""] * 6,
    CONF_CHARGE_FLAGS: [True, False, False, False, True, True],
    CONF_CAPACITY: 96.5,
    CONF_FLOOR_SOC: 20,
    CONF_SLOT_ROLES: ["charge", "hold", "spend", "spend", "charge", "charge"],
    CONF_SLOT_STARTS: ["00:00", "06:00", "13:00", "19:00", "21:00", "23:00"],
    CONF_SOC_SENSOR: "sensor.battery_soc",
    CONF_PV_SENSOR: "sensor.pv_power",
    CONF_LOAD_SENSOR: "sensor.load_power",
    CONF_EV_SENSORS: ["sensor.ev_power"],
    CONF_PRICE_SENSOR: "sensor.price",
    CONF_PRICE_UNIT: "cents",
    CONF_FORECAST_SOURCE: FORECAST_SENSOR,
    CONF_FORECAST_ENTITY: "sensor.pv_tomorrow",
}


@pytest.fixture
async def planner(recorder_mock, enable_custom_integrations, hass, freezer):
    """Set up the integration at 22:00 local, cloudy, price ok, enabled and live."""
    freezer.move_to("2026-10-15 22:00:00-07:00")
    for ent in SLOT_ENTITIES:
        hass.states.async_set(ent, "20")
    hass.states.async_set("sensor.battery_soc", "30")
    hass.states.async_set("sensor.price", "3.0")
    hass.states.async_set("sensor.ev_power", "0", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.pv_tomorrow", "12", {"unit_of_measurement": "kWh"})
    entry = MockConfigEntry(domain=DOMAIN, data=dict(ENTRY_DATA), title="TOU")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    calls = async_mock_service(hass, "number", "set_value")
    c = entry.runtime_data
    c.started_at = dt_util.utcnow() - timedelta(hours=1)  # past the grace period
    c.write_spacing = 0
    c.st.enabled = True
    c.st.dry_run = False
    await c.async_plan("test")
    return c, calls, entry
