# Corresponding Source and Third-Party Source Information

LoL Replay Tool is licensed under `GPL-3.0-only`.

Under the [September 4 maintainer decision](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/issues/54#issuecomment-5538215910),
external legal review remains unperformed. Unavailable historical artifacts,
unverified publisher-internal artifact chains and byte-for-byte rebuilding
limits are disclosed rather than treated as automatic Release blockers.
This does not establish legal compliance or complete any missing source,
license, notice, native-binary, Runtime, clean-VM or independent-review evidence.

[9月4日の管理者決定](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/issues/54#issuecomment-5538215910)により、
外部法務レビューは未実施と明記し、取得不能な履歴成果物、未確認のpublisher内部
artifact chain、byte単位の再buildの限界は自動停止条件ではなく開示事項とします。
法的適合を断定せず、source、license、notice、native binary、Runtime、clean VM、
独立レビューの技術証拠不足は引き続き公開を止めます。

`release_disclosure` records only the decision's approved classifications.
The old review-required flags and reasons, and unverified fact flags, are
retained as history; only records with a validated disclosure are treated
under that limited classification. They are not marked as reviewed or verified.

`release_disclosure`は9月4日決定による限定した分類変更を記録します。旧review-required
flag・reasonと未確認の事実flagは履歴として保持し、認定を検証できた項目だけを
開示扱いにします。レビューや検証が完了したことにはしません。

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
coverage, including wheel-vendored native code. The two excluded Microsoft
Runtime records (`microsoft-vc-runtime-python` and `microsoft-vc-runtime`) are
documented external prerequisites under the decision above, not claimed to
have verified source or completed legal review. The license-materials archive contains
the project license, notices, source information, Qt replacement instructions,
component lock, copied license texts and generated build inventory.
`SHA256SUMS.txt` identifies every published asset.

今後のバイナリReleaseでは、上記の固定した資産名で、検証できたsourceと
ライセンス資料を提供します。プロジェクトsource archiveは実際にビルドした
commitから生成し、番号付き第三者source archiveは2 GiB未満の複数資産へ
分割できます。配布に含む全runtime componentとwheel内native codeのsource coverageが
検証済みになるまでRelease workflowは公開を拒否します。非同梱のMicrosoft Runtime
2記録（`microsoft-vc-runtime-python`、`microsoft-vc-runtime`）は上記決定による
外部前提であり、source検証や法務レビューが完了したとは扱いません。ライセンス
資料には本体license、notice、source情報、Qt置換手順、component lock、個別license
本文と生成した配布inventoryを含めます。
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

## Microsoft Visual C++ Runtime prerequisite

Windows x64 binary distributions use the Microsoft Visual C++ 2015–2022
Redistributable x64 as an external, user-installed prerequisite. Runtime DLLs
and `vc_redist.x64.exe` are intentionally absent from the application,
installer, and Release assets; the installer does not download or install
them. It checks both HKLM registry views, `Installed`, and `Version` before any
file change, accepts newer compatible versions, and fails closed if the x64
prerequisite is missing, inconsistent, or below `14.44.35211.0`. Interactive
guidance may open Microsoft's official page only after consent; silent mode
returns a non-zero status without browsing or prompting.

Custom native wheels used by a future binary build must be produced by a
reproducible source-build or repair recipe with pinned wheel/source/tool
inputs, SHA256 values, PE import inventories, and build provenance. The recipe
must show that hashed Microsoft Runtime imports and app-local Runtime files
are absent from the dist, expanded installer, and Release assets; these checks
remain fail-closed. External legal review remains unperformed and disclosed;
no public Release is authorized while the mandatory technical gates remain
open.

Windows x64バイナリ配布物は、利用者が導入するMicrosoft Visual C++
2015–2022 Redistributable x64を外部前提とします。Runtime DLLと
`vc_redist.x64.exe`はアプリ、インストーラー、Release資産へ含めず、
インストーラーもダウンロード・導入しません。ファイル変更前にHKLM両view、
`Installed`、`Version`を検査し、より新しい互換Versionを許可しますが、x64前提が
不足・不整合、または`14.44.35211.0`未満ならfail-closedで停止します。
対話時の公式案内は同意後だけ開き、silent modeはブラウザーや対話を行わず非0で
終了します。custom native wheelのsource、固定hash、PE import、provenanceと、
CI / Release workflowがdist・完成installer展開物・Release assetで実施する、
app-local/ハッシュ付きRuntimeを拒否する監査は、公開Releaseの前提として未完了です。

Inno Setup 6.7.3 is a build-time toolchain whose selected Setup/Uninstall
stubs and LZMA decompression code are embedded in the public installer. The
compiler, LZMA compression components and other build-only files are not
redistributed in the installed application. The exact official source archive is locked to
`https://github.com/jrsoftware/issrc/archive/refs/tags/is-6_7_3.zip`; its URL,
size and SHA256, together with the compiler/stub identities, are recorded in
`licenses/components.json` and the sealed build provenance. That archive is
included in a numbered third-party source Release asset. The pinned Inno Setup
License text is distributed at `licenses/inno-setup/LICENSE.txt`.

Inno Setup 6.7.3はbuild時のtoolchainであり、選択したSetup/Uninstall stubと
LZMA展開コードが公開するインストーラーへ埋め込まれます。compiler、LZMA圧縮
component、その他のbuild専用ファイルはインストール後のアプリケーションには
再配布しません。正確な公式source archiveは
`https://github.com/jrsoftware/issrc/archive/refs/tags/is-6_7_3.zip`に固定し、
URL、size、SHA256、compiler/stubの識別情報を`licenses/components.json`と
sealed build provenanceへ記録します。このarchiveは番号付き第三者source Release
資産に収録し、固定したInno Setup License本文は
`licenses/inno-setup/LICENSE.txt`で配布します。

The current lock includes verified candidates for LoL Replay Tool, Python,
PyQt6, obsws-python, OpenCV and the FFmpeg codec library in opencv-python. It
does not yet prove complete source coverage for every runtime wheel. The 20 Qt
6.10.2 artifacts shipped by this application are byte-identical to members of
the official `qtbase`, `qtsvg` and `qtimageformats` MSVC 2022 archives. Their
official module SBOMs identify the build configuration and source revisions,
and their substantive source inventories match the three locked official
submodule source archives. The referenced third-party license texts are also
packaged. The PyQt6-Qt6 wheel publisher's complete repackaging provenance is
still unverified and is disclosed under the decision above. The unused Mesa `opengl32sw.dll`
is excluded from the application distribution. Microsoft Visual C++ runtime
files are classified separately as excluded external prerequisites; their
absence from the application, installer and Release assets remains mandatory.
The NumPy and SciPy records pin the exact MacPython `openblas-libs` tags,
OpenBLAS commit, Windows workflow and applied patch, but not the complete
Rtools/GCC toolchain manifests or publisher artifact chains; those
limits are disclosed. The records' incomplete native source coverage remains
a Release gate independently of the publisher-internal chain.
Package-specific license texts and the Qt SBOMs are copied under
`licenses/python-packages/`.

現在のlockには、LoL Replay Tool、Python、PyQt6、obsws-python、OpenCV、
opencv-python内FFmpeg codec libraryについて検証済みの候補を記録していますが、
すべてのruntime wheelのsource coverageはまだ完了していません。このアプリが
同梱する20個のQt 6.10.2成果物は、公式`qtbase`、`qtsvg`、`qtimageformats`の
MSVC 2022 archive memberとbyte単位で一致します。公式module SBOMからbuild設定と
source revisionを確認し、実質的なsource inventoryを固定した3つの公式submodule
source archiveと照合し、参照される第三者ライセンス本文も同梱します。
PyQt6-Qt6 wheel公開者による再packaging工程全体のprovenanceは未確認のまま開示します。
未使用のMesa `opengl32sw.dll`はアプリ配布物から除外します。Microsoft Visual C++
Runtimeは非同梱の外部前提として別componentに分類し、app、installer、Release
assetのRuntime不存在検査を維持します。
NumPyとSciPyについては、MacPython `openblas-libs`のexact tag、OpenBLAS commit、
Windows workflow、適用patchを固定しましたが、Rtools/GCC toolchainの
完全なmanifestと公開wheelまでのartifact chainは未確認のまま開示します。
これとは別に、未完了のnative source coverageのRelease gateを維持します。

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
withdrawn. The September 4 decision treats the unavailable original artifact
as a disclosed limitation, without claiming a completed historical or external
legal review. The source, license, notice and technical checks for a new
distribution remain mandatory.

v0.5.2インストーラーは撤回され、現在ダウンロードできません。元の
GitHub Actions成果物は保持されていないため、後から追加する履歴資料だけでは
当時のインストーラー内の全ファイルを再構成・独立検証できません。v0.5.2用に
残すsourceやhashは履歴識別情報であり、インストーラーの復元・差し替えや
再現ビルドの証明ではありません。既知のビルド基準はActions run
`28287427901`、commit `c88ded675accf403f4d5e2bfee1bc53247c14af7`です。
バイナリの復元・差し替え・上書きは行いません。管理者はこの監査上の制約と
残余リスクを認識して受け入れ、インストーラーの撤回を維持する決定を記録しています。
9月4日の決定に従い、取得不能な元artifactは開示事項とし、履歴監査や外部法務
レビューが完了したとは扱いません。新しい配布物のsource、license、noticeと
技術検査は維持します。

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
