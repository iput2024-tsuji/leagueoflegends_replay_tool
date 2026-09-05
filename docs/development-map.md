# 開発マップ

LoL Replay Toolの変更箇所を判断するための開発者向け地図です。利用者向けの概要は `README.md`、ブランチ・PR・Release運用は `CONTRIBUTING.md`、実機確認は `docs/manual-test-checklist.md` を参照してください。

## 主要ファイル一覧

| ファイル | 主な責務 | 変更時に確認すること |
| --- | --- | --- |
| `main.py` | GUI起動、`--self-check` の入口 | GUIなし診断、終了コード、PyInstaller起動 |
| `src/app.py` | PyQt6画面、画面遷移、`RecorderWorker`、設定UI | UIスレッドを止めないこと、ワーカー終了、手動GUI確認 |
| `src/controllers.py` | UIから設定・録画・分析処理を呼ぶ境界 | UIと外部依存を直接結合しないこと |
| `src/recording_supervisor.py` | 監視、録画、終了、保存、通知をつなぐアプリケーションフロー | 正常終了、部分保存、中断、次試合監視 |
| `src/recordtest.py` | `RecordingSessionManager`、`LoLAutoRecorder`、録画状態遷移とセッション統合、既存import互換facade | 非同期状態遷移、外部API失敗、既存テストのモック境界 |
| `src/recorder_config.py` | `AppConfig`、設定値の構造化と読み込み、ユーザーデータ配下のパス解決 | 既定値、相対・絶対パス、開発版と配布版の保存先 |
| `src/riot_api.py` | `RiotAPIClient`、Live Client API／LCUの非同期取得、レスポンス解析、poll状態の区別 | 未起動、404、認証失敗、タイムアウト、一時障害と試合外の区別 |
| `src/storage_policy.py` | 容量上限、アプリ所有の動画・クリップ判定、上限超過時の安全な削除 | 設定済みの保存先以外を削除しないこと、保持対象、壊れたJSON、削除失敗 |
| `src/obs_websocket_client.py` | `ObsWebSocketClient`、OBS接続、request/response、シーン・入力・録画・音声制御 | 認証、タイムアウト、再試行、`recordtest`のimport・monkeypatch互換性 |
| `src/obs_runtime.py` | OBSプロセス所有権とRecorder生成 | 管理対象OBSだけを制御・終了すること |
| `src/obs_process.py` | OBS起動、プロセス探索、ログ診断、終了 | Windows実機、既存OBSとの衝突、プロセス取り違え |
| `src/obs_bootstrap.py` | portable OBSの設定ファイル生成・補正 | 既存設定の移行、WebSocket認証、INI互換性 |
| `src/obs_transaction_fs.py` | OBS移行のhandle相対filesystem primitive、物理identity、metadata検査 | alias、ADS、ACL、unsupported filesystemのfail-closed |
| `src/recording_state.py` | 録画状態、終了理由、終了判定 | API一時障害と本当の試合終了を区別すること |
| `src/session_log.py` | JSONスキーマ、読み込み、移行、原子的保存 | 後方互換性、破損ファイル、保存失敗 |
| `src/recording_library.py` | 動画、JSON、関連クリップの安全な削除 | 設定ディレクトリ外を削除しないこと |
| `src/player.py` | リプレイ一覧、mpv再生、同期、FFmpegクリップ出力 | mpv DLL、動画欠落、ワーカーキャンセル、FFmpeg |
| `src/analytics.py` | JSON集約、特徴量生成、決定木分析 | 少数データ、壊れたJSON、分析結果の説明 |
| `src/config_schema.py` / `src/config_store.py` | 設定の既定値・補正・保存・移行 | 古い設定、原子的保存、秘密値をログへ出さないこと |
| `scripts/build.ps1` | PyInstallerによるWindows onedirビルド | 同梱物、hidden import、self-check |
| `scripts/build_installer.ps1` / `installer/LoLReplayTool.iss` | テスト、ビルド、self-check、Inno Setup | 新規・更新・アンインストール、ユーザーデータ保持 |

## 録画開始の流れ

```text
MainWindow
  -> RecorderWorker (QThread + asyncio event loop)
  -> RecordingSupervisor.run()
  -> ConfigController.run_preflight()
  -> RecordingController / OBSRuntimeManager.open_recorder()
  -> LoLAutoRecorder.wait_for_game_start_async()
     -> LCU gameflow / champion select
     -> Live Client API / LoLゲームプロセス
  -> LoLAutoRecorder.start_recording_async()
  -> ObsWebSocketClient.start_recording()
```

起動前に設定補正、管理対象OBSの確認、WebSocket接続、音声設定を行います。試合開始検知はLCUだけに依存せず、Live Client APIやゲームプロセスの状態も扱います。検知条件を変更した場合は、Dodge、再接続、APIタイムアウト、LoL未起動、前試合の状態残りをテストします。

## 録画終了の流れ

```text
RecordingSupervisor
  -> LoLAutoRecorder.record_until_end_async()
  -> RecordingEndDetector
     -> GameEndイベント
     -> Live Client API状態
     -> LCU gameflow
     -> LoLゲームプロセス
  -> ObsWebSocketClient.stop_recording()
  -> LoLAutoRecorder.finalize_session()
```

一時的な通信失敗、試合データ欠落、ゲームプロセス終了には猶予時間があります。正常終了は `COMPLETED`、録画途中の失敗は `FAILED_PARTIAL`、利用者終了などは `ABORTED` として扱い、保存可能なセッション情報を失わない設計です。終了判定の変更では、早期停止と終了しない状態の両方を回帰確認します。

## JSON保存の流れ

`LoLAutoRecorder.finalize_session()` が試合、Ban/Pick、イベント、動画パス、同期時刻、終了状態をまとめ、`save_json()` から `session_log.py` の保存処理へ渡します。JSONは一時ファイルを経由して原子的に置換され、`schema_version` によって将来の移行を管理します。

スキーマ変更では次を守ります。

- 既存キーを安易に削除・改名しない
- `SessionLogV1`、読み込み移行、プレーヤー、分析の利用箇所を同時に確認する
- 古いJSON、欠落キー、壊れたJSONをテストする
- 動画パスは設定した録画ディレクトリから安全に再解決できる形式を維持する

## リプレイ再生の流れ

```text
PlayerPage / ReplaySelectDialog
  -> 設定されたJSONディレクトリを列挙
  -> session_log.pyで読み込み
  -> obs_record_pathを録画ディレクトリから解決
  -> PlayerWidget / PlayerRuntime
  -> python-mpv + mpv DLLで再生
  -> JSONイベント時刻 + sync_game_timeでシーク
```

クリップ出力は別ワーカーが固定配布元からFFmpegを準備し、対象区間を書き出します。再生関連の変更では、動画・JSONの欠落、Windowsパス、mpvロード失敗、同期補正、ワーカーのキャンセルと画面離脱を確認します。削除は `RecordingLibrary` を通し、動画、JSON、関連クリップ以外へ範囲を広げないでください。

## 分析処理の流れ

```text
AnalyticsPage
  -> AnalyticsWorker
  -> AnalyticsController
  -> GameDataAnalyzer.load_dataframe()
  -> セッションJSONを試合・イベント行へ変換
  -> 15分以内イベントと敵チャンピオンを特徴量化
  -> pandas DataFrame
  -> scikit-learn DecisionTreeClassifier
  -> 勝率サマリーと戦術インサイトをUI表示
```

分析は勝敗両方を含む十分な試合数がない場合に「データ不足」を返します。特徴量追加時は名称衝突、欠損値、古いJSON、少数データ、同一クラスだけのデータを確認し、結果を因果関係ではなく観測傾向として表示します。

## 変更時の注意点

- PyQt6のGUIスレッドで通信、ファイル走査、学習、待機を実行しない
- `QThread` とasyncioワーカーには停止要求、待機上限、参照寿命のテストを用意する
- OBS、LCU、Live Client APIは実環境なしでモックできる境界を維持する
- 設定・JSONの互換性と原子的保存を保ち、利用者データを破壊しない
- ファイル削除とプロセス終了は、アプリが所有・管理する対象だけに限定する
- 利用者向け変更では日英README・CHANGELOGの同期要否を判断する
- 変更に対応するテストを選び、外部依存部分は手動チェックリストの結果をPRへ残す

## 実環境依存の注意点

### OBS

OBS Studioは利用者が公式Releaseから明示的に入手し、専用`obs-portable`へ配置します。本プロジェクトは自動取得、ミラー、同梱、再配布を行いません。アプリ管理のportable OBSだけを対象とし、通常版OBSや他プロセスが使用するWebSocketポートを制御しません。WebSocketはローカル接続でもパスワード認証を必須とします。シーン、音声、録画エンコーダ、起動・終了を変更した場合は実際のOBSログと録画ファイルを確認します。

起動前設定は`src/obs_bootstrap.py`の共通transactionで更新します。bootstrap設定、WebSocket設定、選択中の`user.ini`、既存と管理対象の全録画profileを1回のpreflightで固定し、bootstrapとprofileが同じoriginal `user.ini` snapshotから最終payloadを合成します。変更対象だけでなく計画に含まれる未変更targetも停止前後とcommit直前に再検証します。desired payloadは設定fileの読み取り上限以内であることをjournal作成前に検査します。設定更新とOBSコピー移行は同じprocess間lockを使いますが、settings journalとcopy journalの状態判定は分離します。設定のstale状態を未完了コピーとして扱ってはいけません。

settings transactionの書き込み境界はlock取得時にopenしたmanaged rootの物理directory leaseです。directory作成、journal／一時file書き込み、replace、deleteはそのhandleから相対的に行い、各phaseと停止callbackの前後でlock fileとrootのphysical identityを再検証します。lock取得後にmanaged rootの名前や実体が差し替わった場合は、新しいlexical rootへ書き込まずRecoveryとして停止します。

settings transactionは次の順序を守ります。

1. `preparing`: journalを永続化し、既存targetのrollback backupと全desired一時fileを同じdirectoryへ書き、`fsync`、再open、size・SHA-256照合を完了する。このphaseではtargetを変更せず、復旧も所有中の一時fileとjournalだけを除去する。
2. strict process queryはErrorAction Stop付きPowerShellでPIDと絶対executable pathを列挙し、各rowをPython側の`OpenProcess` handleから取得したPID、lexically equivalent path、raw creation FILETIMEへ置き換えて完全identityを固定する。query失敗、stderr、欠落row、handle不一致、handle close失敗を0件とは扱わない。CIMが終了済みprocess objectを返しても、row bind handleがsignaledならactive snapshotから除外する。管理対象外OBSがないことを確認してから、停止APIはこの完全なprocess identity集合を受け取り、複数PIDのgraceful／force signalそれぞれの直前に再照会して、未signal対象の存在、signal済み対象の同一identityまたは消滅、extra processがないことを検証する。終了直後の遷移で同じexited identityが1回だけ残った場合はidentity単位で一度だけ再照会し、反復すればfail-closeする。`taskkill`の非0終了はsignal成功として帰属しない。停止後もstrict queryを行い、停止前の全管理processがidentity付きsignal結果またはGPU再起動対象のPopen handleと結び付いた既知processで説明でき、全OBS processが消えたことを確認する。停止前後で全snapshot、profile directory、欠落していた`basic.ini`を再検証し、OBS終了時flushまたは競合変更を検知する。
3. `committing`: phase更新を永続化してからdesiredを確定する。全targetを安全に再openし、desiredのsize、SHA-256、replaceしたfile identityを確認できるまで`committed`へ進まない。途中終了時は全targetをoriginalへrollbackする。
4. `committed`: 全targetのdesired一致を確認し、desiredを維持したままbackup、一時file、journalを清掃する。committed journal更新または親directoryの永続化結果が不明な場合は同じprocessで清掃せず、backupとmarkerを残して次回のdurable phase判定へ委ねる。

停止直後の変更が、存在状態とsecurityを維持した既存の`global.ini`、`user.ini`、WebSocket `config.json`、またはprofile `basic.ini`だけに限られ、停止前に管理OBSが実在したことをsealed evidenceで証明できる場合だけ、同一の外側mutation guardと同一root leaseを保持したまま一度だけ再計画します。全transaction targetが停止前から存在し、fresh planで実際に変更するfileもこの既知設定範囲だけであることが追加条件です。fresh planのfile identity、size、SHA-256、security、directory identity／security、全ancestor、portable marker、profile直下のname／kind／identity inventory、target／directory／validation setが停止直後の非機密observationと完全一致しなければcommitしません。retryのcommit直前にはread-onlyのstrict queryでOBS processが0件であることを再確認し、二度目の停止、kill、または再計画は行いません。初回no-opでも停止flushでfresh planが変更ありになった場合はfresh plan由来の変更結果を返します。

同一ユーザー権限の外部writerが同じ既知設定fileをOBS停止callbackと同時に更新した場合、その書き込みをOBS自身のflushと完全には識別できません。この制約は、strictな停止前後process証跡、既知path限定、全targetの存在維持、content digest、identity、security、集合、ancestor、profile topologyの完全一致によって許容範囲を狭めます。いずれかを証明できなければ従来どおりfail-closeします。観測対象はplanの入力、target解決、containment、alias安全性に影響する範囲です。`logs`配下やprofile directory内の無関係なencoder JSONなど、planが列挙も参照もしないvolatile siblingの内容はrecursiveにhashせず、profiles root直下の構成と各対象`basic.ini`を固定します。`stop_managed_processes=False`、portable markerだけの更新、migration finalizer、共通low-level transactionを直接使う内部経路には自動再計画を接続しません。

Windowsでは各signal直前のstrict再照会後に`OpenProcess`し、handle由来のPID、lexically equivalentな実行file、`GetProcessTimes`のraw creation FILETIMEを完全一致で再照合します。millisecond単位へ丸めた`creation_time`比較はraw FILETIMEを持たないlegacy／非Windows test double向けのfallbackだけです。検証済みhandleはgraceful `taskkill`の発行中から最終strict zero snapshot取得後まで保持するため、その間はprocess objectとPIDの対応が解放されません。strict再照会から`OpenProcess`までに元processが終了・PID再利用された場合も、handle identity不一致としてreplacementをsignalせずfail-closeします。timeout時のforceはPIDを再指定せず、同じhandleへ`TerminateProcess`します。GPU再起動では起動直後のstrict identityをPopen handleへ固定し、停止直前にhandleが生存中かつidentityが完全一致する場合だけ既知processとして扱います。元handleの終了を確認できない場合や、同じPIDが別creation FILETIMEで再出現した場合は、replacementをsignal済みとして除外せずfail-closeします。

通常のruntime起動は、Popenを生成する前にmutating process lease transactionを取得します。同じtransaction内で既存leaseの不存在を確認し、strict process snapshotを検証して同じmanaged executableが動作していないことを確認し、root／lockとlease不存在を再検証してからPopenへ進みます。v1／v2／破損lease、strict query失敗、同じmanaged executable、lock競合ではPopenを生成せず、既存processへsignalしません。管理対象外の別pathにあるOBSだけはこのadmissionを妨げません。

Windowsのruntime起動admissionは、path文字列ではなく同時にopenしたexecutable handleのvolume serialと128-bit file IDでmanaged executableとの物理同一性を判定します。8.3短縮名、volume GUID path、hardlinkのように物理identityが同じ別表記はmanagedとして扱い、安定した別identityのOBSは管理対象外として起動を妨げません。managed pathとstrict snapshotの各候補は、rootからexecutableまでの全namespace componentを`FILE_SHARE_DELETE`なし・`FILE_FLAG_OPEN_REPARSE_POINT`で固定し、regular file、非reparse、`QueryDosDevice` mapping、各componentのidentityをPopen直前と直後に再検証します。junction、symbolic link、SUBST／DOS-device mapping、identity取得不能、途中差し替えはunsupportedまたは不明としてfail-closeし、Popenも既存processへのsignalも行いません。

新規processはCreateProcess互換の通常絶対managed executable pathとその親directoryを`cmd[0]`／`cwd`へ渡す一方、固定handleから得たvolume GUID pathは物理identity照合用に保持し、Popen imageがmanaged executableと物理的に同じことを確認してからschema v2 leaseへbindします。lease内のpathは利用者が確認できるcanonical managed pathのままです。owned判定では物理同一性に加えてPIDとraw creation FILETIMEの完全一致を維持するため、same-PID replacementを認可しません。namespace handleはlease bindまで保持される短い間だけrename／deleteを抑止します。bind後のhandle close失敗は完了済みの所有権を反転させずcritical診断として残します。`is_managed_process()`は表示・探索向けのbest-effortな真偽値ですが、起動admissionとsignal認可は不明をfalseへ弱めず型付きエラーにします。

通常のruntime起動で作成するprocess leaseはschema v2とし、PID、絶対executable path、Popenが所有するhandleから得たraw creation FILETIMEを保存します。起動直後にこの完全identityを確立できなければ、admissionから保持している同じtransaction内で、そのPopen handleだけを停止して起動失敗にします。停止にも失敗した場合はPIDを示して手動終了を要求し、identityなしのprocessを成功扱いで返しません。旧schemaのleaseは読み取り互換のため残しますが、live processの自動停止権限には使用しません。破損lease、query失敗、identity不一致、same-PID replacementではleaseを維持してfail-closeし、利用者へOBSの手動終了と再試行を案内します。破損状態が解消しない場合は、画面に表示したleaseの絶対pathを、全OBSの終了確認後にだけ退避または削除します。

process leaseの読み取り、探索、作成、通常終了、stale owned cleanup、明示clearは、thread内lock、専用の永続process間lock `.lol_replay_obs_lease.lock`、固定したroot／file handleの順で同じtransactionに入ります。起動transactionはadmissionより前からno-clobber publish、確定descriptorからの再読込、Popenへのlease bindまで保持するため、同じmanaged rootの協調starterはPopen生成前に直列化されます。書き込みは同じroot handleに対する排他的な `.lol_replay_obs_lease.tmp.<32 lowercase hex>` を`fsync`してからno-clobberでatomic publishします。Popen後のidentity取得、publish、またはlease bindが失敗した場合もtransactionを保持したまま元のPopen handleだけをcleanupし、PID再指定や他processへのsignalは行いません。publish前の失敗では所有一時fileを回収し、publish済みまたはcommit不確実な失敗では主leaseを推測削除せず次回起動をfail-closeさせます。強制終了で残った厳密形式の一時fileは次のtransactionで同じhandleから回収し、予約prefixの不正な名前、物理identity／root binding不一致、回収不能はfail-closeします。管理rootとcontrol namespaceがともに存在しない読み取りはrootやlockを作成しません。回復案内にはleaseとlockの絶対pathを併記し、すべてのOBSと関連toolを終了してから再試行するよう示します。

Windowsでleaseを開くときは、同じfile handleからraw bytesとphysical identityを取得し、regular file、single link、non-reparse、root binding、size上限、対応schema（v1／v2）を検証します。旧schemaは読み取り互換だけに使用し、live processへのsignalを認可する時点では完全identityを持つschema v2を必須にします。このhandleは`FILE_SHARE_READ`だけを許可して保持するため、transaction外からのin-place write、replace、deleteを停止権限の確認中に受け入れません。通常の`Popen`終了とstale owned cleanupはいずれも、graceful signalの直前とforce signalの直前に、固定したleaseのraw bytes／identityと対象process identityを再検証します。一度でも認可を再証明できなければ、そのsignal以降を発行せずleaseを維持します。POSIXでは同じhandle相対検証とprocess間lockを行いますが、非協調processによるpathname差し替えをWindowsのshare modeと同じ強度では禁止できないため、保証は同じlockを守るwriter間の協調に限定されます。

runtimeのstale owned cleanupは、設定transactionの全OBS zero条件とは目的が異なります。leaseと完全一致する1 processだけを検証済みhandleへ結び付けてgraceful／force停止し、通常版など無関係なOBSは維持します。leaseの読み書きと一致確認後の削除は同じtransaction内で直列化し、runtime cleanupもOBS操作lock内で行います。対象identityの消滅を確認してから、固定したlease handleへWindowsのdelete-on-close（POSIXでは同じdirectory leaseからの相対unlink）を設定して削除します。削除確定後に別writerが新しいleaseを作成しても、その新しいfileは古いhandleのcloseで削除しません。削除／publishという不可逆commit後のdescriptor closeやlock release失敗は、完了済みの結果を失敗へ反転せず診断として記録します。

通常起動したOBSの`Popen` cleanupは、stale-owned cleanupや設定transactionの全OBS zero判定とは別の契約です。PID列挙、`taskkill`、strict process queryを使用せず、呼び出し元が保持する同じ`Popen`の`poll`、`terminate`、bounded `wait`、`kill`だけを使用します。graceful waitのtimeout後は同じ`Popen`へforce cleanupを行い、Windowsでは保持handleのsignaled状態を最終根拠にします。実行pathはprocessがliveの間に検証してfrozen v2 leaseへbindし、既に終了したhandleでは終了後も取得できるPIDとraw creation FILETIMEを再照合するため、終了後に失敗し得る`QueryFullProcessImageNameW`を要求しません。live path照会中に自然終了したraceも同じhandleのsignaled状態を再確認した場合だけこの照合へ移ります。予期しないAPI例外、最終残存、handle identityとbound v2 leaseの不一致、leaseの破損・差し替え・削除失敗は型付き失敗として呼び出し元へ返します。完全identityが一致する元handleの終了を確認した場合だけleaseを削除し、同じPIDのreplacementや他のOBSへsignalしません。WebSocket切断などの後続cleanupは終了失敗後も試行し、先行失敗を主因として追加cleanup失敗をexception noteとlogへ残します。起動失敗cleanupを一度試した`Recorder`は上位runtimeから同じ`Popen`へ重複signalしません。

`Popen`生成後から`RecorderRuntime`、CLI、設定操作へ所有権を返すまでの起動境界は、通常の`Exception`だけでなく`KeyboardInterrupt`、`SystemExit`、`asyncio.CancelledError`などの`BaseException`系中断も同じcleanup契約で扱います。identity取得、lease publish／bind、transaction終了、portable mode確認、エンコーダ検出／GPU再起動、WebSocket client／Recorder／runtime構築、`Recorder.open()`のいずれで中断しても、取得済みの同じ`Popen` handleと部分接続だけを一度cleanupし、元の中断例外objectを再送出します。先行する通常`Exception`の後にcleanupで最初のcontrol-flow中断が発生した場合は、そのcleanup中断の同一objectを主因とし、先行失敗をnote／logへ移します。先行失敗がすでにcontrol-flow中断の場合はそれを保持し、後続cleanup中断をnote／logへ残すため、複数cleanupを通して最初のcontrol-flowだけが主因になります。既存のcause、context、suppress-contextは書き換えません。元handleの終了を証明できない場合やpublish／bind状態が不確実な場合は主leaseを推測削除しません。GPU停止を一度試した後の中断では同じhandleへ再signalせず、手動終了と再試行を案内します。`Recorder.open()`内でcleanupを開始した印は呼出前に立て、同期設定、runtime、CLIの外側cleanupはその印を見て二重shutdown／disconnectを避けます。`force_launch`で既存のowned OBSを引き継いだruntime構築が失敗した場合は、部分接続のcleanup後にlease検証済みstale-owned cleanupを一度だけ試し、PID fallbackや管理対象外OBSへのsignalを行いません。`RecordingSupervisor`は返却されたruntimeを自身へ保存してからrecorderを取得し、保存後の中断ではrecorderが未設定でもruntimeを一度closeします。終了前段の停止要求、finalize、録画停止が中断しても所有runtimeのcloseまたはfallback shutdownを一度試し、同じfirst-control-flow契約で失敗を統合します。factoryの正常returnから呼出側の`STORE_ATTR`までのinterpreter bytecode間には返却objectをPythonコードから参照できない不可避の境界があるため、factoryは正常return直前まで全失敗cleanupを担当し、Supervisorの保証はruntime保存完了後から始まります。CLIは初期化完了前の`KeyboardInterrupt`をcleanup後に再送出し、所有権引渡し後の通常のCtrl+C終了だけは従来の正常終了動作を維持します。

process lease control namespaceのlockと厳密形式の一時fileは、OBSコピー元・設定inventory・migration finalizerの管理対象から除外します。ただし主lease `.lol_replay_obs_lease.json` はruntime所有権の実データなので、migration finalizer前後の外部変更検知から除外しません。予約prefixに似た不正な一時file名は無視せずRecoveryにします。

journalにはrelative path、label、size、SHA-256、所有tokenだけを記録し、WebSocket passwordを含むoriginal／desired bytesは記録しません。次回のアプリ起動、「OBS設定を構成・再検査」、または`scripts/setup_env.py`が同じlock下で自動復旧します。復旧エラー時はOBSをすべて終了し、`obs-portable`全体を変更せず再検査します。markerや一時fileだけを手動削除せず、解消しない場合は`obs-portable`全体とログを退避してから利用者が公式Releaseを再配置します。復旧処理がOBSや他のバイナリを自動取得することはありません。

OBSコピー移行は、表示上のpath文字列ではなくopen済みdirectory handleの物理identityを境界にします。Windowsではvolume serialとfile IDを使い、8.3名、SUBST、volume GUIDなどの別表記がsource／destinationの同一directoryまたはancestor treeを指す場合に拒否します。junctionとsymbolic linkはreparse pointとして拒否します。POSIXではmanaged rootへ到達してからdeviceと`/proc/self/fdinfo`のmount IDを固定し、全descendant directory／file openで同一であることを要求します。これによりLinux bind mountを含むnested mountをsource／destinationの双方で拒否します。`/proc/self/fdinfo`、handle相対I/O、xattr検査のいずれかを提供しないPOSIX runtime／filesystemでは自動移行を開始せず、対応環境上の実pathへ戻してRecoveryする必要があります。

Windowsの管理treeでは、root自身を含む各entryのhandleからowner／group／DACL、file attributes、regular fileのcreation／last-write timeを取得し、名前付き`:$DATA` streamがあれば拒否します。pathへ`:stream`を連結して内容を開くことはありません。POSIXではroot自身を含めてmode、uid、gid、xattr（POSIX ACLを含む）、regular fileのmtime／ctimeを検査します。読み取りで変わり得るatimeと、子entry操作で正当に変わるdirectory時刻は不変条件に含めません。WindowsのSACL／監査情報は通常権限で安定して取得できないため不変条件の対象外です。sourceとdestinationの間ではcontentだけを照合し、metadataはコピーしたものとして扱いません。同一treeの再走査とfinalizer前後ではroot metadataも比較し、transaction管理fileと明示されたfinalizer allowlist以外のmetadata-only変更を拒否します。

### LCU / Live Client API

LCUは正式なサードパーティ向けAPIではなく、LoLクライアント更新で変わる可能性があります。ローカル自己署名証明書、短命な認証情報、クライアント未起動、404、認証失敗、タイムアウトを通常の状態として扱います。認証情報をログ、Issue、PRへ貼り付けないでください。

### mpv

mpv DLLはリポジトリと配布物へ同梱しません。開発環境では `bin/`、配布版ではユーザーデータ配下から探索します。DLL名、Pythonバインディング、Windows DLL探索順の変更はビルド版でも確認します。

### FFmpeg

standalone FFmpegは利用者が明示的に入手・配置し、本プロジェクトは自動取得、ミラー、同梱、再配布を行いません。探索順は明示設定、データ用`bin`、アプリルートの`bin/ffmpeg.exe`と`ffmpeg.exe`、安全な絶対ディレクトリのシステム`PATH`です。設定、fallback、未配置時の案内、クリップ出力、キャンセルを確認し、standalone FFmpeg本体を成果物へ混入させないでください。配布物に含まれるOpenCV FFmpeg DLLは別componentです。

### PyInstaller

Windows向け配布はonedir形式で、依存物を `_internal` に配置します。新モジュール、動的import、データファイル、アイコン、ランタイム依存を変更した場合は `scripts/build.ps1` とパッケージ済みexeの `--self-check` を実行します。ビルド成功だけでなく、OBS、mpv DLL、FFmpeg、設定、録画が意図せず同梱されていないことも確認します。

正式なWindows buildでは`compliance/components.json`の`opencv_source_build_policy`と`scripts/prepare_opencv_wheel.py`を使用し、固定した`opencv-python`／OpenCV／OpenCV FFmpeg入力からIPP無効のOpenCV wheelを構築します。CIとReleaseは共通の`build-opencv.yml`を呼び、Visual Studio 2022を提供する`windows-2022`で2回のclean buildを行います。scikit-build標準の`v143`指定を使い、生成されたcompilerのtoolset directoryが固定版`14.44.35207`以外なら拒否します。アプリbuild・self-check・installer監査は`windows-2025`で行い、同じworkflow runのartifact ID、checkout commit、provenance SHA256を照合してwheelを再監査します。Server 2022上のbuild成功をWindows 11実機検証の代わりにはしません。byte-identicalでない場合もwheel内容、PE import graph、IPP／FFmpeg状態、synthetic動画読込、同期マーカー用primitiveのsemantic manifestが一致しなければ停止します。元のPyPI OpenCV wheelは正式build用binary cacheへ取得せず、生成wheelのSHA256、実際に選択されたMSVC toolset／Windows SDK、固定FFmpeg DLLとの対応をbuild provenanceへ残します。失敗したsource buildのログと2回分の診断JSONは7日間のActions artifactとして保持します。

### Inno Setup

正式対応OSはWindows 11（build 22000以降）です。配布物はx64版で、installerの`ArchitecturesAllowed=x64compatible`はx64 WindowsとWindows 11 ARM64のx64エミュレーションを許可しますが、ARM64-native版は提供しません。PyQt6-Qt6 6.10.2のQt6Coreが参照するICUはWindows標準のSystem32版を利用し、ICU DLLを配布物へ同梱しません。Windows 10対応はLTSC 2019等の対象環境で同じnativeロードとQt locale／Unicode検証が完了するまで宣言しません。installerの`MinVersion`、`ArchitecturesAllowed`、README日英版を同期してください。最終PyInstaller onedirのICU graph確認には`python -m scripts.pe_runtime_audit <dist> --require-qt-system-icu`を使用します。

インストール先はユーザー単位で、設定・ログ・録画などの可変データはアプリ更新から分離されています。インストーラー定義や成果物構成を変更した場合は、新規インストール、上書き更新、アンインストール、データ保持・削除選択をWindows実機で確認します。

上書き更新前の終了連携は、`installer/LoLReplayTool.iss`の`PrepareToInstall`からユーザーsession内のnamed eventをsignalし、アプリが返す安全終了完了eventと`src/single_instance.py`で保持するsingle-instance mutexの消失を両方待ちます。アプリは`src/app.py`から既存のworker終了chainへ入り、`src/recording_supervisor.py`と`src/obs_runtime.py`を通してstrict ownershipを確認できた管理対象OBSだけへ通常終了を要求します。録画開始との境界はsupervisor内のlockで確定し、録画中は更新要求を拒否します。旧version、完了通知前のアプリ異常終了、identity不明、worker停止失敗、管理対象OBSの通常終了timeoutではinstallerをfail-closeし、process名だけの終了やforce killへfallbackしません。

Windows x64のインストール・上書き更新では、この安全終了処理より前にMicrosoft Visual C++ 2015–2022 Redistributable x64の前提条件を検査します。HKLM64/HKLM32のregistry viewから`Installed`と`Version`を確認し、最低`14.44.35211.0`未満、欠損、不整合、x64 Runtime不在はfail-closedとします。より新しい互換Versionは許可します。不足時はMicrosoft公式案内を示しますが、ブラウザー起動は対話時の同意後だけで、silent modeは非0終了します。Runtime DLLと`vc_redist.x64.exe`のダウンロード・同梱・自動実行・UAC昇格は行いません。

custom native wheelの変更は、固定入力のSHA256、source archive、tool version、PE importの変換前後、再現可能recipe、provenanceを記録します。dist、完成インストーラー展開物、Release assetでapp-localまたはハッシュ付きMicrosoft Runtime DLL/importを検査し、未知の名前や件数差異もfail-closedで拒否します。legal/source gateと公開Release停止は完了扱いにしません。
