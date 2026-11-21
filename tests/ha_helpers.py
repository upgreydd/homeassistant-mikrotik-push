"""Helpers shared by the Home Assistant test modules."""

from custom_components.mikrotik_push.const import DOMAIN
from homeassistant.setup import async_setup_component


async def start_flow(hass):
    await async_setup_component(hass, "network", {})
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
