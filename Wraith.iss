; Wraith installer (Inno Setup). Per-user install — no admin prompt, and the
; install folder stays writable so keymaps/recordings save next to the .exe.
; Paths are RELATIVE to this script so it builds anywhere (dev box or CI).
; CI overrides the version per tag: ISCC /DMyAppVersion=0.4.0 Wraith.iss
#define MyAppName "Wraith"
#ifndef MyAppVersion
#define MyAppVersion "0.4.0"
#endif
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
OutputDir=installer
OutputBaseFilename=Wraith-Setup-{#MyAppVersion}
SetupIconFile=wraith.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "dist\Wraith\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Wraith"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Wraith"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Wraith now"; Flags: nowait postinstall skipifsilent
