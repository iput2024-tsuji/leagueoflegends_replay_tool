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
| Microsoft Visual C++ runtime files | Native runtime used by CPython, Qt and numerical wheels | Microsoft Visual C++ Redistributable terms; redistribution/source-exception evidence remains a Release gate |
| Mesa `opengl32sw.dll` in the Qt wheel | Software OpenGL fallback | MIT and bundled component licenses; exact source/build provenance remains a Release gate |
| aiohttp, Requests and supporting packages | Network clients | Component-specific permissive licenses included with the packages |
| PyInstaller bootloader | Executable packaging bootloader | GPL-2.0-or-later with the PyInstaller bootloader exception |
| Inno Setup 6.7.3 | Setup/Uninstall stubs and LZMA decompression code embedded in the public installer; LZMA compression tools are build-only inputs | Inno Setup License; the pinned license text is copied with the distribution materials |

The exact locked component versions and artifact patterns, plus source URLs and
hashes where verified, are recorded in `licenses/components.json`. Missing
runtime source archives, unverified wheel-vendored native sources, the
unverified PyQt6-Qt6 wheel build provenance and Qt plugin notices, Mesa
provenance, and Microsoft runtime exception evidence are Release gates. The generated
`licenses/distribution-manifest.json` records relative paths, hashes and
component classifications observed in the completed build. It is a technical
inventory, not a controlling legal record. Copied package license texts are
under `licenses/python-packages/`. The pinned Inno Setup license is copied to
`licenses/inno-setup/LICENSE.txt`.

正確なcomponentバージョンと成果物pattern、および検証できたsource URL/hashは
`licenses/components.json`に記録します。runtime source archiveの欠落、
wheel同梱native source、PyQt6-Qt6 wheelのbuild provenanceとQt plugin通知、
Mesaのprovenance、Microsoft runtime例外根拠の未確認は
公開を止めるRelease gateです。生成される
`licenses/distribution-manifest.json`は、完成したビルドで確認した相対path、
hash、component分類の技術的なinventoryであり、法的に支配的な記録では
ありません。パッケージから収集したライセンス本文は
`licenses/python-packages/`、固定したInno Setupのライセンス本文は
`licenses/inno-setup/LICENSE.txt`にあります。

Inno Setup 6.7.3 contributes the Setup/Uninstall stubs and LZMA decompression
code embedded in the public installer; these are not files in the installed
application directory. The compiler, LZMA compression components and other
supporting tools are build-only inputs and are not redistributed. The exact
compiler, stub and worker identities are recorded in `licenses/components.json`
and the sealed build provenance. The matching fixed official source archive is
placed in a numbered `LoLReplayTool-third-party-sources-<version>-NN.zip`
Release asset.

Inno Setup 6.7.3のSetup/Uninstall stubとLZMA展開コードは、公開する
インストーラーに埋め込まれますが、インストール後のアプリケーション
ディレクトリへ個別ファイルとして配置されるものではありません。compiler、
LZMA圧縮component、その他の補助toolはbuild時だけの入力で、再配布しません。
正確なcompiler、stub、workerの識別情報は`licenses/components.json`とsealed build
provenanceに記録し、対応する固定済み公式source archiveは番号付きの
`LoLReplayTool-third-party-sources-<version>-NN.zip` Release資産へ収録します。

Corresponding-source information is in `SOURCE_OFFER.md`. Instructions for
replacing the dynamically linked Qt libraries are in `QT_RELINKING.md`.

対応ソースの情報は`SOURCE_OFFER.md`、動的リンクされたQtライブラリの
交換手順は`QT_RELINKING.md`を参照してください。

## User-provided external components / 利用者が用意する外部コンポーネント

The following components are not included in the installer or packaged
application. This project does not automatically download, mirror, bundle, or
redistribute them:

- **OBS Studio 32.1.2** is the currently tested version. The user explicitly
  obtains the Windows x64 ZIP from the
  [official OBS Project Release page](https://github.com/obsproject/obs-studio/releases) and
  extracts it into the application's dedicated `obs-portable` directory. A
  normally installed OBS instance is not managed by this application.
- **Standalone FFmpeg 8.1.1 x64** is the currently tested clip-export tool.
  The user explicitly obtains a suitable build through the
  [official FFmpeg download guidance](https://ffmpeg.org/download.html) and either selects its
  `ffmpeg.exe` in Settings, places it in the application data `bin` directory,
  or makes it available through a safe absolute system `PATH`. This executable
  is separate from the OpenCV FFmpeg DLL already present in the installer.
- **libmpv** is supplied separately by the user. The user must retain the
  license information supplied with that build.
- Riot Games artwork and champion icons are neither bundled nor downloaded.

上記のOBS Studio、standalone FFmpeg、libmpvは、利用者がライセンス条件を
確認して明示的に入手・配置する外部ツールです。本プロジェクトはこれらを
自動取得、ミラー、同梱、再配布しません。アプリ内の公式ページボタンは、
利用者が押した場合に限って上流の案内ページをブラウザーで開きます。
OBSは専用`obs-portable`だけを管理し、通常版OBSのインストール先は利用しません。

The maintainer has recorded acceptance of the audit limitation and residual
risk associated with the withdrawn v0.5.2 installer. That decision does not by
itself mark the historical-remediation compliance gate complete. All recorded
source, provenance, historical, and distribution gates remain in force until
their explicit completion criteria are satisfied.

管理者は、撤回済みv0.5.2インストーラーに関する監査上の制約と残余リスクを
認識し、受け入れる決定を記録しています。ただし、この決定だけで履歴是正の
compliance gateを完了扱いにはしません。source、provenance、履歴、配布条件に
関する各gateは、それぞれの完了条件が明示的に満たされるまで維持します。

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
- Inno Setup license:
  https://raw.githubusercontent.com/jrsoftware/issrc/is-6_7_3/license.txt
- Inno Setup 6.7.3 source:
  https://github.com/jrsoftware/issrc/archive/refs/tags/is-6_7_3.zip
- OBS Studio: https://github.com/obsproject/obs-studio/blob/master/COPYING
- FFmpeg: https://ffmpeg.org/legal.html
