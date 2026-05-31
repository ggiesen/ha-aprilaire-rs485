# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The Aprilaire 8800 (RS-485) integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_ADDRESSES,
    CONF_BAUD,
    CONF_MAX_ADDRESS,
    CONF_OUTDOOR_TEMP_REBROADCAST,
    CONF_OUTDOOR_TEMP_SOURCE,
    CONF_PORT,
    DEFAULT_BAUD,
    DEFAULT_MAX_ADDRESS,
    DOMAIN,
)
from .coordinator import Aprilaire8800Coordinator
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.HUMIDIFIER,
    Platform.SENSOR,
    Platform.TEXT,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Aprilaire 8800 from a config entry."""
    url = entry.data[CONF_PORT]
    baud = entry.data.get(CONF_BAUD, DEFAULT_BAUD)
    max_address = entry.data.get(CONF_MAX_ADDRESS, DEFAULT_MAX_ADDRESS)
    explicit = entry.data.get(CONF_ADDRESSES) or None
    ot_source = entry.data.get(CONF_OUTDOOR_TEMP_SOURCE) or None
    # Default True so existing config entries without the key still get
    # rebroadcast behaviour (the lowest-addressed sensor-equipped node
    # populates OT on its peers).
    ot_rebroadcast = entry.data.get(CONF_OUTDOOR_TEMP_REBROADCAST, True)

    coordinator = Aprilaire8800Coordinator(
        hass=hass,
        url=url,
        baud=baud,
        max_address=max_address,
        explicit_addresses=explicit,
        outdoor_temp_source=ot_source,
        outdoor_temp_rebroadcast=ot_rebroadcast,
    )
    # Stash the originating config entry id on the coordinator so the
    # services module can use it as a stable dedupe key.
    coordinator.config_entry_id = entry.entry_id
    await coordinator.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Register the bus pseudo-device up front so the per-node devices created
    # during platform setup below can reference it as their via_device.
    # Without this the device registry sees via_device pointing at a device
    # that does not exist yet - a deprecation warning today, a hard error
    # from HA 2025.12.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        **coordinator.bus_device_info(),
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Services live at the integration level and are shared across config
    # entries. Register on first setup; unregister when the last entry unloads.
    async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: Aprilaire8800Coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
        # If this was the last entry, drop the services too.
        if not hass.data[DOMAIN]:
            async_unregister_services(hass)
    return unload_ok
