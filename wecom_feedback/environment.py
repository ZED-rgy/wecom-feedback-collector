from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import config_path, ensure_user_data_dirs, user_data_home


@dataclass(frozen=True)
class EnvironmentSnapshot:
    ready: bool
    platform: str
    architecture: str
    frozen: bool
    user_data_path: str
    config_path: str
    data_directory_writable: bool
    wecom_process_count: int
    webview2_installed: bool
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _wecom_process_count() -> int:
    if os.name != "nt":
        return 0
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WXWork.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=3,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    return sum(1 for line in result.stdout.splitlines() if "WXWork.exe" in line)


def _webview2_installed() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        client_key = r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, client_key):
                    return True
            except FileNotFoundError:
                continue
    except (ImportError, OSError):
        pass
    return bool(shutil.which("msedgewebview2.exe"))


def check_environment() -> EnvironmentSnapshot:
    warnings: list[str] = []
    try:
        root = ensure_user_data_dirs()
        probe = root / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        writable = True
    except OSError:
        root = user_data_home()
        writable = False
        warnings.append("用户数据目录不可写，请检查权限")
    process_count = _wecom_process_count()
    if os.name == "nt" and process_count == 0:
        warnings.append("未检测到 WXWork.exe，请先登录企微")
    webview2 = _webview2_installed()
    if os.name == "nt" and not webview2:
        warnings.append("未检测到 WebView2，程序将尝试使用系统浏览器")
    if os.name != "nt":
        warnings.append("本地企微数据库接收仅支持 Windows")
    return EnvironmentSnapshot(
        ready=writable and (os.name != "nt" or process_count > 0),
        platform=platform.platform(),
        architecture=platform.machine(),
        frozen=bool(getattr(sys, "frozen", False)),
        user_data_path=str(root),
        config_path=str(config_path()),
        data_directory_writable=writable,
        wecom_process_count=process_count,
        webview2_installed=webview2,
        warnings=tuple(warnings),
    )
