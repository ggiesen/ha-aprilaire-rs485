# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Sensor platform for the Aprilaire 8800 (RS-485) integration.

Three kinds of sensors get created:

  1. Per-thermostat sensors backed by a NodeState field (temperatures,
     humidities, hold status, firmware version).
  2. Per-support-module sensors, one for each remote or control sensor
     discovered on a node via the RSM query. Created dynamically as the
     topology is reported; placed in EntityCategory.DIAGNOSTIC so they
     don't clutter the default device view but are available for
     dashboards and automations that want per-room visibility.
  3. Bus-level diagnostic sensors (discovered node count and address list)
     attached to a single "Aprilaire 8800 bus" pseudo-device.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    SIGNAL_NODE_DISCOVERED,
    SIGNAL_NODE_SUPPORT_MODULES,
    SIGNAL_NODE_UPDATED,
)
from .coordinator import Aprilaire8800Coordinator, NodeState


def _temp_unit(node: NodeState, scale_attr: str) -> str:
    """Return HA's unit constant for the relevant scale attribute on a node."""
    scale = getattr(node, scale_attr, None)
    if scale == "C":
        return UnitOfTemperature.CELSIUS
    return UnitOfTemperature.FAHRENHEIT


@dataclass(frozen=True, kw_only=True)
class Aprilaire8800SensorEntityDescription(SensorEntityDescription):
    """Describes one of our per-node sensor entities.

    ``value_fn`` extracts the latest value from a NodeState; ``unit_fn``
    returns the unit (depends on per-node scale for temperatures).
    """

    value_fn: Callable[[NodeState], float | int | str | None]
    unit_fn: Callable[[NodeState], str | None]


SENSOR_DESCRIPTIONS: tuple[Aprilaire8800SensorEntityDescription, ...] = (
    Aprilaire8800SensorEntityDescription(
        key="builtin_humidity",
        translation_key="builtin_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda n: n.builtin_humidity,
        unit_fn=lambda _n: PERCENTAGE,
    ),
    Aprilaire8800SensorEntityDescription(
        key="firmware_info",
        translation_key="firmware_info",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda n: n.model_info,
        unit_fn=lambda _n: None,
    ),
    Aprilaire8800SensorEntityDescription(
        key="hold_status",
        translation_key="hold_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda n: n.hold_status,
        unit_fn=lambda _n: None,
    ),
    Aprilaire8800SensorEntityDescription(
        key="indoor_humidity",
        translation_key="indoor_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda n: n.humidity,
        unit_fn=lambda _n: PERCENTAGE,
    ),
    Aprilaire8800SensorEntityDescription(
        key="indoor_temperature",
        translation_key="indoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda n: n.temperature,
        unit_fn=lambda n: _temp_unit(n, "temperature_scale"),
    ),
    Aprilaire8800SensorEntityDescription(
        key="outdoor_humidity",
        translation_key="outdoor_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda n: n.outdoor_humidity,
        unit_fn=lambda _n: PERCENTAGE,
    ),
    Aprilaire8800SensorEntityDescription(
        key="outdoor_temperature",
        translation_key="outdoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda n: n.outdoor_temperature,
        unit_fn=lambda n: _temp_unit(n, "outdoor_temperature_scale"),
    ),
    Aprilaire8800SensorEntityDescription(
        key="remote_temperature",
        translation_key="remote_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda n: n.remote_temperature,
        unit_fn=lambda n: _temp_unit(n, "temperature_scale"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities for all currently-known nodes and the bus."""
    coordinator: Aprilaire8800Coordinator = hass.data[DOMAIN][entry.entry_id]
    added: set[tuple[int, str]] = set()

    @callback
    def _add_for(address: int) -> None:
        new: list[SensorEntity] = []
        for description in SENSOR_DESCRIPTIONS:
            key = (address, description.key)
            if key in added:
                continue
            added.add(key)
            new.append(Aprilaire8800Sensor(coordinator, address, description))
        if new:
            async_add_entities(new)

    @callback
    def _add_support_modules_for(address: int) -> None:
        """Create one sensor per support-module sensor on the given node.

        Skips ``(1, 1)`` when it's RT and ``(1, 2)`` when it's RH because
        those are the canonical outdoor-temperature and outdoor-humidity
        sources, already exposed through the dedicated OT/OH sensors.
        Creating per-module duplicates of those would just clutter the
        device card with two sensors showing the same reading.
        """
        node = coordinator.nodes.get(address)
        if node is None:
            return
        new: list[SensorEntity] = []
        for (module, sensor), type_code in node.support_modules.items():
            if module == 1 and sensor == 1 and type_code == "RT":
                continue  # Surfaced as "Outdoor temperature".
            if module == 1 and sensor == 2 and type_code == "RH":
                continue  # Surfaced as "Outdoor humidity".
            key = (address, f"module_{module}_sensor_{sensor}")
            if key in added:
                continue
            added.add(key)
            new.append(
                Aprilaire8800SupportModuleSensor(coordinator, address, module, sensor, type_code)
            )
        if new:
            async_add_entities(new)

    for addr in list(coordinator.nodes):
        _add_for(addr)
        # Some nodes may already have their RSM topology populated by the
        # time we set up (RSM is part of initial discovery). Add their
        # support-module sensors immediately.
        _add_support_modules_for(addr)

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NODE_DISCOVERED, _add_for))

    # Subscribe per-node to the support-modules signal. The signal is
    # per-node (formatted with address), so the only way to listen
    # generically is to subscribe at discovery time for each node.
    @callback
    def _subscribe_support_modules(address: int) -> None:
        entry.async_on_unload(
            async_dispatcher_connect(
                hass,
                SIGNAL_NODE_SUPPORT_MODULES.format(address=address),
                lambda _addr=address: _add_support_modules_for(_addr),
            )
        )
        _add_support_modules_for(address)

    for addr in list(coordinator.nodes):
        _subscribe_support_modules(addr)
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NODE_DISCOVERED, _subscribe_support_modules)
    )

    # Bus-level sensors: created once per config entry.
    async_add_entities(
        [
            Aprilaire8800BusCountSensor(coordinator),
            Aprilaire8800BusAddressesSensor(coordinator),
        ]
    )


class Aprilaire8800Sensor(SensorEntity):
    """Per-node sensor backed by one field of a NodeState."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: Aprilaire8800SensorEntityDescription

    def __init__(
        self,
        coordinator: Aprilaire8800Coordinator,
        address: int,
        description: Aprilaire8800SensorEntityDescription,
    ) -> None:
        """Initialise the sensor."""
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
        """Available only if the node and underlying value exist."""
        node = self._node
        if node is None:
            return False
        return self.entity_description.value_fn(node) is not None

    @property
    def native_value(self) -> float | int | str | None:
        """Return the latest value for this sensor."""
        node = self._node
        if node is None:
            return None
        return self.entity_description.value_fn(node)

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit appropriate to the current value."""
        node = self._node
        if node is None:
            return None
        return self.entity_description.unit_fn(node)

    async def async_added_to_hass(self) -> None:
        """Subscribe to per-node update signals."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_NODE_UPDATED.format(address=self._address),
                self.async_write_ha_state,
            )
        )


class Aprilaire8800BusCountSensor(SensorEntity):
    """Number of nodes currently known on the bus."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "node_count"

    def __init__(self, coordinator: Aprilaire8800Coordinator) -> None:
        """Initialise the bus-count sensor."""
        self._coordinator = coordinator
        self._attr_unique_id = f"{DOMAIN}_bus_node_count"
        self._attr_device_info = coordinator.bus_device_info()

    @property
    def native_value(self) -> int:
        """Return the count of currently known nodes."""
        return len(self._coordinator.nodes)

    async def async_added_to_hass(self) -> None:
        """Subscribe to discovery signals so the count refreshes."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_NODE_DISCOVERED,
                lambda _a: self.async_write_ha_state(),
            )
        )


class Aprilaire8800BusAddressesSensor(SensorEntity):
    """Comma-separated list of discovered node addresses."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "discovered_addresses"

    def __init__(self, coordinator: Aprilaire8800Coordinator) -> None:
        """Initialise the addresses sensor."""
        self._coordinator = coordinator
        self._attr_unique_id = f"{DOMAIN}_bus_addresses"
        self._attr_device_info = coordinator.bus_device_info()

    @property
    def native_value(self) -> str:
        """Return discovered addresses as a comma-separated string."""
        addrs = sorted(self._coordinator.nodes)
        return ",".join(str(a) for a in addrs) if addrs else "none"

    async def async_added_to_hass(self) -> None:
        """Subscribe to discovery signals so the list refreshes."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_NODE_DISCOVERED,
                lambda _a: self.async_write_ha_state(),
            )
        )


# Type-code semantics from manual p.32:
#   CT - control temperature sensor (feeds into the controlling temp the
#        thermostat uses; averaged with peers if multiple CTs are present)
#   RT - remote temperature sensor (display only)
#   CH - control humidity sensor
#   RH - remote humidity sensor (display only)
#
# We map each to a (device_class, unit_fn) so the entity reports the
# right HA semantics for graphing and unit display.
_TEMP_TYPES = frozenset({"CT", "RT"})
_HUMIDITY_TYPES = frozenset({"CH", "RH"})


class Aprilaire8800SupportModuleSensor(SensorEntity):
    """One sensor on one of a node's external support modules.

    Reads from ``NodeState.support_module_readings[(module, sensor)]``.
    The sensor's device class is fixed at creation time based on the type
    code reported by the node's RSM response; if the user re-cables a
    module the integration must be reloaded to re-create the entities.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: Aprilaire8800Coordinator,
        address: int,
        module: int,
        sensor: int,
        type_code: str,
    ) -> None:
        """Initialise the entity for one (module, sensor) pair on a node."""
        self._coordinator = coordinator
        self._address = address
        self._module = module
        self._sensor = sensor
        self._type_code = type_code
        self._attr_unique_id = f"{DOMAIN}_{address}_module_{module}_sensor_{sensor}"
        self._attr_translation_key = f"support_module_{module}_{sensor}"
        self._attr_device_info = coordinator.device_info(address)
        # Device-class / unit are derived once from the type code. Whether
        # the underlying NodeState scale is F or C is decided per-update
        # by native_unit_of_measurement.
        if type_code in _TEMP_TYPES:
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
        elif type_code in _HUMIDITY_TYPES:
            self._attr_device_class = SensorDeviceClass.HUMIDITY
        # XX is filtered out before instantiation; any other unrecognised
        # code leaves the device class unset, which is the safest fallback.

    @property
    def _node(self) -> NodeState | None:
        return self._coordinator.nodes.get(self._address)

    @property
    def _reading(self) -> tuple[float | int | None, str | None] | None:
        node = self._node
        if node is None:
            return None
        return node.support_module_readings.get((self._module, self._sensor))

    @property
    def available(self) -> bool:
        """Available only when the node has reported a numeric value."""
        reading = self._reading
        return reading is not None and reading[0] is not None

    @property
    def native_value(self) -> float | int | None:
        """Latest reading for this sensor, or None if not yet known."""
        reading = self._reading
        if reading is None:
            return None
        return reading[0]

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Unit derived from the type code and the device-reported scale."""
        if self._type_code in _HUMIDITY_TYPES:
            return PERCENTAGE
        reading = self._reading
        if reading is None or reading[1] is None:
            # Fall back to Fahrenheit so the entity isn't unitless when
            # the first reading hasn't yet been received. Once it arrives
            # the scale will be set correctly.
            return UnitOfTemperature.FAHRENHEIT
        return UnitOfTemperature.CELSIUS if reading[1] == "C" else UnitOfTemperature.FAHRENHEIT

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Expose the type code so users can see CT/RT/CH/RH at a glance."""
        return {
            "type_code": self._type_code,
            "module": str(self._module),
            "sensor": str(self._sensor),
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to per-node update signals."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_NODE_UPDATED.format(address=self._address),
                self.async_write_ha_state,
            )
        )
