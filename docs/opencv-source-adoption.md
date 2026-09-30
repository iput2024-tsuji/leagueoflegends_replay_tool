# OpenCV対応ソースの技術証跡採用

Issue #54の技術metadataを、Issue #139のIPP-free source-built wheelへ対応させる。
この記録は対応ソースとnative構成の宣言であり、clean Windows 11 VM・実LoL受入、
独立レビューMust 0、管理者最終承認の完了を宣言しない。公開Release、tag作成・push、
既存v0.5.2の変更は行わない。

## 採用する証跡

正式CI [run 36705978374](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/actions/runs/36705978374)
のproducerとapp consumerは同じattempt 1、同じcheckout
`d9cadaabf3288a32d52954056b9b3426a4d5a8f7`、tree
`6c3264b735346686d290479836e3a6548181c4aa`を使用した。
そのtreeはPR #140候補`b950815346d160daa13b61dcb720893cecd9132f`と一致する。

| 証跡 | 固定値・結果 |
| --- | --- |
| source wheel artifact | ID `11093605557`、ZIP SHA256 `5f54393cff9ca4d3bae472d746d998fbe376cdffd292b010cda750988bc3f14e` |
| producer provenance外部seal | `5e587204bdd12a27c4ebd821886211f6ffe2531a5abc158279bbbd7fcf15122a`。consumer envと実ファイルが一致 |
| app artifact | ID `11094431812`、ZIP SHA256 `365bf20d77f152945ff73ad16646889a75b7583530150b59d98666628b3d8a35` |
| app build provenance外部seal | `f523dd8f7eee49ec0be8e370c324fa5392bf67f34e2eec40103f303c2771a10d`。packaged provenanceが一致 |
| component lock実ファイル | SHA256 `b5a91738660e2f4c620d0994a6cea44c1661cd41c030c79576d42a52a903fd1f`（CIのCRLF bytes） |
| 2回のclean build | semantic SHA256 `4be414796959ed7f95843b2e99414b36579836eb451749b23e0ca1c4fa57dd22`が一致。wheel全体のbyte一致はfalse |
| IPPとnative構成 | `WITH_IPP=OFF`、`BUILD_IPP_IW=OFF`、IPP build情報なし、G-API/ADEなし。dynamic CRTとFFmpeg bindingを既存validatorで検証 |
| compiler入力 | 8ファイルのsize/SHA256がlockに一致。完全なnative入力のhermetic性は主張しない |
| app実行・installer | packaged self-check exit 0、synthetic sync marker成功、installer content auditとfailure isolation成功 |
| 配布物static Runtime監査 | `--enforce-external --require-qt-system-icu`がexit 0。app-local/hashed/unknown Runtimeを許可しない |

取得した73個の対応ソースarchiveは、総入力サイズ`599644349` bytes、全archiveの
size/SHA256が旧candidate lockに一致した。private source partは73 archives、
`599706604` bytes、SHA256
`64e68b8be3b018bdabfd14b6912fd97cc5ed0770bc5a83e686477350bdc1cc32`。
これは取得・private preparationの証跡であり、公開の記録ではない。

OpenCV coreとPython bindingの入力はcomponentの既存2 archiveと同一である。
producer provenanceの許可input manifestにも次の3 source archiveが同じsize/SHA256で記録され、
他8個は固定build tool wheelである。

| producer source入力 | component source lockとの対応・SHA256 |
| --- | --- |
| `opencv-python-6cb94ce.tar.gz`（1129895 bytes） | `opencv-python.source_archives`。`dcd464bfc0f6f44284fccc690a9933ef93b2604d50fcaea39f7e09a4c19bb2be` |
| `opencv-b4c5ec4.tar.gz`（95448014 bytes） | `opencv-python.source_archives`。`4f2a381ce3377e6f22d0718fe7101e97a21968a21c81f0356f2787bb21024dc4` |
| `opencv-3rdparty-d82ad9a.tar.gz`（22774795 bytes） | `opencv-ffmpeg.source_archives`。`155c5b0674cbcd2da0b07aaff5046d74de09ec152931506f9d36f071c3f87bb5` |

`opencv-3rdparty-d82ad9a.tar.gz`は既存`opencv-ffmpeg` componentのsource archiveにも固定され、
wheelに収録するFFmpeg DLLとその対応するFFmpeg/libvpx/libaom/OpenH264 source・noticeの
既存bindingを維持する。追加のproprietary IPPICV binaryを対応ソースとして扱わない。
その他のnative componentのsource/notice/runtime gatesも変更しない。

FFmpeg DLLのinput `opencv_videoio_ffmpeg_64.dll`とwheel内
`cv2/opencv_videoio_ffmpeg4130_64.dll`は28578304 bytes、SHA256
`fcc614672159094a35815b7ea2819a648cbf86c30d90c57e8c6ba1432f594548`で一致した。
core側のnative生成物はOpenCV source配下の42 C/C++ projectについてReleaseのdynamic CRTを
検証し、PE inventoryとnative importsを既存の厳格な許可集合に比較した。

## Active metadataと旧wheel履歴

active `opencv-python`の`source_status=verified_corresponding_source`と
`native_source_coverage_verified=true`は上記のsource-built recipe、入力source、native inventory、
IPP除去、sealed consumerの技術証跡を表す。`release_legal_review_required=false`は
旧publisher wheelにstatic linkされたproprietary IPPのsource例外を、現在のIPP-free入力に
要求しないという意味である。外部弁護士レビューの完了や法的適合の断定を意味しない。
[管理者決定](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/issues/54#issuecomment-5538215910)
に従い、外部弁護士レビューは未実施として開示を維持する。

旧`wheel_build_evidence`、`vendored_binary_input`、未完のsource/exception/legal状態は
`upstream_wheel_history`に保存する。`binary_archive`は既存validatorが参照する固定済み
publisher wheelの基準metadataとして残るが、source-built policy下のinstall selectorには使用しない。
旧publisher wheelのIPP/runtime検査を履歴参照へ移し、既存assertionは維持する。

active source宣言が一つでも採用された状態で、source-built policyが欠落・null・不正なら
Release gateは必ず拒否する。policyが存在する場合も、source-built directory/payload/provenanceが
欠ければ旧wheelへfallbackしない。producer seal、same-run artifact/commit、実wheel audit、
installed inventory、packaged provenanceを比較する既存経路を維持する。

## 最終統合と未完の受入

この変更でcomponent lock全体のhashとcommit/treeが変わる。旧runのproducer wheel、app、
installer、provenance、source partを、新commitの正式成果物として流用してはいけない。
旧runはこの技術宣言の根拠に限る。既存のsource cache bytesを再利用する場合も、
新lockからinventoryとsource partを再生成し、size/SHA256とindexを再検査する。

PR #140のmerge後に、このIssue #54の最小差分をmainへ載せ直す。最終統合候補の
新しい同一run・同一commitでproducer、app build、packaged self-check、installerを生成し、
外部sealとsource/native/runtime監査をやり直す。Windows buildはこのローカルmetadata差分では
未実行であり、旧runの成功を最終候補の成功に置き換えない。

compiler policyの8 hash、semantic基準`4be414...`、PEの32 bytes限定比較、recipe、
native/runtimeコード、配布機能は変更しない。完全なbyte rebuildや過去の不一致原因の
確定を追加で主張しない。

clean Windows 11 VMと実LoL録画・再生の受入は未実施。Runtime不存在、complete corresponding
source、notice、説明不能なnative binary不存在、独立レビューMust 0を維持し、その結果と
管理者最終承認が揃うまでIssue #54をcloseしない。Issue closeと公開Releaseは別判断とする。
