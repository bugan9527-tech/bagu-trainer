"""Shared installed/portable cleanup: plan, validate, execute, verify, retry."""
import hashlib
import json
import os
from pathlib import Path

from .bank_storage import atomic_json
from .cleanup_manifest import safe_path, identity, profile_files, global_registry

ENVIRONMENT = ("BAGU_LLM_API_KEY", "BAGU_USER_DATA_DIR")
MUTEX_NAME = r"Local\BaguTrainer.Maintenance"


class MaintenanceMutex:
    def __init__(self):
        self.handle = None

    def acquire(self):
        if os.name != "nt":
            return True
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        self.handle = kernel.CreateMutexW(None, True, MUTEX_NAME)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            self.close()
            return False
        return True

    def close(self):
        if self.handle:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(self.handle)
            self.handle = None


def maintenance_active():
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    handle = kernel.OpenMutexW(0x100000, False, MUTEX_NAME)
    if handle:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)
    return bool(handle)


def clear_user_environment():
    """No secret values in results; HKLM entries are reported, never elevated."""
    if os.name != "nt":
        for name in ENVIRONMENT:
            os.environ.pop(name, None)
        return []
    import winreg
    import ctypes
    failures = []
    for hive, key_name, writable in ((winreg.HKEY_CURRENT_USER, "Environment", True),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", False)):
        try:
            with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | (winreg.KEY_SET_VALUE if writable else 0)) as key:
                for name in ENVIRONMENT:
                    try:
                        winreg.QueryValueEx(key, name)
                    except FileNotFoundError:
                        continue
                    if not writable:
                        failures.append(f"系统级环境变量 {name} 仍存在，需要管理员手动清除。")
                        continue
                    winreg.DeleteValue(key, name)
                    try:
                        winreg.QueryValueEx(key, name)
                        failures.append(f"用户环境变量 {name} 删除后仍存在")
                    except FileNotFoundError:
                        pass
        except FileNotFoundError:
            pass
        except OSError as error:
            failures.append(f"环境变量清理失败（{type(error).__name__}）")
    for name in ENVIRONMENT:
        os.environ.pop(name, None)
    from ctypes import wintypes
    result = ctypes.c_size_t()
    send = ctypes.windll.user32.SendMessageTimeoutW
    send.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR,
                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    send(0xFFFF, 0x1A, 0, "Environment", 2, 3000, ctypes.byref(result))
    return failures


class AppCleanup:
    def __init__(self, profiles, journal, *, install=None, registry=None, credential_services=None):
        self.profiles = [safe_path(root) for root in profiles]
        self.journal = safe_path(journal)
        self.install = safe_path(install) if install else None
        self.registry = safe_path(registry) if registry else None
        if any(root == self.journal or root in self.journal.parents for root in self.profiles):
            raise ValueError("卸载恢复记录必须位于待删数据目录之外")
        services = set(credential_services or ())
        if credential_services is None:
            from . import config
            default = Path(os.environ.get("LOCALAPPDATA", str(config.USER_DATA_ROOT.parent))) / "BaguTrainer"
            for root in self.profiles:
                normalized = os.path.normcase(str(root.resolve()))
                services.add("bagu-trainer.profile." + hashlib.sha256(normalized.encode()).hexdigest()[:24])
                if root == default:
                    services.add("bagu-trainer")
        self.services = sorted(services)

    @classmethod
    def discover(cls, journal, *, install=None):
        from . import config
        roots = {Path(config.USER_DATA_ROOT)}
        registry = global_registry()
        if registry.exists():
            safe_path(registry)
            record = json.loads(registry.read_text(encoding="utf-8"))
            for profile in record["profiles"]:
                path = safe_path(profile["root"])
                if path.exists() and identity(path) != profile["identity"]:
                    raise ValueError(f"已登记的数据目录身份发生变化：{path}")
                roots.add(path)
        if not os.environ.get("BAGU_TEST_INSTANCE"):
            roots.add(Path(os.environ["LOCALAPPDATA"]) / "BaguTrainer")
        return cls(sorted(roots), journal, install=install, registry=registry)

    def plan(self):
        files, protected = {}, []
        for root in self.profiles:
            values, kept = profile_files(root)
            files.update({item["path"]: item for item in values})
            protected.extend(kept)
        if self.registry and self.registry.exists():
            files[str(self.registry)] = {"path": str(self.registry), "identity": identity(self.registry)}
        program_files = []
        if self.install:
            manifest = self.install / "BUILD_MANIFEST.json"
            safe_path(manifest)
            record = json.loads(manifest.read_text(encoding="utf-8"))
            if record.get("application") != "BaguTrainer":
                raise ValueError("程序文件清单不属于本应用")
            for entry in [*record["files"], {"path": manifest.name}]:
                relative = Path(entry["path"])
                if relative.is_absolute() or relative.drive or ".." in relative.parts:
                    raise ValueError("程序清单越界")
                path = safe_path(self.install / relative)
                if path.exists():
                    program_files.append({"path": str(path), "identity": identity(path)})
            if os.name == "nt":
                from .login_startup import read_shortcut
                import ctypes
                for folder, relative, arguments in ((0, "AI 八股训练器.lnk", ""),
                        (2, "AI 八股训练器/AI 八股训练器.lnk", ""), (7, "BaguTrainer.lnk", "--background")):
                    buffer = ctypes.create_unicode_buffer(32768)
                    if ctypes.windll.shell32.SHGetFolderPathW(None, folder, None, 0, buffer) != 0:
                        raise OSError("无法核对快捷方式目录")
                    link = safe_path(Path(buffer.value) / relative)
                    if link.exists():
                        entry = read_shortcut(link)
                        if (Path(entry["target"]).resolve() == (self.install / "BaguTrainer.exe").resolve()
                                and entry["arguments"] == arguments):
                            files[str(link)] = {"path": str(link), "identity": identity(link)}
        return {"schema": 1, "profiles": [str(p) for p in self.profiles], "files": list(files.values()),
            "program_files": program_files, "services": self.services,
            "protected": protected, "phase": "data", "failures": []}

    def execute(self, plan=None, *, backend=None, environment=clear_user_environment, progress=None):
        from .secrets import remove_credential
        emit = progress or (lambda message: None)
        if self.journal.exists():
            saved = json.loads(self.journal.read_text(encoding="utf-8"))
            if saved.get("schema") != 1 or saved.get("profiles") != [str(p) for p in self.profiles]:
                raise ValueError("卸载恢复记录与数据位置不一致")
            plan = saved
        elif plan is None:
            plan = self.plan()
        # Freeze the complete inventory before the first credential/config delete.
        atomic_json(self.journal, plan)
        failures = []
        emit("正在清除本机 API 凭据（不会吊销服务商后台 Key）")
        for service in plan["services"]:
            try:
                remove_credential(service, backend)
            except Exception as error:
                failures.append(f"凭据 {service}：{type(error).__name__}，请修复凭据服务后重试。")
        failures.extend(environment())
        emit("正在清理学习数据库、题库、配置与日志")
        def remove_files(items):
            for item in items:
                try:
                    path = safe_path(item["path"])
                    if path.exists():
                        if identity(path) != item["identity"]:
                            raise ValueError("文件身份变化，已保留")
                        path.unlink()
                    if path.exists():
                        raise OSError("删除后仍存在")
                except Exception as error:
                    failures.append(f"{item['path']}：{type(error).__name__}，请解除占用或修复权限后重试。")
        # Maintenance probes reopen these files on every retry. Keep their
        # original identities while a data/credential failure is retryable.
        owner_locks = {root / "runtime/owner.lock" for root in self.profiles}
        remove_files(item for item in plan["files"] if Path(item["path"]) not in owner_locks)
        # Keep the program/retry helper if *any* data/credential removal failed.
        if not failures:
            emit("数据已核验，正在删除本副本的受管程序文件")
            for item in plan["program_files"]:
                try:
                    path = safe_path(item["path"])
                    if path.exists():
                        if identity(path) != item["identity"]:
                            raise ValueError("程序文件身份变化")
                        path.unlink()
                except Exception as error:
                    failures.append(f"{item['path']}：{type(error).__name__}")
        if not failures:
            remove_files(item for item in plan["files"] if Path(item["path"]) in owner_locks)
        plan["failures"] = failures
        if failures:
            atomic_json(self.journal, plan)
        else:
            self.journal.unlink(missing_ok=True)
            # Empty directory removal only; shared directories are never rmtree'd.
            roots = [*self.profiles, *([self.install] if self.install else [])]
            for root in roots:
                if root.exists():
                    parents = {parent for item in [*plan["files"], *plan["program_files"]]
                               for parent in Path(item["path"]).parents if parent != root and root in parent.parents}
                    for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
                        try:
                            safe_path(parent).rmdir()
                        except OSError:
                            pass
                    try:
                        from . import config, paths
                        protected_roots = {Path(root.anchor), Path.home(), Path.home() / "Desktop", Path(config.PROJECT_ROOT)}
                        if not paths.FROZEN:
                            protected_roots.add(Path(paths.INSTALL_ROOT))
                        if root not in protected_roots:
                            root.rmdir()
                    except OSError:
                        pass
        return {"ok": not failures, "failures": failures, "protected": plan["protected"]}
