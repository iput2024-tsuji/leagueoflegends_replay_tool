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
