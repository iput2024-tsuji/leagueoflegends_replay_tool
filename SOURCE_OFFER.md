# Corresponding Source and Third-Party Source Information

LoL Replay Tool is licensed under `GPL-3.0-only`.

## Release source assets

Every future binary Release must provide the source and license materials for
the exact build under these stable asset names:

```text
LoLReplayTool-source-<version>.zip
LoLReplayTool-third-party-sources-<version>-NN.zip
LoLReplayTool-license-materials-<version>.zip
SHA256SUMS.txt
```

`LoLReplayTool-source-<version>.zip` is created from the exact Git commit used
for that binary and contains the preferred form for modifying LoL Replay Tool,
including its build and packaging scripts. The matching Git tag is `v<version>`.

The numbered third-party source archives contain only source archives whose
URL and SHA256 are locked and verified for the packaged components. They may be
split into multiple assets, each smaller than 2 GiB. The Release workflow
refuses publication unless every packaged runtime component has verified source
coverage, including wheel-vendored native code, or a documented system-library
exception has completed legal review. The license-materials archive contains
the project license, notices, source information, Qt replacement instructions,
component lock, copied license texts and generated build inventory.
`SHA256SUMS.txt` identifies every published asset.

今後のバイナリReleaseでは、上記の固定した資産名で、検証できたsourceと
ライセンス資料を提供します。プロジェクトsource archiveは実際にビルドした
commitから生成し、番号付き第三者source archiveは2 GiB未満の複数資産へ
分割できます。全runtime componentとwheel内native codeのsource coverageが
検証済みになるまでRelease workflowは公開を拒否します。
`SHA256SUMS.txt`には公開する全資産を記録します。

## Build inventory and source lock

`licenses/components.json` records the expected component, version, license,
artifact patterns, and source URL/SHA256 where those sources have been
verified. A missing source archive or unverified vendored-native source is an
explicit Release gate. The generated
`licenses/distribution-manifest.json` records the relative path, SHA256 and
component classification for files in the completed packaged application.
The manifest is a technical inventory of that build, not a substitute for
license texts or legal review.

`licenses/components.json`は期待するcomponent、version、license、成果物
patternと、検証済みのsource URL/SHA256を記録します。source archiveの欠落や
wheel内native sourceの未確認は公開を止めるRelease gateです。生成される
`licenses/distribution-manifest.json`は完成した配布物の相対path、SHA256、
component分類を記録する技術的なinventoryであり、ライセンス本文や法的確認に
代わるものではありません。

The current lock includes verified candidates for LoL Replay Tool, Python,
PyQt6, obsws-python, OpenCV and the FFmpeg codec library in opencv-python.
It does not yet prove complete source coverage for every runtime wheel.
In particular, the Qt 6.10.2 official archive is an upstream reference: the
patches, configure options and build scripts used for the PyQt6-Qt6 Windows
wheel have not been verified, so it is not asserted to be that wheel's exact
Corresponding Source. The same fail-closed treatment applies to the wheel's
Qt plugin third-party notices and bundled Mesa `opengl32sw.dll`. Microsoft
Visual C++ runtime files are classified separately; their redistribution and
source-exception basis requires recorded specialist evidence. These gaps
deliberately keep the Release gate closed.
Package-specific license texts are copied under `licenses/python-packages/`.

現在のlockには、LoL Replay Tool、Python、PyQt6、obsws-python、OpenCV、
opencv-python内FFmpeg codec libraryについて検証済みの候補を記録していますが、
すべてのruntime wheelのsource coverageは未確認です。特にQt 6.10.2公式archiveは
上流参考sourceであり、PyQt6-Qt6 Windows wheelで使われたpatch、configure
option、build scriptを確認できていないため、当該wheelのexact Corresponding
Sourceとは断定しません。Qt pluginの第三者通知とwheel同梱Mesa
`opengl32sw.dll`にも同じfail-closed方針を適用します。Microsoft Visual C++
runtimeは別componentとして分類し、再配布とsource例外の根拠を専門家の証拠付きで
記録する必要があります。これらの不足が残る間はRelease gateを閉じたままにします。

If a listed source asset becomes unavailable, request the matching source
through the project's Issue tracker. Maintainers must provide an equivalent
copy at no charge:

https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/issues

## Qt replacement

The packaged application dynamically loads Qt libraries. See
`QT_RELINKING.md` for the file locations, compatibility constraints, replacement
procedure and complete application rebuild instructions.

配布アプリケーションはQtライブラリを動的に読み込みます。ファイルの場所、
互換性条件、交換手順、アプリケーション全体の再ビルド方法は
`QT_RELINKING.md`を参照してください。

## User-provided external tools

OBS Studio, standalone FFmpeg and libmpv are not included in the installer or
packaged application. This project does not automatically download, mirror,
bundle, or redistribute them. The user explicitly obtains each required tool
and remains responsible for the license information accompanying that build.

OBS Studio 32.1.2 is the currently tested version. The application manages
only a portable copy that the user extracts into its dedicated `obs-portable`
directory; it does not manage a normally installed OBS instance. Standalone
FFmpeg 8.1.1 x64 is the currently tested clip-export tool. It is resolved from
the explicit setting, the application-data `bin` directory, application-root
fallbacks, and then safe absolute directories in the system `PATH`. This
standalone executable is distinct from the OpenCV FFmpeg DLL contained in the
packaged application.

Because these external tools are not distributed by this project, they are not
represented as corresponding-source assets for the installer. The application
opens an upstream information page only after an explicit user action.

- OBS Project Releases: https://github.com/obsproject/obs-studio/releases
- FFmpeg download guidance: https://ffmpeg.org/download.html

OBS Studio、standalone FFmpeg、libmpvはインストーラーや配布アプリケーションに
含めません。本プロジェクトはこれらを自動取得、ミラー、同梱、再配布せず、
利用者が各ツールとそのライセンス資料を明示的に入手・配置します。

現在の検証対象はOBS Studio 32.1.2とstandalone FFmpeg 8.1.1 x64です。OBSは
利用者が専用`obs-portable`へ展開したポータブル版だけを管理し、通常版OBSの
インストール先は利用しません。FFmpegは明示設定、アプリデータの`bin`、
アプリルートのfallback、安全な絶対ディレクトリのシステム`PATH`の順で探索します。
standalone FFmpegは配布アプリケーション内のOpenCV FFmpeg DLLとは別物です。
これらの外部ツールは本プロジェクトの配布物ではないため、インストーラーの
corresponding-source assetsには含めません。

## v0.5.2 historical limitation

The v0.5.2 installer has been withdrawn and is not available for download.
Its original GitHub Actions artifact is no longer retained, so later historical
materials cannot reconstruct or independently verify every file from that
installer. Any source or hash retained for v0.5.2 is historical identification,
not a replacement installer or proof of a newly reproduced binary. The known
build reference is Actions run `28287427901` at commit
`c88ded675accf403f4d5e2bfee1bc53247c14af7`. No binary will be restored,
replaced, or overwritten. The maintainer has recorded acceptance of this audit
limitation and the associated residual risk while keeping the installer
withdrawn. That maintainer decision does not by itself mark the recorded
historical-remediation compliance gate complete. Every technical, source,
provenance, historical, and licensing gate for a new distribution remains in
force until its explicit completion criteria are satisfied.

v0.5.2インストーラーは撤回され、現在ダウンロードできません。元の
GitHub Actions成果物は保持されていないため、後から追加する履歴資料だけでは
当時のインストーラー内の全ファイルを再構成・独立検証できません。v0.5.2用に
残すsourceやhashは履歴識別情報であり、インストーラーの復元・差し替えや
再現ビルドの証明ではありません。既知のビルド基準はActions run
`28287427901`、commit `c88ded675accf403f4d5e2bfee1bc53247c14af7`です。
バイナリの復元・差し替え・上書きは行いません。管理者はこの監査上の制約と
残余リスクを認識して受け入れ、インストーラーの撤回を維持する決定を記録しています。
ただし、この管理者決定だけで記録済みの履歴是正compliance gateを完了扱いには
しません。新しい配布物の技術、source、provenance、履歴、ライセンスに関する
各gateは、それぞれの完了条件が明示的に満たされるまで維持します。

## Future OBS bundling

OBS Studio is currently outside the installer. If a future offline installer
bundles OBS Studio or the project integrates libobs, that work requires a
separate licensing and product decision. It must preserve the applicable
license texts and notices, provide the exact corresponding sources and build
information, and extend the artifact checks before publication.

現在、OBS Studioはインストーラーの対象外です。将来のオフライン同梱または
libobs統合は、ライセンスと製品仕様に関する別の判断が必要です。公開前に、
適用されるライセンス本文・通知、正確な対応ソース・ビルド情報を提供し、
成果物検査を拡張する必要があります。
