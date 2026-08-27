from __future__ import annotations

import os
import sys
from pathlib import Path

from .paths import application_home


APP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_RUN_NAME = "WeComFeedbackCollector"


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}" --no-browser'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    executable = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{executable}" -m wecom_feedback desktop --no-browser'


def is_startup_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_RUN_NAME)
    except FileNotFoundError:
        return False
    return str(value).strip() == startup_command()


def set_startup_enabled(enabled: bool) -> None:
    if os.name != "nt":
        raise RuntimeError("开机启动仅支持 Windows")
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, APP_RUN_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_RUN_NAME)
            except FileNotFoundError:
                pass
