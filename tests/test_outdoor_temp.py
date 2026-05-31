# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for outdoor temperature push.

Two test classes worth of behaviour:

1. Pure-Python: ``_clamp_outdoor`` (a staticmethod) and the OT branch of
   ``_apply_to_state`` (also a staticmethod) - extracted from source and
   exec'd in a sandbox so the tests don't need Home Assistant.
2. HA-coupled: the instance methods that read HA state and resolve the
   broadcast value. These require ``homeassistant`` to import the full
   coordinator module.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from protocol import (
    decode_errors,
    decode_humidity,
    decode_hvac,
    decode_temperature,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COORD_PATH = _REPO_ROOT / "custom_components" / "aprilaire_rs485" / "coordinator.py"


def _load_pure_python_helpers():
    """Extract ``_apply_to_state`` and ``_clamp_outdoor`` for HA-free testing.

    Mirrors the pattern in test_coordinator.py - we parse the NodeState
    dataclass and the two staticmethods out of coordinator.py and exec
    them in a sandbox. The exec block fails loudly if source structure
    shifts, which is exactly what we want (refactors must update tests).
    """
    src = _COORD_PATH.read_text(encoding="utf-8")

    ns_start = src.index("@dataclass\nclass NodeState")
    ns_end = src.index("\n\nclass Aprilaire8800Coordinator")
    nodestate_block = src[ns_start:ns_end]

    apply_start = src.index("    @staticmethod\n    def _apply_to_state")
    apply_end = src.index("\n\n\ndef _format_setpoint")
    apply_block = src[apply_start:apply_end]
    apply_block = "\n".join(
        line[4:] if line.startswith("    ") else line for line in apply_block.splitlines()
    )
    apply_block = apply_block.replace("@staticmethod\n", "")

    clamp_marker = "    @staticmethod\n    def _clamp_outdoor"
    clamp_start = src.index(clamp_marker)
    # The next stable marker after _clamp_outdoor is the TX helpers section.
    clamp_end = src.index("    # ---------- TX helpers ----------", clamp_start)
    clamp_src = src[clamp_start:clamp_end]
    clamp_src = "\n".join(
        line[4:] if line.startswith("    ") else line for line in clamp_src.splitlines()
    )
    clamp_src = clamp_src.replace("@staticmethod\n", "")

    ns: dict[str, object] = {
        "__name__": "test_outdoor_state",
        "decode_temperature": decode_temperature,
        "decode_humidity": decode_humidity,
        "decode_hvac": decode_hvac,
        "decode_errors": decode_errors,
    }
    fake_mod = types.ModuleType("test_outdoor_state")
    fake_mod.__dict__.update(ns)
    sys.modules["test_outdoor_state"] = fake_mod
    ns = fake_mod.__dict__

    exec(
        "from __future__ import annotations\n"
        "import contextlib\n"
        "from dataclasses import dataclass, field\n",
        ns,
    )
    exec(nodestate_block, ns)
    exec(apply_block, ns)
    exec(clamp_src, ns)
    return ns["NodeState"], ns["_apply_to_state"], ns["_clamp_outdoor"]


NodeState, apply_to_state, clamp_outdoor = _load_pure_python_helpers()


# ---------- _clamp_outdoor ----------


def test_clamp_outdoor_fahrenheit_within_range() -> None:
    """Values inside the F range pass through unchanged."""
    assert clamp_outdoor(72, "F") == (72, "F")
    assert clamp_outdoor(-40, "F") == (-40, "F")
    assert clamp_outdoor(130, "F") == (130, "F")


def test_clamp_outdoor_fahrenheit_clamps_high() -> None:
    """Values above the F range get pulled down to the maximum."""
    assert clamp_outdoor(200, "F") == (130, "F")


def test_clamp_outdoor_fahrenheit_clamps_low() -> None:
    """Values below the F range get pulled up to the minimum."""
    assert clamp_outdoor(-100, "F") == (-40, "F")


def test_clamp_outdoor_celsius_within_range() -> None:
    """Values inside the C range pass through unchanged."""
    assert clamp_outdoor(20, "C") == (20, "C")
    assert clamp_outdoor(-40, "C") == (-40, "C")
    assert clamp_outdoor(55, "C") == (55, "C")


def test_clamp_outdoor_celsius_clamps_high() -> None:
    """Above the C range gets pulled down. F max 130 differs from C max 55."""
    assert clamp_outdoor(80, "C") == (55, "C")


def test_clamp_outdoor_celsius_clamps_low() -> None:
    """Below the C range gets pulled up."""
    assert clamp_outdoor(-100, "C") == (-40, "C")


# ---------- has_own_outdoor_sensor inference ----------


def test_real_ot_response_sets_owns_sensor_true() -> None:
    """An OT response with a real value sets the flag to True."""
    node = NodeState(address=1)
    apply_to_state(node, "OT", "-5F")
    assert node.has_own_outdoor_sensor is True
    assert node.outdoor_temperature == -5


def test_dashes_ot_response_sets_owns_sensor_false() -> None:
    """An OT response of '--' marks the node as having no sensor."""
    node = NodeState(address=1)
    apply_to_state(node, "OT", "--F")
    assert node.has_own_outdoor_sensor is False
    assert node.outdoor_temperature is None


def test_real_after_dashes_upgrades_to_true() -> None:
    """If '--' was the first response and a real one follows, flip to True."""
    node = NodeState(address=1)
    apply_to_state(node, "OT", "--F")
    assert node.has_own_outdoor_sensor is False
    apply_to_state(node, "OT", "72F")
    assert node.has_own_outdoor_sensor is True


def test_dashes_after_real_keeps_true() -> None:
    """Once we know a node has a sensor, a transient '--' should not unset it.

    The flag tracks 'is this device wired with a sensor at all.' A
    momentary read failure shouldn't make us forget that. Only the
    initial response sets False; later '--' responses are transient.
    """
    node = NodeState(address=1)
    apply_to_state(node, "OT", "72F")
    assert node.has_own_outdoor_sensor is True
    apply_to_state(node, "OT", "--F")
    assert node.has_own_outdoor_sensor is True
    assert node.outdoor_temperature is None


# ---------- HA-coupled tests for resolve / read ----------


@pytest.fixture
def coord_factory():
    """Build a partially-initialised Aprilaire8800Coordinator for resolution tests.

    Bypasses __init__ to avoid constructing the protocol/transport. Sets
    only the attributes the resolution path touches. Requires Home
    Assistant to be importable.
    """
    pytest.importorskip("homeassistant")
    # These imports are intentionally inside the fixture: at module-import
    # time `homeassistant` may not be available (protocol-only test runs),
    # in which case the importorskip above causes the whole fixture to skip.
    # Hoisting them to the top of the file would crash module collection.
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        Aprilaire8800Coordinator,
    )
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        NodeState as RealNodeState,
    )

    class _FakeState:
        def __init__(
            self, state: str, unit: str | None = None, attributes: dict | None = None
        ) -> None:
            self.state = state
            self.attributes = dict(attributes) if attributes else {}
            if unit:
                self.attributes.setdefault("unit_of_measurement", unit)

    class _FakeStates:
        def __init__(self, store: dict) -> None:
            self._store = store

        def get(self, entity_id):
            return self._store.get(entity_id)

    class _FakeUnitSystem:
        def __init__(self, temperature_unit: str) -> None:
            self.temperature_unit = temperature_unit

    class _FakeConfig:
        def __init__(self, temperature_unit: str) -> None:
            self.units = _FakeUnitSystem(temperature_unit)

    class _FakeHass:
        def __init__(self, states: dict, system_temperature_unit: str = "°F") -> None:
            self.states = _FakeStates(states)
            self.config = _FakeConfig(system_temperature_unit)

    def _make(hass_states, nodes, source=None, rebroadcast=True, system_temperature_unit="°F"):
        coord = Aprilaire8800Coordinator.__new__(Aprilaire8800Coordinator)
        coord.hass = _FakeHass(
            {
                k: _FakeState(v[0], v[1] if len(v) > 1 else None, v[2] if len(v) > 2 else None)
                for k, v in hass_states.items()
            },
            system_temperature_unit=system_temperature_unit,
        )
        coord._ot_source = source
        coord._ot_rebroadcast = rebroadcast
        coord.nodes = {}
        for addr, fields in nodes.items():
            node = RealNodeState(address=addr)
            for k, v in fields.items():
                setattr(node, k, v)
            coord.nodes[addr] = node
        return coord

    return _make


def test_resolve_uses_ha_source_when_available(coord_factory) -> None:
    """HA entity takes priority over rebroadcast even if both are configured."""
    coord = coord_factory(
        hass_states={"sensor.outdoor": ("72.4", "°F")},
        nodes={
            1: {
                "has_own_outdoor_sensor": True,
                "outdoor_temperature": 65.0,
                "outdoor_temperature_scale": "F",
            },
        },
        source="sensor.outdoor",
    )
    assert coord._resolve_outdoor_temp_value() == (72, "F")


def test_resolve_ha_celsius_unit(coord_factory) -> None:
    """A Celsius HA source produces a Celsius broadcast."""
    coord = coord_factory(
        hass_states={"sensor.outdoor": ("23.6", "°C")},
        nodes={},
        source="sensor.outdoor",
    )
    assert coord._resolve_outdoor_temp_value() == (24, "C")


def test_resolve_skips_unavailable_ha_source(coord_factory) -> None:
    """An unavailable HA source returns None with no fallback configured."""
    coord = coord_factory(
        hass_states={"sensor.outdoor": ("unavailable",)},
        nodes={},
        source="sensor.outdoor",
        rebroadcast=False,
    )
    assert coord._resolve_outdoor_temp_value() is None


def test_resolve_skips_non_numeric_ha_source(coord_factory) -> None:
    """A garbage HA source value returns None."""
    coord = coord_factory(
        hass_states={"sensor.outdoor": ("warm",)},
        nodes={},
        source="sensor.outdoor",
        rebroadcast=False,
    )
    assert coord._resolve_outdoor_temp_value() is None


def test_resolve_weather_entity_uses_temperature_attribute(coord_factory) -> None:
    """A weather entity's state is a condition; the value comes from its
    temperature attribute, with the scale from temperature_unit."""
    coord = coord_factory(
        hass_states={
            "weather.home": ("partlycloudy", None, {"temperature": 12.4, "temperature_unit": "°C"})
        },
        nodes={},
        source="weather.home",
        rebroadcast=False,
    )
    assert coord._resolve_outdoor_temp_value() == (12, "C")


def test_resolve_climate_entity_uses_current_temperature_and_system_unit(coord_factory) -> None:
    """A climate entity's value comes from current_temperature; with no unit
    attribute the scale falls back to the HA system unit."""
    coord = coord_factory(
        hass_states={"climate.den": ("heat", None, {"current_temperature": 68.0})},
        nodes={},
        source="climate.den",
        rebroadcast=False,
        system_temperature_unit="°F",
    )
    assert coord._resolve_outdoor_temp_value() == (68, "F")


def test_resolve_missing_ha_entity_falls_back(coord_factory) -> None:
    """An unknown entity_id falls through to rebroadcast if enabled."""
    coord = coord_factory(
        hass_states={},
        nodes={
            1: {
                "has_own_outdoor_sensor": True,
                "outdoor_temperature": 58.0,
                "outdoor_temperature_scale": "F",
            },
        },
        source="sensor.does_not_exist",
        rebroadcast=True,
    )
    assert coord._resolve_outdoor_temp_value() == (58, "F")


def test_resolve_falls_through_to_rebroadcast(coord_factory) -> None:
    """If no HA source, lowest-addressed sensor-equipped node wins."""
    coord = coord_factory(
        hass_states={},
        nodes={
            1: {"has_own_outdoor_sensor": False},
            2: {
                "has_own_outdoor_sensor": True,
                "outdoor_temperature": 58.0,
                "outdoor_temperature_scale": "F",
            },
            3: {
                "has_own_outdoor_sensor": True,
                "outdoor_temperature": 60.0,
                "outdoor_temperature_scale": "F",
            },
        },
        source=None,
    )
    assert coord._resolve_outdoor_temp_value() == (58, "F")


def test_resolve_rebroadcast_disabled(coord_factory) -> None:
    """With rebroadcast off, a sensor-equipped node isn't used as fallback."""
    coord = coord_factory(
        hass_states={},
        nodes={
            1: {
                "has_own_outdoor_sensor": True,
                "outdoor_temperature": 58.0,
                "outdoor_temperature_scale": "F",
            },
        },
        source=None,
        rebroadcast=False,
    )
    assert coord._resolve_outdoor_temp_value() is None


def test_resolve_none_when_nothing_available(coord_factory) -> None:
    """No HA source, no sensor-equipped node - returns None cleanly."""
    coord = coord_factory(
        hass_states={},
        nodes={
            1: {"has_own_outdoor_sensor": False},
            2: {"has_own_outdoor_sensor": False},
        },
        source=None,
        rebroadcast=True,
    )
    assert coord._resolve_outdoor_temp_value() is None


def test_resolve_ha_source_missing_unit_defaults_to_fahrenheit(coord_factory) -> None:
    """A unit-less HA source defaults to F (matches 8800's default region)."""
    coord = coord_factory(
        hass_states={"sensor.outdoor": ("45",)},
        nodes={},
        source="sensor.outdoor",
    )
    assert coord._resolve_outdoor_temp_value() == (45, "F")


def test_resolve_clamps_extreme_ha_values(coord_factory) -> None:
    """Out-of-range HA values get pulled into the spec range."""
    coord = coord_factory(
        hass_states={"sensor.outdoor": ("200", "°F")},
        nodes={},
        source="sensor.outdoor",
    )
    assert coord._resolve_outdoor_temp_value() == (130, "F")
