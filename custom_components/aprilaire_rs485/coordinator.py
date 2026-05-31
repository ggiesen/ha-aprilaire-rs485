# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Coordinator: bridges the protocol thread to Home Assistant's event loop.

The protocol runs on its own thread (see protocol.py). This module:
  * Receives parsed messages via a thread-safe callback.
  * Marshals them onto the HA loop with hass.loop.call_soon_threadsafe.
  * Maintains a per-node state dict.
  * Exposes async methods for HA platforms to issue commands.
  * Periodically re-asserts COS subscriptions and polls a small set of values
    as a safety net (COS only fires on changes, and the thermostat loses COS
    config on a power cycle).

Design notes (read these before changing things):
  * We do NOT poll aggressively. The whole point of COS messages is to avoid
    polling. Heavy polling will also starve high-address nodes of their
    unsolicited message slots.
  * Per-node command pacing is enforced inside the protocol layer. The
    coordinator queues writes and trusts that.
  * Writes are best-effort. There is no protocol-level ack. We read back
    critical state from the thermostat's own response (which arrives in the
    same slot) and on the next periodic refresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    COS_ENABLE,
    DEFAULT_REFRESH_INTERVAL_S,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    OUTDOOR_TEMP_BROADCAST_INTERVAL_S,
    SIGNAL_NODE_DISCOVERED,
    SIGNAL_NODE_SUPPORT_MODULES,
    SIGNAL_NODE_UPDATED,
)
from .protocol import (
    Aprilaire8800Protocol,
    NodeMessage,
    decode_errors,
    decode_humidity,
    decode_hvac,
    decode_temperature,
    parse_rsm,
    parse_rxsy_command,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class NodeState:
    """Latest known state of a single node.

    All fields default to None to mean 'not yet observed'. Entities should
    treat None as 'unavailable' rather than guessing.
    """

    address: int
    name: str | None = None
    controller_type: int | None = None  # 0 thermostat, 1 humidistat

    # Sensors
    temperature: float | None = None
    temperature_scale: str | None = None
    humidity: int | None = None
    outdoor_temperature: float | None = None
    outdoor_temperature_scale: str | None = None
    outdoor_humidity: int | None = None
    builtin_humidity: int | None = None
    remote_temperature: float | None = None

    # Whether this node has its own outdoor temperature sensor (direct-wired
    # or via Support Module 1 Sensor 1 configured as RT). None means we have
    # not yet probed; the value is set on the first OT response - a real
    # value implies a sensor, "--" implies none. The protocol guarantees that
    # nodes with their own sensor ignore OT assignment commands, so this
    # flag is primarily used to pick a source for rebroadcast.
    has_own_outdoor_sensor: bool | None = None

    # Setpoints
    setpoint_heat: float | None = None
    setpoint_cool: float | None = None
    setpoint_humid: int | None = None
    setpoint_dehum: int | None = None

    # Auto-mode deadband in degrees of the current scale (DBAND, manual p.12).
    # Thermostat only - humidistats ignore the query, so this stays None for
    # them. Read at startup; the device changes it only via installer setup
    # (COS:N/A), so we don't poll it on the periodic refresh.
    deadband: int | None = None

    # Modes
    mode: str | None = None
    fan_mode: str | None = None

    # Hold
    hold_status: str | None = None
    network_override: str | None = None

    # Relays
    relays: dict[str, bool] = field(default_factory=dict)

    # Alarms
    alarm_filter: bool | None = None
    alarm_water_panel: bool | None = None
    alarm_dehumidifier: bool | None = None
    alarm_system: bool | None = None

    # Maintenance alarm periods (the [alarm]ALMP reminder intervals). Keyed by
    # the short alarm code FLT/WP/DEH/SYS; value is the wire string ("OFF" or a
    # month count). Read at startup; EEPROM-backed (not in the power-cycle
    # reset set), so we don't re-apply or poll them.
    alarm_periods: dict[str, str] = field(default_factory=dict)

    # Errors (zero means no error)
    errors: dict[str, int] = field(default_factory=dict)

    # Recovery
    progressive_recovery: bool | None = None

    # Shadow of permanent message slot contents (PMES1..PMES4).
    # The protocol has no read-back path for messages, so this dict records
    # the last value we wrote per slot. Empty string means "explicitly
    # cleared"; an absent key means "never written by us, unknown".
    # Keys are the slot number (1..4); values are the formatted text that
    # actually went on the wire (post ASCII-strip and 32-char truncation).
    permanent_messages: dict[int, str] = field(default_factory=dict)

    # Misc
    # Identity (from ID query response).
    model_info: str | None = None

    # Liveness.
    last_seen_monotonic: float | None = None

    # Support module topology discovered via RSM. Keys are (module_addr,
    # sensor_num) where module_addr is 1..4 and sensor_num is 1..2. Value
    # is the sensor type code: CT (control temperature), RT (remote
    # temperature, display only), CH (control humidity), RH (remote
    # humidity, display only). Sensors of type XX are NOT included -
    # absent entries mean "no sensor in that position".
    support_modules: dict[tuple[int, int], str] = field(default_factory=dict)

    # Current readings from RxSy queries, keyed the same way as
    # support_modules. Value is (numeric_value, scale) where scale is
    # "F", "C", or "%" depending on sensor type. A reading of None means
    # the sensor is disconnected or in error (wire response "--").
    support_module_readings: dict[tuple[int, int], tuple[float | int | None, str | None]] = field(
        default_factory=dict
    )


class Aprilaire8800Coordinator:
    """Owns the protocol and per-node state.

    Named explicitly (not ``Coordinator``) so it doesn't get confused with
    Home Assistant's ``DataUpdateCoordinator``. This coordinator is event-
    driven via change-of-state messages from the bus, with a periodic
    safety-net refresh, so it does not need the polling semantics
    ``DataUpdateCoordinator`` provides.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        baud: int,
        max_address: int,
        explicit_addresses: list[int] | None = None,
        outdoor_temp_source: str | None = None,
        outdoor_temp_rebroadcast: bool = True,
    ) -> None:
        """Initialise the coordinator but do not yet open the transport."""
        self.hass = hass
        self.protocol = Aprilaire8800Protocol(url=url, baud=baud, max_address=max_address)
        self.nodes: dict[int, NodeState] = {}
        self._explicit_addresses = explicit_addresses
        self._refresh_unsub: Callable[[], None] | None = None
        self._ot_unsub: Callable[[], None] | None = None
        self._discovery_done = asyncio.Event()
        self._cos_applied: set[int] = set()
        # OT push configuration. Source is an HA entity_id (sensor.* or
        # weather.*) producing temperature; if empty, rebroadcast controls
        # whether we re-emit OT from a sensor-equipped node to its peers.
        self._ot_source = (outdoor_temp_source or "").strip() or None
        self._ot_rebroadcast = bool(outdoor_temp_rebroadcast)

    def device_info(self, address: int) -> DeviceInfo:
        """Return DeviceInfo for the given node address.

        Each thermostat shows up as a separate device in the HA device
        registry, with all of its entities grouped underneath.
        """
        node = self.nodes.get(address)
        default_name = f"Aprilaire 8800 #{address}"
        name = (node.name if node and node.name else None) or default_name
        sw_version = node.model_info if node and node.model_info else None
        return DeviceInfo(
            identifiers={(DOMAIN, str(address))},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=name,
            sw_version=sw_version,
            via_device=(DOMAIN, "bus"),
        )

    def bus_device_info(self) -> DeviceInfo:
        """Return DeviceInfo for the bus-level pseudo-device.

        Used for entities that describe the bus itself (transport state,
        discovered node count) rather than any one thermostat.
        """
        return DeviceInfo(
            identifiers={(DOMAIN, "bus")},
            manufacturer=MANUFACTURER,
            model="RS-485 bus",
            name="Aprilaire 8800 bus",
        )

    # ---------- lifecycle ----------

    async def async_start(self) -> None:
        """Open the bus and start discovery."""
        # Listener runs on the RX thread; bounce work onto the HA loop.
        self.protocol.add_listener(self._on_message_from_thread)
        # Open the transport from an executor since pyserial does blocking I/O.
        await self.hass.async_add_executor_job(self.protocol.start)
        await self._async_discover()
        self._refresh_unsub = async_track_time_interval(
            self.hass,
            self._async_periodic_refresh,
            timedelta(seconds=DEFAULT_REFRESH_INTERVAL_S),
        )
        # Outdoor temperature push runs on its own cadence (5 minutes - well
        # inside the 10-minute validity window on the device side).
        if self._ot_source or self._ot_rebroadcast:
            self._ot_unsub = async_track_time_interval(
                self.hass,
                self._async_broadcast_outdoor_temp,
                timedelta(seconds=OUTDOOR_TEMP_BROADCAST_INTERVAL_S),
            )

    async def async_stop(self) -> None:
        """Stop the periodic refresh and tear down the bus connection."""
        if self._refresh_unsub is not None:
            self._refresh_unsub()
            self._refresh_unsub = None
        if self._ot_unsub is not None:
            self._ot_unsub()
            self._ot_unsub = None
        await self.hass.async_add_executor_job(self.protocol.stop)

    # ---------- discovery ----------

    async def _async_discover(self) -> None:
        """Find nodes on the bus.

        If the user provided an explicit address list, we just probe those.
        Otherwise we send a global NULL query and collect responses across
        one full frame interval.
        """
        if self._explicit_addresses:
            for addr in self._explicit_addresses:
                self.nodes.setdefault(addr, NodeState(address=addr))
            for addr in self._explicit_addresses:
                await self._async_apply_cos(addr)
                await self._async_initial_query(addr)
            return

        # Broadcast probe and wait for responses to trickle in.
        # Spec: nodes respond during their slot, so we need to wait
        # at least max_address * slot_width.
        wait_s = self.protocol.slot_seconds * self.protocol.max_address + 0.5
        _LOGGER.info(
            "Discovering nodes (waiting %.1fs for responses on %d slots)",
            wait_s,
            self.protocol.max_address,
        )
        await self.hass.async_add_executor_job(
            self.protocol.send,
            None,
            "",
            None,
            True,
            True,  # global SN?
        )
        await asyncio.sleep(wait_s)
        _LOGGER.info("Discovery found %d nodes: %s", len(self.nodes), sorted(self.nodes))
        for addr in sorted(self.nodes):
            await self._async_apply_cos(addr)
            await self._async_initial_query(addr)
        self._discovery_done.set()

    async def _async_apply_cos(self, addr: int) -> None:
        """Enable change-of-state subscriptions on a node.

        Required after any power cycle of the thermostat (settings reset to
        defaults per manual pp.17-18).
        """
        # Use CP1 (active pattern) and enable the COS items we want.
        await self._async_send(addr, "CP", "1")
        for k, v in COS_ENABLE.items():
            await self._async_send(addr, k, v)
        self._cos_applied.add(addr)

    async def _async_initial_query(self, addr: int) -> None:
        """Pull the values that aren't reliably provided by COS at startup."""
        # CT tells us whether this is a thermostat or humidistat node.
        await self._async_query(addr, "CT")
        await self._async_query(addr, "NAME")
        await self._async_query(addr, "ID")
        await self._async_query(addr, "M")
        await self._async_query(addr, "F")
        await self._async_query(addr, "T")
        await self._async_query(addr, "HUM")
        await self._async_query(addr, "SH")
        await self._async_query(addr, "SC")
        await self._async_query(addr, "DBAND")
        await self._async_query(addr, "SHUM")
        await self._async_query(addr, "SDEH")
        await self._async_query(addr, "OT")
        await self._async_query(addr, "OH")
        await self._async_query(addr, "HVAC")
        await self._async_query(addr, "HOLD")
        await self._async_query(addr, "HOLDSTAT")
        await self._async_query(addr, "FLTALM")
        await self._async_query(addr, "WPALM")
        await self._async_query(addr, "DEHALM")
        await self._async_query(addr, "SYSALM")
        await self._async_query(addr, "FLTALMP")
        await self._async_query(addr, "WPALMP")
        await self._async_query(addr, "DEHALMP")
        await self._async_query(addr, "SYSALMP")
        await self._async_query(addr, "ERROR")
        await self._async_query(addr, "RECOVSTAT")
        # RSM is read once at startup; support modules are physical
        # wiring and don't change without an installer event. The
        # response populates node.support_modules; subsequent RxSy
        # queries in periodic_refresh use that map.
        await self._async_query(addr, "RSM")
        # Block to let RSM come back so the periodic refresh that
        # follows can query the right RxSy commands. Short sleep, well
        # within the slot timing - this is run sequentially in startup
        # only, never in the hot path.
        await asyncio.sleep(self.protocol.slot_seconds + 0.05)
        node = self.nodes.get(addr)
        if node:
            for module, sensor in node.support_modules:
                await self._async_query(addr, f"R{module}S{sensor}")

    async def _async_periodic_refresh(self, _now=None) -> None:
        """Re-poll the bits that matter and re-apply COS subscriptions.

        Cheap (a handful of explicit queries per node, well-spaced by the
        protocol layer). Runs every DEFAULT_REFRESH_INTERVAL_S. COS settings
        reset on every thermostat power cycle, so re-applying them on each
        refresh is the safety net.
        """
        for addr in list(self.nodes):
            await self._async_apply_cos(addr)
            await self._async_query(addr, "T")
            await self._async_query(addr, "HUM")
            await self._async_query(addr, "M")
            await self._async_query(addr, "F")
            await self._async_query(addr, "SH")
            await self._async_query(addr, "SC")
            await self._async_query(addr, "HVAC")
            await self._async_query(addr, "HOLD")
            await self._async_query(addr, "ERROR")
            # Support module sensors have no COS bit, so they only
            # update on explicit query. Up to 8 queries per node here in
            # the worst case (4 modules x 2 sensors). The protocol layer
            # enforces inter-command spacing.
            node = self.nodes.get(addr)
            if node:
                for module, sensor in node.support_modules:
                    await self._async_query(addr, f"R{module}S{sensor}")

    # ---------- outdoor temperature push ----------
    #
    # Manual p.34: nodes without their own outdoor sensor accept OT
    # assignment commands and use the value until a 10-minute validity
    # window expires. Nodes with their own sensor (direct-wired or via
    # Support Module 1 Sensor 1 = RT) silently ignore the assignment.
    # Broadcasting globally is therefore safe regardless of which nodes
    # have sensors. The source of the value is the host's choice.

    async def _async_broadcast_outdoor_temp(self, _now=None) -> None:
        """Push a fresh outdoor temperature value to the bus.

        Called on a fixed cadence (OUTDOOR_TEMP_BROADCAST_INTERVAL_S).
        Picks a value from either the configured HA source entity or, if
        unset, the lowest-addressed node that has its own sensor. Sends
        nothing if neither path yields a usable value - thermostats that
        depend on the value will simply report no outdoor temp until the
        next successful broadcast.
        """
        value_scale = self._resolve_outdoor_temp_value()
        if value_scale is None:
            _LOGGER.debug(
                "OT broadcast skipped: no source value available (ha_source=%s, rebroadcast=%s)",
                self._ot_source,
                self._ot_rebroadcast,
            )
            return
        value, scale = value_scale
        payload = f"{value}{scale}"
        _LOGGER.debug("Broadcasting OT=%s globally", payload)
        # Global write (addr=None): every node receives, sensor-equipped
        # nodes ignore per spec. expect_response=False because the
        # assignment form does not generate a response (only the COS
        # message from any node whose displayed OT changes).
        await self.hass.async_add_executor_job(
            self.protocol.send, None, "OT", payload, False, False
        )

    def _resolve_outdoor_temp_value(self) -> tuple[int, str] | None:
        """Return ``(value, scale)`` for the next OT broadcast, or None.

        Source priority:
          1. ``CONF_OUTDOOR_TEMP_SOURCE`` - HA entity state.
          2. Lowest-addressed node where ``has_own_outdoor_sensor`` is
             True and ``outdoor_temperature`` is populated.

        Values are rounded to whole degrees (the protocol uses integers)
        and clamped to the spec ranges (-40..130 F, -40..55 C).
        """
        if self._ot_source:
            ha = self._read_ha_temperature(self._ot_source)
            if ha is not None:
                return ha

        if not self._ot_rebroadcast:
            return None

        # Rebroadcast path: pick the lowest address with a real own-sensor
        # reading. We treat the cached node.outdoor_temperature as fresh
        # enough because COS updates it whenever the device's value
        # changes by >=1 degree.
        for addr in sorted(self.nodes):
            node = self.nodes[addr]
            if node.has_own_outdoor_sensor and node.outdoor_temperature is not None:
                scale = node.outdoor_temperature_scale or "F"
                return self._clamp_outdoor(round(node.outdoor_temperature), scale)
        return None

    def _read_ha_temperature(self, entity_id: str) -> tuple[int, str] | None:
        """Read an HA temperature entity, return ``(int, scale)`` or None.

        The numeric value lives in a different place per domain: ``sensor``
        and ``number`` carry it in the state itself, ``weather`` in its
        ``temperature`` attribute, and ``climate`` in its
        ``current_temperature`` attribute.
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        if state.state in ("unknown", "unavailable", None, ""):
            return None
        attrs = state.attributes or {}
        domain = entity_id.split(".", 1)[0]
        if domain == "weather":
            raw = attrs.get("temperature")
        elif domain == "climate":
            raw = attrs.get("current_temperature")
        else:
            raw = state.state
        try:
            value = float(raw)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "OT source %s has no usable numeric temperature (%r); skipping",
                entity_id,
                raw,
            )
            return None
        # Unit: sensors/numbers expose unit_of_measurement; weather exposes
        # temperature_unit; climate exposes neither, so fall back to the HA
        # system unit. Tolerant of "°F"/"°C"/"F"/"C"; default to Fahrenheit
        # since the 8800 ships in F in North America.
        unit = attrs.get("unit_of_measurement") or attrs.get("temperature_unit") or ""
        if not unit:
            unit = self.hass.config.units.temperature_unit
        scale = "C" if "C" in unit and "F" not in unit else "F"
        return self._clamp_outdoor(round(value), scale)

    @staticmethod
    def _clamp_outdoor(value: int, scale: str) -> tuple[int, str]:
        """Clamp a temperature value to the protocol's documented range."""
        if scale == "C":
            return max(-40, min(55, value)), "C"
        return max(-40, min(130, value)), "F"

    # ---------- TX helpers ----------

    async def _async_send(self, addr: int | None, cmd: str, value: str | None) -> None:
        await self.hass.async_add_executor_job(self.protocol.send, addr, cmd, value, False, True)

    async def _async_query(self, addr: int | None, cmd: str) -> None:
        await self.hass.async_add_executor_job(self.protocol.send, addr, cmd, None, True, True)

    # ---------- public write API for platforms ----------

    async def async_set_mode(self, addr: int, mode: str) -> None:
        """Set the system mode of the given node."""
        await self._async_send(addr, "M", mode)

    async def async_set_fan(self, addr: int, fan: str) -> None:
        """Set the fan mode of the given node."""
        await self._async_send(addr, "F", fan)

    async def async_set_heat_setpoint(self, addr: int, value: float) -> None:
        """Set the heat setpoint of the given node."""
        await self._async_send(addr, "SH", _format_setpoint(value))

    async def async_set_cool_setpoint(self, addr: int, value: float) -> None:
        """Set the cool setpoint of the given node."""
        await self._async_send(addr, "SC", _format_setpoint(value))

    async def async_set_humid_setpoint(self, addr: int, percent: int) -> None:
        """Set the humidification setpoint of the given node."""
        await self._async_send(addr, "SHUM", f"{percent}")

    async def async_set_dehum_setpoint(self, addr: int, percent: int) -> None:
        """Set the dehumidification setpoint of the given node."""
        await self._async_send(addr, "SDEH", f"{percent}")

    async def async_clear_hold(self, addr: int) -> None:
        """Clear any active schedule hold on the given node."""
        await self._async_send(addr, "HOLDSTAT", "NONE")

    async def async_set_permanent_hold(self, addr: int, on: bool) -> None:
        """Enable or disable a permanent schedule hold on the given node."""
        await self._async_send(addr, "PERMHOLD", "ON" if on else "OFF")

    async def async_clear_alarm(self, addr: int, alarm: str) -> None:
        """Clear an alarm on the given node. ``alarm`` is one of FLT, WP, DEH, SYS."""
        await self._async_send(addr, f"{alarm}ALM", "OFF")

    async def async_set_alarm_period(self, addr: int, alarm: str, value: str) -> None:
        """Set a maintenance alarm period (reminder interval).

        ``alarm`` is one of FLT, WP, DEH, SYS; ``value`` is the wire string
        "OFF" or a month count (filter: 1/3/6/12; others: 1-12).
        """
        await self._async_send(addr, f"{alarm}ALMP", value)

    # ---------- messaging ----------
    #
    # The 8800 has five distinct display slots:
    #   TMPMES         - one transient message, RAM-backed, cleared at power loss
    #                    or by writing an empty value
    #   PMES1..PMES4   - four permanent messages, EEPROM-backed, persist across
    #                    power cycles; the device UI selects which one is shown
    #
    # The text format is the same in all five cases. We funnel everything
    # through async_set_display_message / async_clear_display_message and
    # keep the formatting (length cap, ASCII normalisation, CR stripping)
    # in one place so the wire payload is always well-formed.

    async def async_set_display_message(self, addr: int, slot: str, text: str) -> None:
        """Set one of the five display slots on the given node.

        Args:
            addr: Node address.
            slot: One of ``TMPMES``, ``PMES1``, ``PMES2``, ``PMES3``, ``PMES4``.
            text: Message text. Will be normalised before transmission.

        Raises:
            ValueError: If ``slot`` is not a recognised display slot.
        """
        if slot not in VALID_MESSAGE_SLOTS:
            raise ValueError(f"slot must be one of {VALID_MESSAGE_SLOTS}, got {slot!r}")
        payload = format_message_text(text)
        await self._async_send(addr, slot, payload)
        # Shadow the formatted value so text entities reflect what the wire
        # actually received, not the raw user input. TMPMES is not shadowed -
        # it is volatile (cleared on thermostat power loss) and so the
        # shadow would silently drift out of sync.
        if slot.startswith("PMES"):
            node = self.nodes.get(addr)
            if node is not None:
                node.permanent_messages[int(slot[4:])] = payload
                async_dispatcher_send(self.hass, SIGNAL_NODE_UPDATED.format(address=addr))

    async def async_clear_display_message(self, addr: int, slot: str) -> None:
        """Clear one of the five display slots on the given node.

        Sends an empty value, which the device interprets as "no message".

        Raises:
            ValueError: If ``slot`` is not a recognised display slot.
        """
        if slot not in VALID_MESSAGE_SLOTS:
            raise ValueError(f"slot must be one of {VALID_MESSAGE_SLOTS}, got {slot!r}")
        await self._async_send(addr, slot, "")
        if slot.startswith("PMES"):
            node = self.nodes.get(addr)
            if node is not None:
                node.permanent_messages[int(slot[4:])] = ""
                async_dispatcher_send(self.hass, SIGNAL_NODE_UPDATED.format(address=addr))

    # ---------- inbound dispatch ----------

    def _on_message_from_thread(self, msg: NodeMessage) -> None:
        """Bounce a message received on the RX thread onto the HA loop."""
        self.hass.loop.call_soon_threadsafe(self._handle_message, msg)

    @callback
    def _handle_message(self, msg: NodeMessage) -> None:
        node = self.nodes.get(msg.address)
        new_node = node is None
        if node is None:
            node = NodeState(address=msg.address)
            self.nodes[msg.address] = node
        if msg.name and not node.name:
            node.name = msg.name

        cmd = msg.command
        val = msg.value or ""

        try:
            self._apply_to_state(node, cmd, val)
        except Exception:
            _LOGGER.exception("Failed applying %s=%s to node %d", cmd, val, msg.address)

        if new_node:
            async_dispatcher_send(self.hass, SIGNAL_NODE_DISCOVERED, msg.address)
        async_dispatcher_send(self.hass, SIGNAL_NODE_UPDATED.format(address=msg.address))
        # RSM responses re-define which per-module sensor entities should
        # exist on a node. Fire a dedicated signal so the sensor platform
        # can create entities for newly-discovered sensors without
        # listening to the firehose of routine value updates.
        if cmd == "RSM":
            async_dispatcher_send(
                self.hass,
                SIGNAL_NODE_SUPPORT_MODULES.format(address=msg.address),
            )

    @staticmethod
    def _apply_to_state(node: NodeState, cmd: str, val: str) -> None:
        # Sensor / control values
        if cmd in ("T", "TEMP"):
            t, s = decode_temperature(val)
            node.temperature = t
            node.temperature_scale = s
        elif cmd == "HUM":
            node.humidity = decode_humidity(val)
        elif cmd in ("OT", "R"):
            t, s = decode_temperature(val)
            node.outdoor_temperature = t
            node.outdoor_temperature_scale = s
            # Per manual p.34, a node without its own outdoor sensor
            # responds to a direct OT query with "--". A real numeric
            # response (or scale-only "--F" / "--C") means the node has a
            # sensor. We only set the flag on the first definitive
            # response; an "--" with scale set by decode_temperature also
            # indicates "no sensor". We can't distinguish "sensor
            # disconnected" from "no sensor ever installed" - both look
            # the same on the wire - so the flag is best-effort.
            if t is not None:
                node.has_own_outdoor_sensor = True
            elif node.has_own_outdoor_sensor is None:
                node.has_own_outdoor_sensor = False
        elif cmd == "OH":
            node.outdoor_humidity = decode_humidity(val)
        elif cmd == "BIHUM":
            node.builtin_humidity = decode_humidity(val)
        elif cmd == "RTS":
            t, _ = decode_temperature(val)
            node.remote_temperature = t

        # Setpoints
        elif cmd == "SH":
            t, _ = decode_temperature(val)
            node.setpoint_heat = t
        elif cmd == "SC":
            t, _ = decode_temperature(val)
            node.setpoint_cool = t
        elif cmd == "DBAND":
            # Response is "[value][scale]" e.g. "3F"; we keep the magnitude.
            t, _ = decode_temperature(val)
            if t is not None:
                node.deadband = int(t)
        elif cmd == "SHUM":
            node.setpoint_humid = decode_humidity(val)
        elif cmd == "SDEH":
            node.setpoint_dehum = decode_humidity(val)

        # Modes
        elif cmd in ("M", "MODE"):
            node.mode = val.strip().upper()
        elif cmd in ("F", "FAN"):
            node.fan_mode = val.strip().upper()

        # Hold / override
        elif cmd == "HOLDSTAT":
            node.hold_status = val.strip().upper()
        elif cmd == "HOLD":
            node.network_override = val.strip().upper()

        # Relays
        elif cmd in ("HVAC", "H"):
            node.relays = decode_hvac(val)

        # Alarms
        elif cmd == "FLTALM":
            node.alarm_filter = val.strip().upper() == "ON"
        elif cmd == "WPALM":
            node.alarm_water_panel = val.strip().upper() == "ON"
        elif cmd == "DEHALM":
            node.alarm_dehumidifier = val.strip().upper() == "ON"
        elif cmd == "SYSALM":
            node.alarm_system = val.strip().upper() == "ON"

        # Alarm periods (reminder intervals). cmd is "<short>ALMP".
        elif cmd in ("FLTALMP", "WPALMP", "DEHALMP", "SYSALMP"):
            node.alarm_periods[cmd[:-4]] = val.strip().upper()

        # Errors
        elif cmd == "ERROR":
            node.errors = decode_errors(val)

        # Recovery
        elif cmd == "RECOVSTAT":
            node.progressive_recovery = val.strip().upper() == "ON"

        # Controller type
        elif cmd == "CT":
            with contextlib.suppress(ValueError):
                node.controller_type = int(val.strip())

        elif cmd == "NAME":
            node.name = val.strip() or node.name

        # Identity / firmware (from ID query).
        elif cmd == "ID":
            node.model_info = val.strip() or node.model_info

        # Support module topology.
        elif cmd == "RSM":
            new_topology = parse_rsm(val)
            node.support_modules = new_topology
            # Drop cached readings for sensors that no longer exist (e.g.
            # if a module was unplugged between probes).
            node.support_module_readings = {
                key: reading
                for key, reading in node.support_module_readings.items()
                if key in new_topology
            }

        # Per-support-module sensor readings (R[x]S[y]=...). The command
        # name itself carries the module/sensor index, so we parse it
        # rather than maintaining a separate elif for every R1S1..R4S2.
        elif (rxsy := parse_rxsy_command(cmd)) is not None:
            module, sensor = rxsy
            # Type determines how we decode: temperature or humidity.
            sensor_type = node.support_modules.get((module, sensor))
            stripped = val.strip()
            if sensor_type in ("CT", "RT") or stripped.endswith(("F", "C")):
                # Temperature sensor. decode_temperature handles "--F" too.
                temp, scale = decode_temperature(stripped)
                node.support_module_readings[(module, sensor)] = (temp, scale)
            elif sensor_type in ("CH", "RH") or "%" in stripped or "--" in stripped:
                # Humidity sensor; decode_humidity returns None for "--".
                hum = decode_humidity(stripped)
                node.support_module_readings[(module, sensor)] = (hum, "%")

        # Anything else: ignored. Add more handlers as needed.


def _format_setpoint(value: float) -> str:
    """Format a setpoint as an integer ASCII number.

    The 8800 only accepts integer setpoints in its native scale. We round
    half-up before sending. The caller is responsible for any scale
    conversion (we don't second-guess SCALE here).
    """
    return f"{round(value)}"


# Messaging helpers. These live at module level (rather than as methods on
# the coordinator) so they can be unit-tested without Home Assistant in the
# import path.

#: Maximum characters in a display message. The 8800 LCD is a small fixed
#: dot-matrix; longer text is truncated rather than rejected so an automation
#: that pastes a long status string degrades gracefully.
MESSAGE_MAX_LENGTH = 32

#: The five writable display slots, in the form the wire protocol expects.
VALID_MESSAGE_SLOTS: tuple[str, ...] = ("TMPMES", "PMES1", "PMES2", "PMES3", "PMES4")


def format_message_text(text: str | None) -> str:
    """Normalise a message string for transmission to the LCD.

    Drops characters the wire protocol or the display cannot represent:

    - Non-ASCII (the protocol is 8-bit ASCII).
    - CR and LF (CR is the command terminator; LF makes no sense on a
      single-line display). Both are replaced with a single space so that
      multi-line input doesn't collapse into nothing.
    - Everything past MESSAGE_MAX_LENGTH characters.

    A None input maps to an empty string, which clears the slot on the wire.
    """
    if text is None:
        return ""
    text = text.encode("ascii", errors="ignore").decode("ascii")
    text = text.replace("\r", " ").replace("\n", " ")
    return text[:MESSAGE_MAX_LENGTH]
