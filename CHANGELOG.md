# Changelog

## [0.2.0] - 2026-06-11

### Added

- Added configurable Windows notifications for recording start, recording completion, recording failure, and minimizing to the system tray.
- Added a master notification switch and independent switches for each notification event.
- Added recording-start notifications only after OBS confirms that recording is active.

### Changed

- Added repository rules that keep branch names, commit messages, and Pull Request titles focused on the change instead of the tool used to create it.

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
