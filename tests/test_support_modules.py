# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for support-module discovery and per-module sensor entities.

Two test categories:

  1. ``parse_rsm`` and ``parse_rxsy_command`` in protocol.py - pure-Python
     parsers tested against the wire-format examples in the manual (p.32)
     directly. No HA dependency.
  2. The ``Aprilaire8800SupportModuleSensor`` entity itself - filtering
     rules, unit selection, availability. Requires HA imports.

State-mutation tests for the RSM and RxSy branches of _apply_to_state
live in ``test_coordinator.py`` where the load helper already exists.
"""

from __future__ import annotations

import pytest
from protocol import parse_rsm, parse_rxsy_command

# ---------- parse_rsm ----------


def test_parse_rsm_empty_returns_empty() -> None:
    """A node with no support modules returns an empty dict, not None."""
    assert parse_rsm("") == {}


def test_parse_rsm_single_module_two_sensors() -> None:
    """Manual p.32 example: M1:RT,RH."""
    assert parse_rsm("M1:RT,RH") == {(1, 1): "RT", (1, 2): "RH"}


def test_parse_rsm_multiple_modules() -> None:
    """Manual p.32 example: M1:CT,RH M3:CT,RT."""
    assert parse_rsm("M1:CT,RH M3:CT,RT") == {
        (1, 1): "CT",
        (1, 2): "RH",
        (3, 1): "CT",
        (3, 2): "RT",
    }


def test_parse_rsm_filters_xx() -> None:
    """XX placeholders are dropped - they mean 'no sensor'."""
    assert parse_rsm("M1:CT,XX") == {(1, 1): "CT"}
    assert parse_rsm("M2:XX,CH") == {(2, 2): "CH"}


def test_parse_rsm_rejects_invalid_module_address() -> None:
    """Module addresses outside 1..4 are silently dropped."""
    assert parse_rsm("M0:RT,RH M5:CT,RT") == {}


def test_parse_rsm_rejects_invalid_s1_codes() -> None:
    """S1 only accepts CT, RT, XX - CH and RH are S2-only types per manual."""
    result = parse_rsm("M1:CH,RT")
    assert (1, 1) not in result
    assert result.get((1, 2)) == "RT"


def test_parse_rsm_malformed_block_dropped() -> None:
    """Blocks without a colon are ignored, not raised."""
    assert parse_rsm("M1RTRH M2:CT,RT") == {(2, 1): "CT", (2, 2): "RT"}


def test_parse_rsm_extra_sensors_truncated() -> None:
    """If a device reports more than 2 sensor codes, drop the extras."""
    assert parse_rsm("M1:CT,RT,XX,XX") == {(1, 1): "CT", (1, 2): "RT"}


# ---------- parse_rxsy_command ----------


def test_parse_rxsy_valid_pairs() -> None:
    """All eight valid (module, sensor) pairs round-trip."""
    for m in (1, 2, 3, 4):
        for s in (1, 2):
            assert parse_rxsy_command(f"R{m}S{s}") == (m, s)


def test_parse_rxsy_case_insensitive() -> None:
    """Wire responses may use either case; both should parse."""
    assert parse_rxsy_command("r1s2") == (1, 2)
    assert parse_rxsy_command("R3S1") == (3, 1)


def test_parse_rxsy_rejects_out_of_range() -> None:
    """Module > 4 and sensor > 2 do not match the protocol grammar."""
    assert parse_rxsy_command("R5S1") is None
    assert parse_rxsy_command("R1S3") is None
    assert parse_rxsy_command("R0S0") is None


def test_parse_rxsy_rejects_unrelated_commands() -> None:
    """Other commands that vaguely look like the pattern don't match."""
    assert parse_rxsy_command("RSM") is None
    assert parse_rxsy_command("RTS") is None
    assert parse_rxsy_command("T") is None
    assert parse_rxsy_command("") is None


# ---------- entity tests (require HA) ----------

pytest.importorskip("pytest_homeassistant_custom_component")
pytest.importorskip("homeassistant")

from homeassistant.const import PERCENTAGE, UnitOfTemperature  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.aprilaire_rs485.const import DOMAIN  # noqa: E402
from custom_components.aprilaire_rs485.coordinator import (  # noqa: E402
    NodeState,
)
from custom_components.aprilaire_rs485.sensor import (  # noqa: E402
    Aprilaire8800SupportModuleSensor,
)


class _FakeCoordinator:
    """Minimal stand-in providing only what the entity reads from."""

    def __init__(self, node: NodeState) -> None:
        self.nodes = {node.address: node}

    def device_info(self, _addr: int) -> dict:
        return {"identifiers": {(DOMAIN, "1")}}

    def bus_device_info(self) -> dict:
        # Needed by the bus-level sensor classes that async_setup_entry
        # also creates. We don't assert anything about them in these
        # tests, but they have to construct successfully.
        return {"identifiers": {(DOMAIN, "bus")}}


def _node_with(
    topology: dict[tuple[int, int], str],
    readings: dict[tuple[int, int], tuple[float | int | None, str | None]] | None = None,
) -> NodeState:
    """Construct a NodeState with the given module topology and readings."""
    node = NodeState(address=1)
    node.support_modules = topology
    if readings:
        node.support_module_readings = readings
    return node


def test_entity_construction_sets_identifiers() -> None:
    """Unique ID and translation key follow the (module, sensor) pair."""
    coord = _FakeCoordinator(_node_with({(2, 1): "CT"}))
    ent = Aprilaire8800SupportModuleSensor(coord, 1, 2, 1, "CT")

    assert ent.unique_id == f"{DOMAIN}_1_module_2_sensor_1"
    assert ent.translation_key == "support_module_2_1"
    assert ent.extra_state_attributes == {
        "type_code": "CT",
        "module": "2",
        "sensor": "1",
    }


def test_entity_temperature_value_and_unit() -> None:
    """A populated F reading flows through to native_value with F unit."""
    coord = _FakeCoordinator(_node_with({(1, 1): "CT"}, {(1, 1): (72, "F")}))
    ent = Aprilaire8800SupportModuleSensor(coord, 1, 1, 1, "CT")

    assert ent.native_value == 72
    assert ent.native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT
    assert ent.available is True


def test_entity_temperature_celsius_unit() -> None:
    """A reading with scale C reports Celsius as the unit."""
    coord = _FakeCoordinator(_node_with({(1, 1): "CT"}, {(1, 1): (22, "C")}))
    ent = Aprilaire8800SupportModuleSensor(coord, 1, 1, 1, "CT")

    assert ent.native_value == 22
    assert ent.native_unit_of_measurement == UnitOfTemperature.CELSIUS


def test_entity_humidity_uses_percentage_regardless_of_reading_scale() -> None:
    """Humidity sensors always show % - the reading's scale field is ignored."""
    coord = _FakeCoordinator(_node_with({(2, 2): "RH"}, {(2, 2): (38, "%")}))
    ent = Aprilaire8800SupportModuleSensor(coord, 1, 2, 2, "RH")

    assert ent.native_unit_of_measurement == PERCENTAGE
    assert ent.native_value == 38


def test_entity_unavailable_when_no_reading() -> None:
    """Before the first RxSy response, the entity is unavailable."""
    coord = _FakeCoordinator(_node_with({(3, 1): "CT"}))
    ent = Aprilaire8800SupportModuleSensor(coord, 1, 3, 1, "CT")

    assert ent.available is False
    assert ent.native_value is None


def test_entity_unavailable_when_reading_is_dashes() -> None:
    """A '--' reading on the wire stores None and marks unavailable."""
    coord = _FakeCoordinator(_node_with({(1, 1): "CT"}, {(1, 1): (None, "F")}))
    ent = Aprilaire8800SupportModuleSensor(coord, 1, 1, 1, "CT")

    assert ent.available is False


def test_entity_unit_falls_back_to_fahrenheit_when_no_reading_yet() -> None:
    """Unit before the first reading defaults to F (the protocol's default scale)."""
    coord = _FakeCoordinator(_node_with({(1, 1): "CT"}))
    ent = Aprilaire8800SupportModuleSensor(coord, 1, 1, 1, "CT")

    assert ent.native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT


# ---------- platform setup filtering rules ----------
#
# These verify that ``async_setup_entry`` skips creating sensors for
# (1, 1)=RT and (1, 2)=RH so the user doesn't see those readings
# duplicated alongside the dedicated Outdoor temperature / humidity
# entities. We test through the platform setup function with a stubbed
# add_entities callback.


async def test_platform_skips_m1s1_when_type_is_rt(hass: HomeAssistant) -> None:
    """M1S1=RT is filtered out by async_setup_entry (it's the OT source)."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    from custom_components.aprilaire_rs485.sensor import (  # noqa: PLC0415
        async_setup_entry,
    )

    coord = _FakeCoordinator(_node_with({(1, 1): "RT", (1, 2): "CH", (2, 1): "CT"}))
    hass.data[DOMAIN] = {"fake_entry_id": coord}

    entry = MagicMock()
    entry.entry_id = "fake_entry_id"
    entry.async_on_unload = MagicMock()

    added: list = []

    def _add(entities):
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    sm_uids = {e.unique_id for e in added if isinstance(e, Aprilaire8800SupportModuleSensor)}
    # M1S1=RT skipped; M1S2=CH and M2S1=CT both created.
    assert f"{DOMAIN}_1_module_1_sensor_1" not in sm_uids
    assert f"{DOMAIN}_1_module_1_sensor_2" in sm_uids
    assert f"{DOMAIN}_1_module_2_sensor_1" in sm_uids


async def test_platform_skips_m1s2_when_type_is_rh(hass: HomeAssistant) -> None:
    """M1S2=RH is filtered out (it's the OH source)."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    from custom_components.aprilaire_rs485.sensor import (  # noqa: PLC0415
        async_setup_entry,
    )

    coord = _FakeCoordinator(_node_with({(1, 1): "CT", (1, 2): "RH"}))
    hass.data[DOMAIN] = {"fake_entry_id": coord}

    entry = MagicMock()
    entry.entry_id = "fake_entry_id"
    entry.async_on_unload = MagicMock()

    added: list = []

    def _add(entities):
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    sm_uids = {e.unique_id for e in added if isinstance(e, Aprilaire8800SupportModuleSensor)}
    # M1S2=RH skipped; M1S1=CT created.
    assert f"{DOMAIN}_1_module_1_sensor_1" in sm_uids
    assert f"{DOMAIN}_1_module_1_sensor_2" not in sm_uids


async def test_platform_does_not_skip_non_outdoor_rt_rh(
    hass: HomeAssistant,
) -> None:
    """Filter is positional - RT on M2S1 or RH on M3S2 are NOT skipped.

    The skip rule applies only to M1S1=RT and M1S2=RH because those are
    where outdoor sensors live per the manual. The same type code on
    other module positions is a legitimate per-room sensor.
    """
    from unittest.mock import MagicMock  # noqa: PLC0415

    from custom_components.aprilaire_rs485.sensor import (  # noqa: PLC0415
        async_setup_entry,
    )

    coord = _FakeCoordinator(_node_with({(2, 1): "RT", (3, 2): "RH"}))
    hass.data[DOMAIN] = {"fake_entry_id": coord}

    entry = MagicMock()
    entry.entry_id = "fake_entry_id"
    entry.async_on_unload = MagicMock()

    added: list = []

    def _add(entities):
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)

    sm_uids = {e.unique_id for e in added if isinstance(e, Aprilaire8800SupportModuleSensor)}
    assert f"{DOMAIN}_1_module_2_sensor_1" in sm_uids
    assert f"{DOMAIN}_1_module_3_sensor_2" in sm_uids
