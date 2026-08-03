## 関連Issue

- Closes #
- Issueなしの場合の理由:

## 変更内容

-

## 変更理由

-

## 確認結果

- [ ] Ruff: `.\venv\Scripts\python.exe -m ruff check src tests`
- [ ] pytest: `.\venv\Scripts\python.exe -m pytest -p no:cacheprovider tests`
- [ ] Windowsビルド: `pwsh -NoProfile -File .\scripts\build.ps1`
- [ ] self-check: `.\dist\LoLReplayTool\LoLReplayTool.exe --self-check`
- [ ] 手動テスト
- [ ] 対象外または未実施の項目について理由を記載した

## 手動テスト内容

- 環境:
- 実施項目:
- 結果:

## 影響範囲

- [ ] 録画・試合検知
- [ ] OBS
- [ ] リプレイ再生・mpv
- [ ] 設定・保存データ
- [ ] ビルド・インストーラー
- [ ] CI・Release
- [ ] ドキュメントのみ

## レビュー指摘

### Must

-

### Should

-

### Nit

-

## 対応しない指摘と理由

-

## リリース更新の要否

- [ ] 不要（理由を下記に記載）
- [ ] 必要（Patch相当）
- [ ] 必要（Minor相当）
- [ ] 必要（Major相当）
- [ ] 必要な `CHANGELOG.md` と `CHANGELOG.en.md` の更新を行った
- [ ] READMEを変更した場合、`README.md` と `README.en.md` の内容を同期した

- 判断理由:
