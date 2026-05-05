# LoL Replay Tool

LoL Replay Tool は、League of Legends の試合を自動録画し、試合イベントを JSON として蓄積し、そのデータから戦術インサイトを抽出する Windows 向けの意思決定支援ツールです。

単なる「LoL の自動録画ソフト」ではなく、録画・イベント同期・リプレイ閲覧・データ分析を一体化し、蓄積されたプレイデータから scikit-learn の決定木モデルを使って「勝利の方程式」や「敗北しやすいパターン」を自動抽出することを目指しています。

## Riot Games 免責事項

LoL Replay Tool is not endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.

このツールは League of Legends のローカル API を利用しますが、Riot Games 公式の製品・サービスではありません。

## バリュープロポジション

LoL の振り返りは、動画を見返すだけでは「何が勝敗に効いていたのか」を定量的に把握しづらいという課題があります。このツールは以下を自動化します。

- LoL の試合開始・終了を検知し、OBS を制御して録画する
- Riot LCU API から取得したイベントを JSON として保存する
- 動画とイベントを同期し、キル・デス・オブジェクトイベントへジャンプできるプレーヤーを提供する
- 複数試合の JSON を pandas で集約し、scikit-learn の決定木で勝敗に影響しやすい条件をルール化する

これにより、プレイヤーは「15分以内のヴォイドグラブ取得数」「序盤タワー破壊」「ファーストブラッド」「敵チャンピオン構成」などの特徴量から、勝率に結びつきやすい戦術パターンを確認できます。

## 主な機能

- メイン画面起動時のバックグラウンド自動監視
- ポータブル OBS の自動起動・録画制御
- Riot LCU API からの試合状態・イベント取得
- 録画動画と JSON イベントログの保存
- mpv ベースのリプレイプレーヤー
- イベントリストからのシーク再生
- 同期補正 UI
- リプレイ一覧画面でのチャンピオン・勝敗表示
- データ分析画面での勝率サマリーと戦術インサイト表示
- 設定画面からの保存先、FPS、音声デバイス、容量制限の管理
- pytest による非同期処理・外部接続層の単体テスト

## Tech Stack

| 技術 | 用途 |
| --- | --- |
| Python | アプリ全体の実装言語 |
| PyQt6 | GUI、画面遷移、タスクトレイ、設定画面、分析画面 |
| QThread | UI をブロックしないバックグラウンド監視・分析処理 |
| asyncio | 録画監視ワーカー内の非同期イベントループ管理 |
| aiohttp | Riot LCU API への非同期 HTTP 通信 |
| obsws-python | OBS WebSocket 経由の録画制御・シーン設定・音声設定 |
| python-mpv | 録画動画の再生エンジン |
| OpenCV | 同期マーカー検出 |
| pandas | JSON ログのフラット化、集計、特徴量生成 |
| scikit-learn | MultiLabelBinarizer による敵チャンピオン特徴量化、DecisionTreeClassifier による戦術ルール抽出 |
| pytest | 単体テスト、非同期処理テスト |
| unittest.mock / AsyncMock | Riot API や OBS WebSocket を起動しないモックテスト |
| PyInstaller | Windows 向け配布ビルド |

## システムアーキテクチャのハイライト

### UI と非同期 I/O の統合

録画監視は GUI スレッドとは独立した `RecorderWorker` 上で実行します。`QThread` の `run()` 内で `asyncio.new_event_loop()` により専用イベントループを生成し、Riot LCU API と OBS WebSocket への通信を非同期で処理します。

この構成により、以下を両立しています。

- PyQt6 の GUI をフリーズさせない
- 試合開始・終了の監視をバックグラウンドで継続する
- Ctrl+C やタスクトレイ終了時に停止シグナルを安全に伝播する
- `time.sleep()` に依存しないレスポンシブな終了処理を実現する

### 保守性とテスト容易性: DI と MVC

初期実装では、OBS 制御、Riot API 通信、録画セッション管理、UI 操作が密結合になりやすい構造でした。現在は責務を分離し、外部依存を注入可能にしています。

主な分離方針:

- `OBSClient`: OBS WebSocket 通信と OBS 制御のみを担当
- `RiotAPIClient`: LCU API からのデータ取得とパースのみを担当
- `RecordingSessionManager` / `LoLAutoRecorder`: 録画ワークフローをオーケストレーション
- `controllers.py`: UI から呼び出される設定、音声、分析、録画のコントローラー層
- `app.py`: PyQt6 の画面表示とユーザー操作に集中
- `analytics.py`: JSON から分析用 DataFrame と ML 用特徴量を生成

DI により、OBS や LoL クライアントを実際に起動せずにクライアント層をモックできます。これにより、ネットワーク異常や状態遷移のテストが現実的になっています。

## データ分析パイプライン

録画された試合は JSON として保存され、その後 `GameDataAnalyzer` によって分析可能な形式へ変換されます。

```text
Riot LCU API
  -> 試合情報・イベント取得
  -> JSON 保存
  -> pandas DataFrame へフラット化
  -> 15分以内のイベントを特徴量化
  -> enemy_champions を MultiLabelBinarizer でダミー変数化
  -> scikit-learn DecisionTreeClassifier で学習
  -> 勝利の方程式 / 敗北パターンを自然文ルールとして抽出
  -> PyQt6 の分析画面に表示
```

現在の特徴量例:

- `horde_kill_15m`: 15分以内のヴォイドグラブ取得数
- `own_building_kill_15m`: 15分以内の自チームのタワー破壊数
- `first_blood`: ファーストブラッド取得の有無
- `enemy_Darius`, `enemy_Aatrox` など: 敵チームに特定チャンピオンがいるかどうか

決定木から抽出される表示例:

```text
勝利の方程式: 15分以内HordeKill >= 2 AND 敵にDariusがいない -> WinRate 85% (n=13)
敗北のパターン: ファーストブラッド取得なし AND 敵にAatroxがいる -> WinRate 22% (n=9)
```

## テスト戦略

このプロジェクトでは、実際の OBS や LoL クライアントを起動しなくても重要なロジックを検証できるようにしています。

- `pytest` による単体テスト
- `unittest.mock.AsyncMock` による非同期 API クライアントのモック
- Riot LCU API サーバー未起動時のハンドリング検証
- OBS WebSocket の切断・タイムアウト時の例外処理検証
- `GameEnd` イベントを受信して録画停止・JSON 保存に進む非同期フローの検証
- pandas / scikit-learn による特徴量生成と決定木ルール抽出の検証

実行例:

```powershell
pytest tests
```

## ディレクトリ構成

```text
src/
  app.py             # PyQt6 GUI、画面遷移、タスクトレイ、RecorderWorker
  recordtest.py      # 録画ワークフロー、OBS/Riot API クライアント、設定モデル
  player.py          # mpv ベースのリプレイプレーヤー
  analytics.py       # JSON 分析、特徴量生成、決定木インサイト抽出
  controllers.py     # UI とバックエンド処理を分離するコントローラー層
  app_paths.py       # 実行環境ごとのパス解決
config/
  setting.sample.json
recordings/
  json/              # 試合ログ JSON
assets/
  app/               # アプリアイコン
  champions/icons/   # チャンピオンアイコン
bin/
  *.dll              # 利用者が配置する mpv DLL
  ffmpeg.exe         # 利用者が配置するクリップ出力用FFmpeg
obs-portable/
  bin/64bit/obs64.exe # 利用者が配置するポータブル OBS
tests/
  test_analytics.py
  test_recorder_async.py
  test_recordtest_clients.py
```

## 必要環境

- Windows
- Python 3.10+ 推奨
- OBS Studio ポータブル版
- mpv DLL
- FFmpeg (`bin/ffmpeg.exe`)

このリポジトリおよびビルド成果物には、OBS Studio 本体、mpv DLL、Riot Games の画像アセットを同梱しません。

- OBS Studio は利用者が公式配布元から取得し、`obs-portable` に配置してください。
- mpv DLL は利用者が正規の配布元から取得し、`bin/` に配置してください。
- FFmpeg は利用者が正規の配布元から取得し、`bin/ffmpeg.exe` に配置してください。システム PATH 上の FFmpeg には依存しません。
- チャンピオンアイコンを使う場合は、利用者が `assets/champions/icons` に配置してください。Riot Games のアセットを利用する場合は、Riot Games の規約・ポリシーに従ってください。

## セットアップ

```powershell
pip install -r requirements.txt
copy config\setting.sample.json config\setting.json
python main.py
```

通常は `config/setting.json` を手動編集する必要はありません。

- OBS は `obs-portable` に配置されたポータブル版のみ利用します
- ユーザー環境にインストール済みの OBS は利用しません
- 起動時に `obs-portable/obs_portable_mode.txt` と OBS の `global.ini` を自動生成・補正します
- 初回セットアップで「環境を自動修復」を実行すると、WebSocket、シーン、同期用色ソースを自動構成します
- 音声デバイス、録画保存先、FPS、容量制限はアプリの設定画面から変更できます

`config/setting.json` は `.gitignore` 済みです。

## 使い方

### 統合アプリ

```powershell
python main.py
```

- アプリ起動後、メイン画面表示と同時に LoL の試合監視を開始します
- 試合開始を検知すると OBS 録画を開始します
- 試合終了後、録画停止と JSON 保存を行います
- リプレイ画面から過去試合を選択してイベント同期再生できます
- 分析画面から勝率サマリーと戦術インサイトを確認できます

### JSON の保存先

JSON は既定で `recordings/json/` に保存されます。

```text
lol_YYYYMMDD_HHMMSS.json
```

動画ファイル名は JSON の `obs_record_path` に保存され、プレーヤー側で `paths.recordings_dir` から再解決します。

## 配布用ビルド

Windows 向けに PyInstaller の `onedir` 形式でビルドします。

```powershell
pip install pyinstaller
.\scripts\build.ps1
```

出力先:

```text
dist\LoLReplayTool\
```

注意点:

- `config/setting.json` は初回起動時に自動生成されます
- OBS Studio 本体はビルド成果物へコピーされません
- mpv DLL はビルド成果物へコピーされません
- FFmpeg はビルド成果物へコピーされません
- チャンピオンアイコンはビルド成果物へコピーされません
- `assets/app/app.ico` が存在する場合、exe アイコンとウィンドウアイコンに反映されます

## トラブルシュート

- `ポータブルOBSが見つかりません`
  - `obs-portable/bin/64bit/obs64.exe` が存在するように配置してください。
- `mpv DLL が見つかりません`
  - `bin/` に `mpv-1.dll`, `libmpv-1.dll`, `mpv-2.dll`, `libmpv-2.dll` のいずれかを配置してください。
- `FFmpegが見つかりません`
  - `bin/ffmpeg.exe` を配置してください。クリップ出力はシステム PATH の FFmpeg を使用しません。
- イベントが表示されない
  - JSON の `events` / `events_all` を確認してください。
- 分析結果が表示されない
  - 勝敗が判定できる JSON が複数件あるか確認してください。
  - 決定木分析には勝敗両方を含むデータが必要です。
- 同期が合わない
  - 設定画面から同期補正を行うか、「環境を自動修復」を再実行してください。

