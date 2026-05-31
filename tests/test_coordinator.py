# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the coordinator's state-application logic.

These tests pull NodeState and _apply_to_state out of coordinator.py and
exercise them directly without instantiating the whole HA-dependent
coordinator class. We do this by extracting just those two definitions
from the coordinator source file and exec'ing them in a tiny namespace
that has the protocol decoders available.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

# Protocol decoders are importable because conftest.py puts the integration
# directory on sys.path. The protocol module is also used directly in the
# wire-format integration tests at the bottom of this file.
from protocol import (
    Aprilaire8800Protocol,
    decode_errors,
    decode_humidity,
    decode_hvac,
    decode_temperature,
    parse_rsm,
    parse_rxsy_command,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COORD_PATH = _REPO_ROOT / "custom_components" / "aprilaire_rs485" / "coordinator.py"


def _load_state_module():
    """Return (NodeState, apply_to_state) lifted out of coordinator.py.

    We can't import coordinator.py directly without HomeAssistant, so we
    parse out the dataclass and the static method and exec them in a
    fresh namespace. The extracted blocks must stay in sync with the
    integration; the test will fail loudly if the source structure shifts.
    """
    src = _COORD_PATH.read_text(encoding="utf-8")

    start = src.index("@dataclass\nclass NodeState")
    end = src.index("\n\nclass Aprilaire8800Coordinator")
    nodestate_block = src[start:end]

    apply_start = src.index("    @staticmethod\n    def _apply_to_state")
    apply_end = src.index("\n\n\ndef _format_setpoint")
    apply_block = src[apply_start:apply_end]
    # Dedent the class body and drop the @staticmethod decorator.
    apply_block = "\n".join(
        line[4:] if line.startswith("    ") else line for line in apply_block.splitlines()
    )
    apply_block = apply_block.replace("@staticmethod\n", "")

    ns: dict[str, object] = {
        "__name__": "test_state",  # Needed by dataclasses internals.
        "decode_temperature": decode_temperature,
        "decode_humidity": decode_humidity,
        "decode_hvac": decode_hvac,
        "decode_errors": decode_errors,
        # parse_rsm and parse_rxsy_command are called from inside
        # _apply_to_state for the RSM and RxSy branches. They are pure
        # functions in protocol.py and have no HA dependency, so we can
        # pass them straight in.
        "parse_rsm": parse_rsm,
        "parse_rxsy_command": parse_rxsy_command,
    }
    # dataclasses' KW_ONLY detection does sys.modules[cls.__module__].__dict__;
    # register a stand-in so that lookup works.
    fake_mod = types.ModuleType("test_state")
    fake_mod.__dict__.update(ns)
    sys.modules["test_state"] = fake_mod
    ns = fake_mod.__dict__

    exec(
        "from __future__ import annotations\n"
        "import contextlib\n"
        "from dataclasses import dataclass, field\n",
        ns,
    )
    exec(nodestate_block, ns)
    exec(apply_block, ns)
    return ns["NodeState"], ns["_apply_to_state"]


NodeState, apply_to_state = _load_state_module()


# ---------- alarm parsing ----------


def test_filter_alarm_on_off() -> None:
    """FLTALM=ON sets alarm_filter True; FLTALM=OFF sets it False."""
    node = NodeState(address=1)
    apply_to_state(node, "FLTALM", "ON")
    assert node.alarm_filter is True
    apply_to_state(node, "FLTALM", "OFF")
    assert node.alarm_filter is False


@pytest.mark.parametrize(
    ("cmd", "attr"),
    [
        ("FLTALM", "alarm_filter"),
        ("WPALM", "alarm_water_panel"),
        ("DEHALM", "alarm_dehumidifier"),
        ("SYSALM", "alarm_system"),
    ],
)
def test_all_four_alarms_parse(cmd: str, attr: str) -> None:
    """Each of the four maintenance alarms is recorded under the right field."""
    node = NodeState(address=1)
    apply_to_state(node, cmd, "ON")
    assert getattr(node, attr) is True


# ---------- error parsing ----------


def test_error_response_populates_all_six_fields() -> None:
    """An ERROR=NNNNNN response populates all six per-sensor severity flags."""
    node = NodeState(address=1)
    apply_to_state(node, "ERROR", "012003")
    assert node.errors == {
        "builtin_temp": 0,
        "remote_temp": 1,
        "outdoor_temp": 2,
        "builtin_humidity": 0,
        "comm": 0,
        "eeprom": 3,
    }


def test_error_response_clears_on_all_zero() -> None:
    """ERROR=000000 leaves all severities at 0."""
    node = NodeState(address=1)
    apply_to_state(node, "ERROR", "000000")
    assert all(v == 0 for v in node.errors.values())


# ---------- identity / status ----------


def test_id_response_stored_as_model_info() -> None:
    """An ID query response is captured into NodeState.model_info."""
    node = NodeState(address=1)
    apply_to_state(node, "ID", "MODEL# 8800 REV: 1.0 RPC 2011")
    assert node.model_info == "MODEL# 8800 REV: 1.0 RPC 2011"


def test_hold_status_stored() -> None:
    """HOLDSTAT response is normalised to upper case and stored."""
    node = NodeState(address=1)
    apply_to_state(node, "HOLDSTAT", "perm")
    assert node.hold_status == "PERM"


def test_network_override_stored() -> None:
    """HOLD response (network override) is stored in network_override."""
    node = NodeState(address=1)
    apply_to_state(node, "HOLD", "ON")
    assert node.network_override == "ON"
    apply_to_state(node, "HOLD", "OFF")
    assert node.network_override == "OFF"


def test_controller_type_parsed() -> None:
    """CT response is parsed to int."""
    node = NodeState(address=1)
    apply_to_state(node, "CT", "1")
    assert node.controller_type == 1


def test_controller_type_bad_value_does_not_raise() -> None:
    """Garbage CT value is ignored, not raised."""
    node = NodeState(address=1)
    apply_to_state(node, "CT", "garbage")
    assert node.controller_type is None


def test_deadband_parsed_with_scale() -> None:
    """A DBAND response like '3F' stores the magnitude as an int."""
    node = NodeState(address=1)
    apply_to_state(node, "DBAND", "3F")
    assert node.deadband == 3


def test_deadband_parsed_without_scale() -> None:
    """A bare DBAND value (no scale suffix) parses too."""
    node = NodeState(address=1)
    apply_to_state(node, "DBAND", "2")
    assert node.deadband == 2


def test_alarm_periods_parsed() -> None:
    """[alarm]ALMP responses populate node.alarm_periods keyed by short code."""
    node = NodeState(address=1)
    apply_to_state(node, "FLTALMP", "6")
    apply_to_state(node, "WPALMP", "12")
    apply_to_state(node, "DEHALMP", "off")
    apply_to_state(node, "SYSALMP", "OFF")
    assert node.alarm_periods == {"FLT": "6", "WP": "12", "DEH": "OFF", "SYS": "OFF"}


def test_builtin_humidity_and_remote_temp_parsed() -> None:
    """BIHUM and RTS responses populate their NodeState fields (both are polled)."""
    node = NodeState(address=1)
    apply_to_state(node, "BIHUM", "42%")
    assert node.builtin_humidity == 42
    apply_to_state(node, "RTS", "68F")
    assert node.remote_temperature == 68.0


# ---------- relay state for hvac_action ----------


def test_hvac_relays_decoded_into_node() -> None:
    """An HVAC= response populates node.relays with parsed booleans."""
    node = NodeState(address=1)
    apply_to_state(node, "HVAC", "G+Y1-W1+Y2-W2-B-O-")
    assert node.relays["G"] is True
    assert node.relays["W1"] is True
    assert node.relays["Y1"] is False


# ---------- message text formatter ----------
#
# These tests pull format_message_text and the related constants directly
# out of coordinator.py the same way NodeState and apply_to_state are
# loaded above. Keeping the formatter at module level (rather than a static
# method on the coordinator class) is what makes this possible without HA.


def _load_message_helpers() -> tuple:
    """Return (format_message_text, MESSAGE_MAX_LENGTH, VALID_MESSAGE_SLOTS)."""
    src = _COORD_PATH.read_text(encoding="utf-8")
    start = src.index("MESSAGE_MAX_LENGTH = 32")
    # Pull from MESSAGE_MAX_LENGTH down to the end of format_message_text.
    block = src[start:]
    ns: dict[str, object] = {"__name__": "test_message_helpers"}
    exec("from __future__ import annotations\n", ns)
    exec(block, ns)
    return (
        ns["format_message_text"],
        ns["MESSAGE_MAX_LENGTH"],
        ns["VALID_MESSAGE_SLOTS"],
    )


format_message_text, MESSAGE_MAX_LENGTH, VALID_MESSAGE_SLOTS = _load_message_helpers()


def test_message_helpers_constants() -> None:
    """Spot-check the exported constants against what the services rely on."""
    assert MESSAGE_MAX_LENGTH == 32
    assert VALID_MESSAGE_SLOTS == ("TMPMES", "PMES1", "PMES2", "PMES3", "PMES4")


def test_format_message_passes_normal_ascii() -> None:
    """Plain ASCII messages survive intact, case preserved."""
    assert format_message_text("Filter change due") == "Filter change due"


def test_format_message_strips_non_ascii() -> None:
    """Non-ASCII characters are dropped, the rest of the string is kept."""
    assert format_message_text("Caf\u00e9 open") == "Caf open"


def test_format_message_replaces_cr_and_lf() -> None:
    """CR and LF become spaces so multi-line input still produces output.

    A CR in the payload would otherwise terminate the command early and the
    rest of the text would either silently fail or get interpreted as a
    fresh, malformed command.
    """
    assert format_message_text("line1\nline2") == "line1 line2"
    assert format_message_text("line1\rline2") == "line1 line2"
    assert format_message_text("line1\r\nline2") == "line1  line2"


def test_format_message_truncates_at_max_length() -> None:
    """Long input is truncated at MESSAGE_MAX_LENGTH, not rejected."""
    long_text = "X" * 100
    result = format_message_text(long_text)
    assert len(result) == MESSAGE_MAX_LENGTH
    assert result == "X" * MESSAGE_MAX_LENGTH


def test_format_message_none_becomes_empty_string() -> None:
    """None input maps to '' (which clears the slot on the wire)."""
    assert format_message_text(None) == ""


def test_format_message_empty_string_stays_empty() -> None:
    """Empty input stays empty (i.e. is a valid clear request)."""
    assert format_message_text("") == ""


def test_format_message_pure_non_ascii_becomes_empty() -> None:
    """A message of only non-ASCII characters is dropped to an empty string."""
    assert format_message_text("\u2603\u2603\u2603") == ""


# ---------- end-to-end: formatter + protocol encoder ----------


def test_encoded_temporary_message_matches_wire_format() -> None:
    """The formatter output, fed through the protocol encoder, matches spec.

    The 8800 expects ``SN<addr> TMPMES=<text><CR>`` on the wire. This test
    confirms that the formatter and the encoder agree on the exact bytes
    the bus will see, so a regression in either one is caught here.
    """
    formatted = format_message_text("Filter change due")
    payload = Aprilaire8800Protocol._encode(1, "TMPMES", formatted, query=False)
    assert payload == b"SN1 TMPMES=Filter change due\r"


def test_encoded_clear_permanent_message_matches_wire_format() -> None:
    """Clearing a permanent slot is an empty-value assignment on the wire."""
    payload = Aprilaire8800Protocol._encode(7, "PMES3", "", query=False)
    assert payload == b"SN7 PMES3=\r"


def test_encoded_long_message_is_truncated_before_encoding() -> None:
    """A 100-char message is truncated by the formatter to fit MESSAGE_MAX_LENGTH."""
    formatted = format_message_text("Y" * 100)
    payload = Aprilaire8800Protocol._encode(1, "TMPMES", formatted, query=False)
    # The "Y" sequence on the wire is exactly MESSAGE_MAX_LENGTH long.
    assert payload == b"SN1 TMPMES=" + (b"Y" * MESSAGE_MAX_LENGTH) + b"\r"


# ---------- support module state-mutation ----------


def test_apply_rsm_populates_topology() -> None:
    """RSM response replaces node.support_modules with the parsed topology."""
    node = NodeState(address=1)
    apply_to_state(node, "RSM", "M1:RT,RH M3:CT,RT")

    assert node.support_modules == {
        (1, 1): "RT",
        (1, 2): "RH",
        (3, 1): "CT",
        (3, 2): "RT",
    }


def test_apply_rsm_drops_readings_for_removed_modules() -> None:
    """A subsequent RSM that no longer lists a module clears its cached readings.

    Useful when an installer unplugs a module between integration restarts;
    the cached reading would otherwise be stale forever.
    """
    node = NodeState(address=1)
    node.support_modules = {(1, 1): "RT", (2, 1): "CT"}
    node.support_module_readings = {(1, 1): (50, "F"), (2, 1): (72, "F")}

    apply_to_state(node, "RSM", "M1:RT,RH")  # Module 2 unplugged.

    assert (2, 1) not in node.support_module_readings
    assert (1, 1) in node.support_module_readings  # Still wired up.


def test_apply_rxsy_temperature_stores_value_and_scale() -> None:
    """R1S1=68F populates the readings dict with the right value and scale."""
    node = NodeState(address=1)
    node.support_modules = {(1, 1): "CT"}

    apply_to_state(node, "R1S1", "68F")

    assert node.support_module_readings[(1, 1)] == (68, "F")


def test_apply_rxsy_humidity_stores_value_with_percent_scale() -> None:
    """R1S2=45% populates the readings dict with the humidity and '%' scale."""
    node = NodeState(address=1)
    node.support_modules = {(1, 2): "RH"}

    apply_to_state(node, "R1S2", "45%")

    assert node.support_module_readings[(1, 2)] == (45, "%")


def test_apply_rxsy_disconnected_stores_none_value() -> None:
    """A '--' response means 'sensor not present' - store None, keep the slot."""
    node = NodeState(address=1)
    node.support_modules = {(2, 1): "CT"}

    apply_to_state(node, "R2S1", "--F")

    assert (2, 1) in node.support_module_readings
    assert node.support_module_readings[(2, 1)][0] is None


def test_apply_rxsy_without_known_type_falls_back_to_scale_heuristic() -> None:
    """If RSM hasn't been processed yet, decode based on the value's scale.

    Defensive against races where an RxSy response lands before its
    parent RSM response: we still want to capture the reading rather
    than discard it.
    """
    node = NodeState(address=1)
    # node.support_modules is empty - we haven't seen RSM yet.

    apply_to_state(node, "R1S1", "68F")
    assert node.support_module_readings[(1, 1)] == (68, "F")

    apply_to_state(node, "R1S2", "45%")
    assert node.support_module_readings[(1, 2)] == (45, "%")
