from __future__ import annotations

"""Read the signed-in Windows WeCom account's local message store.

The adapter is intentionally read-only: it reads WXWork process memory to find
the in-memory wxSQLite3 key, snapshots encrypted database/WAL bytes, and
decrypts them into in-memory SQLite databases. It never injects code into
WXWork and never modifies the original databases.

The page cipher and protobuf text extraction are compatible with the MIT
licensed tzwkb/wecom-agent implementation, adapted here for this project's
privacy and lifecycle requirements.
"""

import ctypes
import hashlib
import logging
import os
import re
import sqlite3
import struct
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from ctypes import wintypes

from ..config import Settings
from ..models import RawMessage
from ..services.ingestion import mentions_target

logger = logging.getLogger("wecom_feedback.windows_local_db")

PAGE_SIZE = 4096
SQLITE_HEADER = b"SQLite format 3\x00"
WXSQLITE3_SALT = b"sAlT"
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE_PAGE_TYPES = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}
WRITABLE_PAGE_TYPES = {0x04, 0x08, 0x40, 0x80}


class WindowsLocalDbError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalDbDiagnostic:
    ready: bool
    process_found: bool
    database_found: bool
    key_verified: bool
    group_found: bool
    client_version: str = ""
    message_count: int = 0
    mention_count: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * 260),
    ]


class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


@dataclass(frozen=True)
class _MemoryRegion:
    base: int
    size: int
    state: int
    protect: int
    kind: int

    @property
    def readable(self) -> bool:
        base_protect = self.protect & 0xFF
        return (
            self.state == MEM_COMMIT
            and not (self.protect & PAGE_GUARD)
            and base_protect != PAGE_NOACCESS
            and base_protect in READABLE_PAGE_TYPES
        )

    @property
    def writable_private(self) -> bool:
        return self.readable and self.kind == MEM_PRIVATE and (self.protect & 0xFF) in WRITABLE_PAGE_TYPES


class _ProcessMemory:
    PROCESS_VM_READ = 0x0010
    PROCESS_QUERY_INFORMATION = 0x0400

    def __init__(self, pid: int):
        if os.name != "nt":
            raise WindowsLocalDbError("本地数据库接收器仅支持 Windows")
        self.pid = pid
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel.OpenProcess.restype = wintypes.HANDLE
        self._kernel.ReadProcessMemory.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._kernel.ReadProcessMemory.restype = wintypes.BOOL
        self._kernel.VirtualQueryEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.POINTER(_MEMORY_BASIC_INFORMATION),
            ctypes.c_size_t,
        ]
        self._kernel.VirtualQueryEx.restype = ctypes.c_size_t
        self.handle = self._kernel.OpenProcess(
            self.PROCESS_VM_READ | self.PROCESS_QUERY_INFORMATION, False, pid
        )
        if not self.handle:
            raise WindowsLocalDbError(f"无法只读打开企微进程（错误码 {ctypes.get_last_error()}）")

    def close(self) -> None:
        if self.handle:
            self._kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "_ProcessMemory":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def read(self, address: int, size: int) -> bytes:
        if size <= 0:
            return b""
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t()
        ok = self._kernel.ReadProcessMemory(
            self.handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(read)
        )
        return buffer.raw[: read.value] if ok else b""

    def region_at(self, address: int) -> _MemoryRegion | None:
        mbi = _MEMORY_BASIC_INFORMATION()
        if not self._kernel.VirtualQueryEx(
            self.handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)
        ):
            return None
        return _MemoryRegion(
            int(mbi.BaseAddress or 0), int(mbi.RegionSize), int(mbi.State), int(mbi.Protect), int(mbi.Type)
        )

    def regions(self, maximum_address: int = 0x80000000) -> Iterable[_MemoryRegion]:
        address = 0
        while address < maximum_address:
            region = self.region_at(address)
            if region is None:
                break
            yield region
            next_address = region.base + max(region.size, 0x1000)
            address = max(address + 0x1000, next_address)


def _kernel32() -> ctypes.WinDLL:
    if os.name != "nt":
        raise WindowsLocalDbError("本地数据库接收器仅支持 Windows")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _wxwork_processes() -> list[tuple[int, int]]:
    kernel = _kernel32()
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return []
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    found: list[tuple[int, int]] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.szExeFile.lower() == "wxwork.exe":
                pid = int(entry.th32ProcessID)
                working_set = 0
                try:
                    with _ProcessMemory(pid) as process:
                        counters = _PROCESS_MEMORY_COUNTERS()
                        counters.cb = ctypes.sizeof(counters)
                        if psapi.GetProcessMemoryInfo(
                            process.handle, ctypes.byref(counters), ctypes.sizeof(counters)
                        ):
                            working_set = int(counters.WorkingSetSize)
                except WindowsLocalDbError:
                    pass
                found.append((pid, working_set))
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return sorted(found, key=lambda item: item[1], reverse=True)


def _module_info(pid: int, module_name: str = "WXWork.exe") -> tuple[int, Path]:
    kernel = _kernel32()
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000008 | 0x00000010, pid)
    if snapshot == wintypes.HANDLE(-1).value:
        raise WindowsLocalDbError("无法读取企微模块信息")
    try:
        entry = _MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Module32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.szModule.lower() == module_name.lower():
                return ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0, Path(entry.szExePath)
            more = kernel.Module32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    raise WindowsLocalDbError("未找到企微主模块")


def _pe_sections(data: bytes) -> tuple[int, list[tuple[int, int, int, int]]]:
    if data[:2] != b"MZ":
        raise WindowsLocalDbError("企微主程序不是有效 PE 文件")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\x00\x00":
        raise WindowsLocalDbError("企微主程序 PE 头无效")
    section_count = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    image_base = (
        struct.unpack_from("<I", data, optional + 28)[0]
        if magic == 0x10B
        else struct.unpack_from("<Q", data, optional + 24)[0]
    )
    start = optional + optional_size
    sections: list[tuple[int, int, int, int]] = []
    for index in range(section_count):
        offset = start + index * 40
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from(
            "<IIII", data, offset + 8
        )
        sections.append((virtual_address, virtual_size, raw_pointer, raw_size))
    return int(image_base), sections


def _file_offset_to_rva(offset: int, sections: list[tuple[int, int, int, int]]) -> int | None:
    for virtual_address, _virtual_size, raw_pointer, raw_size in sections:
        if raw_pointer <= offset < raw_pointer + raw_size:
            return virtual_address + offset - raw_pointer
    return None


def _db_key_manager_vtable_rva(executable: Path) -> int:
    data = executable.read_bytes()
    image_base, sections = _pe_sections(data)
    type_name = b".?AVDbKeyManager@logic@wework@@"
    name_offset = data.find(type_name)
    if name_offset < 8:
        raise WindowsLocalDbError("当前企微版本未找到 DbKeyManager 类型信息")
    type_descriptor_offset = name_offset - 8
    type_descriptor_rva = _file_offset_to_rva(type_descriptor_offset, sections)
    if type_descriptor_rva is None:
        raise WindowsLocalDbError("无法映射 DbKeyManager 类型信息")
    descriptor_va = image_base + type_descriptor_rva
    descriptor_pointer = struct.pack("<I", descriptor_va)
    reference = data.find(descriptor_pointer)
    while reference >= 12:
        complete_locator_offset = reference - 12
        signature, _offset, _cd_offset, descriptor, _hierarchy = struct.unpack_from(
            "<IIIII", data, complete_locator_offset
        )
        if signature in (0, 1) and descriptor == descriptor_va:
            locator_rva = _file_offset_to_rva(complete_locator_offset, sections)
            if locator_rva is not None:
                locator_va = image_base + locator_rva
                locator_reference = data.find(struct.pack("<I", locator_va))
                if locator_reference >= 0:
                    vtable_reference_rva = _file_offset_to_rva(locator_reference, sections)
                    if vtable_reference_rva is not None:
                        return vtable_reference_rva + 4
        reference = data.find(descriptor_pointer, reference + 1)
    raise WindowsLocalDbError("无法定位 DbKeyManager 虚表")


def _generate_initial_vector(page_number: int) -> bytes:
    value = page_number + 1
    output = bytearray()
    for _ in range(4):
        quotient = value // 52774
        value = 40692 * (value - 52774 * quotient) - 3791 * quotient
        if value < 0:
            value += 2147483399
        output.extend(struct.pack("<I", value & 0xFFFFFFFF))
    return hashlib.md5(output).digest()


def _derive_page_key(raw_key: bytes, page_number: int) -> bytes:
    return hashlib.md5(raw_key + struct.pack("<I", page_number) + WXSQLITE3_SALT).digest()


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES
    except ImportError as exc:  # pragma: no cover - Windows optional dependency
        raise WindowsLocalDbError(
            "本地数据库解密需要 pycryptodome，请执行: pip install -e \".[windows]\""
        ) from exc
    return AES.new(key, AES.MODE_CBC, iv).decrypt(data)


def _has_plain_header_fragment(page: bytes) -> bool:
    if len(page) < 24:
        return False
    fragment = page[16:24]
    page_size = int.from_bytes(fragment[:2], "big")
    if page_size == 1:
        page_size = 65536
    return (
        512 <= page_size <= 65536
        and page_size & (page_size - 1) == 0
        and fragment[5:8] == b"\x40\x20\x20"
    )


def _quick_verify_key(raw_key: bytes, first_page: bytes) -> bool:
    if len(raw_key) != 16 or len(first_page) < 32 or not _has_plain_header_fragment(first_page):
        return False
    cipher = first_page[8:16] + first_page[24:32]
    plain = _aes_cbc_decrypt(
        _derive_page_key(raw_key, 1), _generate_initial_vector(1), cipher
    )
    return plain[:8] == first_page[16:24]


def _decrypt_page(raw_key: bytes, encrypted_page: bytes, page_number: int) -> bytes:
    if len(encrypted_page) != PAGE_SIZE:
        raise WindowsLocalDbError("数据库页长度不是 4096 字节")
    page = bytearray(encrypted_page)
    if page_number == 1 and _has_plain_header_fragment(page):
        fragment = bytes(page[16:24])
        page[16:24] = page[8:16]
        page[16:] = _aes_cbc_decrypt(
            _derive_page_key(raw_key, 1), _generate_initial_vector(1), bytes(page[16:])
        )
        if bytes(page[16:24]) != fragment:
            raise WindowsLocalDbError("数据库密钥校验失败")
        page[:16] = SQLITE_HEADER
        return bytes(page)
    return _aes_cbc_decrypt(
        _derive_page_key(raw_key, page_number),
        _generate_initial_vector(page_number),
        bytes(page),
    )


def _verify_key(raw_key: bytes, first_page: bytes) -> bool:
    if not _quick_verify_key(raw_key, first_page):
        return False
    try:
        page = _decrypt_page(raw_key, first_page[:PAGE_SIZE], 1)
    except (ValueError, WindowsLocalDbError):
        return False
    return page[:16] == SQLITE_HEADER and page[100] in (0x02, 0x05, 0x0A, 0x0D)


def _decrypt_database_bytes(encrypted: bytes, raw_key: bytes) -> bytearray:
    output = bytearray()
    for offset in range(0, len(encrypted), PAGE_SIZE):
        page = encrypted[offset : offset + PAGE_SIZE]
        if len(page) < PAGE_SIZE:
            page += b"\x00" * (PAGE_SIZE - len(page))
        output.extend(_decrypt_page(raw_key, page, offset // PAGE_SIZE + 1))
    return output


def _apply_wal_snapshot(plain_database: bytearray, encrypted_wal: bytes, raw_key: bytes) -> bytearray:
    if len(encrypted_wal) < 32:
        return plain_database
    magic, _version, wal_page_size = struct.unpack_from(">III", encrypted_wal, 0)
    if magic not in (0x377F0682, 0x377F0683) or wal_page_size not in (0, PAGE_SIZE):
        return plain_database
    salt = encrypted_wal[16:24]
    frame_size = 24 + PAGE_SIZE
    frame_count = (len(encrypted_wal) - 32) // frame_size
    frames: list[tuple[int, int, bytes]] = []
    last_commit = -1
    commit_size = 0
    for index in range(frame_count):
        start = 32 + index * frame_size
        header = encrypted_wal[start : start + 24]
        if header[8:16] != salt:
            break
        page_number, database_size = struct.unpack_from(">II", header, 0)
        if not page_number:
            break
        page = encrypted_wal[start + 24 : start + frame_size]
        frames.append((page_number, database_size, page))
        if database_size:
            last_commit = len(frames) - 1
            commit_size = database_size
    if last_commit < 0:
        return plain_database
    needed = commit_size * PAGE_SIZE
    if len(plain_database) < needed:
        plain_database.extend(b"\x00" * (needed - len(plain_database)))
    for page_number, _database_size, encrypted_page in frames[: last_commit + 1]:
        start = (page_number - 1) * PAGE_SIZE
        plain_database[start : start + PAGE_SIZE] = _decrypt_page(
            raw_key, encrypted_page, page_number
        )
    del plain_database[needed:]
    return plain_database


def _looks_like_key(candidate: bytes) -> bool:
    return (
        len(candidate) == 16
        and len(set(candidate)) >= 11
        and sum(value < 0x20 or value > 0x7E for value in candidate) >= 3
    )


def _scan_key(first_page: bytes) -> tuple[int, bytes, str]:
    processes = _wxwork_processes()
    if not processes:
        raise WindowsLocalDbError("未发现已登录的企业微信进程")
    last_error = ""
    for pid, _working_set in processes:
        try:
            module_base, executable = _module_info(pid)
            vtable = module_base + _db_key_manager_vtable_rva(executable)
            pointer = struct.pack("<I", vtable)
            with _ProcessMemory(pid) as process:
                candidate_regions: dict[int, _MemoryRegion] = {}
                for region in process.regions():
                    if not region.writable_private or region.size > 64 * 1024 * 1024:
                        continue
                    data = process.read(region.base, region.size)
                    position = data.find(pointer)
                    while position >= 0:
                        object_address = region.base + position
                        object_region = process.region_at(object_address)
                        if object_region and object_region.writable_private:
                            candidate_regions[object_region.base] = object_region
                        position = data.find(pointer, position + 1)
                for region in candidate_regions.values():
                    data = process.read(region.base, region.size)
                    for offset in range(0, len(data) - 15, 8):
                        candidate = data[offset : offset + 16]
                        if _looks_like_key(candidate) and _quick_verify_key(candidate, first_page):
                            if _verify_key(candidate, first_page):
                                versions = sorted(
                                    (
                                        child.name
                                        for child in executable.parent.iterdir()
                                        if child.is_dir() and re.fullmatch(r"\d+(?:\.\d+){2,}", child.name)
                                    ),
                                    reverse=True,
                                )
                                version = versions[0] if versions else executable.parent.name
                                return pid, candidate, version
            last_error = "已定位密钥管理器，但未找到可验证的消息库密钥"
        except (OSError, ValueError, struct.error, WindowsLocalDbError) as exc:
            last_error = str(exc)
    raise WindowsLocalDbError(last_error or "无法从企微进程读取数据库密钥")


def _discover_data_directories() -> list[Path]:
    root = Path.home() / "Documents" / "WXWork"
    candidates = [
        path.parent
        for path in root.glob("*/Data/message.db")
        if "Backup" not in path.parts and path.is_file()
    ]
    return sorted(candidates, key=lambda path: (path / "message.db").stat().st_mtime, reverse=True)


def _read_varint(data: bytes, position: int) -> tuple[int, int]:
    value = shift = 0
    while position < len(data) and shift < 64:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7
    raise ValueError("invalid varint")


def _text_segment(segment: bytes) -> str | None:
    if not segment or b"\x00" in segment:
        return None
    try:
        text = segment.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = "".join(char if char.isprintable() or char in "\n\t" else " " for char in text).strip()
    if len(text) < 2 or re.fullmatch(r"[0-9a-fA-F]{32,}", text):
        return None
    return text


def _protobuf_strings(data: bytes, depth: int = 0) -> list[str]:
    if depth > 4 or not data:
        return []
    position = fields = 0
    output: list[str] = []
    try:
        while position < len(data):
            tag, position = _read_varint(data, position)
            if tag == 0:
                return []
            wire = tag & 7
            fields += 1
            if wire == 0:
                _, position = _read_varint(data, position)
            elif wire == 1:
                position += 8
            elif wire == 5:
                position += 4
            elif wire == 2:
                length, position = _read_varint(data, position)
                if position + length > len(data):
                    return []
                segment = data[position : position + length]
                position += length
                nested = _protobuf_strings(segment, depth + 1)
                if nested:
                    output.extend(nested)
                else:
                    text = _text_segment(segment)
                    if text:
                        output.append(text)
            else:
                return []
    except (IndexError, ValueError):
        return []
    return output if fields else []


def decode_message_text(raw: object) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    data = bytes(raw)
    if not data:
        return ""
    control_count = sum(byte < 32 and byte not in (9, 10, 13) for byte in data)
    if control_count == 0:
        try:
            return data.decode("utf-8").strip()
        except UnicodeDecodeError:
            pass
    strings = _protobuf_strings(data)
    unique: list[str] = []
    for text in strings:
        text = text.strip()
        if text and text not in unique:
            unique.append(text)
    return "\n".join(unique[:12])


def _message_datetime(timestamp: object) -> datetime:
    try:
        value = int(timestamp or 0)
        if value > 10_000_000_000:
            value //= 1000
        return datetime.fromtimestamp(value, timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return datetime.now(timezone.utc)


class WindowsWeComLocalDbReceiver:
    """Poll @mentions from the target group without requiring a visible window."""

    def __init__(self, settings: Settings, on_message: Callable[[RawMessage], bool | None]):
        self.settings = settings
        self.on_message = on_message
        self._key: bytes | None = None
        self._pid: int | None = None
        self._data_directory: Path | None = None
        self._client_version = ""
        self._stop = False
        self._seen_message_ids: set[str] = set()

    def _ensure_key(self) -> tuple[Path, bytes]:
        directories = _discover_data_directories()
        if not directories:
            raise WindowsLocalDbError("未找到 Documents\\WXWork 下的消息数据库")
        if self._data_directory in directories and self._key:
            first_page = (self._data_directory / "message.db").read_bytes()[:PAGE_SIZE]
            if _verify_key(self._key, first_page):
                return self._data_directory, self._key
        last_error = ""
        for directory in directories:
            first_page = (directory / "message.db").read_bytes()[:PAGE_SIZE]
            try:
                pid, key, version = _scan_key(first_page)
            except WindowsLocalDbError as exc:
                last_error = str(exc)
                continue
            self._pid = pid
            self._key = key
            self._data_directory = directory
            self._client_version = version
            return directory, key
        raise WindowsLocalDbError(last_error or "本机消息数据库密钥验证失败")

    @staticmethod
    def _snapshot_database(source: Path, key: bytes) -> bytes:
        encrypted = source.read_bytes()
        if len(encrypted) < PAGE_SIZE or not _verify_key(key, encrypted[:PAGE_SIZE]):
            raise WindowsLocalDbError(f"{source.name} 与当前消息库密钥不匹配")
        plain = _decrypt_database_bytes(encrypted, key)
        wal_path = Path(str(source) + "-wal")
        if wal_path.exists():
            try:
                plain = _apply_wal_snapshot(plain, wal_path.read_bytes(), key)
            except (OSError, ValueError, WindowsLocalDbError):
                logger.warning("忽略不完整的 %s 快照；下轮将重试", wal_path.name)
        # The committed WAL frames have already been merged into this private
        # snapshot. Mark the in-memory copy as rollback-journal mode so SQLite
        # does not try to open a sibling -wal file that intentionally does not
        # exist. These bytes belong to the copy, never the WeCom source file.
        if len(plain) >= 20:
            plain[18:20] = b"\x01\x01"
        return bytes(plain)

    def _open_snapshots(self) -> dict[str, sqlite3.Connection]:
        directory, key = self._ensure_key()
        connections: dict[str, sqlite3.Connection] = {}
        try:
            for name in ("message.db", "session.db", "user.db"):
                snapshot = self._snapshot_database(directory / name, key)
                connection = sqlite3.connect(":memory:")
                if not hasattr(connection, "deserialize"):
                    connection.close()
                    raise WindowsLocalDbError("当前 Python SQLite 不支持内存数据库快照")
                connection.deserialize(snapshot)
                connection.row_factory = sqlite3.Row
                connections[name] = connection
            return connections
        except Exception:
            for connection in connections.values():
                connection.close()
            raise

    def _target_rows(
        self, connections: dict[str, sqlite3.Connection]
    ) -> tuple[str, list[sqlite3.Row], dict[str, str]]:
        messages = connections["message.db"]
        sessions = connections["session.db"]
        users_db = connections["user.db"]
        group_rows = sessions.execute(
            "SELECT id FROM conversation_table WHERE name=?", (self.settings.target_group_name,)
        ).fetchall()
        if not group_rows:
            raise WindowsLocalDbError(f"本地会话中未找到群：{self.settings.target_group_name}")
        if len(group_rows) > 1:
            raise WindowsLocalDbError(f"存在多个同名群：{self.settings.target_group_name}")
        conversation_id = str(group_rows[0]["id"])
        names: dict[str, str] = {}
        for row in users_db.execute("SELECT id, name, real_name FROM user_table"):
            name = row["real_name"] or row["name"]
            if name:
                names[str(row["id"])] = str(name)
        for row in sessions.execute(
            "SELECT user_id, nick_name FROM conversation_user_table WHERE conversation_id=?",
            (conversation_id,),
        ):
            if row["nick_name"]:
                names.setdefault(str(row["user_id"]), str(row["nick_name"]))
        rows = messages.execute(
            "SELECT message_id, server_id, sequence, sender_id, conversation_id, "
            "content_type, send_time, content FROM message_table "
            "WHERE conversation_id=? ORDER BY send_time, sequence, message_id",
            (conversation_id,),
        ).fetchall()
        return conversation_id, rows, names

    def _raw_messages(
        self, conversation_id: str, rows: list[sqlite3.Row], names: dict[str, str]
    ) -> list[RawMessage]:
        decoded = [decode_message_text(row["content"]) for row in rows]
        output: list[RawMessage] = []
        window = max(0, self.settings.context_window_seconds)
        for index, (row, content) in enumerate(zip(rows, decoded)):
            if not content or not mentions_target(content, self.settings.target_account_names):
                continue
            timestamp = _message_datetime(row["send_time"])
            context: list[dict[str, object]] = []
            for nearby_row, nearby_content in zip(rows, decoded):
                if not nearby_content:
                    continue
                nearby_time = _message_datetime(nearby_row["send_time"])
                if abs((nearby_time - timestamp).total_seconds()) <= window:
                    nearby_sender = str(nearby_row["sender_id"])
                    context.append(
                        {
                            "message_id": str(nearby_row["message_id"]),
                            "sender": names.get(nearby_sender, nearby_sender),
                            "created_at": nearby_time.isoformat(),
                            "content": nearby_content,
                        }
                    )
            sender_id = str(row["sender_id"])
            stable_id = row["server_id"] or row["message_id"]
            content_type = int(row["content_type"] or 0)
            output.append(
                RawMessage(
                    message_id=f"local-{stable_id}",
                    seq=int(row["sequence"] or row["message_id"] or index),
                    account_id=sender_id,
                    room_id=conversation_id,
                    group_name=self.settings.target_group_name,
                    group_remark=self.settings.target_group_remark,
                    sender_id=sender_id,
                    sender_name=names.get(sender_id, sender_id),
                    message_type="text" if content_type in (0, 2) else f"type-{content_type}",
                    raw_content=content,
                    content=content,
                    mentioned_account=True,
                    created_at=timestamp,
                    payload={
                        "source": "windows_local_db",
                        "content_type": content_type,
                        "context": context,
                    },
                )
            )
        return output

    def poll_once(self) -> int:
        accepted = 0
        connections = self._open_snapshots()
        try:
            conversation_id, rows, names = self._target_rows(connections)
            for message in self._raw_messages(conversation_id, rows, names):
                if message.message_id in self._seen_message_ids:
                    continue
                if self.on_message(message) is not False:
                    accepted += 1
                self._seen_message_ids.add(message.message_id)
        finally:
            for connection in connections.values():
                connection.close()
        return accepted

    def diagnose(self) -> LocalDbDiagnostic:
        process_found = bool(_wxwork_processes()) if os.name == "nt" else False
        database_found = bool(_discover_data_directories()) if os.name == "nt" else False
        try:
            connections = self._open_snapshots()
            try:
                conversation_id, rows, names = self._target_rows(connections)
                mentions = self._raw_messages(conversation_id, rows, names)
            finally:
                for connection in connections.values():
                    connection.close()
            return LocalDbDiagnostic(
                ready=True,
                process_found=process_found,
                database_found=database_found,
                key_verified=True,
                group_found=True,
                client_version=self._client_version,
                message_count=len(rows),
                mention_count=len(mentions),
            )
        except Exception as exc:
            return LocalDbDiagnostic(
                ready=False,
                process_found=process_found,
                database_found=database_found,
                key_verified=bool(self._key),
                group_found=False,
                client_version=self._client_version,
                error=str(exc),
            )

    def run_forever(self, poll_seconds: float | None = None) -> None:
        self._stop = False
        interval = max(2.0, poll_seconds or self.settings.poll_interval_seconds)
        logger.info("Windows local database receiver started; interval=%ss", interval)
        while not self._stop:
            try:
                accepted = self.poll_once()
                if accepted:
                    logger.info("local database receiver processed %s mention(s)", accepted)
            except Exception:
                logger.exception("local database receiver poll failed; will retry")
            time.sleep(interval)

    def stop(self) -> None:
        self._stop = True
