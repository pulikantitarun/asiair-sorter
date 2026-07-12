; ============================================================
; ASIAIR Session Sorter — Inno Setup Installer Script
; ============================================================

#define AppName      "ASIAIR Session Sorter"
#define AppVersion   "2.2"
#define AppPublisher "pulikantitarun"
#define AppExeName   "ASIAIR_Sorter.exe"

; Paths relative to this .iss file (works locally and in CI)
#define SourceExe    "..\dist\ASIAIR_Sorter.exe"
#define AppIcon      "..\asiair_sorter.ico"
#define OutputDir    "..\installer_output"

[Setup]
AppId={{8F3A2C1B-4D6E-4F7A-9B2C-1E5D8F3A2C1B}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}

; Install location
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes

; Output
OutputDir={#OutputDir}
OutputBaseFilename=ASIAIR_Sorter_Setup_v{#AppVersion}

; Icon for the installer wizard and uninstaller
SetupIconFile={#AppIcon}
UninstallDisplayIcon={app}\{#AppExeName}

; Compression
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

; Look & feel
WizardStyle=modern
WizardSizePercent=110

; Privileges — lets user choose admin or per-user install
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; Uninstall
UninstallDisplayName={#AppName}

; Misc
ShowLanguageDialog=no
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; \
  GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Main app executable
Source: "{#SourceExe}"; DestDir: "{app}"; DestName: "{#AppExeName}"; \
  Flags: ignoreversion

; Bundle the icon so the uninstaller entry looks right
Source: "{#AppIcon}"; DestDir: "{app}"; DestName: "app.ico"; \
  Flags: ignoreversion

[Icons]
; Start Menu
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExeName}"; \
  IconFilename: "{app}\app.ico"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

; Desktop shortcut (optional)
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
  IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; \
  Description: "Launch {#AppName} now"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{userdocs}\.asiair_sorter_config.json"

[Code]
procedure InitializeWizard();
begin
  WizardForm.WelcomeLabel2.Caption :=
    'This will install ' + ExpandConstant('{#AppName}') + ' ' +
    ExpandConstant('{#AppVersion}') + ' on your computer.' + #13#10 + #13#10 +
    'ASIAIR Session Sorter organises your Asiair SD card data into a ' +
    'clean folder structure — sorting FITS frames by date, target and ' +
    'filter, removing preview JPGs from the SD card, and placing PHD2 ' +
    'guide logs alongside each session.' + #13#10 + #13#10 +
    'Click Next to continue.';
end;
