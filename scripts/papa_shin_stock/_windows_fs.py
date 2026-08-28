"""Small Win32 handle layer for security-sensitive cache filesystem actions.

The module imports on non-Windows hosts so its policy can be unit-tested with an
in-memory API.  ``Kernel32Api`` itself is instantiated only on native Windows.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import ntpath
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from types import TracebackType
from typing import Any, Protocol


GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
DELETE = 0x00010000
FILE_READ_ATTRIBUTES = 0x00000080
FILE_WRITE_ATTRIBUTES = 0x00000100
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
CREATE_NEW = 1
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

_FILE_STANDARD_INFO = 1
_FILE_ATTRIBUTE_TAG_INFO = 9
_FILE_ID_BOTH_DIRECTORY_INFO = 10
_FILE_ID_BOTH_DIRECTORY_RESTART_INFO = 11
_FILE_ID_INFO = 18
_FILE_DISPOSITION_INFO = 4
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_FILE_OPEN = 1
_FILE_CREATE = 2
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_OPEN_REPARSE_POINT = 0x00200000
_SYNCHRONIZE = 0x00100000
_OBJ_CASE_INSENSITIVE = 0x00000040
_ERROR_NO_MORE_FILES = 18
_ERROR_HANDLE_EOF = 38
_DIRECTORY_QUERY_BUFFER_BYTES = 64 * 1024
_MAX_DIRECTORY_QUERY_CALLS = 64
_MAX_DIRECTORY_ENTRIES = 4096
_FILE_RENAME_REPLACE_IF_EXISTS = 0x00000001
_FILE_RENAME_POSIX_SEMANTICS = 0x00000002
# Native FILE_INFORMATION_CLASS values, not FILE_INFO_BY_HANDLE_CLASS values.
_FILE_RENAME_INFORMATION = 10
_FILE_RENAME_INFORMATION_EX = 65


class WindowsFilesystemError(OSError):
    """A Win32 object failed a fail-closed identity or type check."""


@dataclass(frozen=True, slots=True)
class WindowsIdentity:
    volume: int
    file_id: int


@dataclass(frozen=True, slots=True)
class MarkerEvidence:
    root_identity: WindowsIdentity
    marker_identity: WindowsIdentity
    payload: bytes


class _Api(Protocol):
    def create_file(
        self, path: str, access: int, share: int, creation: int, flags: int
    ) -> int: ...

    def close(self, handle: int) -> None: ...
    def create_child(
        self,
        parent_handle: int,
        name: str,
        access: int,
        share: int,
        creation: int,
        flags: int,
    ) -> int: ...
    def identity(self, handle: int) -> tuple[int, int]: ...
    def attributes(self, handle: int) -> tuple[bool, bool]: ...
    def link_count(self, handle: int) -> int: ...
    def size(self, handle: int) -> int: ...
    def last_write_time(self, handle: int) -> float: ...
    def final_path(self, handle: int) -> str: ...
    def long_path(self, path: str) -> str: ...
    def list_directory_handle(self, handle: int) -> list[str]: ...
    def rename_relative(
        self,
        handle: int,
        parent_handle: int,
        destination_name: str,
        *,
        replace: bool,
    ) -> None: ...
    def dispose(self, handle: int) -> None: ...
    def read(self, handle: int, maximum: int) -> bytes: ...
    def write(self, handle: int, payload: bytes) -> None: ...
    def touch(self, handle: int) -> None: ...
    def flush(self, handle: int) -> None: ...


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]


class _FILE_ID_INFO_STRUCT(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", _FILE_ID_128),
    ]


class _FILE_ATTRIBUTE_TAG_INFO_STRUCT(ctypes.Structure):
    _fields_ = [("FileAttributes", ctypes.c_ulong), ("ReparseTag", ctypes.c_ulong)]


class _FILE_STANDARD_INFO_STRUCT(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("NumberOfLinks", ctypes.c_ulong),
        ("DeletePending", ctypes.c_ubyte),
        ("Directory", ctypes.c_ubyte),
    ]


class _FILE_ID_BOTH_DIR_INFO_STRUCT(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wintypes.DWORD),
        ("FileIndex", wintypes.DWORD),
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("AllocationSize", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
        ("FileNameLength", wintypes.DWORD),
        ("EaSize", wintypes.DWORD),
        ("ShortNameLength", ctypes.c_byte),
        ("ShortName", ctypes.c_wchar * 12),
        ("FileId", ctypes.c_longlong),
        ("FileName", ctypes.c_wchar * 1),
    ]


class _FILE_BASIC_INFO_STRUCT(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
    ]


class _FILE_DISPOSITION_INFO_STRUCT(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


class _FILE_RENAME_INFORMATION_HEADER(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_ubyte),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", ctypes.c_uint16 * 1),
    ]


class _FILE_RENAME_INFORMATION_EX_HEADER(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", ctypes.c_uint16 * 1),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [
        ("Status", ctypes.c_void_p),
        ("Information", ctypes.c_size_t),
    ]


class Kernel32Api:
    """Direct, dependency-free binding for the small Win32 API surface used here."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsFilesystemError("Win32 backend is only available on Windows")
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise WindowsFilesystemError("WinDLL is unavailable")
        self.kernel32 = loader("kernel32", use_last_error=True)
        self.ntdll = loader("ntdll", use_last_error=True)
        self._bind()

    def _bind(self) -> None:
        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self.kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self.kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self.kernel32.GetLongPathNameW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        self.kernel32.GetLongPathNameW.restype = wintypes.DWORD
        self.kernel32.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        self.kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self.kernel32.ReadFile.restype = wintypes.BOOL
        self.kernel32.WriteFile.argtypes = self.kernel32.ReadFile.argtypes
        self.kernel32.WriteFile.restype = wintypes.BOOL
        self.kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self.kernel32.FlushFileBuffers.restype = wintypes.BOOL
        self.kernel32.GetSystemTimeAsFileTime.argtypes = [
            ctypes.POINTER(wintypes.FILETIME)
        ]
        self.kernel32.GetSystemTimeAsFileTime.restype = None
        self.kernel32.SetFileTime.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        self.kernel32.SetFileTime.restype = wintypes.BOOL
        self.ntdll.NtCreateFile.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            ctypes.POINTER(_OBJECT_ATTRIBUTES),
            ctypes.POINTER(_IO_STATUS_BLOCK),
            ctypes.c_void_p,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
        ]
        self.ntdll.NtCreateFile.restype = wintypes.LONG
        self.ntdll.NtSetInformationFile.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_IO_STATUS_BLOCK),
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_int,
        ]
        self.ntdll.NtSetInformationFile.restype = wintypes.LONG
        self.ntdll.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
        self.ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

    @staticmethod
    def _raise_code(code: int, message: str) -> None:
        if code in {2, 3}:
            raise FileNotFoundError(code, message)
        if code in {80, 183}:
            raise FileExistsError(code, message)
        if code == 5:
            raise PermissionError(code, message)
        raise WindowsFilesystemError(code, message)

    @classmethod
    def _raise(cls, message: str) -> None:
        cls._raise_code(ctypes.get_last_error(), message)

    def create_file(
        self, path: str, access: int, share: int, creation: int, flags: int
    ) -> int:
        handle = self.kernel32.CreateFileW(
            path, access, share, None, creation, flags, None
        )
        numeric = int(handle)
        if numeric == _INVALID_HANDLE_VALUE:
            self._raise("CreateFileW failed")
        return numeric

    def close(self, handle: int) -> None:
        if not self.kernel32.CloseHandle(handle):
            self._raise("CloseHandle failed")

    def create_child(
        self,
        parent_handle: int,
        name: str,
        access: int,
        share: int,
        creation: int,
        flags: int,
    ) -> int:
        name_buffer = ctypes.create_unicode_buffer(name)
        encoded_length = len(name.encode("utf-16-le"))
        object_name = _UNICODE_STRING(
            encoded_length,
            encoded_length + ctypes.sizeof(ctypes.c_wchar),
            ctypes.cast(name_buffer, wintypes.LPWSTR),
        )
        attributes = _OBJECT_ATTRIBUTES(
            ctypes.sizeof(_OBJECT_ATTRIBUTES),
            parent_handle,
            ctypes.pointer(object_name),
            _OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        output = wintypes.HANDLE()
        io_status = _IO_STATUS_BLOCK()
        if creation == CREATE_NEW:
            disposition = _FILE_CREATE
        elif creation == OPEN_EXISTING:
            disposition = _FILE_OPEN
        else:
            raise WindowsFilesystemError("unsupported relative creation mode")
        options = (
            _FILE_SYNCHRONOUS_IO_NONALERT
            | _FILE_OPEN_REPARSE_POINT
            | (
                _FILE_DIRECTORY_FILE
                if flags & FILE_FLAG_BACKUP_SEMANTICS
                else _FILE_NON_DIRECTORY_FILE
            )
        )
        status = self.ntdll.NtCreateFile(
            ctypes.byref(output),
            access | _SYNCHRONIZE,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            FILE_ATTRIBUTE_NORMAL,
            share,
            disposition,
            options,
            None,
            0,
        )
        if status < 0:
            code = int(self.ntdll.RtlNtStatusToDosError(status))
            self._raise_code(code, "NtCreateFile failed")
        if output.value is None:
            raise WindowsFilesystemError("NtCreateFile returned no handle")
        return int(output.value)

    def _info(self, handle: int, kind: int, structure: Any) -> Any:
        value = structure()
        if not self.kernel32.GetFileInformationByHandleEx(
            handle, kind, ctypes.byref(value), ctypes.sizeof(value)
        ):
            self._raise("GetFileInformationByHandleEx failed")
        return value

    def identity(self, handle: int) -> tuple[int, int]:
        value = self._info(handle, _FILE_ID_INFO, _FILE_ID_INFO_STRUCT)
        return (
            int(value.VolumeSerialNumber),
            int.from_bytes(bytes(value.FileId.Identifier), "little"),
        )

    def attributes(self, handle: int) -> tuple[bool, bool]:
        value = self._info(
            handle, _FILE_ATTRIBUTE_TAG_INFO, _FILE_ATTRIBUTE_TAG_INFO_STRUCT
        )
        return (
            bool(value.FileAttributes & FILE_ATTRIBUTE_DIRECTORY),
            bool(value.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT),
        )

    def link_count(self, handle: int) -> int:
        value = self._info(handle, _FILE_STANDARD_INFO, _FILE_STANDARD_INFO_STRUCT)
        return int(value.NumberOfLinks)

    def size(self, handle: int) -> int:
        value = self._info(handle, _FILE_STANDARD_INFO, _FILE_STANDARD_INFO_STRUCT)
        return int(value.EndOfFile)

    def last_write_time(self, handle: int) -> float:
        value = self._info(handle, 0, _FILE_BASIC_INFO_STRUCT)
        return float(value.LastWriteTime) / 10_000_000.0 - 11_644_473_600.0

    def final_path(self, handle: int) -> str:
        size = self.kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not size:
            self._raise("GetFinalPathNameByHandleW failed")
        buffer = ctypes.create_unicode_buffer(size + 1)
        written = self.kernel32.GetFinalPathNameByHandleW(
            handle, buffer, len(buffer), 0
        )
        if not written or written >= len(buffer):
            self._raise("GetFinalPathNameByHandleW failed")
        return buffer.value

    def long_path(self, path: str) -> str:
        size = self.kernel32.GetLongPathNameW(path, None, 0)
        if not size:
            self._raise("GetLongPathNameW failed")
        buffer = ctypes.create_unicode_buffer(size + 1)
        written = self.kernel32.GetLongPathNameW(path, buffer, len(buffer))
        if not written or written >= len(buffer):
            self._raise("GetLongPathNameW failed")
        return buffer.value

    def list_directory_handle(self, handle: int) -> list[str]:
        names: list[str] = []
        information_class = _FILE_ID_BOTH_DIRECTORY_RESTART_INFO
        for _ in range(_MAX_DIRECTORY_QUERY_CALLS):
            buffer = ctypes.create_string_buffer(_DIRECTORY_QUERY_BUFFER_BYTES)
            if not self.kernel32.GetFileInformationByHandleEx(
                handle,
                information_class,
                buffer,
                len(buffer),
            ):
                code = ctypes.get_last_error()
                if code in {_ERROR_NO_MORE_FILES, _ERROR_HANDLE_EOF}:
                    return names
                raise WindowsFilesystemError(
                    code, "directory handle enumeration failed"
                )
            information_class = _FILE_ID_BOTH_DIRECTORY_INFO
            offset = 0
            while True:
                minimum = _FILE_ID_BOTH_DIR_INFO_STRUCT.FileName.offset
                if offset < 0 or offset + minimum > len(buffer):
                    raise WindowsFilesystemError("invalid directory inventory")
                entry = _FILE_ID_BOTH_DIR_INFO_STRUCT.from_buffer(buffer, offset)
                name_length = int(entry.FileNameLength)
                if (
                    name_length < 0
                    or name_length % ctypes.sizeof(ctypes.c_wchar) != 0
                    or offset + minimum + name_length > len(buffer)
                ):
                    raise WindowsFilesystemError("invalid directory inventory")
                name = ctypes.wstring_at(
                    ctypes.addressof(buffer) + offset + minimum,
                    name_length // ctypes.sizeof(ctypes.c_wchar),
                )
                if name not in {".", ".."}:
                    if not name or PureWindowsPath(name).name != name:
                        raise WindowsFilesystemError("unsafe directory entry")
                    names.append(name)
                    if len(names) > _MAX_DIRECTORY_ENTRIES:
                        raise WindowsFilesystemError(
                            "directory inventory exceeds bound"
                        )
                next_offset = int(entry.NextEntryOffset)
                if next_offset == 0:
                    break
                if next_offset < minimum or next_offset % 8 != 0:
                    raise WindowsFilesystemError("invalid directory inventory")
                offset += next_offset
        raise WindowsFilesystemError("directory enumeration did not terminate")

    def rename_relative(
        self,
        handle: int,
        parent_handle: int,
        destination_name: str,
        *,
        replace: bool,
    ) -> None:
        encoded = destination_name.encode("utf-16-le")
        header_type: type[ctypes.Structure]
        information_class: int
        if replace:
            header_type = _FILE_RENAME_INFORMATION_EX_HEADER
            information_class = _FILE_RENAME_INFORMATION_EX
        else:
            header_type = _FILE_RENAME_INFORMATION_HEADER
            information_class = _FILE_RENAME_INFORMATION
        name_offset = header_type.FileName.offset
        # NtSetInformationFile expects the complete fixed header in addition to
        # the variable-length UTF-16 name, including the structure's tail
        # padding.  Using only ``FileName.offset`` produces an undersized x64
        # record and is rejected with STATUS_INVALID_PARAMETER.
        buffer = ctypes.create_string_buffer(ctypes.sizeof(header_type) + len(encoded))
        header = header_type.from_buffer(buffer)
        if replace:
            header.Flags = (
                _FILE_RENAME_REPLACE_IF_EXISTS | _FILE_RENAME_POSIX_SEMANTICS
            )
        else:
            header.ReplaceIfExists = 0
        header.RootDirectory = parent_handle
        header.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded, len(encoded))
        io_status = _IO_STATUS_BLOCK()
        status = self.ntdll.NtSetInformationFile(
            handle,
            ctypes.byref(io_status),
            buffer,
            len(buffer),
            information_class,
        )
        if status < 0:
            code = int(self.ntdll.RtlNtStatusToDosError(status))
            self._raise_code(code, "relative handle rename failed")

    def dispose(self, handle: int) -> None:
        value = _FILE_DISPOSITION_INFO_STRUCT(1)
        if not self.kernel32.SetFileInformationByHandle(
            handle,
            _FILE_DISPOSITION_INFO,
            ctypes.byref(value),
            ctypes.sizeof(value),
        ):
            self._raise("handle disposition failed")

    def read(self, handle: int, maximum: int) -> bytes:
        buffer = ctypes.create_string_buffer(maximum)
        received = wintypes.DWORD()
        if not self.kernel32.ReadFile(
            handle, buffer, maximum, ctypes.byref(received), None
        ):
            self._raise("ReadFile failed")
        return buffer.raw[: received.value]

    def write(self, handle: int, payload: bytes) -> None:
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(payload)
        if not self.kernel32.WriteFile(
            handle, buffer, len(payload), ctypes.byref(written), None
        ) or written.value != len(payload):
            self._raise("WriteFile failed")

    def touch(self, handle: int) -> None:
        timestamp = wintypes.FILETIME()
        self.kernel32.GetSystemTimeAsFileTime(ctypes.byref(timestamp))
        if not self.kernel32.SetFileTime(
            handle, None, None, ctypes.byref(timestamp)
        ):
            self._raise("SetFileTime failed")

    def flush(self, handle: int) -> None:
        if not self.kernel32.FlushFileBuffers(handle):
            self._raise("FlushFileBuffers failed")


class VerifiedHandle:
    def __init__(
        self,
        api: _Api,
        handle: int,
        path: str,
        identity: WindowsIdentity,
        directory: bool,
    ) -> None:
        self.api = api
        self.handle = handle
        self.path = path
        self.identity = identity
        self.directory = directory

    def close(self) -> None:
        handle = self.handle
        self.handle = -1
        if handle >= 0:
            self.api.close(handle)

    def rebind(self, path: str) -> None:
        self.path = path

    def assert_current(self) -> None:
        if self.handle < 0:
            raise WindowsFilesystemError("Win32 handle is closed")
        identity = WindowsIdentity(*self.api.identity(self.handle))
        is_directory, is_reparse = self.api.attributes(self.handle)
        if (
            identity != self.identity
            or is_directory != self.directory
            or is_reparse
        ):
            raise WindowsFilesystemError("Win32 handle identity changed")
        if not self.directory and self.api.link_count(self.handle) != 1:
            raise WindowsFilesystemError("hard-linked file rejected")
        if not WindowsFilesystem._final_path_matches(
            self.api, self.handle, self.path
        ):
            raise WindowsFilesystemError("Win32 final path changed")

    def __enter__(self) -> "VerifiedHandle":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class WindowsCacheRoot:
    """Retained root and marker handles for one cache filesystem operation."""

    def __init__(
        self,
        filesystem: "WindowsFilesystem",
        root: VerifiedHandle,
        marker: VerifiedHandle,
        marker_name: str,
        marker_evidence: MarkerEvidence,
        maximum: int,
    ) -> None:
        self.filesystem = filesystem
        self.root = root
        self.marker = marker
        self.marker_name = marker_name
        self.marker_evidence = marker_evidence
        self.maximum = maximum

    def assert_current(self) -> None:
        self.root.assert_current()
        self.marker.assert_current()
        with self.filesystem.open_child(
            self.root,
            self.marker_name,
            directory=False,
            expected=self.marker_evidence.marker_identity,
            pin_namespace=True,
            immutable=True,
        ) as marker:
            payload = self.filesystem._read_open_file(marker, self.maximum)
        if payload != self.marker_evidence.payload:
            raise WindowsFilesystemError("cache root marker changed")
        self.root.assert_current()
        self.marker.assert_current()

    def _open_directory_chain(
        self,
        parts: tuple[str, ...],
        *,
        writable: bool = False,
    ) -> tuple[VerifiedHandle, list[VerifiedHandle]]:
        self.assert_current()
        current = self.root
        opened: list[VerifiedHandle] = []
        try:
            for name in parts:
                child = self.filesystem.open_child(
                    current,
                    name,
                    directory=True,
                    writable=writable,
                    pin_namespace=True,
                )
                opened.append(child)
                current = child
            return current, opened
        except BaseException:
            for handle in reversed(opened):
                handle.close()
            raise

    @staticmethod
    def _close_directory_chain(opened: list[VerifiedHandle]) -> None:
        for handle in reversed(opened):
            handle.close()

    def ensure_directory(self, parts: tuple[str, ...]) -> WindowsIdentity:
        self.assert_current()
        current = self.root
        opened: list[VerifiedHandle] = []
        try:
            for name in parts:
                try:
                    child = self.filesystem.open_child(
                        current,
                        name,
                        directory=True,
                        writable=True,
                        pin_namespace=True,
                    )
                except OSError as error:
                    if not self.filesystem._is_missing_error(error):
                        raise
                    try:
                        child = self.filesystem.open_child(
                            current,
                            name,
                            directory=True,
                            writable=True,
                            pin_namespace=True,
                            creation=CREATE_NEW,
                        )
                    except OSError as error:
                        if not self.filesystem._is_exists_error(error):
                            raise
                        child = self.filesystem.open_child(
                            current,
                            name,
                            directory=True,
                            writable=True,
                            pin_namespace=True,
                        )
                    self.filesystem.api.flush(current.handle)
                opened.append(child)
                current = child
            current.assert_current()
            result = current.identity
            self.assert_current()
            return result
        finally:
            self._close_directory_chain(opened)

    def create_directory(
        self, parent_parts: tuple[str, ...], name: str
    ) -> WindowsIdentity:
        parent, opened = self._open_directory_chain(
            parent_parts, writable=True
        )
        try:
            with self.filesystem.open_child(
                parent,
                name,
                directory=True,
                writable=True,
                pin_namespace=True,
                creation=CREATE_NEW,
            ) as created:
                self.filesystem.api.flush(created.handle)
                identity = created.identity
            self.filesystem.api.flush(parent.handle)
            self.assert_current()
            return identity
        finally:
            self._close_directory_chain(opened)

    def list_directory(self, parts: tuple[str, ...]) -> list[str]:
        directory, opened = self._open_directory_chain(parts)
        try:
            names = sorted(
                self.filesystem.api.list_directory_handle(directory.handle)
            )
            directory.assert_current()
            self.assert_current()
            return names
        finally:
            self._close_directory_chain(opened)

    def directory_identity(self, parts: tuple[str, ...]) -> WindowsIdentity:
        directory, opened = self._open_directory_chain(parts)
        try:
            directory.assert_current()
            identity = directory.identity
            self.assert_current()
            return identity
        finally:
            self._close_directory_chain(opened)

    def file_identity(
        self, parent_parts: tuple[str, ...], name: str
    ) -> WindowsIdentity:
        parent, opened = self._open_directory_chain(parent_parts)
        try:
            with self.filesystem.open_child(
                parent,
                name,
                directory=False,
                pin_namespace=True,
                immutable=True,
            ) as entry:
                entry.assert_current()
                identity = entry.identity
            self.assert_current()
            return identity
        finally:
            self._close_directory_chain(opened)

    def snapshot_flat_directory(
        self, parts: tuple[str, ...]
    ) -> dict[str, WindowsIdentity]:
        directory, opened = self._open_directory_chain(parts)
        try:
            names = self.filesystem.api.list_directory_handle(directory.handle)
            inventory: dict[str, WindowsIdentity] = {}
            for name in names:
                with self.filesystem.open_child(
                    directory,
                    name,
                    directory=False,
                    pin_namespace=True,
                    immutable=True,
                ) as entry:
                    inventory[name] = entry.identity
            repeated = self.filesystem.api.list_directory_handle(
                directory.handle
            )
            if len(repeated) != len(names) or set(repeated) != set(names):
                raise WindowsFilesystemError("directory inventory changed")
            directory.assert_current()
            self.assert_current()
            return inventory
        finally:
            self._close_directory_chain(opened)

    def read_file(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        maximum: int,
    ) -> bytes:
        parent, opened = self._open_directory_chain(parent_parts)
        try:
            with self.filesystem.open_child(
                parent,
                name,
                directory=False,
                pin_namespace=True,
                immutable=True,
            ) as source:
                payload = self.filesystem._read_open_file(source, maximum)
                source.assert_current()
            self.assert_current()
            return payload
        finally:
            self._close_directory_chain(opened)

    def read_optional_file(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        maximum: int,
    ) -> bytes | None:
        try:
            return self.read_file(parent_parts, name, maximum)
        except OSError as error:
            if self.filesystem._is_missing_error(error):
                return None
            raise

    def write_new_file(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        payload: bytes,
    ) -> WindowsIdentity:
        parent, opened = self._open_directory_chain(
            parent_parts, writable=True
        )
        try:
            identity = self.filesystem.replace_file_cas(
                parent, name, expected=None, payload=payload
            )
            self.assert_current()
            return identity
        finally:
            self._close_directory_chain(opened)

    def replace_file_cas(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        *,
        expected: bytes | None,
        payload: bytes,
    ) -> WindowsIdentity:
        parent, opened = self._open_directory_chain(
            parent_parts, writable=True
        )
        try:
            identity = self.filesystem.replace_file_cas(
                parent, name, expected=expected, payload=payload
            )
            self.assert_current()
            return identity
        finally:
            self._close_directory_chain(opened)

    def delete_file_cas(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        expected: bytes,
    ) -> None:
        parent, opened = self._open_directory_chain(
            parent_parts, writable=True
        )
        try:
            self.filesystem.delete_file_cas(parent, name, expected)
            self.assert_current()
        finally:
            self._close_directory_chain(opened)

    def delete_file_identity(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        expected: WindowsIdentity,
    ) -> None:
        parent, opened = self._open_directory_chain(
            parent_parts, writable=True
        )
        try:
            self.filesystem.delete_file_identity(parent, name, expected)
            self.assert_current()
        finally:
            self._close_directory_chain(opened)

    def last_write_time(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        *,
        directory: bool,
        expected: WindowsIdentity | None = None,
    ) -> float:
        parent, opened = self._open_directory_chain(parent_parts)
        try:
            with self.filesystem.open_child(
                parent,
                name,
                directory=directory,
                expected=expected,
                pin_namespace=True,
                immutable=not directory,
            ) as entry:
                value = self.filesystem.api.last_write_time(entry.handle)
                entry.assert_current()
            self.assert_current()
            return value
        finally:
            self._close_directory_chain(opened)

    def touch_file(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        expected: WindowsIdentity,
    ) -> None:
        parent, opened = self._open_directory_chain(
            parent_parts, writable=True
        )
        try:
            with self.filesystem.open_child(
                parent,
                name,
                directory=False,
                destructive=True,
                expected=expected,
                writable=True,
            ) as entry:
                self.filesystem.api.touch(entry.handle)
                self.filesystem.api.flush(entry.handle)
                entry.assert_current()
            self.filesystem.api.flush(parent.handle)
            self.assert_current()
        finally:
            self._close_directory_chain(opened)

    def verify_file(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        *,
        expected_bytes: int,
        expected_sha256: str,
        progress: Callable[[], None] | None = None,
    ) -> None:
        parent, opened = self._open_directory_chain(parent_parts)
        try:
            with self.filesystem.open_child(
                parent,
                name,
                directory=False,
                pin_namespace=True,
                immutable=True,
            ) as source:
                actual_size = self.filesystem.api.size(source.handle)
                if actual_size != expected_bytes:
                    raise WindowsFilesystemError("Win32 file size changed")
                digest = hashlib.sha256()
                remaining = expected_bytes
                while remaining:
                    chunk = self.filesystem.api.read(
                        source.handle, min(1024 * 1024, remaining)
                    )
                    if not chunk:
                        raise WindowsFilesystemError("short Win32 file read")
                    digest.update(chunk)
                    remaining -= len(chunk)
                    if progress is not None:
                        progress()
                if (
                    remaining != 0
                    or digest.hexdigest().lower() != expected_sha256.lower()
                ):
                    raise WindowsFilesystemError("Win32 file checksum changed")
                source.assert_current()
            self.assert_current()
        finally:
            self._close_directory_chain(opened)

    def rename_directory(
        self,
        parent_parts: tuple[str, ...],
        source_name: str,
        expected: WindowsIdentity,
        destination_name: str,
    ) -> None:
        parent, opened = self._open_directory_chain(
            parent_parts, writable=True
        )
        try:
            self.filesystem.rename_directory(
                parent, source_name, expected, destination_name
            )
            self.assert_current()
        finally:
            self._close_directory_chain(opened)

    def delete_flat_directory(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        expected: WindowsIdentity,
        *,
        quarantine_name: str,
        expected_inventory: dict[str, WindowsIdentity] | None = None,
        expected_payloads: dict[str, bytes] | None = None,
        expected_write_times: dict[str, float] | None = None,
    ) -> bool:
        parent, opened = self._open_directory_chain(
            parent_parts, writable=True
        )
        try:
            removed = self.filesystem.delete_flat_directory_handle(
                parent,
                name,
                expected,
                quarantine_name=quarantine_name,
                expected_inventory=expected_inventory,
                expected_payloads=expected_payloads,
                expected_write_times=expected_write_times,
            )
            self.assert_current()
            return removed
        finally:
            self._close_directory_chain(opened)

    def create_download_destination(
        self,
        parent_parts: tuple[str, ...],
        name: str,
    ) -> "WindowsDownloadDestination":
        parent, opened = self._open_directory_chain(
            parent_parts, writable=True
        )
        try:
            file_handle = self.filesystem.open_child(
                parent,
                name,
                directory=False,
                destructive=True,
                writable=True,
                creation=CREATE_NEW,
            )
        except BaseException:
            self._close_directory_chain(opened)
            raise
        return WindowsDownloadDestination(
            self,
            parent_parts,
            parent,
            opened,
            file_handle,
            name,
        )

    def close(self) -> None:
        marker = self.marker
        root = self.root
        self.marker = VerifiedHandle(
            marker.api, -1, marker.path, marker.identity, marker.directory
        )
        self.root = VerifiedHandle(
            root.api, -1, root.path, root.identity, root.directory
        )
        try:
            marker.close()
        finally:
            root.close()

    def __enter__(self) -> "WindowsCacheRoot":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class _WindowsHandleWriter:
    def __init__(self, destination: "WindowsDownloadDestination") -> None:
        self.destination = destination

    def write(self, payload: bytes) -> int:
        if not isinstance(payload, bytes):
            payload = bytes(payload)
        self.destination._assert_open()
        self.destination.session.filesystem.api.write(
            self.destination.file.handle, payload
        )
        return len(payload)

    def flush(self) -> None:
        self.destination._assert_open()

    def __enter__(self) -> "_WindowsHandleWriter":
        self.destination._assert_open()
        if self.destination.writer_active:
            raise WindowsFilesystemError("download writer is already active")
        self.destination.writer_active = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.destination.writer_active = False


class WindowsDownloadDestination:
    def __init__(
        self,
        session: WindowsCacheRoot,
        parent_parts: tuple[str, ...],
        parent: VerifiedHandle,
        opened: list[VerifiedHandle],
        file_handle: VerifiedHandle,
        name: str,
    ) -> None:
        self.session = session
        self.parent_parts = parent_parts
        self.parent = parent
        self.opened = opened
        self.file = file_handle
        self.path = Path(session.root.path).joinpath(*parent_parts, name)
        self.writer_active = False
        self.deleted = False
        self.closed = False

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def parents(self) -> Any:
        return self.path.parents

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def _assert_open(self) -> None:
        if self.closed or self.deleted or self.file.handle < 0:
            raise WindowsFilesystemError("download destination is closed")
        self.session.assert_current()
        self.parent.assert_current()
        self.file.assert_current()

    def open(self, mode: str = "r", *args: object, **kwargs: object) -> _WindowsHandleWriter:
        if mode != "wb" or args or kwargs:
            raise WindowsFilesystemError("unsupported download open mode")
        self._assert_open()
        return _WindowsHandleWriter(self)

    def write_bytes(self, payload: bytes) -> int:
        with self.open("wb") as stream:
            return stream.write(payload)

    def fsync(self) -> None:
        self._assert_open()
        self.session.filesystem.api.flush(self.file.handle)
        self.file.assert_current()

    def unlink(self, missing_ok: bool = False) -> None:
        if self.deleted:
            if missing_ok:
                return
            raise WindowsFilesystemError("download destination is missing")
        self._assert_open()
        self.session.filesystem.api.dispose(self.file.handle)
        self.session.filesystem.api.flush(self.parent.handle)
        self.deleted = True

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.file.close()
        finally:
            WindowsCacheRoot._close_directory_chain(self.opened)


class WindowsFilesystem:
    """Fail-closed operations pinned to Win32 handles and file IDs."""

    def __init__(self, api: _Api | None = None) -> None:
        self.api = api if api is not None else Kernel32Api()

    @staticmethod
    def _canonical(path: str | os.PathLike[str]) -> str:
        value = str(path)
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return ntpath.normcase(ntpath.normpath(value))

    @classmethod
    def _final_path_matches(cls, api: _Api, handle: int, requested: str) -> bool:
        return cls._canonical(api.final_path(handle)) == cls._canonical(
            api.long_path(requested)
        )

    @staticmethod
    def _child(parent: str, name: str) -> str:
        if not name or name in {".", ".."} or PureWindowsPath(name).name != name:
            raise WindowsFilesystemError("unsafe child name")
        return str(PureWindowsPath(parent) / name)

    def open_verified(
        self,
        path: str | os.PathLike[str],
        *,
        directory: bool,
        destructive: bool = False,
        expected: WindowsIdentity | None = None,
        creation: int = OPEN_EXISTING,
        writable: bool = False,
        pin_namespace: bool = False,
        immutable: bool = False,
    ) -> VerifiedHandle:
        if writable and immutable:
            raise WindowsFilesystemError("immutable handle cannot be writable")
        requested = str(path)
        access = FILE_READ_ATTRIBUTES | GENERIC_READ
        if destructive:
            access |= DELETE
        if writable:
            access |= GENERIC_WRITE | FILE_WRITE_ATTRIBUTES
        share = FILE_SHARE_READ
        if not immutable:
            share |= FILE_SHARE_WRITE
        if not destructive and not pin_namespace and not immutable:
            share |= FILE_SHARE_DELETE
        flags = FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= FILE_FLAG_BACKUP_SEMANTICS
        handle = self.api.create_file(requested, access, share, creation, flags)
        try:
            identity = WindowsIdentity(*self.api.identity(handle))
            is_directory, is_reparse = self.api.attributes(handle)
            if is_directory != directory or is_reparse:
                raise WindowsFilesystemError("unexpected Win32 object type")
            if not directory and self.api.link_count(handle) != 1:
                raise WindowsFilesystemError("hard-linked file rejected")
            if expected is not None and identity != expected:
                raise WindowsFilesystemError("Win32 file identity changed")
            if not self._final_path_matches(self.api, handle, requested):
                raise WindowsFilesystemError("Win32 final path changed")
            return VerifiedHandle(self.api, handle, requested, identity, directory)
        except BaseException:
            self.api.close(handle)
            raise

    def open_child(
        self,
        parent: VerifiedHandle,
        name: str,
        *,
        directory: bool,
        destructive: bool = False,
        expected: WindowsIdentity | None = None,
        creation: int = OPEN_EXISTING,
        writable: bool = False,
        pin_namespace: bool = False,
        immutable: bool = False,
    ) -> VerifiedHandle:
        if writable and immutable:
            raise WindowsFilesystemError("immutable handle cannot be writable")
        requested = self._child(parent.path, name)
        access = FILE_READ_ATTRIBUTES | GENERIC_READ
        if destructive:
            access |= DELETE
        if writable:
            access |= GENERIC_WRITE | FILE_WRITE_ATTRIBUTES
        share = FILE_SHARE_READ
        if not immutable:
            share |= FILE_SHARE_WRITE
        if not destructive and not pin_namespace and not immutable:
            share |= FILE_SHARE_DELETE
        flags = FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= FILE_FLAG_BACKUP_SEMANTICS
        handle = self.api.create_child(
            parent.handle,
            name,
            access,
            share,
            creation,
            flags,
        )
        try:
            identity = WindowsIdentity(*self.api.identity(handle))
            is_directory, is_reparse = self.api.attributes(handle)
            if is_directory != directory or is_reparse:
                raise WindowsFilesystemError("unexpected Win32 object type")
            if not directory and self.api.link_count(handle) != 1:
                raise WindowsFilesystemError("hard-linked file rejected")
            if expected is not None and identity != expected:
                raise WindowsFilesystemError("Win32 file identity changed")
            if not self._final_path_matches(self.api, handle, requested):
                raise WindowsFilesystemError("Win32 final path changed")
            return VerifiedHandle(
                self.api, handle, requested, identity, directory
            )
        except BaseException:
            self.api.close(handle)
            raise

    def _initialize_marker_handle(
        self,
        root: VerifiedHandle,
        marker_name: str,
        payload: bytes,
        maximum: int,
    ) -> MarkerEvidence:
        if len(payload) > maximum:
            raise WindowsFilesystemError("marker exceeds size limit")
        if self.api.list_directory_handle(root.handle):
            raise WindowsFilesystemError("unmarked cache root is not empty")
        marker_identity: WindowsIdentity | None = None
        try:
            with self.open_child(
                root,
                marker_name,
                directory=False,
                destructive=True,
                creation=CREATE_NEW,
                writable=True,
            ) as marker:
                self.api.write(marker.handle, payload)
                self.api.flush(marker.handle)
                marker_identity = marker.identity
            if self.api.list_directory_handle(root.handle) != [marker_name]:
                raise WindowsFilesystemError(
                    "cache root changed during initialization"
                )
            self.api.flush(root.handle)
            return self._attest_marker_handle(
                root, marker_name, maximum, expected=marker_identity
            )
        except BaseException:
            if marker_identity is not None:
                try:
                    with self.open_child(
                        root,
                        marker_name,
                        directory=False,
                        destructive=True,
                        expected=marker_identity,
                        immutable=True,
                    ) as marker:
                        marker.assert_current()
                        self.api.dispose(marker.handle)
                    self.api.flush(root.handle)
                except OSError:
                    pass
            raise

    def _attest_marker_handle(
        self,
        root: VerifiedHandle,
        marker_name: str,
        maximum: int,
        *,
        expected: WindowsIdentity | None = None,
    ) -> MarkerEvidence:
        root.assert_current()
        with self.open_child(
            root,
            marker_name,
            directory=False,
            expected=expected,
            pin_namespace=True,
            immutable=True,
        ) as marker:
            payload = self._read_open_file(marker, maximum)
            marker_identity = marker.identity
        root.assert_current()
        return MarkerEvidence(root.identity, marker_identity, payload)

    def open_cache_root(
        self,
        root: str,
        marker_name: str,
        *,
        payload: bytes | None,
        maximum: int,
        create: bool,
    ) -> WindowsCacheRoot:
        root_handle: VerifiedHandle | None = None
        marker_handle: VerifiedHandle | None = None
        try:
            try:
                root_handle = self.open_verified(
                    root,
                    directory=True,
                    writable=True,
                    pin_namespace=True,
                )
            except OSError as error:
                if not create or not self._is_missing_error(error):
                    raise
                root_handle = self._create_directory_tree(root)
            names = self.api.list_directory_handle(root_handle.handle)
            if marker_name not in names:
                if not create or payload is None or names:
                    raise WindowsFilesystemError("cache root marker is missing")
                evidence = self._initialize_marker_handle(
                    root_handle, marker_name, payload, maximum
                )
            else:
                evidence = self._attest_marker_handle(
                    root_handle, marker_name, maximum
                )
            marker_handle = self.open_child(
                root_handle,
                marker_name,
                directory=False,
                expected=evidence.marker_identity,
                pin_namespace=True,
                immutable=True,
            )
            result = WindowsCacheRoot(
                self,
                root_handle,
                marker_handle,
                marker_name,
                evidence,
                maximum,
            )
            result.assert_current()
            root_handle = None
            marker_handle = None
            return result
        finally:
            try:
                if marker_handle is not None:
                    marker_handle.close()
            finally:
                if root_handle is not None:
                    root_handle.close()

    def _create_directory_tree(self, root: str) -> VerifiedHandle:
        target = PureWindowsPath(root)
        if not target.is_absolute() or not target.name:
            raise WindowsFilesystemError("cache root must be an absolute leaf")
        missing_names: list[str] = []
        candidate = target
        ancestor: VerifiedHandle | None = None
        while ancestor is None:
            parent = candidate.parent
            if parent == candidate or not candidate.name:
                raise WindowsFilesystemError("cache root parent is unavailable")
            missing_names.append(candidate.name)
            try:
                ancestor = self.open_verified(
                    str(parent),
                    directory=True,
                    writable=True,
                    pin_namespace=True,
                )
            except OSError as error:
                if not self._is_missing_error(error):
                    raise
                candidate = parent
        current = ancestor
        try:
            for name in reversed(missing_names):
                child: VerifiedHandle | None = None
                try:
                    child = self.open_child(
                        current,
                        name,
                        directory=True,
                        writable=True,
                        pin_namespace=True,
                    )
                except OSError as error:
                    if not self._is_missing_error(error):
                        raise
                    try:
                        child = self.open_child(
                            current,
                            name,
                            directory=True,
                            writable=True,
                            pin_namespace=True,
                            creation=CREATE_NEW,
                        )
                    except OSError as error:
                        if not self._is_exists_error(error):
                            raise
                        child = self.open_child(
                            current,
                            name,
                            directory=True,
                            writable=True,
                            pin_namespace=True,
                        )
                    self.api.flush(current.handle)
                previous = current
                current = child
                previous.close()
            result = current
            current = None  # type: ignore[assignment]
            return result
        finally:
            if current is not None:
                current.close()

    @staticmethod
    def _is_missing_error(error: OSError) -> bool:
        return isinstance(error, FileNotFoundError) or getattr(
            error, "errno", None
        ) in {2, 3}

    @staticmethod
    def _is_exists_error(error: OSError) -> bool:
        return isinstance(error, FileExistsError) or getattr(
            error, "errno", None
        ) in {80, 183}

    def _read_open_file(self, handle: VerifiedHandle, maximum: int) -> bytes:
        size = self.api.size(handle.handle)
        if size < 0 or size > maximum:
            raise WindowsFilesystemError("file exceeds read bound")
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = self.api.read(handle.handle, min(1024 * 1024, remaining))
            if not chunk:
                raise WindowsFilesystemError("short Win32 file read")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def replace_file_cas(
        self,
        parent: VerifiedHandle,
        name: str,
        *,
        expected: bytes | None,
        payload: bytes,
    ) -> WindowsIdentity:
        temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
        temporary: VerifiedHandle | None = None
        target: VerifiedHandle | None = None
        published = False
        try:
            temporary = self.open_child(
                parent,
                temporary_name,
                directory=False,
                destructive=True,
                creation=CREATE_NEW,
                writable=True,
            )
            self.api.write(temporary.handle, payload)
            self.api.flush(temporary.handle)
            try:
                target = self.open_child(
                    parent,
                    name,
                    directory=False,
                    destructive=True,
                    immutable=True,
                )
            except OSError as error:
                if not self._is_missing_error(error):
                    raise
                target = None
            if expected is None:
                if target is not None:
                    raise WindowsFilesystemError("Win32 CAS target appeared")
            elif target is None or self._read_open_file(
                target, len(expected)
            ) != expected:
                raise WindowsFilesystemError("Win32 CAS target changed")
            destination = self._child(parent.path, name)
            try:
                self.api.rename_relative(
                    temporary.handle,
                    parent.handle,
                    name,
                    replace=target is not None,
                )
            except OSError:
                temporary.rebind(destination)
                try:
                    published = (
                        WindowsIdentity(*self.api.identity(temporary.handle))
                        == temporary.identity
                        and self._final_path_matches(
                            self.api, temporary.handle, destination
                        )
                    )
                except OSError:
                    published = False
                raise
            temporary.rebind(destination)
            if (
                WindowsIdentity(*self.api.identity(temporary.handle))
                != temporary.identity
                or not self._final_path_matches(
                    self.api, temporary.handle, temporary.path
                )
            ):
                raise WindowsFilesystemError("atomic replacement was not confirmed")
            published = True
            self.api.flush(parent.handle)
            return temporary.identity
        finally:
            if not published and temporary is not None:
                try:
                    self.api.dispose(temporary.handle)
                except OSError:
                    pass
            if target is not None:
                target.close()
            if temporary is not None:
                temporary.close()

    def delete_file_cas(
        self,
        parent: VerifiedHandle,
        name: str,
        expected: bytes,
    ) -> None:
        with self.open_child(
            parent,
            name,
            directory=False,
            destructive=True,
            immutable=True,
        ) as victim:
            if self._read_open_file(victim, len(expected)) != expected:
                raise WindowsFilesystemError("Win32 CAS target changed")
            victim.assert_current()
            self.api.dispose(victim.handle)
        self.api.flush(parent.handle)

    def delete_file_identity(
        self,
        parent: VerifiedHandle,
        name: str,
        expected: WindowsIdentity,
    ) -> None:
        with self.open_child(
            parent,
            name,
            directory=False,
            destructive=True,
            expected=expected,
            immutable=True,
        ) as victim:
            victim.assert_current()
            self.api.dispose(victim.handle)
        self.api.flush(parent.handle)

    def rename_directory(
        self,
        parent: VerifiedHandle,
        source_name: str,
        expected: WindowsIdentity,
        destination_name: str,
    ) -> None:
        destination = self._child(parent.path, destination_name)
        with self.open_child(
            parent,
            source_name,
            directory=True,
            destructive=True,
            expected=expected,
        ) as source:
            self.api.rename_relative(
                source.handle,
                parent.handle,
                destination_name,
                replace=False,
            )
            source.rebind(destination)
            source.assert_current()
            self.api.flush(parent.handle)

    def delete_flat_directory_handle(
        self,
        parent: VerifiedHandle,
        name: str,
        expected: WindowsIdentity,
        *,
        quarantine_name: str,
        expected_inventory: dict[str, WindowsIdentity] | None = None,
        expected_payloads: dict[str, bytes] | None = None,
        expected_write_times: dict[str, float] | None = None,
    ) -> bool:
        quarantine = self._child(parent.path, quarantine_name)
        with self.open_child(
            parent,
            name,
            directory=True,
            destructive=True,
            expected=expected,
        ) as victim:
            names = self.api.list_directory_handle(victim.handle)
            pinned: dict[str, WindowsIdentity] = {}
            for child_name in names:
                with self.open_child(
                    victim,
                    child_name,
                    directory=False,
                    destructive=True,
                    immutable=True,
                ) as child:
                    pinned[child_name] = child.identity
            confirmed_names = self.api.list_directory_handle(victim.handle)
            if len(confirmed_names) != len(names) or set(confirmed_names) != set(
                names
            ):
                raise WindowsFilesystemError("directory inventory changed")
            if expected_inventory is not None and pinned != expected_inventory:
                raise WindowsFilesystemError("directory inventory changed")
            self.api.rename_relative(
                victim.handle,
                parent.handle,
                quarantine_name,
                replace=False,
            )
            victim.rebind(quarantine)
            victim.assert_current()
            self.api.flush(parent.handle)
            moved_names = self.api.list_directory_handle(victim.handle)
            if len(moved_names) != len(names) or set(moved_names) != set(names):
                return False
            opened_children: dict[str, VerifiedHandle] = {}
            try:
                for child_name, identity in pinned.items():
                    opened_children[child_name] = self.open_child(
                        victim,
                        child_name,
                        directory=False,
                        destructive=True,
                        expected=identity,
                        immutable=True,
                    )
                confirmed_names = self.api.list_directory_handle(victim.handle)
                if len(confirmed_names) != len(names) or set(confirmed_names) != set(
                    names
                ):
                    return False
                if expected_payloads is not None:
                    if not set(expected_payloads).issubset(opened_children):
                        return False
                    for child_name, payload in expected_payloads.items():
                        if self._read_open_file(
                            opened_children[child_name], len(payload)
                        ) != payload:
                            return False
                if expected_write_times is not None:
                    if not set(expected_write_times).issubset(opened_children):
                        return False
                    for child_name, timestamp in expected_write_times.items():
                        if (
                            self.api.last_write_time(
                                opened_children[child_name].handle
                            )
                            != timestamp
                        ):
                            return False
                victim.assert_current()
                for child in opened_children.values():
                    child.assert_current()
                for child in opened_children.values():
                    self.api.dispose(child.handle)
            finally:
                for child in reversed(list(opened_children.values())):
                    child.close()
            if self.api.list_directory_handle(victim.handle):
                raise WindowsFilesystemError("quarantine is not empty")
            self.api.dispose(victim.handle)
        self.api.flush(parent.handle)
        return True

    def initialize_marker(
        self, root: str, marker_name: str, payload: bytes, maximum: int
    ) -> MarkerEvidence:
        if len(payload) > maximum:
            raise WindowsFilesystemError("marker exceeds size limit")
        with self.open_verified(root, directory=True, destructive=True) as root_handle:
            if self.api.list_directory_handle(root_handle.handle):
                raise WindowsFilesystemError("unmarked cache root is not empty")
            marker_path = self._child(root_handle.path, marker_name)
            marker_identity: WindowsIdentity
            with self.open_child(
                root_handle,
                marker_name,
                directory=False,
                destructive=True,
                creation=CREATE_NEW,
                writable=True,
            ) as marker:
                self.api.write(marker.handle, payload)
                self.api.flush(marker.handle)
                marker_identity = marker.identity
                if self.api.list_directory_handle(root_handle.handle) != [marker_name]:
                    raise WindowsFilesystemError("cache root changed during initialization")
            with self.open_child(
                root_handle, marker_name, directory=False
            ) as confirmed_marker:
                confirmed_payload = self.api.read(confirmed_marker.handle, maximum + 1)
                if (
                    confirmed_marker.identity != marker_identity
                    or confirmed_payload != payload
                    or self.api.identity(root_handle.handle)
                    != (root_handle.identity.volume, root_handle.identity.file_id)
                ):
                    raise WindowsFilesystemError("marker write verification failed")
                return MarkerEvidence(
                    root_handle.identity,
                    confirmed_marker.identity,
                    confirmed_payload,
                )

    def attest_marker(
        self, root: str, marker_name: str, maximum: int
    ) -> MarkerEvidence:
        with self.open_verified(root, directory=True, destructive=True) as root_handle:
            with self.open_child(
                root_handle, marker_name, directory=False
            ) as marker:
                payload = self.api.read(marker.handle, maximum + 1)
                if len(payload) > maximum:
                    raise WindowsFilesystemError("marker exceeds size limit")
                if self.api.identity(root_handle.handle) != (
                    root_handle.identity.volume,
                    root_handle.identity.file_id,
                ):
                    raise WindowsFilesystemError("cache root identity changed")
                return MarkerEvidence(root_handle.identity, marker.identity, payload)

    def delete_file(
        self, parent: str, name: str, expected: WindowsIdentity
    ) -> None:
        path = self._child(parent, name)
        with self.open_verified(
            parent, directory=True, destructive=True
        ) as parent_handle:
            with self.open_child(
                parent_handle,
                name,
                directory=False,
                destructive=True,
                expected=expected,
            ) as victim:
                if self._canonical(PureWindowsPath(victim.path).parent) != self._canonical(
                    parent_handle.path
                ):
                    raise WindowsFilesystemError("victim escaped parent")
                self.api.dispose(victim.handle)

    def delete_flat_directory(
        self,
        parent: str,
        name: str,
        expected: WindowsIdentity,
        *,
        quarantine_name: str,
        expected_inventory: dict[str, WindowsIdentity] | None = None,
        expected_payloads: dict[str, bytes] | None = None,
        expected_write_times: dict[str, float] | None = None,
    ) -> bool:
        with self.open_verified(
            parent, directory=True, writable=True, pin_namespace=True
        ) as parent_handle:
            return self.delete_flat_directory_handle(
                parent_handle,
                name,
                expected,
                quarantine_name=quarantine_name,
                expected_inventory=expected_inventory,
                expected_payloads=expected_payloads,
                expected_write_times=expected_write_times,
            )
