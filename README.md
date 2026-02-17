# LoL Replay Tool

LoLの試合録画（OBS自動制御）と、イベントログ同期再生プレーヤーをまとめたツールです。

## 主な機能
- 試合開始/終了の自動検知とOBS録画制御
- 重要イベントのログ保存（自分のキル/デス + オブジェクト）
- 録画動画とイベントの同期再生プレーヤー
- 初回セットアップウィザード（OBS検出・接続テスト・自動診断）

## 必要環境
- Windows
- Python 3.10+ 推奨
- OBS Studio（WebSocket 5.x 有効、ポート/パスワード設定）
- mpv の DLL（`bin/` に `mpv-1.dll` / `libmpv-1.dll` / `libmpv-2.dll` のいずれか）

## セットアップ
```powershell
pip install -r requirements.txt
```

設定ファイルを作成:
```powershell
copy config\setting.sample.json config\setting.json
```

`config/setting.json` を編集して以下を合わせてください。
- OBSのパス (`obs.dir`)
- OBS WebSocket のポート/パスワード
- シーン名 (`scene_name`) / 同期用の赤色ソース名 (`source_name`)
- JSON保存先 (`paths.json_dir`)

または、アプリ起動後に表示される「初回セットアップ」で自動検出・保存できます。

※ `config/setting.json` は `.gitignore` 済みです。

## 使い方

### 録画 & ログ保存
```powershell
python src\recordtest.py
```

- 試合開始を検知すると録画開始
- 試合終了で録画停止＆JSON保存
- Ctrl+C で終了（OBSも終了）

JSONは `recordings/json/` に `lol_YYYYMMDD_HHMMSS.json` で保存されます。

### プレーヤー
```powershell
python src\player.py
```

- JSONを選ぶと `obs_record_path` から動画を開きます  
  見つからない場合は JSON と同じフォルダを探します。
- 同期マーカーを検出してイベントとシーク位置を合わせます。

#### キー操作
- Space: 再生/一時停止
- ← / →: コマ戻し/コマ送り
- F: フルスクリーン
- Esc: フルスクリーン解除
- N / P: 次/前のイベント

## 配布用ビルド（PyInstaller）
Windows向けに `onedir` でビルドします（設定ファイルを書き込むため）。

アプリアイコンを設定したい場合は `assets\app\app.ico` を用意してください。

```powershell
pip install pyinstaller
.\scripts\build.ps1
```

出力先: `dist\LoLReplayTool\`
- 初回起動時に `config\setting.json` が自動生成されます
- `config\setting.json` を編集して OBS パス等を設定してください
- 初回起動時はセットアップウィザードで設定できます
- OBS は `bin\OBS-Studio` (ポータブル) を優先して利用します
- シーン/同期用色ソースが不足している場合は起動時に自動作成します
- mpv DLL は同梱せず、利用者が `bin\` に配置します
- ビルド時に `dist\LoLReplayTool\bin\` は空フォルダとして作成されます
- `assets\app\app.ico` が存在する場合、exeアイコンとウィンドウアイコンに反映されます
- OBS 本体は同梱しません（各自でインストール）
- ビルド成果物は `LoLReplayTool.exe` と同じ階層に `config` / `assets` / `bin` が配置されます（`_internal` 非使用）

### アイコン画像の推奨仕様
- 形式: `.ico`（複数サイズ同梱）
- 推奨サイズ: `16x16`, `32x32`, `48x48`, `256x256`
- 背景: 透過PNGベースで作成してから `.ico` 化
- ファイル配置: `assets\app\app.ico`

## 配布運用（固定手順）

### 1. 配布者の手順
1. リポジトリ直下でビルドする。
   ```powershell
   .\venv\Scripts\Activate.ps1
   .\scripts\build.ps1
   ```
2. `dist\LoLReplayTool\` ディレクトリを丸ごとZIP化して配布する。
3. ZIP化前に以下が入っているか確認する。
   - `LoLReplayTool.exe`
   - `config\setting.sample.json`
   - `config\champion_aliases.json`
   - `assets\champions\icons\`
   - `bin\`（空でも可）

### 2. 受け取り側の初回セットアップ
1. ZIPを展開する。
2. `bin\` に mpv DLL を配置する（`mpv-1.dll` / `libmpv-1.dll` / `libmpv-2.dll` のいずれか）。
3. `LoLReplayTool.exe` を起動し、`config\setting.json` が生成されることを確認する。
4. アプリの設定画面で `obs.dir` / WebSocketポート / パスワード / シーン名 / ソース名を設定する。
   - 迷った場合は「設定 > 録画前チェックを実行」で自動修復できます。

## JSONの内容（例）
```json
{
  "summoner_name": "...",
  "sync_game_time": 12.34,
  "obs_record_path": "C:/path/to/video.mp4",
  "events": [],
  "events_all": [],
  "counts": { "filtered": 0, "all": 0 }
}
```

## トラブルシュート
- `mpv-1.dll が見つかりません`  
  `bin/` に DLL を配置してください。
- イベントが表示されない  
  JSONの `events` / `events_all` を確認してください。
- 同期が合わない  
  OBSの赤色ソースが左上に出るように配置してください。
