# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Humidifier platform for the Aprilaire 8800 (RS-485) integration.

For each humidistat-mode node (``CT=1``) we create two entities - one
``HumidifierEntity`` with class ``humidifier`` and another with class
``dehumidifier`` - because HA's humidifier entity model is single-direction
while the 8800 can do humidify, dehumidify, or both via AUTO.

This area of the integration is the most likely to need rework on real
hardware. If the two-entity-per-node model proves confusing, replacing this
platform with a combination of ``number`` and ``select`` entities is a
reasonable alternative.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any

from homeassistant.components.humidifier import (
    HumidifierAction,
    HumidifierDeviceClass,
    HumidifierEntity,
    HumidifierEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CT_HUMIDISTAT,
    DOMAIN,
    MODE_AUTO,
    MODE_DEHUM,
    MODE_HUMID,
    MODE_OFF,
    SIGNAL_NODE_DISCOVERED,
    SIGNAL_NODE_UPDATED,
)
from .coordinator import Aprilaire8800Coordinator, NodeState

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up humidifier entities for humidistat-mode nodes."""
    coordinator: Aprilaire8800Coordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[int] = set()

    @callback
    def _maybe_add(address: int) -> None:
        if address in known:
            return
        node = coordinator.nodes.get(address)
        if node is None or node.controller_type is None:
            return
        if node.controller_type != CT_HUMIDISTAT:
            return
        known.add(address)
        async_add_entities(
            [
                Aprilaire8800Humidifier(coordinator, address, dehumidifier=False),
                Aprilaire8800Humidifier(coordinator, address, dehumidifier=True),
            ]
        )

    tracked: set[int] = set()

    @callback
    def _track_node(address: int) -> None:
        # Subscribe to this node's update signal once, so a CT response that
        # arrives after discovery (the common case - queries are answered
        # asynchronously on the RX thread, so controller_type is usually
        # still None at platform-setup time) re-triggers entity creation.
        # This also covers nodes that first appear after platform setup,
        # which would otherwise never get humidifier entities.
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


class Aprilaire8800Humidifier(HumidifierEntity):
    """Humidifier or dehumidifier entity for one humidistat-mode node."""

    _attr_has_entity_name = True
    _attr_max_humidity = 90
    _attr_min_humidity = 10
    _attr_should_poll = False
    _attr_supported_features = HumidifierEntityFeature(0)

    def __init__(
        self,
        coordinator: Aprilaire8800Coordinator,
        address: int,
        dehumidifier: bool,
    ) -> None:
        """Initialise the entity for the given direction."""
        self._coordinator = coordinator
        self._address = address
        self._dehumidifier = dehumidifier
        suffix = "dehumidifier" if dehumidifier else "humidifier"
        self._attr_unique_id = f"{DOMAIN}_{address}_{suffix}"
        self._attr_translation_key = suffix
        self._attr_name = suffix.capitalize()
        self._attr_device_class = (
            HumidifierDeviceClass.DEHUMIDIFIER if dehumidifier else HumidifierDeviceClass.HUMIDIFIER
        )
        self._attr_device_info = coordinator.device_info(address)

    @property
    def _node(self) -> NodeState | None:
        return self._coordinator.nodes.get(self._address)

    @property
    def available(self) -> bool:
        """Return whether the underlying node has been seen."""
        return self._node is not None

    @property
    def current_humidity(self) -> int | None:
        """Return the controlling humidity reading."""
        node = self._node
        return node.humidity if node else None

    @property
    def target_humidity(self) -> int | None:
        """Return the configured setpoint for this direction."""
        node = self._node
        if not node:
            return None
        return node.setpoint_dehum if self._dehumidifier else node.setpoint_humid

    @property
    def is_on(self) -> bool | None:
        """Return whether this direction is currently the active mode."""
        node = self._node
        if not node or node.mode is None:
            return None
        wanted = MODE_DEHUM if self._dehumidifier else MODE_HUMID
        return node.mode in (wanted, MODE_AUTO)

    @property
    def action(self) -> HumidifierAction | None:
        """Return the high-level action of this humidifier.

        We do not have wired feedback on the humidifier coil so we cannot
        distinguish ``HUMIDIFYING`` from ``IDLE``; we report ``IDLE`` and let
        the user infer from the relay state if they need it.
        """
        node = self._node
        if not node:
            return None
        if node.mode == MODE_OFF:
            return HumidifierAction.OFF
        return HumidifierAction.IDLE

    async def async_set_humidity(self, humidity: int) -> None:
        """Set the humidification or dehumidification setpoint."""
        if self._dehumidifier:
            await self._coordinator.async_set_dehum_setpoint(self._address, humidity)
        else:
            await self._coordinator.async_set_humid_setpoint(self._address, humidity)

    async def async_turn_on(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Activate this direction by setting the node mode accordingly."""
        mode = MODE_DEHUM if self._dehumidifier else MODE_HUMID
        await self._coordinator.async_set_mode(self._address, mode)

    async def async_turn_off(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Turn the node off entirely.

        Note: a node has a single system mode, so turning either direction
        ``off`` switches the node out of humidify/dehumidify entirely.
        """
        await self._coordinator.async_set_mode(self._address, MODE_OFF)

    async def async_added_to_hass(self) -> None:
        """Subscribe to per-node update signals."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_NODE_UPDATED.format(address=self._address),
                self.async_write_ha_state,
            )
        )
