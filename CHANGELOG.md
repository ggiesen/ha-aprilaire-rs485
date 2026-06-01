# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Bus diagnostic counters on the bus device: ten `total_increasing` sensors
  covering parse errors, transport errors, apply errors, unknown commands,
  write-verification failures, and query timeouts, plus activity totals
  (messages sent, messages received, verifications attempted, queries sent).
  Error counters refresh immediately on change; activity totals poll. All reset
  to zero on restart by design.
- Diagnostic events fired alongside the error counters so automations and the
  logbook can react without polling: `aprilaire_rs485_parse_error`,
  `aprilaire_rs485_transport_error`, `aprilaire_rs485_apply_error`,
  `aprilaire_rs485_unknown_command`, `aprilaire_rs485_write_verification_failed`,
  `aprilaire_rs485_query_timeout`, and `aprilaire_rs485_query_recovered`, each
  with a context payload.
- Write verification: setpoint, mode, and fan writes are checked five seconds
  later against the device's reported state, surfacing writes that were lost or
  rejected by a hold/lockout (which the protocol otherwise drops silently).
- Per-node query-timeout tracking with transition semantics: when a node stops
  answering, the counter increments once and a `query_timeout` event fires (not
  once per unanswered query); when it answers again, a `query_recovered` event
  fires. An intermittently-slow node shows up as a few episodes rather than
  dozens of per-query timeouts.
- The unknown-command allow-list is the full 8800 command set from the
  Programmer's Manual (RPC P/N 10009414), so `unknown_commands` flags only
  genuinely off-protocol traffic. This covers change-of-state codes such as
  `PROGFMT`, `EVTSDAY`, and `PROGUPDT` that only appear when a setup or schedule
  changes on the thermostat, plus the COS-enable acknowledgements and the
  TIME/DATE/PERMHOLD write echoes.

### Changed

- The manifest `issue_tracker` now points to the GitLab issues page, where
  development and triage happen. New-issue creation from GitHub is funneled to
  GitLab as well.
- Corrected the clock-sync documentation: the thermostat's clock is backed by
  batteries and rides out ordinary system power loss, so it is lost only when
  those batteries are depleted or removed -- not on every power interruption as
  the notes previously implied.

## [0.1.1] - 2026-05-31

### Changed

- Raised the minimum supported Home Assistant version to 2026.3.0, matching the
  bundled brand icon (which renders on 2026.3+) and the Home Assistant APIs the
  integration relies on.

## [0.1.0] - 2026-05-31

First public release.

### Added

- Aprilaire Model 8800 RS-485 protocol handler over local serial, `socket://`,
  or `rfc2217://`, with a push-based coordinator that re-applies change-of-state
  subscriptions and reconnects automatically.
- Climate, humidifier, sensor, binary_sensor, button, select, and text
  platforms, plus bus-level diagnostics (connection, node count, discovered
  addresses).
- Outdoor temperature sharing: push a Home Assistant temperature to thermostats
  that lack their own outdoor sensor, or rebroadcast from one that has one.
- A message center: four permanent-message `text` entities and services to set
  and clear the temporary and permanent display messages.
- A standalone `cli_test.py` for bench-checking wiring, baud, and addressing,
  and a test suite covering protocol parsing, state application, the
  outdoor-temperature pipeline, support modules, services, and the config and
  options flows.
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
  keeping the thermostat clocks correct (the RTC drifts slowly, and the clock
  is lost only when the backup batteries are depleted or removed -- the
  batteries hold it through ordinary system power loss).
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

[Unreleased]: https://github.com/ggiesen/ha-aprilaire-rs485/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/ggiesen/ha-aprilaire-rs485/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ggiesen/ha-aprilaire-rs485/releases/tag/v0.1.0
