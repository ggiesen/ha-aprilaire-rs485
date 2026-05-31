# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Config flow for the Aprilaire 8800 (RS-485) integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ADDRESSES,
    CONF_BAUD,
    CONF_MAX_ADDRESS,
    CONF_OUTDOOR_TEMP_REBROADCAST,
    CONF_OUTDOOR_TEMP_SOURCE,
    CONF_PORT,
    DEFAULT_BAUD,
    DEFAULT_MAX_ADDRESS,
    DOMAIN,
)

# The CONF_PORT field accepts anything pyserial.serial_for_url understands:
#   /dev/ttyUSB0          - Local serial (Linux), typical for the 8811 via USB-RS-232.
#   /dev/ttyAMA0          - Raspberry Pi UART.
#   COM3                  - Windows local serial.
#   hwgrep://USB-RS422    - Find local serial by device description.
#   socket://host:port    - TCP-to-RS-485 gateway (raw TCP).
#   rfc2217://host:port   - RFC 2217 remote serial.
#
# For TCP gateways the device side must be wired as 4-wire RS-422/485. A
# 2-wire half-duplex gateway will not work with the 8800 protocol.

_VALID_URL_PREFIXES = (
    "/",
    "COM",
    "com",
    "hwgrep://",
    "loop://",
    "rfc2217://",
    "socket://",
    "spy://",
)


def _looks_like_valid_url(value: str) -> bool:
    """Return True if value plausibly parses as a pyserial URL or device path."""
    value = value.strip()
    if not value:
        return False
    return any(value.startswith(prefix) for prefix in _VALID_URL_PREFIXES)


def _outdoor_temp_source_selector() -> selector.EntitySelector:
    """Entity picker for the outdoor temperature source.

    Filtered to the domains the coordinator can actually read: temperature
    sensors (numeric state) plus weather/climate entities (read from their
    ``temperature`` / ``current_temperature`` attribute). Shared by the config
    flow and the options flow.
    """
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter=[
                selector.EntityFilterSelectorConfig(domain="sensor", device_class="temperature"),
                selector.EntityFilterSelectorConfig(domain=["weather", "climate"]),
            ]
        )
    )


DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PORT, default="/dev/ttyUSB0"): str,
        vol.Optional(CONF_BAUD, default=DEFAULT_BAUD): vol.In([9600, 19200]),
        vol.Optional(CONF_MAX_ADDRESS, default=DEFAULT_MAX_ADDRESS): vol.All(
            int, vol.Range(min=1, max=64)
        ),
        # Comma-separated address list, e.g. "1,2,5". Empty -> discover.
        vol.Optional(CONF_ADDRESSES, default=""): str,
        # Outdoor temperature source entity. A temperature sensor (value read
        # from its numeric state), or a weather/climate entity (value read
        # from its temperature / current_temperature attribute). Omitted means
        # "no HA source". Picker filters to those domains so the user can't
        # choose an entity the coordinator can't read.
        vol.Optional(CONF_OUTDOOR_TEMP_SOURCE): _outdoor_temp_source_selector(),
        # If true, when no HA source is configured the integration rebroadcasts
        # outdoor temperature from the lowest-addressed thermostat that has its
        # own sensor to peers that don't. Defaults to true so the typical
        # multi-zone install gets working OT on every screen without extra
        # configuration.
        vol.Optional(CONF_OUTDOOR_TEMP_REBROADCAST, default=True): bool,
    }
)


class Aprilaire8800ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Aprilaire 8800 (RS-485)."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for editing runtime-tunable settings."""
        return Aprilaire8800OptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial user step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            url = (user_input.get(CONF_PORT) or "").strip()
            if not _looks_like_valid_url(url):
                errors[CONF_PORT] = "invalid_url"

            addresses_raw = (user_input.get(CONF_ADDRESSES) or "").strip()
            addresses: list[int] = []
            if addresses_raw:
                try:
                    addresses = sorted(
                        {int(s.strip()) for s in addresses_raw.split(",") if s.strip()}
                    )
                    if any(a < 1 or a > 64 for a in addresses):
                        raise ValueError
                except ValueError:
                    errors[CONF_ADDRESSES] = "invalid_addresses"

            ot_source = (user_input.get(CONF_OUTDOOR_TEMP_SOURCE) or "").strip()
            # If a source was given it must look like an entity_id - a domain
            # and an object_id separated by a dot. We don't verify the entity
            # actually exists here, since the user may configure the source
            # before adding the producing entity.
            if ot_source and ("." not in ot_source or ot_source.count(".") != 1):
                errors[CONF_OUTDOOR_TEMP_SOURCE] = "invalid_entity_id"

            if not errors:
                data = {
                    CONF_ADDRESSES: addresses,
                    CONF_BAUD: user_input[CONF_BAUD],
                    CONF_MAX_ADDRESS: user_input[CONF_MAX_ADDRESS],
                    CONF_PORT: url,
                    CONF_OUTDOOR_TEMP_SOURCE: ot_source,
                    CONF_OUTDOOR_TEMP_REBROADCAST: bool(
                        user_input.get(CONF_OUTDOOR_TEMP_REBROADCAST, True)
                    ),
                }
                await self.async_set_unique_id(f"{DOMAIN}:{url}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"Aprilaire 8800 ({url})", data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA,
            errors=errors,
        )


class Aprilaire8800OptionsFlow(OptionsFlow):
    """Edit runtime-tunable settings after initial setup.

    Only the outdoor-temperature source and rebroadcast toggle are editable
    here; the transport (port/baud/addresses) is fixed at setup time. Saving
    triggers a reload of the config entry (registered in ``__init__``) so the
    coordinator restarts with the new settings.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the options step."""
        if user_input is not None:
            source = (user_input.get(CONF_OUTDOOR_TEMP_SOURCE) or "").strip()
            return self.async_create_entry(
                data={
                    CONF_OUTDOOR_TEMP_SOURCE: source,
                    CONF_OUTDOOR_TEMP_REBROADCAST: bool(
                        user_input.get(CONF_OUTDOOR_TEMP_REBROADCAST, True)
                    ),
                }
            )

        # Pre-fill with the effective current values (options override data).
        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Optional(CONF_OUTDOOR_TEMP_SOURCE): _outdoor_temp_source_selector(),
                vol.Optional(CONF_OUTDOOR_TEMP_REBROADCAST, default=True): bool,
            }
        )
        suggested: dict[str, Any] = {
            CONF_OUTDOOR_TEMP_REBROADCAST: current.get(CONF_OUTDOOR_TEMP_REBROADCAST, True),
        }
        if current.get(CONF_OUTDOOR_TEMP_SOURCE):
            suggested[CONF_OUTDOOR_TEMP_SOURCE] = current[CONF_OUTDOOR_TEMP_SOURCE]
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
        )
