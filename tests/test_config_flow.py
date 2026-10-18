"""Config flow tests."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.data_entry_flow import FlowResultType

from custom_components.tou_battery_planner.const import DOMAIN


async def _start(hass):
    for i in range(1, 7):
        hass.states.async_set(f"number.sa_tou_{i}_battery_soc", "20")
        hass.states.async_set(f"time.sa_tou_{i}_start_time", "00:00:00")
        hass.states.async_set(f"switch.global_p_house_tou_{i}_grid_charging", "off")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["step_id"] == "inverter"
    return result


async def test_full_flow_with_prefill(recorder_mock, enable_custom_integrations, hass):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "slot_count": 6,
            "capacity_kwh": 96.5,
            "floor_soc": 20,
            "first_soc_entity": "number.sa_tou_1_battery_soc",
        },
    )
    assert result["step_id"] == "slots"
    # prefilled defaults for slots 2..6
    defaults = {
        str(k): k.default()
        for k in result["data_schema"].schema
        if hasattr(k, "default") and callable(k.default) and k.default is not vol.UNDEFINED
    }
    assert defaults["soc_3"] == "number.sa_tou_3_battery_soc"
    assert defaults["charge_switch_3"] == "switch.global_p_house_tou_3_grid_charging"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **{f"soc_{i}": f"number.sa_tou_{i}_battery_soc" for i in range(1, 7)},
            **{f"charge_{i}": i in (1, 5, 6) for i in range(1, 7)},
        },
    )
    assert result["step_id"] == "sensors"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "soc_sensor": "sensor.soc",
            "pv_sensor": "sensor.pv",
            "load_sensor": "sensor.load",
            "price_sensor": "sensor.price",
            "price_unit": "auto",
        },
    )
    assert result["step_id"] == "forecast"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"forecast_source": "open_meteo"}
    )
    assert result["step_id"] == "roles"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "role_1": "overnight",
            "start_1": "00:00:00",
            "role_2": "morning",
            "start_2": "06:00:00",
            "role_3": "peak",
            "start_3": "13:00:00",
            "role_4": "evening",
            "start_4": "19:00:00",
            "role_5": "overnight",
            "start_5": "21:00:00",
            "role_6": "overnight",
            "start_6": "23:00:00",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["slot_starts"][4] == "21:00"
    assert data["capacity_kwh"] == 96.5


async def test_duplicate_slot_entities_rejected(recorder_mock, enable_custom_integrations, hass):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"slot_count": 2, "capacity_kwh": 50, "floor_soc": 20}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "soc_1": "number.sa_tou_1_battery_soc",
            "soc_2": "number.sa_tou_1_battery_soc",
            "charge_1": True,
            "charge_2": False,
        },
    )
    assert result["errors"] == {"base": "duplicate_slot_entity"}
