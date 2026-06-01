# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the bus-level diagnostic counters and error events.

Layers covered:

  1. Protocol counter increments: parse_error_count, transport_error_count,
     messages_sent_count, messages_received_count. Exercised by driving
     the protocol's RX path directly with synthesized input or by
     stubbing the transport.
  2. Protocol error-listener dispatch: verifying the BusError payload
     shape and that listener exceptions don't kill the RX thread.
  3. Coordinator-side _is_known_command allow-list: every command name
     that _apply_to_state handles must be recognised here, and unknown
     commands return False.
  4. Coordinator-side counters and HA event firing in _handle_message.
"""
from __future__ import annotations

import pytest
from protocol import (
    ERROR_PARSE,
    ERROR_TRANSPORT,
    BusError,
    NodeMessage,
)

# ---------- protocol-layer counters ----------
#
# We test _dispatch_raw directly rather than running the full RX loop:
# spinning up real threads to verify counter increments is fragile and
# slow. _dispatch_raw is the function that ends up incrementing
# parse_error_count and messages_received_count anyway, so testing it
# directly is the cleanest path.


def _make_protocol():
    """Build a protocol instance without starting any threads."""
    # Imported lazily so this file can be collected even if the
    # integration package isn't importable (e.g. running just the
    # protocol-layer tests without HA installed).
    from protocol import Aprilaire8800Protocol  # noqa: PLC0415

    return Aprilaire8800Protocol(url="loop://", baud=9600, max_address=4)


def test_protocol_messages_received_increments_on_every_line() -> None:
    """Each call to _dispatch_raw increments messages_received_count."""
    p = _make_protocol()
    assert p.messages_received_count == 0

    p._dispatch_raw("SN1 T=72F")
    assert p.messages_received_count == 1

    p._dispatch_raw("SN2 HUM=45")
    assert p.messages_received_count == 2


def test_protocol_messages_received_counts_unparseable_lines() -> None:
    """Lines that fail parsing still count toward messages_received.

    This is intentional: the denominator for parse-error-rate is "every
    line we saw," not "every line we successfully parsed."
    """
    p = _make_protocol()
    p._dispatch_raw("garbage that won't parse")

    assert p.messages_received_count == 1
    assert p.parse_error_count == 1


def test_protocol_parse_error_increments_only_on_failure() -> None:
    """parse_error_count goes up for unparseable lines, not valid ones."""
    p = _make_protocol()
    p._dispatch_raw("SN1 T=72F")  # valid
    p._dispatch_raw("not a valid line")  # invalid

    assert p.parse_error_count == 1
    assert p.messages_received_count == 2


def test_protocol_error_listener_receives_parse_error() -> None:
    """Registered error listeners get the BusError for parse failures."""
    p = _make_protocol()
    captured: list[BusError] = []
    p.add_error_listener(captured.append)

    p._dispatch_raw("definitely not a valid bus message")

    assert len(captured) == 1
    assert captured[0].category == ERROR_PARSE
    assert captured[0].raw == "definitely not a valid bus message"
    assert "unparseable" in captured[0].detail.lower()


def test_protocol_error_listener_can_be_removed() -> None:
    """Removed listeners no longer receive events."""
    p = _make_protocol()
    captured: list[BusError] = []
    p.add_error_listener(captured.append)
    p.remove_error_listener(captured.append)

    p._dispatch_raw("garbage")

    assert captured == []


def test_protocol_error_listener_exception_is_swallowed() -> None:
    """A misbehaving listener must not kill the RX thread.

    The protocol layer can't recover from a thread death (RX would stop
    feeding messages, the integration would silently freeze), so an
    exception in a listener must be logged and ignored.
    """
    p = _make_protocol()

    def bad_listener(_err: BusError) -> None:
        raise RuntimeError("listener bug")

    good_calls: list[BusError] = []
    p.add_error_listener(bad_listener)
    p.add_error_listener(good_calls.append)

    # Must not raise.
    p._dispatch_raw("garbage")

    # The good listener still got called after the bad one raised.
    assert len(good_calls) == 1


def test_protocol_multiple_listeners_all_invoked() -> None:
    """All registered error listeners receive each event."""
    p = _make_protocol()
    a: list[BusError] = []
    b: list[BusError] = []
    p.add_error_listener(a.append)
    p.add_error_listener(b.append)

    p._dispatch_raw("garbage")

    assert len(a) == 1
    assert len(b) == 1


def test_protocol_valid_line_does_not_fire_error_listener() -> None:
    """Successfully-parsed lines should not invoke error listeners."""
    p = _make_protocol()
    errors: list[BusError] = []
    p.add_error_listener(errors.append)

    p._dispatch_raw("SN1 T=72F")

    assert errors == []


def test_protocol_listener_isolation_between_message_and_error() -> None:
    """Message listeners run on parse success; error listeners on failure.

    Make sure we haven't accidentally crossed wires between the two
    callback paths.
    """
    p = _make_protocol()
    msgs: list[NodeMessage] = []
    errs: list[BusError] = []
    p.add_listener(msgs.append)
    p.add_error_listener(errs.append)

    p._dispatch_raw("SN1 T=72F")  # parses cleanly
    p._dispatch_raw("garbage")     # fails to parse

    assert len(msgs) == 1
    assert msgs[0].address == 1
    assert msgs[0].command == "T"
    assert len(errs) == 1
    assert errs[0].category == ERROR_PARSE


# ---------- BusError dataclass shape ----------


def test_bus_error_category_required() -> None:
    """BusError needs at minimum a category and detail."""
    err = BusError(category=ERROR_PARSE, detail="oops")
    assert err.category == ERROR_PARSE
    assert err.detail == "oops"
    assert err.raw is None  # optional


def test_bus_error_with_raw_payload() -> None:
    """Raw bytes can be attached for parse errors."""
    err = BusError(category=ERROR_PARSE, detail="bad", raw="SN1 ?@#")
    assert err.raw == "SN1 ?@#"


def test_bus_error_transport_omits_raw_by_design() -> None:
    """Transport errors have no associated wire content."""
    err = BusError(category=ERROR_TRANSPORT, detail="open failed: ENOENT")
    assert err.raw is None


# ---------- _is_known_command allow-list ----------
#
# Every command name that _apply_to_state handles must be in
# _KNOWN_RESPONSE_COMMANDS (or recognised dynamically by
# parse_rxsy_command). If a new handler is added to _apply_to_state
# without updating the allow-list, the unknown_command_count will
# false-positive on every response of that type - a real and noisy bug.


@pytest.fixture(scope="module")
def is_known():
    """Load _is_known_command via the existing exec-based loader.

    The function lives at module scope in coordinator.py and depends
    only on parse_rxsy_command, so it loads cleanly through the same
    test-coordinator loader used elsewhere.
    """
    pytest.importorskip("homeassistant")
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        _is_known_command,
    )

    return _is_known_command


def test_is_known_command_recognises_sensor_values(is_known) -> None:
    """T, TEMP, HUM, OT and friends are recognised."""
    for cmd in ("T", "TEMP", "HUM", "OT", "OH", "BIHUM", "RTS", "CT"):
        assert is_known(cmd), f"{cmd} should be recognised"


def test_is_known_command_recognises_setpoints(is_known) -> None:
    """Setpoint commands are recognised."""
    for cmd in ("SH", "SC", "SHUM", "SDEH"):
        assert is_known(cmd), f"{cmd} should be recognised"


def test_is_known_command_recognises_mode_and_fan(is_known) -> None:
    """Mode and fan command aliases are all recognised."""
    for cmd in ("M", "MODE", "F", "FAN"):
        assert is_known(cmd), f"{cmd} should be recognised"


def test_is_known_command_recognises_status(is_known) -> None:
    """Hold, HVAC, recovery status commands are recognised."""
    for cmd in ("HOLDSTAT", "HOLD", "HVAC", "H", "RECOVSTAT"):
        assert is_known(cmd), f"{cmd} should be recognised"


def test_is_known_command_recognises_alarms_and_errors(is_known) -> None:
    """All four alarm commands and ERROR are recognised."""
    for cmd in ("FLTALM", "WPALM", "DEHALM", "SYSALM", "ERROR"):
        assert is_known(cmd), f"{cmd} should be recognised"


def test_is_known_command_recognises_identity(is_known) -> None:
    """Identity and topology commands are recognised."""
    for cmd in ("NAME", "ID", "RSM"):
        assert is_known(cmd), f"{cmd} should be recognised"


def test_is_known_command_recognises_rxsy_dynamically(is_known) -> None:
    """RxSy commands match by pattern, not by enumeration."""
    for module in (1, 2, 3, 4):
        for sensor in (1, 2):
            cmd = f"R{module}S{sensor}"
            assert is_known(cmd), f"{cmd} should be recognised as RxSy"


def test_is_known_command_rejects_unknown(is_known) -> None:
    """Genuinely unknown commands return False.

    These would trigger the unknown_command_count and event in
    _handle_message - which is exactly the diagnostic signal we want
    to surface.
    """
    for cmd in ("BOGUS", "X", "FUTURE_COMMAND", "R5S1", "R1S3", ""):
        assert not is_known(cmd), f"{cmd} should not be recognised"


def test_is_known_command_case_sensitive_for_set(is_known) -> None:
    """The static allow-list is upper-case; lower-case is rejected.

    The 8800 protocol uppercases everything on the wire, and the parsers
    in protocol.py preserve case. Accepting lower-case in the static
    allow-list would mask a real bug (some upstream layer mishandling
    case). RxSy is handled separately - parse_rxsy_command is
    intentionally case-tolerant for defensive parsing, so lower-case
    "r1s1" still resolves as known.
    """
    assert not is_known("t")
    assert not is_known("hum")
    assert not is_known("mode")
    # RxSy via the parser remains case-insensitive by design.
    assert is_known("r1s1")
    assert is_known("R1S1")


def test_is_known_command_recognises_deadband_and_alarm_periods(is_known) -> None:
    """DBAND and the four *ALMP reminder-interval responses are recognised.

    These handlers exist in the current _apply_to_state (deadband and the
    maintenance-alarm period selects) but were absent from the original
    snapshot's allow-list; if they regress, every such response would
    false-positive as an unknown command.
    """
    for cmd in ("DBAND", "FLTALMP", "WPALMP", "DEHALMP", "SYSALMP"):
        assert is_known(cmd), f"{cmd} should be recognised"


def test_is_known_command_recognises_ignored_bare_responses(is_known) -> None:
    """PRESENT (bare discovery reply) and BLTON are knowingly ignored, not unknown.

    _apply_to_state has no branch for these, but they're legitimate responses
    (PRESENT arrives for every node on every discovery), so they must be on the
    allow-list or they'd flood unknown_command on every restart.
    """
    assert is_known("PRESENT")
    assert is_known("BLTON")


def test_is_known_command_recognises_clock_write_echoes(is_known) -> None:
    """TIME/DATE echoes from the clock-sync push are recognised, not unknown.

    _async_push_clock writes TIME and DATE globally at startup and hourly; the
    device echoes them back like any other write ack. They carry no state we
    read, so they're knowingly ignored rather than counted as unknown.
    """
    assert is_known("TIME")
    assert is_known("DATE")


def test_is_known_command_recognises_cos_acknowledgements(is_known) -> None:
    """COS-enable echoes (C1..C19, CP) are recognised, not flagged unknown.

    The device acks every COS-enable write by echoing it back, on every node
    each time COS is applied (every periodic refresh). Treating those as
    unknown commands would swamp the counter with expected traffic - the exact
    bug this guards against. Iterating COS_ENABLE keeps the allow-list in sync
    with what we actually enable.
    """
    from custom_components.aprilaire_rs485.const import COS_ENABLE  # noqa: PLC0415

    assert is_known("CP")
    for code in COS_ENABLE:
        assert is_known(code), f"COS-enable ack {code} should be recognised"


def test_apply_to_state_branches_all_in_allowlist(is_known) -> None:
    """Audit: every command literal handled in _apply_to_state is recognised.

    Parses the _apply_to_state source for ``cmd == "X"`` and ``cmd in (...)``
    branches and asserts each literal resolves as known. This is the
    regression guard the spec calls for: adding a handler branch without
    updating _KNOWN_RESPONSE_COMMANDS would make that response false-positive
    as unknown, and this test fails loudly when that happens.
    """
    import re  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    pytest.importorskip("homeassistant")
    coord_path = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "aprilaire_rs485"
        / "coordinator.py"
    )
    src = coord_path.read_text(encoding="utf-8")
    start = src.index("    def _apply_to_state")
    end = src.index("\n\n\ndef _format_setpoint")
    body = src[start:end]

    commands: set[str] = set()
    # cmd == "X"
    commands.update(re.findall(r'cmd == "([^"]+)"', body))
    # cmd in ("X", "Y", ...)
    for group in re.findall(r"cmd in \(([^)]*)\)", body):
        commands.update(re.findall(r'"([^"]+)"', group))

    assert commands, "failed to extract any command literals - parser drifted"
    unknown = sorted(c for c in commands if not is_known(c))
    assert not unknown, f"_apply_to_state handles {unknown} but they're not in the allow-list"


# ---------- coordinator counters via _handle_message ----------
#
# These tests exercise the coordinator's apply-error and unknown-command
# counting in isolation. They need a real Coordinator instance, which
# requires HA imports.

pytest.importorskip("pytest_homeassistant_custom_component")
pytest.importorskip("homeassistant")


@pytest.fixture
async def coord(hass):
    """Construct a coordinator without starting its transport."""
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        Aprilaire8800Coordinator,
    )

    return Aprilaire8800Coordinator(
        hass=hass,
        url="loop://",
        baud=9600,
        max_address=4,
    )


async def test_coordinator_apply_error_increments_on_exception(
    hass, coord
) -> None:
    """If _apply_to_state raises, apply_error_count goes up."""
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_APPLY_ERROR,
    )

    events: list = []
    hass.bus.async_listen(EVENT_APPLY_ERROR, events.append)

    # _apply_to_state is a @staticmethod, so accessing it via the class
    # gives the unwrapped function. To restore correctly we need the
    # staticmethod descriptor itself, which lives in the class __dict__.
    cls = type(coord)
    original_descriptor = cls.__dict__["_apply_to_state"]

    def boom(node, cmd, val):  # noqa: ARG001
        raise RuntimeError("simulated apply failure")

    cls._apply_to_state = staticmethod(boom)
    try:
        coord._handle_message(
            NodeMessage(address=1, command="T", value="72F", name=None, raw="")
        )
        await hass.async_block_till_done()
    finally:
        # Restoring the descriptor (not the unwrapped function) preserves
        # @staticmethod semantics for subsequent tests in the session.
        cls._apply_to_state = original_descriptor

    assert coord.apply_error_count == 1
    assert len(events) == 1
    payload = events[0].data
    assert payload["address"] == 1
    assert payload["command"] == "T"
    assert payload["value"] == "72F"
    assert "simulated apply failure" in payload["detail"]


async def test_coordinator_unknown_command_increments(hass, coord) -> None:
    """A command not in the allow-list increments unknown_command_count."""
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_UNKNOWN_COMMAND,
    )

    events: list = []
    hass.bus.async_listen(EVENT_UNKNOWN_COMMAND, events.append)

    coord._handle_message(
        NodeMessage(
            address=3, command="WIDGETSTAT", value="42", name=None, raw=""
        )
    )
    await hass.async_block_till_done()

    assert coord.unknown_command_count == 1
    assert len(events) == 1
    payload = events[0].data
    assert payload["address"] == 3
    assert payload["command"] == "WIDGETSTAT"
    assert payload["value"] == "42"


async def test_coordinator_known_command_does_not_increment_unknown(
    hass, coord
) -> None:
    """A handled command does not trip the unknown-command counter."""
    coord._handle_message(
        NodeMessage(address=1, command="T", value="72F", name=None, raw="")
    )
    await hass.async_block_till_done()

    assert coord.unknown_command_count == 0
    assert coord.apply_error_count == 0


async def test_coordinator_protocol_error_proxy_via_handle(hass, coord) -> None:
    """Protocol errors come through _handle_protocol_error to HA events."""
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_PARSE_ERROR,
        EVENT_TRANSPORT_ERROR,
    )

    parse_events: list = []
    transport_events: list = []
    hass.bus.async_listen(EVENT_PARSE_ERROR, parse_events.append)
    hass.bus.async_listen(EVENT_TRANSPORT_ERROR, transport_events.append)

    coord._handle_protocol_error(
        BusError(category=ERROR_PARSE, detail="bad line", raw="oops")
    )
    coord._handle_protocol_error(
        BusError(category=ERROR_TRANSPORT, detail="connection refused")
    )
    await hass.async_block_till_done()

    assert len(parse_events) == 1
    assert parse_events[0].data == {"detail": "bad line", "raw": "oops"}
    assert len(transport_events) == 1
    assert transport_events[0].data == {"detail": "connection refused"}


async def test_coordinator_counter_proxy_properties(coord) -> None:
    """The four coordinator properties proxy the protocol's counters."""
    # Inject values directly onto the protocol object.
    coord.protocol.parse_error_count = 5
    coord.protocol.transport_error_count = 7
    coord.protocol.messages_sent_count = 100
    coord.protocol.messages_received_count = 200

    assert coord.parse_error_count == 5
    assert coord.transport_error_count == 7
    assert coord.messages_sent_count == 100
    assert coord.messages_received_count == 200


async def test_coordinator_signal_fires_on_unknown(hass, coord) -> None:
    """SIGNAL_BUS_ERRORS_UPDATED fires when unknown_command increments."""
    from homeassistant.helpers.dispatcher import (  # noqa: PLC0415
        async_dispatcher_connect,
    )

    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        SIGNAL_BUS_ERRORS_UPDATED,
    )

    fired: list[int] = []
    unsub = async_dispatcher_connect(
        hass, SIGNAL_BUS_ERRORS_UPDATED, lambda: fired.append(1)
    )
    try:
        coord._handle_message(
            NodeMessage(
                address=1, command="MYSTERY", value="x", name=None, raw=""
            )
        )
        await hass.async_block_till_done()
    finally:
        unsub()

    assert len(fired) == 1


# ---------- write verification ----------
#
# These tests exercise the _register_verification machinery. The
# verification window is 5 seconds in production but the tests
# monkey-patch VERIFICATION_DELAY_S down to ~0 so the suite runs fast.
# Per-test setup pattern:
#
#   1. Register a verification with a synthetic check function.
#   2. Manipulate NodeState to simulate device-side success or failure.
#   3. await asyncio.sleep(epsilon) and check counters / events.


async def test_verification_success_does_not_increment_failures(
    hass, coord, monkeypatch
) -> None:
    """When the check function returns matches=True, no failure event fires."""
    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_WRITE_VERIFICATION_FAILED,
    )

    monkeypatch.setattr(coord_module, "VERIFICATION_DELAY_S", 0.01)

    events: list = []
    hass.bus.async_listen(EVENT_WRITE_VERIFICATION_FAILED, events.append)

    # Pre-populate node state so the check matches.
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        NodeState,
    )

    coord.nodes[1] = NodeState(address=1, setpoint_heat=72.0)

    coord._register_verification(
        1, "SH", "72",
        lambda n: coord._check_setpoint(n.setpoint_heat, 72.0),
    )
    # Let the verification task run to completion.
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(0.05)
    await hass.async_block_till_done()

    assert coord.verifications_attempted_count == 1
    assert coord.verification_failures_count == 0
    assert events == []


async def test_verification_mismatch_fires_failure(
    hass, coord, monkeypatch
) -> None:
    """When NodeState shows a different value, a failure event fires."""
    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_WRITE_VERIFICATION_FAILED,
    )

    monkeypatch.setattr(coord_module, "VERIFICATION_DELAY_S", 0.01)

    events: list = []
    hass.bus.async_listen(EVENT_WRITE_VERIFICATION_FAILED, events.append)

    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        NodeState,
    )

    # Node-side state holds 70, but we register a verification for 72.
    coord.nodes[1] = NodeState(address=1, setpoint_heat=70.0)

    coord._register_verification(
        1, "SH", "72",
        lambda n: coord._check_setpoint(n.setpoint_heat, 72.0),
    )
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(0.05)
    await hass.async_block_till_done()

    assert coord.verifications_attempted_count == 1
    assert coord.verification_failures_count == 1
    assert len(events) == 1
    payload = events[0].data
    assert payload["address"] == 1
    assert payload["command"] == "SH"
    assert payload["expected"] == "72"
    assert payload["actual"] == "70.0"


async def test_verification_node_disappeared_fires_failure(
    hass, coord, monkeypatch
) -> None:
    """If the node isn't in coordinator state at check time, that's a failure."""
    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_WRITE_VERIFICATION_FAILED,
    )

    monkeypatch.setattr(coord_module, "VERIFICATION_DELAY_S", 0.01)

    events: list = []
    hass.bus.async_listen(EVENT_WRITE_VERIFICATION_FAILED, events.append)

    # Don't populate coord.nodes[99] - simulate node disappearing.
    coord._register_verification(
        99, "SH", "72",
        lambda n: coord._check_setpoint(n.setpoint_heat, 72.0),
    )
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(0.05)
    await hass.async_block_till_done()

    assert coord.verification_failures_count == 1
    assert events[0].data["actual"] == "<node not in state>"
    assert "disappeared" in events[0].data["detail"]


async def test_verification_newer_supersedes_older(
    hass, coord, monkeypatch
) -> None:
    """A second verification for the same (addr, cmd) cancels the first.

    The older write's verification would have been misleading anyway:
    if both writes succeeded the device ends at the second value, which
    would make the first verification falsely fail.
    """
    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_WRITE_VERIFICATION_FAILED,
    )

    monkeypatch.setattr(coord_module, "VERIFICATION_DELAY_S", 0.05)

    events: list = []
    hass.bus.async_listen(EVENT_WRITE_VERIFICATION_FAILED, events.append)

    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        NodeState,
    )

    # Device-side will hold the second-written value.
    coord.nodes[1] = NodeState(address=1, setpoint_heat=70.0)

    # First write: expects 72.
    coord._register_verification(
        1, "SH", "72",
        lambda n: coord._check_setpoint(n.setpoint_heat, 72.0),
    )
    # Immediately follow up with a second write of the same command.
    coord._register_verification(
        1, "SH", "70",
        lambda n: coord._check_setpoint(n.setpoint_heat, 70.0),
    )
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(0.15)
    await hass.async_block_till_done()

    # Two attempts registered, second one matches, first was cancelled
    # before it could fire (so no failure event).
    assert coord.verifications_attempted_count == 2
    assert coord.verification_failures_count == 0
    assert events == []


async def test_verification_for_different_commands_coexist(
    hass, coord, monkeypatch
) -> None:
    """Verifications for distinct (addr, cmd) pairs don't interfere."""
    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )

    monkeypatch.setattr(coord_module, "VERIFICATION_DELAY_S", 0.01)

    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        NodeState,
    )

    coord.nodes[1] = NodeState(
        address=1, setpoint_heat=72.0, setpoint_cool=78.0
    )

    coord._register_verification(
        1, "SH", "72",
        lambda n: coord._check_setpoint(n.setpoint_heat, 72.0),
    )
    coord._register_verification(
        1, "SC", "78",
        lambda n: coord._check_setpoint(n.setpoint_cool, 78.0),
    )
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(0.05)
    await hass.async_block_till_done()

    assert coord.verifications_attempted_count == 2
    assert coord.verification_failures_count == 0


async def test_verification_check_exception_recorded_as_failure(
    hass, coord, monkeypatch
) -> None:
    """A buggy check function fails the verification rather than crashing."""
    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_WRITE_VERIFICATION_FAILED,
    )

    monkeypatch.setattr(coord_module, "VERIFICATION_DELAY_S", 0.01)

    events: list = []
    hass.bus.async_listen(EVENT_WRITE_VERIFICATION_FAILED, events.append)

    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        NodeState,
    )

    coord.nodes[1] = NodeState(address=1)

    def bad_check(_node):
        raise RuntimeError("check is buggy")

    coord._register_verification(1, "SH", "72", bad_check)
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(0.05)
    await hass.async_block_till_done()

    assert coord.verification_failures_count == 1
    assert "<check error>" in events[0].data["actual"]
    assert "check is buggy" in events[0].data["detail"]


async def test_verification_cancel_all_on_shutdown(
    hass, coord, monkeypatch
) -> None:
    """async_stop cancels in-flight verifications without firing failures."""
    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_WRITE_VERIFICATION_FAILED,
    )

    # Long enough that the test can definitely cancel before it fires.
    monkeypatch.setattr(coord_module, "VERIFICATION_DELAY_S", 5.0)

    events: list = []
    hass.bus.async_listen(EVENT_WRITE_VERIFICATION_FAILED, events.append)

    coord._register_verification(
        1, "SH", "72", lambda _n: (False, "would have failed"),
    )
    # Cancel without waiting.
    coord._cancel_all_verifications()
    import asyncio  # noqa: PLC0415

    # Give the cancelled task a moment to wind down.
    await asyncio.sleep(0.05)

    # No failure event was fired because the task was cancelled
    # before its sleep completed.
    assert events == []
    assert coord.verification_failures_count == 0
    # Attempts counter still incremented at registration time - the
    # write was attempted, we just stopped checking before we could
    # verify.
    assert coord.verifications_attempted_count == 1


# ---------- _check_* comparison helpers ----------


def test_check_setpoint_matches_within_tolerance() -> None:
    """Setpoint comparison tolerates floating-point fuzz."""
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        Aprilaire8800Coordinator,
    )

    matches, actual = Aprilaire8800Coordinator._check_setpoint(72.0, 72)
    assert matches is True
    assert actual == "72.0"

    matches, _ = Aprilaire8800Coordinator._check_setpoint(72.3, 72)
    assert matches is True  # within 0.5

    matches, _ = Aprilaire8800Coordinator._check_setpoint(73.0, 72)
    assert matches is False


def test_check_setpoint_handles_none() -> None:
    """None means we haven't received the value back yet - treat as no match."""
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        Aprilaire8800Coordinator,
    )

    matches, actual = Aprilaire8800Coordinator._check_setpoint(None, 72)
    assert matches is False
    assert actual == "<unset>"


def test_check_percent_exact_integer() -> None:
    """Humidity percent values must match exactly."""
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        Aprilaire8800Coordinator,
    )

    assert Aprilaire8800Coordinator._check_percent(45, 45)[0] is True
    assert Aprilaire8800Coordinator._check_percent(46, 45)[0] is False
    assert Aprilaire8800Coordinator._check_percent(None, 45)[0] is False


def test_check_string_ci_case_insensitive() -> None:
    """Mode/fan comparison is case-insensitive defensively."""
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        Aprilaire8800Coordinator,
    )

    assert Aprilaire8800Coordinator._check_string_ci("HEAT", "heat")[0] is True
    assert Aprilaire8800Coordinator._check_string_ci("H", "H")[0] is True
    assert Aprilaire8800Coordinator._check_string_ci("COOL", "HEAT")[0] is False
    assert Aprilaire8800Coordinator._check_string_ci(None, "H")[0] is False


# ---------- per-node query timeouts ----------
#
# The check fires QUERY_RESPONSE_TIMEOUT_S after each per-node query.
# If the node's last_seen_monotonic hasn't been updated since the
# query went out, increment query_timeouts_count and fire the event.


async def test_query_to_responsive_node_no_timeout(
    hass, coord, monkeypatch
) -> None:
    """If last_seen_monotonic moves forward after the query, no timeout."""
    import asyncio as _asyncio  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_QUERY_TIMEOUT,
    )
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        NodeState,
    )

    monkeypatch.setattr(coord_module, "QUERY_RESPONSE_TIMEOUT_S", 0.05)
    events: list = []
    hass.bus.async_listen(EVENT_QUERY_TIMEOUT, events.append)

    # Node was previously seen, will be seen again right after the query.
    coord.nodes[1] = NodeState(address=1, last_seen_monotonic=_time.monotonic())

    await coord._async_query(1, "T")
    # Simulate a response arriving immediately - bump last_seen forward.
    coord.nodes[1].last_seen_monotonic = _time.monotonic()
    await _asyncio.sleep(0.15)
    await hass.async_block_till_done()

    assert coord.queries_sent_count == 1
    assert coord.query_timeouts_count == 0
    assert events == []


async def test_query_to_silent_node_fires_timeout(
    hass, coord, monkeypatch
) -> None:
    """If last_seen_monotonic stays stale through the window, timeout fires."""
    import asyncio as _asyncio  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_QUERY_TIMEOUT,
    )
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        NodeState,
    )

    monkeypatch.setattr(coord_module, "QUERY_RESPONSE_TIMEOUT_S", 0.05)
    events: list = []
    hass.bus.async_listen(EVENT_QUERY_TIMEOUT, events.append)

    # Node was seen a while ago, but won't respond to this query.
    stale_time = _time.monotonic() - 30.0
    coord.nodes[1] = NodeState(address=1, last_seen_monotonic=stale_time)

    await coord._async_query(1, "T")
    # Don't bump last_seen - simulate no response.
    await _asyncio.sleep(0.15)
    await hass.async_block_till_done()

    assert coord.queries_sent_count == 1
    assert coord.query_timeouts_count == 1
    assert len(events) == 1
    payload = events[0].data
    assert payload["address"] == 1
    assert payload["deadline_seconds"] == 0.05
    # Last seen ~30 seconds ago (with some test-execution slack).
    assert 29 < payload["last_seen_seconds_ago"] < 32


async def test_query_to_never_seen_node_no_timeout(
    hass, coord, monkeypatch
) -> None:
    """A node with last_seen_monotonic=None doesn't generate timeout events.

    During initial discovery and for explicitly-configured-but-offline
    addresses, we don't have evidence the node has ever responded.
    Firing timeouts in this state would spam the user with events for
    addresses they don't actually have thermostats at.
    """
    import asyncio as _asyncio  # noqa: PLC0415

    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_QUERY_TIMEOUT,
    )
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        NodeState,
    )

    monkeypatch.setattr(coord_module, "QUERY_RESPONSE_TIMEOUT_S", 0.05)
    events: list = []
    hass.bus.async_listen(EVENT_QUERY_TIMEOUT, events.append)

    # Configured-but-never-seen: NodeState exists but last_seen_monotonic
    # is None.
    coord.nodes[5] = NodeState(address=5, last_seen_monotonic=None)

    await coord._async_query(5, "T")
    await _asyncio.sleep(0.15)
    await hass.async_block_till_done()

    # Query counted, but no timeout event because we've never seen the
    # node respond - we don't know if it's offline or never existed.
    assert coord.queries_sent_count == 1
    assert coord.query_timeouts_count == 0
    assert events == []


async def test_query_to_missing_node_no_timeout(
    hass, coord, monkeypatch
) -> None:
    """Same as never-seen: an address with no NodeState entry skips silently."""
    import asyncio as _asyncio  # noqa: PLC0415

    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_QUERY_TIMEOUT,
    )

    monkeypatch.setattr(coord_module, "QUERY_RESPONSE_TIMEOUT_S", 0.05)
    events: list = []
    hass.bus.async_listen(EVENT_QUERY_TIMEOUT, events.append)

    # Don't pre-populate coord.nodes - simulate a totally unknown address.
    await coord._async_query(7, "T")
    await _asyncio.sleep(0.15)
    await hass.async_block_till_done()

    assert coord.queries_sent_count == 1
    assert coord.query_timeouts_count == 0
    assert events == []


async def test_broadcast_query_not_tracked(hass, coord, monkeypatch) -> None:
    """addr=None broadcasts don't schedule per-node timeout checks.

    The bus_node_count and bus_addresses sensors cover the "is anything
    there" signal. Tracking broadcast timeouts would either fire on
    every unpopulated address (noise) or require knowing what should
    have responded (complexity).
    """
    import asyncio as _asyncio  # noqa: PLC0415

    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )

    monkeypatch.setattr(coord_module, "QUERY_RESPONSE_TIMEOUT_S", 0.05)

    await coord._async_query(None, "T")
    await _asyncio.sleep(0.15)
    await hass.async_block_till_done()

    assert coord.queries_sent_count == 1  # Still counted as a query.
    assert coord.query_timeouts_count == 0  # But not tracked for timeout.
    # And no task was scheduled.
    assert len(coord._pending_query_checks) == 0


async def test_query_check_cancelled_on_shutdown(
    hass, coord, monkeypatch
) -> None:
    """async_stop cancels in-flight query checks; no spurious timeout fires."""
    import asyncio as _asyncio  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_QUERY_TIMEOUT,
    )
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        NodeState,
    )

    # Long window so we have time to cancel before it fires.
    monkeypatch.setattr(coord_module, "QUERY_RESPONSE_TIMEOUT_S", 5.0)
    events: list = []
    hass.bus.async_listen(EVENT_QUERY_TIMEOUT, events.append)

    coord.nodes[1] = NodeState(address=1, last_seen_monotonic=_time.monotonic())
    await coord._async_query(1, "T")

    # Cancel before the timeout window elapses.
    coord._cancel_all_query_checks()
    await _asyncio.sleep(0.05)

    assert events == []
    assert coord.query_timeouts_count == 0


async def test_query_check_task_self_prunes(hass, coord, monkeypatch) -> None:
    """Completed query checks remove themselves from the pending set.

    Without self-pruning the set would grow unboundedly on a busy bus
    (~400 queries/min x 60min = 24,000 dead Task references per hour).
    """
    import asyncio as _asyncio  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        NodeState,
    )

    monkeypatch.setattr(coord_module, "QUERY_RESPONSE_TIMEOUT_S", 0.01)

    coord.nodes[1] = NodeState(address=1, last_seen_monotonic=_time.monotonic())
    await coord._async_query(1, "T")
    # Bump last_seen so it completes cleanly (not a failure).
    coord.nodes[1].last_seen_monotonic = _time.monotonic()
    await _asyncio.sleep(0.05)
    await hass.async_block_till_done()

    assert len(coord._pending_query_checks) == 0


async def test_handle_message_updates_last_seen(hass, coord) -> None:
    """Every received message bumps last_seen_monotonic for its node.

    This is the field the query-timeout check uses to determine
    responsiveness. If it weren't being maintained, all per-node
    queries would eventually time out regardless of bus health.
    """
    import time as _time  # noqa: PLC0415

    from protocol import NodeMessage  # noqa: PLC0415

    before = _time.monotonic()
    coord._handle_message(
        NodeMessage(address=4, command="T", value="72F", name=None, raw="")
    )
    await hass.async_block_till_done()

    node = coord.nodes[4]
    assert node.last_seen_monotonic is not None
    assert node.last_seen_monotonic >= before


async def test_query_with_response_before_check_completes(
    hass, coord, monkeypatch
) -> None:
    """A response arriving partway through the window still avoids timeout."""
    import asyncio as _asyncio  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    from custom_components.aprilaire_rs485 import (  # noqa: PLC0415
        coordinator as coord_module,
    )
    from custom_components.aprilaire_rs485.const import (  # noqa: PLC0415
        EVENT_QUERY_TIMEOUT,
    )
    from custom_components.aprilaire_rs485.coordinator import (  # noqa: PLC0415
        NodeState,
    )

    monkeypatch.setattr(coord_module, "QUERY_RESPONSE_TIMEOUT_S", 0.10)
    events: list = []
    hass.bus.async_listen(EVENT_QUERY_TIMEOUT, events.append)

    coord.nodes[1] = NodeState(
        address=1, last_seen_monotonic=_time.monotonic() - 10.0
    )

    await coord._async_query(1, "T")
    # Halfway through the window, simulate a response.
    await _asyncio.sleep(0.05)
    coord.nodes[1].last_seen_monotonic = _time.monotonic()
    await _asyncio.sleep(0.10)
    await hass.async_block_till_done()

    assert events == []
    assert coord.query_timeouts_count == 0
