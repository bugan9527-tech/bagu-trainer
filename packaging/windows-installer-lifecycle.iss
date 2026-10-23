; Shared production lifecycle; the acceptance fixture compiles this same code.
[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppGuid}_is1';
  InventoryName = 'BaguTrainer.install-files.txt';
  InvalidHandle = -1;

type
  TBackup = record
    RelativePath: String;
    Existed: Boolean;
  end;
  TPreservedLink = record
    Path, CopyPath: String;
    Existed: Boolean;
  end;
  IShellLinkW = interface(IUnknown)
    '{000214F9-0000-0000-C000-000000000046}'
    function GetPath(pszFile: String; cchMaxPath: Integer; pfd: Integer; fFlags: DWORD): HResult;
    procedure Dummy1; procedure Dummy2;
    function GetDescription(pszName: String; cchMaxName: Integer): HResult;
    function SetDescription(pszName: String): HResult;
    function GetWorkingDirectory(pszDir: String; cchMaxPath: Integer): HResult;
    function SetWorkingDirectory(pszDir: String): HResult;
    function GetArguments(pszArgs: String; cchMaxPath: Integer): HResult;
  end;
  IPersist = interface(IUnknown)
    '{0000010C-0000-0000-C000-000000000046}'
    function GetClassID(var classID: TGUID): HResult;
  end;
  IPersistFile = interface(IPersist)
    '{0000010B-0000-0000-C000-000000000046}'
    function IsDirty: HResult;
    function Load(pszFileName: String; dwMode: Longint): HResult;
  end;

var
  LockHandles: array of Integer;
  NewFiles, OldFiles: TArrayOfString;
  Backups: array of TBackup;
  BackupDir, PreviousDir: String;
  Installed, BackupReady: Boolean;
  WaitForm: TSetupForm;
  WaitLabel: TNewStaticText;
  WaitTimer, WaitTicks: Cardinal;
  WaitChecking, WaitSucceeded: Boolean;
  PreservedLinks: array of TPreservedLink;
  LinkBackups: array of TPreservedLink;

function OpenFileW(Name: String; Access, Sharing, Security, Creation, Flags, Template: Cardinal): Integer;
  external 'CreateFileW@kernel32.dll stdcall';
function CloseFileHandle(Handle: Integer): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';
function LockByte(Handle: Integer; Low, High, CountLow, CountHigh: Cardinal): Boolean;
  external 'LockFile@kernel32.dll stdcall';
function FileAttributes(Name: String): Cardinal;
  external 'GetFileAttributesW@kernel32.dll stdcall';
function WriteByte(Handle: Integer; Buffer: AnsiString; Count: Cardinal; var Written: Cardinal; Overlapped: Cardinal): Boolean;
  external 'WriteFile@kernel32.dll stdcall';
function FileSizeLow(Handle: Integer; High: Cardinal): Cardinal;
  external 'GetFileSize@kernel32.dll stdcall';
function SetWaitTimer(Window: HWND; ID, Interval: Cardinal; Callback: LongWord): Cardinal;
  external 'SetTimer@user32.dll stdcall';
function KillWaitTimer(Window: HWND; ID: Cardinal): Boolean;
  external 'KillTimer@user32.dll stdcall';

function DefaultDataRoot: String;
begin
  Result := ExpandConstant('{#DataRoot}');
end;

function DesktopShortcut: String;
begin
  Result := ExpandConstant('{#DesktopRoot}\AI 八股训练器.lnk');
end;

function MenuShortcut: String;
begin
  Result := ExpandConstant('{#MenuRoot}\AI 八股训练器\AI 八股训练器.lnk');
end;

function StartupShortcut: String;
begin
  Result := ExpandConstant('{#StartupRoot}\BaguTrainer.lnk');
end;

function RetainedDataDescription: String;
begin
  Result := '个人配置、题库、学习记录和日志将保留在：' + #13#10 + DefaultDataRoot;
  if (GetEnv('BAGU_USER_DATA_DIR') <> '') and (CompareText(GetEnv('BAGU_USER_DATA_DIR'), DefaultDataRoot) <> 0) then
    Result := Result + #13#10 + '显式数据目录也保留：' + GetEnv('BAGU_USER_DATA_DIR');
  Result := Result + #13#10 + 'Windows 凭据管理器中的 API Key 和自定义数据库不会删除。';
end;

function Normalize(Path: String): String;
begin
  Result := Lowercase(RemoveBackslashUnlessRoot(ExpandFileName(Path)));
end;

function UnicodePos(Needle, Haystack: String): Integer;
begin
  { Pos accepts AnyString. A narrow literal can select its ANSI overload,
    turning Chinese filenames into '?' on a non-Chinese Windows code page. }
  Result := Pos(Needle, Haystack);
end;

function Within(A, B: String): Boolean;
begin
  A := Normalize(A); B := Normalize(B);
  Result := (A = B) or (UnicodePos(AddBackslash(B), AddBackslash(A)) = 1);
end;

function NoReparse(Path: String): Boolean;
var Attr: Cardinal; Parent: String;
begin
  Result := False;
  Path := ExpandFileName(Path);
  while Path <> '' do begin
    Attr := FileAttributes(Path);
    if (Attr <> $FFFFFFFF) and ((Attr and $400) <> 0) then exit;
    Parent := ExtractFileDir(RemoveBackslashUnlessRoot(Path));
    if Parent = Path then break;
    Path := Parent;
  end;
  Result := True;
end;

function SafeRelative(Path: String): Boolean;
begin
  Result := (Path <> '') and (UnicodePos('..', Path) = 0) and (UnicodePos(':', Path) = 0) and
    (UnicodePos('/', Path) = 0) and (Path[1] <> '\') and (UnicodePos('*', Path) = 0) and
    (UnicodePos('?', Path) = 0) and (UnicodePos('{', Path) = 0) and
    (CompareText(ExtractFileExt(Path), '.lnk') <> 0);
end;

function HasPath(List: TArrayOfString; Name: String): Boolean;
var I: Integer;
begin
  Result := False;
  for I := 0 to GetArrayLength(List) - 1 do
    if CompareText(List[I], Name) = 0 then begin Result := True; exit; end;
end;

procedure ReleaseMaintenanceLocks;
var I: Integer;
begin
  for I := 0 to GetArrayLength(LockHandles) - 1 do CloseFileHandle(LockHandles[I]);
  SetArrayLength(LockHandles, 0);
end;

function AcquireLock(Root: String): Boolean;
var Handle, N: Integer; Written: Cardinal;
begin
  Result := False;
  if not NoReparse(Root) then exit;
  Root := AddBackslash(Root) + 'runtime';
  if not NoReparse(Root + '\owner.lock') then exit;
  if not ForceDirectories(Root) then exit;
  Handle := OpenFileW(Root + '\owner.lock', $C0000000, 3, 0, 4, $80, 0);
  if Handle = InvalidHandle then exit;
  if not LockByte(Handle, 0, 0, 1, 0) then begin CloseFileHandle(Handle); exit; end;
  if FileSizeLow(Handle, 0) = 0 then
    if not WriteByte(Handle, '0', 1, Written, 0) then begin CloseFileHandle(Handle); exit; end;
  N := GetArrayLength(LockHandles);
  SetArrayLength(LockHandles, N + 1); LockHandles[N] := Handle;
  Result := True;
end;

function AcquireMaintenanceLocks: Boolean;
var DefaultRoot, Profile, First, Second: String;
begin
  ReleaseMaintenanceLocks;
  DefaultRoot := DefaultDataRoot;
  Profile := GetEnv('BAGU_USER_DATA_DIR');
  if (Profile <> '') and ((Length(Profile) < 3) or (Copy(Profile, 2, 2) <> ':\')) then begin Result := False; exit; end;
  First := DefaultRoot; Second := Profile;
  if (Second <> '') and (CompareText(Normalize(First), Normalize(Second)) > 0) then begin
    First := Profile; Second := DefaultRoot;
  end;
  Result := AcquireLock(First);
  if Result and (Second <> '') and (Normalize(First) <> Normalize(Second)) then Result := AcquireLock(Second);
  if not Result then ReleaseMaintenanceLocks;
end;

function IsDirEmpty(Path: String): Boolean;
var Found: TFindRec;
begin
  Result := True;
  if FindFirst(AddBackslash(Path) + '*', Found) then begin
    try
      repeat
        if (Found.Name <> '.') and (Found.Name <> '..') then begin Result := False; break; end;
      until not FindNext(Found);
    finally FindClose(Found); end;
  end;
end;

function ValidateTarget(Target: String): String;
var Data, Profile: String;
begin
  Result := '';
  Data := DefaultDataRoot; Profile := GetEnv('BAGU_USER_DATA_DIR');
  if not NoReparse(Target) or Within(Target, Data) or Within(Data, Target) or
     (Length(RemoveBackslashUnlessRoot(ExpandFileName(Target))) <= 3) or
     Within(ExpandConstant('{localappdata}'), Target) then
    Result := '安装位置不安全或与个人数据目录冲突。请选择专用程序目录。';
  if (Profile <> '') and (Within(Target, Profile) or Within(Profile, Target)) then
    Result := '安装位置与 BAGU_USER_DATA_DIR 冲突。';
  if (PreviousDir <> '') and (Normalize(Target) <> Normalize(PreviousDir)) then
    Result := '升级须使用原安装位置；如需搬家，请先卸载后重新安装，个人数据将保留。';
  if (PreviousDir = '') and DirExists(Target) and not IsDirEmpty(Target) then
    Result := '目标不是空目录。不能覆盖便携版或其他目录；请选择新的专用程序目录。';
end;

function CheckOwnedFiles(List: TArrayOfString): Boolean;
var I, Handle: Integer; Target: String;
begin
  Result := False;
  for I := 0 to GetArrayLength(List) - 1 do begin
    if not SafeRelative(List[I]) then exit;
    Target := AddBackslash(ExpandConstant('{app}')) + List[I];
    if not NoReparse(Target) then exit;
    if FileExists(Target) then begin
      Handle := OpenFileW(Target, $C0000000, 0, 0, 3, $80, 0);
      if Handle = InvalidHandle then exit;
      CloseFileHandle(Handle);
    end;
  end;
  Result := True;
end;

function ValidInventory(List: TArrayOfString): Boolean;
var I: Integer;
begin
  Result := False;
  for I := 0 to GetArrayLength(List) - 1 do begin
    if not SafeRelative(List[I]) then begin Log('Unsafe inventory member: [' + List[I] + ']'); exit; end;
    if not NoReparse(AddBackslash(ExpandConstant('{app}')) + List[I]) then begin
      Log('Reparse path in inventory member: [' + List[I] + ']'); exit;
    end;
  end;
  Result := True;
end;

function TryMaintenance: Boolean;
begin
  Result := AcquireMaintenanceLocks;
  if Result then begin
    Result := CheckOwnedFiles(OldFiles) and CheckOwnedFiles(NewFiles);
    if not Result then ReleaseMaintenanceLocks;
  end;
end;

procedure WaitTick(Window: HWND; MessageID: Cardinal; TimerID: Cardinal; Time: Cardinal);
begin
  if WaitChecking then exit;
  WaitChecking := True;
  try
    Inc(WaitTicks);
    if TryMaintenance then begin WaitSucceeded := True; WaitForm.Close; end
    else begin
      WaitLabel.Caption := '训练器正在运行或程序文件被占用。请在训练器中正常退出；安装程序会自动继续检查。'
        + #13#10#13#10 + '已等待 ' + IntToStr(WaitTicks div 2) + ' 秒。不会强制结束任何进程，也不会覆盖正在使用的文件。';
      if WaitTicks >= 240 then WaitForm.Close;
    end;
  except
    WaitForm.Close;
  end;
  WaitChecking := False;
end;

function WaitForMaintenance(IsUninstall: Boolean): Boolean;
var Silent: Boolean; Seconds, Tick: Integer; CancelButton: TNewButton;
begin
  Result := TryMaintenance;
  if Result then exit;
  if IsUninstall then Silent := UninstallSilent else Silent := WizardSilent;
#ifdef LifecycleTestForceWaitDialog
  #ifndef LifecycleTestRoot
    #error A forced wait-dialog fixture must have isolated identity and paths
  #endif
  Silent := False;
#endif
  if Silent then begin
    { Automation may explicitly request a bounded wait; never open a dialog. }
    Seconds := StrToIntDef(ExpandConstant('{param:MAINTENANCEWAIT|0}'), 0);
    if Seconds < 0 then Seconds := 0;
    if Seconds > 300 then Seconds := 300;
    for Tick := 1 to Seconds * 4 do begin
      Sleep(250);
      if TryMaintenance then begin Result := True; exit; end;
    end;
    exit;
  end;
  WaitForm := CreateCustomForm(ScaleX(520), ScaleY(190), False, False);
  try
    WaitForm.Caption := '等待训练器正常退出';
    WaitLabel := TNewStaticText.Create(WaitForm);
    WaitLabel.Parent := WaitForm; WaitLabel.AutoSize := False; WaitLabel.WordWrap := True;
    WaitLabel.SetBounds(ScaleX(20), ScaleY(20), ScaleX(480), ScaleY(110));
    WaitLabel.Caption := '请在训练器中正常退出。正在自动检查应用与文件占用；可随时取消等待。';
    CancelButton := TNewButton.Create(WaitForm);
    CancelButton.Parent := WaitForm; CancelButton.Caption := '取消等待'; CancelButton.Cancel := True;
    CancelButton.ModalResult := mrCancel;
    CancelButton.SetBounds(ScaleX(400), ScaleY(145), ScaleX(100), ScaleY(28));
    WaitTicks := 0; WaitSucceeded := False;
    WaitTimer := SetWaitTimer(0, 0, 500, CreateCallback(@WaitTick));
    if WaitTimer = 0 then begin Result := False; exit; end;
    Log('等待界面已显示；仅检测维护锁和文件占用，不结束进程。');
    try WaitForm.ShowModal; Result := WaitSucceeded;
    finally KillWaitTimer(0, WaitTimer); WaitTimer := 0; end;
  finally WaitForm.Free; end;
  if not Result then ReleaseMaintenanceLocks;
end;

function InitializeSetup: Boolean;
var PreviousVersion: String; OldVersion, NewVersion: Int64;
begin
  Result := True;
  RegQueryStringValue(HKCU64, UninstallKey, 'InstallLocation', PreviousDir);
  if RegQueryStringValue(HKCU64, UninstallKey, 'DisplayVersion', PreviousVersion) then begin
    Result := StrToVersion(PreviousVersion, OldVersion) and StrToVersion('{#AppVersion}', NewVersion);
    if not Result then begin MsgBox('已安装版本信息损坏，请先修复旧安装。', mbError, MB_OK); exit; end;
    if ComparePackedVersion(OldVersion, NewVersion) > 0 then begin
      MsgBox('已安装更高版本，不能降级。', mbError, MB_OK); Result := False;
    end;
  end;
end;

procedure SnapshotShortcuts; forward;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var I, N: Integer; Target, Inventory: String;
begin
  Result := ValidateTarget(ExpandConstant('{app}'));
  if Result <> '' then exit;
  ExtractTemporaryFile(InventoryName);
  if not LoadStringsFromFile(ExpandConstant('{tmp}\') + InventoryName, NewFiles) or
     (GetArrayLength(NewFiles) = 0) then begin
    Result := '安装器文件清单无法读取，安装已停止。'; ReleaseMaintenanceLocks; exit;
  end;
  Inventory := AddBackslash(ExpandConstant('{app}')) + InventoryName;
  SetArrayLength(OldFiles, 0);
  if PreviousDir <> '' then
    if not NoReparse(Inventory) or not LoadStringsFromFile(Inventory, OldFiles) then begin
      Result := '旧安装的文件清单缺失，无法安全升级。请先修复旧安装。'; ReleaseMaintenanceLocks; exit;
    end;
  if not ValidInventory(OldFiles) or not ValidInventory(NewFiles) then begin
    Result := '受管文件清单或路径不安全，安装已停止。'; exit;
  end;
  if not WaitForMaintenance(False) then begin
    Result := '仍未取得维护锁或文件仍被占用。请正常退出训练器后重试；尚未更新程序文件。'; exit;
  end;
  for I := 0 to GetArrayLength(NewFiles) - 1 do
    if FileExists(AddBackslash(ExpandConstant('{app}')) + NewFiles[I]) and not HasPath(OldFiles, NewFiles[I]) then begin
      Result := '新版本与目录中的未知文件冲突，安装已停止。'; ReleaseMaintenanceLocks; exit;
    end;
  BackupDir := ExpandConstant('{tmp}\rollback');
  ForceDirectories(BackupDir);
  SetArrayLength(Backups, 0);
  for I := 0 to GetArrayLength(OldFiles) + GetArrayLength(NewFiles) do begin
    if I < GetArrayLength(OldFiles) then Target := OldFiles[I]
    else if I < GetArrayLength(OldFiles) + GetArrayLength(NewFiles) then Target := NewFiles[I - GetArrayLength(OldFiles)]
    else Target := InventoryName;
    N := GetArrayLength(Backups); SetArrayLength(Backups, N + 1);
    Backups[N].RelativePath := Target;
    Backups[N].Existed := FileExists(AddBackslash(ExpandConstant('{app}')) + Target);
    if Backups[N].Existed then
      if not FileCopy(AddBackslash(ExpandConstant('{app}')) + Target, BackupDir + '\' + IntToStr(N), False) then begin
        Result := '无法备份旧程序，未开始安装。请检查可用空间和权限。'; ReleaseMaintenanceLocks; exit;
      end;
  end;
  SnapshotShortcuts;
  BackupReady := True;
end;

function OwnedShortcut(Path, Arguments: String): Boolean;
var Obj: IUnknown; SL: IShellLinkW; PF: IPersistFile; Target, Args: String;
begin
  Result := False;
  if not FileExists(Path) then exit;
  try
    Obj := CreateComObject(StringToGuid('{00021401-0000-0000-C000-000000000046}'));
    PF := IPersistFile(Obj); OleCheck(PF.Load(Path, 0)); SL := IShellLinkW(Obj);
    SetLength(Target, 32768); OleCheck(SL.GetPath(Target, 32768, 0, 4)); SetLength(Target, UnicodePos(#0, Target) - 1);
    SetLength(Args, 32768); OleCheck(SL.GetArguments(Args, 32768)); SetLength(Args, UnicodePos(#0, Args) - 1);
    Result := (Normalize(Target) = Normalize(ExpandConstant('{app}\BaguTrainer.exe'))) and (Trim(Args) = Arguments);
  except
    Log('快捷方式归属无法确认，保留原入口。');
  end;
end;

procedure SnapshotLink(Path, Arguments: String);
var N: Integer;
begin
  if FileExists(Path) and not OwnedShortcut(Path, Arguments) then exit;
  if not NoReparse(Path) then RaiseException('快捷方式路径不安全，未更新入口。');
  N := GetArrayLength(LinkBackups); SetArrayLength(LinkBackups, N + 1);
  LinkBackups[N].Path := Path; LinkBackups[N].Existed := FileExists(Path);
  LinkBackups[N].CopyPath := ExpandConstant('{tmp}\link-backup-') + IntToStr(N);
  if LinkBackups[N].Existed then
    if not FileCopy(Path, LinkBackups[N].CopyPath, False) then RaiseException('无法备份本安装的快捷方式。');
end;

procedure SnapshotShortcuts;
begin
  SetArrayLength(LinkBackups, 0);
  SnapshotLink(MenuShortcut, ''); SnapshotLink(DesktopShortcut, ''); SnapshotLink(StartupShortcut, '--background');
end;

procedure DeleteOwnedShortcut(Path, Arguments: String);
begin
  if OwnedShortcut(Path, Arguments) and NoReparse(Path) then
    if not DeleteFile(Path) then RaiseException('无法删除本安装所属的快捷方式，请关闭占用该入口的程序后重试。');
end;

procedure UpdateOwnedShortcut(Path, Arguments: String; ShowCommand: Integer);
var Target: String;
begin
  if FileExists(Path) and not OwnedShortcut(Path, Arguments) then begin
    Log('快捷方式指向其他运行副本或无法确认归属，保留原入口：' + Path);
    exit;
  end;
  if not NoReparse(Path) or not ForceDirectories(ExtractFileDir(Path)) then
    RaiseException('无法安全创建快捷方式目录。');
  Target := ExpandConstant('{app}\BaguTrainer.exe');
  CreateShellLink(Path, '{#ProductName}', Target, Arguments, ExpandConstant('{app}'), Target, 0, ShowCommand);
end;

procedure MaintainShortcuts;
begin
  UpdateOwnedShortcut(MenuShortcut, '', SW_SHOWNORMAL);
  if WizardIsTaskSelected('desktopicon') then UpdateOwnedShortcut(DesktopShortcut, '', SW_SHOWNORMAL)
  else DeleteOwnedShortcut(DesktopShortcut, '');
  { Preserve enabled state; never take over a portable/source startup entry. }
  if OwnedShortcut(StartupShortcut, '--background') then
    UpdateOwnedShortcut(StartupShortcut, '--background', 7);
end;

procedure PreserveForeignShortcut(Path, Arguments: String);
var N: Integer;
begin
  if not FileExists(Path) or OwnedShortcut(Path, Arguments) then exit;
  if not NoReparse(Path) then RaiseException('无法安全保留其他副本的快捷方式，卸载已停止。');
  N := GetArrayLength(PreservedLinks); SetArrayLength(PreservedLinks, N + 1);
  PreservedLinks[N].Path := Path;
  PreservedLinks[N].CopyPath := ExpandConstant('{tmp}\preserved-link-') + IntToStr(N);
  if not FileCopy(Path, PreservedLinks[N].CopyPath, False) then
    RaiseException('无法保留其他副本的快捷方式，卸载已停止。');
end;

procedure RestoreForeignShortcuts;
var I: Integer;
begin
  { Old installers used unconditional [Icons] records. Shield foreign targets
    from those historical uninstall records, without modifying their logs. }
  for I := 0 to GetArrayLength(PreservedLinks) - 1 do
    if not FileExists(PreservedLinks[I].Path) and NoReparse(PreservedLinks[I].Path) then begin
      if not ForceDirectories(ExtractFileDir(PreservedLinks[I].Path)) or
         not FileCopy(PreservedLinks[I].CopyPath, PreservedLinks[I].Path, True) then
        RaiseException('其他副本的快捷方式恢复失败，已在临时目录保留备份。');
    end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var I: Integer;
begin
  if CurStep = ssPostInstall then begin
    for I := 0 to GetArrayLength(OldFiles) - 1 do
      if not HasPath(NewFiles, OldFiles[I]) then
        if FileExists(AddBackslash(ExpandConstant('{app}')) + OldFiles[I]) then
          if not DeleteFile(AddBackslash(ExpandConstant('{app}')) + OldFiles[I]) then
            RaiseException('无法清理旧程序文件，请重新运行安装器修复。');
    MaintainShortcuts;
    Installed := True;
    ReleaseMaintenanceLocks;
  end;
end;

procedure DeinitializeSetup;
var I: Integer; Target: String;
begin
  if BackupReady and not Installed then begin
    for I := GetArrayLength(Backups) - 1 downto 0 do begin
      Target := AddBackslash(ExpandConstant('{app}')) + Backups[I].RelativePath;
      if Backups[I].Existed then begin
        ForceDirectories(ExtractFileDir(Target));
        if not FileCopy(BackupDir + '\' + IntToStr(I), Target, False) then
          MsgBox('旧程序恢复失败，请重新运行安装器修复。个人数据未删除。', mbError, MB_OK);
      end else DeleteFile(Target);
    end;
    for I := 0 to GetArrayLength(LinkBackups) - 1 do begin
      Target := LinkBackups[I].Path;
      if NoReparse(Target) then begin
        if LinkBackups[I].Existed then begin
          ForceDirectories(ExtractFileDir(Target));
          FileCopy(LinkBackups[I].CopyPath, Target, False);
        end else if (Target = StartupShortcut) then DeleteOwnedShortcut(Target, '--background')
        else DeleteOwnedShortcut(Target, '');
      end;
    end;
  end;
  ReleaseMaintenanceLocks;
end;

function InitializeUninstall: Boolean;
begin
  Result := NoReparse(ExpandConstant('{app}'));
  if Result then begin
    Result := LoadStringsFromFile(ExpandConstant('{app}\') + InventoryName, OldFiles);
    if Result then Result := (GetArrayLength(OldFiles) > 0) and ValidInventory(OldFiles);
    if Result then Result := WaitForMaintenance(True);
  end;
  if not Result then begin
    ReleaseMaintenanceLocks;
    if not UninstallSilent then
      SuppressibleMsgBox('程序仍在运行、文件被占用或清单不安全。未删除程序；请正常退出后重试。', mbError, MB_OK, IDOK);
  end else begin
    if not UninstallSilent then SuppressibleMsgBox(RetainedDataDescription, mbInformation, MB_OK, IDOK);
  end;
end;

procedure InitializeUninstallProgressForm;
var LabelControl: TNewStaticText; Height: Integer;
begin
  Height := UninstallProgressForm.ClientHeight;
  UninstallProgressForm.Height := UninstallProgressForm.Height + ScaleY(100);
  LabelControl := TNewStaticText.Create(UninstallProgressForm);
  LabelControl.Parent := UninstallProgressForm; LabelControl.AutoSize := False; LabelControl.WordWrap := True;
  LabelControl.SetBounds(ScaleX(16), Height - ScaleY(20), UninstallProgressForm.ClientWidth - ScaleX(32), ScaleY(82));
  LabelControl.Caption := RetainedDataDescription;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then begin
    PreserveForeignShortcut(DesktopShortcut, '');
    PreserveForeignShortcut(MenuShortcut, '');
    PreserveForeignShortcut(StartupShortcut, '--background');
    DeleteOwnedShortcut(DesktopShortcut, '');
    DeleteOwnedShortcut(MenuShortcut, '');
    DeleteOwnedShortcut(StartupShortcut, '--background');
  end else if CurUninstallStep = usPostUninstall then begin
    RestoreForeignShortcuts;
    RemoveDir(ExtractFileDir(MenuShortcut)); { Only removes an empty owned folder. }
  end;
end;

procedure DeinitializeUninstall;
begin
  RestoreForeignShortcuts;
  ReleaseMaintenanceLocks;
end;
