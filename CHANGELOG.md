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

### Fixed

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
