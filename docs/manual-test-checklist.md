# 手動テストチェックリスト

自動テストだけでは確認できないWindows実環境、LoLクライアント、OBS、mpv、FFmpegとの連携を確認するためのチェックリストです。すべてのPRで全項目を実施するのではなく、変更の影響範囲に該当する項目を選び、環境と結果をPRへ記録します。

## 事前記録

- [ ] 対象ブランチまたはコミットを記録した
- [ ] Windowsバージョン、Pythonまたはビルド版、LoLクライアント状態を記録した
- [ ] 開発・ビルド確認では`pwsh --version`のmajor versionが7以上であることを記録した（Windows PowerShell 5.1は対象外）
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
- [ ] 全一時file準備後のOBS停止で既存の`global.ini`、`user.ini`、WebSocket設定、profile `basic.ini`へ未知keyを含む実flushを発生させ、同じmutation guard／root lease内の一度だけのfresh planで未知keyを保持してdesiredを確定する。retryでOBSを再停止・追加killしない
- [ ] 初回planがno-opの状態から停止flushで既知設定を変更し、fresh plan由来のchanged flag、変更profile path、画面／ログの更新結果が変更ありになる。fresh planもno-opの場合でもcommit直前のstrict zero-process queryを省略しない
- [ ] retry準備中にfile hash／identity／security、rootまたはancestor directory identity／security、portable marker、profile直下のname／kind／identity、target／directory／validation setのいずれかを変え、flush後bytesを上書きせずfail-closeする
- [ ] 欠落targetを含むplan、既存だが修正予定のportable markerを含むplan、停止中の対象file作成／削除、観測・計画入力範囲内の未知file／path／profile topology変更では自動再計画せず、停止後の外部変更を保持して他targetを確定しない
- [ ] retry直前のstrict process query失敗、管理／非管理OBSの再出現、fresh prepare失敗、二度目のconflictをそれぞれ発生させ、二度目のstop／kill／retryなしでfail-closeし、WebSocket passwordが例外、cause chain、journal、ログへ残らない
- [ ] `committing`更新の直前／直後、各target確定の直前／直後で強制終了し、次回実行で全targetが混在せずoriginalへrollbackされる
- [ ] `committed`更新の直前はoriginalへrollbackされ、更新直後またはcleanup途中の強制終了では全desiredを保持して次回実行で一時fileとjournalだけを清掃する
- [ ] `committed` journalのatomic replace後に親directory flushを失敗させ、同じprocessではbackup／markerを清掃せず、次回実行が実際に残った`committing`／`committed` phaseに従ってrollbackまたはcleanupする
- [ ] 管理対象外OBSが停止前または停止直後に存在する場合、管理対象processを終了・設定を確定せず案内を表示する。管理対象OBSのkill APIが成功扱いでもprocessが残る場合は確定しない
- [ ] 通常／`preparing`復旧後の設定commitはstop 1回、`committing`／`committed`復旧後のcommitは復旧stopとcommit stopの計2回、停止flushからのretryは追加stop／kill 0回になる
- [ ] GPU検出後の再起動では起動直後にPID、絶対executable path、creation FILETIMEをPopen handleへ固定し、停止前strict snapshotとの完全一致とhandle生存を確認する。既知processは元handleの終了、残りの管理processは`OpenProcess`したhandleを最終zero確認後まで保持したidentity付きstrict signal結果で説明し、同じPIDが異なるcreation FILETIMEで再出現した場合はreplacementをsignalせず失敗する。terminate失敗時も安全に特定できる残りtreeを停止するがevidenceは発行せず、flush再計画が成功してもterminateとsignalを繰り返さない
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

### OBS runtime所有processの終了（破棄可能な専用環境）

通常利用のOBS設定とは別に、管理対象portable OBSと管理対象外OBSを同時に起動できる環境を用意します。試行前後に各processのPID、絶対executable path、raw creation FILETIMEとprocess leaseを記録します。

- [ ] schema v2 leaseと一致する管理対象OBSをruntime終了で停止し、同時に動作する管理対象外OBSのPID、path、raw creation FILETIMEが不変であることと、管理対象の消滅確認後にだけleaseが削除されることを確認する
- [ ] 管理対象OBSのgraceful停止がtimeoutした場合、同じ検証済みhandleへのforce停止で終了し、管理対象外OBSへsignalしない
- [ ] 旧schema leaseがlive processを指す場合はsignalせず、手動終了と再試行の案内を表示してleaseを維持する。手動終了後の再試行ではstrictなPID不在を確認してleaseを削除する
- [ ] 破損leaseでは自動停止・自動削除せず、全OBSの手動終了後にだけ、案内へ表示された絶対pathのleaseを退避または削除して再試行できる
- [ ] query失敗、identity欠落、same-PID replacement、signal失敗、wait失敗、`TerminateProcess`失敗、`CloseHandle`失敗、最終残存をそれぞれ発生させ、対象外processへsignalせずleaseを維持して失敗を表示する
- [ ] 新規OBS起動直後にPopen handleからraw creation FILETIMEを取得できない場合、その新規processだけを停止して起動失敗にし、既存OBSを変更しない
- [ ] 通常起動したschema v2 lease付きOBSを終了し、元Popen handleだけがgraceful終了する場合とgraceful timeout後に同じhandleをforce終了する場合の両方で、終了確認後にだけleaseが削除される
- [ ] 通常Popen終了の`terminate`、`kill`、非timeout `wait`、`poll`失敗と最終残存を個別に発生させ、型付き失敗と手動終了案内が表示されること、元handleの終了を確認できない場合はleaseが維持されることを確認する
- [ ] 通常Popenと同じPIDを別creation FILETIMEで表すreplacementを模擬し、元Popen以外へsignalせず、replacementのlease bytesを変更しない
- [ ] 通常Popenのbound lease欠落・破損・handle identity不一致、disk上のlease破損・差し替え・削除失敗を個別に発生させ、成功扱いせず既存leaseを維持する
- [ ] 通常Popen終了とWebSocket切断が同時に失敗する場合、終了失敗を主因として表示し、切断を一度試行した事実と切断失敗をnoteまたはlogで確認する
- [ ] portable mode不一致、起動直後identity取得失敗、Recorder起動失敗、GPU再起動失敗で、cleanupも通常の`Exception`で失敗する場合は先行エラーを主因としてcleanup失敗をnoteまたはlogへ残し、同じPopenへ重複signalしない
- [ ] Popen生成直後、lease作成前後、transaction終了、portable mode／encoder確認、GPU再起動、WebSocket client／Recorder／runtime構築、`Recorder.open()`、接続test、`RecordingSupervisor`のruntime保存後からrecorder取得および終了前段へ`KeyboardInterrupt`、`SystemExit`、非同期cancelを個別に注入し、最初のcontrol-flow中断と同じ例外objectが主因であること、同じPopenへのsignal、runtime close、shutdown、disconnectが各1回以下であることを確認する。cleanup失敗時はnote／logと手動終了案内を確認し、終了を証明できないhandleおよびpublish／bind不確実なleaseを維持する
- [ ] 先行する通常`Exception`の後でcleanupへcontrol-flow中断を注入し、cleanup中断の同一objectが主因へ昇格することを確認する。先行失敗がcontrol-flow中断の場合は後続の異なる中断を複数cleanupへ注入し、最初の中断だけを主因として後続失敗をnote／logへ残し、各例外の既存cause、context、suppress-contextが変わらないことを確認する
- [ ] 後段の実OBS A/Bコピーとは別に用意した同一の破棄可能な専用OBS rootで、holder process Aが `.lol_replay_obs_lease.lock` を取得したことをeventで確認してからreader process Bを開始し、Bが待機すること、Aのrelease後だけBが進むこと、両processの終了コードとlock再利用を確認する。固定sleepで順序を推測しない
- [ ] Windowsの子processを、専用lock取得後かつ厳密形式の `.lol_replay_obs_lease.tmp.<32 lowercase hex>` 永続化後に強制終了し、次のprocessが同じlockを取得して一時fileだけを回収し、既存leaseとOBS本体を変更しないことを確認する
- [ ] Windowsでschema v2 leaseをtransaction中に固定し、別processからのin-place write、replace、deleteがshare violationで拒否されること、元transactionが同じraw bytes／physical identityを再検証できることを確認する。POSIXで同等試験を行う場合は、同じprocess間lockを守る協調writer間の保証として記録する
- [ ] schema v2、旧schema、破損leaseを個別に配置してruntime起動を試し、strict process query、Popen、signalがすべて0回で、leaseのraw bytesとphysical identityが不変であることを確認する
- [ ] leaseがない状態で同じmanaged executableを起動してからruntime起動を試し、strict snapshotで検出してPopenとsignalを行わず手動確認を案内すること、同時に動作する管理対象外OBSのPID、path、raw creation FILETIMEが不変であることを確認する
- [ ] 実OBSとは別のGUID付きfresh TEMP rootへ通常fileのダミーexecutableを作り、そのhardlink、取得可能な8.3短縮名、volume GUID pathをstrict snapshotへ個別に返す。いずれも同じvolume serial／128-bit file IDとしてmanaged判定され、Popenと既存processへのsignalが0回であることを確認する
- [ ] 同じダミーexecutableへのjunction、symbolic link、SUBST／DOS-device mappingと、権限不足、欠落、FileId取得失敗、reparse判定、component identity／`QueryDosDevice` mappingの途中変化を個別に発生させる。unsupportedまたは不明としてPopenとsignalが0回になり、lease、lock、strict snapshotの契約が変わらないことを確認する
- [ ] stableな別物理identityのダミーOBS候補は管理対象外としてadmissionを通過すること、起動後再検証失敗またはcontrol-flow中断では新規Popen handleだけを1回cleanupし、既存候補へsignalしないことを確認する
- [ ] Windowsの破棄可能な専用環境で、CreateProcess互換の通常絶対managed pathを`cmd[0]`、その親directoryを`cwd`として承認済みの無害なhelperを実際に起動し、固定handleのvolume GUID pathは物理identity照合にだけ使う。Popen imageの物理identity、canonical managed pathのschema v2 lease、PID、raw creation FILETIMEを照合する。namespaceを固定しているPopenからlease bindまでの短い間だけrename／deleteがshare violationになることと、完了後に解放されることを確認する。実OBS、既存`obs-portable`、既存processはこの試験へ使用しない
- [ ] 同じmanaged rootを使う独立した2子processをeventで同時開始し、片方をtransaction内のPopen呼び出しでevent待機させてから他方のlock contentionを観測し、その後だけpublishを許可する。固定sleepを使わず、Popen 1回、成功1件、schema v2 lease 1件、敗者のPopen 0回、signal 0回であることを確認する
- [ ] 承認済み原本からsingle fresh TEMP copyを作り、holder子processが`start_obs()`で実OBSを起動して返されたPopenを強参照する。schema v2 lease確定後だけ別のspawn contenderを開始し、実Popen直前guardで誤起動を中断できる状態にしたうえで、contenderのstrict process query、Popen、signalがすべて0回、leaseのraw bytes／physical identityとholderのPID／path／raw creation FILETIMEが不変であることを確認する。最後にholderが保存済みの元Popen handleだけを1回終了し、終了確認後にleaseが消滅することを確認する
- [ ] identity取得、publish前、publish後、Popenへのlease bindで個別に失敗させ、admissionからの同じtransactionを保持したまま元Popen handleだけをcleanupすることを確認する。cleanup中の後続starterがPopenへ進まず、publish前は主leaseと所有一時fileが残らず、publish後／commit不確実時は主leaseを推測削除しないことも確認する
- [ ] 通常`Popen`終了とstale owned cleanupの両方で、graceful直前とforce直前にlease bytes／identityまたは対象handle identityを差し替えるfault injectionを行い、認可を失った段階以降のsignalを発行せずleaseを維持することを確認する。実OBSのPID再利用は発生させない
- [ ] 古いlease handleへdelete-on-closeを設定し、そのhandleをcloseした直後に協調writerが同じpathへ新しいleaseを作成しても、外側transaction終了後まで新leaseが保持されることを確認する
- [ ] OBSコピー／設定inventory中は、厳密形式のprocess lease一時fileを内容を開く前に除外し、専用lockはmetadata固定のため開く場合があっても最終比較対象から除外すること、主leaseの変更はfinalizerが検知すること、予約prefixの不正な名前は絶対root／lease／lock pathと全OBS・関連toolの終了／再試行案内を伴うRecoveryになることを確認する
- [ ] 実OBS試験前にstrict queryでOBSが0件であることを確認する。試験対象原本のcanonical absolute path、取得元／version、file count、総bytes、fingerprintを事前にIssue／PRで承認済みbaselineとして記録し、開始時にすべて完全一致する場合だけ続行する。fingerprintはrelative path（`\`を`/`へ変換）のOrdinal順に`path<NUL>size<NUL>file_sha256`を作り、`file_sha256`はlowercase hex、LF結合は末尾LFなしとしたUTF-8 bytes全体のSHA-256とする。開始時と終了時のpath、取得元／version、file count、総bytes、fingerprintをPRの試験証跡へ記録する
- [ ] hash／copy前と起動直前の両方でstrict queryがOBS 0件であることを確認し、開始時、copy直後、A停止後、終了時の原本inventoryをPRの試験証跡へ記録する
- [ ] OSのexclusive directory createを使ってGUIDを含む新規TEMP trial rootをatomicに作り、原本からfreshなA/Bを個別コピーする。`/MIR`や既存trialの再利用を行わない。原本、A、Bのdirectory physical identityが相互に異なり、相互のancestorでないこと、全entryにreparse point、special file、hardlinkがなく、起動前のA/B inventoryが原本と双方向一致し、process leaseと予約一時fileが存在しないことを確認する
- [ ] 各copyを`manager.start_obs(env=manager.isolated_env(), hidden=True)`で直接起動し、返されたPopenを強参照する。Popen handle identityとstrict snapshotを照合し、A起動後はAだけ、B起動後はA+Bだけのexact identityであることと、各copyのportable mode `true`をboundedに確認する。global OBS 1件を前提とする上位起動helperはBに使用しない
- [ ] A/B双方についてPID、絶対path、raw creation FILETIME、parsed schema、lease bytes／SHA-256／physical identity、段階別strict snapshot、終了return codeをtrial外のPRまたはtask logへ保存し、成功時にtrialを削除しても証跡を残す。process、portable mode、helper、inventoryの照会はboundedにし、timeout、query失敗、malformed結果を0件や成功として扱わない
- [ ] Bの`_process_lease_transaction()`で同じlease snapshot／descriptorをA停止前から停止後のraw bytes／physical identity／parsed lease再検証まで固定する。そのtransaction中にBの`read_process_lease()`や終了処理を再入せず、contextを完全に終了してからBを停止する。Aだけを通常Popen cleanupし、Aの終了とlease削除、Bの生存およびPID／path／raw FILETIME／lease不変を確認する。原本のfile count／bytes／fingerprintも開始時と同一であることを再確認する
- [ ] 実OBS試験の`finally`ではPopenごとに取得済み、cleanup試行済み、正常完了を記録し、正常完了済みhandleへ再度signalせず、未完了の保存済み元Popen handleだけをcleanupする。`start_obs()`がcleanupにも失敗してPopenを返さない場合はPID指定へfallbackせず即時中止し、strict／helper照会結果とtrial pathを記録して手動確認する。主試験、全cleanup、元handleの終了、lease消滅、strict queryによるOBS 0件、trial配下executableのhelper process 0件、原本baseline不変をすべて確認できた場合に限り、作成時と同じtrial root physical identity、canonical TEMP直下のGUID leaf、trial直下がA/Bだけであること、原本tree外、全descendantのnon-reparse／通常file・directory／regular fileのsingle-link、原本・A・B間のphysical identity非共有を再検証して、そのtrial rootだけを削除する。いずれかが失敗または確認不能ならtrial rootを削除せず絶対pathを記録する。PID再利用競争は実OBSで発生させずunit testだけで検証する

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
- [ ] ロード画面から録画された動画でも、自動同期後に試合序盤・中盤・終盤のイベントがそれぞれ5秒前へジャンプする
- [ ] 同期時刻を取得できない録画は成功表示やoffset 0で自動ジャンプせず、通常再生と「現在位置で同期」を利用できる
- [ ] 同期補正後にイベント時刻と動画が一致し、再読み込み後も補正が保たれる
- [ ] FFmpeg未配置時は自動通信せず、利用者の明示操作でだけ公式案内ページを開く
- [ ] FFmpegは明示設定、データ用`bin`、アプリルートfallback、安全な絶対PATHの順で選択される
- [ ] 利用者が配置したFFmpegでクリップ出力、進捗、キャンセル、出力を確認する

## ビルドとself-check

- [ ] 正式対応OSはWindows 11（build 22000以降）、配布物はx64版、installerは`x64compatible`であり、Windows 10対応やARM64-native版を宣言していない
- [ ] 固定PyQt6-Qt6 6.10.2のQt6Coreが要求する`icuuc.dll`をfresh processでロードし、loaded module handleから実際の`%SystemRoot%\System32\icuuc.dll`であること、ICU DLLのapp-local配置が0件であることを確認する
- [ ] Qtのlocale、collation、Unicode境界処理を実行し、System32 ICUを利用した結果を記録する

- [ ] 利用者管理のOBS、standalone FFmpeg、mpv DLLを含まない通常成果物で`pwsh -NoProfile -File .\scripts\build.ps1`が成功する
- [ ] `.\dist\LoLReplayTool\LoLReplayTool.exe --self-check` が終了コード0になる
- [ ] OBS、mpv、FFmpeg未配置の警告が想定どおりで、必須診断は成功する
- [ ] ビルド成果物へOBSまたはstandalone FFmpegが混入した場合は検査が失敗する
- [ ] ビルド成果物のrootまたは下位directoryへ`libmpv-*.dll`／`mpv-*.dll`が混入した場合は、対象pathと`%LOCALAPPDATA%\LoLReplayTool\bin`への利用者配置方針を表示して検査が失敗する

## インストーラー確認が必要なケース

次の変更では `pwsh -NoProfile -File .\scripts\build_installer.ps1` による生成と
実際のインストール確認を行います。

- [ ] `installer/LoLReplayTool.iss`、`VERSION`、インストール先、ショートカットを変更した
- [ ] PyInstallerの成果物構成、データファイル、exe名を変更した
- [ ] 更新・アンインストール・ユーザーデータ保持方針を変更した
- [ ] 新しいランタイム依存ファイルや権限要件を追加した

確認時は、新規インストール、上書き更新、アンインストールを行い、設定・録画の保持、削除オプション、スタートメニュー、管理者権限不要の動作を記録します。

Microsoft Visual C++ 2015–2022 Redistributable x64を外部前提とする変更では、次の結果も記録します。

- [ ] x64 Redistributable未導入、`Installed`欠損、Version欠損、必要Version未満、x86のみ、registry不整合で、ファイル変更・既存アプリ変更・shortcut作成・user data変更なしにfail-closedとなる
- [ ] x64 Redistributable導入済みでは新規インストールと上書き更新が成功し、より新しい互換Versionも受け入れる
- [ ] 対話時の不足案内はMicrosoft公式ページだけを示し、同意した場合だけブラウザーを開く。silent modeは対話・ブラウザーなしで明確な非0終了となる
- [ ] Runtime DLLおよび`vc_redist.x64.exe`がアプリ、installer、Release assetへ存在せず、自動download/install/UAC昇格も行われない
- [ ] dist、完成installer展開物、Release assetの監査が、大小文字やサブディレクトリにかかわらず`msvcp*.dll`、`vcruntime*.dll`、`vcomp*.dll`、`concrt*.dll`とハッシュ付きRuntime importを拒否する
- [ ] custom wheelの固定入力、source archive、tool、SHA256、PE import変換前後、provenanceを再実行して同一結果となり、未知対象・件数差異・hash変更を拒否する

上書き更新の安全終了を変更した場合は、次も記録します。

- [ ] アプリを既定設定でタスクトレイへ格納した状態から上書き更新し、アプリ、録画監視、管理対象OBSが通常終了して更新が続行される
- [ ] 実際のLoL試合を録画中に上書き更新を開始すると、更新だけが中止され、アプリと管理対象OBSが録画を継続し、試合終了後の再試行を案内する
- [ ] 通常版OBSだけを起動した状態、および通常版OBSと管理対象OBSを同時に起動した状態で、通常版OBSへ終了signalを送らない
- [ ] 安全終了protocolを持たない旧versionのアプリが起動中の場合、process名による強制終了を行わず、タスクトレイの「終了」から終了するよう案内して更新を中止する
- [ ] 管理対象OBSのidentity確認失敗、通常終了timeout、設定workerの停止timeoutを再現し、install先を更新せずアプリを残して復旧案内を表示する
- [ ] 成功、録画中拒否、timeoutの各結果で、設定、録画ファイル、セッションログ、OBS所有情報が破損していない
