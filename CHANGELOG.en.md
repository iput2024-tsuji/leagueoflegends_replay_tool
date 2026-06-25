# Changelog

## [0.5.0] - 2026-06-25

### Added

- Added a recording encoder setting with "Auto (GPU priority)" and "CPU stable (x264)" choices.

### Changed

- Changed the default OBS recording encoder back to GPU-priority automatic selection, choosing H.264 encoders in the order NVIDIA NVENC, Intel Quick Sync, AMD AMF, then x264.
- Excluded HEVC and AV1 from automatic selection, falling back to x264 when no supported H.264 hardware encoder is available.

### Fixed

- Retry recording once with x264 when recording fails to start with a GPU encoder.
- Fixed the recording-start preparation path so choosing "CPU stable (x264)" does not switch back to GPU-priority automatic selection.

## [0.4.9] - 2026-06-23

### Fixed

- Stop recording when the LoL game process crashes or practice mode is closed without the normal end-match flow.
- Added LCU Gameflow phase and LoL game-process monitoring to recording-end detection, covering cases where the Live Client API does not emit `GameEnd`.
- Added recording-stop logs that identify whether Live Client, LCU Gameflow, or LoL process disappearance ended the recording.

## [0.4.8] - 2026-06-23

### Fixed

- Unified the managed OBS profile as `LoLReplayTool` so installed first-run environments do not keep using OBS-generated `LoL_Replay_Tool` profiles.
- Force the OBS `user.ini` profile selection to `LoLReplayTool` before launch so `Simple / x264 / mkv` settings are loaded when OBS initializes recording output.
- Added recording-start diagnostics for the active OBS profile, scene collection, output list, and `simple_file_output` status and path.

## [0.4.7] - 2026-06-22

### Fixed

- Normalized the managed OBS recording profile to `Simple / x264 / mkv` before startup and at runtime, reducing recording-start failures when stale Advanced/NVENC settings remain.
- Treat failed raw OBS recording requests as explicit errors and include output mode, recording format, encoder, and output path in recording-start diagnostics.

## [0.4.6] - 2026-06-21

### Fixed

- Changed the default OBS recording encoder to x264 to reduce recording-start failures caused by NVENC initialization.
- Disabled process audio capture on the LoL window capture source so LoL window transitions are less likely to block recording startup.

## [0.4.5] - 2026-06-21

### Fixed

- Avoided reapplying OBS video settings immediately before recording starts, reducing OBS output-start failures.
- Added a short settling delay before the first recording-start request and before the recovery retry to avoid starting recording immediately after settings are applied.

## [0.4.4] - 2026-06-20

### Fixed

- Reapplies OBS output settings when OBS WebSocket accepts a recording-start request but OBS does not transition into recording state.
- Switches to x264 and retries recording once with `ToggleRecord` during recovery.
- Includes recovery failure details and OBS log diagnostics in the recording failure message when recovery does not start recording.

## [0.4.3] - 2026-06-20

### Fixed

- Wait for the LoL Live Client connection for a short period after LCU game-start detection before starting OBS recording.
- Extended the OBS recording-start confirmation timeout to reduce failures caused by startup-time initialization delays.
- Suppressed repeated recording-start retries during the same match after a failure and included OBS log diagnostics in the failure message.

## [0.4.2] - 2026-06-17

### Changed

- Aligned shared default configuration values between the schema and runtime settings to reduce the risk of one side drifting from the other.
- Exposed the champion aliases file path through `AppConfig.paths`.

### Fixed

- Avoided duplicate tactical insight samples from the same match and only shows conditions when enough observations are available.
- Changed tactical insight wording to "observed win rate" to make it clear that results come from accumulated observations.
- Allowed MPV initialization to be retried in the same process after the MPV DLL is added later.
- Rejected unsafe paths while extracting the OBS ZIP archive to prevent unintended overwrites from archive member paths.
- Made recording deletion plan creation less likely to stop entirely when temporary I/O errors occur.
- Made automatic storage-limit cleanup continue processing other owned sessions when files are locked, deleted, or inaccessible.

## [0.4.1] - 2026-06-13

### Fixed

- Added the dedicated LCU Gameflow phase API to game-start monitoring so recording can start when the Live Client API or Gameflow session API is unavailable.
- Normalized variations of `GameStart`, `InProgress`, and `Reconnect` to detect game-start states reliably.

## [0.4.0] - 2026-06-13

### Added

- Added locally available champion icons to the Ban/Pick sequence and final team compositions in the replay view.
- Kept text-only display when a champion icon is unavailable. The application does not download icons automatically.

### Changed

- Removed the internal status-detail line shown below the recording status badge on the home screen.
- Enabled recording-completion notifications by default for new configurations.

### Fixed

- Delayed recording-completion notifications until the LoL game process clears, reducing interference from Windows in-game notification suppression.
- Added logs for notification requests, settings-based suppression, tray submission, and delivery failures.

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
