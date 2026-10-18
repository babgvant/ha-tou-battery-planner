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
            "role_1": "charge",
            "start_1": "00:00:00",
            "role_2": "hold",
            "start_2": "06:00:00",
            "role_3": "spend",
            "start_3": "13:00:00",
            "role_4": "spend",
            "start_4": "19:00:00",
            "role_5": "charge",
            "start_5": "21:00:00",
            "role_6": "charge",
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


async def test_energy_sensor_rejected_for_load(recorder_mock, enable_custom_integrations, hass):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"slot_count": 1, "capacity_kwh": 50, "floor_soc": 20}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"soc_1": "number.sa_tou_1_battery_soc", "charge_1": False},
    )
    hass.states.async_set("sensor.load_energy", "1200", {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.load_w", "900", {"unit_of_measurement": "W"})
    base = {"soc_sensor": "sensor.soc", "pv_sensor": "sensor.load_w", "price_sensor": "sensor.p"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**base, "load_sensor": "sensor.load_energy", "price_unit": "auto"}
    )
    assert result["errors"] == {"base": "energy_not_power"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**base, "load_sensor": "sensor.load_w", "price_unit": "auto"}
    )
    assert result["step_id"] == "forecast"


async def test_roles_prefilled_from_live_times_and_charge(
    recorder_mock, enable_custom_integrations, hass
):
    result = await _start(hass)
    starts = ["00:00", "08:00", "11:00", "13:00", "15:30", "21:00"]
    for i, st in enumerate(starts, start=1):
        hass.states.async_set(f"time.sa_tou_{i}_start_time", f"{st}:00")
        hass.states.async_set(
            f"switch.global_p_house_tou_{i}_grid_charging", "on" if i == 4 else "off"
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "slot_count": 6,
            "capacity_kwh": 96.5,
            "floor_soc": 40,
            "first_soc_entity": "number.sa_tou_1_battery_soc",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **{f"soc_{i}": f"number.sa_tou_{i}_battery_soc" for i in range(1, 7)},
            **{f"time_{i}": f"time.sa_tou_{i}_start_time" for i in range(1, 7)},
            **{
                f"charge_switch_{i}": f"switch.global_p_house_tou_{i}_grid_charging"
                for i in range(1, 7)
            },
        },
    )
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
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"forecast_source": "open_meteo"}
    )
    assert result["step_id"] == "roles"
    defaults = {
        str(k): k.default()
        for k in result["data_schema"].schema
        if callable(getattr(k, "default", None)) and k.default is not vol.UNDEFINED
    }
    assert [defaults[f"start_{i}"] for i in range(1, 7)] == [f"{s}:00" for s in starts]
    assert [defaults[f"role_{i}"] for i in range(1, 7)] == [
        "hold",
        "hold",
        "hold",
        "charge",
        "hold",
        "hold",
    ]
