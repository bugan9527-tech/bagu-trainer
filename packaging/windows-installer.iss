; Compile only through scripts/build_installer.py: PayloadInclude is an exact,
; audited file list, not a wildcard over the repository or dist directory.
#ifndef AppVersion
  #error AppVersion must come from scripts/build_installer.py
#endif
#ifndef PayloadInclude
  #error An audited PayloadInclude is required
#endif
#ifndef OutputPath
  #error OutputPath is required
#endif
#ifdef LifecycleTestRoot
  #ifndef LifecycleTestAppId
    #error An isolated test AppId is required
  #endif
  #define ProductName "BaguTrainer lifecycle fixture"
  #define AppGuid LifecycleTestAppId
  #define SetupAppId LifecycleTestAppId
  #define SetupIdentity LifecycleTestAppId
  #define DataRoot LifecycleTestRoot + "\user-data"
  #define InstallRoot LifecycleTestRoot + "\installed"
  #define DesktopRoot LifecycleTestRoot + "\desktop"
  #define MenuRoot LifecycleTestRoot + "\start-menu"
  #define StartupRoot LifecycleTestRoot + "\startup"
#else
  #define ProductName "AI 八股训练器"
  #define AppGuid "{C95D2707-83B5-4D34-A88D-7F30C5AC6E19}"
  #define SetupAppId "{{C95D2707-83B5-4D34-A88D-7F30C5AC6E19}"
  #define SetupIdentity "C95D2707-83B5-4D34-A88D-7F30C5AC6E19"
  #define DataRoot "{localappdata}\BaguTrainer"
  #define InstallRoot "{localappdata}\Programs\BaguTrainer"
  #define DesktopRoot "{userdesktop}"
  #define MenuRoot "{userprograms}"
  #define StartupRoot "{userstartup}"
#endif

[Setup]
AppId={#SetupAppId}
AppName={#ProductName}
AppVersion={#AppVersion}
AppVerName={#ProductName} {#AppVersion}
AppPublisher=BaguTrainer
VersionInfoVersion={#AppVersion}.0
VersionInfoProductVersion={#AppVersion}
VersionInfoProductName={#ProductName}
VersionInfoDescription={#ProductName} 安装程序
DefaultDirName={#InstallRoot}
DefaultGroupName=AI 八股训练器
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0
UsePreviousAppDir=yes
UsePreviousTasks=yes
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
SetupIconFile=BaguTrainer.ico
UninstallDisplayIcon={app}\BaguTrainer.exe
UninstallDisplayName={#ProductName}
Uninstallable=yes
CloseApplications=no
RestartApplications=no
SetupMutex=BaguTrainer.Setup.{#SetupIdentity}
OutputDir={#OutputPath}
OutputBaseFilename=BaguTrainer-{#AppVersion}-windows-x64-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=110
ShowLanguageDialog=no
AllowNetworkDrive=no
AllowUNCPath=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"

[Messages]
WelcomeLabel2=即将安装 [name/ver]。%n%n本程序仅为当前用户安装，无需管理员权限。%n%n升级和修复安装保留个人数据。主动卸载将清除受管题库、学习记录、配置及本机 API 凭据。继续前请先正常退出训练器。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

#include PayloadInclude

; Shortcuts are managed in Pascal Script with ownership checks, not
; unconditional [Icons] uninstall records. Legacy records are shielded too.
#ifdef LifecycleTestLegacyIcons
  #ifndef LifecycleTestRoot
    #error Legacy icon fixtures require isolated paths and identity
  #endif
[Icons]
Name: "{#DesktopRoot}\AI 八股训练器"; Filename: "{app}\BaguTrainer.exe"; Tasks: desktopicon
Name: "{#MenuRoot}\AI 八股训练器\AI 八股训练器"; Filename: "{app}\BaguTrainer.exe"
#endif

[Run]
Filename: "{app}\BaguTrainer.exe"; Description: "立即启动 AI 八股训练器"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent unchecked; BeforeInstall: ReleaseMaintenanceLocks

#include "windows-installer-lifecycle.iss"
