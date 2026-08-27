; Inno Setup 6 script for the per-user Windows distribution.
; Build the EXE first with ..\build_windows.ps1, then compile this file.

#define MyAppName "企微客户反馈助手"
#define MyAppExeName "WeComFeedbackCollector.exe"

[Setup]
AppId={{B5F8D9F7-6B1D-4E4E-8B85-9A41D0C2C8EA}
AppName={#MyAppName}
AppVersion=0.1.0
DefaultDirName={localappdata}\Programs\WeComFeedbackCollector
DefaultGroupName={#MyAppName}
OutputDir=..\dist-installer
OutputBaseFilename=WeComFeedbackCollector-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern
; Sign the installer in CI or on the release machine:
; SignTool=release-sign $f

[Files]
Source: "..\WeComFeedbackCollector.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\.env.example"; DestDir: "{app}"; DestName: ".env.example"; Flags: ignoreversion onlyifdoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--no-browser"; Description: "启动{#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\WeComFeedbackCollector\logs"
