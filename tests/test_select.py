# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the alarm-period select entities."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")
pytest.importorskip("homeassistant")

from custom_components.aprilaire_rs485.select import (
    SELECT_DESCRIPTIONS,
    Aprilaire8800AlarmPeriodSelect,
)


class _FakeNode:
    def __init__(self, periods: dict[str, str]) -> None:
        self.alarm_periods = periods


class _FakeCoordinator:
    def __init__(self, node: _FakeNode) -> None:
        self.nodes = {1: node}
        self.async_set_alarm_period = AsyncMock()

    def device_info(self, address: int) -> dict:
        return {}


def _desc(key: str):
    return next(d for d in SELECT_DESCRIPTIONS if d.key == key)


def _sel(key: str, periods: dict[str, str]):
    coord = _FakeCoordinator(_FakeNode(periods))
    return Aprilaire8800AlarmPeriodSelect(coord, 1, _desc(key)), coord


def test_filter_options_are_restricted() -> None:
    """The filter alarm offers only OFF/1/3/6/12, lower-cased."""
    sel, _ = _sel("alarm_period_filter", {})
    assert sel.options == ["off", "1", "3", "6", "12"]


def test_month_options_full_range() -> None:
    """The non-filter alarms offer OFF plus 1-12."""
    sel, _ = _sel("alarm_period_water_panel", {})
    assert sel.options == ["off", *(str(m) for m in range(1, 13))]


def test_current_option_reflects_wire_value() -> None:
    """A read period maps to its lower-cased option and makes the entity available."""
    sel, _ = _sel("alarm_period_filter", {"FLT": "6"})
    assert sel.available is True
    assert sel.current_option == "6"


def test_off_maps_to_lowercase_option() -> None:
    """The OFF wire value maps to the 'off' option."""
    sel, _ = _sel("alarm_period_system", {"SYS": "OFF"})
    assert sel.current_option == "off"


def test_unavailable_until_period_read() -> None:
    """Before the ALMP response arrives the entity is unavailable."""
    sel, _ = _sel("alarm_period_filter", {})
    assert sel.available is False
    assert sel.current_option is None


async def test_select_option_sends_uppercase_wire() -> None:
    """Choosing an interval sends the upper-cased wire value to the coordinator."""
    sel, coord = _sel("alarm_period_dehumidifier", {"DEH": "OFF"})
    await sel.async_select_option("6")
    coord.async_set_alarm_period.assert_awaited_once_with(1, "DEH", "6")


async def test_select_off_sends_off() -> None:
    """Choosing 'off' sends OFF."""
    sel, coord = _sel("alarm_period_filter", {"FLT": "12"})
    await sel.async_select_option("off")
    coord.async_set_alarm_period.assert_awaited_once_with(1, "FLT", "OFF")
