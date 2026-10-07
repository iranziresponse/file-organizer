; Inno Setup script for Orch. Builds a normal Next-Next-Install wizard
; around the PyInstaller onedir output (dist\Orch\Orch.exe + _internal\),
; instead of shipping a bare exe or a zip a non-technical student has to
; unzip themselves. See .github/workflows/release.yml for how this gets
; compiled (ISCC, with /DMyAppVersion passed in from the release tag) and
; organizer/core/updater.py for how the in-app self-updater re-runs this
; same installer silently to update an existing install in place.
;
; PrivilegesRequired=lowest + a per-user install directory: this targets
; personal student laptops, not IT-managed machines, so it must never
; need admin/UAC just to install. AppMutex matches the exact named mutex
; gui/app.py's _enforce_single_instance() already creates, so Setup can
; detect a running copy and ask the user to close it before continuing,
; the same way any other well-behaved Windows installer does.

#ifndef MyAppVersion
#define MyAppVersion "0.0.0"
#endif

#define MyAppName "Orch"
#define MyAppExeName "Orch.exe"
#define MyAppPublisher "Iranzi"

[Setup]
AppId={{F6C2E6C1-6B6E-4B0C-9B8B-2C6E6F1B7C41}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppMutex=Iranzi.Orch.Desktop.SingleInstance
DefaultDirName={localappdata}\Programs\Orch
DefaultGroupName=Orch
DisableProgramGroupPage=yes
DisableDirPage=no
DisableWelcomePage=no
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=Orch-Setup
OutputDir=..\dist_installer
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\organizer\static\organizer\img\orch-mark.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\Orch\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist_installer\MicrosoftEdgeWebView2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\Orch"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall Orch"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Orch"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebView2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Checking the Microsoft Edge WebView2 runtime..."; Flags: waituntilterminated runhidden
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Orch now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Removes the whole install directory, including _internal\, on
; uninstall -- this is read-only bundled code, never the user's data
; (that lives in %LOCALAPPDATA%\Orch, see runtime.py, and is
; deliberately left alone so uninstalling never touches it).
Type: filesandordirs; Name: "{app}"
