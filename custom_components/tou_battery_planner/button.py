"""Action buttons."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity

from .entity import TouEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    c = entry.runtime_data
    async_add_entities(
        [
            TouButton(c, "plan_now", "Plan now", "mdi:calendar-refresh"),
            TouButton(c, "recalibrate", "Recalibrate", "mdi:chart-bell-curve"),
            TouButton(c, "write_slot_times", "Write slot start times", "mdi:clock-edit"),
        ]
    )


class TouButton(TouEntity, ButtonEntity):
    def __init__(self, controller, key, name, icon) -> None:
        super().__init__(controller, key, name)
        self._key = key
        self._attr_icon = icon

    async def async_press(self) -> None:
        c = self.controller
        if self._key == "plan_now":
            await c.async_plan("button")
            await c.async_reconcile("plan")
        elif self._key == "recalibrate":
            await c.async_learn()
            await c.async_plan("recalibrate")
            await c.async_reconcile("plan")
        else:
            await c.async_write_slot_times()
