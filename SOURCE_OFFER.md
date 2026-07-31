# Corresponding Source and Third-Party Source Information

LoL Replay Tool is licensed under `GPL-3.0-only`.

## LoL Replay Tool source

The complete preferred form for modifying each released version, including
build and packaging scripts, is provided from the matching Git tag:

https://github.com/iput2024-tsuji/leagueoflegends_replay_tool

Each binary Release must include a `LoLReplayTool-source-<version>.zip` archive
created from the exact commit used to build that binary.

The source archive covers LoL Replay Tool itself. Exact third-party versions,
license files and upstream source locations are supplied separately in the
license materials archive. If a listed source becomes unavailable, request the
matching source through the project's Issue tracker; maintainers must provide
an equivalent copy at no charge.

## Bundled components

The packaged application includes Python packages pinned in `requirements.txt`.
Their license files are copied to `licenses/python-packages/` during the build.
The generated `licenses/python-packages.json` records the exact installed
versions and copied files.

Important upstream source locations:

- PyQt6 6.10.2:
  https://pypi.org/project/PyQt6/6.10.2/#files
- Qt 6.10.2:
  https://download.qt.io/official_releases/qt/6.10/6.10.2/single/
- obsws-python 1.8.0:
  https://pypi.org/project/obsws-python/1.8.0/#files
- opencv-python 4.13.0.90:
  https://pypi.org/project/opencv-python/4.13.0.90/#files
- OpenCV:
  https://github.com/opencv/opencv
- FFmpeg:
  https://github.com/FFmpeg/FFmpeg
- python-mpv 1.0.8:
  https://pypi.org/project/python-mpv/1.0.8/#files
- NumPy, pandas, SciPy and scikit-learn:
  https://pypi.org/

The opencv-python Windows wheel includes an FFmpeg DLL under LGPL-2.1. Its
`LICENSE-3RD-PARTY.txt` is preserved by the license collection step.

## Components acquired after installation

- OBS Studio 32.1.2 is downloaded from its official Release and retains the
  license and notice files present in the official archive. Matching sources:
  https://github.com/obsproject/obs-studio/releases/tag/32.1.2
- FFmpeg 8.1.1 essentials build is downloaded from Gyan.dev. Its license,
  README and other notice files are retained next to the installed executable.
  Build information: https://www.gyan.dev/ffmpeg/builds/
  Matching FFmpeg source commit:
  https://github.com/FFmpeg/FFmpeg/commit/239f2c733d
- The mpv DLL is not distributed by this project. Users must obtain a
  correctly licensed build. mpv licensing depends on its build configuration:
  https://github.com/mpv-player/mpv

For source availability problems, open an Issue at:

https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/issues

## If OBS Studio is bundled in a future offline installer

OBS Studio is currently downloaded after installation. A future offline
installer must not simply copy the OBS program directory. Before distribution,
maintainers must:

1. preserve OBS Studio's GPL text and every bundled plugin/library notice;
2. provide equivalent access to the exact corresponding OBS and applicable
   bundled component source used by that binary;
3. preserve build scripts and configuration needed to reproduce modifications;
4. review codec, FFmpeg, plugin, font, trademark and installer notices for the
   selected OBS package; and
5. extend the build manifest and CI checks so an OBS binary cannot be published
   without its license and source materials.

The exact obligations depend on the selected OBS build and must be checked
again when offline bundling is designed.
