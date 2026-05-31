# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Climate platform for the Aprilaire 8800 (RS-485) integration.

A ``climate`` entity is created for each node whose ``CT`` (controller type)
is 0 (thermostat). Humidistat-mode nodes (CT=1) get ``humidifier`` entities
instead and are skipped here.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CT_HUMIDISTAT,
    DOMAIN,
    FAN_AUTO,
    FAN_CIRC,
    FAN_ON,
    MODE_AUTO,
    MODE_COOL,
    MODE_EMHT,
    MODE_HEAT,
    MODE_OFF,
    SETPOINT_COOL_MAX_C,
    SETPOINT_COOL_MAX_F,
    SETPOINT_COOL_MIN_C,
    SETPOINT_COOL_MIN_F,
    SETPOINT_HEAT_MAX_C,
    SETPOINT_HEAT_MAX_F,
    SETPOINT_HEAT_MIN_C,
    SETPOINT_HEAT_MIN_F,
    SIGNAL_NODE_DISCOVERED,
    SIGNAL_NODE_UPDATED,
)
from .coordinator import Aprilaire8800Coordinator, NodeState

_LOGGER = logging.getLogger(__name__)

# Map between Aprilaire mode strings (the "verbose form" the device always
# returns per manual p.36) and Home Assistant HVAC modes.
_HA_TO_MODE = {
    HVACMode.COOL: MODE_COOL,
    HVACMode.HEAT: MODE_HEAT,
    HVACMode.HEAT_COOL: MODE_AUTO,
    HVACMode.OFF: MODE_OFF,
}

_MODE_TO_HA = {
    MODE_AUTO: HVACMode.HEAT_COOL,
    MODE_COOL: HVACMode.COOL,
    MODE_EMHT: HVACMode.HEAT,  # Emergency heat is surfaced as an attribute.
    MODE_HEAT: HVACMode.HEAT,
    MODE_OFF: HVACMode.OFF,
}

_FAN_TO_HA = {FAN_AUTO: "auto", FAN_CIRC: "circulate", FAN_ON: "on"}
_HA_TO_FAN = {v: k for k, v in _FAN_TO_HA.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up climate entities for thermostat-mode nodes."""
    coordinator: Aprilaire8800Coordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[int] = set()

    @callback
    def _maybe_add(address: int) -> None:
        if address in known:
            return
        node = coordinator.nodes.get(address)
        if node is None or node.controller_type is None:
            return
        if node.controller_type == CT_HUMIDISTAT:
            return
        known.add(address)
        async_add_entities([Aprilaire8800Climate(coordinator, address)])

    tracked: set[int] = set()

    @callback
    def _track_node(address: int) -> None:
        # Subscribe to this node's update signal once, so a CT response that
        # arrives after discovery (the common case - queries are answered
        # asynchronously on the RX thread, so controller_type is usually
        # still None at platform-setup time) re-triggers entity creation.
        # This also covers nodes that first appear after platform setup,
        # which would otherwise never get a climate entity.
        if address not in tracked:
            tracked.add(address)
            entry.async_on_unload(
                async_dispatcher_connect(
                    hass,
                    SIGNAL_NODE_UPDATED.format(address=address),
                    partial(_maybe_add, address),
                )
            )
        _maybe_add(address)

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NODE_DISCOVERED, _track_node))
    for addr in list(coordinator.nodes):
        _track_node(addr)


class Aprilaire8800Climate(ClimateEntity):
    """Climate entity for one thermostat-mode Aprilaire 8800 node."""

    _attr_fan_modes = ["auto", "circulate", "on"]
    _attr_has_entity_name = True
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.HEAT_COOL,
    ]
    _attr_name = None  # Use the device name.
    _attr_should_poll = False
    _attr_target_temperature_step = 1  # Device setpoints are whole degrees (F or C).

    def __init__(self, coordinator: Aprilaire8800Coordinator, address: int) -> None:
        """Initialise the climate entity."""
        self._coordinator = coordinator
        self._address = address
        self._attr_unique_id = f"{DOMAIN}_{address}_climate"
        self._attr_device_info = coordinator.device_info(address)

    @property
    def _node(self) -> NodeState | None:
        return self._coordinator.nodes.get(self._address)

    @property
    def available(self) -> bool:
        """Return whether the underlying node has been seen."""
        return self._node is not None

    @property
    def min_temp(self) -> float:
        """Lower bound for the active control, per the device setpoint ranges.

        HA uses one min/max for both the single setpoint and the range
        handles, so in heat_cool we expose the heat-setpoint floor (the low
        handle); single cool mode uses the cool floor. The device ignores
        out-of-range writes regardless, so this is a UX bound, not a guard.
        """
        celsius = self.temperature_unit == UnitOfTemperature.CELSIUS
        node = self._node
        if node and node.mode == MODE_COOL:
            return SETPOINT_COOL_MIN_C if celsius else SETPOINT_COOL_MIN_F
        return SETPOINT_HEAT_MIN_C if celsius else SETPOINT_HEAT_MIN_F

    @property
    def max_temp(self) -> float:
        """Upper bound for the active control, per the device setpoint ranges.

        In heat_cool this is the cool-setpoint ceiling (the high handle);
        single heat/emergency-heat modes use the heat ceiling.
        """
        celsius = self.temperature_unit == UnitOfTemperature.CELSIUS
        node = self._node
        if node and node.mode in (MODE_HEAT, MODE_EMHT):
            return SETPOINT_HEAT_MAX_C if celsius else SETPOINT_HEAT_MAX_F
        return SETPOINT_COOL_MAX_C if celsius else SETPOINT_COOL_MAX_F

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Expose the temperature feature appropriate to the current mode.

        HA renders the dual low/high range control only when
        TARGET_TEMPERATURE_RANGE is active, and the single setpoint control
        for TARGET_TEMPERATURE. Advertising both at once makes the frontend
        show a range slider even in single-setpoint heat/cool modes, so we
        switch based on the node's current mode. AUTO maps to the range
        view (heat + cool setpoints); everything else (including OFF, where
        HA hides the control anyway) uses the single setpoint.
        """
        features = (
            ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        node = self._node
        if node and node.mode == MODE_AUTO:
            return features | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        return features | ClimateEntityFeature.TARGET_TEMPERATURE

    @property
    def temperature_unit(self) -> str:
        """Return the unit reported by the device."""
        node = self._node
        if node and node.temperature_scale == "C":
            return UnitOfTemperature.CELSIUS
        return UnitOfTemperature.FAHRENHEIT

    @property
    def current_temperature(self) -> float | None:
        """Return the controlling temperature."""
        node = self._node
        return node.temperature if node else None

    @property
    def current_humidity(self) -> float | None:
        """Return the controlling humidity if the node also has one."""
        node = self._node
        return node.humidity if node else None

    @property
    def target_temperature(self) -> float | None:
        """Return the single setpoint relevant to the current mode."""
        node = self._node
        if not node:
            return None
        if node.mode in (MODE_HEAT, MODE_EMHT):
            return node.setpoint_heat
        if node.mode == MODE_COOL:
            return node.setpoint_cool
        return None

    @property
    def target_temperature_low(self) -> float | None:
        """Return the heat setpoint for the AUTO range view."""
        node = self._node
        return node.setpoint_heat if node else None

    @property
    def target_temperature_high(self) -> float | None:
        """Return the cool setpoint for the AUTO range view."""
        node = self._node
        return node.setpoint_cool if node else None

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return the current mode mapped to HA's HVACMode."""
        node = self._node
        if not node or node.mode is None:
            return None
        return _MODE_TO_HA.get(node.mode)

    @property
    def hvac_action(self) -> HVACAction | None:  # noqa: PLR0911
        """Infer what the equipment is doing from the relay state.

        The 8800 does not expose an explicit "calling for heat/cool" flag; we
        derive it from the relay map plus the current mode. Heat-pump aux-
        with-compressor edge cases may misclassify. Branching is intrinsic
        here so PLR0911 is suppressed.
        """
        node = self._node
        if not node:
            return None
        relays = node.relays
        if not relays:
            return None
        is_calling_heat = (
            relays.get("W1")
            or relays.get("W2")
            or (
                node.mode in (MODE_HEAT, MODE_EMHT, MODE_AUTO)
                and (relays.get("Y1") or relays.get("Y2"))
            )
        )
        is_calling_cool = node.mode == MODE_COOL and (relays.get("Y1") or relays.get("Y2"))
        if node.mode == MODE_OFF:
            if relays.get("G"):
                return HVACAction.FAN
            return HVACAction.OFF
        if is_calling_heat:
            return HVACAction.HEATING
        if is_calling_cool:
            return HVACAction.COOLING
        if relays.get("G"):
            return HVACAction.FAN
        return HVACAction.IDLE

    @property
    def fan_mode(self) -> str | None:
        """Return the current fan mode in HA form."""
        node = self._node
        if not node or node.fan_mode is None:
            return None
        return _FAN_TO_HA.get(node.fan_mode)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Surface a few node-specific bits that don't fit the climate model."""
        node = self._node
        if not node:
            return {}
        attrs: dict[str, Any] = {
            "hold_status": node.hold_status,
            "node_address": self._address,
            "outdoor_temperature": node.outdoor_temperature,
            "progressive_recovery": node.progressive_recovery,
        }
        if node.mode == MODE_EMHT:
            attrs["emergency_heat"] = True
        if node.deadband is not None:
            # Minimum gap the device enforces between heat and cool setpoints
            # in auto. HA has no way to enforce this in the slider, so it is
            # surfaced for visibility only; the device corrects violations and
            # reports the adjusted setpoints back via COS.
            attrs["auto_deadband"] = node.deadband
        if node.errors:
            attrs["errors"] = node.errors
        return attrs

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Push new setpoints to the device based on the current mode."""
        node = self._node
        if not node:
            return
        target = kwargs.get(ATTR_TEMPERATURE)
        low = kwargs.get("target_temp_low")
        high = kwargs.get("target_temp_high")
        if target is not None:
            if node.mode in (MODE_HEAT, MODE_EMHT):
                await self._coordinator.async_set_heat_setpoint(self._address, float(target))
            elif node.mode == MODE_COOL:
                await self._coordinator.async_set_cool_setpoint(self._address, float(target))
            else:
                _LOGGER.debug("Ignoring set_temperature in mode %s", node.mode)
        if low is not None:
            await self._coordinator.async_set_heat_setpoint(self._address, float(low))
        if high is not None:
            await self._coordinator.async_set_cool_setpoint(self._address, float(high))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the system mode."""
        mapped = _HA_TO_MODE.get(hvac_mode)
        if mapped is None:
            _LOGGER.warning("Unsupported HVAC mode %s", hvac_mode)
            return
        await self._coordinator.async_set_mode(self._address, mapped)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the fan mode."""
        mapped = _HA_TO_FAN.get(fan_mode)
        if mapped is None:
            return
        await self._coordinator.async_set_fan(self._address, mapped)

    async def async_added_to_hass(self) -> None:
        """Subscribe to per-node update signals."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_NODE_UPDATED.format(address=self._address),
                self.async_write_ha_state,
            )
        )
