# 開発・リリース運用ルール

`main` は常にテスト済みで、Releaseを作成できる状態に保ちます。通常の変更を
`main` へ直接pushせず、作業ブランチとPull Request（PR）を使用してください。

## ブランチ

作業開始時に最新の `main` から、変更目的に合うブランチを作成します。

- `feature/<name>`: 新機能
- `fix/<name>`: 不具合修正
- `docs/<name>`: ドキュメントのみ
- `chore/<name>`: 保守、依存関係、CIなど

`<name>` は英小文字のkebab-caseで、変更内容が分かる名前にします。

```powershell
git switch main
git pull --ff-only origin main
git switch -c fix/game-start-detection
```

## 実装とコミット

- 1つのブランチでは、1つの目的に必要な変更だけを扱います。
- 挙動を変更した場合は、対応するテストを追加または更新します。
- コミットメッセージは、変更した内容を命令形の短い英文で記述します。
- ユーザーに影響する変更は、Release時に `CHANGELOG.md` へ記載します。
- ビルド成果物、仮想環境、一時ファイルはコミットしません。

## ローカル検証

コードを変更したPRでは、原則として次を実行します。

```powershell
.\venv\Scripts\python.exe -m ruff check src tests
.\venv\Scripts\python.exe -m pytest -p no:cacheprovider tests
```

パッケージング、実行時依存関係、GUI、OBS、mpv、インストーラーに影響する場合は、
Windowsビルドとセルフチェックも実行します。

```powershell
.\scripts\build.ps1
.\dist\LoLReplayTool\LoLReplayTool.exe --self-check
```

ドキュメントだけの変更では、アプリのビルドとテストを省略できます。

## Pull Request

- PRのマージ先は `main` にします。
- PR本文に変更理由、確認方法、影響範囲を記載します。
- GitHub Actionsの `Lint & Test` と `Build Windows artifact` が成功するまで
  マージしません。
- PRのレビュー指摘と失敗したチェックを解消してからマージします。
- マージ方式は原則としてSquash mergeを使用します。
- マージ後は作業ブランチを削除します。

PRタイトルは、変更種別と内容が判別できる形式を推奨します。

```text
fix: improve game start detection
feature: add recording deletion
docs: document release workflow
```

## Release

Releaseは `main` からのみ作成します。作業ブランチへReleaseタグを付けてはいけません。

1. `VERSION` を公開するバージョンへ更新します。
2. `CHANGELOG.md` に同じバージョンの変更内容を追加します。
3. 通常のPRとしてレビューとCIを通し、`main` へマージします。
4. `main` の対象コミットに `vX.Y.Z` 形式のタグを作成してpushします。
5. Release workflowの成功と公開された成果物を確認します。

```powershell
git switch main
git pull --ff-only origin main
git tag v0.2.0
git push origin v0.2.0
```

バージョンは次を基準に決定します。

- Patch: 後方互換性のある不具合修正
- Minor: 後方互換性のある機能追加
- Major: 互換性を壊す変更

緊急修正でも `fix/*` ブランチとPRを使用し、CIを省略しません。
