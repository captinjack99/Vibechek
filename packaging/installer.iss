; Inno Setup script for the Vibechek Windows installer.
;
; Prereqs:
;   1. Inno Setup 6+ installed (https://jrsoftware.org/isinfo.php)
;   2. PyInstaller build already run:  packaging\build-windows.bat
;
; Build:
;   ISCC.exe packaging\installer.iss
;
; Output:
;   dist\vibechek-setup.exe

#define MyAppName "Vibechek"
#define MyAppVersion "0.2.0-dev"
#define MyAppPublisher "Vibechek contributors"
#define MyAppURL "https://github.com/papapew/Vibechek"
#define MyAppExeName "vibechek.exe"

[Setup]
AppId={{B9D7E2A4-3F1C-4D6F-8C2E-9E4A1B5D7F8A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=vibechek-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Vibechek is a CLI; install per-user (no admin prompt) by default.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ChangesEnvironment=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "Add Vibechek to your PATH (recommended)"; GroupDescription: "Shell integration:"; Flags: checkedonce

[Files]
; The PyInstaller --one-folder output. The build script must place these here.
Source: "..\dist\vibechek\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName} (CLI)"; Filename: "{cmd}"; Parameters: "/k ""{app}\{#MyAppExeName} --help"""; WorkingDir: "{userprofile}"

[Run]
Filename: "{cmd}"; Parameters: "/k ""{app}\{#MyAppExeName} --help"""; Description: "Open a Vibechek shell"; Flags: nowait postinstall skipifsilent

[Registry]
; Add install dir to user PATH if the optional task is selected.
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
    ValueData: "{olddata};{app}"; \
    Check: NeedsAddPath('{app}'); Tasks: addtopath

[Code]
function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  // Only append if our directory isn't already on the PATH.
  Result := Pos(';' + Param + ';', ';' + OrigPath + ';') = 0;
end;
