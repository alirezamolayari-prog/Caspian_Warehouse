; Inno Setup 6 script for Caspian Warehouse.
; Normally compiled by packaging\build.ps1, which passes:
;   /DAppVersion=1.2.3  /DAppVersionNumeric=1.2.3.0  /DDistDir=<PyInstaller output>
;   /DOutputDir=<folder>  [/DMariaDbMsi=<path to mariadb-*.msi>]
; Silent install (used by tests):  Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
;   [/DIR="..."] [/TASKS="dbserver"] [/DBPASSWORD=...]

#define AppName "Caspian Warehouse"
#define Publisher "Caspian Furniture Market"
#define RepoUrl "https://github.com/alirezamolayari-prog/Caspian_Warehouse"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppVersionNumeric
  #define AppVersionNumeric "0.0.0.0"
#endif
#ifndef DistDir
  #define DistDir "..\dist\CaspianWarehouse"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif

[Setup]
; Never change AppId: it is how upgrades find the existing installation.
AppId={{6C1C8C2E-8B7E-4C63-9E3A-2E6A4F1B7D11}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#Publisher}
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}/issues
AppUpdatesURL={#RepoUrl}/releases
AppCopyright=Copyright (c) 2026 {#Publisher}
VersionInfoVersion={#AppVersionNumeric}
VersionInfoCompany={#Publisher}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
DefaultDirName={autopf}\Caspian Warehouse
DefaultGroupName=Caspian Warehouse
DisableProgramGroupPage=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\CaspianWarehouse.exe
LicenseFile=..\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=CaspianWarehouse-Setup-{#AppVersion}
SetupIconFile=caspian.ico
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
; User data (settings, logs, backups, credentials, the database) lives outside {app}
; and is never touched by upgrades or uninstall.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
#ifdef MariaDbMsi
Name: "dbserver"; Description: "Install the database server (MariaDB) on this PC — choose this on the main or only computer. Leave it unticked on other PCs of a network."; GroupDescription: "Database:"; Check: NoDatabaseServerYet
#endif

[InstallDelete]
; Remove the previous version's libraries so old and new files never mix after an upgrade.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\docs\USER_GUIDE.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\LAN_SETUP.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion
#ifdef MariaDbMsi
Source: "{#MariaDbMsi}"; DestName: "mariadb.msi"; Flags: dontcopy
#endif

[Icons]
Name: "{group}\Caspian Warehouse"; Filename: "{app}\CaspianWarehouse.exe"
Name: "{group}\User guide"; Filename: "{app}\docs\USER_GUIDE.md"
Name: "{group}\{cm:UninstallProgram,Caspian Warehouse}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Caspian Warehouse"; Filename: "{app}\CaspianWarehouse.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CaspianWarehouse.exe"; Description: "{cm:LaunchProgram,Caspian Warehouse}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
var
  DbPage: TInputQueryWizardPage;

function ServiceExists(const Name: String): Boolean;
begin
  Result := RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\' + Name);
end;

function NoDatabaseServerYet: Boolean;
begin
  { An existing server (or an upgrade on the server PC) must not get a second one. }
  Result := not (ServiceExists('MariaDB') or ServiceExists('MySQL') or
                 ServiceExists('MySQL80') or ServiceExists('MySQL57'));
end;

function DbTaskSelected: Boolean;
begin
#ifdef MariaDbMsi
  Result := WizardIsTaskSelected('dbserver');
#else
  Result := False;
#endif
end;

procedure InitializeWizard;
begin
  DbPage := CreateInputQueryPage(wpSelectTasks,
    'Database password',
    'Choose a password for the database administrator (MariaDB "root")',
    'Write this password down and keep it somewhere safe. It is needed for year-end closing, ' +
    'for read-only access by AI tools and for restoring on a new computer. ' +
    'At least 8 characters; do not use the " character.');
  DbPage.Add('Password:', True);
  DbPage.Add('Repeat password:', True);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = DbPage.ID) and not DbTaskSelected;
end;

function PasswordProblem(const Pw: String): String;
begin
  Result := '';
  if Length(Pw) < 8 then
    Result := 'The password must have at least 8 characters.'
  else if Pos('"', Pw) > 0 then
    Result := 'The password must not contain the " character.';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Problem: String;
begin
  Result := True;
  { Silent installs never show pages (and a plain MsgBox would wait forever there);
    PrepareToInstall validates /DBPASSWORD instead. }
  if WizardSilent then
    Exit;
  if (CurPageID = DbPage.ID) and DbTaskSelected then
  begin
    Problem := PasswordProblem(DbPage.Values[0]);
    if (Problem = '') and (DbPage.Values[0] <> DbPage.Values[1]) then
      Problem := 'The two passwords are not the same.';
    if Problem <> '' then
    begin
      MsgBox(Problem, mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function DbPassword: String;
begin
  Result := ExpandConstant('{param:DBPASSWORD|}');
  if Result = '' then
    Result := DbPage.Values[0];
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if DbTaskSelected then
    Result := PasswordProblem(DbPassword);  { e.g. silent install without /DBPASSWORD }
end;

procedure InstallDatabase;
var
  Pw, Msi, PwFile, Params: String;
  Code: Integer;
begin
  Pw := DbPassword;
  WizardForm.StatusLabel.Caption := 'Installing the database server (this can take a minute)...';
  ExtractTemporaryFile('mariadb.msi');
  Msi := ExpandConstant('{tmp}\mariadb.msi');
  Params := '/i "' + Msi + '" /qn SERVICENAME=MariaDB PORT=3306 UTF8=1 PASSWORD="' + Pw +
            '" /l*v "' + ExpandConstant('{tmp}\mariadb-install.log') + '"';
  if not Exec('msiexec.exe', Params, '', SW_HIDE, ewWaitUntilTerminated, Code) or
     ((Code <> 0) and (Code <> 3010)) then
  begin
    SuppressibleMsgBox('The database server could not be installed (error ' + IntToStr(Code) +
      '). Caspian Warehouse itself is installed; see docs\LAN_SETUP.md for installing ' +
      'MariaDB manually.', mbError, MB_OK, IDOK);
    Exit;
  end;

  { Create the app's own database account and save the connection for the signed-in user. }
  WizardForm.StatusLabel.Caption := 'Preparing the database...';
  PwFile := ExpandConstant('{tmp}\dbpw.txt');
  SaveStringsToUTF8File(PwFile, [Pw], False);
  if not ExecAsOriginalUser(ExpandConstant('{app}\CaspianWarehouse.exe'),
                            '--provision "' + PwFile + '"', '', SW_HIDE,
                            ewWaitUntilTerminated, Code) or (Code <> 0) then
    SuppressibleMsgBox('The database server is installed, but the automatic setup did not ' +
      'finish. When Caspian Warehouse starts, choose "This PC", tick "New installation: ' +
      'create a dedicated app user", and enter the user root with the password you chose.',
      mbInformation, MB_OK, IDOK);
  DeleteFile(PwFile);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and DbTaskSelected then
    InstallDatabase;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    SuppressibleMsgBox('Caspian Warehouse was removed. Your data was kept: the database ' +
      '(MariaDB can be removed separately in Apps & features), your settings, and your ' +
      'backups in Documents\Caspian Backups.', mbInformation, MB_OK, IDOK);
end;
