# LoL Replay Tool

LoLの試合録画（OBS自動制御）と、イベントログ同期再生プレーヤーをまとめたツールです。

## 主な機能
- 試合開始/終了の自動検知とOBS録画制御
- 重要イベントのログ保存（自分のキル/デス + オブジェクト）
- 録画動画とイベントの同期再生プレーヤー
- 初回セットアップウィザード（環境を自動修復）

## 必要環境
- Windows
- Python 3.10+ 推奨
- OBS Studio（ポータブル版を `bin/OBS-Studio` に配置）
- mpv の DLL（`bin/` に `mpv-1.dll` / `libmpv-1.dll` / `libmpv-2.dll` のいずれか）

## セットアップ
```powershell
pip install -r requirements.txt
```

設定ファイルを作成:
```powershell
copy config\setting.sample.json config\setting.json
```

通常は `config/setting.json` を手動編集する必要はありません。
- OBSは `bin/OBS-Studio` のポータブル版のみ利用します
- WebSocket設定はアプリ側で自動補完します
- 初回起動のセットアップで「環境を自動修復」を実行すると、シーン/色ソースを自動作成します

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
- `config\setting.json` は初回起動時に自動生成されます
- OBS は `bin\OBS-Studio` (ポータブル) のみ利用します（ユーザー環境のOBSは利用しません）
- ビルド時に `bin\OBS-Studio` は配布物へ自動コピーされます（未配置ならビルド失敗）
- ポータブルOBSのWebSocket設定はアプリ側で自動補完します
- 設定画面または初回セットアップで「環境を自動修復」を実行すると、配布先でも設定不要で動かせます
- mpv DLL は同梱せず、利用者が `bin\` に配置します
- `assets\app\app.ico` が存在する場合、exeアイコンとウィンドウアイコンに反映されます
- ビルド成果物は `LoLReplayTool.exe` と同じ階層に `config` / `assets` / `bin` が配置されます（`_internal` 非使用）

### アイコン画像の推奨仕様
- 形式: `.ico`（複数サイズ同梱）
- 推奨サイズ: `16x16`, `32x32`, `48x48`, `256x256`
- 背景: 透過PNGベースで作成してから `.ico` 化
- ファイル配置: `assets\app\app.ico`

## 配布運用（固定手順）

### 1. 配布者の手順
1. リポジトリ直下の `bin\OBS-Studio\` にポータブルOBSを配置する。
2. リポジトリ直下でビルドする。
   ```powershell
   .\venv\Scripts\Activate.ps1
   .\scripts\build.ps1
   ```
3. `dist\LoLReplayTool\` ディレクトリを丸ごとZIP化して配布する。
4. ZIP化前に以下が入っているか確認する。
   - `LoLReplayTool.exe`
   - `config\setting.sample.json`
   - `config\champion_aliases.json`
   - `assets\champions\icons\`
   - `bin\OBS-Studio\`（ビルド時に自動コピーされたポータブルOBS）
   - `bin\` 配下の mpv DLL（`mpv-1.dll` など）

### 2. 受け取り側の初回セットアップ
1. ZIPを展開する。
2. `LoLReplayTool.exe` を起動する。
3. 初回セットアップで「環境を自動修復」を1回実行する。
4. 以後は設定変更なしで録画開始できる。

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
  設定画面の「環境を自動修復」を再実行してください。
