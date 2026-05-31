# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the clear-alarm button entities."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")
pytest.importorskip("homeassistant")

from custom_components.aprilaire_rs485.button import (
    BUTTON_DESCRIPTIONS,
    Aprilaire8800ClearAlarmButton,
)


class _FakeCoordinator:
    """Coordinator stand-in exposing what the button touches."""

    def __init__(self) -> None:
        self.nodes = {1: object()}
        self.async_clear_alarm = AsyncMock()

    def device_info(self, address: int) -> dict:
        return {}


def _desc(key: str):
    return next(d for d in BUTTON_DESCRIPTIONS if d.key == key)


@pytest.mark.parametrize(
    ("key", "expected_alarm"),
    [
        ("clear_alarm_filter", "FLT"),
        ("clear_alarm_water_panel", "WP"),
        ("clear_alarm_dehumidifier", "DEH"),
        ("clear_alarm_system", "SYS"),
    ],
)
async def test_press_clears_the_right_alarm(key: str, expected_alarm: str) -> None:
    """Pressing a button sends the matching short alarm code to the coordinator."""
    coord = _FakeCoordinator()
    button = Aprilaire8800ClearAlarmButton(coord, 1, _desc(key))
    assert button.available is True
    await button.async_press()
    coord.async_clear_alarm.assert_awaited_once_with(1, expected_alarm)


async def test_button_unavailable_when_node_absent() -> None:
    """A button for an unknown node is unavailable."""
    coord = _FakeCoordinator()
    button = Aprilaire8800ClearAlarmButton(coord, 1, BUTTON_DESCRIPTIONS[0])
    coord.nodes.clear()
    assert button.available is False
