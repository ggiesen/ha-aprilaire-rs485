# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Text platform for the Aprilaire 8800 (RS-485) integration.

Exposes the four permanent (EEPROM-backed) message slots on each thermostat
as editable text entities. The transient TMPMES slot is intentionally not
exposed here - it is volatile (cleared on thermostat power loss), so a text
entity reflecting it would silently drift out of sync with reality. Use the
``send_temporary_message`` service for transient messages.

Design notes:

- The 8800 message slots are network-write-only; there is no read-back path
  on the wire. The integration shadows the last value written in the node
  state so the entity stays consistent across service calls, refreshes, and
  HA restarts.
- The entity uses RestoreEntity to seed the shadow on startup. This is
  best-effort: if someone power-cycled the thermostat while HA was down and
  the EEPROM somehow ended up different, the shadow will not reflect that
  until the next write. The slots are write-only anyway, so this is the
  best we can do.
- Writing an empty string clears the slot (the thermostat treats empty
  payload as "no message"). Writing a non-empty string normalises it
  through ``format_message_text`` before transmission, so the value the
  entity reports is the value the LCD actually shows.
"""

from __future__ import annotations

import logging

from homeassistant.components.text import TextEntity, TextEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, SIGNAL_NODE_DISCOVERED, SIGNAL_NODE_UPDATED
from .coordinator import (
    MESSAGE_MAX_LENGTH,
    Aprilaire8800Coordinator,
    format_message_text,
)

_LOGGER = logging.getLogger(__name__)

# One description per permanent slot. The slot number is parsed back out of
# the description key in the entity constructor.
_SLOT_DESCRIPTIONS: tuple[TextEntityDescription, ...] = tuple(
    TextEntityDescription(
        key=f"permanent_message_{i}",
        translation_key=f"permanent_message_{i}",
        entity_category=EntityCategory.CONFIG,
        native_max=MESSAGE_MAX_LENGTH,
        native_min=0,
        mode="text",
    )
    for i in range(1, 5)
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up four message-slot text entities per discovered node."""
    coordinator: Aprilaire8800Coordinator = hass.data[DOMAIN][entry.entry_id]
    added: set[tuple[int, str]] = set()

    @callback
    def _add_for(address: int) -> None:
        new: list[Aprilaire8800MessageText] = []
        for description in _SLOT_DESCRIPTIONS:
            key = (address, description.key)
            if key in added:
                continue
            added.add(key)
            new.append(Aprilaire8800MessageText(coordinator, address, description))
        if new:
            async_add_entities(new)

    for addr in list(coordinator.nodes):
        _add_for(addr)
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NODE_DISCOVERED, _add_for))


class Aprilaire8800MessageText(TextEntity, RestoreEntity):
    """A single PMES1..PMES4 slot exposed as an editable text entity.

    The native value mirrors what the thermostat LCD will display, not what
    the user originally typed. Differences arise from non-ASCII stripping,
    CR/LF collapsing to spaces, and 32-character truncation - all done by
    ``format_message_text`` in the coordinator.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: Aprilaire8800Coordinator,
        address: int,
        description: TextEntityDescription,
    ) -> None:
        """Initialise one message-slot entity for a specific node + slot."""
        self._coordinator = coordinator
        self._address = address
        # description.key is "permanent_message_<n>"; extract the slot number.
        self._slot = int(description.key.rsplit("_", 1)[1])
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{address}_{description.key}"
        self._attr_device_info = coordinator.device_info(address)
        self._attr_native_value = self._current_shadow_value()

    def _current_shadow_value(self) -> str:
        """Return whatever the coordinator has shadowed for this slot, or ''."""
        node = self._coordinator.nodes.get(self._address)
        if node is None:
            return ""
        return node.permanent_messages.get(self._slot, "")

    async def async_added_to_hass(self) -> None:
        """Restore the prior state and subscribe to coordinator updates.

        Restore happens before the dispatcher subscription so a write that
        races with startup wins over the restored value (the coordinator's
        shadow is the source of truth once a write has happened).
        """
        await super().async_added_to_hass()

        # Seed the coordinator's shadow from the last persisted state, but
        # only if nothing has been written this session - never clobber a
        # newer value with restored state.
        last_state = await self.async_get_last_state()
        if (
            last_state is not None
            and last_state.state is not None
            and last_state.state not in ("unknown", "unavailable")
        ):
            node = self._coordinator.nodes.get(self._address)
            if node is not None and self._slot not in node.permanent_messages:
                # format_message_text is idempotent on already-formatted
                # input; running it again costs nothing and guards against
                # restored values that bypass the formatter for any reason.
                node.permanent_messages[self._slot] = format_message_text(last_state.state)

        self._attr_native_value = self._current_shadow_value()

        # Pick up later writes (including service-call writes that bypass
        # this entity entirely) by listening for node-update signals.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_NODE_UPDATED.format(address=self._address),
                self._handle_node_update,
            )
        )

    @callback
    def _handle_node_update(self) -> None:
        """Refresh from the coordinator shadow if it has changed."""
        new_value = self._current_shadow_value()
        if new_value != self._attr_native_value:
            self._attr_native_value = new_value
            self.async_write_ha_state()

    async def async_set_value(self, value: str) -> None:
        """Push a new value to the thermostat.

        Empty string clears the slot. Non-empty values are sent verbatim;
        the coordinator handles formatting, shadow update, and dispatches
        SIGNAL_NODE_UPDATED, which this entity's _handle_node_update
        picks up to refresh its native_value. We do not duplicate that
        state write here.
        """
        slot_name = f"PMES{self._slot}"
        if value:
            await self._coordinator.async_set_display_message(self._address, slot_name, value)
        else:
            await self._coordinator.async_clear_display_message(self._address, slot_name)
