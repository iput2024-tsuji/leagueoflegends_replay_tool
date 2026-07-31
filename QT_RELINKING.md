# Replacing the Qt Libraries / Qtライブラリの交換手順

LoL Replay Tool uses the free GPL edition of PyQt6 and dynamically loads the
Qt 6 libraries included in the Windows application. This document explains
how a recipient can replace those Qt libraries with a compatible modified
build. It does not change the license that applies to PyQt6 or any Qt module.

LoL Replay Toolは無償GPL版のPyQt6を使用し、Windows配布物に含まれる
Qt 6ライブラリを動的に読み込みます。この文書は、受領者がQtライブラリを
互換性のある変更版へ交換する方法を説明します。PyQt6または各Qtモジュールに
適用されるライセンスを変更するものではありません。

## Identify the files / 対象ファイルの確認

The installed Qt runtime is normally under:

```text
<installation directory>\_internal\PyQt6\Qt6\
  bin\          Qt DLLs
  plugins\      platform, image-format, TLS and other plugins
  translations\ translation data, when present
```

The exact files and SHA256 values in a particular build are listed in
`licenses/distribution-manifest.json`. That manifest is a technical inventory,
not a substitute for the applicable license texts.

インストール済みQtランタイムは通常、上記の場所にあります。個々の
ビルドに含まれる正確なファイルとSHA256値は
`licenses/distribution-manifest.json`で確認できます。このmanifestは
技術的なinventoryであり、適用されるライセンス本文の代わりではありません。

## Replace a dynamically linked Qt build / 動的リンクされたQtの交換

1. Close LoL Replay Tool and make a backup copy of the installation directory.
2. Obtain or build Qt for Windows x86-64. Use a Qt 6 version, compiler ABI and
   configuration compatible with the bundled PyQt6 extension modules.
3. Replace the applicable DLLs under `PyQt6\Qt6\bin` and the corresponding
   plugin directories under `PyQt6\Qt6\plugins`. Keep each plugin beside the
   dependencies required by that same Qt build.
4. Preserve the license texts, notices, build configuration and source
   information supplied with the replacement.
5. Start `LoLReplayTool.exe --self-check`, then test application startup,
   settings, recording controls and replay playback.

1. LoL Replay Toolを終了し、インストール先全体をバックアップします。
2. Windows x86-64向けQtを入手またはビルドします。同梱PyQt6拡張モジュールと
   互換性のあるQt 6のバージョン、コンパイラーABI、構成を使用してください。
3. `PyQt6\Qt6\bin`の対象DLLと、`PyQt6\Qt6\plugins`の対応plugin
   ディレクトリを交換します。同じQtビルドが必要とする依存ファイルも一緒に
   配置してください。
4. 交換版に付属するライセンス本文、通知、ビルド構成、ソース情報を保持します。
5. `LoLReplayTool.exe --self-check`を実行した後、起動、設定、録画制御、
   リプレイ再生を確認します。

Do not replace the PyQt6 `.pyd` extension modules independently unless you
also rebuild PyQt6 for the selected Qt and Python ABI. Mixing incompatible Qt
DLLs, plugins, PyQt6 modules, or C/C++ runtimes can prevent the application
from starting.

選択したQtとPython ABI向けにPyQt6も再ビルドする場合を除き、PyQt6の
`.pyd`拡張モジュールだけを個別に交換しないでください。互換性のない
Qt DLL、plugin、PyQt6モジュール、C/C++ランタイムを混在させると、
アプリケーションが起動しないことがあります。

## Rebuild the complete application / アプリケーション全体の再ビルド

The preferred form for modification is available in the source archive
attached to the matching GitHub Release and in the matching Git tag. On
Windows x86-64, use the Python 3.14.6 runtime locked for Release builds:

```powershell
py -3.14 -m venv venv
.\venv\Scripts\python.exe --version
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\scripts\build.ps1
.\dist\LoLReplayTool\LoLReplayTool.exe --self-check
```

To use a locally built PyQt6 or Qt, install that build into the same virtual
environment before running `scripts\build.ps1`. Confirm the selected packages
and licenses in the generated inventory. The resulting `dist\LoLReplayTool`
directory can be run directly; Inno Setup can package it by using
`scripts\build_installer.ps1`.

対応するGitHub Releaseに添付されたソースアーカイブと同じGit tagが、
改変に適したソースです。Releaseビルドで固定するPython 3.14.6 x86-64を
使用し、上記のversion確認結果も確認してください。ローカルでビルドした
PyQt6またはQtを使う場合は、`scripts\build.ps1`の前に同じ仮想環境へ導入し、
生成されたinventoryで採用パッケージとライセンスを確認してください。
生成された`dist\LoLReplayTool`は直接実行でき、
`scripts\build_installer.ps1`を使ってInno Setup形式にできます。

Nothing in this project adds a restriction against reverse engineering needed
to debug modifications to an LGPL-covered Qt library. Replacement builds remain
subject to their own licenses, and the person redistributing a modified package
must review and satisfy those terms.

本プロジェクトは、LGPL対象Qtライブラリの変更をデバッグするために必要な
リバースエンジニアリングを追加で制限しません。交換版にはその版自身の
ライセンスが適用され、変更したパッケージを再配布する人は該当条件を確認し、
遵守する必要があります。
