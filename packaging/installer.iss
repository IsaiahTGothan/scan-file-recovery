; Inno Setup script for Lifeboat Data Recovery.
; Build after PyInstaller:  ISCC /DAppVersion=1.0.0 packaging\installer.iss

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "Lifeboat Data Recovery"
#define AppPublisher "Zays"
#define AppURL "https://zays.us"
#define AppExe "Lifeboat.exe"

[Setup]
AppId={{6E5B1A2C-4F0D-4C55-9A0E-8C7D2B1F4E10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\Lifeboat
DefaultGroupName=Lifeboat
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Lifeboat-Setup-{#AppVersion}
SetupIconFile=..\lifeboat\assets\lifeboat.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
WizardImageFile=wizard-large.bmp
WizardSmallImageFile=wizard-small.bmp
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
MinVersion=10.0
CloseApplications=yes
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} installer

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Lifeboat\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Lifeboat command line"; Filename: "{cmd}"; Parameters: "/k ""cd /d ""{app}"" && lifeboat-cli --help"""; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent shellexec
