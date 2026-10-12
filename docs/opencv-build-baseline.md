# OpenCVビルド入力と比較基準の更新

関連: [#139](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/issues/139)、
[#140](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/pull/140)。
2026年9月30日時点の修正候補。正式成果物の受入完了を意味しない。

## 観測した入力差

`14.44.35207` はMSVCの配置ディレクトリ名であり、実ファイルの固定にはならない。
保存した実OpenCVの `getBuildInformation()` とOBJのCodeView `S_COMPILE3` は、
次のcompiler patchを記録している。

| CI run | 実compiler | 比較対象のsemantic SHA256 |
| --- | --- | --- |
| [34443406642](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/actions/runs/34443406642) | 19.44.35228.0 | `2826ea102992dd29be108df17d0d95c0a8a81733df49b9b6cb02378ec51972ff` |
| [35832950746](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/actions/runs/35832950746) | 19.44.35228.0 | `ece4d1d3c74b3727b6368d3084ec90991a1bbd415d372370b18919d76caca538` |
| [36561616218](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/actions/runs/36561616218) | 19.44.35229.0 | `4be414796959ed7f95843b2e99414b36579836eb451749b23e0ca1c4fa57dd22` |

9月23日と29日では `cl.exe`、`c1.dll`、`c1xx.dll`、`c2.dll`、`link.exe`、
`lib.exe`、`mspdbcore.dll`、`mspdbsrv.exe` の8ファイルすべてのSHA256が変わった。
ソースarchive、生成された42個のcompile project、保持されたCL command tlog、
buildの絶対作業パスは一致している。

保持された6個のOBJではcode sectionとrelocation bytesが一致した。
一方、匿名namespace ID、関連するdata section、compiler metadata、`.chks64` は変わった。
これは当該6個の比較であり、全OpenCVの同等性や、過去の不一致の原因確定を示さない。

旧成功runと9月23日のrunは、保存されたcl hashやbuild informationまで一致する。
旧成功時のbackend DLLの実ハッシュは残っていないため、この差までpatch更新だけで
説明したとは扱わない。旧入力の復元ができた、という記録にも置き換えない。

## 修正の範囲

`compliance/components.json` の `build_environment.msvc_tools` で、19.44.35229側の
観測済み8ファイルのsize/SHA256を明示的に採用する。
事前に選んだVisual Studio instanceを `CMAKE_GENERATOR_INSTANCE` でCMakeへ渡し、
そのinstanceの `Hostx64/x64` 入力をbuild前に照合する。build後にも、生成されたC/C++
compiler・linker・archiverのパスと8ファイルを再照合し、証跡の検証でもpolicyとの一致を要求する。

この修正は選択した8ファイルの無記録の更新を防ぐもの。その他のcompiler helpers・
言語resources、SDK、headers、static librariesまでを固定した完全な環境としては扱わない。
実際にロードされたDLLのidentityもこのreceiptだけでは証明しない。
これらがnative出力を変えれば、既存の厳密なsemantic比較で停止する。

比較基準は、旧入力を復元するのではなく、上記の明示的なcompiler入力への移行として
`4be414…` を候補にする。run 36561616218の2回のクリーンビルドはこの値で一致した。
8file receiptも両方で一致する。候補修正後の正式CIで改めて2回buildし、同じ基準を
満たすまで採用完了とは扱わない。失敗runのwheelや証跡を正式成果物へ流用しない。

IPP/G-API/ADE除去、Limited API、dynamic CRT、対応ソース、FFmpeg binding、未知native、
Runtime非同梱、アプリ側の同一run/commit/artifact検査は維持する。
PE比較で除く範囲も、既存のtimestampとRSDS GUIDの32 bytesから増やさない。

## 証跡の識別

run 36561616218の静的比較元は次のarchive。hashは取得したarchiveの完全性を識別する。
compiler backendの実際のロード履歴を証明するものではない。

| artifact ID | size | archive SHA256 |
| --- | ---: | --- |
| 11031187179 | 915805 | `c2cadb74b1928001c791f0b132b2ca35fa5334cd3a65245ec273f5b77bc6c491` |
| 11031287076 | 78485225 | `f88483a020fb10957358b517c2286d4ac7dd04f264fb833a0f3e67f941e0dd0e` |

8file receiptは `build-diagnostics.json`、compiler patchは実 `build-information.txt` と
保持されたOBJ、比較値は両方の `*-build-evidence.json` から確認する。
正式producerは新しいbuildでreceiptを検査・封印し、consumerもそのreceiptを検査する。

## 次の判定

1. 独立レビューで、入力選択と固定値移行の根拠、従来の検査の維持を確認する。
2. 正式CIのOpenCV producer、Windowsアプリbuild、packaged self-check、installer監査を通す。
3. 同じrun/commitから得た成果物を [#54](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/issues/54)
   のclean Windows 11 VM・実LoL受入へ渡す。実機受入前に配布可能とは判定しない。

10月11日前後までに現在の方式で解消の見通しが立たない場合は、固定Windows build VMの
実現性確認へ計画を更新する。期待値の追従更新やPE除外の拡張を繰り返して通さない。

instance指定の挙動は [CMake 3.31の公式文書](https://cmake.org/cmake/help/v3.31/envvar/CMAKE_GENERATOR_INSTANCE.html)
に従う。generator/toolsetと実ファイルの検査は両方必要になる。

## 2026年10月12日の継続調査

[run 36705978374](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/actions/runs/36705978374)
は上記8ファイル固定後のproducer・app・installerまで成功した。一方、対応ソース採用候補の
[run 36728221228](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/actions/runs/36728221228)
は、同じrun内の2回のclean buildが一致した後、固定semantic policyとの不一致で停止した。
app buildは実行されていない。成功runの受入結果をこの失敗runへ転用しない。

失敗runのsemantic SHA256は `ed37eb3a921751bc32da1ea7a3b2c07a316ce78d46a3a4314763ae27bae0c286`。
旧成功とのsemantic差はcv2の比較用SHA256を記録する2項目だけで、8ファイルの実hash、
固定入力、生成compile projectは一致する。runner imageの更新は観測した相関であり、
原因の証明ではない。期待値の差し替えやPE除外範囲の拡張は行わない。

保存wheelを実行せず旧成功と失敗runのcv2を比較し、失敗runの新しいPDBのsection contributionへ
対応付けたところ、コード領域の差は8関数とその周辺にあり、4件が
`opencv_dnn.dir/Release/topk_layer.obj`、4件が `libprotobuf.dir/Release/message.obj` に属する。
RTTIなどの文字列にも匿名namespaceとlambdaの識別子の差がある。旧成功のOBJ/PDBは保存されて
いないため、旧側の関数名、全コードの同等性、識別子の変化を生んだ入力はまだ確定できない。

次の診断では上記2個のRelease OBJを既存の限定保存対象へ追加する。他のOBJ、Debug生成物、
環境変数一覧やbuild binlogは保存対象へ追加しない。診断のサイズ・件数・redirect拒否と、
本来のbuild失敗を診断失敗で置き換えない処理は維持する。正式provenanceとReleaseゲートは
この診断追加で変更しない。topkはPCHを利用するため、header入力を調べる場合は当該TUだけでなく
PCH作成元も確認する。

固定ビルドVM案は独立したbuild専用VMとして実現性を評価する。既存のRuntime受入VMは転用しない。
固定versionのVS/SDK取得物と実入力の封印、offlineでの2回のclean build、同runのconsumerへの接続が
必要であり、旧成功環境を復元できたとは扱わない。配布日は原因解消と最終受入後に確定する。

### 匿名namespaceの共通seed

続く静的解析では、保存tlogのsource/OBJ対応を使うと、46個すべての匿名namespace IDが
次の式で旧側・新側とも一致した。PCH使用の21個は末尾byteが1、非使用の25個は0で一致する。

```text
ID = crc32(lowercase absolute source path UTF-8 + PCH byte, seed) XOR FFFFFFFF
old seed = 48631799
new seed = 555ef44e
```

これは保存されたpath・PCHフラグ・IDへ完全に当てはまるモデルであり、seedを生成した入力を
当該runで直接観測した証明ではない。旧新の完全job logにもコンピューター名は記録されていなかった。

ホストに既存の別版MSVC 19.34で、headerを使わない小さなC++をcompileだけして調べたところ、
独立した4個目のsource pathも含め、実OSのコンピューター名のUTF-8 bytes（NULなし）のCRC32が
seedとなる式に一致した。子processの`COMPUTERNAME`だけの置換や`/Brepro`ではIDは変わらなかった。
これを対象MSVC 19.44へ一般化せず、生成OBJのlink・実行や正式recipeのflag変更は行わない。

次の正式producerで`GetComputerNameW`を読み取り、そのCRC32と取得方法だけを診断へ記録する。
生のコンピューター名は保存しない。API失敗・空値・容量不整合は診断エラーとして記録し、元の
build失敗を置き換えない。非Windowsでは未観測とする。このfieldは限定診断だけに追加し、
正式provenance・semantic policy・アプリの公開API/録画JSONへ変更しない。新しいOBJのIDから得た
seedとの直接照合が成立するまで、ホスト名を原因確定や修正完了として扱わない。

### 対象MSVC 19.44の直接照合と次の限定実験

[run 38202319340](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/actions/runs/38202319340)
の実checkoutは、head `8dd4fcd` とmain `987a240` のmerge commit `d0967c2`。
A/B両診断の実 `GetComputerNameW` CRCは `1eeb6040` で、各46個のnamespace IDを
source path・PCH byteからforward計算するseedとすべて一致した。対象compilerでも、
記録された識別子と実OSホスト名CRCの直接照合が成立した。

A/B semantic SHA256はともに `59c18bd1d60c7fdd975bfe4e314a376980f47af22d2fb84bd1ddad980169ef16`。
直前run 38200049047と完全一致し、旧成功policyとの差はcv2比較用SHA256の2項目のみ。
producerは固定policyとの差で停止し、app・self-check・installerは実行されていない。
旧成功時のホスト名・OBJ/PDBは未保存で、全コードの同等性と実loaded tool identityまでを
証明したとは扱わない。今回も期待hash・PE除外範囲は変更しない。

別版MSVC 19.34の小さなcompile-only実験では、`/experimental:deterministic` が警告D9002なしで
受理され、匿名namespace IDのseedが実ホスト名CRCから0へ変わった。lambda IDは同一だった。
flagの存在は [Microsoftの17.1 release notes](https://learn.microsoft.com/en-us/visualstudio/releases/2022/release-notes-v17.1)
にも記載されるが、19.44でのホスト名依存除去の仕様・保証を確認したものではない。

次は固定8ファイルを照合した対象MSVCで、固定した小さなTUのsource path・PCH条件と
control／flagありを比較する独立診断を行う。生成物はlink・実行せず、ホスト名CRCと限定した
source／OBJ／compiler receiptを保存する。通常CIのtests・producer・appの依存と検査は維持する。
診断の成功を製品wheelの証拠や配布可能判定として使用しない。対象実験と正式2cleanbuildを
確認するまで、flagを正式recipeへ追加しない。

専用Server VMのライセンス・ISOは未所持。既存PCへの固定Build Tools／SDK導入案と公式取得物は
準備したが、共有Installer・SDK・Runtimeへの影響があるため未実行とし、先に上記診断を行う。
既存のRuntime受入VMと10月10日に完了した受入結果は保持する。
