# LoL Replay Tool

LoLの試合録画（OBS自動制御）と、イベントログ同期再生プレーヤーをまとめたツールです。

## 主な機能
- 試合開始/終了の自動検知とOBS録画制御
- 重要イベントのログ保存（自分のキル/デス + オブジェクト）
- 録画動画とイベントの同期再生プレーヤー

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

```powershell
pip install pyinstaller
.\scripts\build.ps1
```

出力先: `dist\LoLReplayTool\`
- 初回起動時に `config\setting.json` が自動生成されます
- `config\setting.json` を編集して OBS パス等を設定してください
- mpv DLL は `bin\` に同梱されます
- OBS 本体は同梱しません（各自でインストール）

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
