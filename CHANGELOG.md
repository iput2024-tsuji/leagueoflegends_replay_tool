# Changelog

## [0.1.4] - 2026-06-11

### Fixed

- Improved game start detection so `gameTime=0` is recognized as a valid start.
- Added an LCU Gameflow fallback using the `InProgress` and `Reconnect` phases when the Live Client API is unavailable.
- Added periodic diagnostics showing both Live Client API status and the current LCU phase.

## [0.1.3] - 2026-06-10

### Added

- Added fractional recording frame-rate settings such as `144/1` and `240000/1001`.
- Enabled LoL application audio capture through the OBS window capture source.

### Changed

- Disabled OBS desktop audio and limited manual audio configuration to the microphone input.
