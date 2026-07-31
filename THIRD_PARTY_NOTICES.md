# Third-Party Notices / 第三者ソフトウェア通知

LoL Replay Tool is licensed under `GPL-3.0-only`; the full project license is
in `LICENSE`. This document is a readable summary of important third-party
components. The copied license texts, component lock and generated inventory
are provided to help recipients identify the files in a particular build.
They do not replace the applicable license texts or legal analysis.

LoL Replay Toolは`GPL-3.0-only`で提供され、プロジェクトのライセンス全文は
`LICENSE`にあります。この文書は主な第三者コンポーネントを読みやすく
まとめたものです。同梱するライセンス本文、component lock、生成inventoryは、
特定ビルド内のファイルを識別するための補助資料であり、適用される
ライセンス本文や法的判断に代わるものではありません。

## Components in the Windows application / Windows配布物に含まれるもの

| Component | Role | License summary |
| --- | --- | --- |
| Python and its bundled OpenSSL runtime | Runtime, TLS and cryptography | Python Software Foundation License 2.0; Python's third-party license page contains notices for bundled components |
| PyQt6 | GUI bindings | GPL-3.0-only for the free edition used by this project |
| Qt 6 | GUI libraries and plugins | Module-specific LGPL-3.0, GPL-2.0/GPL-3.0 or other applicable terms |
| obsws-python | OBS WebSocket client | GPL-3.0-only |
| python-mpv | Python binding loaded at runtime | GPL-2.0-or-later; a separately supplied libmpv build has its own build-dependent terms |
| opencv-python / OpenCV | Image and video processing | MIT packaging code; Apache-2.0 OpenCV code; bundled third-party notices also apply |
| OpenCV FFmpeg DLL | Video I/O used by OpenCV | LGPL-2.1-or-later for the locked build, with the notices shipped for that wheel |
| NumPy, pandas, SciPy, scikit-learn | Numerical and analytics libraries | Primarily BSD-3-Clause, with component-specific bundled notices |
| OpenBLAS and other numerical binaries | Numerical runtime used by wheels | Component-specific permissive licenses included with the wheels |
| aiohttp, Requests and supporting packages | Network clients | Component-specific permissive licenses included with the packages |
| PyInstaller bootloader | Executable packaging bootloader | GPL-2.0-or-later with the PyInstaller bootloader exception |

The exact locked component versions, source URLs, source hashes and artifact
patterns are recorded in `licenses/components.json`. The generated
`licenses/distribution-manifest.json` records relative paths, hashes and
component classifications observed in the completed build. It is a technical
inventory, not a controlling legal record. Copied package license texts are
under `licenses/python-packages/`.

正確なcomponentバージョン、source URL、source hash、成果物patternは
`licenses/components.json`に記録します。生成される
`licenses/distribution-manifest.json`は、完成したビルドで確認した相対path、
hash、component分類の技術的なinventoryであり、法的に支配的な記録では
ありません。パッケージから収集したライセンス本文は
`licenses/python-packages/`にあります。

Corresponding-source information is in `SOURCE_OFFER.md`. Instructions for
replacing the dynamically linked Qt libraries are in `QT_RELINKING.md`.

対応ソースの情報は`SOURCE_OFFER.md`、動的リンクされたQtライブラリの
交換手順は`QT_RELINKING.md`を参照してください。

## Components acquired after installation / インストール後に取得するもの

The following components are not included in the installer or its packaged
application directory:

- **OBS Studio 32.1.2** is downloaded from a pinned official OBS Project
  Release when recording support is first prepared.
- **Gyan.dev FFmpeg 8.1.1 essentials build** is downloaded from a pinned
  location when clip export first requires it. This executable is separate
  from the OpenCV FFmpeg DLL already present in the installer. License and
  README materials from the downloaded archive are stored in the application
  data directory.
- **libmpv** is supplied separately by the user. The user must retain the
  license information supplied with that build.
- Riot Games artwork and champion icons are neither bundled nor downloaded.

上記OBS StudioとGyan.dev FFmpegは、インストーラーおよびインストール直後の
アプリケーション配布ディレクトリには含まれず、必要になった時点で固定した
取得元から別途ダウンロードされます。この自動取得が本プロジェクトによる
配布に当たるか、および必要な対応は専門家確認中です。その確認が完了するまで
新しい公開Releaseは行いません。

Whether those automatic OBS Studio and Gyan.dev FFmpeg downloads constitute
distribution by this project, and what additional measures may be required,
remain subject to specialist review. No new public Release will be made until
that review is recorded.

## Upstream license information / 一次情報

- PyQt6: https://riverbankcomputing.com/software/pyqt/
- Qt open-source obligations:
  https://www.qt.io/licensing/open-source-obligations
- obsws-python:
  https://github.com/aatikturk/obsws-python/blob/main/LICENSE
- python-mpv:
  https://github.com/jaseg/python-mpv/blob/main/LICENSE.GPL
- mpv: https://github.com/mpv-player/mpv/blob/master/Copyright
- OpenCV: https://opencv.org/license/
- opencv-python notices:
  https://github.com/opencv/opencv-python/blob/master/LICENSE-3RD-PARTY.txt
- PyInstaller: https://pyinstaller.org/en/stable/license.html
- OBS Studio: https://github.com/obsproject/obs-studio/blob/master/COPYING
- FFmpeg: https://ffmpeg.org/legal.html
