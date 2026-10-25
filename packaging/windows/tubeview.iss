; Inno Setup script: turns the PyInstaller build (dist\TubeView) into TubeView-Setup.exe.
; Built by the GitHub workflow: iscc /DAppVersion=1.2.3 packaging\windows\tubeview.iss
#ifndef AppVersion
  #define AppVersion "dev"
#endif

[Setup]
AppId={{6F0B7E2C-6C9A-4F4B-9C55-2B7C3D8E1A10}
AppName=TubeView
AppVersion={#AppVersion}
AppPublisher=TubeView
DefaultDirName={localappdata}\Programs\TubeView
DefaultGroupName=TubeView
DisableProgramGroupPage=yes
; installs for the current user only: no admin prompt
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=TubeView-Setup
SetupIconFile=..\icons\tubeview.ico
UninstallDisplayIcon={app}\TubeView.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\..\dist\TubeView\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\TubeView"; Filename: "{app}\TubeView.exe"
Name: "{autodesktop}\TubeView"; Filename: "{app}\TubeView.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\TubeView.exe"; Description: "Open TubeView"; Flags: nowait postinstall skipifsilent
