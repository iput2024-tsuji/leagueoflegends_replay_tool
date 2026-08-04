# 開発マップ

LoL Replay Toolの変更箇所を判断するための開発者向け地図です。利用者向けの概要は `README.md`、ブランチ・PR・Release運用は `CONTRIBUTING.md`、実機確認は `docs/manual-test-checklist.md` を参照してください。

## 主要ファイル一覧

| ファイル | 主な責務 | 変更時に確認すること |
| --- | --- | --- |
| `main.py` | GUI起動、`--self-check` の入口 | GUIなし診断、終了コード、PyInstaller起動 |
| `src/app.py` | PyQt6画面、画面遷移、`RecorderWorker`、設定UI | UIスレッドを止めないこと、ワーカー終了、手動GUI確認 |
| `src/controllers.py` | UIから設定・録画・分析処理を呼ぶ境界 | UIと外部依存を直接結合しないこと |
| `src/recording_supervisor.py` | 監視、録画、終了、保存、通知をつなぐアプリケーションフロー | 正常終了、部分保存、中断、次試合監視 |
| `src/recordtest.py` | `LoLAutoRecorder`、OBSクライアント、Live Client/LCU連携、録画処理 | 非同期状態遷移、外部API失敗、既存テストのモック境界 |
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

通常のruntime起動で作成するprocess leaseはschema v2とし、PID、絶対executable path、Popenが所有するhandleから得たraw creation FILETIMEを保存します。起動直後にこの完全identityを確立できなければ、そのPopenだけを停止して起動失敗にします。停止にも失敗した場合はPIDを示して手動終了を要求し、identityなしのprocessを成功扱いで返しません。旧schemaのleaseは読み取り互換のため残しますが、live processの自動停止権限には使用しません。破損lease、query失敗、identity不一致、same-PID replacementではleaseを維持してfail-closeし、利用者へOBSの手動終了と再試行を案内します。破損状態が解消しない場合は、画面に表示したleaseの絶対pathを、全OBSの終了確認後にだけ退避または削除します。

runtimeのstale owned cleanupは、設定transactionの全OBS zero条件とは目的が異なります。leaseと完全一致する1 processだけを検証済みhandleへ結び付けてgraceful／force停止し、通常版など無関係なOBSは維持します。leaseの読み書きと一致確認後の削除は同一process内で直列化し、runtime cleanupもOBS操作lock内で行います。leaseは対象identityの消滅、最終strict snapshot、handle closeまで正常に確認できた場合だけ削除します。

通常起動したOBSの`Popen` cleanupは、stale-owned cleanupや設定transactionの全OBS zero判定とは別の契約です。PID列挙、`taskkill`、strict process queryを使用せず、呼び出し元が保持する同じ`Popen`の`poll`、`terminate`、bounded `wait`、`kill`だけを使用します。graceful waitのtimeout後は同じ`Popen`へforce cleanupを行い、Windowsでは保持handleのsignaled状態を最終根拠にします。実行pathはprocessがliveの間に検証してfrozen v2 leaseへbindし、既に終了したhandleでは終了後も取得できるPIDとraw creation FILETIMEを再照合するため、終了後に失敗し得る`QueryFullProcessImageNameW`を要求しません。live path照会中に自然終了したraceも同じhandleのsignaled状態を再確認した場合だけこの照合へ移ります。予期しないAPI例外、最終残存、handle identityとbound v2 leaseの不一致、leaseの破損・差し替え・削除失敗は型付き失敗として呼び出し元へ返します。完全identityが一致する元handleの終了を確認した場合だけleaseを削除し、同じPIDのreplacementや他のOBSへsignalしません。WebSocket切断などの後続cleanupは終了失敗後も試行し、先行失敗を主因として追加cleanup失敗をexception noteとlogへ残します。起動失敗cleanupを一度試した`Recorder`は上位runtimeから同じ`Popen`へ重複signalしません。

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

### Inno Setup

インストール先はユーザー単位で、設定・ログ・録画などの可変データはアプリ更新から分離されています。インストーラー定義や成果物構成を変更した場合は、新規インストール、上書き更新、アンインストール、データ保持・削除選択をWindows実機で確認します。
