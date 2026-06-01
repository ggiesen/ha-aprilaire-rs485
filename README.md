# Aprilaire 8800 RS-485 - Home Assistant integration

A Home Assistant custom integration for the 2011-era **Aprilaire Model 8800
Communicating Thermostat** (and the same hardware configured as a humidistat).
It talks to the thermostats over their RS-485 ASCII protocol, documented in the
8800 Programmer's Manual (RPC P/N 10009414), either through a local serial port
or a TCP-to-RS-485 gateway.

> This is **not** for the newer Wi-Fi-connected Aprilaire models (8810, 8910W,
> 8920W, and the Wi-Fi 8800-series). Those use a different protocol and are
> handled by the `aprilaire` integration built into Home Assistant.

## Features

- **Climate** entity per thermostat: current temperature, mode (off/heat/cool/
  auto), fan mode (auto/on/circulate), heat and cool setpoints, and an inferred
  HVAC action. Setpoint ranges and step follow the device.
- **Humidifier / dehumidifier** entities per humidistat, with the humidify and
  dehumidify directions composing onto the device's single mode.
- **Sensors**: indoor / outdoor / remote temperature, indoor / outdoor /
  built-in humidity, firmware version, schedule hold status, and one diagnostic
  sensor per connected support-module sensor.
- **Maintenance alarms** as problem binary sensors (air filter, water panel,
  dehumidifier, HVAC system), each with a **button** to clear it and a
  **select** to set its reminder interval.
- **Diagnostics**: per-node sensor and communication error flags, progressive
  recovery and network-hold status, plus a bus-level connection sensor, node
  count, and discovered-address list. The bus device also exposes ten
  reliability counters (parse / transport / apply / unknown-command errors,
  write-verification failures, query timeouts, and activity totals) and fires
  matching events, so bus trouble and rejected writes are visible rather than
  silent. See [Diagnostics](#diagnostics-bus-counters-and-events).
- **Display messaging**: the thermostat's message center exposed as text
  entities and services (temporary and four permanent message slots).
- **Outdoor temperature sharing**: push a Home Assistant temperature to
  thermostats that lack their own outdoor sensor, or rebroadcast from one that
  has one.
- **Clock sync**: keep the thermostat clocks set from Home Assistant's local
  time (optional, on by default).

## Tested hardware

This integration has been used with:

- Aprilaire Model 8800 thermostats and humidistats
- Aprilaire 8811 Protocol Adapter (RS-232 to 4-wire RS-485/422)
- [Waveshare Industrial Isolated USB to 4-Ch RS232 Converter](https://www.waveshare.com/usb-to-4ch-rs232.htm?sku=26854)
  (the USB-to-RS-232 adapter feeding the 8811)

Other pyserial-compatible USB-to-RS-232 adapters and 4-wire RS-485/422 TCP
gateways should work, but have not been tested directly. See
[Limitations and untested paths](#limitations-and-untested-paths).

## Connecting to the bus

The 8800 bus is **4-wire full-duplex RS-485/422** (separate transmit and
receive pairs). There are two supported transports.

### Local serial via the 8811 Protocol Adapter (recommended)

```
Home Assistant host
  USB-to-RS-232 adapter
    Aprilaire 8811 Protocol Adapter (powered by its 9 V supply)
      CAT-5 to the 8818 distribution panel (for multiple thermostats)
        thermostat(s) at addresses 1-N
```

The 8811 is a transparent RS-232 to RS-485/422 converter and is already a
4-wire device, so the 4-wire requirement is satisfied automatically. Set the
baud rate on the thermostats; the 8811 follows the RS-232 side.

Port values for this transport (entered in the config flow):

- `/dev/ttyUSB0` - Linux USB-serial adapter
- `/dev/ttyAMA0` - Raspberry Pi UART
- `COM3` - Windows
- `hwgrep://USB-RS232` - match by adapter description (stable across reboots if
  the adapter has a fixed descriptor)

### TCP-to-RS-485 gateway

A network gateway works only if it supports **4-wire RS-422**. Many low-cost
RS-485 gateways are 2-wire half-duplex and will not work with this protocol.

Gateways with 4-wire RS-422 support include the USR-TCP232-410S, the Moxa NPort
5130A/5150 (switchable to 4-wire), and some Lantronix EDS models (check the
spec sheet). On the gateway, set 4-wire (RS-422) mode, the matching baud rate,
8N1, the lowest available packet/forwarding timeout (10-20 ms; a 250 ms default
makes the integration feel sluggish), and raw TCP mode unless you specifically
want RFC 2217.

Port values for this transport:

- `socket://192.168.1.50:8000` - raw TCP
- `rfc2217://192.168.1.50:2217` - RFC 2217

The integration sets `TCP_NODELAY` and `SO_KEEPALIVE` and reconnects
automatically after a drop, with a 5-second backoff.

## Installation

Requires Home Assistant 2026.3 or newer.

### HACS

1. In HACS, open **Integrations -> three-dot menu -> Custom repositories**.
2. Add `https://github.com/ggiesen/ha-aprilaire-rs485` with category
   **Integration**.
3. Search for "Aprilaire 8800 (RS-485)" and install it.
4. Restart Home Assistant.
5. Go to **Settings -> Devices & services -> Add Integration** and add
   **Aprilaire 8800 (RS-485)**.

### Manual

Copy the `custom_components/aprilaire_rs485/` directory from this repository
into your Home Assistant `config/custom_components/` directory (so you end up
with `config/custom_components/aprilaire_rs485/`), restart Home Assistant, then
add the integration from **Settings -> Devices & services**.

## Configuration

The integration is configured entirely through the UI. The setup dialog asks
for:

- **Port** - the serial path or gateway URL from
  [Connecting to the bus](#connecting-to-the-bus).
- **Baud rate** - 9600 (the device default) or 19200. Must match the
  thermostats. To change it, change the thermostats first; the bus must be at a
  single consistent rate.
- **Maximum node address** - set this to the number of thermostats you actually
  have, and set the matching number-of-thermostats value on each thermostat.
  Leaving it higher than your real count slows the whole bus down.
- **Explicit address list** (optional) - a comma-separated list of addresses to
  use instead of auto-discovery.
- **Outdoor temperature source** (optional) and **Rebroadcast** - see
  [Outdoor temperature sharing](#outdoor-temperature-sharing).
- **Sync thermostat clock** (on by default) - see [Clock sync](#clock-sync).

The outdoor-temperature settings, rebroadcast, and clock-sync toggle can be
changed later from the integration's **Configure** button.

## Entities

Each setup creates one "Aprilaire 8800 bus" device for bus-wide diagnostics,
plus one device per discovered thermostat or humidistat with its own entities.

### Bus device

| Entity | Type | Notes |
|---|---|---|
| Bus connection | binary_sensor | On when the transport is open |
| Node count | sensor | Diagnostic; number of known nodes |
| Discovered addresses | sensor | Diagnostic; comma-separated address list |
| Parse / transport / apply / unknown-command errors | sensor | Diagnostic counters; see [Diagnostics](#diagnostics-bus-counters-and-events) |
| Verification failures, query timeouts | sensor | Diagnostic counters; reliability signals |
| Messages sent / received, verifications attempted, queries sent | sensor | Diagnostic activity totals (denominators for rates) |

### Per-node entities

| Entity | When created | Notes |
|---|---|---|
| Climate | Thermostat nodes | Temperature, mode, fan, setpoints, inferred HVAC action; `auto_deadband` attribute |
| Humidifier, Dehumidifier | Humidistat nodes | Two entities per node (see [Humidistats](#humidistats)) |
| Indoor / outdoor / remote temperature | Per node | Scale follows the device |
| Indoor / outdoor / built-in humidity | Per node | Percent |
| Schedule hold status | Per node | Diagnostic (none/temporary/permanent/vacation) |
| Firmware | Per node | Diagnostic |
| Air filter / water panel / dehumidifier / HVAC system alarm | Per node | Problem class |
| Clear ... alarm (one per alarm) | Per node | Button; clears the alarm |
| ... alarm interval (one per alarm) | Per node | Select; reminder interval (off / months) |
| Sensor or comm error | Per node | Problem class; true if any error flag is set |
| Built-in / remote / outdoor temperature, built-in humidity, communication, EEPROM error | Per node | Problem class, diagnostic; individual error flags |
| Network hold active | Per node | Diagnostic |
| Progressive recovery | Per node | Running class |
| Permanent message 1-4 | Per node | Text; see [Messaging](#messaging) |
| Module N sensor S | Per support-module sensor | Diagnostic; see [Support-module sensors](#support-module-sensors) |

### Humidistats

A humidistat node (an 8800 configured as a humidity controller) gets a
**Humidifier** and a **Dehumidifier** entity. The device has a single mode
(off / humidify / dehumidify / both), and the two entities compose onto it:
turning one on while the other is on selects the "both" mode; turning one off
while in "both" leaves the other running. This matches how the Wi-Fi
`aprilaire` integration models humidity.

The RS-485 protocol does not report which directions a humidistat actually has
wired, so both entities are always created. If your unit only humidifies, the
dehumidifier entity simply stays off.

### Support-module sensors

A node can have up to four addressable support modules, each with two sensors
(sensor 1 temperature; sensor 2 temperature or humidity). The integration reads
the module layout from the `RSM` query and creates one diagnostic sensor per
connected sensor, named "Module N sensor S", with the appropriate temperature
or humidity device class.

Two positions are not duplicated as their own entities: module 1 / sensor 1
configured as a remote temperature is surfaced as **Outdoor temperature**, and
module 1 / sensor 2 configured as remote humidity is surfaced as **Outdoor
humidity**. Remote sensors in any other position get their own entity.

Support-module readings are polled on each refresh (they have no change-of-state
push). The added bus traffic is small even on a fully populated bus.

## Diagnostics: bus counters and events

This protocol has no acknowledgements: a malformed or unaddressed command is
silently dropped, so on the wire a write either works or it doesn't, with no
telemetry. To make bus health visible, the bus device exposes ten diagnostic
counter sensors (all `total_increasing`, all under the diagnostic category).
They reset to zero on integration restart by design; Home Assistant's long-term
statistics treat a `total_increasing` reset correctly.

Six count problems, and update immediately:

| Counter | Counts | Points at |
|---|---|---|
| Parse errors | A complete line arrived but couldn't be parsed | Electrical noise, mis-termination, bus wiring |
| Transport errors | Opening, reading, or writing the serial/TCP transport failed | USB cable, TCP gateway, serial port config |
| Apply errors | A message parsed but updating state raised | Integration bug or an unexpected response format |
| Unknown commands | A response arrived whose command code isn't in the 8800 command set | Off-protocol traffic, or a firmware variant with a command not in the Programmer's Manual |
| Verification failures | After a write, the device value didn't converge to what we wrote within five seconds | Write lost, filtered by hold/lockout, or rejected out of range |
| Query timeouts | A node went unresponsive (a per-node query got no reply within two seconds, and the node wasn't already flagged unresponsive) | Node offline, interference, or a slot collision. Counts unresponsive *episodes*, not individual queries |

Four are activity totals, useful as denominators for rate calculations. They
increment far more often, so they refresh on Home Assistant's normal poll cycle
rather than per-increment: Messages sent, Messages received, Verifications
attempted, Queries sent.

Separating parse from transport errors matters: "100 parse errors, 0 transport
errors" implicates the bus wiring, while "0 parse, 100 transport" implicates the
USB cable or TCP gateway.

### Diagnostic events

Each error fires a Home Assistant event so automations and the logbook can
react without polling a counter. A node going unresponsive fires
`query_timeout` once, and answering again fires `query_recovered`:

| Event | Payload |
|---|---|
| `aprilaire_rs485_parse_error` | `detail`, `raw` (the offending line) |
| `aprilaire_rs485_transport_error` | `detail` |
| `aprilaire_rs485_apply_error` | `address`, `command`, `value`, `detail` |
| `aprilaire_rs485_unknown_command` | `address`, `command`, `value` |
| `aprilaire_rs485_write_verification_failed` | `address`, `command`, `expected`, `actual`, `detail` |
| `aprilaire_rs485_query_timeout` | `address`, `deadline_seconds`, `last_seen_seconds_ago` |
| `aprilaire_rs485_query_recovered` | `address`, `unresponsive_seconds` |

The `raw` field on a parse error is the exact line off the wire, handy for a bug
report. The `expected` / `actual` on a verification failure show what the device
held instead of what was written.

### Deriving rates

The raw counters pair with built-in Home Assistant helpers. Parse-error rate as
a percentage of received messages:

```yaml
template:
  - sensor:
      - name: Aprilaire parse error rate
        unit_of_measurement: "%"
        state: >-
          {% set e = states('sensor.aprilaire_8800_bus_parse_errors') | float(0) %}
          {% set t = states('sensor.aprilaire_8800_bus_messages_received') | float(0) %}
          {{ (e / t * 100) | round(3) if t > 0 else 0 }}
```

Per-minute throughput with the `derivative` helper:

```yaml
sensor:
  - platform: derivative
    source: sensor.aprilaire_8800_bus_messages_received
    name: Aprilaire messages per minute
    unit_time: min
```

Alert when a write didn't take:

```yaml
automation:
  - alias: Aprilaire write was rejected
    triggers:
      - trigger: event
        event_type: aprilaire_rs485_write_verification_failed
    actions:
      - action: notify.mobile_app_your_phone
        data:
          message: >-
            Node {{ trigger.event.data.address }} {{ trigger.event.data.command }}:
            wrote {{ trigger.event.data.expected }}, device held
            {{ trigger.event.data.actual }}.
```

### Expected false positives

A couple of cases count as failures without being broken hardware. They're worth
knowing before you wire an alert to them:

- A verification failure fires if someone physically changes the same setting on
  the thermostat within the five-second window, or if a hold/lockout silently
  rejects the write. The hold case is arguably a feature: it surfaces a write
  that was refused.
- A query timeout counts an unresponsive *episode*, not each unanswered query:
  one increment and one `query_timeout` event when a node stops answering, and
  a `query_recovered` event when it answers again. An intermittently-slow node
  therefore shows up as a handful of episodes rather than dozens of per-query
  timeouts. A marginal bus can still log the odd episode; calibrate alerts
  against your own baseline rather than the first nonzero reading.

## Outdoor temperature sharing

The protocol lets the host broadcast an outdoor temperature to the bus.
Thermostats that have their own outdoor sensor ignore it; those without one
display the broadcast value (which expires after 10 minutes if not refreshed).

- **Outdoor temperature source** - pick a temperature `sensor`, a `weather`
  entity, or a `climate` entity. The value is read every 5 minutes (from the
  sensor state, the weather entity's `temperature` attribute, or the climate
  entity's `current_temperature` attribute) and broadcast as whole degrees.
- **Rebroadcast** (on by default) - when no source is set, the integration
  takes the reading from the lowest-addressed thermostat that has its own
  outdoor sensor and shares it with the others. Turn this off to broadcast
  nothing.

Outdoor humidity cannot be shared this way; the protocol marks it read-only, so
a thermostat only gets outdoor humidity from its own support-module sensor.

## Clock sync

The 8800 keeps its own clock, which drifts slowly. Backup batteries hold the
time through ordinary system power loss; the clock is only lost when those
batteries are depleted or removed. With clock sync enabled (the default), the
integration pushes Home Assistant's local date and time to the bus at startup
and hourly. It does **not** change the thermostat's daylight-saving setting -
the thermostat keeps handling DST, and the periodic push keeps the displayed
time correct across transitions and restores it after the clock has been lost
(depleted or replaced batteries). Turn it off if you would rather manage the
clock at the thermostat.

## Messaging

The 8800 message center has one temporary slot (cleared on power loss) and four
permanent slots (stored in EEPROM). The four permanent slots are exposed as
`text` entities ("Permanent message 1" through "4"); which one is displayed is
chosen at the thermostat. Because the permanent slots are write-only on the
wire, the text entities shadow the last value written and restore it across
Home Assistant restarts.

All five slots are also writable through services, which accept the standard
target fields (entity, device, area, or label):

| Service | Fields | Purpose |
|---|---|---|
| `aprilaire_rs485.send_temporary_message` | `message` | Show a message until cleared |
| `aprilaire_rs485.clear_temporary_message` | (target only) | Clear the temporary message |
| `aprilaire_rs485.set_permanent_message` | `slot` (1-4), `message` | Write a permanent slot |
| `aprilaire_rs485.clear_permanent_message` | `slot` (1-4) | Clear a permanent slot |

Message text is normalized for the wire: non-ASCII characters are dropped, line
breaks become spaces, and length is capped at 32 characters. Targeting only the
bus device is rejected; targeting the bus alongside real thermostats skips the
bus.

### Example automation

```yaml
- alias: "Aprilaire filter alert"
  trigger:
    - platform: state
      entity_id: binary_sensor.kitchen_air_filter_alarm
      to: "on"
  action:
    - action: aprilaire_rs485.send_temporary_message
      target:
        device_id: 4f3b2a...  # the kitchen thermostat
      data:
        message: "REPLACE FILTER"
```

## Limitations and untested paths

**Not available on this hardware** (the Wi-Fi `aprilaire` integration has these,
but the 2011 RS-485 8800 protocol does not): air cleaning and ventilation /
fresh-air control.

**Not implemented in this version:**

- Schedule editing (`PROGDxEy`) - the schedule still runs on the device; it
  just is not editable from Home Assistant.
- Schedule holds as climate presets - hold status is exposed as a sensor and can
  be cleared, but is not surfaced as a climate `preset_mode`.
- User lockouts and PIN.
- Outdoor humidity push (read-only in the protocol).

**Implemented but not verified on hardware here.** These paths follow the
manual and the test suite but have not been exercised against real devices, so
treat them as provisional and please report issues:

- Support modules and their sensors (`RSM` / `RxSy`)
- The direct-wired remote temperature sensor (`RTS`) and built-in humidity
  (`BIHUM`) readings
- Outdoor-sensor detection and the outdoor-temperature broadcast/rebroadcast
- More than two thermostats on one bus
- 19200 baud
- TCP-to-RS-485 gateways (only local serial via the 8811 has been used)
- Heat-pump and emergency-heat modes
- Model 8870 nodes. The 8800 protocol is a superset, so an 8870 is detected
  from its `ID` response and logged with a limited-support notice, but it still
  runs the full 8800 command profile. Core climate works; the 8800-only
  commands return nothing on an 8870, so those features (built-in humidity,
  remote temperature, deadband, maintenance alarms, progressive recovery) stay
  unavailable. A model-specific profile is not implemented (no 8870 to test).

## Bench testing

A standalone `cli_test.py` is included for checking wiring and addressing
without Home Assistant. Run it against the same transport you plan to use:

```bash
python cli_test.py /dev/ttyUSB0 --discover -v          # local serial
python cli_test.py socket://192.168.1.50:8000 --discover -v   # TCP gateway
python cli_test.py /dev/ttyUSB0 --addr 1 --selftest -v
python cli_test.py /dev/ttyUSB0 --addr 1 --cmd SH --value 68
```

If `--discover` finds nothing, check (roughly in order) the gateway wiring mode
(must be 4-wire), A/B pair polarity, bus termination, baud rate, and the
port/URL. A 2-wire half-duplex gateway cannot work and must be replaced.

## Wiring notes

- **Termination and biasing**: RS-485/422 wants 120 ohm termination at the ends
  of each pair and bias resistors at the host. Without termination on a long
  run, corrupted bytes appear and the affected responses are dropped.
- **Timing**: the integration does its serial I/O on a dedicated thread and
  spaces transmissions per the manual. At 19200 baud the timing windows are
  half the size, so a marginal cable that works at 9600 may not.
- **Power cycles**: the 8800 resets its change-of-state and command-response
  settings on power-up. The integration re-applies them on each refresh, so
  updates resume after a brownout.
- **No write acknowledgement**: the protocol does not acknowledge writes. The
  integration reads critical writes back to confirm them.

## Repository, development, and contributing

Development happens on GitLab (`gitlab.com/ggiesen/ha-aprilaire-rs485`); the
GitHub mirror (`github.com/ggiesen/ha-aprilaire-rs485`) is what HACS installs
from. File issues and pull requests against the GitLab repository; the GitHub
side is pull-only and is overwritten on each sync.

The test suite runs in two layers. The protocol-only tests need just `pyserial`
and `pytest`:

```bash
pip install -e ".[test]"
pytest tests/test_protocol.py tests/test_coordinator.py tests/test_outdoor_temp.py
```

The Home Assistant tests need `pytest-homeassistant-custom-component`:

```bash
pip install -e ".[test-ha]"
pytest tests/
```

Lint and format with `ruff` (`ruff check .` and `ruff format --check .`). When a
change affects user-visible behavior or configuration, update this README and
`CHANGELOG.md` under `## [Unreleased]`.

## Licence

This project is licensed under the [Mozilla Public License 2.0](LICENSE). The
8800 and 8811 manuals are the property of Research Products Corporation /
Aprilaire. The brand icon and logo under
`custom_components/aprilaire_rs485/brand/` are Aprilaire trademarks (from the
`home-assistant/brands` repository), included only to identify the supported
hardware and not covered by the MPL.
