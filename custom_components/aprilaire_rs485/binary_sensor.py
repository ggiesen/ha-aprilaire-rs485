# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Binary sensor platform for the Aprilaire 8800 (RS-485) integration.

Exposes per-thermostat alarms (filter, water panel, dehumidifier, system),
the six individual sensor and comm error flags from the ERROR= response,
the progressive-recovery flag, the host-driven HOLD network override flag,
and a bus-level connectivity sensor reflecting whether the transport is
currently open.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_NODE_DISCOVERED, SIGNAL_NODE_UPDATED
from .coordinator import Aprilaire8800Coordinator, NodeState


def _error_flag(node: NodeState, key: str) -> bool | None:
    """Return True if a given ERROR= severity field is non-zero, else False.

    Returns None when no ERROR= response has been received yet.
    """
    if not node.errors:
        return None
    return bool(node.errors.get(key, 0))


@dataclass(frozen=True, kw_only=True)
class Aprilaire8800BinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a per-node binary sensor backed by a NodeState field."""

    value_fn: Callable[[NodeState], bool | None]


BINARY_SENSOR_DESCRIPTIONS: tuple[Aprilaire8800BinarySensorEntityDescription, ...] = (
    # Maintenance alarms (the four FLT/WP/DEH/SYS alarms).
    Aprilaire8800BinarySensorEntityDescription(
        key="alarm_dehumidifier",
        translation_key="alarm_dehumidifier",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda n: n.alarm_dehumidifier,
    ),
    Aprilaire8800BinarySensorEntityDescription(
        key="alarm_filter",
        translation_key="alarm_filter",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda n: n.alarm_filter,
    ),
    Aprilaire8800BinarySensorEntityDescription(
        key="alarm_system",
        translation_key="alarm_system",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda n: n.alarm_system,
    ),
    Aprilaire8800BinarySensorEntityDescription(
        key="alarm_water_panel",
        translation_key="alarm_water_panel",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda n: n.alarm_water_panel,
    ),
    # Sensor/comm error flags from ERROR=NNNNNN (each digit 0-9; non-zero =
    # problem). All exposed as diagnostic entities since most installs will
    # never see one trip.
    Aprilaire8800BinarySensorEntityDescription(
        key="error_builtin_humidity",
        translation_key="error_builtin_humidity",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda n: _error_flag(n, "builtin_humidity"),
    ),
    Aprilaire8800BinarySensorEntityDescription(
        key="error_builtin_temp",
        translation_key="error_builtin_temp",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda n: _error_flag(n, "builtin_temp"),
    ),
    Aprilaire8800BinarySensorEntityDescription(
        key="error_comm",
        translation_key="error_comm",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda n: _error_flag(n, "comm"),
    ),
    Aprilaire8800BinarySensorEntityDescription(
        key="error_eeprom",
        translation_key="error_eeprom",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda n: _error_flag(n, "eeprom"),
    ),
    Aprilaire8800BinarySensorEntityDescription(
        key="error_outdoor_temp",
        translation_key="error_outdoor_temp",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda n: _error_flag(n, "outdoor_temp"),
    ),
    Aprilaire8800BinarySensorEntityDescription(
        key="error_remote_temp",
        translation_key="error_remote_temp",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda n: _error_flag(n, "remote_temp"),
    ),
    # Rollup of any error severity > 0.
    Aprilaire8800BinarySensorEntityDescription(
        key="error_any",
        translation_key="error_any",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda n: None if not n.errors else any(v != 0 for v in n.errors.values()),
    ),
    # Network override flag (HOLD command on the bus). Tells you whether the
    # thermostat is currently being held by a host-issued command.
    Aprilaire8800BinarySensorEntityDescription(
        key="network_override",
        translation_key="network_override",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda n: None if n.network_override is None else n.network_override == "ON",
    ),
    Aprilaire8800BinarySensorEntityDescription(
        key="progressive_recovery",
        translation_key="progressive_recovery",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda n: n.progressive_recovery,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary-sensor entities for all currently-known nodes and the bus."""
    coordinator: Aprilaire8800Coordinator = entry.runtime_data
    added: set[tuple[int, str]] = set()

    @callback
    def _add_for(address: int) -> None:
        new: list[BinarySensorEntity] = []
        for description in BINARY_SENSOR_DESCRIPTIONS:
            key = (address, description.key)
            if key in added:
                continue
            added.add(key)
            new.append(Aprilaire8800BinarySensor(coordinator, address, description))
        if new:
            async_add_entities(new)

    for addr in list(coordinator.nodes):
        _add_for(addr)
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NODE_DISCOVERED, _add_for))

    # Bus-level connectivity sensor (one per config entry).
    async_add_entities([Aprilaire8800BusConnectivity(coordinator)])


class Aprilaire8800BinarySensor(BinarySensorEntity):
    """Per-node binary sensor backed by one field of a NodeState."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: Aprilaire8800BinarySensorEntityDescription

    def __init__(
        self,
        coordinator: Aprilaire8800Coordinator,
        address: int,
        description: Aprilaire8800BinarySensorEntityDescription,
    ) -> None:
        """Initialise the entity."""
        self._coordinator = coordinator
        self._address = address
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{address}_{description.key}"
        self._attr_device_info = coordinator.device_info(address)

    @property
    def _node(self) -> NodeState | None:
        return self._coordinator.nodes.get(self._address)

    @property
    def available(self) -> bool:
        """Available only if we have a value for this flag."""
        node = self._node
        if node is None:
            return False
        return self.entity_description.value_fn(node) is not None

    @property
    def is_on(self) -> bool | None:
        """Return the current state of the flag."""
        node = self._node
        if node is None:
            return None
        return self.entity_description.value_fn(node)

    async def async_added_to_hass(self) -> None:
        """Subscribe to per-node update signals."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_NODE_UPDATED.format(address=self._address),
                self.async_write_ha_state,
            )
        )


class Aprilaire8800BusConnectivity(BinarySensorEntity):
    """Reflects whether the bus transport is currently open.

    ``True`` means the underlying serial/socket connection is up. It does
    not mean any particular thermostat is responding. A node going dark
    while the transport stays up will show via the per-node entities going
    unavailable as their values stop refreshing.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = True  # Polled because pyserial transport has no event hook.
    _attr_translation_key = "bus_connectivity"

    def __init__(self, coordinator: Aprilaire8800Coordinator) -> None:
        """Initialise the bus connectivity entity."""
        self._coordinator = coordinator
        self._attr_unique_id = f"{DOMAIN}_bus_connectivity"
        self._attr_device_info = coordinator.bus_device_info()

    @property
    def is_on(self) -> bool:
        """Return True when the transport is currently open."""
        return self._coordinator.protocol.is_connected
