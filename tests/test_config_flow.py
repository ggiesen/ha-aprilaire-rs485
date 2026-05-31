# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the Aprilaire 8800 config flow.

These tests follow the Home Assistant testing guidelines: they interact with
the integration only via Home Assistant's public interfaces (``hass.config_entries``,
``async_setup_component``) and never reach into internal state.

They require the ``pytest-homeassistant-custom-component`` package and the
full Home Assistant test environment. Without those, the entire module is
skipped so the protocol-only test run still passes.

Run with:
    pip install pytest-homeassistant-custom-component
    pytest tests/test_config_flow.py
"""

from __future__ import annotations

import pytest

# Skip the whole module unless the HA test framework is importable.
pytest.importorskip("pytest_homeassistant_custom_component")
pytest.importorskip("homeassistant")

# Imports below this point are deferred to runtime via the fixtures so that
# the module-level importorskip can short-circuit cleanly.
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aprilaire_rs485.const import (
    CONF_ADDRESSES,
    CONF_BAUD,
    CONF_MAX_ADDRESS,
    CONF_OUTDOOR_TEMP_REBROADCAST,
    CONF_OUTDOOR_TEMP_SOURCE,
    CONF_PORT,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations):
    """Wire HA's loader to discover ``custom_components/aprilaire_rs485``.

    Without this fixture the test environment refuses to load the
    integration ("Cannot find integration aprilaire_rs485"). It is the
    standard pattern documented by pytest-homeassistant-custom-component.
    The ``enable_custom_integrations`` argument is the fixture-injection
    side-effect; we don't reference it directly.
    """


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """A valid form submission creates a config entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PORT: "loop://",
            CONF_BAUD: 9600,
            CONF_MAX_ADDRESS: 4,
            CONF_ADDRESSES: "1,2",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"].startswith("Aprilaire 8800")
    assert result["data"][CONF_PORT] == "loop://"
    assert result["data"][CONF_BAUD] == 9600
    assert result["data"][CONF_MAX_ADDRESS] == 4
    assert result["data"][CONF_ADDRESSES] == [1, 2]


async def test_user_flow_rejects_invalid_url(hass: HomeAssistant) -> None:
    """An obviously malformed URL is rejected with an error key."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PORT: "not-a-url",
            CONF_BAUD: 9600,
            CONF_MAX_ADDRESS: 4,
            CONF_ADDRESSES: "",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PORT: "invalid_url"}


async def test_user_flow_rejects_addresses_out_of_range(hass: HomeAssistant) -> None:
    """Addresses outside 1..64 produce an invalid_addresses error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PORT: "/dev/ttyUSB0",
            CONF_BAUD: 9600,
            CONF_MAX_ADDRESS: 4,
            CONF_ADDRESSES: "0,5",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ADDRESSES: "invalid_addresses"}


async def test_user_flow_prevents_duplicate_port(hass: HomeAssistant) -> None:
    """The same port can only be configured once.

    The unique_id is derived from the port string, so a second flow with the
    same port should abort.
    """
    # First entry succeeds.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PORT: "loop://",
            CONF_BAUD: 9600,
            CONF_MAX_ADDRESS: 4,
            CONF_ADDRESSES: "",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # Second entry with the same port aborts.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PORT: "loop://",
            CONF_BAUD: 9600,
            CONF_MAX_ADDRESS: 4,
            CONF_ADDRESSES: "",
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_sets_outdoor_temp_source(hass: HomeAssistant) -> None:
    """The options flow writes the chosen source and rebroadcast to entry.options."""
    hass.states.async_set(
        "sensor.outdoor",
        "12",
        {"device_class": "temperature", "unit_of_measurement": "°C"},
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PORT: "loop://",
            CONF_OUTDOOR_TEMP_SOURCE: "",
            CONF_OUTDOOR_TEMP_REBROADCAST: True,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_OUTDOOR_TEMP_SOURCE: "sensor.outdoor",
            CONF_OUTDOOR_TEMP_REBROADCAST: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_OUTDOOR_TEMP_SOURCE] == "sensor.outdoor"
    assert entry.options[CONF_OUTDOOR_TEMP_REBROADCAST] is False


async def test_options_flow_clears_outdoor_temp_source(hass: HomeAssistant) -> None:
    """Omitting the source clears it (empty string), leaving rebroadcast as the fallback."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PORT: "loop://"},
        options={CONF_OUTDOOR_TEMP_SOURCE: "sensor.old", CONF_OUTDOOR_TEMP_REBROADCAST: False},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_OUTDOOR_TEMP_REBROADCAST: True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_OUTDOOR_TEMP_SOURCE] == ""
    assert entry.options[CONF_OUTDOOR_TEMP_REBROADCAST] is True
