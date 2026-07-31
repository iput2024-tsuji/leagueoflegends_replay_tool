# Third-Party Notices / 第三者ソフトウェア通知

LoL Replay Tool is licensed under `GPL-3.0-only`. The full license is in
`LICENSE`. This file summarizes important third-party components; the
authoritative license texts copied from the installed packages are under
`licenses/python-packages/`, with exact package versions recorded in
`licenses/python-packages.json`.

LoL Replay Tool は `GPL-3.0-only` で提供されます。ライセンス全文は
`LICENSE`、同梱Pythonパッケージから収集した原文ライセンスと正確な
バージョン一覧は `licenses/` 以下にあります。

## Components in the Windows application / Windows配布物に含まれるもの

| Component | Role | License summary |
| --- | --- | --- |
| Python and its bundled OpenSSL runtime | Runtime, TLS and cryptography | Python Software Foundation License 2.0; Python's third-party license page includes the OpenSSL Apache-2.0 text and other bundled notices |
| PyQt6 | GUI bindings | GPL-3.0-only for the free edition |
| Qt 6 | GUI libraries and plugins | LGPL-3.0, GPL-2.0/GPL-3.0, or commercial terms depending on the module |
| obsws-python | OBS WebSocket client | GPL-3.0-only |
| python-mpv | Python binding loaded at runtime | GPL-2.0-or-later; the separately supplied libmpv build may use GPL-2.0-or-later or LGPL-2.1-or-later terms |
| opencv-python / OpenCV | Image and video processing | MIT packaging code; Apache-2.0 OpenCV code; bundled third-party notices apply |
| OpenCV FFmpeg DLL | Video I/O used by OpenCV | LGPL-2.1-or-later and the notices shipped with the opencv-python wheel |
| NumPy, pandas, SciPy, scikit-learn | Numerical and analytics libraries | Primarily BSD-3-Clause, with component-specific bundled notices |
| OpenBLAS and other numerical binaries | Numerical runtime used by wheels | Component-specific permissive licenses included with the wheels |
| aiohttp, Requests and supporting packages | Network clients | Component-specific permissive licenses included with the packages |
| PyInstaller bootloader | Executable packaging bootloader | GPL-2.0-or-later with the PyInstaller bootloader exception |

The generated manifest and copied license files, rather than this summary, are
the controlling record for the exact build. Source locations and corresponding
source information are listed in `SOURCE_OFFER.md`.

生成された一覧と原文ライセンスが、実際のビルド内容に対する正式な記録です。
対応ソースと入手先は `SOURCE_OFFER.md` を参照してください。

## Components acquired after installation / インストール後に取得するもの

- **OBS Studio 32.1.2** is downloaded from the official OBS Project release
  when required. The official archive is kept intact when installed, except
  for debug symbols removed to reduce disk usage. OBS Studio is GPL-2.0-or-later;
  its plugins and bundled libraries also have their own notices.
- **Gyan.dev FFmpeg 8.1.1 essentials build** is downloaded on first clip export.
  This is separate from the OpenCV FFmpeg DLL already present in the installer.
  The application preserves the downloaded archive's license and README files
  under the application data `licenses/` directory.
- **libmpv** is not distributed by this project. The user supplies a compatible
  64-bit DLL and must retain the license information supplied with that build.
- Riot Games artwork and champion icons are neither bundled nor downloaded.

## Upstream license information / 一次情報

- PyQt6: https://www.riverbankcomputing.com/software/pyqt/
- Qt licensing: https://www.qt.io/licensing/open-source-obligations
- obsws-python: https://github.com/aatikturk/obsws-python/blob/main/LICENSE
- python-mpv: https://github.com/jaseg/python-mpv/blob/main/LICENSE.GPL
- mpv: https://github.com/mpv-player/mpv/blob/master/Copyright
- OpenCV: https://opencv.org/license/
- opencv-python wheel notices:
  https://github.com/opencv/opencv-python/blob/master/LICENSE-3RD-PARTY.txt
- PyInstaller: https://pyinstaller.org/en/stable/license.html
- OBS Studio: https://github.com/obsproject/obs-studio/blob/master/COPYING
- FFmpeg: https://ffmpeg.org/legal.html
