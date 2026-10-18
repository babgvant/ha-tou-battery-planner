"""Config flow and options flow."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector as sel

from .const import (
    CONF_AFTERNOON_SHARE,
    CONF_BUFFER_KWH,
    CONF_CAPACITY,
    CONF_CHARGE_FLAGS,
    CONF_CHARGE_SWITCHES,
    CONF_CLOUDY_THRESHOLD,
    CONF_DEADLINE,
    CONF_EV_SENSORS,
    CONF_FLAT_DAILY_KWH,
    CONF_FLOOR_SOC,
    CONF_FORECAST_ATTR,
    CONF_FORECAST_ENTITY,
    CONF_FORECAST_SOURCE,
    CONF_FORECAST_TODAY_ATTR,
    CONF_FORECAST_TODAY_ENTITY,
    CONF_GATE_PRICE,
    CONF_LAT,
    CONF_LEARNING_DAYS,
    CONF_LOAD_SENSOR,
    CONF_LON,
    CONF_PAUSE_MINUTES,
    CONF_PLAN_EVENING,
    CONF_PLAN_MORNING,
    CONF_PRICE_SENSOR,
    CONF_PRICE_UNIT,
    CONF_PV_SENSOR,
    CONF_SLOT_COUNT,
    CONF_SLOT_ROLES,
    CONF_SLOT_STARTS,
    CONF_SOC_ENTITIES,
    CONF_SOC_SENSOR,
    CONF_SPIKE_PRICE,
    CONF_TIME_ENTITIES,
    CONF_TOU_SWITCH,
    CONF_WRITE_BUDGET,
    DOMAIN,
    FORECAST_OPEN_METEO,
    FORECAST_SENSOR,
    TUNABLE_DEFAULTS,
)
from .planner import DEFAULT_LAYOUT, Role

_SOC_PATTERN = re.compile(r"^(number\..*_tou_)1(_battery_soc)$")
_TIME_PATTERN = re.compile(r"^(time\..*_tou_)1(_start_time)$")


def _ent(domain: str | list[str], multiple: bool = False) -> sel.EntitySelector:
    return sel.EntitySelector(sel.EntitySelectorConfig(domain=domain, multiple=multiple))


def _num(lo: float, hi: float, step: float, unit: str | None = None) -> sel.NumberSelector:
    cfg: dict[str, Any] = {"min": lo, "max": hi, "step": step, "mode": sel.NumberSelectorMode.BOX}
    if unit:
        cfg["unit_of_measurement"] = unit
    return sel.NumberSelector(sel.NumberSelectorConfig(**cfg))


def _prefill(hass, first_soc: str | None, n: int) -> tuple[str | None, str | None]:
    """Derive slot n SOC/time entity ids from the slot 1 SOC entity by pattern."""
    if not first_soc:
        return None, None
    m = _SOC_PATTERN.match(first_soc)
    if not m:
        return None, None
    soc = f"{m.group(1)}{n}{m.group(2)}"
    time_id = None
    cand = soc.replace("number.", "time.").replace("_battery_soc", "_start_time")
    if hass.states.get(cand):
        time_id = cand
    return (soc if hass.states.get(soc) else None), time_id


def _prefill_charge_switch(hass, first_soc: str | None, n: int) -> str | None:
    """Find the single switch.*_tou_<n>_grid_charging entity, if unambiguous."""
    if not first_soc:
        return None
    pattern = re.compile(rf"^switch\..*_tou_{n}_grid_charging$")
    found = [st.entity_id for st in hass.states.async_all("switch") if pattern.match(st.entity_id)]
    return found[0] if len(found) == 1 else None


def _tunable_schema(defaults: dict[str, Any]) -> dict:
    d = {**TUNABLE_DEFAULTS, **defaults}
    return {
        vol.Required(CONF_FLOOR_SOC, default=d[CONF_FLOOR_SOC]): _num(0, 100, 1, "%"),
        vol.Required(CONF_BUFFER_KWH, default=d[CONF_BUFFER_KWH]): _num(0, 50, 0.5, "kWh"),
        vol.Required(CONF_CLOUDY_THRESHOLD, default=d[CONF_CLOUDY_THRESHOLD]): _num(
            0, 200, 1, "kWh"
        ),
        vol.Required(CONF_GATE_PRICE, default=d[CONF_GATE_PRICE]): _num(0, 100, 0.1, "c/kWh"),
        vol.Required(CONF_SPIKE_PRICE, default=d[CONF_SPIKE_PRICE]): _num(0, 500, 0.5, "c/kWh"),
        vol.Required(CONF_DEADLINE, default=d[CONF_DEADLINE]): sel.TimeSelector(),
        vol.Required(CONF_PLAN_EVENING, default=d[CONF_PLAN_EVENING]): sel.TimeSelector(),
        vol.Required(CONF_PLAN_MORNING, default=d[CONF_PLAN_MORNING]): sel.TimeSelector(),
        vol.Required(CONF_AFTERNOON_SHARE, default=d[CONF_AFTERNOON_SHARE]): _num(0, 1, 0.05),
        vol.Required(CONF_LEARNING_DAYS, default=d[CONF_LEARNING_DAYS]): _num(7, 90, 1, "d"),
        vol.Required(CONF_WRITE_BUDGET, default=d[CONF_WRITE_BUDGET]): _num(1, 60, 1),
        vol.Required(CONF_FLAT_DAILY_KWH, default=d[CONF_FLAT_DAILY_KWH]): _num(5, 300, 1, "kWh"),
        vol.Required(CONF_PAUSE_MINUTES, default=d[CONF_PAUSE_MINUTES]): _num(1, 1440, 1, "min"),
    }


def _hhmm(value: str) -> str:
    return value[:5]


class TouConfigFlow(ConfigFlow, domain=DOMAIN):
    """Multi-step setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._first_soc: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return TouOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_inverter()

    async def async_step_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._first_soc = user_input.pop("first_soc_entity", None)
            self._data.update(user_input)
            self._data[CONF_SLOT_COUNT] = int(user_input[CONF_SLOT_COUNT])
            return await self.async_step_slots()
        schema = vol.Schema(
            {
                vol.Required(CONF_SLOT_COUNT, default=6): _num(1, 6, 1),
                vol.Required(CONF_CAPACITY): _num(1, 1000, 0.1, "kWh"),
                vol.Required(CONF_FLOOR_SOC, default=TUNABLE_DEFAULTS[CONF_FLOOR_SOC]): _num(
                    0, 100, 1, "%"
                ),
                vol.Optional("first_soc_entity"): _ent("number"),
                vol.Optional(CONF_TOU_SWITCH): _ent("switch"),
            }
        )
        return self.async_show_form(step_id="inverter", data_schema=schema)

    async def async_step_slots(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        n = self._data[CONF_SLOT_COUNT]
        errors: dict[str, str] = {}
        if user_input is not None:
            socs = [user_input[f"soc_{i}"] for i in range(1, n + 1)]
            if len(set(socs)) != len(socs):
                errors["base"] = "duplicate_slot_entity"
            else:
                self._data[CONF_SOC_ENTITIES] = socs
                self._data[CONF_TIME_ENTITIES] = [
                    user_input.get(f"time_{i}", "") for i in range(1, n + 1)
                ]
                self._data[CONF_CHARGE_SWITCHES] = [
                    user_input.get(f"charge_switch_{i}", "") for i in range(1, n + 1)
                ]
                self._data[CONF_CHARGE_FLAGS] = [
                    bool(user_input.get(f"charge_{i}", False)) for i in range(1, n + 1)
                ]
                return await self.async_step_sensors()
        fields: dict[Any, Any] = {}
        for i in range(1, n + 1):
            soc, tm = _prefill(self.hass, self._first_soc, i)
            if i == 1 and self._first_soc:
                soc = self._first_soc
                _, tm = _prefill(self.hass, self._first_soc, 1)
            fields[vol.Required(f"soc_{i}", **({"default": soc} if soc else {}))] = _ent("number")
            fields[vol.Optional(f"time_{i}", **({"default": tm} if tm else {}))] = _ent("time")
            cs = _prefill_charge_switch(self.hass, self._first_soc, i)
            fields[vol.Optional(f"charge_switch_{i}", **({"default": cs} if cs else {}))] = _ent(
                "switch"
            )
            # Fallback when no switch is chosen; default off so the user opts in per slot.
            fields[vol.Required(f"charge_{i}", default=False)] = bool
        return self.async_show_form(step_id="slots", data_schema=vol.Schema(fields), errors=errors)

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            self._data.setdefault(CONF_EV_SENSORS, [])
            return await self.async_step_forecast()
        schema = vol.Schema(
            {
                vol.Required(CONF_SOC_SENSOR): _ent("sensor"),
                vol.Required(CONF_PV_SENSOR): _ent("sensor"),
                vol.Required(CONF_LOAD_SENSOR): _ent("sensor"),
                vol.Optional(CONF_EV_SENSORS, default=[]): _ent("sensor", multiple=True),
                vol.Required(CONF_PRICE_SENSOR): _ent("sensor"),
                vol.Required(CONF_PRICE_UNIT, default="auto"): sel.SelectSelector(
                    sel.SelectSelectorConfig(options=["auto", "cents", "dollars"])
                ),
            }
        )
        return self.async_show_form(step_id="sensors", data_schema=schema)

    async def async_step_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_FORECAST_SOURCE] == FORECAST_SENSOR and not user_input.get(
                CONF_FORECAST_ENTITY
            ):
                errors["base"] = "forecast_entity_required"
            else:
                self._data.update(user_input)
                return await self.async_step_roles()
        schema = vol.Schema(
            {
                vol.Required(CONF_FORECAST_SOURCE, default=FORECAST_OPEN_METEO): sel.SelectSelector(
                    sel.SelectSelectorConfig(
                        options=[FORECAST_OPEN_METEO, FORECAST_SENSOR],
                        translation_key="forecast_source",
                    )
                ),
                vol.Optional(CONF_LAT, default=self.hass.config.latitude): _num(-90, 90, 0.001),
                vol.Optional(CONF_LON, default=self.hass.config.longitude): _num(-180, 180, 0.001),
                vol.Optional(CONF_FORECAST_ENTITY): _ent(["sensor", "input_number"]),
                vol.Optional(CONF_FORECAST_ATTR): str,
                vol.Optional(CONF_FORECAST_TODAY_ENTITY): _ent(["sensor", "input_number"]),
                vol.Optional(CONF_FORECAST_TODAY_ATTR): str,
            }
        )
        return self.async_show_form(step_id="forecast", data_schema=schema, errors=errors)

    async def async_step_roles(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        n = self._data[CONF_SLOT_COUNT]
        errors: dict[str, str] = {}
        if user_input is not None:
            roles = [user_input[f"role_{i}"] for i in range(1, n + 1)]
            if not {Role.OVERNIGHT.value, Role.CHARGE.value} & set(roles):
                errors["base"] = "no_charge_slot"
            else:
                self._data[CONF_SLOT_ROLES] = roles
                self._data[CONF_SLOT_STARTS] = [
                    _hhmm(user_input[f"start_{i}"]) for i in range(1, n + 1)
                ]
                for key, val in TUNABLE_DEFAULTS.items():
                    self._data.setdefault(key, val)
                return self.async_create_entry(title="TOU Battery Planner", data=self._data)
        fields: dict[Any, Any] = {}
        for i in range(1, n + 1):
            start, role = DEFAULT_LAYOUT[i - 1]
            fields[vol.Required(f"role_{i}", default=role.value)] = sel.SelectSelector(
                sel.SelectSelectorConfig(
                    options=[r.value for r in Role], translation_key="slot_role"
                )
            )
            fields[vol.Required(f"start_{i}", default=f"{start}:00")] = sel.TimeSelector()
        return self.async_show_form(step_id="roles", data_schema=vol.Schema(fields), errors=errors)


class TouOptionsFlow(OptionsFlow):
    """Tunables."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            for key in (CONF_DEADLINE, CONF_PLAN_EVENING, CONF_PLAN_MORNING):
                user_input[key] = _hhmm(user_input[key])
            for key in (CONF_FLOOR_SOC, CONF_LEARNING_DAYS, CONF_WRITE_BUDGET, CONF_PAUSE_MINUTES):
                user_input[key] = int(user_input[key])
            return self.async_create_entry(data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        schema = _tunable_schema(
            {
                k: (
                    f"{v}:00"
                    if k in (CONF_DEADLINE, CONF_PLAN_EVENING, CONF_PLAN_MORNING)
                    and len(str(v)) == 5
                    else v
                )
                for k, v in current.items()
                if k in TUNABLE_DEFAULTS
            }
        )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
