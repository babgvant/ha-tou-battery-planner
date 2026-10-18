"""Tunables as number entities (CONFIG category)."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory

from .const import (
    CONF_BUFFER_KWH,
    CONF_CLOUDY_THRESHOLD,
    CONF_FLAT_DAILY_KWH,
    CONF_FLOOR_SOC,
    CONF_GATE_PRICE,
    CONF_LEARNING_DAYS,
    CONF_PAUSE_MINUTES,
    CONF_SPEND_PV_SHARE,
    CONF_SPIKE_PRICE,
    CONF_WRITE_BUDGET,
)
from .entity import TouEntity

# key, name, min, max, step, unit, integer
NUMBERS = (
    (CONF_FLOOR_SOC, "Floor SOC", 0, 100, 1, "%", True),
    (CONF_BUFFER_KWH, "Reserve buffer", 0, 50, 0.5, "kWh", False),
    (CONF_CLOUDY_THRESHOLD, "Cloudy threshold", 0, 200, 1, "kWh", False),
    (CONF_GATE_PRICE, "Charge price gate", 0, 100, 0.1, "c/kWh", False),
    (CONF_SPIKE_PRICE, "Spike release price", 0, 500, 0.5, "c/kWh", False),
    (CONF_SPEND_PV_SHARE, "PV share during spend slots", 0, 1, 0.05, None, False),
    (CONF_LEARNING_DAYS, "Learning window", 7, 90, 1, "d", True),
    (CONF_WRITE_BUDGET, "Write budget per hour", 1, 60, 1, None, True),
    (CONF_FLAT_DAILY_KWH, "Fallback daily load", 5, 300, 1, "kWh", False),
    (CONF_PAUSE_MINUTES, "Manual change pause", 1, 1440, 1, "min", True),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    c = entry.runtime_data
    async_add_entities(TouNumber(c, *n) for n in NUMBERS)


class TouNumber(TouEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, controller, key, name, lo, hi, step, unit, integer) -> None:
        super().__init__(controller, f"opt_{key}", name)
        self._key = key
        self._integer = integer
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self) -> float | None:
        v = self.controller._opt(self._key)
        return None if v is None else float(v)

    async def async_set_native_value(self, value: float) -> None:
        self.controller.set_option(self._key, int(value) if self._integer else float(value))
