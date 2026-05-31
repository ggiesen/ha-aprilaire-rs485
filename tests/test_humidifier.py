# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Compose-logic tests for the humidifier/dehumidifier entities.

The Aprilaire 8800 humidistat has a single node ``MODE``
(OFF/HUMID/DEHUM/AUTO). The two ``HumidifierEntity`` toggles compose onto that
single mode the same way the core ``aprilaire`` integration's two humidifier
toggles do for its IP-based thermostats: enabling one direction while the
other is active means AUTO; disabling one while in AUTO leaves the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")
pytest.importorskip("homeassistant")

from custom_components.aprilaire_rs485.const import (
    CT_HUMIDISTAT,
    MODE_AUTO,
    MODE_DEHUM,
    MODE_HUMID,
    MODE_OFF,
)
from custom_components.aprilaire_rs485.humidifier import Aprilaire8800Humidifier


@dataclass
class _FakeNode:
    address: int
    controller_type: int = CT_HUMIDISTAT
    mode: str | None = None


class _FakeCoordinator:
    """Coordinator stand-in exposing what the humidifier entity touches."""

    def __init__(self, node: _FakeNode) -> None:
        self.nodes = {node.address: node}
        self.async_set_mode = AsyncMock()

    def device_info(self, address: int) -> dict:
        return {}


def _entity(start_mode: str, *, dehumidifier: bool):
    node = _FakeNode(address=1, mode=start_mode)
    coord = _FakeCoordinator(node)
    return Aprilaire8800Humidifier(coord, 1, dehumidifier=dehumidifier), coord


@pytest.mark.parametrize(
    ("dehumidifier", "start_mode", "turn_on", "expected"),
    [
        # Humidifier toggle composes with the dehumidify side.
        (False, MODE_OFF, True, MODE_HUMID),
        (False, MODE_DEHUM, True, MODE_AUTO),
        (False, MODE_AUTO, False, MODE_DEHUM),
        (False, MODE_HUMID, False, MODE_OFF),
        # Dehumidifier toggle composes with the humidify side.
        (True, MODE_OFF, True, MODE_DEHUM),
        (True, MODE_HUMID, True, MODE_AUTO),
        (True, MODE_AUTO, False, MODE_HUMID),
        (True, MODE_DEHUM, False, MODE_OFF),
    ],
)
async def test_toggle_composes_onto_node_mode(
    dehumidifier: bool, start_mode: str, turn_on: bool, expected: str
) -> None:
    """Turning a direction on/off maps to the correct combined node mode."""
    entity, coord = _entity(start_mode, dehumidifier=dehumidifier)
    if turn_on:
        await entity.async_turn_on()
    else:
        await entity.async_turn_off()
    coord.async_set_mode.assert_awaited_once_with(1, expected)


async def test_turn_on_no_node_is_noop() -> None:
    """With no known node, turning on does not call the coordinator."""
    node = _FakeNode(address=1, mode=MODE_OFF)
    coord = _FakeCoordinator(node)
    entity = Aprilaire8800Humidifier(coord, 1, dehumidifier=False)
    coord.nodes.clear()  # node disappeared
    await entity.async_turn_on()
    coord.async_set_mode.assert_not_awaited()
