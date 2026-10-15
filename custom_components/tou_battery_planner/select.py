"""Profile mode select."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity

from .const import PROFILE_MODES
from .entity import TouEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([ProfileModeSelect(entry.runtime_data)])


class ProfileModeSelect(TouEntity, SelectEntity):
    _attr_options = PROFILE_MODES
    _attr_icon = "mdi:weather-partly-cloudy"

    def __init__(self, controller) -> None:
        super().__init__(controller, "profile_mode", "Profile mode")

    @property
    def current_option(self) -> str:
        return self.controller.st.profile_mode

    async def async_select_option(self, option: str) -> None:
        self.controller.set_profile_mode(option)
