"""Small Win32 handle layer for security-sensitive cache filesystem actions.

The module imports on non-Windows hosts so its policy can be unit-tested with an
in-memory API.  ``Kernel32Api`` itself is instantiated only on native Windows.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import ntpath
import os
from dataclasses import dataclass
from pathlib import PureWindowsPath
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
_FILE_ID_INFO = 18
_FILE_RENAME_INFO = 3
_FILE_DISPOSITION_INFO = 4
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


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
    def identity(self, handle: int) -> tuple[int, int]: ...
    def attributes(self, handle: int) -> tuple[bool, bool]: ...
    def link_count(self, handle: int) -> int: ...
    def final_path(self, handle: int) -> str: ...
    def list_directory(self, path: str) -> list[str]: ...
    def rename(self, handle: int, destination: str) -> None: ...
    def dispose(self, handle: int) -> None: ...
    def read(self, handle: int, maximum: int) -> bytes: ...
    def write(self, handle: int, payload: bytes) -> None: ...
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


class _FILE_DISPOSITION_INFO_STRUCT(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


class _FILE_RENAME_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_ubyte),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_ulong),
        ("FileName", ctypes.c_wchar * 1),
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

    @staticmethod
    def _raise(message: str) -> None:
        code = ctypes.get_last_error()
        raise WindowsFilesystemError(code, message)

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

    def list_directory(self, path: str) -> list[str]:
        return os.listdir(path)

    def rename(self, handle: int, destination: str) -> None:
        encoded = destination.encode("utf-16-le")
        name_offset = _FILE_RENAME_INFO_HEADER.FileName.offset
        buffer = ctypes.create_string_buffer(name_offset + len(encoded))
        header = _FILE_RENAME_INFO_HEADER.from_buffer(buffer)
        header.ReplaceIfExists = 0
        header.RootDirectory = None
        header.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded, len(encoded))
        if not self.kernel32.SetFileInformationByHandle(
            handle, _FILE_RENAME_INFO, buffer, len(buffer)
        ):
            self._raise("handle rename failed")

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

    def __enter__(self) -> "VerifiedHandle":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


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
    ) -> VerifiedHandle:
        requested = str(path)
        access = FILE_READ_ATTRIBUTES | GENERIC_READ
        if destructive:
            access |= DELETE
        if writable:
            access |= GENERIC_WRITE | FILE_WRITE_ATTRIBUTES
        share = FILE_SHARE_READ | FILE_SHARE_WRITE
        if not destructive:
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
            if self._canonical(self.api.final_path(handle)) != self._canonical(requested):
                raise WindowsFilesystemError("Win32 final path changed")
            return VerifiedHandle(self.api, handle, requested, identity, directory)
        except BaseException:
            self.api.close(handle)
            raise

    def initialize_marker(
        self, root: str, marker_name: str, payload: bytes, maximum: int
    ) -> MarkerEvidence:
        if len(payload) > maximum:
            raise WindowsFilesystemError("marker exceeds size limit")
        with self.open_verified(root, directory=True, destructive=True) as root_handle:
            if self.api.list_directory(root_handle.path):
                raise WindowsFilesystemError("unmarked cache root is not empty")
            marker_path = self._child(root_handle.path, marker_name)
            marker_identity: WindowsIdentity
            with self.open_verified(
                marker_path,
                directory=False,
                destructive=True,
                creation=CREATE_NEW,
                writable=True,
            ) as marker:
                self.api.write(marker.handle, payload)
                self.api.flush(marker.handle)
                marker_identity = marker.identity
                if self.api.list_directory(root_handle.path) != [marker_name]:
                    raise WindowsFilesystemError("cache root changed during initialization")
            with self.open_verified(marker_path, directory=False) as confirmed_marker:
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
            marker_path = self._child(root_handle.path, marker_name)
            with self.open_verified(marker_path, directory=False) as marker:
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
            with self.open_verified(
                path, directory=False, destructive=True, expected=expected
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
    ) -> bool:
        path = self._child(parent, name)
        quarantine = self._child(parent, quarantine_name)
        with self.open_verified(parent, directory=True, destructive=True) as parent_handle:
            with self.open_verified(
                path, directory=True, destructive=True, expected=expected
            ) as victim:
                if self._canonical(PureWindowsPath(victim.path).parent) != self._canonical(
                    parent_handle.path
                ):
                    raise WindowsFilesystemError("victim escaped parent")
                names = self.api.list_directory(victim.path)
                pinned: dict[str, WindowsIdentity] = {}
                for child_name in names:
                    child_path = self._child(victim.path, child_name)
                    with self.open_verified(
                        child_path, directory=False, destructive=True
                    ) as child:
                        pinned[child_name] = child.identity
                confirmed_names = self.api.list_directory(victim.path)
                if len(confirmed_names) != len(names) or set(confirmed_names) != set(
                    names
                ):
                    raise WindowsFilesystemError("directory inventory changed")
                for child_name, identity in pinned.items():
                    child_path = self._child(victim.path, child_name)
                    with self.open_verified(
                        child_path,
                        directory=False,
                        destructive=True,
                        expected=identity,
                    ):
                        pass
                self.api.rename(victim.handle, quarantine)
                if self._canonical(self.api.final_path(victim.handle)) != self._canonical(
                    quarantine
                ):
                    raise WindowsFilesystemError("handle rename was not confirmed")
                for child_name, identity in pinned.items():
                    moved_path = self._child(quarantine, child_name)
                    with self.open_verified(
                        moved_path,
                        directory=False,
                        destructive=True,
                        expected=identity,
                    ) as child:
                        self.api.dispose(child.handle)
                if self.api.list_directory(quarantine):
                    raise WindowsFilesystemError("quarantine is not empty")
                self.api.dispose(victim.handle)
                return True
