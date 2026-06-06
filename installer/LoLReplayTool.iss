#ifndef AppVersion
  #define AppVersion "0.1.0"
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
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter={#AppExeName}
RestartApplications=no
SetupMutex=LoLReplayToolInstallerMutex
ChangesAssociations=no
ChangesEnvironment=no
Uninstallable=yes

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "デスクトップにショートカットを作成"; GroupDescription: "追加タスク:"; Flags: unchecked

[Files]
Source: "..\dist\LoLReplayTool\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{#AppName} を起動"; Flags: nowait postinstall skipifsilent

[Code]
procedure DeleteManagedUserData(const DataDir: String);
begin
  DelTree(AddBackslash(DataDir) + 'config', True, True, True);
  DelTree(AddBackslash(DataDir) + 'logs', True, True, True);
  DelTree(AddBackslash(DataDir) + 'bin', True, True, True);
  DelTree(AddBackslash(DataDir) + 'obs-portable', True, True, True);
  DelTree(AddBackslash(DataDir) + 'downloads', True, True, True);
  DelTree(AddBackslash(DataDir) + 'assets', True, True, True);
  DeleteFile(AddBackslash(DataDir) + '.app-instance.lock');
  RemoveDir(DataDir);
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

  if MsgBox(
    '設定、ログ、ダウンロード済みの OBS / FFmpeg も削除しますか？' + #13#10 + #13#10 +
    '録画ファイルはこの選択に関係なく削除されません。',
    mbConfirmation,
    MB_YESNO
  ) = IDYES then
    DeleteManagedUserData(DataDir);
end;
