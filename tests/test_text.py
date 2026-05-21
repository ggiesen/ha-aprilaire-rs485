# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the message-slot text platform.

These tests require the full Home Assistant test environment. They exercise
the text entity in isolation against a stand-in coordinator so the
behaviour is verified without spinning up the protocol thread or a real
config entry. The integration parts (entity in HA, restore on startup) are
covered by the targeted assertions on the entity's lifecycle hooks.

Run with:
    pip install pytest-homeassistant-custom-component
    pytest tests/test_text.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")
pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.aprilaire_rs485.const import (
    DOMAIN,
    SIGNAL_NODE_UPDATED,
)
from custom_components.aprilaire_rs485.text import (
    _SLOT_DESCRIPTIONS,
    Aprilaire8800MessageText,
)


@dataclass
class _FakeNode:
    """Stand-in for NodeState exposing only what the text entity touches."""

    address: int
    permanent_messages: dict[int, str] = field(default_factory=dict)


class _FakeCoordinator:
    """Stand-in coordinator with the surface area the text entity calls.

    Matches the real coordinator's contract: on every successful set/clear
    we update the shadow dict on the matching node AND fire
    SIGNAL_NODE_UPDATED. Tests that don't care about the dispatcher path
    just assert on the AsyncMocks; tests that do care subscribe via
    ``async_added_to_hass``.
    """

    def __init__(self, hass: HomeAssistant | None = None) -> None:
        self.hass = hass
        self.nodes: dict[int, _FakeNode] = {}
        self.async_set_display_message = AsyncMock(side_effect=self._fake_set)
        self.async_clear_display_message = AsyncMock(side_effect=self._fake_clear)

    async def _fake_set(self, addr: int, slot: str, text: str) -> None:
        node = self.nodes.setdefault(addr, _FakeNode(address=addr))
        if slot.startswith("PMES"):
            node.permanent_messages[int(slot[4:])] = text
            if self.hass is not None:
                async_dispatcher_send(self.hass, SIGNAL_NODE_UPDATED.format(address=addr))

    async def _fake_clear(self, addr: int, slot: str) -> None:
        node = self.nodes.setdefault(addr, _FakeNode(address=addr))
        if slot.startswith("PMES"):
            node.permanent_messages[int(slot[4:])] = ""
            if self.hass is not None:
                async_dispatcher_send(self.hass, SIGNAL_NODE_UPDATED.format(address=addr))

    def device_info(self, address: int) -> dict:
        return {"identifiers": {(DOMAIN, str(address))}}


@pytest.fixture
def coord_with_node() -> _FakeCoordinator:
    """Build a coordinator that already knows about node 1."""
    coord = _FakeCoordinator()
    coord.nodes[1] = _FakeNode(address=1)
    return coord


@pytest.fixture
def coord_with_node_and_hass(hass: HomeAssistant) -> _FakeCoordinator:
    """Like coord_with_node but wired to fire dispatcher signals."""
    coord = _FakeCoordinator(hass=hass)
    coord.nodes[1] = _FakeNode(address=1)
    return coord


def _build_entity(
    coord: _FakeCoordinator, address: int, slot_index: int
) -> Aprilaire8800MessageText:
    """Build a text entity for a given slot (1..4)."""
    description = _SLOT_DESCRIPTIONS[slot_index - 1]
    return Aprilaire8800MessageText(coord, address, description)


def test_initial_native_value_empty(coord_with_node: _FakeCoordinator) -> None:
    """A fresh entity with no shadow returns empty string, not None.

    HA's TextEntity treats None as unavailable; empty string is the
    correct 'cleared' value.
    """
    entity = _build_entity(coord_with_node, 1, 1)
    assert entity.native_value == ""


def test_unique_id_and_slot_parsing(coord_with_node: _FakeCoordinator) -> None:
    """unique_id is stable across the four slots and the slot is parsed."""
    entities = [_build_entity(coord_with_node, 1, i) for i in (1, 2, 3, 4)]
    ids = [e.unique_id for e in entities]
    assert ids == [
        f"{DOMAIN}_1_permanent_message_1",
        f"{DOMAIN}_1_permanent_message_2",
        f"{DOMAIN}_1_permanent_message_3",
        f"{DOMAIN}_1_permanent_message_4",
    ]
    assert [e._slot for e in entities] == [1, 2, 3, 4]


async def test_set_value_non_empty_calls_coordinator_set(
    hass: HomeAssistant, coord_with_node_and_hass: _FakeCoordinator
) -> None:
    """Non-empty value routes to async_set_display_message with the right slot.

    Verifies the contract twice: once on the mock call args (the entity
    sent the right command), and once on the entity's value AFTER the
    coordinator's dispatcher signal has propagated (the entity refreshed
    via the canonical path, not via a duplicated direct write).
    """
    entity = _build_entity(coord_with_node_and_hass, 1, 2)
    entity.hass = hass
    entity.entity_id = "text.thermostat_1_permanent_message_2"
    await entity.async_added_to_hass()

    await entity.async_set_value("FILTER DUE")
    await hass.async_block_till_done()

    coord_with_node_and_hass.async_set_display_message.assert_awaited_once_with(
        1, "PMES2", "FILTER DUE"
    )
    coord_with_node_and_hass.async_clear_display_message.assert_not_awaited()
    assert entity.native_value == "FILTER DUE"


async def test_set_value_empty_calls_coordinator_clear(
    hass: HomeAssistant, coord_with_node_and_hass: _FakeCoordinator
) -> None:
    """Empty string routes to async_clear_display_message."""
    # Start with a value present so we can verify it gets cleared.
    coord_with_node_and_hass.nodes[1].permanent_messages[3] = "OLD MSG"

    entity = _build_entity(coord_with_node_and_hass, 1, 3)
    entity.hass = hass
    entity.entity_id = "text.thermostat_1_permanent_message_3"
    await entity.async_added_to_hass()

    await entity.async_set_value("")
    await hass.async_block_till_done()

    coord_with_node_and_hass.async_clear_display_message.assert_awaited_once_with(1, "PMES3")
    coord_with_node_and_hass.async_set_display_message.assert_not_awaited()
    assert entity.native_value == ""


async def test_dispatcher_update_refreshes_native_value(
    hass: HomeAssistant, coord_with_node: _FakeCoordinator
) -> None:
    """Service-call writes that update the shadow propagate via dispatcher.

    Simulates the path a ``set_permanent_message`` service call takes: the
    coordinator updates its shadow then fires SIGNAL_NODE_UPDATED. The
    text entity is expected to pick that up and refresh its native value.
    """
    entity = _build_entity(coord_with_node, 1, 1)
    entity.hass = hass
    entity.entity_id = "text.thermostat_1_permanent_message_1"
    await entity.async_added_to_hass()

    # Simulate the coordinator updating its shadow (as the service handler
    # would have done by calling async_set_display_message on the real
    # coordinator) and firing the per-node updated signal.
    coord_with_node.nodes[1].permanent_messages[1] = "SERVICE WROTE THIS"
    async_dispatcher_send(hass, SIGNAL_NODE_UPDATED.format(address=1))
    await hass.async_block_till_done()

    assert entity.native_value == "SERVICE WROTE THIS"


async def test_dispatcher_update_for_other_slot_ignored(
    hass: HomeAssistant, coord_with_node: _FakeCoordinator
) -> None:
    """Updates to other slots on the same node don't affect this entity."""
    entity = _build_entity(coord_with_node, 1, 1)
    entity.hass = hass
    entity.entity_id = "text.thermostat_1_permanent_message_1"
    await entity.async_added_to_hass()

    # Touch slot 2; this entity tracks slot 1, so its value should remain "".
    coord_with_node.nodes[1].permanent_messages[2] = "SLOT TWO MSG"
    async_dispatcher_send(hass, SIGNAL_NODE_UPDATED.format(address=1))
    await hass.async_block_till_done()

    assert entity.native_value == ""


async def test_restore_seeds_shadow_when_empty(
    hass: HomeAssistant, coord_with_node: _FakeCoordinator, monkeypatch
) -> None:
    """If we restored a prior state and shadow is empty, seed it."""
    entity = _build_entity(coord_with_node, 1, 4)
    entity.hass = hass
    entity.entity_id = "text.thermostat_1_permanent_message_4"

    class _LastState:
        state = "PRIOR VALUE"

    async def _fake_last_state(self):  # noqa: ARG001
        return _LastState()

    monkeypatch.setattr(Aprilaire8800MessageText, "async_get_last_state", _fake_last_state)

    await entity.async_added_to_hass()

    assert coord_with_node.nodes[1].permanent_messages[4] == "PRIOR VALUE"
    assert entity.native_value == "PRIOR VALUE"


async def test_restore_does_not_clobber_fresher_shadow(
    hass: HomeAssistant, coord_with_node: _FakeCoordinator, monkeypatch
) -> None:
    """A write that landed before restore wins; the older value is ignored.

    This is the race we care about: HA starts up, the coordinator gets a
    write for slot 1 (e.g. another integration's startup automation), then
    the text entity's async_added_to_hass runs and tries to seed from the
    last state. The fresher write must not be clobbered.
    """
    # A write has already landed for slot 1.
    coord_with_node.nodes[1].permanent_messages[1] = "FRESH"

    entity = _build_entity(coord_with_node, 1, 1)
    entity.hass = hass
    entity.entity_id = "text.thermostat_1_permanent_message_1"

    class _LastState:
        state = "STALE"

    async def _fake_last_state(self):  # noqa: ARG001
        return _LastState()

    monkeypatch.setattr(Aprilaire8800MessageText, "async_get_last_state", _fake_last_state)

    await entity.async_added_to_hass()

    assert coord_with_node.nodes[1].permanent_messages[1] == "FRESH"
    assert entity.native_value == "FRESH"


async def test_restore_skips_unavailable_states(
    hass: HomeAssistant, coord_with_node: _FakeCoordinator, monkeypatch
) -> None:
    """unknown/unavailable from RestoreEntity does not poison the shadow."""
    entity = _build_entity(coord_with_node, 1, 1)
    entity.hass = hass
    entity.entity_id = "text.thermostat_1_permanent_message_1"

    class _LastState:
        state = "unknown"

    async def _fake_last_state(self):  # noqa: ARG001
        return _LastState()

    monkeypatch.setattr(Aprilaire8800MessageText, "async_get_last_state", _fake_last_state)

    await entity.async_added_to_hass()

    assert 1 not in coord_with_node.nodes[1].permanent_messages
    assert entity.native_value == ""


def test_description_max_length_matches_wire_limit() -> None:
    """The frontend max-length hint matches the wire limit (32 chars)."""
    for desc in _SLOT_DESCRIPTIONS:
        assert desc.native_max == 32
        assert desc.native_min == 0
