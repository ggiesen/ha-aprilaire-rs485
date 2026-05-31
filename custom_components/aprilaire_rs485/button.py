# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Button platform for the Aprilaire 8800 (RS-485) integration.

One button per node clears each of the four maintenance alarms (air filter,
water panel, dehumidifier, HVAC system) by sending ``[alarm]ALM=OFF`` (manual
p.26). The alarm states themselves are exposed as problem ``binary_sensor``
entities; these buttons are the write side. Pressing a button while the alarm
is inactive is a harmless no-op on the device.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_NODE_DISCOVERED
from .coordinator import Aprilaire8800Coordinator


@dataclass(frozen=True, kw_only=True)
class Aprilaire8800ClearAlarmButtonDescription(ButtonEntityDescription):
    """Describes a clear-alarm button.

    ``alarm`` is the short alarm code the coordinator expects: FLT, WP, DEH or
    SYS (it appends ``ALM`` and sends ``=OFF``).
    """

    alarm: str


BUTTON_DESCRIPTIONS: tuple[Aprilaire8800ClearAlarmButtonDescription, ...] = (
    Aprilaire8800ClearAlarmButtonDescription(
        key="clear_alarm_filter",
        translation_key="clear_alarm_filter",
        alarm="FLT",
        entity_category=EntityCategory.CONFIG,
    ),
    Aprilaire8800ClearAlarmButtonDescription(
        key="clear_alarm_water_panel",
        translation_key="clear_alarm_water_panel",
        alarm="WP",
        entity_category=EntityCategory.CONFIG,
    ),
    Aprilaire8800ClearAlarmButtonDescription(
        key="clear_alarm_dehumidifier",
        translation_key="clear_alarm_dehumidifier",
        alarm="DEH",
        entity_category=EntityCategory.CONFIG,
    ),
    Aprilaire8800ClearAlarmButtonDescription(
        key="clear_alarm_system",
        translation_key="clear_alarm_system",
        alarm="SYS",
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up clear-alarm buttons for all currently-known nodes."""
    coordinator: Aprilaire8800Coordinator = hass.data[DOMAIN][entry.entry_id]
    added: set[tuple[int, str]] = set()

    @callback
    def _add_for(address: int) -> None:
        new: list[ButtonEntity] = []
        for description in BUTTON_DESCRIPTIONS:
            key = (address, description.key)
            if key in added:
                continue
            added.add(key)
            new.append(Aprilaire8800ClearAlarmButton(coordinator, address, description))
        if new:
            async_add_entities(new)

    for addr in list(coordinator.nodes):
        _add_for(addr)
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NODE_DISCOVERED, _add_for))


class Aprilaire8800ClearAlarmButton(ButtonEntity):
    """Clears one maintenance alarm on a node."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: Aprilaire8800ClearAlarmButtonDescription

    def __init__(
        self,
        coordinator: Aprilaire8800Coordinator,
        address: int,
        description: Aprilaire8800ClearAlarmButtonDescription,
    ) -> None:
        """Initialise the button."""
        self._coordinator = coordinator
        self._address = address
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{address}_{description.key}"
        self._attr_device_info = coordinator.device_info(address)

    @property
    def available(self) -> bool:
        """Available whenever the node is known."""
        return self._coordinator.nodes.get(self._address) is not None

    async def async_press(self) -> None:
        """Send the clear-alarm command for this button's alarm."""
        await self._coordinator.async_clear_alarm(self._address, self.entity_description.alarm)
