# Aprilaire 8800 RS-485 - Home Assistant integration

Custom-component integration for the original 2011-era **Aprilaire Model 8800
Communicating Thermostat**, speaking the RS-485 ASCII protocol documented in
the 8800 Programmer's Manual (RPC P/N 10009414). Works through:

- A local serial port (typically an **Aprilaire 8811 Protocol Adapter** plus a
  USB-to-RS-232 cable), or
- A **TCP-to-RS-485 gateway** (raw TCP via `socket://` or telnet-style via
  `rfc2217://`).

> **Not** the integration for the newer Wi-Fi 8810 / 8910W / 8920W / 8800-series
> internet-connected models - those are covered by the `aprilaire` integration
> in HA core, which uses a completely different protocol.

## Read this before you install

This protocol was designed in an era when "host" meant a dedicated panel,
not a multitasking Linux box running 200 other things. Several quirks bite
back if you ignore them.

### Transport: pick one and verify it actually works

#### Path A: Local serial via the 8811 Protocol Adapter (recommended)

This is the canonical setup and the path I'd default to.

```
Home Assistant box
    +-- USB-to-RS-232 cable
           +-- Aprilaire 8811 Protocol Adapter (powered by its 9 V wall-wart)
                 +-- CAT-5 to 8818 distribution panel (multi-thermostat)
                       +-- thermostat(s) at addresses 1-N
```

The 8811 is a transparent RS-232 <-> RS-485/422 converter - bytes pass straight
through. It's already a 4-wire RS-422 device, so the "4-wire full-duplex"
warning is satisfied automatically. You set thermostat-side baud rate on the
device UI; the 8811 inherits it from the RS-232 side.

URL examples for this path:
- `/dev/ttyUSB0` (Linux, USB-serial adapter)
- `/dev/ttyAMA0` (Raspberry Pi UART)
- `COM3` (Windows)
- `hwgrep://USB-RS232` (find by description - survives reboots if your USB
  serial adapter has a stable descriptor)

The 8811 needs its 9 V DC supply connected. The bundled RS-232 cable is 3 ft;
factor that into where you mount it.

#### Path B: TCP-to-RS-485 gateway

This works, but only with the right kind of gateway. The 8800 protocol is
**4-wire full-duplex** (separate TX and RX pairs). Most consumer
TCP-to-RS-485 gateways are **2-wire half-duplex** and will not work.

Gateways known/spec'd to support 4-wire RS-422:
- **USR-TCP232-410S** - explicitly supports RS-422 4-wire
- **Moxa NPort 5130A** and **5150** - software-switchable to 4-wire RS-422
- **Lantronix EDS** family - depends on model, check spec sheet
- **Custom ESP32** with two MAX485 chips on separate pairs (Tx + Rx)

Gateways that **will not work** (do not buy these for this purpose):
- Standard USR-TCP232 models (RS-485 2-wire only)
- esp-link / esp-rfc2217 on stock ESP boards (2-wire)
- Generic Waveshare RS485-to-Ethernet (mostly 2-wire)

Gateway configuration to set correctly:
- **Wiring mode**: RS-422 (4-wire), not RS-485 (2-wire)
- **Serial baud**: match the thermostat's `BAUD` setting (default 9600)
- **Data bits / parity / stop bits**: 8N1
- **Packet idle time / forwarding timeout**: lower the better, 10-20 ms
  ideal. If left at the typical 250 ms default, HA will look sluggish.
- **TCP raw mode** (not Telnet/RFC 2217), unless you specifically want RFC 2217

URL examples for this path:
- `socket://192.168.1.50:8000` - raw TCP
- `rfc2217://192.168.1.50:2217` - RFC 2217 (gateway handles baud negotiation)

The integration sets `TCP_NODELAY` and `SO_KEEPALIVE` on the underlying socket
and reconnects automatically after a drop (with a 5-second back-off).

### Termination and biasing

RS-485/422 wants 120 ohm termination at the far ends of each pair and bias
resistors on the host side. The 8800 installation manual covers wiring.
Without termination on a long run you'll see corrupted bytes; the parser
will silently drop those lines (and you'll wonder why responses are missing).

### The host owns timing

Sub-slots are 65 ms at 9600 baud (32.768 ms at 19200). The integration uses a
dedicated thread - not HA's event loop - for I/O, and enforces minimum gaps
between transmissions per the manual. If you see truncated messages, lower
bus load (don't poll aggressively) before suspecting a bug.

### Baud rate (9600 or 19200)

The integration supports both 9600 bps (manual default) and 19200 bps. Set
the value in the config flow to match whatever the thermostats are configured
for. To change the rate, do it on the thermostats themselves through their
UI (or via `SN# BAUD=` on the bus if you know what you're doing); the bus
must be at one consistent rate. Changing the integration's rate without
changing the thermostats first will make the bus look dead.

At 19200 the protocol timing windows are half the size, so a marginal cable
run or noisy ground that worked at 9600 may not work at 19200. Default to
9600 unless you have a specific reason to go faster.

### Set `max_address` to your real node count

Leaving it at 64 makes a full frame ~16.8 s at 9600 baud, and high-address
nodes literally never get to send unsolicited messages between host
commands. Set the integration's `max_address` option to the number of
thermostats you actually have, **and** configure the matching `NETST` value
on each thermostat (via its UI or `SN# NETST=N`).

### Power cycles erase change-of-state subscriptions

The 8800 resets `CR` and `CP*` settings to defaults on every power-up
(manual p.17). The coordinator re-applies COS subscriptions on every
periodic refresh as a safety net, so you don't lose update events after a
brownout.

### No protocol-level acknowledgements

Writes the thermostat can't or won't action are silently dropped. The
coordinator verifies critical writes by reading them back, but be suspicious
of any setpoint that doesn't move when you change it.

## Install

### Option A: HACS (recommended)

1. In HACS, open *Integrations -> three-dot menu -> Custom repositories*.
2. Add `https://github.com/ggiesen/ha-aprilaire-rs485` with category
   *Integration*.
3. Search for "Aprilaire 8800 (RS-485)" in HACS and install.
4. Restart Home Assistant.
5. *Settings -> Devices & services -> Add Integration -> Aprilaire 8800
   (RS-485)*. The "Port" field accepts any of the URL forms listed above.

### Option B: Manual

Copy the `custom_components/aprilaire_rs485/` directory from this repo into
your HA `config/custom_components/` directory:

```
config/
  custom_components/
    aprilaire_rs485/
      __init__.py
      manifest.json
      const.py
      protocol.py
      coordinator.py
      config_flow.py
      services.py
      services.yaml
      strings.json
      climate.py
      humidifier.py
      sensor.py
      binary_sensor.py
      text.py
      translations/
        en.json
```

Restart HA, then *Settings -> Devices & services -> Add Integration ->
Aprilaire 8800 (RS-485)*.

## Validate on real hardware first

**Do not** install the HA integration and immediately curse it. Run the
standalone CLI tester against your bus first, with whichever transport you
plan to use:

```bash
# Via the 8811 on a local serial port.
python cli_test.py /dev/ttyUSB0 --discover -v

# Via a TCP gateway.
python cli_test.py socket://192.168.1.50:8000 --discover -v

# Read everything from node 1.
python cli_test.py /dev/ttyUSB0 --addr 1 --selftest -v

# Confirm a setpoint change reaches the device LCD.
python cli_test.py /dev/ttyUSB0 --addr 1 --cmd SH --value 68
```

If `--discover` produces nothing, in order of likelihood the problem is:
1. Wrong wiring mode on the gateway (2-wire instead of 4-wire)
2. Swapped A/B pair polarity
3. No termination on a long run
4. Wrong baud rate
5. Wrong URL (port number, IP)
6. Gateway is half-duplex hardware (i.e. it cannot work - replace it)

Solve all of that with the CLI tester before touching HA.

## What's modelled in HA

Two device categories appear in the device registry:

1. A single "Aprilaire 8800 bus" pseudo-device, with diagnostic entities for
   the bus as a whole.
2. One device per discovered thermostat, with its own climate/humidifier/
   sensor/binary_sensor entities grouped underneath.

### Bus-level entities

| Entity                          | Type            | Notes                                    |
|---------------------------------|-----------------|------------------------------------------|
| Bus connection                  | binary_sensor   | Connectivity class. True when the transport is open. Polled.|
| Node count                      | sensor          | Diagnostic. Number of currently-known nodes.|
| Discovered addresses            | sensor          | Diagnostic. Comma-separated address list.|

### Per-thermostat entities

| Entity                          | When created                                    | Notes |
|---------------------------------|-------------------------------------------------|-------|
| `climate`                       | Thermostat-mode nodes (CT=0)                    | Temp, mode, fan, setpoints, inferred hvac_action |
| `humidifier`, `dehumidifier`    | Humidistat-mode nodes (CT=1)                    | Two entities per humidistat node                  |
| Indoor / outdoor / remote temperature | Per node                                  | Temperatures, scale follows device                |
| Indoor / outdoor / built-in humidity  | Per node                                  | Percent                                           |
| Schedule hold status            | Per node                                        | Diagnostic. NONE/TEMP/PERM/etc.                   |
| Firmware                        | Per node                                        | Diagnostic. From ID query response.               |
| Air filter alarm                | Per node                                        | Problem class. From FLTALM.                       |
| Water panel alarm               | Per node                                        | Problem class. From WPALM.                        |
| Dehumidifier alarm              | Per node                                        | Problem class. From DEHALM.                       |
| HVAC system alarm               | Per node                                        | Problem class. From SYSALM.                       |
| Sensor or comm error (rollup)   | Per node                                        | Problem class. True if any ERROR field non-zero.  |
| Built-in temperature error      | Per node                                        | Problem class, diagnostic. ERROR digit 1.         |
| Remote temperature error        | Per node                                        | Problem class, diagnostic. ERROR digit 2.         |
| Outdoor temperature error       | Per node                                        | Problem class, diagnostic. ERROR digit 3.         |
| Built-in humidity error         | Per node                                        | Problem class, diagnostic. ERROR digit 4.         |
| Communication error             | Per node                                        | Problem class, diagnostic. ERROR digit 5.         |
| EEPROM error                    | Per node                                        | Problem class, diagnostic. ERROR digit 6.         |
| Network hold active             | Per node                                        | Running class, diagnostic. From HOLD.             |
| Progressive recovery            | Per node                                        | Running class. From RECOVSTAT.                    |
| Module N sensor S               | Per (module, sensor) reported in RSM            | Diagnostic. One per support-module sensor; details below. |

### Support-module sensors

A communicating 8800 node can have up to four addressable support modules
(8081/8082 and equivalents), each with two sensors. Sensor 1 is always a
temperature sensor; Sensor 2 is temperature or humidity. The
configuration is reported by the device via the `RSM` query (manual
p.32) and the readings by `RxSy` queries (p.33).

For each connected sensor whose type code is not `XX`, the integration
creates one diagnostic sensor entity named `Module N sensor S`. The
sensor's type code (`CT`, `RT`, `CH`, `RH`) is exposed in
`extra_state_attributes.type_code`. Device class is `temperature` for
`CT`/`RT` and `humidity` for `CH`/`RH`. Entities are placed under
`EntityCategory.DIAGNOSTIC` so a fully-loaded node (4 modules × 2
sensors = 8 extra entities) does not clutter the default device view.

**Two positions are deliberately not exposed as per-module entities:**

- `(M1, S1) = RT` is the canonical outdoor-temperature source per the
  manual (p.34, priority 2). It is surfaced as the `Outdoor temperature`
  sensor and the M1S1 entity is not created to avoid showing the same
  reading twice.
- `(M1, S2) = RH` is the canonical outdoor-humidity source (p.35). Same
  treatment: surfaced as `Outdoor humidity`, the M1S2 entity is
  suppressed.

The filter is positional: `RT` on `M2S1` or `RH` on `M3S2` (or any
non-M1 position) is a legitimate per-room remote sensor and gets its
own entity.

Support-module sensors have no COS bit on the wire, so they are polled
on every periodic refresh rather than updated via push. With the
default refresh interval of 60 seconds, each existing
`(module, sensor)` pair on each node adds one extra query per cycle. On
a fully-loaded 16-node bus with all 8 sensor positions populated that
is 128 extra queries per minute - measurable, but well under the bus's
sustained capacity at 9600 baud. If you have very few support modules,
the cost is negligible.

## Messaging

The 8800 has five writable display slots:

- **TMPMES** - one transient message, RAM-backed, cleared when the
  thermostat loses power or you call `clear_temporary_message`.
- **PMES1 through PMES4** - four permanent messages, EEPROM-backed,
  persist across power cycles. Which permanent slot is currently shown
  is selected on the thermostat itself.

The integration exposes all five through four services on the integration
domain. All four accept the standard HA target fields, so a single call
can address one thermostat, a device, an area, or a label.

| Service                                          | Fields                | Purpose                                     |
|--------------------------------------------------|-----------------------|---------------------------------------------|
| `aprilaire_rs485.send_temporary_message`    | `message`             | Show a message until cleared                |
| `aprilaire_rs485.clear_temporary_message`   | (none beyond target)  | Wipe the temporary message                  |
| `aprilaire_rs485.set_permanent_message`     | `slot` (1-4), `message` | Write text into one of the four permanent slots |
| `aprilaire_rs485.clear_permanent_message`   | `slot` (1-4)          | Wipe a permanent slot                       |

Constraints applied at the boundary so the wire payload is always valid:

- ASCII only. Non-ASCII characters are dropped (not replaced with `?`).
- CR and LF become spaces. CR would otherwise terminate the command
  mid-message; LF has no meaning on a single-line display.
- Length is capped at 32 characters. Longer input is truncated, not
  rejected, so a slightly-too-long automation message still gets through.
- Targeting only the bus device raises an error. Targeting the bus plus
  one or more real thermostats silently skips the bus.

### Permanent message text entities

Each thermostat exposes the four permanent slots as `text` entities, named
`Permanent message 1` through `Permanent message 4`. They live under
`EntityCategory.CONFIG`, so they don't clutter the default device card but
are available in Lovelace, the device page, and automations.

The slots are network-write-only on the wire (the thermostat UI cannot
edit them directly). To work around the missing read path, the entity
shadows the last value written by the integration and uses HA's
`RestoreEntity` to persist that shadow across HA restarts. Service-call
writes and direct entity edits both go through the same coordinator
method, so the entity and any automations stay in sync.

The `TMPMES` (temporary) slot is intentionally not exposed as an entity -
it clears on thermostat power loss, so a persistent entity would silently
drift out of sync after a power blip. Use the `send_temporary_message`
service for transient text.

A typical use is mapping each slot to a meaningful purpose: slot 1 for
service reminders, slot 2 for occupancy notes, slot 3 for installer
contact info, slot 4 for whatever else. The thermostat's local UI selects
which permanent slot is currently displayed.

### Example automations

```yaml
# Notify the kitchen thermostat when the air filter alarm trips.
- alias: "Aprilaire filter alert"
  trigger:
    - platform: state
      entity_id: binary_sensor.kitchen_aprilaire_8800_air_filter_alarm
      to: "on"
  action:
    - service: aprilaire_rs485.send_temporary_message
      target:
        device_id: 4f3b2a... # the kitchen thermostat device
      data:
        message: "REPLACE FILTER"

# Once a year, write the maintenance schedule into permanent slot 4
# on every thermostat in the house.
- alias: "Annual service reminder refresh"
  trigger:
    - platform: time
      at: "03:00:00"
  condition:
    - condition: template
      value_template: "{{ now().month == 1 and now().day == 1 }}"
  action:
    - service: aprilaire_rs485.set_permanent_message
      target:
        area_id: living_areas
      data:
        slot: 4
        message: "Service due 2026-06"
```

## Outdoor temperature push

The 8800 protocol lets the host push outdoor temperature to the bus via a
global `OT=` assignment. Thermostats that have their own direct-wired
outdoor sensor or a Support Module 1 / Sensor 1 monitor sensor ignore the
broadcast (manual p.34, source priority 1 and 2). Thermostats without
their own sensor accept the value and display it. The validity period is
10 minutes - values older than that revert to `--`.

Two configuration options govern this:

- **Outdoor temperature source** (`outdoor_temp_source`, optional, set in
  the config flow via an entity picker): a temperature `sensor` (e.g.
  `sensor.openweather_temperature` or a Zigbee sensor on your deck), a
  `weather` entity (e.g. `weather.home`), or a `climate` entity. When set,
  the integration reads this entity every 5 minutes and broadcasts it
  globally as integer degrees. The value is taken from the sensor's numeric
  state, the weather entity's `temperature` attribute, or the climate
  entity's `current_temperature` attribute, respectively; the unit follows
  the entity (or the HA system unit for `climate`).

- **Rebroadcast** (`outdoor_temp_rebroadcast`, default true): if no HA
  source is configured, the integration looks for the lowest-addressed
  thermostat that has its own outdoor sensor and rebroadcasts that
  value. This is the typical install where one thermostat near an
  outdoor wall has the 8052 sensor wired and the others don't. Set to
  false to disable rebroadcast entirely.

Source priority within the integration:
1. HA entity (if configured and the entity is available)
2. Lowest-addressed thermostat with a working sensor (if rebroadcast is on)
3. Skip this cycle (no broadcast sent)

Outdoor humidity (`OH`) cannot be pushed - the protocol marks it
read-only. Thermostats receive OH only from a Support Module 1 / Sensor
2 monitor sensor on their own bus connection.

## Intentionally not built in this version

- **Schedule editing** (`PROGDxEy`) - protocol-side done, no HA UI. Editing
  on-device is usually less painful than re-mapping into HA.
- **Lockouts and PIN** - not exposed. Niche.
- **Outdoor humidity push** - the OH command is read-only in the protocol
  (manual p.35), so unlike OT there's no way for the host to share an
  outdoor humidity value with thermostats that lack their own sensor.
  Thermostats only get OH from Support Module 1, Sensor 2 configured as
  RH monitor.
- **Daemon + MQTT split** - a better architecture for >4 thermostats
  or heavy automation. The protocol module is self-contained enough to lift
  out into its own process.

## Known weak spots

- `humidifier.py` maps awkwardly onto HA's `HumidifierEntity` in AUTO
  (simultaneous humid+dehum) mode. Replace with `number` + `select` if it
  bothers you.
- `hvac_action` inference from relays is heuristic. The device doesn't
  expose "I am calling for heat" explicitly - only relay state + mode. Heat
  pump aux-with-compressor edge cases may misclassify.
- COS re-application on every periodic refresh is wasteful but cheap. A
  smarter approach watches for the COM error bit going 1->0.

## Compatibility note: 8870

The 8800 protocol is a superset of the 8870. The protocol module should
handle 8870 nodes too, but commands added specifically for the 8800
(programmable schedules, alarm periods, balance points, etc.) will return
nothing on an 8870. The CLI tester is the right tool to find out for sure.

## Repository layout

This repo is hosted on **GitLab** (`gitlab.com/ggiesen/ha-aprilaire-rs485`)
and **mirrored to GitHub** (`github.com/ggiesen/ha-aprilaire-rs485`). The
mirror exists so HACS can install the integration; all actual development
happens on the GitLab side.

- **Issues and PRs**: file on whichever side you prefer; the GitHub mirror
  is the one most HACS users will land on, so issue triage starts there.
- **CI**: GitLab CI runs lint and the full test suite on every push
  (`.gitlab-ci.yml`). GitHub Actions runs the `hassfest` and `HACS`
  validators on the mirror (`.github/workflows/`). The two pipelines
  cover different things and both must pass for a release to ship.

## Development and testing

The test suite is split into two layers:

- **Protocol-only tests** (`tests/test_protocol.py`,
  `tests/test_coordinator.py`, `tests/test_outdoor_temp.py`) have no Home
  Assistant dependency. They cover the wire format, the state-application
  logic, the message formatter, and the outdoor-temperature push pipeline.
  Run them with just `pyserial` and `pytest` installed:
  ```bash
  pip install -e ".[test]"
  pytest tests/test_protocol.py tests/test_coordinator.py tests/test_outdoor_temp.py
  ```
- **HA-integration tests** (`tests/test_config_flow.py`,
  `tests/test_services.py`, `tests/test_support_modules.py`,
  `tests/test_text.py`) cover the config flow, the four messaging
  services, support-module sensor discovery, and the text-platform
  entities. They need `pytest-homeassistant-custom-component` (which
  pulls in `homeassistant`):
  ```bash
  pip install -e ".[test-ha]"
  pytest tests/
  ```

`pyproject.toml` sets `asyncio_mode = "auto"`, so async tests don't need
explicit decorators. The HA tests use the `enable_custom_integrations`
fixture (autouse-wrapped in `test_config_flow.py`) so the loader finds
the integration during the run.

Lint and format are handled by `ruff`. CI runs both `ruff check .` and
`ruff format --check .`; run the same locally:
```bash
pip install -e ".[dev]"
ruff check .
ruff format --check .
```

## Contributing

This is a personal project, but PRs are welcome. Please:

1. Open a PR against `master` on the **GitLab** side. The GitHub mirror is
   pull-based; merges into the GitHub copy will be overwritten on the next
   sync.
2. Make sure `ruff check .`, `ruff format --check .`, and the full
   `pytest tests/` suite pass locally.
3. If the change touches user-visible behaviour or config, update both the
   README and `CHANGELOG.md` (under `## [Unreleased]`).

## Licence

This project is licensed under the [Mozilla Public License 2.0](LICENSE).
The 8800 and 8811 manuals are the property of Research Products Corporation
/ Aprilaire.
