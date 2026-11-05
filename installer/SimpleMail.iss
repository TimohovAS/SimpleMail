; ISCC /DAppVersion=<version> installer\SimpleMail.iss
#ifndef AppVersion
  #error AppVersion must be supplied by tools/build_installer.ps1
#endif
#define ProjectRoot SourcePath + ".."
#ifndef BundleDir
  #define BundleDir ProjectRoot + "\dist\SimpleMail"
#endif
#ifdef ValidationBuild
  #define StartupValue "SimpleMail.Installer.Validation"
  #define UninstallKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\SimpleMail.Installer.Validation_is1"
#else
  #define StartupValue "SimpleMail"
  #define UninstallKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\{CF396668-ED30-4522-86ED-D96CA667DE83}_is1"
#endif

[Setup]
#ifdef ValidationBuild
; Separate identity and shortcut: smoke tests cannot replace a user's installation.
AppId=SimpleMail.Installer.Validation
#else
AppId={{CF396668-ED30-4522-86ED-D96CA667DE83}
#endif
AppName=SimpleMail
AppVersion={#AppVersion}
AppPublisher=TimohovAS
AppPublisherURL=https://github.com/TimohovAS/SimpleMail
AppUpdatesURL=https://github.com/TimohovAS/SimpleMail/releases
DefaultDirName={autopf}\SimpleMail
DefaultGroupName=SimpleMail
; Program Files для всех пользователей; /CURRENTUSER оставлен для проверочных установок.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
UsePreviousPrivileges=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\SimpleMail.exe
SetupIconFile={#ProjectRoot}\mailclient\icons\simplemail.ico
LicenseFile={#ProjectRoot}\LICENSE
OutputDir={#ProjectRoot}\dist\installer
OutputBaseFilename=SimpleMail-Setup-{#AppVersion}-x64
#ifdef ValidationBuild
; Same archive format; faster compression keeps installer tests practical in CI.
Compression=lzma2/fast
#else
Compression=lzma2
#endif
SolidCompression=yes
WizardStyle=modern
#ifdef EnableSigning
SignTool=simplemail_sign
SignedUninstaller=yes
#endif
AppMutex=Local\TimohovAS.SimpleMail.Running
SetupMutex=Local\TimohovAS.SimpleMail.Setup
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd,*.pak
RestartApplications=no
Uninstallable=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "serbianlatin"; MessagesFile: "languages\SerbianLatin.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ProjectRoot}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
#ifdef ValidationBuild
Name: "{autoprograms}\SimpleMail installer validation"; Filename: "{app}\SimpleMail.exe"; Parameters: "{code:LaunchParameters}"
#else
Name: "{autoprograms}\SimpleMail"; Filename: "{app}\SimpleMail.exe"; Parameters: "{code:LaunchParameters}"; AppUserModelID: "TimohovAS.SimpleMail"
Name: "{autodesktop}\SimpleMail"; Filename: "{app}\SimpleMail.exe"; Parameters: "{code:LaunchParameters}"; Tasks: desktopicon; AppUserModelID: "TimohovAS.SimpleMail"
#endif

[Run]
Filename: "{app}\SimpleMail.exe"; Parameters: "{code:LaunchParameters}"; Description: "{cm:LaunchProgram,SimpleMail}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
var
  LegacyDir, LegacyUninstaller, LegacyMailData: String;

{ Установка 0.10–0.11 для текущего пользователя (HKCU, %LOCALAPPDATA%\Programs). }
function HasLegacyInstall: Boolean;
begin
  Result := IsAdminInstallMode and (LegacyDir <> '');
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  if IsAdminInstallMode and RegQueryStringValue(HKCU, '{#UninstallKey}', 'InstallLocation', LegacyDir) then begin
    LegacyDir := RemoveBackslash(LegacyDir);
    RegQueryStringValue(HKCU, '{#UninstallKey}', 'UninstallString', LegacyUninstaller);
    RegQueryStringValue(HKCU, '{#UninstallKey}', 'Inno Setup CodeFile: MailData', LegacyMailData);
  end;
end;

function MailData: String;
begin
  Result := ExpandConstant('{param:MAILDATA|}');
  if Result = '' then
    Result := GetPreviousData('MailData', '');
  if Result = '' then
    Result := LegacyMailData;
end;

function LaunchParameters(Param: String): String;
var
  Data: String;
begin
  Data := MailData;
  Result := '';
  if Data <> '' then
    { A trailing backslash must not escape the closing command-line quote. }
    Result := '--data "' + AddBackslash(Data) + '."';
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'MailData', MailData);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExistingVersion, NewVersion: Int64;
begin
  Result := '';
  if GetPackedVersion(ExpandConstant('{app}\SimpleMail.exe'), ExistingVersion) then
    if StrToVersion('{#AppVersion}', NewVersion) then
      if ComparePackedVersion(ExistingVersion, NewVersion) > 0 then
        Result := CustomMessage('NewerVersionInstalled');
end;

function StartupExecutable(Command: String): String;
var
  EndPosition: Integer;
begin
  Command := Trim(Command);
  Result := '';
  if Command = '' then Exit;
  if Command[1] = '"' then begin
    Delete(Command, 1, 1);
    EndPosition := Pos('"', Command);
    if EndPosition = 0 then Exit;
  end else begin
    EndPosition := Pos(' ', Command);
    if EndPosition = 0 then EndPosition := Length(Command) + 1;
  end;
  Result := Copy(Command, 1, EndPosition - 1);
end;

procedure InitializeWizard;
begin
  { Обновление из старой версии передаёт /DIR старой папки: ставим в Program Files. }
  if HasLegacyInstall and (CompareText(RemoveBackslash(WizardForm.DirEdit.Text), LegacyDir) = 0) then
    WizardForm.DirEdit.Text := ExpandConstant('{autopf}\SimpleMail');
end;

procedure RemoveLegacyInstall;
var
  Command, OldExe, Startup: String;
  ResultCode: Integer;
begin
  if not HasLegacyInstall or (CompareText(LegacyDir, RemoveBackslash(ExpandConstant('{app}'))) = 0) then Exit;
  OldExe := AddBackslash(LegacyDir) + 'SimpleMail.exe';
  { Старый деинсталлятор удаляет автозапуск на старую EXE: переносим его на новую. }
  Startup := '';
  if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#StartupValue}', Command) then
    if CompareText(StartupExecutable(Command), OldExe) = 0 then
      Startup := Command;
  if (LegacyUninstaller <> '') and FileExists(RemoveQuotes(LegacyUninstaller)) then
    if not Exec(RemoveQuotes(LegacyUninstaller), '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '',
                SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      Log(Format('Legacy uninstall failed: %d', [ResultCode]));
  if Startup <> '' then begin
    StringChangeEx(Startup, OldExe, ExpandConstant('{app}\SimpleMail.exe'), True);
    RegWriteStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#StartupValue}', Startup);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { Старая копия удаляется только после успешной установки новой. }
  if CurStep = ssPostInstall then
    RemoveLegacyInstall;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Command: String;
begin
  if CurUninstallStep = usUninstall then
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#StartupValue}', Command) then
      if CompareText(StartupExecutable(Command), ExpandConstant('{app}\SimpleMail.exe')) = 0 then
        RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#StartupValue}');
end;

[CustomMessages]
english.NewerVersionInstalled=A newer SimpleMail version is already installed in this folder. Downgrading is not supported.
russian.NewerVersionInstalled=В этой папке уже установлена более новая версия SimpleMail. Установка старой версии не поддерживается.
serbianlatin.NewerVersionInstalled=U ovoj fascikli je već instalirana novija verzija SimpleMail-a. Vraćanje na stariju verziju nije podržano.
