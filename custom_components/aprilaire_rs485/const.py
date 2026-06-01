# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Constants for the aprilaire_rs485 integration."""

from __future__ import annotations

DOMAIN = "aprilaire_rs485"

MANUFACTURER = "Aprilaire"
MODEL = "8800"

CONF_ADDRESSES = "addresses"  # Optional explicit address list.
CONF_BAUD = "baud"
CONF_DISCOVER = "discover"
CONF_MAX_ADDRESS = "max_address"
CONF_PORT = "port"  # Holds a pyserial URL or device path.
# Outdoor temperature push. If CONF_OUTDOOR_TEMP_SOURCE is non-empty it must
# be the entity_id of an HA temperature sensor; its value is broadcast to the
# bus on a fixed cadence. If empty and CONF_OUTDOOR_TEMP_REBROADCAST is true,
# the integration picks the lowest-addressed thermostat with its own outdoor
# sensor and rebroadcasts that value to peers that lack one. The 8800
# protocol gives sensor-equipped nodes priority - they ignore the assignment
# - so broadcasting globally is always safe.
CONF_OUTDOOR_TEMP_SOURCE = "outdoor_temp_source"
CONF_OUTDOOR_TEMP_REBROADCAST = "outdoor_temp_rebroadcast"
# When true, push HA's local wall-clock time/date to the bus periodically and
# leave the thermostat's DLS (DST) setting alone.
CONF_CLOCK_SYNC = "clock_sync"

DEFAULT_BAUD = 9600
DEFAULT_MAX_ADDRESS = 8  # See README for why this should match your real node count.
DEFAULT_REFRESH_INTERVAL_S = 60
DEFAULT_CLOCK_SYNC = True
# Manual p.34: thermostats discard a pushed OT value after a 10-minute
# validity window. Broadcast at half that, comfortably ahead of expiry.
OUTDOOR_TEMP_BROADCAST_INTERVAL_S = 300
# Clock push cadence. The device RTC drifts ~1 min/30 days, so this is mostly
# about realigning across DST transitions and recovering within one interval
# after the clock is lost (backup batteries depleted or removed; ordinary
# system power loss is held by the batteries).
CLOCK_SYNC_INTERVAL_S = 3600

# COS bits we enable so the thermostat pushes us updates instead of us polling.
# These reset to defaults on every thermostat power-cycle (manual p.17), so the
# coordinator re-applies them on every periodic refresh.
COS_ENABLE = {
    "C1": "ON",  # Relay output state.
    "C2": "ON",  # Controlling temperature / humidity.
    "C3": "ON",  # Outdoor temperature / remote humidity.
    "C5": "ON",  # Setpoints.
    "C6": "ON",  # Network override.
    "C7": "ON",  # System mode.
    "C8": "ON",  # Fan mode.
    "C13": "ON",  # Time/date/program format setup changes.
    "C14": "ON",  # Alarms.
    "C15": "ON",  # Progressive recovery.
    "C16": "ON",  # Schedule changes.
    "C17": "ON",  # Hold status.
    "C19": "ON",  # Errors.
}

# Controller type code returned by the CT command.
CT_HUMIDISTAT = 1
CT_THERMOSTAT = 0

# System modes (verbose form; the node always returns the verbose form per
# manual p.36 regardless of which form was sent).
MODE_AUTO = "AUTO"
MODE_COOL = "COOL"
MODE_DEHUM = "DEHUM"
MODE_EMHT = "EMHT"
MODE_HEAT = "HEAT"
MODE_HUMID = "HUMID"
MODE_OFF = "OFF"

FAN_AUTO = "AUTO"
FAN_CIRC = "CIRC"
FAN_ON = "ON"

# Setpoint ranges accepted by the device, used to bound the climate entity's
# controls per mode (manual pp.38, 40 for SH/SC). The device silently ignores
# out-of-range writes, so these are a UX nicety, not a safety boundary.
SETPOINT_HEAT_MIN_F = 40
SETPOINT_HEAT_MAX_F = 90
SETPOINT_COOL_MIN_F = 42
SETPOINT_COOL_MAX_F = 99
SETPOINT_HEAT_MIN_C = 4
SETPOINT_HEAT_MAX_C = 32
SETPOINT_COOL_MIN_C = 6
SETPOINT_COOL_MAX_C = 37

# Dispatcher signal templates.
SIGNAL_NODE_DISCOVERED = "aprilaire_rs485_node_discovered"
SIGNAL_NODE_UPDATED = "aprilaire_rs485_node_updated_{address}"

# Fired after an RSM response is processed for a node, signalling that the
# node's support module topology is now known (or has changed). Distinct
# from SIGNAL_NODE_UPDATED to avoid waking the support-module entity
# discovery logic on every routine value update.
SIGNAL_NODE_SUPPORT_MODULES = "aprilaire_rs485_support_modules_{address}"

# Dispatched whenever any of the six bus error counters increments, so the
# diagnostic error-counter sensors refresh immediately. One shared signal for
# all of them keeps each refresh cheap (a single attribute read) and avoids a
# per-counter signal proliferation. The activity counters do not use this -
# they increment too often, so they ride HA's normal poll cadence instead.
SIGNAL_BUS_ERRORS_UPDATED = "aprilaire_rs485_bus_errors_updated"

# HA event types fired alongside each error-counter increment, so automations
# and the logbook can react without polling. Payload schemas:
#   parse_error               -> {"detail": str, "raw": str | None}
#   transport_error           -> {"detail": str}
#   apply_error               -> {"address": int, "command": str,
#                                 "value": str, "detail": str}
#   unknown_command           -> {"address": int, "command": str, "value": str}
#   write_verification_failed -> {"address": int, "command": str,
#                                 "expected": str, "actual": str, "detail": str}
#   query_timeout             -> {"address": int, "deadline_seconds": float,
#                                 "last_seen_seconds_ago": float}
#   query_recovered           -> {"address": int, "unresponsive_seconds": float}
# query_timeout fires once when a node transitions to unresponsive (not per
# query); query_recovered fires when it answers again.
EVENT_PARSE_ERROR = "aprilaire_rs485_parse_error"
EVENT_TRANSPORT_ERROR = "aprilaire_rs485_transport_error"
EVENT_APPLY_ERROR = "aprilaire_rs485_apply_error"
EVENT_UNKNOWN_COMMAND = "aprilaire_rs485_unknown_command"
EVENT_WRITE_VERIFICATION_FAILED = "aprilaire_rs485_write_verification_failed"
EVENT_QUERY_TIMEOUT = "aprilaire_rs485_query_timeout"
EVENT_QUERY_RECOVERED = "aprilaire_rs485_query_recovered"

# How long after a verifiable write to check the device converged to the value
# we wrote. Covers slot scheduling plus COS delivery latency, and is short
# enough that a user physically changing the same setting within the window is
# rare.
VERIFICATION_DELAY_S = 5.0
# How long after a per-node query to expect any message back from that node
# before counting it a timeout. Roughly 8x the 9600-baud slot width (262 ms).
QUERY_RESPONSE_TIMEOUT_S = 2.0
