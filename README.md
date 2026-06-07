# LoL Replay Tool

LoL Replay Tool は、League of Legends の試合を自動録画し、試合イベントを JSON として蓄積し、そのデータから戦術インサイトを抽出する Windows 向けの意思決定支援ツールです。

単なる「LoL の自動録画ソフト」ではなく、録画・イベント同期・リプレイ閲覧・データ分析を一体化し、蓄積されたプレイデータから scikit-learn の決定木モデルを使って「勝利の方程式」や「敗北しやすいパターン」を自動抽出することを目指しています。

## ダウンロード

[GitHub Releases](https://github.com/iput2024-tsuji/leagueoflegends_replay_tool/releases/latest)から最新版の`LoLReplayTool-Setup-*.exe`をダウンロードできます。

OBSとFFmpegは必要になった時点でアプリが自動取得します。リプレイ再生に必要なmpv DLLは同梱していないため、利用者が別途入手し、`%LOCALAPPDATA%\LoLReplayTool\bin`へ配置してください。Releaseに添付される`SHA256SUMS.txt`でインストーラーの整合性を確認できます。

## Riot Games 免責事項

LoL Replay Tool is not endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.

このツールは League of Legends のローカル API を利用しますが、Riot Games 公式の製品・サービスではありません。

Ban/Pick取得にはLeague Client API（LCU）を利用します。LCUはRiot Gamesによる正式なサードパーティ向けサポート対象ではないため、クライアント更新によって取得仕様が変わる可能性があります。

## バリュープロポジション

LoL の振り返りは、動画を見返すだけでは「何が勝敗に効いていたのか」を定量的に把握しづらいという課題があります。このツールは以下を自動化します。

- LoL の試合開始・終了を検知し、OBS を制御して録画する
- Riot LCU API から取得したイベントを JSON として保存する
- チャンピオン選択中のBan/Pick順、チャンピオン、味方・敵、担当位置をJSONへ保存する
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
| Inno Setup | Windows 向けインストーラー、更新、アンインストール |

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
bin/
  *.dll              # 開発実行時に利用者が配置する mpv DLL
  ffmpeg.exe         # 開発実行時のクリップ出力用FFmpeg
obs-portable/
  bin/64bit/obs64.exe # 開発実行時のポータブル OBS
installer/
  LoLReplayTool.iss  # Inno Setup 定義
tests/
  test_analytics.py
  test_recorder_async.py
  test_recordtest_clients.py
```

## 必要環境

- Windows
- Python 3.14（CI と配布ビルドの検証対象）
- mpv DLL

このリポジトリおよびビルド成果物には、OBS Studio 本体、mpv DLL、Riot Games の画像アセットを同梱しません。

- OBS Studio は初回起動時に固定バージョンを自動取得し、開発実行時は `obs-portable`、配布版では `%LOCALAPPDATA%\LoLReplayTool\obs-portable` に配置します。
- mpv DLL は利用者が正規の配布元から取得し、開発実行時は `bin/`、配布版では `%LOCALAPPDATA%\LoLReplayTool\bin` に配置してください。
- FFmpeg は初回クリップ出力時に固定バージョンを自動取得し、開発実行時は `bin/ffmpeg.exe`、配布版では `%LOCALAPPDATA%\LoLReplayTool\bin\ffmpeg.exe` に配置します。システム PATH 上の FFmpeg には依存しません。
- Riot Games のチャンピオンアイコンは同梱せず、自動ダウンロードも行いません。

## セットアップ

```powershell
pip install -r requirements.txt
copy config\setting.sample.json config\setting.json
python main.py
```

直接依存は `requirements.in` / `requirements-dev.in`、固定済み依存は `requirements.txt` / `requirements-dev.txt` で管理します。Python や PyQt の更新時は `.in` ファイルを基準にロックファイルを再生成してください。

通常は `config/setting.json` を手動編集する必要はありません。

- OBS は `obs-portable` に配置されたポータブル版のみ利用します
- ユーザー環境にインストール済みの OBS は利用しません
- 初回起動時、メインウィンドウ表示前にGUIブートストラッパーがOBSを自動取得します
- FFmpegはクリップ出力を初めて実行した時点で必要な場合のみ取得します
- 同時起動を抑止し、セットアップ処理もプロセス間ロックで直列化します
- ダウンロードは接続待ちと全体時間に上限を設け、失敗時はミラーへ切り替えて再試行します
- ダウンロード対象は固定バージョンで、SHA256ハッシュ検証に失敗したファイルは展開しません
- 起動時に `obs-portable/obs_portable_mode.txt` と OBS の `global.ini` を自動生成・補正します
- OBS WebSocket は初回設定時にローカル用パスワードを自動生成し、認証必須で構成します
- 初回セットアップで「環境を自動修復」を実行すると、WebSocket、シーン、同期用色ソースを自動構成します
- 音声デバイス、録画保存先、FPS、容量制限はアプリの設定画面から変更できます

`config/setting.json` は `.gitignore` 済みです。配布版では設定、録画、ログ、OBS/FFmpeg などの可変データを `%LOCALAPPDATA%\LoLReplayTool` に保存します。旧配布フォルダ内の `config/setting.json`、`obs-portable`、`bin/OBS-Studio` は初回起動時に新しい保存先へコピー移行されます。

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

JSON は既定で、開発実行時は `recordings/json/`、配布版では `%LOCALAPPDATA%\LoLReplayTool\recordings\json\` に保存されます。

```text
lol_YYYYMMDD_HHMMSS.json
```

動画ファイル名は JSON の `obs_record_path` に保存され、プレーヤー側で `paths.recordings_dir` から再解決します。

チャンピオン選択を取得できた試合では、JSONの`ban_pick`に以下を保存します。

- `actions`: 実行順、Ban/Pick種別、味方・敵、チャンピオンID・名称、担当位置
- `teams`: チャンピオン選択終了時点の味方・敵チーム構成
- `local_player_cell_id`: 自分のチャンピオン選択スロット
- `last_phase`: 最後に取得したチャンピオン選択フェーズ

Hover中の未確定チャンピオンは保存せず、確定したアクションだけを記録します。Dodge後に別のチャンピオン選択が始まった場合は、古い履歴を破棄して実際に開始した試合の履歴へ切り替えます。

## 配布用ビルド

Windows 向けにPyInstallerの`onedir`形式でビルドします。依存ファイルは`_internal`へまとめ、配布ルートには実行ファイルと第三者ソフトウェアの注意事項だけを配置します。

```powershell
pip install pyinstaller
.\scripts\build.ps1
```

出力先:

```text
dist\LoLReplayTool\
  LoLReplayTool.exe
  THIRD_PARTY_NOTICES.md
  _internal\
```

注意点:

- `config/setting.json` は初回起動時に自動生成されます
- OBS Studio 本体はビルド成果物へコピーされません
- mpv DLL はビルド成果物へコピーされません
- FFmpeg はビルド成果物へコピーされません
- チャンピオンアイコンはビルド成果物へコピーされません
- 配布版の可変データは `dist\LoLReplayTool` ではなく `%LOCALAPPDATA%\LoLReplayTool` に作成されます
- `assets/app/app.ico` が存在する場合、exe アイコンとウィンドウアイコンに反映されます

## インストーラー

Inno Setup 6をインストールしたWindows環境で、テスト、アプリビルド、自己診断、インストーラー生成を一括実行します。

```powershell
winget install --id JRSoftware.InnoSetup -e
.\scripts\build_installer.ps1
```

出力先:

```text
dist\installer\LoLReplayTool-Setup-0.1.0.exe
```

バージョンは`VERSION`から読み取ります。明示的に変更する場合は`-Version 1.2.3`を指定します。アプリだけを事前ビルド済みの場合は`-SkipBuild`、テスト済みの場合は`-SkipTests`を利用できます。

インストール先は`%LOCALAPPDATA%\Programs\LoLReplayTool`です。管理者権限は不要で、スタートメニューのショートカットとアンインストーラーが登録されます。設定、ログ、OBS、FFmpeg、録画は従来どおり`%LOCALAPPDATA%\LoLReplayTool`に保存されるため、アプリ更新で上書きされません。

アンインストール時は、設定、ログ、ダウンロード済みOBS/FFmpegを削除するか確認します。どちらを選んでも`recordings`フォルダと、設定で指定した外部録画保存先は削除しません。

### GitHub Releaseの公開

`VERSION`を更新してコミットした後、同じバージョンの`v`付きタグをプッシュすると、GitHub ActionsがインストーラーをビルドしてReleaseへ自動公開します。

```powershell
git tag v0.1.0
git push origin v0.1.0
```

タグと`VERSION`が一致しない場合や、テスト・Ruff・ビルドのいずれかが失敗した場合はReleaseを公開しません。

## トラブルシュート

- 配布版の基本診断を行う
  - `LoLReplayTool.exe --self-check` を実行してください。GUIを開かずに設定ファイル、保存先の書き込み、OBS/FFmpeg/mpv配置状況を確認します。
  - OBS、FFmpeg、mpv DLL が未配置でも診断自体は失敗扱いにせず、警告として表示します。
- `ポータブルOBSが見つかりません`
  - 配布版では `%LOCALAPPDATA%\LoLReplayTool\obs-portable\bin\64bit\obs64.exe`、開発実行時は `obs-portable\bin\64bit\obs64.exe` が存在するように配置してください。
- `mpv DLL が見つかりません`
  - 配布版では`%LOCALAPPDATA%\LoLReplayTool\bin`、開発実行時はリポジトリの`bin/`に`mpv-1.dll`, `libmpv-1.dll`, `mpv-2.dll`, `libmpv-2.dll`のいずれかを配置してください。
- `FFmpegが見つかりません`
  - 通常は初回クリップ出力時に自動取得します。手動配置する場合、配布版では `%LOCALAPPDATA%\LoLReplayTool\bin\ffmpeg.exe`、開発実行時は `bin\ffmpeg.exe` を配置してください。クリップ出力はシステム PATH の FFmpeg を使用しません。
- `OBS WebSocketポートが既に使用されています`
  - 通常版 OBS や手動起動した OBS が動いている場合は終了してください。このアプリは `obs-portable` 配下の管理対象 OBS だけを起動・制御します。
- OBS がタスクトレイに表示される
  - 既存の OBS をすべて終了してからアプリを起動してください。管理対象 OBS は起動直前に `global.ini` のトレイ設定を無効化します。
- イベントが表示されない
  - JSON の `events` / `events_all` を確認してください。
- 分析結果が表示されない
  - 勝敗が判定できる JSON が複数件あるか確認してください。
  - 決定木分析には勝敗両方を含むデータが必要です。
- 同期が合わない
  - 設定画面から同期補正を行うか、「環境を自動修復」を再実行してください。

