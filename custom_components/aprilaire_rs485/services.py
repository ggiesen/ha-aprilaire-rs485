# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Service handlers for the Aprilaire 8800 (RS-485) integration.

Four services are registered on the integration domain (not on a platform):

  - ``send_temporary_message``    Display a transient message until cleared.
  - ``clear_temporary_message``   Wipe the transient message.
  - ``set_permanent_message``     Write one of four EEPROM-backed slots.
  - ``clear_permanent_message``   Wipe one of four EEPROM-backed slots.

All four accept the standard HA target fields (entity_id, device_id,
area_id, label_id). Targets are resolved to node addresses via the device
registry. If a user accidentally targets the bus pseudo-device, the bus is
silently skipped; if the call resolves to zero thermostats overall, a
HomeAssistantError is raised so automations fail loud rather than silently
doing nothing.

Services are registered once across all config entries (the first
async_setup_entry triggers registration; the last async_unload_entry
removes them). This is the standard HA pattern for integration-level
services.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_extract_referenced_entity_ids

from .const import DOMAIN
from .coordinator import Aprilaire8800Coordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_SEND_TEMPORARY_MESSAGE = "send_temporary_message"
SERVICE_CLEAR_TEMPORARY_MESSAGE = "clear_temporary_message"
SERVICE_SET_PERMANENT_MESSAGE = "set_permanent_message"
SERVICE_CLEAR_PERMANENT_MESSAGE = "clear_permanent_message"

ATTR_MESSAGE = "message"
ATTR_SLOT = "slot"

# Schemas. cv.ENTITY_SERVICE_FIELDS gives us entity_id / device_id /
# area_id / label_id - i.e. the standard target selectors.
_SEND_TEMP_SCHEMA = vol.Schema(
    {
        **cv.ENTITY_SERVICE_FIELDS,
        vol.Required(ATTR_MESSAGE): cv.string,
    }
)
_CLEAR_TEMP_SCHEMA = vol.Schema({**cv.ENTITY_SERVICE_FIELDS})
_SET_PERM_SCHEMA = vol.Schema(
    {
        **cv.ENTITY_SERVICE_FIELDS,
        vol.Required(ATTR_SLOT): vol.All(int, vol.Range(min=1, max=4)),
        vol.Required(ATTR_MESSAGE): cv.string,
    }
)
_CLEAR_PERM_SCHEMA = vol.Schema(
    {
        **cv.ENTITY_SERVICE_FIELDS,
        vol.Required(ATTR_SLOT): vol.All(int, vol.Range(min=1, max=4)),
    }
)


def _coordinators(hass: HomeAssistant) -> list[Aprilaire8800Coordinator]:
    """Return every coordinator-like value currently set up under our domain.

    Uses duck typing rather than isinstance so test doubles work without
    subclassing. The filter is still useful as a guard against unexpected
    values ever ending up in hass.data[DOMAIN]; we just check for the
    attributes services actually touch.
    """
    bucket = hass.data.get(DOMAIN, {})
    return [
        v
        for v in bucket.values()
        if hasattr(v, "nodes") and hasattr(v, "async_set_display_message")
    ]


@callback
def _resolve_targets(
    hass: HomeAssistant, call: ServiceCall
) -> list[tuple[Aprilaire8800Coordinator, int]]:
    """Resolve a service call's target to (coordinator, node_address) pairs.

    Walks every referenced entity to its device, pulls the (DOMAIN, addr)
    identifier off the device, and looks the address up in each known
    coordinator. The bus pseudo-device (identifier ``(DOMAIN, "bus")``) is
    skipped silently. Results are de-duplicated so targeting both a device
    and one of its entities doesn't double-fire.
    """
    selected = async_extract_referenced_entity_ids(hass, call)
    all_entity_ids = selected.referenced | selected.indirectly_referenced

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    coordinators = _coordinators(hass)
    # Key on (entry_id, address) to dedupe; value is the tuple we'll call.
    resolved: dict[tuple[str, int], tuple[Aprilaire8800Coordinator, int]] = {}

    def _record_address(device_id: str) -> None:
        device = dev_reg.async_get(device_id)
        if device is None:
            return
        for ident in device.identifiers:
            if len(ident) != 2 or ident[0] != DOMAIN:
                continue
            if ident[1] == "bus":
                continue
            try:
                addr = int(ident[1])
            except ValueError:
                continue
            for coord in coordinators:
                if addr in coord.nodes:
                    entry_id = (
                        coord.config_entry_id if hasattr(coord, "config_entry_id") else id(coord)
                    )
                    resolved[(str(entry_id), addr)] = (coord, addr)
                    break

    # Entities -> devices -> addresses.
    for entity_id in all_entity_ids:
        entry = ent_reg.async_get(entity_id)
        if entry is None or entry.platform != DOMAIN or entry.device_id is None:
            continue
        _record_address(entry.device_id)

    # Devices targeted directly (without any of their entities being named).
    for device_id in selected.referenced_devices:
        _record_address(device_id)

    return list(resolved.values())


def _require_targets(
    hass: HomeAssistant, call: ServiceCall
) -> list[tuple[Aprilaire8800Coordinator, int]]:
    """Like _resolve_targets but raises if the call resolved to nothing.

    A service call with no matching thermostats is almost always a user
    error (typo in entity_id, targeting the bus, targeting an area with no
    Aprilaire devices). Failing loudly is more useful than silently doing
    nothing.
    """
    targets = _resolve_targets(hass, call)
    if not targets:
        raise HomeAssistantError(
            "No Aprilaire 8800 thermostats matched the service call target. "
            "Check that the entity_id, device_id, or area contains at least "
            "one thermostat (not the bus pseudo-device)."
        )
    return targets


async def _handle_send_temp(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for send_temporary_message."""
    message = call.data[ATTR_MESSAGE]
    for coord, addr in _require_targets(hass, call):
        await coord.async_set_display_message(addr, "TMPMES", message)


async def _handle_clear_temp(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for clear_temporary_message."""
    for coord, addr in _require_targets(hass, call):
        await coord.async_clear_display_message(addr, "TMPMES")


async def _handle_set_perm(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for set_permanent_message."""
    slot = call.data[ATTR_SLOT]
    message = call.data[ATTR_MESSAGE]
    slot_name = f"PMES{slot}"
    for coord, addr in _require_targets(hass, call):
        await coord.async_set_display_message(addr, slot_name, message)


async def _handle_clear_perm(hass: HomeAssistant, call: ServiceCall) -> None:
    """Service handler for clear_permanent_message."""
    slot = call.data[ATTR_SLOT]
    slot_name = f"PMES{slot}"
    for coord, addr in _require_targets(hass, call):
        await coord.async_clear_display_message(addr, slot_name)


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register all messaging services on the integration domain.

    Idempotent: re-registering an already-registered service is a no-op
    in HA, but we guard explicitly anyway for clarity.
    """
    if hass.services.has_service(DOMAIN, SERVICE_SEND_TEMPORARY_MESSAGE):
        return

    async def _send_temp(call: ServiceCall) -> None:
        await _handle_send_temp(hass, call)

    async def _clear_temp(call: ServiceCall) -> None:
        await _handle_clear_temp(hass, call)

    async def _set_perm(call: ServiceCall) -> None:
        await _handle_set_perm(hass, call)

    async def _clear_perm(call: ServiceCall) -> None:
        await _handle_clear_perm(hass, call)

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_TEMPORARY_MESSAGE, _send_temp, schema=_SEND_TEMP_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_TEMPORARY_MESSAGE, _clear_temp, schema=_CLEAR_TEMP_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_PERMANENT_MESSAGE, _set_perm, schema=_SET_PERM_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_PERMANENT_MESSAGE, _clear_perm, schema=_CLEAR_PERM_SCHEMA
    )
    _LOGGER.debug("Aprilaire 8800 messaging services registered")


@callback
def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove all messaging services. Called when the last entry unloads."""
    for service in (
        SERVICE_SEND_TEMPORARY_MESSAGE,
        SERVICE_CLEAR_TEMPORARY_MESSAGE,
        SERVICE_SET_PERMANENT_MESSAGE,
        SERVICE_CLEAR_PERMANENT_MESSAGE,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
