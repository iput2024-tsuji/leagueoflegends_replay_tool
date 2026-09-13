# TFTの録画除外

関連: [Issue #143](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/issues/143)

## 判定材料

2026-09-14に確認したRiotの一次資料を使用する。実行時に新しい外部APIへの通信やcatalogの自動取得は追加しない。

- [Riot TFT資料のQueues節](https://developer.riotgames.com/docs/tft#queues) は、Data DragonのTFT queue catalogを案内している。
- [16.18.1の公式catalog](https://ddragon.leagueoflegends.com/cdn/16.18.1/data/en_US/tft-queues.json) に掲載されたIDは1090、1100、1110、1130、1160、1170、1210、1220、6000、6100、6120、6130。1210のqueueTypeは`CHONCC_TREASURE`で、TFTという文字列を含まない。
- [13.24.1の公式catalog](https://ddragon.leagueoflegends.com/cdn/13.24.1/data/en_US/tft-queues.json) の1180・1190と、[LoL公式queue一覧](https://static.developer.riotgames.com/docs/lol/queues.json) の1111も既知のTFTとして扱う。
- 既存のLCU session/catalogまたはLive Clientから取得できた`game_mode == TFT`と、`queue_type`のアンダースコア区切りにある`TFT`も分類材料にする。表示名や数値範囲から推測しない。LCU queue/catalogの`gameMode`も既存metadataへの補完対象にする。

公式catalogのqueue IDは判定根拠だが、[LCUは第三者向けに正式サポートされたAPIではない](https://developer.riotgames.com/docs/lol#league-client-api)。この調査だけで全TFTモードの網羅性、現在のLCU応答の形、実LoL/TFTでの動作を証明したとは扱わない。

## 待機と開始

GUIとCLIが共有する`LoLAutoRecorder.wait_for_game_start_async()`で判定する。TFTなら録画開始へ進まず、監視を継続する。記録前のBan/Pickデータも破棄し、監視停止時に不要なsession JSONを保存しない。OBSの起動・所有権・終了処理は変更しない。

TFTと判定済みの同一試合では、情報の一時欠損や別情報源との分類矛盾だけで除外を解除しない。別game IDまたは試合前のphaseへの変化を確認したときに古い分類を破棄する。同じLobby等の応答を繰り返しても解除しない。新旧どちらにもgame IDがない場合は、分類を取得した同じ情報源でqueueやgame modeが変化したことを新しい分類の根拠として扱う。片方だけのID欠損は新試合と判断しない。LCU sessionと専用phaseの両経路、およびLive Clientだけで次の試合を検出する場合を検証する。

LCU開始検知後のLive Client待機を終えた時点でも、LCUのphaseとmetadataを再取得する。猶予中にTFT情報が届いた場合は除外し、Lobby等へ戻った場合は古い開始phaseで録画しない。

すべての分類情報が不明な場合は、通常LoLの既存の開始条件を維持する。Live Clientの有効なgameTime、または既存の猶予を経たLCU開始phaseで録画するため、分類情報を取得できないTFTまで除外できる保証はない。新しいLoLモードを一律に録画禁止にしないための互換性上の境界として扱う。

## 検証

自動テストの応答は合成fixtureであり、ユーザーの実試合から採取したものではない。既知queue、種別のみ、未知・欠損、TFTからLoLへの遷移、同一game IDの矛盾、両phase経路、Live Clientのみ、猶予後の分類到着・phase変更、キャンセルを確認する。GUI/CLIから開始・通知・保存へ進まないことも確認する。

Ruff、全pytest、外部runtime policy、Windows buildとpackaged self-checkに加え、[手動チェックリスト](manual-test-checklist.md)の実LoL/TFT確認を行う。実試合開始前には管理者へ交代し、利用者の録画を削除しない。実応答を保存する場合は分類・phaseに必要な項目だけを残し、認証情報・個人情報を含めない。

実クライアントでの応答確認と受入試験は、合成fixtureやCI成功で代替しない。
