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

## Microsoft Visual C++ Runtime prerequisite / Microsoft Visual C++ Runtimeの前提条件

The Windows x64 application uses the Microsoft Visual C++ 2015–2022
Redistributable x64 as a user-installed external prerequisite. Its DLLs and
`vc_redist.x64.exe` are not included in the application, installer, or Release
assets. The installer does not download, execute, or elevate to install the
Redistributable. Before file changes, it checks both HKLM registry views,
`Installed`, and `Version`, accepts newer compatible versions, and fails closed
when the x64 prerequisite is missing, inconsistent, or below `14.44.35211.0`.
Interactive users may consent to open Microsoft's official guidance page;
silent installation never browses or prompts and exits non-zero.

Windows x64アプリケーションは、利用者が導入するMicrosoft Visual C++
2015–2022 Redistributable x64を外部前提とします。DLLと`vc_redist.x64.exe`は
アプリ、インストーラー、Release資産へ含めません。インストーラーはRuntimeを
ダウンロード・実行・UAC昇格して導入せず、ファイル変更前にHKLMの両registry view、
`Installed`、`Version`を検査します。より新しい互換Versionは許可し、x64前提が
不足・不整合、または`14.44.35211.0`未満ならfail-closedで停止します。
対話時のみ利用者の同意後にMicrosoft公式案内ページを開き、silent modeでは
ブラウザーも対話も行わず非0で終了します。

This records packaging behavior only and is not legal advice or a completed
GPL/Microsoft redistribution review. The corresponding source, provenance, and
public-Release gates remain open until their explicit criteria are met.
これは配布方式の記録であり、法的助言やGPL/Microsoft再配布条件の確認完了を
意味しません。対応source、provenance、公開Release gateは完了条件を満たすまで
維持します。

## Components in the Windows application / Windows配布物に含まれるもの

| Component | Role | License summary |
| --- | --- | --- |
| Python and its bundled OpenSSL runtime | Runtime, TLS and cryptography | Python Software Foundation License 2.0; Python's third-party license page contains notices for bundled components |
| PyQt6 | GUI bindings | GPL-3.0-only for the free edition used by this project |
| Qt 6 | GUI libraries and plugins | LGPL-3.0-only for this distribution; official module SBOMs and the license texts for the shipped dependency closure are included |
| obsws-python | OBS WebSocket client | GPL-3.0-only |
| python-mpv | Python binding loaded at runtime | GPL-2.0-or-later; a separately supplied libmpv build has its own build-dependent terms |
| opencv-python / OpenCV | Image and video processing | MIT packaging code; Apache-2.0 OpenCV code; bundled third-party notices also apply |
| OpenCV FFmpeg DLL | Video I/O used by OpenCV | LGPL-2.1-or-later for the locked build, with the notices shipped for that wheel |
| NumPy, pandas, SciPy, scikit-learn | Numerical and analytics libraries | Primarily BSD-3-Clause, with component-specific bundled notices |
| OpenBLAS and other numerical binaries | Numerical runtime used by wheels | OpenBLAS and LAPACK use BSD-style terms; statically linked GCC runtime portions are covered by GPL-3.0-or-later with GCC Runtime Library Exception 3.1, with the full notices included in the wheels |
| Microsoft Visual C++ runtime prerequisite | External runtime used by CPython, Qt and numerical wheels; not bundled | External legal review remains unperformed; packaging checks verify the excluded external-runtime boundary |
| aiohttp, Requests and supporting packages | Network clients | Component-specific permissive licenses included with the packages |
| PyInstaller bootloader | Executable packaging bootloader | GPL-2.0-or-later with the PyInstaller bootloader exception |
| Inno Setup 6.7.3 | Setup/Uninstall stubs and LZMA decompression code embedded in the public installer; LZMA compression tools are build-only inputs | Inno Setup License; the pinned license text is copied with the distribution materials |

The exact locked component versions and artifact patterns, plus source URLs and
hashes where verified, are recorded in `licenses/components.json`. The 20 Qt
artifacts shipped by this application are byte-identical to members of the
official Qt 6.10.2 archives. Their official module SBOMs, corresponding
submodule source archives and referenced license texts are locked. The
PyQt6-Qt6 wheel publisher's complete repackaging provenance remains unverified
and disclosed under the September 4 decision linked below. Microsoft Runtime
records describe excluded external prerequisites. Source, license, notice and
native-binary checks remain mandatory. The generated `licenses/distribution-manifest.json`
records relative paths, hashes and component classifications observed in the
completed build. It is a technical inventory, not a controlling legal record.
Copied package license texts and the Qt SBOMs are under
`licenses/python-packages/`. The pinned Inno Setup license is copied to
`licenses/inno-setup/LICENSE.txt`.

The unused Mesa software OpenGL fallback `opengl32sw.dll` supplied by the Qt
wheel is removed by the packaging policy and is not distributed.

For NumPy and SciPy, the locked source set includes their source distributions,
the pinned OpenBLAS sources and recipes, GCC 10.3.0, the Rtools recipes and
patches, and the complete MinGW source tree described in `SOURCE_OFFER.md`.
The existing wheel notices are retained. GCC license texts and its Runtime
Library Exception, and the MinGW CRT, headers and winpthreads notices, are
copied under `licenses/python-packages/{numpy,scipy}/runtime-sources/`.
Retaining this conservative source set does not mean every included tool or
runtime is bundled with the application. Complete publisher toolchain
manifests and internal artifact chains, and external legal review, remain
unverified and disclosed; final distribution checks remain mandatory.

正確なcomponentバージョンと成果物pattern、および検証できたsource URL/hashは
`licenses/components.json`に記録します。このアプリが同梱する20個のQt成果物は
公式Qt 6.10.2 archiveのmemberとbyte単位で一致し、公式module SBOM、対応する
submodule source archive、参照されるライセンス本文を固定しています。
PyQt6-Qt6 wheel公開者による再packaging工程全体のprovenanceは未確認のまま開示し、
Microsoft Runtimeは非同梱の外部前提として記録します。source、license、noticeと
native binaryの技術検査は維持します。生成される
`licenses/distribution-manifest.json`は、完成したbuildで確認した相対path、hash、
component分類の技術的なinventoryであり、法的に支配的な記録ではありません。
パッケージのライセンス本文とQt SBOMは`licenses/python-packages/`、固定した
Inno Setupのライセンス本文は`licenses/inno-setup/LICENSE.txt`にあります。

Qt wheelが提供する未使用のMesa software OpenGL fallback `opengl32sw.dll`は
packaging policyで除外し、配布しません。

NumPy/SciPyのsource集合は、本体sdist、固定OpenBLAS source/recipe、GCC
10.3.0、Rtoolsのrecipeとpatch、`SOURCE_OFFER.md`に記載したMinGW全sourceを
含みます。既存wheel noticeを保持し、GCCのlicense/Runtime Library Exceptionと
MinGWのCRT/headers/winpthreads noticeを
`licenses/python-packages/{numpy,scipy}/runtime-sources/`へ収録します。保守的な
source集合の全tool・全runtimeをアプリへ同梱する意味ではありません。publisher
toolchainの完全なmanifestと内部artifact chain、外部法務レビューは未確認のまま
開示し、正式配布物の検証は維持します。

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

The [September 4 maintainer decision](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/issues/54#issuecomment-5538215910)
treats unavailable v0.5.2 artifacts and unverified publisher-internal chains as
disclosed limitations. External legal review remains unperformed; this fact
alone does not block a Release. Source, license, notice, native-binary, Runtime,
clean-VM and independent-review requirements remain mandatory.

[9月4日の管理者決定](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/issues/54#issuecomment-5538215910)により、
取得不能なv0.5.2元artifactと未確認のpublisher内部chainは開示事項とします。
外部法務レビューは未実施であり、それ自体を公開停止条件にはしません。
source、license、notice、native binary、Runtime、clean VM、独立レビューの
技術条件は維持します。

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
