# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The climate entity exposes the auto-mode deadband (the device `DBAND`
  setting) as an `auto_deadband` attribute. Home Assistant has no way to
  enforce a minimum gap in the setpoint slider, so this is for visibility; the
  device itself corrects deadband violations and reports the adjusted
  setpoints back.
- An options flow to change the outdoor temperature source and rebroadcast
  toggle after initial setup (Settings -> Devices & services -> the entry ->
  Configure). Saving reloads the integration so the change takes effect.
- A bundled brand icon and logo (in `brand/`) so the integration shows the
  Aprilaire artwork in the UI with no `home-assistant/brands` submission.
  Requires HA 2026.3+ (local brand images); on older HA the icon is absent
  unless the assets are also submitted to the brands repo.
- A `button` per node to clear each maintenance alarm (air filter, water
  panel, dehumidifier, HVAC system), sending `[alarm]ALM=OFF`. The alarm
  states remain as problem `binary_sensor` entities; the buttons are the
  clear/acknowledge action, previously only possible at the thermostat.
- Optional clock sync: when enabled (default), the integration pushes Home
  Assistant's local wall-clock time and date to the bus on startup and hourly,
  keeping the thermostat clocks correct (they drift and reset on power loss).
  The thermostat's own DST (`DLS`) setting is left untouched -- the device
  keeps owning DST and the periodic re-push realigns the wall time across
  transitions. Toggle in the config and options flows.
- A `select` per node for each maintenance-alarm reminder interval
  (`[alarm]ALMP`): air filter (OFF/1/3/6/12 months) and water panel,
  dehumidifier, and HVAC system (OFF/1-12 months), as config entities. These
  are the thresholds that drive the alarm sensors. `HUMTYP` (humidifier
  hardware type) is intentionally left to the thermostat's setup screens.

### Fixed

- `services.yaml` declared an unsupported `device:` filter under `target`,
  which hassfest rejects. Removed it; the `entity` integration filter already
  scopes the target picker and device/area targeting still works.
- The built-in humidity (`BIHUM`) and direct-wired remote temperature (`RTS`)
  sensors were parsed but never queried, so they never reported a value. Both
  are now polled on startup and each refresh (they have no change-of-state
  push); on thermostats this makes the built-in humidity reading work.
- `decode_errors` no longer raises on a malformed `ERROR` response (e.g. two
  bus responses merged into one line when a `CR` delimiter is lost on the
  wire). Lines whose first six characters aren't all digits are dropped
  silently, consistent with how the other decoders handle corrupt input.
- Dropped a spurious "Event loop is closed" traceback that could be logged
  when the RX thread had a message in flight as Home Assistant shut down or
  reloaded the entry. The in-flight message is now discarded quietly during
  teardown.
- The bus sensors (node count, discovered addresses) and support-module
  sensor discovery dispatched `async_write_ha_state` / entity creation through
  bare `lambda`s, which Home Assistant runs on an executor thread - now a hard
  "calls async_write_ha_state from a thread other than the event loop" error.
  They now dispatch through `@callback` targets so they run on the event loop.
  (Same class as the climate/humidifier dispatch fix; missed in `sensor.py`.)
- Climate and humidifier entities were never created on a running system. The
  per-node update subscription was connected through a bare `lambda`, so Home
  Assistant ran it in an executor thread where `async_add_entities` fails
  ("Task was destroyed but it is pending"). Dispatch now goes through the
  `@callback`-decorated handler and runs on the event loop.
- A node discovered after platform setup never received an update
  subscription, so a thermostat that appeared late (or whose `CT` response
  arrived after discovery) never got a climate/humidifier entity. Each node is
  now subscribed when it is discovered.
- Per-node devices referenced the bus pseudo-device as their `via_device`
  before that device existed, which logged a deprecation warning and would
  break in Home Assistant 2025.12. The bus device is now registered during
  setup before any node entity references it.
- `weather` and `climate` entities now work as the outdoor temperature
  source. Previously the reader only parsed an entity's numeric state, so the
  `weather.home` example in the docs silently failed; it now reads the
  `temperature` / `current_temperature` attribute for those domains.
- The humidifier/dehumidifier toggles now compose onto the node's single mode
  instead of clobbering it. Turning a direction on while the other is active
  now reaches AUTO (previously unreachable from the UI), and turning one off
  now drops to the other direction instead of switching the node fully off.

### Changed

- The climate entity advertises the temperature feature matching the current
  mode (single `TARGET_TEMPERATURE` in heat/cool, `TARGET_TEMPERATURE_RANGE`
  in auto) instead of both at once, so the frontend no longer shows a
  dual-setpoint range control in single-setpoint modes. It also declares
  `TURN_ON` and `TURN_OFF`.
- The climate entity's min/max setpoint bounds now follow the device's
  documented ranges per mode (heat 40-90F/4-32C, cool 42-99F/6-37C) instead of
  Home Assistant's generic defaults, and the setpoint step is set to 1 degree.
- The outdoor temperature source is now chosen with an entity picker
  (filtered to temperature sensors plus weather and climate entities) instead
  of a free-text entity_id field.

## [0.1.0] - 2026-05-20

### Added

- Initial release.
- Aprilaire Model 8800 RS-485 protocol handler (local serial, `socket://`,
  `rfc2217://`).
- DataUpdateCoordinator with COS subscription re-application and automatic
  reconnect.
- Climate, humidifier, sensor, binary_sensor, and text platforms.
- Bus-level diagnostic entities (connection, node count, discovered addresses).
- Outdoor temperature push (HA entity source + lowest-addressed-thermostat
  rebroadcast).
- Four messaging services (`send_temporary_message`,
  `clear_temporary_message`, `set_permanent_message`,
  `clear_permanent_message`).
- Standalone `cli_test.py` for bench-validating wiring/baud/addressing before
  installing the integration.
- Test suite covering protocol parsing, coordinator state application,
  outdoor temperature push, support modules, services, text entities, and
  config flow.

[Unreleased]: https://github.com/ggiesen/ha-aprilaire-rs485/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ggiesen/ha-aprilaire-rs485/releases/tag/v0.1.0
