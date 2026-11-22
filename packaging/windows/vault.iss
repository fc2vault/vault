; Vault for Windows — Inno Setup script. build_windows.ps1 fills the staging folder and runs ISCC:
;   ISCC /DVersion=1.9.0 /DStage=<staging dir> /DOutDir=<out dir> vault.iss
; Installs for the current user only (no administrator rights), into %LOCALAPPDATA%\Programs\Vault.

#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId={{7C1E4E59-2B7A-4C5E-9D0B-5A1F3E8C6D21}
AppName=Vault
AppVersion={#Version}
AppVerName=Vault {#Version}
AppPublisher=fc2vault
AppPublisherURL=https://github.com/fc2vault/vault
AppSupportURL=https://github.com/fc2vault/vault/issues
DefaultDirName={localappdata}\Programs\Vault
DefaultGroupName=Vault
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=Vault-windows-setup
SetupIconFile={#Stage}\Vault.ico
UninstallDisplayIcon={app}\Vault.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start Vault when I log on (runs in the background)"; GroupDescription: "Start:"; Flags: unchecked

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Vault"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\Vault.ico"; Comment: "Open Vault in your browser"
Name: "{autoprograms}\Vault - Stop"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.pyw"" --stop"; WorkingDir: "{app}"; IconFilename: "{app}\Vault.ico"; Comment: "Stop the Vault server"
Name: "{autoprograms}\Vault - Choose movie folder"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.pyw"" --library"; WorkingDir: "{app}"; IconFilename: "{app}\Vault.ico"
Name: "{autodesktop}\Vault"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\Vault.ico"; Tasks: desktopicon
Name: "{userstartup}\Vault"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.pyw"" --background"; WorkingDir: "{app}"; IconFilename: "{app}\Vault.ico"; Tasks: startup

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.pyw"""; WorkingDir: "{app}"; Description: "Open Vault now"; Flags: postinstall nowait skipifsilent

; an update started from inside Vault (Settings → Updates) runs this setup silently with
; /RELAUNCH=1: start Vault again when it is done (the open page reloads itself)
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.pyw"" --background"; WorkingDir: "{app}"; Flags: nowait; Check: RelaunchRequested

[UninstallRun]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.pyw"" --stop"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "StopVault"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python\Lib\__pycache__"

[Code]
function RelaunchRequested: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

// Updating: stop a running Vault first so its files can be replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer;
begin
  if FileExists(ExpandConstant('{app}\launcher.pyw')) then
    Exec(ExpandConstant('{app}\python\pythonw.exe'), '"' + ExpandConstant('{app}\launcher.pyw') + '" --stop',
         ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
