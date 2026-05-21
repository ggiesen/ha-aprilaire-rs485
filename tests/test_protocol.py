# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unit tests for the protocol layer.

These tests exercise message parsing, encoding, and value decoding directly,
without any Home Assistant fixtures. They are deliberately fast and have no
external dependencies so they can run anywhere pyserial is available.
"""

from __future__ import annotations

import time

import pytest
from protocol import (
    Aprilaire8800Protocol,
    NodeMessage,
    decode_errors,
    decode_humidity,
    decode_hvac,
    decode_temperature,
    parse_message,
)

# ---------- parser ----------


def test_parse_simple_value_message() -> None:
    """A typical SN# CMD=VALUE response parses cleanly."""
    msg = parse_message("SN1 T=72F")
    assert msg == NodeMessage(address=1, command="T", value="72F", name=None, raw="SN1 T=72F")


def test_parse_message_with_name_prefix() -> None:
    """Multi-word names before the command keyword are captured into ``name``."""
    msg = parse_message("SN12 MASTER BEDROOM T=72F")
    assert msg is not None
    assert msg.address == 12
    assert msg.name == "MASTER BEDROOM"
    assert msg.command == "T"
    assert msg.value == "72F"


def test_parse_two_digit_address() -> None:
    """Two-digit node addresses parse correctly."""
    msg = parse_message("SN64 SH=68F")
    assert msg is not None
    assert msg.address == 64


def test_parse_present_response() -> None:
    """Bare SN# is the response to a global NULL query (PRESENT)."""
    msg = parse_message("SN1")
    assert msg is not None
    assert msg.command == "PRESENT"
    assert msg.value is None


def test_parse_id_response() -> None:
    """The ID response has its own multi-token format and is recognised."""
    msg = parse_message("SN1 MODEL# 8800 REV: 1.0 RPC 2011")
    assert msg is not None
    assert msg.command == "ID"
    assert msg.value is not None
    assert "8800" in msg.value


def test_parse_name_only_response() -> None:
    """A bare-text response with no '=' is treated as a NAME reply."""
    msg = parse_message("SN1 MASTER BEDROOM")
    assert msg is not None
    assert msg.command == "NAME"
    assert msg.name == "MASTER BEDROOM"
    assert msg.value == "MASTER BEDROOM"


def test_parse_blton_ack() -> None:
    """BLTON has a no-value response that we recognise as a write ack."""
    msg = parse_message("SN1 BLTON")
    assert msg is not None
    assert msg.command == "BLTON"
    assert msg.value is None


def test_parse_hvac_value_kept_as_is() -> None:
    """HVAC packed relay strings round-trip through parse without changes."""
    msg = parse_message("SN1 HVAC=G-Y1-W1+Y2-W2+B+O-")
    assert msg is not None
    assert msg.command == "HVAC"
    assert msg.value == "G-Y1-W1+Y2-W2+B+O-"


def test_parse_garbage_returns_none() -> None:
    """Lines that don't begin with SN are ignored."""
    assert parse_message("garbage") is None
    assert parse_message("") is None
    assert parse_message("SX1 T=72F") is None


def test_parse_address_out_of_range_still_parses() -> None:
    """Addresses outside 1..64 still parse; the parser doesn't enforce range."""
    # The 8800 spec allows 1..64 but the parser is liberal in what it accepts;
    # range enforcement is the encoder/coordinator's job.
    msg = parse_message("SN99 T=72F")
    assert msg is not None
    assert msg.address == 99


# ---------- temperature decoder ----------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("72F", (72.0, "F")),
        ("-10F", (-10.0, "F")),
        ("+5F", (5.0, "F")),
        ("20C", (20.0, "C")),
        ("0F", (0.0, "F")),
        ("--F", (None, "F")),
        ("--", (None, None)),
        ("", (None, None)),
    ],
)
def test_decode_temperature(value: str, expected: tuple) -> None:
    """The temperature decoder handles signed values and unavailable markers."""
    assert decode_temperature(value) == expected


# ---------- humidity decoder ----------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("35%", 35),
        ("0%", 0),
        ("99%", 99),
        ("--%", None),
        ("--", None),
    ],
)
def test_decode_humidity(value: str, expected: int | None) -> None:
    """The humidity decoder handles percent and unavailable markers."""
    assert decode_humidity(value) == expected


# ---------- HVAC relay decoder ----------


def test_decode_hvac_all_off() -> None:
    """All-off relay string parses to a dict of False values."""
    relays = decode_hvac("G-Y1-W1-Y2-W2-B-O-")
    assert relays == {
        "G": False,
        "Y1": False,
        "W1": False,
        "Y2": False,
        "W2": False,
        "B": False,
        "O": False,
    }


def test_decode_hvac_mixed() -> None:
    """A documented mixed example parses to the expected booleans."""
    relays = decode_hvac("G-Y1-W1+Y2-W2+B+O-")
    assert relays["W1"] is True
    assert relays["W2"] is True
    assert relays["B"] is True
    assert relays["O"] is False
    assert relays["G"] is False


def test_decode_hvac_malformed_returns_partial_or_empty() -> None:
    """Malformed relay strings are tolerated; we just stop at the first mismatch."""
    # No exception on garbage.
    assert decode_hvac("nonsense") == {}


# ---------- ERROR decoder ----------


def test_decode_errors_all_zero() -> None:
    """All-zero error string decodes to all-zero flags."""
    errors = decode_errors("000000")
    assert errors == {
        "builtin_temp": 0,
        "remote_temp": 0,
        "outdoor_temp": 0,
        "builtin_humidity": 0,
        "comm": 0,
        "eeprom": 0,
    }


def test_decode_errors_comm_error() -> None:
    """Position 4 in the error string corresponds to the comm flag."""
    errors = decode_errors("000010")
    assert errors["comm"] == 1
    assert errors["builtin_temp"] == 0


def test_decode_errors_too_short_returns_empty() -> None:
    """Strings shorter than six digits return an empty dict rather than raising."""
    assert decode_errors("01") == {}
    assert decode_errors("") == {}


# ---------- encoder ----------


def test_encode_query_explicit() -> None:
    """An explicit-address query encodes as 'SN# CMD?<CR>'."""
    assert Aprilaire8800Protocol._encode(1, "T", None, True) == b"SN1 T?\r"


def test_encode_assignment_explicit() -> None:
    """An explicit-address assignment encodes as 'SN# CMD=VALUE<CR>'."""
    assert Aprilaire8800Protocol._encode(1, "SH", "68", False) == b"SN1 SH=68\r"


def test_encode_global_query() -> None:
    """A global (addr=None) query encodes with no number after SN."""
    assert Aprilaire8800Protocol._encode(None, "T", None, True) == b"SN T?\r"


def test_encode_addr_zero_is_global() -> None:
    """Address 0 is treated as global per manual p.5."""
    assert Aprilaire8800Protocol._encode(0, "T", None, True) == b"SN T?\r"


def test_encode_rejects_out_of_range() -> None:
    """Explicit addresses outside 1..64 raise."""
    with pytest.raises(ValueError, match="out of range"):
        Aprilaire8800Protocol._encode(65, "T", None, True)
    with pytest.raises(ValueError, match="out of range"):
        Aprilaire8800Protocol._encode(-1, "T", None, True)


def test_encode_uppercases_command() -> None:
    """Command keywords are normalised to upper case."""
    assert Aprilaire8800Protocol._encode(1, "sh", "68", False) == b"SN1 SH=68\r"


# ---------- end-to-end through loop:// transport ----------


def test_loopback_round_trip() -> None:
    """Sending three commands through loop:// produces three parsed messages.

    The loop:// transport echoes writes back as reads, so we use it to verify
    that the TX and RX threads start up, write our bytes, read them back,
    parse them, and dispatch to listeners - without needing a real device.
    """
    received: list[NodeMessage] = []
    proto = Aprilaire8800Protocol(url="loop://", baud=9600, max_address=4)
    proto.add_listener(received.append)
    proto.start()
    try:
        # Wait for the RX thread to open the transport.
        deadline = time.monotonic() + 2.0
        while not proto.is_connected and time.monotonic() < deadline:
            time.sleep(0.05)
        assert proto.is_connected

        proto.send(1, "SH", "68")
        proto.send(2, "M", "COOL")
        proto.send(3, "F", "AUTO")

        # The first send has no prior gap; the other two each wait
        # slot_width + sub_slot_width ~ 0.328s. Plus loopback latency. Be
        # generous to avoid flake on busy CI runners.
        deadline = time.monotonic() + 3.0
        while len(received) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        proto.stop()

    assert len(received) == 3
    by_addr = {m.address: m for m in received}
    assert by_addr[1].command == "SH"
    assert by_addr[1].value == "68"
    assert by_addr[2].command == "M"
    assert by_addr[2].value == "COOL"
    assert by_addr[3].command == "F"
    assert by_addr[3].value == "AUTO"


def test_baud_validation() -> None:
    """The constructor rejects unsupported baud rates."""
    with pytest.raises(ValueError, match="Unsupported baud rate"):
        Aprilaire8800Protocol(url="loop://", baud=4800)


def test_max_address_validation() -> None:
    """The constructor rejects max_address outside 1..64."""
    with pytest.raises(ValueError, match="max_address"):
        Aprilaire8800Protocol(url="loop://", baud=9600, max_address=0)
    with pytest.raises(ValueError, match="max_address"):
        Aprilaire8800Protocol(url="loop://", baud=9600, max_address=100)


def test_timing_parameters_match_manual() -> None:
    """Spot-check the derived timing constants against the manual's table.

    Manual p.2 specifies:
      9600 baud:  slot 262.144 ms, sub-slot 65.536 ms
      19200 baud: slot 131.072 ms, sub-slot 32.768 ms

    These are not arbitrary numbers; they fall out of the bit timing
    (slot = 2520 bit-times, sub-slot = 630 bit-times). The integration
    derives min_gap and global_response_gap from these, so verifying both
    rates here catches any regression that would silently mistime writes.
    """
    p9600 = Aprilaire8800Protocol(url="loop://", baud=9600, max_address=8)
    assert p9600.slot_seconds == pytest.approx(0.262144)
    # Internal derived values, exercised here so they stay in sync with the manual.
    assert p9600._slot_s == pytest.approx(0.262144)
    assert p9600._subslot_s == pytest.approx(0.065536)
    assert p9600._min_gap_s == pytest.approx(0.262144 + 0.065536)
    assert p9600._global_response_gap_s == pytest.approx(0.262144 * 8)

    p19200 = Aprilaire8800Protocol(url="loop://", baud=19200, max_address=8)
    assert p19200.slot_seconds == pytest.approx(0.131072)
    assert p19200._slot_s == pytest.approx(0.131072)
    assert p19200._subslot_s == pytest.approx(0.032768)
    assert p19200._min_gap_s == pytest.approx(0.131072 + 0.032768)
    assert p19200._global_response_gap_s == pytest.approx(0.131072 * 8)

    # 19200 should be roughly twice as fast as 9600 across the board.
    assert p9600.slot_seconds == pytest.approx(p19200.slot_seconds * 2)


def test_baud_property_round_trips() -> None:
    """The configured baud is reflected by the public property at both rates."""
    assert Aprilaire8800Protocol(url="loop://", baud=9600).baud == 9600
    assert Aprilaire8800Protocol(url="loop://", baud=19200).baud == 19200


@pytest.mark.parametrize("baud", [9600, 19200])
def test_loopback_round_trip_at_each_baud(baud: int) -> None:
    """The driver writes and reads three commands at both supported baud rates.

    Exercises both the threading and the timing path. At 19200 the
    inter-command gap is half what it is at 9600, so the test is a sanity
    check that the timing math is hooked up to the actual rate.
    """
    received: list[NodeMessage] = []
    proto = Aprilaire8800Protocol(url="loop://", baud=baud, max_address=4)
    proto.add_listener(received.append)
    proto.start()
    try:
        deadline = time.monotonic() + 2.0
        while not proto.is_connected and time.monotonic() < deadline:
            time.sleep(0.05)
        assert proto.is_connected

        proto.send(1, "SH", "68")
        proto.send(2, "M", "COOL")
        proto.send(3, "F", "AUTO")

        # Generous deadline; the inter-command gaps differ between 9600
        # (~0.328 s each) and 19200 (~0.164 s each) but both fit easily.
        deadline = time.monotonic() + 3.0
        while len(received) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        proto.stop()

    assert len(received) == 3
    by_addr = {m.address: m for m in received}
    assert by_addr[1].command == "SH"
    assert by_addr[1].value == "68"
    assert by_addr[2].command == "M"
    assert by_addr[2].value == "COOL"
    assert by_addr[3].command == "F"
    assert by_addr[3].value == "AUTO"
