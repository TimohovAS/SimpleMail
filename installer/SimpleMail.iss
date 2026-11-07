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
  #define StartupLink "SimpleMail.Installer.Validation.lnk"
  #define UninstallKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\SimpleMail.Installer.Validation_is1"
#else
  #define StartupValue "SimpleMail"
  #define StartupLink "SimpleMail.lnk"
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

[UninstallDelete]
Type: files; Name: "{app}\installer-language.txt"

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
    { A remembered per-user profile wins over an older installer's saved path. }
    Result := '--profile-fallback "' + AddBackslash(Data) + '."';
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

procedure UpdateStartupProfile;
var
  Command, Executable, Parameters, Updated: String;
  PrefixLength: Integer;
  AlreadyMinimized: Boolean;
begin
  { Preserve enabled startup, including a portable or legacy installation.
    Setup never resolves another user's active profile. The application does
    that on launch; a legacy --data argument becomes only its fallback. }
  if not RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#StartupValue}', Command) then Exit;
  Command := Trim(Command);
  Executable := StartupExecutable(Command);
  if CompareText(ExtractFileName(Executable), 'SimpleMail.exe') <> 0 then Exit;
  PrefixLength := Length(Executable);
  if Command[1] = '"' then PrefixLength := PrefixLength + 2;
  Parameters := Trim(Copy(Command, PrefixLength + 1, Length(Command)));
  if Pos('--data ', Parameters) = 1 then begin
    Delete(Parameters, 1, Length('--data '));
    Parameters := '--profile-fallback ' + Parameters;
  end else if Pos('--data=', Parameters) = 1 then begin
    Delete(Parameters, 1, Length('--data='));
    Parameters := '--profile-fallback=' + Parameters;
  end else if (Parameters = '') or (Parameters = '--minimized') then
    Parameters := LaunchParameters('')
  else if (Pos('--profile-fallback ', Parameters) <> 1) and
          (Pos('--profile-fallback=', Parameters) <> 1) then
    Exit;
  { Windows startup is quiet; normal shortcuts and Setup's Run action still show
    the window. Preserve the old profile hint, including its quoting. }
  AlreadyMinimized := Parameters = '--minimized';
  if Length(Parameters) >= Length(' --minimized') then
    AlreadyMinimized := AlreadyMinimized or
      (Copy(Parameters, Length(Parameters) - Length(' --minimized') + 1,
            Length(' --minimized')) = ' --minimized');
  if not AlreadyMinimized then
    Parameters := Trim(Parameters + ' --minimized');
  { С 0.19.5 автозапуск — ярлык в папке «Автозагрузка»: запись программы в Run
    антивирусы (Kaspersky PDM) принимали за закрепление трояна. }
  try
    CreateShellLink(ExpandConstant('{userstartup}\{#StartupLink}'), 'SimpleMail',
                    ExpandConstant('{app}\SimpleMail.exe'), Parameters, ExpandConstant('{app}'),
                    '', 0, SW_SHOWNORMAL);
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#StartupValue}');
  except
    Log('Unable to create the startup shortcut: ' + GetExceptionMessage);
    Updated := '"' + ExpandConstant('{app}\SimpleMail.exe') + '"';
    if Parameters <> '' then Updated := Updated + ' ' + Parameters;
    if not RegWriteStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#StartupValue}', Updated) then
      Log('Unable to update the SimpleMail startup profile.');
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  InterfaceLanguage: String;
begin
  if CurStep = ssPostInstall then begin
    InterfaceLanguage := 'ru';
    if ActiveLanguage = 'serbianlatin' then
      InterfaceLanguage := 'sr_Latn';
    { A machine-wide default; each user's saved choice takes precedence. }
    if not SaveStringToFile(ExpandConstant('{app}\installer-language.txt'), InterfaceLanguage, False) then
      RaiseException('Unable to save the application interface language.');
  end;
  { Старая копия удаляется только после успешной установки новой. }
  if CurStep = ssPostInstall then begin
    RemoveLegacyInstall;
    UpdateStartupProfile;
  end;
end;

var
  UninstallMailData: String;

function InitializeUninstall: Boolean;
begin
  Result := True;
  { Ключ удаления исчезает до usPostUninstall: папку данных запоминаем заранее. }
  if not RegQueryStringValue(HKA, '{#UninstallKey}', 'Inno Setup CodeFile: MailData', UninstallMailData) then
    UninstallMailData := '';
end;

procedure DeleteMatching(Dir, Pattern: String);
var
  Found: TFindRec;
begin
  if FindFirst(AddBackslash(Dir) + Pattern, Found) then
    try
      repeat
        if Found.Attributes and FILE_ATTRIBUTE_DIRECTORY = 0 then
          DeleteFile(AddBackslash(Dir) + Found.Name);
      until not FindNext(Found);
    finally
      FindClose(Found);
    end;
end;

{ Только файлы SimpleMail: папку данных могли указать любую (/MAILDATA), даже корень диска. }
procedure DeleteDataDir(Dir: String);
begin
  Dir := RemoveBackslash(Dir);
  if not DirExists(Dir) then Exit;
  DelTree(Dir + '\logs', True, True, True);
  DelTree(Dir + '\restored', True, True, True);
  DeleteMatching(Dir, 'mail.db');
  DeleteMatching(Dir, 'mail.db-*');
  DeleteMatching(Dir, 'mail.db.old-*');
  DeleteMatching(Dir, 'before-restore-*.smbak');
  DeleteMatching(Dir, '.simplemail.lock');
  RemoveDir(Dir);
end;

procedure ForgetRemovedProfile(Data: String);
var
  Locator, EscapedPath, Expected, Archive: String;
  Contents: AnsiString;
begin
  { Only a confirmed data deletion can forget this user's active profile.
    Match the exact format written by profiles.py; leave unknown files alone. }
  if FileExists(AddBackslash(Data) + 'mail.db') then Exit;
  Locator := ExpandConstant('{%USERPROFILE}\.simplemail\profile.json');
  if not LoadStringFromFile(Locator, Contents) then Exit;
  EscapedPath := RemoveBackslash(ExpandFileName(Data));
  StringChangeEx(EscapedPath, '\', '\\', True);
  StringChangeEx(EscapedPath, '"', '\"', True);
  Expected := '{"version": 1, "data_dir": "' + EscapedPath + '"}';
  if CompareText(Trim(Utf8Decode(Contents)), Expected) <> 0 then Exit;
  Archive := Locator + '.before-removal';
  while FileExists(Archive) do Archive := Archive + '.old';
  if not RenameFile(Locator, Archive) then
    Log('Unable to archive the removed SimpleMail profile location.');
end;

procedure AskRemoveUserData;
var
  Data, Mail: String;
begin
  Data := UninstallMailData;
  if Data = '' then
    Data := ExpandConstant('{userappdata}\SimpleMail');
  Data := RemoveBackslash(Data);
  Mail := ExpandConstant('{userdocs}\SimpleMail');
  if not DirExists(Data) and not DirExists(Mail) then Exit;
  { По умолчанию «Нет»; тихое удаление (/SUPPRESSMSGBOXES, обновление старой версии) почту сохраняет. }
  if SuppressibleMsgBox(FmtMessage(CustomMessage('RemoveUserData'), [Data, Mail]),
                        mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) <> IDYES then Exit;
  DeleteDataDir(Data);
  if CompareText(Data, ExpandConstant('{userappdata}\SimpleMail')) = 0 then
    DelTree(Data, True, True, True);
  ForgetRemovedProfile(Data);
  DelTree(Mail, True, True, True);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Command: String;
begin
  if CurUninstallStep = usUninstall then begin
    DeleteFile(ExpandConstant('{userstartup}\{#StartupLink}'));
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#StartupValue}', Command) then
      if CompareText(StartupExecutable(Command), ExpandConstant('{app}\SimpleMail.exe')) = 0 then
        RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#StartupValue}');
  end;
  if CurUninstallStep = usPostUninstall then begin
    { Кеш скачанных установщиков (~150 МБ) не нужен без программы. }
    DelTree(ExpandConstant('{localappdata}\SimpleMail\Updates'), True, True, True);
    RemoveDir(ExpandConstant('{localappdata}\SimpleMail'));
    AskRemoveUserData;
  end;
end;

[CustomMessages]
english.NewerVersionInstalled=A newer SimpleMail version is already installed in this folder. Downgrading is not supported.
russian.NewerVersionInstalled=В этой папке уже установлена более новая версия SimpleMail. Установка старой версии не поддерживается.
serbianlatin.NewerVersionInstalled=U ovoj fascikli je već instalirana novija verzija SimpleMail-a. Vraćanje na stariju verziju nije podržano.
english.RemoveUserData=Also delete SimpleMail mail and settings?%n%n%1 — mail database, settings and logs%n%2 — messages and attachments (default folder)%n%nMail folders chosen manually in mailbox settings and passwords saved in Windows Credential Manager are not deleted.%n%nNo — everything is kept for a future installation.
russian.RemoveUserData=Удалить также почту и настройки SimpleMail?%n%n%1 — база писем, настройки и журналы%n%2 — письма и вложения (папка по умолчанию)%n%nПапки писем, выбранные вручную в настройках ящиков, и пароли в диспетчере учётных данных Windows не удаляются.%n%nНет — всё сохранится для будущей установки.
serbianlatin.RemoveUserData=Obrisati i poštu i podešavanja SimpleMail-a?%n%n%1 — baza poruka, podešavanja i dnevnici%n%2 — poruke i prilozi (podrazumevana fascikla)%n%nFascikle za poštu izabrane ručno u podešavanjima naloga i lozinke sačuvane u Windows upravljaču akreditivima se ne brišu.%n%nNe — sve ostaje sačuvano za buduću instalaciju.
