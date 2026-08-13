# LoL Replay Tool

[日本語](README.md) | [English](README.en.md)

LoL Replay Tool is a Windows decision-support application that automatically records League of Legends matches, stores match events as JSON, and extracts tactical insights from accumulated play data.

It combines recording, event synchronization, replay review, and data analysis. The project aims to identify observed conditions associated with higher or lower win rates using decision-tree models built with scikit-learn.

## Download

No public installer is currently available. The v0.5.2 installer was withdrawn
while its distribution-license and corresponding-source materials are
revalidated, and it can no longer be downloaded from
[GitHub Releases](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/releases).
Use of any locally retained v0.5.2 installer is not recommended. Any SHA-256
recorded for v0.5.2 is only a historical identifier for the withdrawn binary;
it does not identify a currently downloadable binary or prove a reproducible
build. The original Actions artifact is no longer retained, so every file in
that installer cannot now be independently audited. The maintainer has accepted
this audit limitation and the residual risk while keeping v0.5.2 withdrawn.

The next public version will attach its installer, exact project source,
third-party sources, license materials, and a `SHA256SUMS.txt` covering every
asset to the same Release. OBS Studio and standalone FFmpeg are external tools
that users explicitly obtain and place. This project does not automatically
download, mirror, bundle, or redistribute them. No new public Release will be
made until the remaining gates for the distributed files, including runtime and
wheel-vendored native source coverage and PyQt6-Qt6 wheel build provenance, are
complete. Publication also requires an explicit maintainer instruction.

An mpv DLL is also not bundled. Obtain a supported 64-bit DLL separately and
place it in `%LOCALAPPDATA%\LoLReplayTool\bin`.

## Riot Games Disclaimer

LoL Replay Tool is not endorsed by Riot Games and does not reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games and all associated properties are trademarks or registered trademarks of Riot Games, Inc.

This tool uses local League of Legends APIs but is not an official Riot Games product or service.

Ban/Pick capture uses the League Client API (LCU). LCU is not an officially supported third-party API, so a League Client update may change its behavior.

## Key Features

- Background match monitoring after the main window starts
- Automatic startup and recording control of the managed portable OBS instance
- Match state and event capture through Riot local APIs
- Recording video and JSON event-log storage
- Ban/Pick order, champion, team, and position capture
- Queue information such as Ranked, Normal, and ARAM
- mpv-based replay playback
- Seeking from events categorized as kills, deaths, assists, objectives, and buildings
- Ban/Pick order and final team composition in the replay view, with locally available champion icons
- Synchronization correction UI
- Replay filtering by champion, result, and match type
- Safe deletion of recordings and related data
- Win-rate summaries and tactical insights
- Configurable storage, frame rate, microphone, capacity limits, and Windows notifications
- Recording-completion notifications are sent after the game process clears to avoid in-game notification suppression
- Automated tests for asynchronous workflows and external integration boundaries

## Technology Stack

| Technology | Purpose |
| --- | --- |
| Python | Application implementation |
| PyQt6 | GUI, navigation, tray integration, settings, and analytics |
| QThread | Background monitoring and analysis without blocking the UI |
| asyncio | Asynchronous recording-monitor event loop |
| aiohttp | Asynchronous Riot API requests |
| obsws-python | OBS recording, scene, and audio control |
| python-mpv | Replay playback |
| OpenCV | Synchronization marker detection |
| pandas | JSON aggregation and feature generation |
| scikit-learn | Champion feature encoding and decision-tree analysis |
| pytest | Unit and asynchronous tests |
| PyInstaller | Windows application packaging |
| Inno Setup | Installer, updates, and uninstallation |

## Architecture

Recording monitoring runs in a `RecorderWorker` separate from the GUI thread. The worker creates its own asyncio event loop and handles Riot API and OBS WebSocket communication without freezing PyQt6.

The main responsibilities are separated as follows:

- `obs_websocket_client.py` / `OBSClient`: OBS WebSocket communication and OBS control
- `riot_api.py` / `RiotAPIClient`: Live Client API and LCU requests, response parsing, and poll-state classification
- `recorder_config.py` / `AppConfig`: structured recording settings, loading, and user-data path resolution
- `storage_policy.py`: storage limits, app-owned recording checks, and safe deletion when the limit is exceeded
- `RecordingSessionManager` / `LoLAutoRecorder`: recording workflow orchestration
- `controllers.py`: settings, audio, analytics, and recording controllers
- `app.py`: PyQt6 views and user interaction
- `analytics.py`: DataFrame generation, features, and tactical rules

External integrations are injectable, so tests can simulate OBS and Riot API behavior without launching either application.

## Analytics Pipeline

```text
Riot local APIs
  -> match information and events
  -> JSON storage
  -> pandas DataFrame
  -> early-game feature generation
  -> enemy champion encoding with MultiLabelBinarizer
  -> DecisionTreeClassifier training
  -> human-readable observed high- and low-win-rate conditions
  -> PyQt6 analytics view
```

Example features include:

- Void Grubs taken within 15 minutes
- Allied towers destroyed within 15 minutes
- First Blood
- Presence of specific enemy champions

## Development Workflow

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for branch, Pull Request, validation, language, and Release rules.

Maintainer-authored commits and Pull Requests are managed in Japanese. English Issues and Pull Requests from external contributors are welcome.

## Project Layout

```text
src/
  app.py                  # PyQt6 GUI and RecorderWorker
  recordtest.py           # Recording state transitions, session integration, and compatibility facade
  recorder_config.py      # Structured recording settings, loading, and path resolution
  riot_api.py             # Live Client API / LCU client and poll-state classification
  storage_policy.py       # Storage limits and safe deletion of app-owned recordings
  obs_websocket_client.py # OBS WebSocket client and request/response handling
  recording_library.py    # Safe recording and metadata deletion
  player.py               # mpv replay player
  analytics.py            # Data analysis and tactical rules
  controllers.py          # UI/backend controller layer
  app_paths.py            # Runtime path resolution
config/
  setting.sample.json
recordings/
  json/
assets/
  app/
bin/
  *.dll                  # User-provided mpv DLL for development
  ffmpeg.exe
obs-portable/
  bin/64bit/obs64.exe
installer/
  LoLReplayTool.iss
tests/
```

## Requirements

- Windows
- Python 3.14 for the currently tested development and build environment
- PowerShell 7 or later (`pwsh`, for development and builds only)
- A portable OBS Studio installation
- Standalone FFmpeg when using clip export
- A supported 64-bit mpv DLL

Run the Windows development and build `.ps1` scripts with PowerShell 7 or
later. Windows PowerShell 5.1 (`powershell.exe`) is not supported. Install
PowerShell 7 by following the [official Microsoft instructions](https://learn.microsoft.com/powershell/scripting/install/install-powershell-on-windows),
then confirm that `pwsh --version` reports major version 7 or later. The
`powershell` code blocks in this README assume a `pwsh` session.

OBS Studio, standalone FFmpeg used for clip export, mpv DLLs, and Riot Games
image assets are not included in this repository or the packaged application.

- Obtain the Windows x64 ZIP from the [official OBS Project Releases](https://github.com/obsproject/obs-studio/releases) and extract it so that `obs-portable\bin\64bit\obs64.exe` exists for development, or `%LOCALAPPDATA%\LoLReplayTool\obs-portable\bin\64bit\obs64.exe` exists for an installed build.
- Place an mpv DLL in `bin/` for development or `%LOCALAPPDATA%\LoLReplayTool\bin` for an installed build.
- Obtain a suitable Windows build through the [official FFmpeg download guidance](https://ffmpeg.org/download.html). Select `ffmpeg.exe` in Settings, place it in `bin/` for development or `%LOCALAPPDATA%\LoLReplayTool\bin` for an installed build, or expose it through a system `PATH` containing only safe absolute directories.
- Champion icons are neither bundled nor downloaded.

This project does not automatically download, mirror, bundle, or redistribute
OBS Studio, standalone FFmpeg, or mpv DLLs. The in-application upstream-page
buttons open the provider's page only after an explicit user action.

## Development Setup

```powershell
pip install -r requirements.txt
copy config\setting.sample.json config\setting.json
python main.py
```

Direct dependencies are maintained in `requirements.in` and `requirements-dev.in`. Locked dependencies are stored in `requirements.txt` and `requirements-dev.txt`.

Manual editing of `config/setting.json` is normally unnecessary. The application:

- Uses only the managed portable OBS installation
- Starts without OBS and shows the official Release page and dedicated placement path when it is missing
- Resolves FFmpeg in this order: `paths.ffmpeg_executable`, configured `bin_dir`, `bin/ffmpeg.exe` and `ffmpeg.exe` under the application root, then safe absolute directories from the system `PATH`
- Prevents simultaneous application startup
- Configures OBS portable mode and authenticated local WebSocket access
- Provides an explicit OBS configuration and recheck action for scenes and synchronization sources
- Exposes recording, audio, storage, and notification settings in the GUI

Packaged builds store mutable data under `%LOCALAPPDATA%\LoLReplayTool`.

## Usage

```powershell
python main.py
```

- Match monitoring starts with the main screen.
- OBS recording starts after match-start detection.
- The recording encoder defaults to GPU-priority automatic selection and falls back to x264 if recording cannot start.
- Recording stops and JSON is saved after the match ends.
- Previous matches can be opened from the replay screen.
- The trash button moves the video, JSON, and related clips to the Windows Recycle Bin.
- Tactical summaries are available from the analytics screen.

JSON is saved to `recordings/json/` in development and `%LOCALAPPDATA%\LoLReplayTool\recordings\json\` in packaged builds.

```text
lol_YYYYMMDD_HHMMSS.json
```

When available, `match` contains queue ID, queue type, display name, game mode, map, and game ID. `ban_pick` contains confirmed Ban/Pick actions, team compositions, the local player cell ID, and the last champion-select phase.

Hovered but unconfirmed champions are not stored. If a champion-select session is dodged, its history is discarded when a new session starts.

## Tests

```powershell
.\venv\Scripts\python.exe -m ruff check src tests
.\venv\Scripts\python.exe -m pytest -p no:cacheprovider tests
```

The test suite covers API failures, OBS disconnections, asynchronous recording transitions, game-start and game-end detection, configuration, playback lifecycle, and analytics.

## Windows Build

The application uses PyInstaller `onedir` packaging. The output includes the
GPL text, corresponding-source information, Qt replacement instructions, and
the license texts collected from the exact installed Python packages:

```powershell
pip install pyinstaller
pwsh -NoProfile -File .\scripts\build.ps1
```

Output:

```text
dist\LoLReplayTool\
  LoLReplayTool.exe
  LICENSE
  SOURCE_OFFER.md
  THIRD_PARTY_NOTICES.md
  QT_RELINKING.md
  licenses\
    components.json
    distribution-manifest.json
  _internal\
```

`licenses/distribution-manifest.json` is a technical inventory of physical
files, SHA256 values, and component classifications observed in the completed
build. It does not replace the applicable license texts or legal analysis.

OBS, the standalone `ffmpeg.exe`, mpv DLLs, configuration, and recordings are
not copied into the application distribution directory. The FFmpeg DLL used by
the bundled `opencv-python` wheel and its notices are included.

## Installer

Install Inno Setup 6 and run:

```powershell
winget install --id JRSoftware.InnoSetup -e
pwsh -NoProfile -File .\scripts\build_installer.ps1
```

The default installation directory is `%LOCALAPPDATA%\Programs\LoLReplayTool`, so administrator privileges are not required.

The uninstaller provides separate unchecked options for deleting application data and recordings. Recording cleanup only targets `%LOCALAPPDATA%\LoLReplayTool\recordings`; an external recording directory configured by the user is not deleted.

## Publishing a GitHub Release

Publish only after the licensing review, a dedicated Release-preparation Issue,
CI, real-Windows checks, and an explicit maintainer decision are complete.
Update `VERSION` and both changelogs, merge the change into `main`, and then
create the matching tag. A tag push alone does not publish a Release; the
`release` Environment requires maintainer approval. After pushing the tag,
manually run the `Release` workflow with `workflow_dispatch`, enter the same
`vX.Y.Z` in `tag`, and enter the exact uppercase word `PUBLISH` in
`publish_confirmation`. The publish job waits for approval after the prepare
job has verified and generated every asset; inspect those results and the asset
list before approving the `release` Environment.

```powershell
git tag vX.Y.Z
git push origin vX.Y.Z
```

GitHub Actions validates the tag, `VERSION`, ancestry from `main`, source hash,
tests, Ruff, the Windows build, and license materials before publishing. Each
Release contains:

```text
LoLReplayTool-Setup-<version>.exe
LoLReplayTool-source-<version>.zip
LoLReplayTool-third-party-sources-<version>-NN.zip
LoLReplayTool-license-materials-<version>.zip
SHA256SUMS.txt
```

Third-party sources may be split into multiple assets smaller than 2 GiB.
Published Releases are not edited, assets are not overwritten, and tags are
not moved or deleted; a correction uses a new version. OBS Studio and standalone
FFmpeg are user-provided external tools. This project does not automatically
download, mirror, bundle, or redistribute them.

## License

LoL Replay Tool is licensed under `GPL-3.0-only`. A commercial-PyQt distribution
path is not currently adopted. See `LICENSE` for the full text,
`THIRD_PARTY_NOTICES.md` for third-party summaries and primary sources,
`SOURCE_OFFER.md` for corresponding-source information, and `QT_RELINKING.md`
for Qt library replacement and rebuild instructions.

GPL rights already granted to recipients of a distributed version cannot later
be revoked. Whether a future version can be relicensed must be assessed again
against the copyright permissions and dependencies then in effect, with all
required rights-holder agreements and commercial licensing or replacement of
GPL dependencies obtained where necessary.

## Troubleshooting

- Run `LoLReplayTool.exe --self-check` to inspect configuration, writable paths, and OBS/FFmpeg/mpv availability without opening the GUI.
- If portable OBS is missing, obtain and extract the Windows x64 ZIP from the official OBS Project Release page. Verify `%LOCALAPPDATA%\LoLReplayTool\obs-portable\bin\64bit\obs64.exe` for an installed build or `obs-portable\bin\64bit\obs64.exe` for development. A normally installed OBS instance is not managed.
- If the mpv DLL is missing, place one of `mpv-1.dll`, `libmpv-1.dll`, `mpv-2.dll`, or `libmpv-2.dll` in the appropriate `bin` directory.
- If FFmpeg is missing, select `ffmpeg.exe` in Settings or place it in the application-data or development `bin` directory. The search order is the explicit setting, data `bin`, application-root `bin` and root fallbacks, then safe absolute directories from the system `PATH`.
- If the OBS WebSocket port is already in use, close any normal or manually started OBS instance. This application controls only its managed portable OBS.
- If OBS appears in the system tray, close all existing OBS processes before restarting the application.
- If events are missing, inspect the JSON `events` and `events_all` fields.
- Analytics requires multiple JSON files containing both wins and losses.
- If synchronization is incorrect, use synchronization correction or run the OBS configuration and recheck action again.
