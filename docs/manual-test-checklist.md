# 手動テストチェックリスト

自動テストだけでは確認できないWindows実環境、LoLクライアント、OBS、mpv、FFmpegとの連携を確認するためのチェックリストです。すべてのPRで全項目を実施するのではなく、変更の影響範囲に該当する項目を選び、環境と結果をPRへ記録します。

## 事前記録

- [ ] 対象ブランチまたはコミットを記録した
- [ ] Windowsバージョン、Pythonまたはビルド版、LoLクライアント状態を記録した
- [ ] OBS、mpv DLL、FFmpegの入手元と利用者が配置した場所を記録した
- [ ] 既存設定・録画を退避する必要がある場合はバックアップした

## アプリ起動

- [ ] `python main.py` またはビルド版exeが起動し、メイン画面が表示される
- [ ] 二重起動が抑止され、先に起動したプロセスへ悪影響がない
- [ ] 初回セットアップ、通常起動、終了、タスクトレイ終了が固まらない
- [ ] OBS未配置でも自動通信せずにメイン画面と手動配置案内を表示できる
- [ ] 旧OBS配置のコピーを途中失敗させた場合、コピー中マーカーが残る間は準備完了・起動扱いにならず、旧配置を保持した再検査でコピー完了・マーカー除去・起動まで復旧できる
- [ ] GUIの「OBS設定を構成・再検査」と`scripts/setup_env.py`を同時実行してもコピーは1プロセスだけが行い、完了前にOBSが起動しない
- [ ] 旧OBS配置のコピー中に実行プロセスを強制終了しても、旧配置を保持した再検査でstale状態から再開できる
- [ ] コピー中マーカーが示す旧配置を移動・削除した場合、別の候補を混在コピーせず、`obs-portable`全体の退避を含む復旧案内を表示する
- [ ] 起動失敗時に利用者が対応可能なメッセージとログが残る

### OBSコピー移行のtransaction耐久性（破棄可能な専用環境）

実際のOBS配置とは別のテスト用source／destinationを用意し、sourceと外部directoryに変更検知用sentinelを置きます。各試行ではOS、filesystem、file数、総bytes、最大階層、開始・終了時刻を記録し、強制終了にはタスクマネージャーまたは`Stop-Process -Force`を使用します。

- [ ] 同じsourceから異なる2つのdestinationへ同時に移行すると、後から開始した処理はsource lockで拒否され、journal・copy一時file・確定fileを作成しない
- [ ] destination／sourceに残ったstale lockは再利用でき、0-byte lockは安全に初期化される一方、別processが保持中のlockはstale扱いしない
- [ ] source lock取得直後にsourceへコピー中markerを作成した競合を検知し、destinationのコピーを開始しない
- [ ] journal一時fileの確定直前と確定直後にprocessを強制終了し、再起動時に所有権を検証できる一時fileだけを回収または再開する
- [ ] data copy一時fileの確定直前と確定直後にprocessを強制終了し、再起動後の全file hashがsourceと一致する
- [ ] `finalize_pending`更新の直前と直後にprocessを強制終了し、再起動時にコピーを混在させず最終化だけを安全に再試行する
- [ ] コピー中marker削除の直前と直後にprocessを強制終了し、再起動後は完了済みdestinationを再コピーせず利用できる
- [ ] 各強制終了点でsourceと外部sentinelが不変であり、成功後は所有中marker／一時fileがなく、失敗時は復旧に必要なmarker／一時fileが保持される
- [ ] markerなしの正当なjournal一時file1件だけはsource fingerprint一致時に回収でき、空・破損・所有者不一致・source不一致・複数・nestedの一時fileは変更せず復旧案内を表示する
- [ ] destination配下のdirectoryを走査中にjunctionへ差し替えても外部treeを読まず、外部sentinelを変更せず、準備完了扱いにしない
- [ ] `global.ini`など許可された最終化fileをread後からreplace直前に同名別fileへ差し替えると、別fileを上書きせずmarkerを維持して復旧案内を表示する
- [ ] 許可された最終化fileと同名の大文字小文字違い、未知のfile、`temp_appdata`、lease情報を最終化処理が変更した場合は検知する
- [ ] 非管理者のWindowsユーザーで移行先directoryを作成し、終了後に別processから作成・読み書き・一覧・renameできる
- [ ] Windowsで保持中のnested directoryは外部からrename／junction差し替えできず、relative replace／unlink後も別processから正常に再openできる
- [ ] NTFS上でsourceを指すSUBST drive、volume GUID path、8.3 short nameをdestinationまたはそのancestorとして指定し、source内／destination内の両方向aliasをmarker・lock・一時file作成前に拒否する
- [ ] sourceまたはdestination配下のdirectoryをjunction／symbolic linkにした場合、外部sentinelを読まず変更せず、marker・lock・一時fileを作成しない
- [ ] regular file、nested directory、managed root自身へ名前付きNTFS ADS（例`:issue83-test:$DATA`）を付け、default streamと外部sentinelを変更せずRecovery案内にする
- [ ] allowlist外fileとmanaged root自身のDACL、read-only／hidden属性、last-write timeだけをfinalizer中に変更し、content hashが同じでも変更を検知してmarkerを維持する。directoryのchild操作による時刻変更だけでは誤検知しない
- [ ] security descriptor／ADS列挙を拒否するACL、または必要なmetadata APIを提供しないfilesystemで、copy確定fileを作らずRecovery案内にする
- [ ] 3,000～5,000 fileのsynthetic treeを移行し、file数・総bytes・経過時間・process handle数の開始値／最大値／終了値・source read倍率をPRへ記録する。時間の固定合否値は設けず、handleがfile数に比例して残存しないことと、source全体の反復読み込み回数が設計値を超えないことを確認する

Linuxでmount権限のある破棄可能な環境では、source rootとsource内のchildをそれぞれ別pathへbind mountし、そのalias配下をdestinationにした場合に永続transaction file作成前で拒否することも確認します。source／destinationのmanaged root配下へnested bind mountを置いた場合も、外部sentinelを読まず書かずRecovery案内にします。POSIXでの同一権限processによるpath差し替え防止は協調lockが前提です。`/proc/self/fdinfo`がない、またはmount IDを読めないPOSIX環境と、handleからxattr／ACLを取得できないfilesystemは自動移行の非対応条件です。Windowsでdirectory metadata flushがruntime／filesystemから提供されない場合も、各fileの`fsync`、journalの順序、再起動時検証で復旧できることを記録します。

## 設定画面

- [ ] 保存先、FPS、解像度、容量制限、通知設定を読み書きできる
- [ ] 音声デバイス一覧の更新と設定適用がUIを停止させない
- [ ] 不正値が補正または明示的に拒否され、再起動後も設定が保たれる
- [ ] 「OBS設定を構成・再検査」で専用`obs-portable`の設定を構成・復旧できる
- [ ] standalone FFmpegの明示設定を保存し、再起動後も同じ実行ファイルを利用できる

### OBS録画profileのpath安全性（破棄可能なNTFS環境）

実際の設定を退避し、外部sentinelと変更前のINI bytes、起動中の管理対象OBS PIDを記録してから確認します。

- [ ] `config/obs-studio/basic/profiles`を外部directoryへのjunctionにした場合、「OBS設定を構成・再検査」とOBS起動を拒否し、外部sentinel・既存INI・管理対象OBS processを変更しない
- [ ] profiles配下のprofile directoryを外部directoryへのjunctionにした場合、同じく全profile書き込み前かつOBS停止前に拒否する
- [ ] `basic.ini`または`user.ini`を外部fileへのhardlinkにした場合、link元・link先・先行する安全なprofileのbytesを変更せず、OBSを停止しない
- [ ] junction／hardlinkを除去した通常配置では、既存の未知設定を保ったままprofileを修復し、管理対象OBSを起動できる

### OBS起動前設定transactionの耐久性（破棄可能な専用環境）

実際に利用するOBS設定ではなく、破棄可能な専用`obs-portable`を複製して確認します。開始前に全targetのbytes、管理対象OBS PID、外部sentinelを記録し、WebSocket passwordはログ、journal、PRへ記録しません。強制終了後も`.lol_replay_obs_settings_transaction.json`や所有中の`*.copy.tmp`／`*.write.tmp`を個別に削除せず、次の起動または「OBS設定を構成・再検査」で復旧させます。

- [ ] 通常起動、GPU検出後の再起動、「OBS設定を構成・再検査」、`scripts/setup_env.py`を同時に開始しても、同じ管理rootでは1処理だけがlockを取得し、後続処理が設定やコピーを開始しない
- [ ] 設定がすでにdesiredと一致するno-opでも、明示的な起動・修復操作では管理対象OBSを停止し、全processの終了確認後にだけ続行する
- [ ] `preparing` journal確定の直前／直後、各backup／desired一時fileの書き込み途中／直後で強制終了し、targetが不変のまま次回実行で所有中一時fileとjournalだけを清掃できる
- [ ] 全一時file準備後のOBS停止でINIをflushさせ、計画時snapshotとの差を検知してflush後のbytesを保持し、他targetを一つも確定しない
- [ ] `committing`更新の直前／直後、各target確定の直前／直後で強制終了し、次回実行で全targetが混在せずoriginalへrollbackされる
- [ ] `committed`更新の直前はoriginalへrollbackされ、更新直後またはcleanup途中の強制終了では全desiredを保持して次回実行で一時fileとjournalだけを清掃する
- [ ] `committed` journalのatomic replace後に親directory flushを失敗させ、同じprocessではbackup／markerを清掃せず、次回実行が実際に残った`committing`／`committed` phaseに従ってrollbackまたはcleanupする
- [ ] 管理対象外OBSが停止前または停止直後に存在する場合、管理対象processを終了・設定を確定せず案内を表示する。管理対象OBSのkill APIが成功扱いでもprocessが残る場合は確定しない
- [ ] 計画時に未変更だった既存profileの`basic.ini`を停止中に変更した場合と、計画時に欠落していた`basic.ini`を停止中に作成した場合を検知し、変更されたbytesを保持して他targetを確定しない
- [ ] 別targetを更新するtransactionで、計画時に未変更だった`global.ini`、`user.ini`、WebSocket設定を停止中に変更した場合も検知し、他targetを確定しない
- [ ] `user.ini`の独自section／keyを保持し、bootstrap設定と管理profile選択が同じoriginal snapshotへ合成される。bootstrap preflight後の外部変更は再読込で混在させず停止前に拒否する
- [ ] 読み取り上限を1 byte超えるdesired payloadと、予約済みmarker／lock名をdirectoryに含むplanを、journal・一時file作成とOBS停止より前に拒否する
- [ ] journalを確認し、relative path、size、SHA-256、所有情報以外の設定本文やWebSocket passwordが含まれない
- [ ] stale settings journalが環境準備完了またはOBSコピー中状態と誤表示されず、次回起動・再検査・旧配置コピー前にsettings phaseどおり復旧される
- [ ] POSIXの破棄可能な環境でlock取得後にmanaged rootをrenameし、同じlexical pathへ別rootを作成してtransaction fileを移しても、新rootと外部sentinelを変更せずRecoveryにする。new root側で別lockを取得できる状況でも旧rootのlock所有をcommit権限に使わない
- [ ] 復旧不能の案内では、全OBS終了、`obs-portable`全体とログの退避、再検査の順を確認する。marker単体削除やOBS／FFmpegの自動取得・再配布を案内または実行しない

## LoLクライアント未起動時

- [ ] アプリが異常終了せず、試合待機状態を継続する
- [ ] LoLクライアントを後から起動すると監視が再開する
- [ ] LCU認証情報やLive Client APIが未取得でも過剰な通知やダイアログを出さない

## 録画開始検知

- [ ] チャンピオン選択から試合開始まで監視が継続する
- [ ] 実際の試合開始を検知して管理対象OBSが起動し、録画を開始する
- [ ] 録画開始通知と画面上の状態表示が実際の録画状態と一致する
- [ ] ハードウェアエンコーダが利用できない場合にx264へフォールバックする
- [ ] Dodgeまたは試合未開始時に誤った録画・Ban/Pick履歴を残さない

## 録画停止・終了処理

- [ ] `GameEnd`、ゲームプロセス終了、LCU状態遷移に応じて録画を停止する
- [ ] 試合終了直後の一時的なAPI失敗で早期停止しない
- [ ] アプリ終了やエラー時に録画プロセスとOBS接続を安全に終了する
- [ ] 完了、部分保存、中断の状態がログと通知へ正しく反映される

## JSON保存

- [ ] `recordings/json` または設定した保存先にJSONが1件保存される
- [ ] `schema_version`、`session_status`、動画パス、イベント、試合情報が保存される
- [ ] Ban/Pick、勝敗、キュー情報は取得できた範囲で保存される
- [ ] 保存失敗時に既存JSONを破損させず、エラーが確認できる

## リプレイ一覧と削除

- [ ] 保存済みJSONからリプレイ一覧が表示され、絞り込みと再読み込みが動作する
- [ ] チャンピオン、勝敗、マッチ種類、動画有無が正しく表示される
- [ ] 録画削除で対象動画、JSON、関連クリップだけがごみ箱へ移動する
- [ ] 設定した録画ディレクトリ外のファイルを削除しない
- [ ] 壊れたJSONや欠落動画があっても一覧全体が利用できる

## 動画再生とイベントジャンプ

- [ ] mpv DLL配置済み環境で動画を開き、再生・一時停止・シークできる
- [ ] mpv DLL未配置時に導入方法が分かるエラーを表示する
- [ ] イベント一覧からキル、デス、オブジェクト、建造物の時刻へジャンプできる
- [ ] 同期補正後にイベント時刻と動画が一致し、再読み込み後も補正が保たれる
- [ ] FFmpeg未配置時は自動通信せず、利用者の明示操作でだけ公式案内ページを開く
- [ ] FFmpegは明示設定、データ用`bin`、アプリルートfallback、安全な絶対PATHの順で選択される
- [ ] 利用者が配置したFFmpegでクリップ出力、進捗、キャンセル、出力を確認する

## ビルドとself-check

- [ ] 利用者管理のOBS、standalone FFmpeg、mpv DLLを含まない通常成果物で`.\scripts\build.ps1`が成功する
- [ ] `.\dist\LoLReplayTool\LoLReplayTool.exe --self-check` が終了コード0になる
- [ ] OBS、mpv、FFmpeg未配置の警告が想定どおりで、必須診断は成功する
- [ ] ビルド成果物へOBSまたはstandalone FFmpegが混入した場合は検査が失敗する
- [ ] ビルド成果物のrootまたは下位directoryへ`libmpv-*.dll`／`mpv-*.dll`が混入した場合は、対象pathと`%LOCALAPPDATA%\LoLReplayTool\bin`への利用者配置方針を表示して検査が失敗する

## インストーラー確認が必要なケース

次の変更では `.\scripts\build_installer.ps1` による生成と実際のインストール確認を行います。

- [ ] `installer/LoLReplayTool.iss`、`VERSION`、インストール先、ショートカットを変更した
- [ ] PyInstallerの成果物構成、データファイル、exe名を変更した
- [ ] 更新・アンインストール・ユーザーデータ保持方針を変更した
- [ ] 新しいランタイム依存ファイルや権限要件を追加した

確認時は、新規インストール、上書き更新、アンインストールを行い、設定・録画の保持、削除オプション、スタートメニュー、管理者権限不要の動作を記録します。
