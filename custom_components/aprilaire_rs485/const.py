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

DEFAULT_BAUD = 9600
DEFAULT_MAX_ADDRESS = 8  # See README for why this should match your real node count.
DEFAULT_REFRESH_INTERVAL_S = 60
# Manual p.34: thermostats discard a pushed OT value after a 10-minute
# validity window. Broadcast at half that, comfortably ahead of expiry.
OUTDOOR_TEMP_BROADCAST_INTERVAL_S = 300

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

# Dispatcher signal templates.
SIGNAL_NODE_DISCOVERED = "aprilaire_rs485_node_discovered"
SIGNAL_NODE_UPDATED = "aprilaire_rs485_node_updated_{address}"

# Fired after an RSM response is processed for a node, signalling that the
# node's support module topology is now known (or has changed). Distinct
# from SIGNAL_NODE_UPDATED to avoid waking the support-module entity
# discovery logic on every routine value update.
SIGNAL_NODE_SUPPORT_MODULES = "aprilaire_rs485_support_modules_{address}"
