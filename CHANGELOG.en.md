# Changelog

## [0.3.0] - 2026-06-13

### Added

- Added a Ban/Pick tab to the replay view showing the Ban/Pick sequence and final team compositions.
- Split replay event filters into kills, deaths, assists, dragons, Rift Herald/Void Grubs, Baron, buildings, and other events.
- Added player-involved assist events to saved recording sessions.

### Changed

- Classified turret and inhibitor destruction as building events.
- Unified event classification so recording persistence and replay display use the same logic.

## [0.2.1] - 2026-06-12

### Added

- Added an English README with links for switching between the Japanese and English documentation.
- Added automatic bilingual Japanese and English GitHub Release notes.

### Changed

- Added automatic H.264 encoder detection from the managed OBS startup log, with a priority order of NVIDIA NVENC, Intel Quick Sync, AMD AMF, and x264.
- Excluded HEVC and AV1 from automatic selection and added an x264 fallback when no supported hardware encoder is available.

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
