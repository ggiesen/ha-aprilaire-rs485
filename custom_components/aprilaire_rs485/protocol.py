# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Aprilaire Model 8800 RS-485 protocol handler.

This module deliberately does the timing-critical work on a dedicated thread,
not on the asyncio event loop. Home Assistant's event loop sees too much
jitter under load to reliably hit the 65 ms sub-slot windows of this protocol.

Transport is abstracted by pyserial's ``serial_for_url`` helper, so the same
code path handles:

  * Local serial:        ``/dev/ttyUSB0``, ``COM3``
  * Hardware-grep:       ``hwgrep://USB-RS422``
  * TCP serial gateway:  ``socket://192.168.1.50:8000``
  * RFC 2217 gateway:    ``rfc2217://192.168.1.50:2217``

For TCP transports we also disable Nagle so our short commands aren't
buffered for ~40 ms by the kernel. If your gateway also packetises on its
serial-side buffer, configure it for minimal idle-flush time (10-20 ms)
or single-byte packets.

The host transmits whenever it wants (the 8800s are slaves and listen on a
dedicated pair), but it MUST space its commands to give nodes time to respond
inside their assigned slots, and MUST NOT send a fresh <CR> while waiting on a
response.

Timing rules (from the 8800 Programmer's Manual, pp. 2-4):
  - Slot width:    262.144 ms @ 9600 bps,  131.072 ms @ 19200 bps
  - Sub-slot:       65.536 ms @ 9600 bps,   32.768 ms @ 19200 bps
  - After an explicit-address command (response expected or not), wait at
    least (slot_width + sub_slot_width) before the next command to the same
    node. We apply it bus-wide as a conservative simplification.
  - After a global-address command WITH response expected, wait at least
    (slot_width * max_address) - i.e. a full frame.
  - The host should also transmit a <CR> every 12 hours to keep the bus
    synchronized; sending any command satisfies that.

There are no checksums and no error responses. A malformed or unaddressed
command is silently dropped. Confirm critical writes by reading them back.
"""

from __future__ import annotations

import contextlib
import logging
import re
import socket as _socket_mod
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Empty, Queue

import serial

_LOGGER = logging.getLogger(__name__)

CR = b"\r"

SLOT_WIDTH_MS = {9600: 262.144, 19200: 131.072}
SUBSLOT_WIDTH_MS = {9600: 65.536, 19200: 32.768}

# How long to wait between reconnection attempts when the transport is down.
RECONNECT_BACKOFF_S = 5.0
# How long the tx loop will wait for the transport to come up before dropping
# a queued command. We drop rather than infinitely queue because old setpoint
# commands aren't useful - HA will retry from current state.
TX_CONNECTION_TIMEOUT_S = 15.0

# Commands that may legitimately come back with no '=' in the response.
_BARE_RESPONSE_PATTERNS = {
    "ID": re.compile(r"^MODEL#\s+\S+\s+REV:\s+\S+\s+RPC\s+\S+$"),
    "BLTON": re.compile(r"^BLTON$"),
}

# Bus error categories. Kept distinct because they implicate different
# troubleshooting axes: a flood of parse errors points at the bus wiring
# (noise, mis-termination), while transport errors implicate the USB cable,
# TCP gateway, or serial server. Conflating them loses that signal.
ERROR_PARSE = "parse_error"
ERROR_TRANSPORT = "transport_error"


@dataclass
class BusError:
    """A single error event from the protocol layer.

    ``category`` is one of the ERROR_* constants above. ``detail`` is a
    human-readable description suitable for logging or surfacing in an HA
    event payload. ``raw`` is the offending bytes for parse errors, or None
    for transport errors where there's no associated wire content.
    """

    category: str
    detail: str
    raw: str | None = None


@dataclass
class NodeMessage:
    """A parsed message received from a node."""

    address: int
    command: str
    value: str | None
    name: str | None = None
    raw: str = ""


@dataclass
class _PendingTx:
    payload: bytes
    is_global: bool
    expect_response: bool
    description: str
    sent_event: threading.Event = field(default_factory=threading.Event)


class ProtocolError(Exception):
    """Raised for unrecoverable protocol problems."""


class Aprilaire8800Protocol:
    """Thread-based RS-485 driver for the Aprilaire 8800.

    Args:
        url: A pyserial URL or device path. ``/dev/ttyUSB0`` for local serial
            (e.g. through an 8811 + USB-RS-232 cable), ``socket://host:port``
            for a TCP-to-RS-485 gateway, ``rfc2217://host:port`` for a
            telnet-style remote serial.
        baud: Baud rate of the *thermostat bus*, used for timing calculations.
            For TCP transports this must match what the gateway is sending to
            the thermostats - it is unrelated to the TCP link itself.
        max_address: Highest node address present on the bus. Set this to
            match your installed ``NETST`` value (the count of thermostats on
            the bus). Leaving it at 64 makes a full frame ~16.8 s long and
            denies high-address nodes their unsolicited-message slots.
    """

    def __init__(
        self,
        url: str,
        baud: int = 9600,
        max_address: int = 64,
        rx_timeout_s: float = 0.05,
    ) -> None:
        if baud not in SLOT_WIDTH_MS:
            raise ValueError(f"Unsupported baud rate {baud}; use 9600 or 19200")
        if not (1 <= max_address <= 64):
            raise ValueError("max_address must be between 1 and 64")

        self._url = url
        self._baud = baud
        self._max_address = max_address
        self._rx_timeout_s = rx_timeout_s

        self._slot_s = SLOT_WIDTH_MS[baud] / 1000.0
        self._subslot_s = SUBSLOT_WIDTH_MS[baud] / 1000.0
        self._min_gap_s = self._slot_s + self._subslot_s
        self._global_response_gap_s = self._slot_s * self._max_address

        # Transport state (guarded by _conn_lock).
        self._conn_lock = threading.Lock()
        self._serial: serial.SerialBase | None = None
        self._connected = threading.Event()

        self._tx_queue: Queue[_PendingTx] = Queue()
        self._rx_thread: threading.Thread | None = None
        self._tx_thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._listeners: list[Callable[[NodeMessage], None]] = []
        self._raw_listeners: list[Callable[[str], None]] = []
        self._error_listeners: list[Callable[[BusError], None]] = []
        self._listeners_lock = threading.Lock()

        self._last_tx_monotonic: float = 0.0
        self._last_tx_was_global_with_response: bool = False

        # Diagnostic counters, incremented from the RX/TX threads. Plain ints
        # need no lock - CPython's GIL makes ``int += 1`` atomic. parse_errors
        # counts complete lines that failed to parse; transport_errors counts
        # open/read/write failures. messages_received counts every line read
        # (parsed or not), so parse_errors / messages_received is a meaningful
        # ratio; messages_sent counts writes that landed on the wire (a failed
        # write increments transport_errors instead).
        self.parse_error_count: int = 0
        self.transport_error_count: int = 0
        self.messages_sent_count: int = 0
        self.messages_received_count: int = 0

    # ---------- public API ----------

    @property
    def max_address(self) -> int:
        """Return the configured highest node address."""
        return self._max_address

    @property
    def baud(self) -> int:
        """Return the configured bus baud rate."""
        return self._baud

    @property
    def slot_seconds(self) -> float:
        """Return the duration of one TDMA slot in seconds."""
        return self._slot_s

    @property
    def is_connected(self) -> bool:
        """Return True if the transport is currently open."""
        return self._connected.is_set()

    def add_listener(self, cb: Callable[[NodeMessage], None]) -> None:
        """Register a callback to receive parsed node messages."""
        with self._listeners_lock:
            self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[NodeMessage], None]) -> None:
        """Deregister a previously-added listener."""
        with self._listeners_lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    def add_raw_listener(self, cb: Callable[[str], None]) -> None:
        """Register a callback to receive every raw line for debug logging."""
        with self._listeners_lock:
            self._raw_listeners.append(cb)

    def add_error_listener(self, cb: Callable[[BusError], None]) -> None:
        """Register a callback to receive bus error events.

        Listeners are invoked synchronously on the RX/TX thread when errors
        happen, so they should be cheap and non-blocking. The coordinator
        wires this to ``hass.loop.call_soon_threadsafe`` to marshal back onto
        the event loop before doing anything HA-side.
        """
        with self._listeners_lock:
            self._error_listeners.append(cb)

    def remove_error_listener(self, cb: Callable[[BusError], None]) -> None:
        """Deregister a previously-added error listener."""
        with self._listeners_lock:
            if cb in self._error_listeners:
                self._error_listeners.remove(cb)

    def _emit_error(self, error: BusError) -> None:
        """Dispatch an error to registered listeners; never raises.

        A listener exception here would either crash the thread or spam the
        log on every error, both worse than swallowing the listener bug. We
        log it at exception level for visibility but keep processing.
        """
        with self._listeners_lock:
            listeners = list(self._error_listeners)
        for cb in listeners:
            try:
                cb(error)
            except Exception:
                _LOGGER.exception("error listener failed")

    def start(self) -> None:
        """Start the RX and TX threads. Idempotent."""
        if self._rx_thread is not None and self._rx_thread.is_alive():
            return
        _LOGGER.info(
            "Starting Aprilaire 8800 driver: url=%s baud=%d max_address=%d",
            self._url,
            self._baud,
            self._max_address,
        )
        self._stop.clear()
        self._rx_thread = threading.Thread(target=self._rx_loop, name="aprilaire-rx", daemon=True)
        self._tx_thread = threading.Thread(target=self._tx_loop, name="aprilaire-tx", daemon=True)
        self._rx_thread.start()
        self._tx_thread.start()

    def stop(self) -> None:
        """Signal the threads to stop, drain them, and close the transport."""
        self._stop.set()
        # Push a sentinel so the tx loop wakes immediately.
        self._tx_queue.put(
            _PendingTx(
                payload=b"",
                is_global=False,
                expect_response=False,
                description="<stop>",
            )
        )
        if self._tx_thread:
            self._tx_thread.join(timeout=2.0)
        if self._rx_thread:
            self._rx_thread.join(timeout=2.0)
        self._close_transport()

    def send(
        self,
        addr: int | None,
        command: str,
        value: str | None = None,
        query: bool = False,
        expect_response: bool = True,
    ) -> None:
        """Queue a command for transmission."""
        payload = self._encode(addr, command, value, query)
        is_global = addr is None or addr == 0
        item = _PendingTx(
            payload=payload,
            is_global=is_global,
            expect_response=expect_response,
            description=payload.rstrip(CR).decode("ascii", errors="replace"),
        )
        self._tx_queue.put(item)

    def send_and_wait(
        self,
        addr: int | None,
        command: str,
        value: str | None = None,
        query: bool = False,
        expect_response: bool = True,
        timeout: float = 5.0,
    ) -> bool:
        """Queue a command and block until written to the wire."""
        payload = self._encode(addr, command, value, query)
        is_global = addr is None or addr == 0
        item = _PendingTx(
            payload=payload,
            is_global=is_global,
            expect_response=expect_response,
            description=payload.rstrip(CR).decode("ascii", errors="replace"),
        )
        self._tx_queue.put(item)
        return item.sent_event.wait(timeout=timeout)

    # ---------- transport plumbing ----------

    @staticmethod
    def _encode(addr: int | None, command: str, value: str | None, query: bool) -> bytes:
        cmd = command.upper().strip()
        if addr is None or addr == 0:
            head = "SN"
        else:
            if not (1 <= addr <= 64):
                raise ValueError(f"Address {addr} out of range 1..64")
            head = f"SN{addr}"
        if query:
            body = f"{head} {cmd}?"
        elif value is None:
            body = f"{head} {cmd}"
        else:
            body = f"{head} {cmd}={value}"
        return body.encode("ascii") + CR

    def _open_transport(self) -> bool:
        """Attempt to open the transport. Returns True on success."""
        with self._conn_lock:
            if self._serial is not None:
                return True
            try:
                ser = serial.serial_for_url(
                    self._url,
                    baudrate=self._baud,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=self._rx_timeout_s,
                    write_timeout=2.0,
                )
            except Exception as exc:
                _LOGGER.warning("Failed to open %s: %s", self._url, exc)
                self.transport_error_count += 1
                self._emit_error(
                    BusError(category=ERROR_TRANSPORT, detail=f"open failed: {exc}")
                )
                return False
            # If this is a TCP transport (pyserial's socket:// handler), pry
            # out the underlying socket and disable Nagle. Best-effort.
            self._maybe_tune_socket(ser)
            self._serial = ser
            self._connected.set()
            _LOGGER.info("Transport open: %s", self._url)
            return True

    @staticmethod
    def _maybe_tune_socket(ser: serial.SerialBase) -> None:
        sock = getattr(ser, "_socket", None)
        if sock is None:
            return
        try:
            sock.setsockopt(_socket_mod.IPPROTO_TCP, _socket_mod.TCP_NODELAY, 1)
            # Keepalive helps detect a half-dead gateway sooner.
            sock.setsockopt(_socket_mod.SOL_SOCKET, _socket_mod.SO_KEEPALIVE, 1)
        except (OSError, AttributeError) as exc:
            _LOGGER.debug("TCP socket tuning failed (non-fatal): %s", exc)

    def _close_transport(self) -> None:
        with self._conn_lock:
            self._connected.clear()
            if self._serial is not None:
                with contextlib.suppress(Exception):
                    self._serial.close()
                self._serial = None

    # ---------- threads ----------

    def _tx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._tx_queue.get(timeout=0.1)
            except Empty:
                continue
            if self._stop.is_set():
                return
            if not item.payload:
                continue

            # Wait for transport to be available.
            if not self._connected.wait(timeout=TX_CONNECTION_TIMEOUT_S):
                _LOGGER.warning("Transport down; dropping queued command: %s", item.description)
                continue
            if self._stop.is_set():
                return

            # Enforce post-previous-command gap.
            gap = (
                self._global_response_gap_s
                if self._last_tx_was_global_with_response
                else self._min_gap_s
            )
            elapsed = time.monotonic() - self._last_tx_monotonic
            if elapsed < gap:
                wait = gap - elapsed
                _LOGGER.debug("Sleeping %.3fs to respect timing", wait)
                if self._stop.wait(timeout=wait):
                    return

            # Snapshot the serial under the lock to avoid races with reconnect.
            with self._conn_lock:
                ser = self._serial
            if ser is None:
                # Lost connection between the wait and now; requeue once and retry.
                self._tx_queue.put(item)
                continue

            try:
                ser.write(item.payload)
                ser.flush()
            except (serial.SerialException, OSError) as exc:
                _LOGGER.warning("Write failed (%s): %s", item.description, exc)
                self.transport_error_count += 1
                self._emit_error(
                    BusError(
                        category=ERROR_TRANSPORT,
                        detail=f"write failed ({item.description}): {exc}",
                    )
                )
                self._close_transport()
                # Don't requeue. HA will retry from current state on next user
                # action or periodic refresh.
                continue

            _LOGGER.debug("TX -> %s", item.description)
            self.messages_sent_count += 1
            self._last_tx_monotonic = time.monotonic()
            self._last_tx_was_global_with_response = item.is_global and item.expect_response
            item.sent_event.set()

    def _rx_loop(self) -> None:
        buf = bytearray()
        while not self._stop.is_set():
            if not self._connected.is_set() and not self._open_transport():
                if self._stop.wait(timeout=RECONNECT_BACKOFF_S):
                    return
                continue

            with self._conn_lock:
                ser = self._serial
            if ser is None:
                continue

            try:
                chunk = ser.read(64)
            except (serial.SerialException, OSError) as exc:
                _LOGGER.warning("Read failed: %s; will reconnect", exc)
                self.transport_error_count += 1
                self._emit_error(
                    BusError(category=ERROR_TRANSPORT, detail=f"read failed: {exc}")
                )
                self._close_transport()
                buf.clear()
                if self._stop.wait(timeout=RECONNECT_BACKOFF_S):
                    return
                continue

            if not chunk:
                continue
            buf.extend(chunk)
            # Drain whole lines.
            while True:
                idx = -1
                for sep in (b"\r", b"\n"):
                    j = buf.find(sep)
                    if j >= 0 and (idx == -1 or j < idx):
                        idx = j
                if idx < 0:
                    break
                line_bytes = bytes(buf[:idx])
                del buf[: idx + 1]
                if not line_bytes:
                    continue
                try:
                    line = line_bytes.decode("ascii", errors="replace").strip()
                except Exception:
                    continue
                if not line:
                    continue
                self._dispatch_raw(line)

    def _dispatch_raw(self, line: str) -> None:
        _LOGGER.debug("RX <- %s", line)
        # Count every successfully-read line before parsing, so lines that
        # fail to parse still count toward the parse-error-rate denominator.
        self.messages_received_count += 1
        with self._listeners_lock:
            raw_listeners = list(self._raw_listeners)
            listeners = list(self._listeners)
        for cb in raw_listeners:
            try:
                cb(line)
            except Exception:
                _LOGGER.exception("raw listener failed")
        msg = parse_message(line)
        if msg is None:
            _LOGGER.debug("Unparseable: %s", line)
            self.parse_error_count += 1
            self._emit_error(
                BusError(category=ERROR_PARSE, detail="unparseable line", raw=line)
            )
            return
        for cb in listeners:
            try:
                cb(msg)
            except Exception:
                _LOGGER.exception("message listener failed")


# ---------- parsing ----------

_PREFIX_RE = re.compile(r"^SN(?P<addr>\d{1,2})(?:\s+(?P<rest>.*))?$")


def parse_message(line: str) -> NodeMessage | None:  # noqa: PLR0911
    """Parse a single received line into a :class:`NodeMessage`.

    Many early-exit branches are intentional - each ASCII response form is
    matched independently - so PLR0911 is suppressed for this function.
    """
    line = line.strip()
    if not line:
        return None
    m = _PREFIX_RE.match(line)
    if not m:
        return None
    try:
        addr = int(m.group("addr"))
    except ValueError:
        return None
    rest = (m.group("rest") or "").strip()

    if not rest:
        return NodeMessage(address=addr, command="PRESENT", value=None, raw=line)

    if rest.startswith("MODEL#"):
        return NodeMessage(address=addr, command="ID", value=rest, raw=line)

    if "=" in rest:
        before_eq, after_eq = rest.split("=", 1)
        tokens = before_eq.strip().split()
        if not tokens:
            return None
        command = tokens[-1].upper()
        name = " ".join(tokens[:-1]) if len(tokens) > 1 else None
        return NodeMessage(
            address=addr,
            command=command,
            value=after_eq.strip(),
            name=name,
            raw=line,
        )

    if _BARE_RESPONSE_PATTERNS["BLTON"].match(rest):
        return NodeMessage(address=addr, command="BLTON", value=None, raw=line)
    return NodeMessage(address=addr, command="NAME", value=rest, name=rest, raw=line)


# ---------- value decoders ----------

_TEMP_RE = re.compile(r"^(?P<sign>[+-]?)(?P<val>-?\d+)\s*(?P<scale>[FC])?$")
_HUM_RE = re.compile(r"^(?P<val>\d+)%?$")
_NA_RE = re.compile(r"^-+(?P<scale>[FC%])?$")


def decode_temperature(value: str) -> tuple[float | None, str | None]:
    """Decode a thermostat temperature value like ``72F`` or ``-10F``.

    Returns ``(value, scale)`` where ``scale`` is ``"F"`` or ``"C"``. Returns
    ``(None, scale)`` for the ``--F`` "not available" form and
    ``(None, None)`` for empty input.
    """
    if value is None:
        return None, None
    value = value.strip()
    if not value:
        return None, None
    if _NA_RE.match(value):
        scale = value.replace("-", "") or None
        return None, scale
    m = _TEMP_RE.match(value)
    if not m:
        return None, None
    v = int(m.group("val"))
    if m.group("sign") == "-":
        v = -abs(v)
    return float(v), m.group("scale")


def decode_humidity(value: str) -> int | None:
    """Decode a humidity value like ``35%``. Returns ``None`` if unavailable."""
    if value is None:
        return None
    value = value.strip()
    if "--" in value:
        return None
    m = _HUM_RE.match(value)
    if not m:
        return None
    return int(m.group("val"))


def decode_hvac(value: str) -> dict[str, bool]:
    """Decode a packed HVAC relay string like ``G-Y1-W1+Y2-W2+B+O-``.

    Returns ``{relay_name: is_on, ...}`` for relays the device included, or
    an empty dict if the value couldn't be parsed.
    """
    out: dict[str, bool] = {}
    if not value:
        return out
    relays = ["G", "Y1", "W1", "Y2", "W2", "B", "O"]
    s = value.strip()
    for r in relays:
        if s.startswith(r):
            s = s[len(r) :]
            if not s:
                return {}
            sign = s[0]
            s = s[1:]
            out[r] = sign == "+"
        else:
            return out
    return out


def decode_errors(value: str) -> dict[str, int]:
    """Decode the six-digit ERROR response into named flags.

    Returns an empty dict for empty input or anything whose first six
    characters aren't all digits - e.g. a framing glitch that merged two
    responses into one line. Such corrupt lines are dropped rather than
    raising.
    """
    if not value:
        return {}
    s = value.strip()
    if len(s) < 6 or not s[:6].isdigit():
        return {}
    return {
        "builtin_temp": int(s[0]),
        "remote_temp": int(s[1]),
        "outdoor_temp": int(s[2]),
        "builtin_humidity": int(s[3]),
        "comm": int(s[4]),
        "eeprom": int(s[5]),
    }


# ---------- support module parsing ----------
#
# Manual p.32: Up to four support modules per node, each with two
# sensors. Sensor 1 is always temperature (CT, RT, or XX). Sensor 2 is
# temperature or humidity (CT, RT, CH, RH, or XX). Modules not connected
# are OMITTED from the RSM response (not listed as XX).

_RSM_TYPES_S1 = frozenset({"CT", "RT", "XX"})
_RSM_TYPES_S2 = frozenset({"CT", "RT", "CH", "RH", "XX"})


def parse_rsm(value: str) -> dict[tuple[int, int], str]:
    """Parse an RSM response value into ``{(module, sensor): type_code}``.

    Only sensors with a real type code (CT/RT/CH/RH) are returned. XX
    placeholders are filtered out, as are malformed module blocks.
    Module addresses are 1..4 and sensor numbers are 1 or 2.

    Examples:
        ``"M1:RT,RH"`` -> ``{(1, 1): "RT", (1, 2): "RH"}``
        ``"M1:CT,RH M3:CT,RT"`` -> four entries
        ``"M1:CT,XX"`` -> ``{(1, 1): "CT"}`` (XX dropped)
        ``""`` -> ``{}`` (no modules connected)
    """
    out: dict[tuple[int, int], str] = {}
    if not value:
        return out
    for block in value.strip().split():
        if ":" not in block:
            continue
        mod_str, sensors_str = block.split(":", 1)
        if not mod_str.startswith("M"):
            continue
        try:
            mod = int(mod_str[1:])
        except ValueError:
            continue
        if not 1 <= mod <= 4:
            continue
        sensor_codes = [s.strip().upper() for s in sensors_str.split(",")]
        for i, code in enumerate(sensor_codes, start=1):
            if i > 2:
                break
            allowed = _RSM_TYPES_S1 if i == 1 else _RSM_TYPES_S2
            if code in allowed and code != "XX":
                out[(mod, i)] = code
    return out


_RXSY_CMD_RE = re.compile(r"^R(?P<module>[1-4])S(?P<sensor>[1-2])$")


def parse_rxsy_command(command: str) -> tuple[int, int] | None:
    """Parse a command like ``R1S2`` into ``(module, sensor)`` or None.

    Used by the coordinator to recognise per-support-module sensor
    responses. Returns ``None`` for anything that doesn't match the
    pattern so callers can fall through to other command handlers.
    """
    m = _RXSY_CMD_RE.match(command.upper())
    if not m:
        return None
    return int(m.group("module")), int(m.group("sensor"))
