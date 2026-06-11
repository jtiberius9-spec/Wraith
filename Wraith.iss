; Wraith installer (Inno Setup). Per-user install — no admin prompt, and the
; install folder stays writable so keymaps/recordings save next to the .exe.
#define MyAppName "Wraith"
#define MyAppVersion "0.3.0"
#define MyAppPublisher "James"
#define MyAppExeName "Wraith.exe"

[Setup]
AppId={{6F2A9C44-3D1E-4B7A-9E2C-WRAITH000001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Wraith
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=C:\wraith\installer
OutputBaseFilename=Wraith-Setup-{#MyAppVersion}
SetupIconFile=C:\wraith\wraith.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "C:\wraith\dist\Wraith\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Wraith"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Wraith"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Wraith now"; Flags: nowait postinstall skipifsilent
