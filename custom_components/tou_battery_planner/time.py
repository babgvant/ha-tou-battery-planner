"""Time tunables (CONFIG category)."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory

from .const import CONF_DEADLINE, CONF_PLAN_EVENING, CONF_PLAN_MORNING
from .entity import TouEntity
from .planner import parse_hhmm

TIMES = (
    (CONF_DEADLINE, "Charge deadline"),
    (CONF_PLAN_EVENING, "Evening plan time"),
    (CONF_PLAN_MORNING, "Morning plan time"),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    c = entry.runtime_data
    async_add_entities(TouTime(c, *t) for t in TIMES)


class TouTime(TouEntity, TimeEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller, key, name) -> None:
        super().__init__(controller, f"opt_{key}", name)
        self._key = key

    @property
    def native_value(self) -> time:
        return parse_hhmm(str(self.controller._opt(self._key)))

    async def async_set_value(self, value: time) -> None:
        self.controller.set_option(self._key, value.strftime("%H:%M"))
