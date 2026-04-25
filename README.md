# LoL Replay Tool

LoLの試合録画（OBS自動制御）と、イベントログ同期再生プレーヤーをまとめたWindows向けツールです。

## Riot Games 免責事項

LoL Replay Tool is not endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.

このツールはLeague of LegendsのローカルAPIを利用しますが、Riot Games公式の製品・サービスではありません。

## 主な機能

- 試合開始/終了の自動検知とOBS録画制御
- 重要イベントのログ保存（自分のキル/デス + オブジェクト）
- 録画動画とイベントの同期再生プレーヤー
- 初回セットアップウィザード（環境を自動修復）
- アプリ側UIからのOBS音声デバイス設定

## 必要環境

- Windows
- Python 3.10+ 推奨
- OBS Studio ポータブル版（利用者が `bin/OBS-Studio` に配置）
- mpv DLL（利用者が `bin/` に `mpv-1.dll` / `libmpv-1.dll` / `mpv-2.dll` / `libmpv-2.dll` のいずれかを配置）

## 第三者コンポーネントの扱い

このリポジトリおよびビルド成果物には、OBS Studio本体、mpv DLL、Riot Gamesの画像アセットを同梱しません。

- OBS Studioは利用者が公式配布元からポータブル版を取得し、`bin/OBS-Studio` に配置してください。
- mpv DLLは利用者が正規の配布元から取得し、`bin/` に配置してください。
- チャンピオンアイコンを使う場合は、利用者が `assets/champions/icons` に配置してください。Riot Gamesのアセットを利用する場合は、Riot Gamesの規約・ポリシーに従ってください。

## セットアップ

```powershell
pip install -r requirements.txt
```

設定ファイルを作成:

```powershell
copy config\setting.sample.json config\setting.json
```

通常は `config/setting.json` を手動編集する必要はありません。

- OBSは `bin/OBS-Studio` に配置されたポータブル版のみ利用します
- ユーザー環境にインストール済みのOBSは利用しません
- WebSocket設定はアプリ側で自動補完します
- 初回起動のセットアップで「環境を自動修復」を実行すると、シーン/色ソースを自動作成します

`config/setting.json` は `.gitignore` 済みです。

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
- `obs_record_path` はファイル名保存のため、録画ディレクトリごと移動しても再解決できます
- 同期マーカーを検出してイベントとシーク位置を合わせます

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
- OBS Studio本体はビルド成果物へコピーされません
- mpv DLLはビルド成果物へコピーされません
- チャンピオンアイコンはビルド成果物へコピーされません
- `assets\app\app.ico` が存在する場合、exeアイコンとウィンドウアイコンに反映されます
- ビルド成果物は `LoLReplayTool.exe` と同じ階層に `config` / `assets` / `bin` が配置されます（`_internal` 非使用）

### アイコン画像の推奨仕様

- 形式: `.ico`（複数サイズ入り）
- 推奨サイズ: `16x16`, `32x32`, `48x48`, `256x256`
- 背景: 透過PNGベースで作成してから `.ico` 化
- ファイル配置: `assets\app\app.ico`

## 配布運用

### 配布者の手順

1. リポジトリ直下でビルドする。

   ```powershell
   .\venv\Scripts\Activate.ps1
   .\scripts\build.ps1
   ```

2. `dist\LoLReplayTool\` ディレクトリを丸ごとZIP化して配布する。
3. ZIP内にOBS Studio本体、mpv DLL、Riot Gamesの画像アセットが含まれていないことを確認する。

### 受け取り側の初回セットアップ

1. ZIPを展開する。
2. ポータブルOBSを取得し、`LoLReplayTool\bin\OBS-Studio\` に配置する。
3. mpv DLLを取得し、`LoLReplayTool\bin\` に配置する。
4. チャンピオンアイコンを使う場合は、画像ファイルを `LoLReplayTool\assets\champions\icons\` に配置する。
5. `LoLReplayTool.exe` を起動する。
6. 初回セットアップで「環境を自動修復」を1回実行する。

## JSONの内容（例）

```json
{
  "summoner_name": "...",
  "champion_name": "...",
  "game_result": "Win",
  "sync_game_time": 12.34,
  "obs_record_path": "replay_20260425.mp4",
  "events": [],
  "events_all": [],
  "counts": { "filtered": 0, "all": 0 }
}
```

## トラブルシュート

- `ポータブルOBSが見つかりません`
  `bin/OBS-Studio/bin/64bit/obs64.exe` が存在するようにポータブルOBSを配置してください。
- `mpv-1.dll が見つかりません`
  `bin/` に mpv DLL を配置してください。
- イベントが表示されない
  JSONの `events` / `events_all` を確認してください。
- 同期が合わない
  設定画面の「環境を自動修復」を再実行してください。
