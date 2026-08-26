from __future__ import annotations

import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
import threading
import webbrowser
from pathlib import Path

from .startup import application_home


logger = logging.getLogger("wecom_feedback.desktop")
MUTEX_NAME = "Local\\WeComFeedbackCollectorDesktop"


def _acquire_single_instance() -> int:
    if os.name != "nt":
        return 0
    handle = int(ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME))
    if not handle:
        raise RuntimeError("无法创建桌面程序互斥锁")
    if ctypes.windll.kernel32.GetLastError() == 183:
        ctypes.windll.kernel32.CloseHandle(handle)
        raise RuntimeError("企微反馈收集程序已经在运行")
    return handle


def _release_single_instance(handle: int) -> None:
    if handle and os.name == "nt":
        ctypes.windll.kernel32.CloseHandle(handle)


def _tray_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (37, 99, 235, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((10, 12, 54, 45), radius=10, fill="white")
    draw.polygon(((23, 43), (18, 55), (34, 45)), fill="white")
    draw.ellipse((20, 25, 25, 30), fill=(37, 99, 235, 255))
    draw.ellipse((30, 25, 35, 30), fill=(37, 99, 235, 255))
    draw.ellipse((40, 25, 45, 30), fill=(37, 99, 235, 255))
    return image


def run_desktop(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Run an embedded configuration window and background tray services."""
    os.chdir(application_home())
    Path("logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            RotatingFileHandler(
                Path("logs") / "desktop.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        ],
    )
    try:
        import pystray
    except ImportError as exc:
        raise RuntimeError('桌面托盘依赖未安装，请运行：python -m pip install -e ".[desktop]"') from exc
    try:
        import webview
    except ImportError:
        webview = None

    from .webapp import create_dashboard_server, start_background_controllers, stop_background_controllers

    instance_handle = _acquire_single_instance()
    server = create_dashboard_server(host, port)
    server_thread = threading.Thread(target=server.serve_forever, name="wecom-dashboard", daemon=True)
    url = f"http://{host}:{port}/"
    exit_requested = threading.Event()
    embedded_window = None

    def open_console(_icon=None, _item=None) -> None:
        if embedded_window is None:
            webbrowser.open(url)
            return
        embedded_window.show()
        embedded_window.restore()

    def exit_app(icon, _item=None) -> None:
        exit_requested.set()
        icon.stop()
        if embedded_window is not None:
            embedded_window.destroy()
        else:
            server.shutdown()

    icon = pystray.Icon(
        "wecom-feedback-collector",
        _tray_image(),
        "企微反馈收集程序",
        menu=pystray.Menu(
            pystray.MenuItem("打开配置控制台", open_console, default=True),
            pystray.MenuItem("退出程序", exit_app),
        ),
    )
    try:
        server_thread.start()
        start_background_controllers()
        logger.info("desktop application started at %s", url)
        if webview is None:
            if open_browser:
                open_console()
            icon.run()
        else:
            embedded_window = webview.create_window(
                "企微反馈收集控制台",
                url,
                width=1220,
                height=820,
                min_size=(900, 620),
                hidden=not open_browser,
                background_color="#f5f7fb",
                text_select=True,
            )

            def hide_instead_of_close() -> bool:
                if exit_requested.is_set():
                    return True
                embedded_window.hide()
                return False

            embedded_window.events.closing += hide_instead_of_close
            icon.run_detached()
            webview.start(gui="edgechromium", debug=False)
    finally:
        icon.stop()
        stop_background_controllers()
        server.shutdown()
        server.server_close()
        _release_single_instance(instance_handle)
        logger.info("desktop application stopped")
