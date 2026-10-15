"""TOU Battery Planner integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import ATTR_ENTRY_ID, DOMAIN, PLATFORMS
from .controller import TouController

type TouConfigEntry = ConfigEntry[TouController]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY_ID): cv.string})


def _controllers(hass: HomeAssistant, call: ServiceCall) -> list[TouController]:
    wanted = call.data.get(ATTR_ENTRY_ID)
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return [e.runtime_data for e in entries if wanted in (None, e.entry_id)]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def plan_now(call: ServiceCall) -> None:
        for c in _controllers(hass, call):
            await c.async_plan("service")
            await c.async_reconcile("plan")

    async def reconcile_now(call: ServiceCall) -> None:
        for c in _controllers(hass, call):
            await c.async_reconcile("service")

    async def write_slot_times(call: ServiceCall) -> None:
        for c in _controllers(hass, call):
            await c.async_write_slot_times()

    hass.services.async_register(DOMAIN, "plan_now", plan_now, SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, "reconcile_now", reconcile_now, SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, "write_slot_times", write_slot_times, SERVICE_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TouConfigEntry) -> bool:
    controller = TouController(hass, entry)
    await controller.async_start()
    entry.runtime_data = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: TouConfigEntry) -> None:
    controller = entry.runtime_data
    if controller.skip_next_reload:
        controller.skip_next_reload = False
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TouConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop()
        return True
    return False
