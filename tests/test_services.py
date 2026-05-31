# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the messaging services.

These require the full Home Assistant test environment. They follow HA's
testing guidelines: target resolution is verified through the public
service registry, not by reaching into internal helpers.

Run with:
    pip install pytest-homeassistant-custom-component
    pytest tests/test_services.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")
pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

import pytest_asyncio
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_rs485.const import DOMAIN
from custom_components.aprilaire_rs485.services import (
    SERVICE_CLEAR_PERMANENT_MESSAGE,
    SERVICE_CLEAR_TEMPORARY_MESSAGE,
    SERVICE_SEND_TEMPORARY_MESSAGE,
    SERVICE_SET_PERMANENT_MESSAGE,
    async_register_services,
)


@pytest_asyncio.fixture
async def coordinator_with_two_nodes(hass: HomeAssistant):
    """Install a fake coordinator with two nodes and register matching devices.

    Returns the AsyncMocks that stand in for the coordinator's set/clear
    methods so tests can assert call arguments.
    """
    set_msg = AsyncMock()
    clear_msg = AsyncMock()

    # Register a real (mock) config entry so the device registry will
    # accept devices linked to it. The newer HA device registry rejects
    # links to unknown config entries.
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, entry_id="fake_entry_id", state=ConfigEntryState.LOADED
    )
    entry.add_to_hass(hass)

    class FakeCoordinator:
        """Minimal stand-in matching the coordinator's set/clear surface."""

        nodes = {1: object(), 2: object()}
        async_set_display_message = set_msg
        async_clear_display_message = clear_msg
        config_entry_id = "fake_entry_id"

    coord = FakeCoordinator()
    entry.runtime_data = coord

    # Register two devices, one per node, plus the bus pseudo-device. The
    # services must resolve targets to the per-node devices and skip the bus.
    device_reg = dr.async_get(hass)
    device_reg.async_get_or_create(
        config_entry_id="fake_entry_id",
        identifiers={(DOMAIN, "1")},
        manufacturer="Aprilaire",
        model="8800",
        name="Thermostat 1",
    )
    device_reg.async_get_or_create(
        config_entry_id="fake_entry_id",
        identifiers={(DOMAIN, "2")},
        manufacturer="Aprilaire",
        model="8800",
        name="Thermostat 2",
    )
    device_reg.async_get_or_create(
        config_entry_id="fake_entry_id",
        identifiers={(DOMAIN, "bus")},
        manufacturer="Aprilaire",
        model="RS-485 bus",
        name="Aprilaire 8800 bus",
    )

    async_register_services(hass)
    return set_msg, clear_msg


async def test_send_temp_targets_one_device(
    hass: HomeAssistant, coordinator_with_two_nodes: tuple[AsyncMock, AsyncMock]
) -> None:
    """Targeting one device sends one message to that node's address."""
    set_msg, _clear_msg = coordinator_with_two_nodes

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "1")})
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_TEMPORARY_MESSAGE,
        {"device_id": [device.id], "message": "Filter change due"},
        blocking=True,
    )
    set_msg.assert_awaited_once_with(1, "TMPMES", "Filter change due")


async def test_send_temp_targets_multiple_devices(
    hass: HomeAssistant, coordinator_with_two_nodes: tuple[AsyncMock, AsyncMock]
) -> None:
    """Targeting multiple devices fans out one call per resolved address."""
    set_msg, _clear_msg = coordinator_with_two_nodes
    dev_reg = dr.async_get(hass)
    d1 = dev_reg.async_get_device(identifiers={(DOMAIN, "1")})
    d2 = dev_reg.async_get_device(identifiers={(DOMAIN, "2")})

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_TEMPORARY_MESSAGE,
        {"device_id": [d1.id, d2.id], "message": "Hi"},
        blocking=True,
    )
    assert set_msg.await_count == 2
    awaited_addrs = sorted(call.args[0] for call in set_msg.await_args_list)
    assert awaited_addrs == [1, 2]


async def test_send_temp_silently_skips_bus_device(
    hass: HomeAssistant, coordinator_with_two_nodes: tuple[AsyncMock, AsyncMock]
) -> None:
    """The bus pseudo-device is ignored when combined with a real thermostat."""
    set_msg, _clear_msg = coordinator_with_two_nodes
    dev_reg = dr.async_get(hass)
    real = dev_reg.async_get_device(identifiers={(DOMAIN, "1")})
    bus = dev_reg.async_get_device(identifiers={(DOMAIN, "bus")})

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_TEMPORARY_MESSAGE,
        {"device_id": [real.id, bus.id], "message": "Hi"},
        blocking=True,
    )
    set_msg.assert_awaited_once_with(1, "TMPMES", "Hi")


async def test_send_temp_only_bus_raises(
    hass: HomeAssistant, coordinator_with_two_nodes: tuple[AsyncMock, AsyncMock]
) -> None:
    """Targeting only the bus device resolves to nothing and raises."""
    _set, _clear = coordinator_with_two_nodes
    bus = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "bus")})
    with pytest.raises(HomeAssistantError, match="No Aprilaire 8800 thermostats"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_TEMPORARY_MESSAGE,
            {"device_id": [bus.id], "message": "Hi"},
            blocking=True,
        )


async def test_clear_temp_clears_slot(
    hass: HomeAssistant, coordinator_with_two_nodes: tuple[AsyncMock, AsyncMock]
) -> None:
    """clear_temporary_message calls the coordinator's clear with TMPMES."""
    _set, clear_msg = coordinator_with_two_nodes
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "1")})

    await hass.services.async_call(
        DOMAIN,
        SERVICE_CLEAR_TEMPORARY_MESSAGE,
        {"device_id": [device.id]},
        blocking=True,
    )
    clear_msg.assert_awaited_once_with(1, "TMPMES")


async def test_set_perm_uses_correct_slot(
    hass: HomeAssistant, coordinator_with_two_nodes: tuple[AsyncMock, AsyncMock]
) -> None:
    """The slot field is mapped to PMES<slot> on the wire."""
    set_msg, _clear = coordinator_with_two_nodes
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "1")})

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_PERMANENT_MESSAGE,
        {"device_id": [device.id], "slot": 3, "message": "Service 2026-06"},
        blocking=True,
    )
    set_msg.assert_awaited_once_with(1, "PMES3", "Service 2026-06")


async def test_set_perm_rejects_bad_slot(
    hass: HomeAssistant, coordinator_with_two_nodes: tuple[AsyncMock, AsyncMock]
) -> None:
    """Slot outside 1-4 is rejected by the service schema (voluptuous Invalid)."""
    _set, _clear = coordinator_with_two_nodes
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "1")})
    with pytest.raises(vol.Invalid, match="at most 4"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_PERMANENT_MESSAGE,
            {"device_id": [device.id], "slot": 5, "message": "X"},
            blocking=True,
        )


async def test_clear_perm_targets_correct_slot(
    hass: HomeAssistant, coordinator_with_two_nodes: tuple[AsyncMock, AsyncMock]
) -> None:
    """clear_permanent_message wipes only the named slot."""
    _set, clear_msg = coordinator_with_two_nodes
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "1")})

    await hass.services.async_call(
        DOMAIN,
        SERVICE_CLEAR_PERMANENT_MESSAGE,
        {"device_id": [device.id], "slot": 2},
        blocking=True,
    )
    clear_msg.assert_awaited_once_with(1, "PMES2")
