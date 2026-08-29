#ifndef AppVersion
  #define AppVersion "0.1.2"
#endif

#define AppName "LoL Replay Tool"
#define AppExeName "LoLReplayTool.exe"
#define AppPublisher "LoL Replay Tool Contributors"
#define AppURL "https://github.com/iput2024-tsuji/leagueoflegends_replay_tool"

[Setup]
AppId={{B8D87E69-41F7-4B28-978D-2F8FA5AF4BE2}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
LicenseFile=..\LICENSE
InfoBeforeFile=THIRD_PARTY_NOTICES.txt
DefaultDirName={localappdata}\Programs\LoLReplayTool
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
MinVersion=10.0.22000
OutputDir=..\dist\installer
OutputBaseFilename=LoLReplayTool-Setup-{#AppVersion}
SetupIconFile=..\assets\app\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
VersionInfoCopyright=Copyright © 1997-2026 Jordan Russell. Portions Copyright © 2000-2026 Martijn Laan.
VersionInfoDescription=LoL Replay Tool Setup - Inno Setup https://www.innosetup.com
Compression=lzma2/ultra64
LZMAUseSeparateProcess=yes
SolidCompression=yes
WizardStyle=modern
UseSetupLdr=x86
CloseApplications=yes
CloseApplicationsFilter={#AppExeName}
RestartApplications=no
SetupMutex=LoLReplayToolInstallerMutex
ChangesAssociations=no
ChangesEnvironment=no
Uninstallable=not IsContentAuditMode
CreateUninstallRegKey=not IsContentAuditMode

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "デスクトップにショートカットを作成"; GroupDescription: "追加タスク:"; Flags: unchecked

[Files]
Source: "..\dist\LoLReplayTool\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{localappdata}\LoLReplayTool\bin"; Check: not IsContentAuditMode

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Check: not IsContentAuditMode
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon; Check: not IsContentAuditMode

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{#AppName} を起動"; Flags: nowait postinstall skipifsilent; Check: not IsContentAuditMode
Filename: "{localappdata}\LoLReplayTool\bin"; Description: "mpv DLL の配置フォルダーを開く"; Flags: shellexec postinstall skipifsilent unchecked; Check: not IsContentAuditMode

[Code]
const
  SynchronizeAccess = $00100000;
  EventModifyState = $0002;
  ErrorFileNotFound = 2;
  WaitObject0 = 0;
  WaitTimeout = 258;
  UpdateShutdownPollCount = 120;
  UpdateShutdownPollIntervalMs = 250;
  AppMutexName = 'Local\LoLReplayTool.SingleInstance';
  UpdateShutdownEventName = 'Local\LoLReplayTool.UpdateShutdown';
  UpdateShutdownBlockedEventName = 'Local\LoLReplayTool.UpdateShutdownBlocked';
  UpdateShutdownCompleteEventName = 'Local\LoLReplayTool.UpdateShutdownComplete';
  VisualCppRuntimeKey = 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';
  VisualCppRuntimeMinimumVersion = '14.44.35211.0';
  VisualCppRuntimeHelpURL = 'https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist';

function OpenMutex(DesiredAccess: LongWord; InheritHandle: BOOL;
  const Name: String): THandle;
  external 'OpenMutexW@kernel32.dll stdcall';
function OpenEvent(DesiredAccess: LongWord; InheritHandle: BOOL;
  const Name: String): THandle;
  external 'OpenEventW@kernel32.dll stdcall';
function SetEvent(EventHandle: THandle): BOOL;
  external 'SetEvent@kernel32.dll stdcall';
function ResetEvent(EventHandle: THandle): BOOL;
  external 'ResetEvent@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';

var
  MpvInfoPage: TOutputMsgWizardPage;
  DeleteManagedDataOnUninstall: Boolean;
  DeleteRecordingsOnUninstall: Boolean;

function IsContentAuditMode: Boolean;
begin
  Result := ExpandConstant('{param:contentaudit|}') = '1';
end;

function VisualCppRuntimeFailure(const Detail: String): String;
var
  ErrorCode: Integer;
begin
  Result := 'Microsoft Visual C++ 2015–2022 Redistributable x64 が必要です。' + #13#10 +
    Detail + #13#10 + VisualCppRuntimeHelpURL;
  if not WizardSilent then
    if MsgBox(Result + #13#10 + #13#10 +
      'Microsoft公式ページをブラウザーで開きますか？', mbError, MB_YESNO) = IDYES then
      if not ShellExec('open', VisualCppRuntimeHelpURL, '', '',
        SW_SHOWNORMAL, ewNoWait, ErrorCode) then
        Log(Format('Microsoft公式ページを開けませんでした: %d', [ErrorCode]));
end;

function TryParseVisualCppVersion(VersionText: String;
  var PackedVersion: Int64): Boolean;
begin
  if (Length(VersionText) > 0) and
    ((VersionText[1] = 'v') or (VersionText[1] = 'V')) then
    Delete(VersionText, 1, 1);
  Result := (VersionText <> '') and StrToVersion(VersionText, PackedVersion);
end;

function CheckVisualCppRuntime: String;
var
  Installed64: Cardinal;
  Installed32: Cardinal;
  Version64: String;
  Version32: String;
  Version64Packed: Int64;
  Version32Packed: Int64;
  MinimumVersionPacked: Int64;
  HasKey32: Boolean;
begin
  Result := '';
  if not IsWin64 then
  begin
    Result := VisualCppRuntimeFailure(
      'このインストーラーには64bit Windowsが必要なため、インストールを中止しました。');
    Exit;
  end;

  if not RegQueryDWordValue(HKLM64, VisualCppRuntimeKey, 'Installed', Installed64) or
    (Installed64 <> 1) or
    not RegQueryStringValue(HKLM64, VisualCppRuntimeKey, 'Version', Version64) then
  begin
    Result := VisualCppRuntimeFailure(
      '64bit版Runtimeの登録情報を確認できないため、インストールを中止しました。');
    Exit;
  end;

  if not TryParseVisualCppVersion(VisualCppRuntimeMinimumVersion,
    MinimumVersionPacked) then
  begin
    Result := VisualCppRuntimeFailure(
      'インストーラー内の必要version設定が不正なため、インストールを中止しました。');
    Exit;
  end;

  if not TryParseVisualCppVersion(Version64, Version64Packed) then
  begin
    Result := VisualCppRuntimeFailure(
      '64bit版Runtimeのversion情報が不正なため、インストールを中止しました。');
    Exit;
  end;

  if ComparePackedVersion(Version64Packed, MinimumVersionPacked) < 0 then
  begin
    Result := VisualCppRuntimeFailure(
      '64bit版Runtimeが必要なversion未満のため、インストールを中止しました。');
    Exit;
  end;

  HasKey32 := RegKeyExists(HKLM32, VisualCppRuntimeKey);
  if HasKey32 then
  begin
    if not RegQueryDWordValue(HKLM32, VisualCppRuntimeKey, 'Installed', Installed32) or
      (Installed32 <> 1) or
      not RegQueryStringValue(HKLM32, VisualCppRuntimeKey, 'Version', Version32) or
      not TryParseVisualCppVersion(Version32, Version32Packed) or
      (ComparePackedVersion(Version32Packed, Version64Packed) <> 0) then
    begin
      Result := VisualCppRuntimeFailure(
        'Runtimeの32bit/64bit registry viewが不整合なため、インストールを中止しました。');
      Exit;
    end;
  end;
end;

function QueryApplicationRunning(var QueryFailed: Boolean): Boolean;
var
  MutexHandle: THandle;
  LastError: LongWord;
begin
  QueryFailed := False;
  MutexHandle := OpenMutex(SynchronizeAccess, False, AppMutexName);
  if MutexHandle <> 0 then
  begin
    Result := True;
    if not CloseHandle(MutexHandle) then
      QueryFailed := True;
    Exit;
  end;

  LastError := DLLGetLastError;
  if LastError = ErrorFileNotFound then
    Result := False
  else
  begin
    QueryFailed := True;
    Result := False;
  end;
end;

function RequestSafeUpdateShutdown: String;
var
  RequestHandle: THandle;
  BlockedHandle: THandle;
  CompleteHandle: THandle;
  QueryFailed: Boolean;
  ApplicationRunning: Boolean;
  WaitResult: LongWord;
  CompleteWaitResult: LongWord;
  ShutdownCompleted: Boolean;
  HandleCloseFailed: Boolean;
  Attempt: Integer;
begin
  Result := '';
  ApplicationRunning := QueryApplicationRunning(QueryFailed);
  if QueryFailed then
  begin
    Result := 'LoL Replay Tool の実行状態を安全に確認できませんでした。' +
      'アプリとOBSを手動で終了してから更新を再試行してください。';
    Exit;
  end;
  if not ApplicationRunning then
    Exit;

  RequestHandle := 0;
  BlockedHandle := 0;
  CompleteHandle := 0;
  HandleCloseFailed := False;
  try
    RequestHandle := OpenEvent(EventModifyState, False,
      UpdateShutdownEventName);
    BlockedHandle := OpenEvent(SynchronizeAccess or EventModifyState, False,
      UpdateShutdownBlockedEventName);
    CompleteHandle := OpenEvent(SynchronizeAccess or EventModifyState, False,
      UpdateShutdownCompleteEventName);
    if (RequestHandle = 0) or (BlockedHandle = 0) or (CompleteHandle = 0) then
    begin
      Result := '起動中のLoL Replay Toolは更新用の安全終了に対応していません。' +
        'タスクトレイの「終了」からアプリを終了し、更新を再試行してください。';
      Exit;
    end;

    if not ResetEvent(BlockedHandle) then
    begin
      Result := '更新用の安全終了状態を初期化できませんでした。' +
        'アプリとOBSを手動で終了してから更新を再試行してください。';
      Exit;
    end;
    if not ResetEvent(CompleteHandle) then
    begin
      Result := '更新用の安全終了完了状態を初期化できませんでした。' +
        'アプリとOBSを手動で終了してから更新を再試行してください。';
      Exit;
    end;
    if not SetEvent(RequestHandle) then
    begin
      Result := 'LoL Replay Toolへ安全終了を要求できませんでした。' +
        'タスクトレイの「終了」からアプリを終了し、更新を再試行してください。';
      Exit;
    end;

    ShutdownCompleted := False;
    for Attempt := 1 to UpdateShutdownPollCount do
    begin
      WaitResult := WaitForSingleObject(BlockedHandle, 0);
      if WaitResult = WaitObject0 then
      begin
        Result := 'LoL Replay Toolは録画中か、安全終了を完了できませんでした。' +
          'アプリに表示された案内を確認し、録画中の場合は試合終了後に更新を再試行してください。';
        Exit;
      end;
      if (WaitResult <> WaitTimeout) then
      begin
        Result := 'LoL Replay Toolの安全終了結果を確認できませんでした。' +
          'アプリとOBSを手動で終了してから更新を再試行してください。';
        Exit;
      end;

      CompleteWaitResult := WaitForSingleObject(CompleteHandle, 0);
      if CompleteWaitResult = WaitObject0 then
        ShutdownCompleted := True
      else if CompleteWaitResult <> WaitTimeout then
      begin
        Result := 'LoL Replay Toolの安全終了完了通知を確認できませんでした。' +
          'アプリとOBSを手動で終了してから更新を再試行してください。';
        Exit;
      end;

      ApplicationRunning := QueryApplicationRunning(QueryFailed);
      if QueryFailed then
      begin
        Result := 'LoL Replay Toolの終了状態を安全に確認できませんでした。' +
          'アプリとOBSを手動で終了してから更新を再試行してください。';
        Exit;
      end;
      if not ApplicationRunning then
      begin
        if not ShutdownCompleted then
        begin
          CompleteWaitResult := WaitForSingleObject(CompleteHandle, 0);
          ShutdownCompleted := CompleteWaitResult = WaitObject0;
        end;
        if ShutdownCompleted then
          Exit;
        Result := 'LoL Replay Toolが安全終了完了前に終了したため、更新を中止しました。' +
          '管理対象OBSを手動で終了し、アプリを起動し直してから更新を再試行してください。';
        Exit;
      end;
      Sleep(UpdateShutdownPollIntervalMs);
    end;

    Result := 'LoL Replay Toolの安全終了が時間内に完了しませんでした。' +
      'アプリとOBSの状態を確認し、タスクトレイの「終了」から終了してから更新を再試行してください。';
  finally
    if CompleteHandle <> 0 then
      if not CloseHandle(CompleteHandle) then
        HandleCloseFailed := True;
    if BlockedHandle <> 0 then
      if not CloseHandle(BlockedHandle) then
        HandleCloseFailed := True;
    if RequestHandle <> 0 then
      if not CloseHandle(RequestHandle) then
        HandleCloseFailed := True;
    if HandleCloseFailed and (Result = '') then
      Result := '更新用の安全終了handleを解放できなかったため、更新を中止しました。' +
        'アプリとOBSを手動で終了してから更新を再試行してください。';
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  if IsContentAuditMode then
  begin
    Result := '';
    Exit;
  end;

  Result := CheckVisualCppRuntime;
  if Result <> '' then
    Exit;
  Result := RequestSafeUpdateShutdown;
end;

procedure InitializeWizard;
var
  MpvDir: String;
  NewLine: String;
begin
  if IsContentAuditMode then
    Exit;

  MpvDir := ExpandConstant('{localappdata}\LoLReplayTool\bin');
  NewLine := #13#10;
  MpvInfoPage := CreateOutputMsgPage(
    wpSelectTasks,
    'リプレイ再生に必要な mpv DLL',
    'mpv DLL はこのインストーラーに含まれていません。',
    'リプレイを再生するには、64bit 版の libmpv DLL を利用者が別途入手して配置する必要があります。' +
    NewLine + NewLine +
    '配置先:' + NewLine + MpvDir +
    NewLine + NewLine +
    '対応ファイル名:' + NewLine +
    'mpv-2.dll / libmpv-2.dll / mpv-1.dll / libmpv-1.dll' +
    NewLine + NewLine +
    'セットアップ完了画面から配置フォルダーを開くこともできます。' +
    NewLine + NewLine +
    'OBS と standalone FFmpeg も利用者が明示的に入手・配置してください。' +
    NewLine +
    '本アプリはこれらを自動取得、ミラー、同梱、再配布しません。' +
    NewLine + NewLine +
    'OBS: https://github.com/obsproject/obs-studio/releases' + NewLine +
    'FFmpeg: https://ffmpeg.org/download.html'
  );
end;

procedure DeleteManagedUserData(const DataDir: String);
begin
  DelTree(AddBackslash(DataDir) + 'config', True, True, True);
  DelTree(AddBackslash(DataDir) + 'logs', True, True, True);
  DelTree(AddBackslash(DataDir) + 'bin', True, True, True);
  DelTree(AddBackslash(DataDir) + 'obs-portable', True, True, True);
  DelTree(AddBackslash(DataDir) + 'downloads', True, True, True);
  DelTree(AddBackslash(DataDir) + 'assets', True, True, True);
  DelTree(AddBackslash(DataDir) + 'licenses', True, True, True);
  DeleteFile(AddBackslash(DataDir) + '.app-instance.lock');
  RemoveDir(DataDir);
end;

procedure DeleteManagedRecordings(const DataDir: String);
begin
  DelTree(AddBackslash(DataDir) + 'recordings', True, True, True);
  RemoveDir(DataDir);
end;

function ShowUninstallOptions: Boolean;
var
  OptionsForm: TSetupForm;
  TitleLabel: TNewStaticText;
  DescriptionLabel: TNewStaticText;
  ManagedDataCheckBox: TNewCheckBox;
  RecordingsCheckBox: TNewCheckBox;
  WarningLabel: TNewStaticText;
  OkButton: TNewButton;
  CancelButton: TNewButton;
begin
  OptionsForm := CreateCustomForm(ScaleX(520), ScaleY(250), False, False);
  try
    OptionsForm.Caption := '{#AppName} アンインストール';
    OptionsForm.Position := poScreenCenter;

    TitleLabel := TNewStaticText.Create(OptionsForm);
    TitleLabel.Parent := OptionsForm;
    TitleLabel.Left := ScaleX(20);
    TitleLabel.Top := ScaleY(18);
    TitleLabel.Caption := '追加で削除するデータを選択してください';
    TitleLabel.Font.Style := [fsBold];

    DescriptionLabel := TNewStaticText.Create(OptionsForm);
    DescriptionLabel.Parent := OptionsForm;
    DescriptionLabel.Left := ScaleX(20);
    DescriptionLabel.Top := ScaleY(48);
    DescriptionLabel.Width := ScaleX(480);
    DescriptionLabel.AutoSize := False;
    DescriptionLabel.WordWrap := True;
    DescriptionLabel.Caption :=
      'アプリ本体は常に削除されます。以下は初期状態では保持されます。';

    ManagedDataCheckBox := TNewCheckBox.Create(OptionsForm);
    ManagedDataCheckBox.Parent := OptionsForm;
    ManagedDataCheckBox.Left := ScaleX(20);
    ManagedDataCheckBox.Top := ScaleY(88);
    ManagedDataCheckBox.Width := ScaleX(480);
    ManagedDataCheckBox.Caption :=
      '設定、ログ、OBS、FFmpeg、手動配置した mpv DLL も削除する';
    ManagedDataCheckBox.Checked := False;

    RecordingsCheckBox := TNewCheckBox.Create(OptionsForm);
    RecordingsCheckBox.Parent := OptionsForm;
    RecordingsCheckBox.Left := ScaleX(20);
    RecordingsCheckBox.Top := ScaleY(122);
    RecordingsCheckBox.Width := ScaleX(480);
    RecordingsCheckBox.Caption :=
      '録画ファイルとセッションログも削除する';
    RecordingsCheckBox.Checked := False;

    WarningLabel := TNewStaticText.Create(OptionsForm);
    WarningLabel.Parent := OptionsForm;
    WarningLabel.Left := ScaleX(40);
    WarningLabel.Top := ScaleY(151);
    WarningLabel.Width := ScaleX(450);
    WarningLabel.AutoSize := False;
    WarningLabel.WordWrap := True;
    WarningLabel.Font.Color := clRed;
    WarningLabel.Caption :=
      '対象は %LOCALAPPDATA%\LoLReplayTool\recordings のみです。' + #13#10 +
      '設定で指定した外部保存先は削除しません。';

    OkButton := TNewButton.Create(OptionsForm);
    OkButton.Parent := OptionsForm;
    OkButton.Left := OptionsForm.ClientWidth - ScaleX(190);
    OkButton.Top := OptionsForm.ClientHeight - ScaleY(42);
    OkButton.Width := ScaleX(80);
    OkButton.Caption := '続行';
    OkButton.Default := True;
    OkButton.ModalResult := mrOk;

    CancelButton := TNewButton.Create(OptionsForm);
    CancelButton.Parent := OptionsForm;
    CancelButton.Left := OptionsForm.ClientWidth - ScaleX(100);
    CancelButton.Top := OptionsForm.ClientHeight - ScaleY(42);
    CancelButton.Width := ScaleX(80);
    CancelButton.Caption := 'キャンセル';
    CancelButton.Cancel := True;
    CancelButton.ModalResult := mrCancel;

    Result := OptionsForm.ShowModal = mrOk;
    if Result then
    begin
      DeleteManagedDataOnUninstall := ManagedDataCheckBox.Checked;
      DeleteRecordingsOnUninstall := RecordingsCheckBox.Checked;
    end;
  finally
    OptionsForm.Free;
  end;
end;

function InitializeUninstall: Boolean;
begin
  DeleteManagedDataOnUninstall := False;
  DeleteRecordingsOnUninstall := False;
  if UninstallSilent then
    Result := True
  else
    Result := ShowUninstallOptions;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;

  DataDir := ExpandConstant('{localappdata}\LoLReplayTool');
  if not DirExists(DataDir) then
    Exit;

  if DeleteRecordingsOnUninstall then
    DeleteManagedRecordings(DataDir);
  if DeleteManagedDataOnUninstall then
    DeleteManagedUserData(DataDir);
end;
