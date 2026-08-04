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
MinVersion=10.0
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
var
  MpvInfoPage: TOutputMsgWizardPage;
  DeleteManagedDataOnUninstall: Boolean;
  DeleteRecordingsOnUninstall: Boolean;

function IsContentAuditMode: Boolean;
begin
  Result := ExpandConstant('{param:contentaudit|}') = '1';
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
