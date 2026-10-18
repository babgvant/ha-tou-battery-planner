"""Binary sensors for the live overrides."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity

from .entity import TouEntity

DESCRIPTIONS = (
    ("price_spike", "Price spike active", lambda c: c.spike_active, "mdi:flash-alert"),
    (
        "charge_allowed",
        "Charge price gate open",
        lambda c: not c.gate_closed,
        "mdi:battery-charging",
    ),
    ("ev_charging", "EV charging detected", lambda c: c.ev_charging, "mdi:ev-station"),
    (
        "controls_available",
        "Inverter controls available",
        lambda c: c.controls_available,
        "mdi:check-network",
    ),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    c = entry.runtime_data
    async_add_entities(TouBinary(c, *d) for d in DESCRIPTIONS)


class TouBinary(TouEntity, BinarySensorEntity):
    def __init__(self, controller, key, name, fn, icon) -> None:
        super().__init__(controller, key, name)
        self._fn = fn
        self._attr_icon = icon

    @property
    def is_on(self) -> bool:
        return bool(self._fn(self.controller))
