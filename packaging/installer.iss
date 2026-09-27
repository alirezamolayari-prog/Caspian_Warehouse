; Inno Setup script for Caspian Warehouse.
; Built by packaging/build.ps1 (or CI) after PyInstaller has produced dist\CaspianWarehouse.

#define AppName "Caspian Warehouse"
#define AppVersion GetEnv("CASPIAN_VERSION")
#if AppVersion == ""
  #define AppVersion "0.1.0"
#endif
#define DistDir GetEnv("CASPIAN_DIST")
#if DistDir == ""
  #define DistDir "..\dist\CaspianWarehouse"
#endif

[Setup]
AppId={{6C1C8C2E-8B7E-4C63-9E3A-2E6A4F1B7D11}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Caspian Warehouse
DefaultDirName={autopf}\Caspian Warehouse
DefaultGroupName=Caspian Warehouse
UninstallDisplayIcon={app}\CaspianWarehouse.exe
OutputDir=..\dist\installer
OutputBaseFilename=CaspianWarehouse-Setup-{#AppVersion}
SetupIconFile=caspian.ico
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
PrivilegesRequiredOverridesAllowed=dialog
; User data (settings, logs, backups) lives in the user's profile and survives upgrades.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\docs\USER_GUIDE.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\LAN_SETUP.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\Caspian Warehouse"; Filename: "{app}\CaspianWarehouse.exe"
Name: "{group}\User guide"; Filename: "{app}\docs\USER_GUIDE.md"
Name: "{group}\{cm:UninstallProgram,Caspian Warehouse}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Caspian Warehouse"; Filename: "{app}\CaspianWarehouse.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CaspianWarehouse.exe"; Description: "{cm:LaunchProgram,Caspian Warehouse}"; Flags: nowait postinstall skipifsilent
