# LoL Replay Tool

[日本語](README.md) | [English](README.en.md)

LoL Replay Tool is a Windows decision-support application that automatically records League of Legends matches, stores match events as JSON, and extracts tactical insights from accumulated play data.

It combines recording, event synchronization, replay review, and data analysis. The project aims to identify observed conditions associated with higher or lower win rates using decision-tree models built with scikit-learn.

## Download

Download the latest `LoLReplayTool-Setup-*.exe` from [GitHub Releases](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/releases/latest).

The application downloads OBS and FFmpeg when they are needed. An mpv DLL is not bundled. Obtain a supported 64-bit mpv DLL separately and place it in `%LOCALAPPDATA%\LoLReplayTool\bin`. You can verify the installer with the attached `SHA256SUMS.txt`.

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

- `OBSClient`: OBS WebSocket communication and OBS control
- `RiotAPIClient`: Riot local API requests and response parsing
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
  app.py                 # PyQt6 GUI and RecorderWorker
  recordtest.py          # Recording workflow and OBS/Riot clients
  recording_library.py   # Safe recording and metadata deletion
  player.py              # mpv replay player
  analytics.py           # Data analysis and tactical rules
  controllers.py         # UI/backend controller layer
  app_paths.py           # Runtime path resolution
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
- A supported 64-bit mpv DLL

OBS Studio, mpv DLLs, and Riot Games image assets are not included in this repository or the packaged application.

- OBS Studio is downloaded at first startup. Development uses `obs-portable`; packaged builds use `%LOCALAPPDATA%\LoLReplayTool\obs-portable`.
- Place an mpv DLL in `bin/` for development or `%LOCALAPPDATA%\LoLReplayTool\bin` for an installed build.
- FFmpeg is downloaded when clip export is first used. The application does not depend on FFmpeg from the system `PATH`.
- Champion icons are neither bundled nor downloaded.

## Development Setup

```powershell
pip install -r requirements.txt
copy config\setting.sample.json config\setting.json
python main.py
```

Direct dependencies are maintained in `requirements.in` and `requirements-dev.in`. Locked dependencies are stored in `requirements.txt` and `requirements-dev.txt`.

Manual editing of `config/setting.json` is normally unnecessary. The application:

- Uses only the managed portable OBS installation
- Downloads OBS before displaying the main window when required
- Downloads FFmpeg only when clip export requires it
- Prevents simultaneous application startup
- Serializes setup with an inter-process lock
- Uses fixed dependency versions and SHA256 verification
- Configures OBS portable mode and authenticated local WebSocket access
- Provides automatic environment repair for scenes and synchronization sources
- Exposes recording, audio, storage, and notification settings in the GUI

Packaged builds store mutable data under `%LOCALAPPDATA%\LoLReplayTool`.

## Usage

```powershell
python main.py
```

- Match monitoring starts with the main screen.
- OBS recording starts after match-start detection.
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

The application uses PyInstaller `onedir` packaging:

```powershell
pip install pyinstaller
.\scripts\build.ps1
```

Output:

```text
dist\LoLReplayTool\
  LoLReplayTool.exe
  THIRD_PARTY_NOTICES.md
  _internal\
```

OBS, FFmpeg, mpv DLLs, configuration, and recordings are not copied into the application distribution directory.

## Installer

Install Inno Setup 6 and run:

```powershell
winget install --id JRSoftware.InnoSetup -e
.\scripts\build_installer.ps1
```

The default installation directory is `%LOCALAPPDATA%\Programs\LoLReplayTool`, so administrator privileges are not required.

The uninstaller provides separate unchecked options for deleting application data and recordings. Recording cleanup only targets `%LOCALAPPDATA%\LoLReplayTool\recordings`; an external recording directory configured by the user is not deleted.

## Publishing a GitHub Release

Update `VERSION`, add the same version to both `CHANGELOG.md` and `CHANGELOG.en.md`, merge the change into `main`, and push the matching tag:

```powershell
git tag v0.2.0
git push origin v0.2.0
```

GitHub Actions validates tests, Ruff, the Windows build, the version, and both changelog sections before publishing the installer. Release notes contain Japanese first and English second.

## Troubleshooting

- Run `LoLReplayTool.exe --self-check` to inspect configuration, writable paths, and OBS/FFmpeg/mpv availability without opening the GUI.
- If portable OBS is missing, verify `%LOCALAPPDATA%\LoLReplayTool\obs-portable\bin\64bit\obs64.exe` for an installed build or `obs-portable\bin\64bit\obs64.exe` for development.
- If the mpv DLL is missing, place one of `mpv-1.dll`, `libmpv-1.dll`, `mpv-2.dll`, or `libmpv-2.dll` in the appropriate `bin` directory.
- FFmpeg is normally downloaded on first clip export. A manually supplied executable must be placed in the application data `bin` directory or the development `bin` directory.
- If the OBS WebSocket port is already in use, close any normal or manually started OBS instance. This application controls only its managed portable OBS.
- If OBS appears in the system tray, close all existing OBS processes before restarting the application.
- If events are missing, inspect the JSON `events` and `events_all` fields.
- Analytics requires multiple JSON files containing both wins and losses.
- If synchronization is incorrect, use synchronization correction or run automatic environment repair again.
