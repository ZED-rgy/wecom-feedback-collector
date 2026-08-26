# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ["wecom_feedback_desktop.py"],
    pathex=[],
    binaries=[],
    datas=[("web", "web")],
    hiddenimports=[
        "PIL.Image",
        "PIL.ImageDraw",
        "pystray._win32",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WeComFeedbackCollector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
