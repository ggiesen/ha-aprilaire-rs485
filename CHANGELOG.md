# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
