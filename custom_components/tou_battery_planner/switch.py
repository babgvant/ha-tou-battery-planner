"""Master enable and dry-run switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity

from .entity import TouEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    c = entry.runtime_data
    async_add_entities(
        [
            TouSwitch(c, "enabled", "Planner enabled", "mdi:power"),
            TouSwitch(c, "dry_run", "Dry run", "mdi:test-tube"),
        ]
    )


class TouSwitch(TouEntity, SwitchEntity):
    def __init__(self, controller, field, name, icon) -> None:
        super().__init__(controller, field, name)
        self._field = field
        self._attr_icon = icon

    @property
    def is_on(self) -> bool:
        return getattr(self.controller.st, self._field)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._set(False)

    def _set(self, value: bool) -> None:
        if self._field == "enabled":
            self.controller.set_enabled(value)
        else:
            self.controller.set_dry_run(value)
