# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Select platform for the Aprilaire 8800 (RS-485) integration.

Exposes the four maintenance-alarm reminder intervals (``[alarm]ALMP``, manual
p.25) as config selects: air filter (OFF/1/3/6/12 months) and water panel,
dehumidifier, and HVAC system (OFF/1-12 months). These are the thresholds that
drive the alarm ``binary_sensor`` entities. Options are the wire values
lower-cased ("off", "6", ...); the human labels come from the translations.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_NODE_DISCOVERED, SIGNAL_NODE_UPDATED
from .coordinator import Aprilaire8800Coordinator, NodeState

_FILTER_WIRE_VALUES = ("OFF", "1", "3", "6", "12")
_MONTH_WIRE_VALUES = ("OFF", *(str(m) for m in range(1, 13)))


@dataclass(frozen=True, kw_only=True)
class Aprilaire8800AlarmPeriodDescription(SelectEntityDescription):
    """Describes an alarm-period select.

    ``alarm`` is the short code (FLT/WP/DEH/SYS) the coordinator expects;
    ``wire_values`` are the accepted wire strings in display order.
    """

    alarm: str
    wire_values: tuple[str, ...]


SELECT_DESCRIPTIONS: tuple[Aprilaire8800AlarmPeriodDescription, ...] = (
    Aprilaire8800AlarmPeriodDescription(
        key="alarm_period_filter",
        translation_key="alarm_period_filter",
        alarm="FLT",
        wire_values=_FILTER_WIRE_VALUES,
        entity_category=EntityCategory.CONFIG,
    ),
    Aprilaire8800AlarmPeriodDescription(
        key="alarm_period_water_panel",
        translation_key="alarm_period_water_panel",
        alarm="WP",
        wire_values=_MONTH_WIRE_VALUES,
        entity_category=EntityCategory.CONFIG,
    ),
    Aprilaire8800AlarmPeriodDescription(
        key="alarm_period_dehumidifier",
        translation_key="alarm_period_dehumidifier",
        alarm="DEH",
        wire_values=_MONTH_WIRE_VALUES,
        entity_category=EntityCategory.CONFIG,
    ),
    Aprilaire8800AlarmPeriodDescription(
        key="alarm_period_system",
        translation_key="alarm_period_system",
        alarm="SYS",
        wire_values=_MONTH_WIRE_VALUES,
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up alarm-period selects for all currently-known nodes."""
    coordinator: Aprilaire8800Coordinator = hass.data[DOMAIN][entry.entry_id]
    added: set[tuple[int, str]] = set()

    @callback
    def _add_for(address: int) -> None:
        new: list[SelectEntity] = []
        for description in SELECT_DESCRIPTIONS:
            key = (address, description.key)
            if key in added:
                continue
            added.add(key)
            new.append(Aprilaire8800AlarmPeriodSelect(coordinator, address, description))
        if new:
            async_add_entities(new)

    for addr in list(coordinator.nodes):
        _add_for(addr)
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NODE_DISCOVERED, _add_for))


class Aprilaire8800AlarmPeriodSelect(SelectEntity):
    """A maintenance-alarm reminder interval for one node."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: Aprilaire8800AlarmPeriodDescription

    def __init__(
        self,
        coordinator: Aprilaire8800Coordinator,
        address: int,
        description: Aprilaire8800AlarmPeriodDescription,
    ) -> None:
        """Initialise the select."""
        self._coordinator = coordinator
        self._address = address
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{address}_{description.key}"
        self._attr_device_info = coordinator.device_info(address)
        self._attr_options = [wire.lower() for wire in description.wire_values]

    @property
    def _node(self) -> NodeState | None:
        return self._coordinator.nodes.get(self._address)

    @property
    def available(self) -> bool:
        """Available once the node's period for this alarm has been read."""
        node = self._node
        return node is not None and self.entity_description.alarm in node.alarm_periods

    @property
    def current_option(self) -> str | None:
        """Return the current period as a lower-cased wire value."""
        node = self._node
        if node is None:
            return None
        wire = node.alarm_periods.get(self.entity_description.alarm)
        if wire is None:
            return None
        option = wire.lower()
        return option if option in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        """Push the chosen interval to the device."""
        await self._coordinator.async_set_alarm_period(
            self._address, self.entity_description.alarm, option.upper()
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to per-node update signals."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_NODE_UPDATED.format(address=self._address),
                self.async_write_ha_state,
            )
        )
