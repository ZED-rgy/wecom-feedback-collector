from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_transform(value: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI 仅支持 Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source = ctypes.create_string_buffer(value)
    source_blob = _DataBlob(len(value), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    result_blob = _DataBlob()
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(source_blob), None, None, None, None, 0, ctypes.byref(result_blob)
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source_blob), None, None, None, None, 0, ctypes.byref(result_blob)
        )
    if not ok:
        raise OSError(f"Windows DPAPI 调用失败（错误码 {ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(result_blob.pbData, result_blob.cbData)
    finally:
        kernel32.LocalFree(result_blob.pbData)


def protect_secret(value: str) -> str | None:
    """Protect a secret for the current Windows user, returning base64 text."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return "dpapi:" + base64.b64encode(_dpapi_transform(text.encode("utf-8"), protect=True)).decode("ascii")
    except (OSError, RuntimeError, ValueError):
        return None


def unprotect_secret(value: str) -> str:
    """Decode a DPAPI value; return an empty string for invalid ciphertext."""
    text = str(value or "").strip()
    if not text.startswith("dpapi:"):
        return text
    try:
        encrypted = base64.b64decode(text[6:], validate=True)
        return _dpapi_transform(encrypted, protect=False).decode("utf-8")
    except (OSError, RuntimeError, UnicodeDecodeError, ValueError):
        return ""
