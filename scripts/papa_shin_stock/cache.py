from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urljoin, urlsplit

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows only
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX only
    _msvcrt = None

from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.http_client import HttpResponse, SafeHttpClient
from papa_shin_stock.validation import (
    is_bounded_unicode_scalar,
    is_iso_8601_timestamp,
)


_LOCK_TTL_SECONDS = 30 * 60
_LOCK_FUTURE_SKEW_SECONDS = 5 * 60
_DIRECTORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_RUNTIME_REVISION = re.compile(r"[0-9a-f]{32}\Z")
_RUNTIME_STATUS_FILE = re.compile(
    r"\.runtime-status-(?P<directory>[A-Za-z0-9][A-Za-z0-9._-]*)\.json\Z"
)
_RUNTIME_STATUS_TEMP = re.compile(
    r"\.\.runtime-status-(?P<directory>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"\.json\.[0-9a-f]{32}\.tmp\Z"
)
_LOCK_INIT_DIRECTORY = re.compile(r"\.refresh\.lock\.init-[0-9a-f]{32}\Z")
_GENERATION_DELETE_QUARANTINE = re.compile(
    r"\.(?:generation-[A-Za-z0-9][A-Za-z0-9._-]*|\.staging-[0-9a-f]{32})"
    r"\.delete-[0-9a-f]{32}\Z"
)
_GENERATION_QUARANTINE_SETTLE_ATTEMPTS = 3
_GENERATION_QUARANTINE_SETTLE_SECONDS = 0.01
_MAX_STATUS_TEXT = 256
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_CACHE_ROOT_MARKER_NAME = ".papa-shin-stock-cache-root.json"
_CACHE_ROOT_MARKER_KIND = "papa-shin-stock-cache-root"
_CACHE_ROOT_MARKER_VERSION = 1
_CACHE_ROOT_MARKER_MAX_BYTES = 512
_CACHE_ROOT_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
_WINDOWS_POINTER_MAX_BYTES = 16 * 1024
_WINDOWS_STATE_MAX_BYTES = 64 * 1024
_WINDOWS_RUNTIME_MAX_BYTES = 64 * 1024
_WINDOWS_OWNER_MAX_BYTES = 16 * 1024
_WINDOWS_GENERATION_OWNER_MAX_BYTES = 16 * 1024
_WINDOWS_MANIFEST_MAX_BYTES = 1024 * 1024
_WINDOWS_MAX_ROOT_ENTRIES = 4096
_WINDOWS_MAX_RECOVERY_ARTIFACTS = 64
_WINDOWS_GENERATION_OWNER_NAME = ".papa-shin-stock-generation.json"
_WINDOWS_GENERATION_OWNER_KIND = "papa-shin-stock-generation"
_WINDOWS_RUNTIME_OWNER_KIND = "papa-shin-stock-runtime-status"
_WINDOWS_OWNER_SCHEMA_VERSION = 1
_WINDOWS_LOCK_RELEASE_DIRECTORY = re.compile(
    r"\.refresh\.lock\.release-(?P<token>[0-9a-f]{32})-[0-9a-f]{32}\Z"
)
_WINDOWS_LOCK_RECLAIM_DIRECTORY = re.compile(
    r"\.refresh\.lock\.reclaim-(?P<token>[0-9a-f]{32}|ownerless)-[0-9a-f]{32}\Z"
)
_WINDOWS_LOCK_REJECTED_DIRECTORY = re.compile(
    r"\.refresh\.lock\.rejected-(?:release|reclaim)-[0-9a-f]{32}\Z"
)
_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = {
    errno.EBADF,
    errno.EINVAL,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}


def _is_native_windows() -> bool:
    return os.name == "nt"


def _windows_filesystem() -> Any:
    from papa_shin_stock._windows_fs import WindowsFilesystem

    return WindowsFilesystem()


@dataclass(frozen=True, slots=True)
class GenerationIntegrity:
    manifest_sha256: str
    generated_at: str
    products_bytes: int
    products_sha256: str
    offers_bytes: int
    offers_sha256: str


@dataclass(frozen=True, slots=True)
class GenerationFiles:
    generation_id: str
    manifest: Path
    products: Path
    offers: Path
    runtime_status: Path | None = None
    integrity: GenerationIntegrity | None = None

    @classmethod
    def from_directory(
        cls,
        generation_id: str,
        directory: Path,
        runtime_status: Path | None = None,
        integrity: GenerationIntegrity | None = None,
    ) -> "GenerationFiles":
        return cls(
            generation_id=generation_id,
            manifest=directory / "manifest.json",
            products=directory / "products.jsonl",
            offers=directory / "offers.jsonl",
            runtime_status=runtime_status,
            integrity=integrity,
        )

    def assert_readable(self) -> None:
        directory = self.manifest.parent
        _ensure_private_directory(directory)
        for path in (self.manifest, self.products, self.offers):
            try:
                descriptor = _open_private_regular_file(path)
                try:
                    os.read(descriptor, 1)
                finally:
                    os.close(descriptor)
            except (OSError, StockError) as error:
                raise _cache_unavailable() from error


@dataclass(frozen=True, slots=True)
class _WindowsGeneration:
    files: GenerationFiles
    identity: Any
    ownership_token: str


@dataclass(frozen=True, slots=True)
class _WindowsGenerationOwnership:
    generation_id: str
    ownership_token: str


class _WindowsRollbackUnconfirmed(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _WindowsLockSnapshot:
    identity: Any
    token: str | None
    timestamp: float
    inventory: dict[str, Any]
    payloads: dict[str, bytes]
    write_times: dict[str, float]


class CacheRootAttestation:
    """Pinned proof that a cache root is an explicitly owned leaf directory."""

    def __init__(
        self,
        root: Path,
        descriptor: int | None,
        identity: os.stat_result | None,
        marker_evidence: _PublishedCacheRootMarker,
        windows_root: Any | None = None,
    ) -> None:
        self.root = root
        self.descriptor = descriptor
        self.identity = identity
        self.marker_evidence = marker_evidence
        self.windows_root = windows_root

    @property
    def ownership_token(self) -> str:
        return self.marker_evidence.ownership_token

    def assert_current(self) -> None:
        try:
            if self.windows_root is not None:
                self.windows_root.assert_current()
                evidence = self.windows_root.marker_evidence
                if (
                    _parse_cache_root_marker_payload(evidence.payload)
                    != self.ownership_token
                    or evidence.marker_identity.volume
                    != self.marker_evidence.device
                    or evidence.marker_identity.file_id
                    != self.marker_evidence.inode
                ):
                    raise _cache_unavailable()
                return
            if self.identity is None:
                raise _cache_unavailable()
            if self.descriptor is not None:
                opened = os.fstat(self.descriptor)
                if not stat.S_ISDIR(opened.st_mode) or not _same_file_identity(
                    self.identity, opened
                ):
                    raise _cache_unavailable()
            observed = self.root.lstat()
            if (
                not stat.S_ISDIR(observed.st_mode)
                or _is_windows_directory_reparse_point(self.root, observed)
                or not _same_file_identity(self.identity, observed)
            ):
                raise _cache_unavailable()
            marker_evidence = _read_cache_root_marker(self.root, self.descriptor)
            if marker_evidence != self.marker_evidence:
                raise _cache_unavailable()
            confirmed_root = self.root.lstat()
            if (
                not stat.S_ISDIR(confirmed_root.st_mode)
                or _is_windows_directory_reparse_point(
                    self.root, confirmed_root
                )
                or not _same_file_identity(self.identity, confirmed_root)
            ):
                raise _cache_unavailable()
        except StockError:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise _cache_unavailable() from error

    def close(self) -> None:
        windows_root = self.windows_root
        self.windows_root = None
        if windows_root is not None:
            try:
                windows_root.close()
            except OSError as error:
                raise _cache_unavailable() from error
            return
        descriptor = self.descriptor
        self.descriptor = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                raise _cache_unavailable() from error

    def __enter__(self) -> "CacheRootAttestation":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class _PublishedCacheRootMarker:
    ownership_token: str
    device: int
    inode: int
    file_type: int
    link_count: int


def _windows_session(attestation: CacheRootAttestation) -> Any:
    session = attestation.windows_root
    if session is None:
        raise _cache_unavailable()
    return session


def _json_payload(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _parse_windows_json(payload: bytes) -> object:
    try:
        return _parse_json(payload.decode("utf-8"))
    except (
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise _cache_unavailable() from error


def _parse_windows_generation_ownership(
    payload: bytes, expected_root_token: str
) -> _WindowsGenerationOwnership:
    value = _parse_windows_json(payload)
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "schema_version",
        "root_ownership_token",
        "generation_id",
        "ownership_token",
    }:
        raise _cache_unavailable()
    generation_id = value.get("generation_id")
    ownership_token = value.get("ownership_token")
    if (
        value.get("kind") != _WINDOWS_GENERATION_OWNER_KIND
        or value.get("schema_version") != _WINDOWS_OWNER_SCHEMA_VERSION
        or value.get("root_ownership_token") != expected_root_token
        or not isinstance(generation_id, str)
        or not generation_id
        or not isinstance(ownership_token, str)
        or not _RUNTIME_REVISION.fullmatch(ownership_token)
    ):
        raise _cache_unavailable()
    return _WindowsGenerationOwnership(generation_id, ownership_token)


def _windows_runtime_value(
    value: dict[str, object],
    *,
    root_ownership_token: str,
    generation_ownership_token: str,
    directory_name: str,
) -> dict[str, object]:
    return {
        **value,
        "ownership": {
            "kind": _WINDOWS_RUNTIME_OWNER_KIND,
            "schema_version": _WINDOWS_OWNER_SCHEMA_VERSION,
            "root_ownership_token": root_ownership_token,
            "generation_ownership_token": generation_ownership_token,
            "directory_name": directory_name,
        },
    }


def _validate_windows_runtime_payload(
    payload: bytes,
    *,
    expected_generation_id: str,
    expected_directory_name: str,
    expected_root_token: str,
    expected_generation_ownership_token: str | None = None,
) -> RuntimeStatus:
    if not expected_generation_id:
        raise _cache_unavailable()
    value = _parse_windows_json(payload)
    if not isinstance(value, dict) or set(value) != {
        "generation_id",
        "checked_at",
        "stale",
        "warning_code",
        "revision",
        "ownership",
    }:
        raise _cache_unavailable()
    ownership = value.get("ownership")
    if not isinstance(ownership, dict) or set(ownership) != {
        "kind",
        "schema_version",
        "root_ownership_token",
        "generation_ownership_token",
        "directory_name",
    }:
        raise _cache_unavailable()
    generation_ownership_token = ownership.get("generation_ownership_token")
    if (
        ownership.get("kind") != _WINDOWS_RUNTIME_OWNER_KIND
        or ownership.get("schema_version") != _WINDOWS_OWNER_SCHEMA_VERSION
        or ownership.get("root_ownership_token") != expected_root_token
        or ownership.get("directory_name") != expected_directory_name
        or not isinstance(generation_ownership_token, str)
        or not _RUNTIME_REVISION.fullmatch(generation_ownership_token)
        or (
            expected_generation_ownership_token is not None
            and generation_ownership_token
            != expected_generation_ownership_token
        )
    ):
        raise _cache_unavailable()
    return validate_runtime_status(value, expected_generation_id)


@dataclass(frozen=True, slots=True)
class CurrentPointer:
    generation_id: str
    directory_name: str
    activation_token: str | None = None

    @classmethod
    def load(cls, path: Path) -> "CurrentPointer":
        try:
            value = _parse_json(_read_private_text(path))
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ) as error:
            raise _cache_unavailable() from error
        return cls.from_value(value)

    @classmethod
    def load_from_directory(cls, descriptor: int) -> "CurrentPointer":
        try:
            value = _parse_json(_read_private_child_text(descriptor, "current.json"))
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ) as error:
            raise _cache_unavailable() from error
        return cls.from_value(value)

    @classmethod
    def from_value(cls, value: object) -> "CurrentPointer":
        if not isinstance(value, dict):
            raise _cache_unavailable()

        generation_id = value.get("generation_id")
        directory_name = value.get("directory_name")
        activation_token = value.get("activation_token")
        if not isinstance(generation_id, str) or not generation_id:
            raise _cache_unavailable()
        if (
            not isinstance(directory_name, str)
            or not _DIRECTORY_NAME.fullmatch(directory_name)
            or directory_name in {".", ".."}
        ):
            raise _cache_unavailable()
        if activation_token is not None and not isinstance(activation_token, str):
            raise _cache_unavailable()
        return cls(generation_id, directory_name, activation_token)

    def to_dict(self) -> dict[str, str]:
        value = {
            "generation_id": self.generation_id,
            "directory_name": self.directory_name,
        }
        if self.activation_token is not None:
            value["activation_token"] = self.activation_token
        return value


@dataclass(frozen=True, slots=True)
class CacheState:
    generation_id: str
    generated_at: str
    checked_at: str
    manifest_etag: str | None
    manifest_last_modified: str | None
    directory_name: str
    files: GenerationFiles
    runtime_revision: str | None = None
    stale: bool = False
    warning_code: str | None = None

    @classmethod
    def load(
        cls,
        cache_dir: Path,
        progress: Callable[[], None] | None = None,
    ) -> "CacheState | None":
        if _lstat_optional(cache_dir) is None:
            return None
        if _is_native_windows():
            return cls._load_windows(cache_dir, progress)
        with _attest_cache_root(cache_dir, create=False) as attestation:
            state = cls._load_attested(cache_dir, progress, attestation)
            attestation.assert_current()
            return state

    @classmethod
    def load_for_search(cls, cache_dir: Path) -> "CacheState | None":
        if _lstat_optional(cache_dir) is None:
            return None
        if _is_native_windows():
            return cls._load_windows(cache_dir, verify_payloads=False)
        with _attest_cache_root(cache_dir, create=False) as attestation:
            state = cls._load_attested(
                cache_dir,
                None,
                attestation,
                verify_payloads=False,
            )
            attestation.assert_current()
            return state

    @classmethod
    def _load_windows(
        cls,
        cache_dir: Path,
        progress: Callable[[], None] | None = None,
        *,
        verify_payloads: bool = True,
    ) -> "CacheState | None":
        try:
            with _attest_cache_root(cache_dir, create=False) as attestation:
                session = _windows_session(attestation)
                pointer_payload = session.read_optional_file(
                    (), "current.json", _WINDOWS_POINTER_MAX_BYTES
                )
                if pointer_payload is None:
                    return None
                pointer = CurrentPointer.from_value(
                    _parse_windows_json(pointer_payload)
                )
                generation_parts = ("generations", pointer.directory_name)
                generation_ownership = _parse_windows_generation_ownership(
                    session.read_file(
                        generation_parts,
                        _WINDOWS_GENERATION_OWNER_NAME,
                        _WINDOWS_GENERATION_OWNER_MAX_BYTES,
                    ),
                    attestation.ownership_token,
                )
                if generation_ownership.generation_id != pointer.generation_id:
                    raise _cache_unavailable()
                manifest_payload = session.read_file(
                    generation_parts,
                    "manifest.json",
                    _WINDOWS_MANIFEST_MAX_BYTES,
                )
                manifest = Manifest.parse(manifest_payload)
                if manifest.generation_id != pointer.generation_id:
                    raise _cache_unavailable()
                if verify_payloads:
                    session.verify_file(
                        generation_parts,
                        "products.jsonl",
                        expected_bytes=manifest.products.bytes,
                        expected_sha256=manifest.products.sha256,
                        progress=progress,
                    )
                    session.verify_file(
                        generation_parts,
                        "offers.jsonl",
                        expected_bytes=manifest.offers.bytes,
                        expected_sha256=manifest.offers.sha256,
                        progress=progress,
                    )
                value = _parse_windows_json(
                    session.read_file(
                        generation_parts,
                        "state.json",
                        _WINDOWS_STATE_MAX_BYTES,
                    )
                )
                if not isinstance(value, dict):
                    raise _cache_unavailable()
                generation_id = value.get("generation_id")
                generated_at = value.get("generated_at")
                checked_at = value.get("checked_at")
                manifest_etag = value.get("manifest_etag")
                manifest_last_modified = value.get("manifest_last_modified")
                if generation_id != pointer.generation_id:
                    raise _cache_unavailable()
                if not is_iso_8601_timestamp(generated_at):
                    raise _cache_unavailable()
                if not isinstance(checked_at, str) or not checked_at:
                    raise _cache_unavailable()
                if manifest_etag is not None and not isinstance(
                    manifest_etag, str
                ):
                    raise _cache_unavailable()
                if manifest_last_modified is not None and not isinstance(
                    manifest_last_modified, str
                ):
                    raise _cache_unavailable()
                runtime_path = _runtime_status_path(
                    cache_dir, pointer.directory_name
                )
                runtime_payload = session.read_optional_file(
                    (), runtime_path.name, _WINDOWS_RUNTIME_MAX_BYTES
                )
                if runtime_payload is None:
                    runtime = validate_runtime_status(
                        value, pointer.generation_id
                    )
                    runtime_file = None
                else:
                    runtime = _validate_windows_runtime_payload(
                        runtime_payload,
                        expected_generation_id=pointer.generation_id,
                        expected_directory_name=pointer.directory_name,
                        expected_root_token=attestation.ownership_token,
                        expected_generation_ownership_token=(
                            generation_ownership.ownership_token
                        ),
                    )
                    runtime_file = runtime_path
                attestation.assert_current()
                return cls(
                    generation_id=generation_id,
                    generated_at=generated_at,
                    checked_at=runtime.checked_at,
                    manifest_etag=manifest_etag,
                    manifest_last_modified=manifest_last_modified,
                    directory_name=pointer.directory_name,
                    files=GenerationFiles.from_directory(
                        pointer.generation_id,
                        cache_dir / "generations" / pointer.directory_name,
                        runtime_file,
                        _generation_integrity(manifest),
                    ),
                    runtime_revision=runtime.revision,
                    stale=runtime.stale,
                    warning_code=runtime.warning_code,
                )
        except StockError:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise _cache_unavailable() from error

    @classmethod
    def _load_attested(
        cls,
        cache_dir: Path,
        progress: Callable[[], None] | None = None,
        root_attestation: CacheRootAttestation | None = None,
        *,
        verify_payloads: bool = True,
    ) -> "CacheState | None":
        if root_attestation is None:
            raise _cache_unavailable()
        root = _lstat_optional(cache_dir)
        if root is None:
            return None
        pointer_path = cache_dir / "current.json"
        pointer_entry = _lstat_private_child(
            root_attestation.descriptor,
            cache_dir,
            "current.json",
            missing_ok=True,
        )
        if pointer_entry is None:
            return None
        if not stat.S_ISREG(pointer_entry.st_mode):
            raise _cache_unavailable()

        pointer = CurrentPointer.load_from_directory(root_attestation.descriptor)
        generations = cache_dir / "generations"
        generations_descriptor = -1
        generation_descriptor = -1
        try:
            generations_descriptor = _open_private_child_directory(
                root_attestation.descriptor, cache_dir, generations
            )
            generation = generations / pointer.directory_name
            generation_descriptor = _open_private_child_directory(
                generations_descriptor, generations, generation
            )
            return cls._load_generation_attested(
                cache_dir,
                pointer,
                generation,
                generation_descriptor,
                root_attestation,
                progress,
                verify_payloads,
            )
        finally:
            _close_optional_descriptor(
                generation_descriptor if generation_descriptor >= 0 else None
            )
            _close_optional_descriptor(
                generations_descriptor if generations_descriptor >= 0 else None
            )

    @classmethod
    def _load_generation_attested(
        cls,
        cache_dir: Path,
        pointer: CurrentPointer,
        generation: Path,
        generation_descriptor: int,
        root_attestation: CacheRootAttestation,
        progress: Callable[[], None] | None,
        verify_payloads: bool,
    ) -> "CacheState":
        files = GenerationFiles.from_directory(pointer.generation_id, generation)
        try:
            for name in ("manifest.json", "products.jsonl", "offers.jsonl"):
                descriptor = _open_private_child_regular_file(
                    generation_descriptor, name
                )
                _close_optional_descriptor(descriptor)
            manifest = Manifest.parse(
                _read_private_child_bytes(generation_descriptor, "manifest.json")
            )
            if manifest.generation_id != pointer.generation_id:
                raise _cache_unavailable()
            if verify_payloads:
                _verify_child_file(
                    generation_descriptor,
                    "products.jsonl",
                    manifest.products,
                    progress,
                )
                _verify_child_file(
                    generation_descriptor,
                    "offers.jsonl",
                    manifest.offers,
                    progress,
                )
            files = replace(files, integrity=_generation_integrity(manifest))
        except StockError as error:
            if error.code == "cache_locked":
                raise
            raise _cache_unavailable() from error
        except OSError as error:
            raise _cache_unavailable() from error

        state_path = generation / "state.json"
        try:
            value = _parse_json(
                _read_private_child_text(generation_descriptor, "state.json")
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ) as error:
            raise _cache_unavailable() from error
        if not isinstance(value, dict):
            raise _cache_unavailable()

        generation_id = value.get("generation_id")
        generated_at = value.get("generated_at")
        checked_at = value.get("checked_at")
        manifest_etag = value.get("manifest_etag")
        manifest_last_modified = value.get("manifest_last_modified")
        if generation_id != pointer.generation_id:
            raise _cache_unavailable()
        if not is_iso_8601_timestamp(generated_at):
            raise _cache_unavailable()
        if not isinstance(checked_at, str) or not checked_at:
            raise _cache_unavailable()
        if manifest_etag is not None and not isinstance(manifest_etag, str):
            raise _cache_unavailable()
        if manifest_last_modified is not None and not isinstance(
            manifest_last_modified, str
        ):
            raise _cache_unavailable()
        runtime_status, runtime_path = _load_optional_runtime_status_attested(
            root_attestation, pointer.directory_name, pointer.generation_id
        )
        if runtime_status is not None and runtime_path is not None:
            files = replace(files, runtime_status=runtime_path)
        else:
            runtime_status = validate_runtime_status(value, pointer.generation_id)
        result = cls(
            generation_id=generation_id,
            generated_at=generated_at,
            checked_at=runtime_status.checked_at,
            manifest_etag=manifest_etag,
            manifest_last_modified=manifest_last_modified,
            directory_name=pointer.directory_name,
            files=files,
            runtime_revision=runtime_status.revision,
            stale=runtime_status.stale,
            warning_code=runtime_status.warning_code,
        )
        root_attestation.assert_current()
        return result


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    generation_id: str
    checked_at: str
    stale: bool
    warning_code: str | None
    revision: str | None


def validate_runtime_status(
    value: object, expected_generation_id: str
) -> RuntimeStatus:
    if not isinstance(value, dict):
        raise _cache_unavailable()
    generation_id = value.get("generation_id")
    checked_at = value.get("checked_at")
    stale = value.get("stale", False)
    warning_code = value.get("warning_code")
    revision = value.get("revision")
    if generation_id != expected_generation_id:
        raise _cache_unavailable()
    if (
        not is_iso_8601_timestamp(checked_at)
    ):
        raise _cache_unavailable()
    if not isinstance(stale, bool):
        raise _cache_unavailable()
    if stale:
        if (
            not is_bounded_unicode_scalar(
                warning_code, maximum=_MAX_STATUS_TEXT
            )
        ):
            raise _cache_unavailable()
    elif warning_code is not None:
        raise _cache_unavailable()
    if revision is not None and (
        not isinstance(revision, str) or not _RUNTIME_REVISION.fullmatch(revision)
    ):
        raise _cache_unavailable()
    return RuntimeStatus(generation_id, checked_at, stale, warning_code, revision)


def load_runtime_status(path: Path, expected_generation_id: str) -> RuntimeStatus:
    descriptor = -1
    try:
        descriptor = _open_private_regular_file(path)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            value = _parse_json(stream.read())
    except StockError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise _cache_unavailable() from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as error:
                raise _cache_unavailable() from error
    return validate_runtime_status(value, expected_generation_id)


def load_runtime_status_from_directory(
    descriptor: int, name: str, expected_generation_id: str
) -> RuntimeStatus:
    try:
        value = _parse_json(_read_private_child_text(descriptor, name))
    except StockError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise _cache_unavailable() from error
    return validate_runtime_status(value, expected_generation_id)


@dataclass(frozen=True, slots=True)
class ManifestFile:
    url: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Manifest:
    generation_id: str
    generated_at: str
    products: ManifestFile
    offers: ManifestFile
    body: bytes

    @classmethod
    def parse(cls, body: bytes) -> "Manifest":
        try:
            value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        except StockError:
            raise
        except (
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ) as error:
            raise _manifest_invalid() from error
        if not isinstance(value, dict):
            raise _manifest_invalid()

        generation_id = value.get("generation_id")
        generated_at = value.get("generated_at")
        files = value.get("files")
        if not is_bounded_unicode_scalar(generation_id):
            raise _manifest_invalid()
        if not is_iso_8601_timestamp(generated_at):
            raise _manifest_invalid()
        if not isinstance(files, dict):
            raise _manifest_invalid()
        return cls(
            generation_id=generation_id,
            generated_at=generated_at,
            products=_manifest_file(files.get("products")),
            offers=_manifest_file(files.get("offers")),
            body=body,
        )


def _generation_integrity(manifest: Manifest) -> GenerationIntegrity:
    return GenerationIntegrity(
        manifest_sha256=hashlib.sha256(manifest.body).hexdigest(),
        generated_at=manifest.generated_at,
        products_bytes=manifest.products.bytes,
        products_sha256=manifest.products.sha256,
        offers_bytes=manifest.offers.bytes,
        offers_sha256=manifest.offers.sha256,
    )


@dataclass(frozen=True, slots=True)
class RefreshResult:
    status: str
    generation_id: str
    generated_at: str
    checked_at: str
    stale: bool
    warning_code: str | None = None

    @classmethod
    def from_state(
        cls,
        status: str,
        state: CacheState,
        *,
        stale: bool = False,
        warning_code: str | None = None,
    ) -> "RefreshResult":
        return cls(
            status=status,
            generation_id=state.generation_id,
            generated_at=state.generated_at,
            checked_at=state.checked_at,
            stale=stale or state.stale,
            warning_code=warning_code or state.warning_code,
        )

    def to_public_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": self.status,
            "generation": {
                "id": self.generation_id,
                "generated_at": self.generated_at,
                "checked_at": self.checked_at,
                "stale": self.stale,
            },
            "warnings": [],
        }
        if self.warning_code is not None:
            message = (
                "Не удалось удалить часть неактивного кэша"
                if self.warning_code == "cache_cleanup_incomplete"
                else "Не удалось обновить данные; используется предыдущее поколение"
            )
            result["warnings"] = [
                {
                    "code": self.warning_code,
                    "message": message,
                }
            ]
        return result


class RuntimeCommitLock:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor

    @classmethod
    def acquire(
        cls,
        root: Path,
        root_attestation: CacheRootAttestation | None = None,
    ) -> "RuntimeCommitLock":
        return cls(_acquire_runtime_commit_descriptor(root, root_attestation))

    def release(self, *, suppress_errors: bool = False) -> None:
        if self.descriptor < 0:
            return
        descriptor = self.descriptor
        self.descriptor = -1
        failure: OSError | None = None
        try:
            _unlock_commit_descriptor(descriptor)
        except OSError as error:
            failure = error
        try:
            os.close(descriptor)
        except OSError as error:
            if failure is None:
                failure = error
        if failure is not None and not suppress_errors:
            raise _cache_unavailable() from failure

    def __enter__(self) -> "RuntimeCommitLock":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release(suppress_errors=exc_type is not None)


class _RefreshLockPublishLock(RuntimeCommitLock):
    @classmethod
    def acquire(
        cls,
        root: Path,
        root_attestation: CacheRootAttestation | None = None,
    ) -> "_RefreshLockPublishLock":
        return cls(_acquire_runtime_commit_descriptor(root, root_attestation))


class _PrivateDownloadDestination:
    def __init__(
        self,
        path: Path,
        root_attestation: CacheRootAttestation | None = None,
        parent_descriptor: int | None = None,
    ) -> None:
        self.path = path
        self.root_attestation = root_attestation
        self.parent_descriptor = parent_descriptor
        if parent_descriptor is None:
            self.descriptor = _open_private_regular_file(
                path,
                flags=os.O_RDWR | os.O_CREAT | os.O_EXCL,
                create=True,
            )
        else:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                self.descriptor = os.open(
                    path.name,
                    flags,
                    _PRIVATE_FILE_MODE,
                    dir_fd=parent_descriptor,
                )
                _harden_private_descriptor(
                    self.descriptor, stat.S_IFREG, _PRIVATE_FILE_MODE
                )
            except (OSError, StockError, ValueError, TypeError) as error:
                if getattr(self, "descriptor", -1) >= 0:
                    _close_optional_descriptor(self.descriptor)
                raise _cache_unavailable() from error
        self.identity = os.fstat(self.descriptor)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def parents(self) -> Any:
        return self.path.parents

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def open(self, mode: str = "r", *args: object, **kwargs: object) -> Any:
        if mode != "wb" or args or kwargs:
            raise _cache_unavailable()
        self.assert_path_owned()
        duplicate = -1
        try:
            duplicate = os.dup(self.descriptor)
            os.ftruncate(duplicate, 0)
            os.lseek(duplicate, 0, os.SEEK_SET)
            return os.fdopen(duplicate, "wb")
        except (OSError, ValueError, TypeError) as error:
            if duplicate >= 0:
                try:
                    os.close(duplicate)
                except OSError:
                    pass
            raise _cache_unavailable() from error

    def write_bytes(self, payload: bytes) -> int:
        with self.open("wb") as stream:
            return stream.write(payload)

    def unlink(self, missing_ok: bool = False) -> None:
        if self.root_attestation is not None:
            self.root_attestation.assert_current()
        observed = (
            _lstat_optional(self.path)
            if self.parent_descriptor is None
            else _lstat_private_child_descriptor(
                self.parent_descriptor, self.path.name, missing_ok=True
            )
        )
        if observed is None:
            if missing_ok:
                return
            raise _cache_unavailable()
        if not stat.S_ISREG(observed.st_mode) or not _same_file_identity(
            self.identity, observed
        ):
            raise _cache_unavailable()
        if self.parent_descriptor is None:
            removed = _unlink_private_regular_file_if_owned(
                self.path,
                self.identity,
            )
        else:
            _unlink_private_child_regular_file(
                self.parent_descriptor,
                self.path.parent,
                self.path.name,
                self.identity,
            )
            removed = True
        if not removed and not missing_ok:
            raise _cache_unavailable()

    def assert_path_owned(self) -> None:
        if self.descriptor < 0:
            raise _cache_unavailable()
        opened = os.fstat(self.descriptor)
        observed = (
            _lstat_optional(self.path)
            if self.parent_descriptor is None
            else _lstat_private_child_descriptor(
                self.parent_descriptor, self.path.name, missing_ok=True
            )
        )
        if (
            observed is None
            or not stat.S_ISREG(observed.st_mode)
            or not _same_file_identity(self.identity, opened)
            or not _same_file_identity(opened, observed)
        ):
            raise _cache_unavailable()

    def fsync(self) -> None:
        self.assert_path_owned()
        os.fsync(self.descriptor)

    def close(self) -> None:
        if self.descriptor < 0:
            return
        descriptor = self.descriptor
        self.descriptor = -1
        os.close(descriptor)


def _acquire_runtime_commit_descriptor(
    root: Path,
    root_attestation: CacheRootAttestation | None = None,
) -> int:
    descriptor = -1
    owned_attestation: CacheRootAttestation | None = None
    try:
        if root_attestation is None:
            owned_attestation = _attest_cache_root(root, create=True)
            root_attestation = owned_attestation
        root_attestation.assert_current()
        name = ".runtime-status.commit.lock"
        observed = _lstat_private_child(
            root_attestation.descriptor,
            root,
            name,
            missing_ok=True,
        )
        if observed is not None and not stat.S_ISREG(observed.st_mode):
            raise _cache_unavailable()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            name,
            flags,
            _PRIVATE_FILE_MODE,
            dir_fd=root_attestation.descriptor,
        )
        opened = _harden_private_descriptor(
            descriptor, stat.S_IFREG, _PRIVATE_FILE_MODE
        )
        confirmed = os.stat(
            name,
            dir_fd=root_attestation.descriptor,
            follow_symlinks=False,
        )
        if not _same_file_identity(opened, confirmed):
            raise _cache_unavailable()
        opened = os.fstat(descriptor)
        if opened.st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        _lock_commit_descriptor(descriptor)
        root_attestation.assert_current()
        return descriptor
    except StockError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except (OSError, ValueError, TypeError) as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise _cache_unavailable() from error
    finally:
        if owned_attestation is not None:
            owned_attestation.close()


def _create_private_child_directory_descriptor(
    parent_descriptor: int, name: str
) -> int:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise _cache_unavailable()
    descriptor = -1
    try:
        os.mkdir(name, _PRIVATE_DIRECTORY_MODE, dir_fd=parent_descriptor)
        observed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(observed.st_mode):
            raise _cache_unavailable()
        if os.name == "posix":
            effective_uid = getattr(os, "geteuid", lambda: observed.st_uid)()
            if observed.st_uid != effective_uid:
                raise _cache_unavailable()
            if stat.S_IMODE(observed.st_mode) & (
                stat.S_IRUSR | stat.S_IXUSR
            ) != (stat.S_IRUSR | stat.S_IXUSR):
                if not _remove_created_private_directory_entry(
                    parent_descriptor, name, observed
                ):
                    raise _cache_unavailable()
                _mkdir_private_directory_without_process_umask(
                    parent_descriptor, name
                )
                observed = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(observed.st_mode)
                    or observed.st_uid != effective_uid
                ):
                    raise _cache_unavailable()
            _chmod_created_private_directory(parent_descriptor, name, observed)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        opened = _harden_private_descriptor(
            descriptor, stat.S_IFDIR, _PRIVATE_DIRECTORY_MODE
        )
        confirmed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not _same_file_identity(opened, confirmed):
            raise _cache_unavailable()
        return descriptor
    except StockError:
        _close_optional_descriptor(descriptor if descriptor >= 0 else None)
        raise
    except (OSError, ValueError, TypeError) as error:
        _close_optional_descriptor(descriptor if descriptor >= 0 else None)
        raise _cache_unavailable() from error


def _open_or_create_private_child_directory_descriptor(
    parent_descriptor: int, name: str
) -> int:
    observed = _lstat_private_child_descriptor(
        parent_descriptor, name, missing_ok=True
    )
    if observed is None:
        return _create_private_child_directory_descriptor(parent_descriptor, name)
    if not stat.S_ISDIR(observed.st_mode):
        raise _cache_unavailable()
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        opened = _harden_private_descriptor(
            descriptor, stat.S_IFDIR, _PRIVATE_DIRECTORY_MODE
        )
        confirmed = os.stat(
            name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if not _same_file_identity(opened, confirmed):
            raise _cache_unavailable()
        return descriptor
    except StockError:
        _close_optional_descriptor(descriptor if descriptor >= 0 else None)
        raise
    except (OSError, ValueError, TypeError) as error:
        _close_optional_descriptor(descriptor if descriptor >= 0 else None)
        raise _cache_unavailable() from error


class CacheLock:
    def __init__(
        self,
        path: Path,
        token: str,
        identity: tuple[int, int],
        root_attestation: CacheRootAttestation,
        lock_descriptor: int,
    ) -> None:
        self.path = path
        self.token = token
        self.identity = identity
        self.root_attestation = root_attestation
        self.lock_descriptor = lock_descriptor

    @classmethod
    def acquire(cls, root: Path) -> "CacheLock":
        root_attestation: CacheRootAttestation | None = None
        try:
            root_attestation = _attest_cache_root(root, create=True)
        except (OSError, StockError) as error:
            raise StockError(
                "cache_locked", "Не удалось установить блокировку кэша", 6
            ) from error
        path = root / ".refresh.lock"
        token = uuid.uuid4().hex
        candidate = root / f".refresh.lock.init-{token}"
        published = False
        acquired: CacheLock | None = None
        candidate_descriptor = -1
        try:
            try:
                root_attestation.assert_current()
                candidate_descriptor = _create_private_child_directory_descriptor(
                    root_attestation.descriptor, candidate.name
                )
                owner = {"token": token, "created_at": time.time()}
                _write_json_atomic_at(candidate_descriptor, "owner.json", owner)
                _write_bytes_atomic_at(
                    candidate_descriptor, f"heartbeat-{token}", b""
                )
                _fsync_directory_descriptor(candidate_descriptor)
            except StockError as error:
                raise StockError(
                    "cache_locked", "Не удалось установить блокировку кэша", 6
                ) from error
            except OSError as error:
                raise StockError(
                    "cache_locked", "Не удалось установить блокировку кэша", 6
                ) from error
            candidate_stat = os.fstat(candidate_descriptor)
            candidate_identity = (candidate_stat.st_dev, candidate_stat.st_ino)

            try:
                with _RefreshLockPublishLock.acquire(root, root_attestation):
                    root_attestation.assert_current()
                    cls._cleanup_stale_init_directories(
                        root, candidate.name, root_attestation
                    )
                    observed_lock = _lstat_private_child(
                        root_attestation.descriptor,
                        root,
                        path.name,
                        missing_ok=True,
                    )
                    if observed_lock is not None:
                        if not stat.S_ISDIR(observed_lock.st_mode):
                            raise StockError(
                                "cache_locked", "Обновление кэша уже выполняется", 6
                            )
                        if not cls._reclaim_stale_attested(
                            root, path.name, root_attestation
                        ):
                            remaining = _lstat_private_child(
                                root_attestation.descriptor,
                                root,
                                path.name,
                                missing_ok=True,
                            )
                            if remaining is not None:
                                raise StockError(
                                    "cache_locked",
                                    "Обновление кэша уже выполняется",
                                    6,
                                )
                    try:
                        root_attestation.assert_current()
                        os.rename(
                            candidate.name,
                            path.name,
                            src_dir_fd=root_attestation.descriptor,
                            dst_dir_fd=root_attestation.descriptor,
                        )
                    except OSError as error:
                        raise StockError(
                            "cache_locked",
                            "Не удалось установить блокировку кэша",
                            6,
                        ) from error
                    published = True
                    try:
                        _fsync_directory_descriptor(root_attestation.descriptor)
                        published_entry = _lstat_private_child(
                            root_attestation.descriptor,
                            root,
                            path.name,
                            missing_ok=True,
                        )
                        if (
                            published_entry is None
                            or (published_entry.st_dev, published_entry.st_ino)
                            != candidate_identity
                        ):
                            raise StockError(
                                "cache_locked",
                                "Право на обновление кэша было утрачено",
                                6,
                            )
                        root_attestation.assert_current()
                        acquired = cls(
                            path,
                            token,
                            candidate_identity,
                            root_attestation,
                            candidate_descriptor,
                        )
                        acquired.assert_owned()
                        candidate_descriptor = -1
                        root_attestation = None
                    except BaseException:
                        discarded = False
                        try:
                            discarded = cls._discard_published_lock_if_owned_locked(
                                path,
                                token,
                                candidate_identity,
                                root_attestation,
                            )
                        except BaseException:
                            pass
                        if discarded:
                            published = False
                        raise
            except BaseException:
                if published:
                    if cls._discard_published_lock_if_owned(
                        root,
                        path,
                        token,
                        candidate_identity,
                        root_attestation,
                    ):
                        published = False
                raise
            if acquired is None:
                raise StockError(
                    "cache_locked", "Не удалось установить блокировку кэша", 6
                )
            return acquired
        finally:
            if not published and root_attestation is not None:
                try:
                    root_attestation.assert_current()
                    observed_candidate = _lstat_private_child(
                        root_attestation.descriptor,
                        root,
                        candidate.name,
                        missing_ok=True,
                    )
                    if observed_candidate is not None and stat.S_ISDIR(
                        observed_candidate.st_mode
                    ):
                        _remove_private_child_directory(
                            root_attestation.descriptor,
                            root,
                            candidate.name,
                            observed_candidate,
                        )
                except StockError:
                    pass
            if root_attestation is not None:
                root_attestation.close()
            _close_optional_descriptor(
                candidate_descriptor if candidate_descriptor >= 0 else None
            )

    @classmethod
    def _discard_published_lock_if_owned(
        cls,
        root: Path,
        path: Path,
        token: str,
        identity: tuple[int, int],
        root_attestation: CacheRootAttestation | None = None,
    ) -> bool:
        try:
            with _RefreshLockPublishLock.acquire(root, root_attestation):
                return cls._discard_published_lock_if_owned_locked(
                    path, token, identity, root_attestation
                )
        except BaseException:
            return False

    @classmethod
    def _discard_published_lock_if_owned_locked(
        cls,
        path: Path,
        token: str,
        identity: tuple[int, int],
        root_attestation: CacheRootAttestation | None = None,
    ) -> bool:
        root = path.parent
        if root_attestation is None or root_attestation.descriptor is None:
            return False
        root_descriptor = root_attestation.descriptor
        lock_descriptor = -1
        try:
            canonical = _lstat_private_child(
                root_descriptor,
                root,
                path.name,
                missing_ok=True,
            )
            if (
                canonical is None
                or not stat.S_ISDIR(canonical.st_mode)
                or (canonical.st_dev, canonical.st_ino) != identity
            ):
                return False
            lock_descriptor = _open_private_child_directory_for_deletion(
                root_descriptor,
                path.name,
                canonical,
            )
            observed = cls._read_owner_from_directory_descriptor(
                lock_descriptor,
                canonical,
            )
            if observed is None or observed[0] != token:
                return False
            quarantine = path.with_name(
                f"{path.name}.abort-{token}-{uuid.uuid4().hex}"
            )
            try:
                if root_attestation is not None:
                    root_attestation.assert_current()
                os.rename(
                    path.name,
                    quarantine.name,
                    src_dir_fd=root_descriptor,
                    dst_dir_fd=root_descriptor,
                )
            except OSError:
                return False
            moved_identity = _lstat_private_child(
                root_descriptor,
                root,
                quarantine.name,
                missing_ok=True,
            )
            if (
                moved_identity is None
                or not stat.S_ISDIR(moved_identity.st_mode)
                or (moved_identity.st_dev, moved_identity.st_ino) != identity
            ):
                return False
            moved_owner = cls._read_owner_from_directory_descriptor(
                lock_descriptor,
                moved_identity,
            )
            if moved_owner is None or moved_owner[0] != token:
                return False
            return False
        except (OSError, StockError):
            return False
        finally:
            _close_optional_descriptor(
                lock_descriptor if lock_descriptor >= 0 else None
            )

    @classmethod
    def _reclaim_stale_attested(
        cls,
        root: Path,
        name: str,
        root_attestation: CacheRootAttestation,
    ) -> bool:
        root_attestation.assert_current()
        observed = _lstat_private_child(
            root_attestation.descriptor, root, name, missing_ok=True
        )
        if observed is None or not stat.S_ISDIR(observed.st_mode):
            return observed is None
        directory_descriptor = -1
        try:
            directory_descriptor = _open_private_child_directory_for_deletion(
                root_attestation.descriptor, name, observed
            )
            inventory = _snapshot_private_flat_directory_descriptor(
                directory_descriptor
            )
            if inventory is None:
                return False
            owner = cls._read_owner_from_directory_descriptor(
                directory_descriptor, observed
            )
            now = time.time()
            if owner is None:
                if time.time_ns() - observed.st_mtime_ns <= int(
                    _LOCK_TTL_SECONDS * 1_000_000_000
                ):
                    return False
            elif now - owner[1] <= _LOCK_TTL_SECONDS:
                return False
            confirmed = os.fstat(directory_descriptor)
            if not _same_file_identity(observed, confirmed):
                return False
            if owner is not None:
                repeated = cls._read_owner_from_directory_descriptor(
                    directory_descriptor, confirmed
                )
                if (
                    repeated is None
                    or repeated[0] != owner[0]
                    or time.time() - repeated[1] <= _LOCK_TTL_SECONDS
                ):
                    return False
            quarantine_name = f"{name}.reclaim-{uuid.uuid4().hex}"
            root_attestation.assert_current()
            os.rename(
                name,
                quarantine_name,
                src_dir_fd=root_attestation.descriptor,
                dst_dir_fd=root_attestation.descriptor,
            )
            moved = _lstat_private_child(
                root_attestation.descriptor,
                root,
                quarantine_name,
                missing_ok=True,
            )
            if (
                moved is None
                or not stat.S_ISDIR(moved.st_mode)
                or not _same_file_identity(observed, moved)
                or _snapshot_private_flat_directory_descriptor(
                    directory_descriptor
                )
                != inventory
            ):
                return False
            root_attestation.assert_current()
            return _remove_private_child_directory(
                root_attestation.descriptor,
                root,
                quarantine_name,
                moved,
            )
        except (OSError, StockError):
            return False
        finally:
            _close_optional_descriptor(
                directory_descriptor if directory_descriptor >= 0 else None
            )

    @classmethod
    def _reclaim_stale(
        cls,
        path: Path,
        root_attestation: CacheRootAttestation | None = None,
    ) -> bool:
        if cls._contains_unsafe_lock_artifact(path):
            return False
        observed = cls._read_owner(path)
        if observed is None:
            return cls._reclaim_stale_ownerless(path, root_attestation)
        observed_token, created_at = observed
        if time.time() - created_at <= _LOCK_TTL_SECONDS:
            return False

        confirmed = cls._read_owner(path)
        if (
            confirmed is None
            or confirmed[0] != observed_token
            or time.time() - confirmed[1] <= _LOCK_TTL_SECONDS
        ):
            return False

        quarantine = path.with_name(f"{path.name}.reclaim-{uuid.uuid4().hex}")
        try:
            if root_attestation is not None:
                root_attestation.assert_current()
            os.rename(path, quarantine)
        except OSError:
            return False

        moved = cls._read_owner(quarantine)
        if (
            moved is None
            or moved[0] != observed_token
            or time.time() - moved[1] <= _LOCK_TTL_SECONDS
        ):
            return False
        if root_attestation is not None:
            root_attestation.assert_current()
        _remove_private_directory(quarantine)
        return True

    @classmethod
    def _reclaim_stale_ownerless(
        cls,
        path: Path,
        root_attestation: CacheRootAttestation | None = None,
    ) -> bool:
        observed = cls._directory_identity(path)
        if observed is None or time.time_ns() - observed[2] <= int(
            _LOCK_TTL_SECONDS * 1_000_000_000
        ):
            return False
        if cls._directory_identity(path) != observed:
            return False

        quarantine = path.with_name(f"{path.name}.reclaim-{uuid.uuid4().hex}")
        try:
            if root_attestation is not None:
                root_attestation.assert_current()
            os.rename(path, quarantine)
        except OSError:
            return False
        if cls._directory_identity(quarantine) != observed:
            return False
        if root_attestation is not None:
            root_attestation.assert_current()
        _remove_private_directory(quarantine)
        return True

    @staticmethod
    def _directory_identity(path: Path) -> tuple[int, int, int] | None:
        try:
            value = path.stat(follow_symlinks=False)
        except OSError:
            return None
        if not stat.S_ISDIR(value.st_mode):
            return None
        return value.st_dev, value.st_ino, value.st_mtime_ns

    @staticmethod
    def _directory_fencing_identity(path: Path) -> tuple[int, int] | None:
        try:
            value = path.stat(follow_symlinks=False)
        except OSError:
            return None
        if not stat.S_ISDIR(value.st_mode):
            return None
        return value.st_dev, value.st_ino

    @staticmethod
    def _contains_unsafe_lock_artifact(path: Path) -> bool:
        try:
            entries = list(path.iterdir())
        except OSError:
            return True
        for entry in entries:
            if entry.name != "owner.json" and not entry.name.startswith("heartbeat-"):
                continue
            try:
                observed = entry.lstat()
            except OSError:
                return True
            if not stat.S_ISREG(observed.st_mode):
                return True
        return False

    @classmethod
    def _cleanup_stale_init_directories(
        cls,
        root: Path,
        current_candidate_name: str,
        root_attestation: CacheRootAttestation | None = None,
    ) -> None:
        if root_attestation is None or root_attestation.descriptor is None:
            raise StockError(
                "cache_locked", "Не удалось установить блокировку кэша", 6
            )
        try:
            names = _list_private_directory_descriptor(
                root_attestation.descriptor
            )
        except OSError as error:
            raise StockError(
                "cache_locked", "Не удалось установить блокировку кэша", 6
            ) from error
        for name in names:
            if (
                name == current_candidate_name
                or not _LOCK_INIT_DIRECTORY.fullmatch(name)
            ):
                continue
            observed = _lstat_private_child(
                root_attestation.descriptor,
                root,
                name,
                missing_ok=True,
            )
            if observed is None:
                continue
            if not stat.S_ISDIR(observed.st_mode):
                raise StockError(
                    "cache_locked", "Обновление кэша уже выполняется", 6
                )
            if not cls._reclaim_stale_attested(root, name, root_attestation):
                remaining = _lstat_private_child(
                    root_attestation.descriptor,
                    root,
                    name,
                    missing_ok=True,
                )
                if remaining is not None:
                    raise StockError(
                        "cache_locked", "Обновление кэша уже выполняется", 6
                    )

    @staticmethod
    def _read_owner(path: Path) -> tuple[str, float] | None:
        try:
            value = json.loads(_read_private_text(path / "owner.json"))
        except (OSError, StockError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        token = value.get("token")
        created_at = value.get("created_at")
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(created_at, (int, float))
            or isinstance(created_at, bool)
        ):
            return None
        try:
            timestamp = float(created_at)
        except (TypeError, ValueError, OverflowError):
            return None
        if (
            not math.isfinite(timestamp)
            or timestamp > time.time() + _LOCK_FUTURE_SKEW_SECONDS
        ):
            return None
        heartbeat_path = path / f"heartbeat-{token}"
        try:
            heartbeat = _stat_private_regular_file(heartbeat_path)
        except (OSError, StockError):
            heartbeat = None
        if heartbeat is not None:
            heartbeat_timestamp = heartbeat.st_mtime
            if (
                not math.isfinite(heartbeat_timestamp)
                or heartbeat_timestamp > time.time() + _LOCK_FUTURE_SKEW_SECONDS
            ):
                return None
            timestamp = max(timestamp, heartbeat_timestamp)
        return token, timestamp

    @staticmethod
    def _read_owner_from_directory_descriptor(
        directory_descriptor: int,
        expected: os.stat_result,
    ) -> tuple[str, float] | None:
        try:
            opened_directory = os.fstat(directory_descriptor)
            if not stat.S_ISDIR(opened_directory.st_mode) or not _same_file_identity(
                expected,
                opened_directory,
            ):
                return None
            value = json.loads(
                _read_private_child_text(directory_descriptor, "owner.json")
            )
        except (
            OSError,
            StockError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            return None
        if not isinstance(value, dict):
            return None
        token = value.get("token")
        created_at = value.get("created_at")
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(created_at, (int, float))
            or isinstance(created_at, bool)
        ):
            return None
        try:
            timestamp = float(created_at)
        except (TypeError, ValueError, OverflowError):
            return None
        if (
            not math.isfinite(timestamp)
            or timestamp > time.time() + _LOCK_FUTURE_SKEW_SECONDS
        ):
            return None
        try:
            heartbeat = _stat_private_child_regular_file(
                directory_descriptor,
                f"heartbeat-{token}",
            )
        except (OSError, StockError):
            heartbeat = None
        if heartbeat is not None:
            heartbeat_timestamp = heartbeat.st_mtime
            if (
                not math.isfinite(heartbeat_timestamp)
                or heartbeat_timestamp > time.time() + _LOCK_FUTURE_SKEW_SECONDS
            ):
                return None
            timestamp = max(timestamp, heartbeat_timestamp)
        try:
            confirmed_directory = os.fstat(directory_descriptor)
        except OSError:
            return None
        if not stat.S_ISDIR(
            confirmed_directory.st_mode
        ) or not _same_file_identity(expected, confirmed_directory):
            return None
        return token, timestamp

    def assert_owned(self) -> None:
        try:
            self.root_attestation.assert_current()
            observed = _lstat_private_child(
                self.root_attestation.descriptor,
                self.path.parent,
                self.path.name,
                missing_ok=True,
            )
            opened = os.fstat(self.lock_descriptor)
        except StockError:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise StockError(
                "cache_locked", "Право на обновление кэша было утрачено", 6
            ) from error
        if (
            observed is None
            or (observed.st_dev, observed.st_ino) != self.identity
            or (opened.st_dev, opened.st_ino) != self.identity
        ):
            raise StockError(
                "cache_locked", "Право на обновление кэша было утрачено", 6
            )
        owner = self._read_owner_from_directory_descriptor(
            self.lock_descriptor, opened
        )
        if owner is None or owner[0] != self.token:
            raise StockError(
                "cache_locked", "Право на обновление кэша было утрачено", 6
            )

    def heartbeat(self) -> None:
        self.assert_owned()
        try:
            os.utime(
                f"heartbeat-{self.token}",
                None,
                dir_fd=self.lock_descriptor,
                follow_symlinks=False,
            )
        except (OSError, StockError) as error:
            raise StockError(
                "cache_locked", "Право на обновление кэша было утрачено", 6
            ) from error
        self.assert_owned()

    def release(self) -> None:
        try:
            self.root_attestation.assert_current()
            observed = _lstat_private_child(
                self.root_attestation.descriptor,
                self.path.parent,
                self.path.name,
                missing_ok=True,
            )
            if observed is None or (observed.st_dev, observed.st_ino) != self.identity:
                return
            owner = self._read_owner_from_directory_descriptor(
                self.lock_descriptor, os.fstat(self.lock_descriptor)
            )
            if owner is None or owner[0] != self.token:
                return

            quarantine = self.path.with_name(
                f"{self.path.name}.release-{self.token}-{uuid.uuid4().hex}"
            )
            try:
                self.root_attestation.assert_current()
                os.rename(
                    self.path.name,
                    quarantine.name,
                    src_dir_fd=self.root_attestation.descriptor,
                    dst_dir_fd=self.root_attestation.descriptor,
                )
            except (OSError, StockError):
                return

            moved_entry = _lstat_private_child(
                self.root_attestation.descriptor,
                self.path.parent,
                quarantine.name,
                missing_ok=True,
            )
            if (
                moved_entry is None
                or not stat.S_ISDIR(moved_entry.st_mode)
                or (moved_entry.st_dev, moved_entry.st_ino) != self.identity
            ):
                return
            moved = self._read_owner_from_directory_descriptor(
                self.lock_descriptor, moved_entry
            )
            if moved is not None and moved[0] == self.token:
                self.root_attestation.assert_current()
                _remove_private_child_directory(
                    self.root_attestation.descriptor,
                    self.path.parent,
                    quarantine.name,
                    moved_entry,
                )
        except StockError:
            return
        finally:
            _close_optional_descriptor(
                self.lock_descriptor if self.lock_descriptor >= 0 else None
            )
            self.lock_descriptor = -1
            self.root_attestation.close()

    def __enter__(self) -> "CacheLock":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class _WindowsCacheLock:
    def __init__(
        self,
        path: Path,
        token: str,
        identity: Any,
        root_attestation: CacheRootAttestation,
        owner_payload: bytes,
        inventory: dict[str, Any],
        recovery_warning: str | None = None,
    ) -> None:
        self.path = path
        self.token = token
        self.identity = identity
        self.root_attestation = root_attestation
        self.owner_payload = owner_payload
        self.inventory = inventory
        self.recovery_warning = recovery_warning
        self.closed = False

    @property
    def session(self) -> Any:
        return _windows_session(self.root_attestation)

    @staticmethod
    def _owner_value(payload: bytes) -> tuple[str, float]:
        value = _parse_windows_json(payload)
        if not isinstance(value, dict) or set(value) != {"token", "created_at"}:
            raise _cache_unavailable()
        token = value.get("token")
        created_at = value.get("created_at")
        if (
            not isinstance(token, str)
            or not _RUNTIME_REVISION.fullmatch(token)
            or not isinstance(created_at, (int, float))
            or isinstance(created_at, bool)
        ):
            raise _cache_unavailable()
        timestamp = float(created_at)
        if (
            not math.isfinite(timestamp)
            or timestamp > time.time() + _LOCK_FUTURE_SKEW_SECONDS
        ):
            raise _cache_unavailable()
        return token, timestamp

    @classmethod
    def _snapshot(
        cls, session: Any, name: str
    ) -> _WindowsLockSnapshot:
        identity = session.directory_identity((name,))
        inventory = session.snapshot_flat_directory((name,))
        if not inventory:
            timestamp = session.last_write_time(
                (), name, directory=True, expected=identity
            )
            if (
                not math.isfinite(timestamp)
                or timestamp > time.time() + _LOCK_FUTURE_SKEW_SECONDS
            ):
                raise _cache_unavailable()
            return _WindowsLockSnapshot(
                identity=identity,
                token=None,
                timestamp=timestamp,
                inventory=inventory,
                payloads={},
                write_times={},
            )
        owner_payload = session.read_file(
            (name,), "owner.json", _WINDOWS_OWNER_MAX_BYTES
        )
        token, created_at = cls._owner_value(owner_payload)
        heartbeat_name = f"heartbeat-{token}"
        if set(inventory) != {"owner.json", heartbeat_name}:
            raise _cache_unavailable()
        heartbeat_payload = session.read_file((name,), heartbeat_name, 0)
        heartbeat_time = session.last_write_time(
            (name,),
            heartbeat_name,
            directory=False,
            expected=inventory[heartbeat_name],
        )
        if (
            not math.isfinite(heartbeat_time)
            or heartbeat_time > time.time() + _LOCK_FUTURE_SKEW_SECONDS
        ):
            raise _cache_unavailable()
        if (
            session.read_file((name,), "owner.json", _WINDOWS_OWNER_MAX_BYTES)
            != owner_payload
            or session.directory_identity((name,)) != identity
        ):
            raise _cache_unavailable()
        return _WindowsLockSnapshot(
            identity=identity,
            token=token,
            timestamp=max(created_at, heartbeat_time),
            inventory=inventory,
            payloads={"owner.json": owner_payload, heartbeat_name: heartbeat_payload},
            write_times={heartbeat_name: heartbeat_time},
        )

    @staticmethod
    def _root_names(session: Any) -> list[str]:
        names = session.list_directory(())
        if len(names) > _WINDOWS_MAX_ROOT_ENTRIES:
            raise _cache_unavailable()
        return names

    @classmethod
    def _recover_owned_artifacts(
        cls,
        session: Any,
        *,
        token: str | None = None,
        attempts: int = 3,
    ) -> str | None:
        cleanup_incomplete = False
        for _ in range(attempts):
            candidates: list[tuple[str, str | None, str]] = []
            names = cls._root_names(session)
            rejected = [
                name
                for name in names
                if _WINDOWS_LOCK_REJECTED_DIRECTORY.fullmatch(name)
            ]
            if rejected:
                cleanup_incomplete = True
            for name in names:
                release = _WINDOWS_LOCK_RELEASE_DIRECTORY.fullmatch(name)
                reclaim = _WINDOWS_LOCK_RECLAIM_DIRECTORY.fullmatch(name)
                if release is not None:
                    candidate_token = release.group("token")
                    kind = "release"
                elif reclaim is not None:
                    candidate_token = reclaim.group("token")
                    kind = "reclaim"
                else:
                    continue
                if token is not None and candidate_token != token:
                    continue
                candidates.append((name, candidate_token, kind))
            if not candidates:
                return "cache_cleanup_incomplete" if cleanup_incomplete else None
            if (
                len(candidates) + len(rejected)
                > _WINDOWS_MAX_RECOVERY_ARTIFACTS
            ):
                return "cache_cleanup_incomplete"
            for name, candidate_token, kind in candidates:
                try:
                    snapshot = cls._snapshot(session, name)
                    if candidate_token == "ownerless":
                        owned = snapshot.token is None and not snapshot.inventory
                    else:
                        owned = snapshot.token == candidate_token
                    if not owned:
                        cleanup_incomplete = True
                        continue
                    quarantine_name = (
                        f".refresh.lock.rejected-{kind}-{uuid.uuid4().hex}"
                    )
                    removed = session.delete_flat_directory(
                        (),
                        name,
                        snapshot.identity,
                        quarantine_name=quarantine_name,
                        expected_inventory=snapshot.inventory,
                        expected_payloads=snapshot.payloads,
                        expected_write_times=snapshot.write_times,
                    )
                    if not removed:
                        cleanup_incomplete = True
                except (OSError, StockError, ValueError, TypeError):
                    cleanup_incomplete = True
        remaining = [
            name
            for name in cls._root_names(session)
            if _WINDOWS_LOCK_REJECTED_DIRECTORY.fullmatch(name) is not None
            or (
                (match := _WINDOWS_LOCK_RELEASE_DIRECTORY.fullmatch(name))
                is not None
                and (token is None or match.group("token") == token)
            )
            or (
                (match := _WINDOWS_LOCK_RECLAIM_DIRECTORY.fullmatch(name))
                is not None
                and (token is None or match.group("token") == token)
            )
        ]
        return "cache_cleanup_incomplete" if remaining or cleanup_incomplete else None

    @classmethod
    def _cleanup_stale_init_directories(
        cls, session: Any, current_candidate: str
    ) -> str | None:
        cleanup_incomplete = False
        candidates = [
            name
            for name in cls._root_names(session)
            if name != current_candidate and _LOCK_INIT_DIRECTORY.fullmatch(name)
        ]
        if len(candidates) > _WINDOWS_MAX_RECOVERY_ARTIFACTS:
            return "cache_cleanup_incomplete"
        for name in candidates:
            try:
                snapshot = cls._snapshot(session, name)
                if time.time() - snapshot.timestamp <= _LOCK_TTL_SECONDS:
                    continue
                suffix_token = snapshot.token or "ownerless"
                removed = session.delete_flat_directory(
                    (),
                    name,
                    snapshot.identity,
                    quarantine_name=(
                        f".refresh.lock.reclaim-{suffix_token}-{uuid.uuid4().hex}"
                    ),
                    expected_inventory=snapshot.inventory,
                    expected_payloads=snapshot.payloads,
                    expected_write_times=snapshot.write_times,
                )
                if not removed:
                    cleanup_incomplete = True
            except (OSError, StockError, ValueError, TypeError):
                cleanup_incomplete = True
        return "cache_cleanup_incomplete" if cleanup_incomplete else None

    @classmethod
    def _reclaim_stale(cls, session: Any) -> tuple[bool, str | None]:
        try:
            snapshot = cls._snapshot(session, ".refresh.lock")
        except FileNotFoundError:
            return True, None
        except (OSError, StockError, ValueError, TypeError):
            return False, None
        if time.time() - snapshot.timestamp <= _LOCK_TTL_SECONDS:
            return False, None
        suffix_token = snapshot.token or "ownerless"
        warning: str | None = None
        try:
            removed = session.delete_flat_directory(
                (),
                ".refresh.lock",
                snapshot.identity,
                quarantine_name=(
                    f".refresh.lock.reclaim-{suffix_token}-{uuid.uuid4().hex}"
                ),
                expected_inventory=snapshot.inventory,
                expected_payloads=snapshot.payloads,
                expected_write_times=snapshot.write_times,
            )
            if not removed:
                warning = "cache_cleanup_incomplete"
        except (OSError, StockError, ValueError, TypeError):
            warning = "cache_cleanup_incomplete"
        if ".refresh.lock" in cls._root_names(session):
            return False, warning
        recovery = cls._recover_owned_artifacts(session)
        return True, recovery or warning

    @classmethod
    def acquire(cls, root: Path) -> "_WindowsCacheLock":
        attestation: CacheRootAttestation | None = _attest_cache_root(
            root, create=True
        )
        session = _windows_session(attestation)
        token = uuid.uuid4().hex
        candidate_name = f".refresh.lock.init-{token}"
        canonical_name = ".refresh.lock"
        candidate_identity: Any | None = None
        owner_payload = _json_payload({"token": token, "created_at": time.time()})
        heartbeat_name = f"heartbeat-{token}"
        inventory: dict[str, Any] = {}
        published = False
        recovery_warning: str | None = None
        try:
            recovery_warning = cls._recover_owned_artifacts(session)
            candidate_identity = session.create_directory((), candidate_name)
            owner_identity = session.write_new_file(
                (candidate_name,), "owner.json", owner_payload
            )
            inventory["owner.json"] = owner_identity
            heartbeat_identity = session.write_new_file(
                (candidate_name,), heartbeat_name, b""
            )
            inventory[heartbeat_name] = heartbeat_identity
            init_warning = cls._cleanup_stale_init_directories(
                session, candidate_name
            )
            recovery_warning = recovery_warning or init_warning
            if canonical_name in cls._root_names(session):
                reclaimed, reclaim_warning = cls._reclaim_stale(session)
                recovery_warning = recovery_warning or reclaim_warning
                if not reclaimed:
                    raise StockError(
                        "cache_locked", "Обновление кэша уже выполняется", 6
                    )
            try:
                session.rename_directory(
                    (), candidate_name, candidate_identity, canonical_name
                )
                published = True
            except OSError as error:
                if canonical_name in cls._root_names(session):
                    try:
                        canonical_is_candidate = (
                            session.directory_identity((canonical_name,))
                            == candidate_identity
                        )
                    except (OSError, StockError, ValueError, TypeError):
                        canonical_is_candidate = False
                    if canonical_is_candidate:
                        published = True
                        raise
                    raise StockError(
                        "cache_locked", "Обновление кэша уже выполняется", 6
                    ) from error
                raise
            result = cls(
                root / canonical_name,
                token,
                candidate_identity,
                attestation,
                owner_payload,
                inventory,
                recovery_warning,
            )
            result.assert_owned()
            attestation = None  # type: ignore[assignment]
            return result
        except StockError:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise StockError(
                "cache_locked", "Не удалось установить блокировку кэша", 6
            ) from error
        finally:
            if attestation is not None and candidate_identity is not None:
                try:
                    cleanup_name = canonical_name if published else candidate_name
                    if cleanup_name in cls._root_names(session):
                        session.delete_flat_directory(
                            (),
                            cleanup_name,
                            candidate_identity,
                            quarantine_name=(
                                f".refresh.lock.release-{token}-{uuid.uuid4().hex}"
                            ),
                            expected_inventory=inventory,
                            expected_payloads={
                                name: owner_payload if name == "owner.json" else b""
                                for name in inventory
                            },
                        )
                    cls._recover_owned_artifacts(session, token=token)
                except (OSError, StockError, ValueError, TypeError):
                    pass
            if attestation is not None:
                attestation.close()

    def assert_owned(self) -> None:
        try:
            self.root_attestation.assert_current()
            snapshot = self._snapshot(self.session, ".refresh.lock")
        except (OSError, StockError, ValueError, TypeError) as error:
            raise StockError(
                "cache_locked", "Право на обновление кэша было утрачено", 6
            ) from error
        if (
            snapshot.identity != self.identity
            or snapshot.token != self.token
            or snapshot.payloads.get("owner.json") != self.owner_payload
            or snapshot.inventory != self.inventory
        ):
            raise StockError(
                "cache_locked", "Право на обновление кэша было утрачено", 6
            )

    def heartbeat(self) -> None:
        self.assert_owned()
        heartbeat_name = f"heartbeat-{self.token}"
        try:
            self.session.touch_file(
                (".refresh.lock",),
                heartbeat_name,
                self.inventory[heartbeat_name],
            )
        except (OSError, StockError, ValueError, TypeError) as error:
            raise StockError(
                "cache_locked", "Право на обновление кэша было утрачено", 6
            ) from error
        self.assert_owned()

    def release(self) -> str | None:
        if self.closed:
            return self.recovery_warning
        self.closed = True
        warning = self.recovery_warning
        heartbeat_name = f"heartbeat-{self.token}"
        try:
            self.assert_owned()
            self.session.delete_flat_directory(
                (),
                ".refresh.lock",
                self.identity,
                quarantine_name=(
                    f".refresh.lock.release-{self.token}-{uuid.uuid4().hex}"
                ),
                expected_inventory=self.inventory,
                expected_payloads={
                    "owner.json": self.owner_payload,
                    heartbeat_name: b"",
                },
            )
        except (OSError, StockError, ValueError, TypeError):
            # The namespace is re-inventoried below; only exact owned artifacts
            # are eligible for bounded recovery.
            pass
        try:
            recovered = self._recover_owned_artifacts(
                self.session, token=self.token
            )
            warning = warning or recovered
            if ".refresh.lock" in self._root_names(self.session):
                warning = "cache_cleanup_incomplete"
        except (OSError, StockError, ValueError, TypeError):
            warning = "cache_cleanup_incomplete"
        finally:
            try:
                self.root_attestation.close()
            except StockError:
                warning = "cache_cleanup_incomplete"
        self.recovery_warning = warning
        return warning

    def __enter__(self) -> "_WindowsCacheLock":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class StockCache:
    def __init__(self, root: Path, client: SafeHttpClient) -> None:
        self.root = root
        self.client = client

    def refresh(self, config: StockConfig) -> RefreshResult:
        if _is_native_windows():
            return self._refresh_windows(config)
        previous: CacheState | None = None
        try:
            with CacheLock.acquire(self.root) as lock:
                previous = self._load_if_readable(lock.heartbeat)
                response = self.client.get_manifest(
                    previous.manifest_etag if previous else None,
                    previous.manifest_last_modified if previous else None,
                )
                if response.status == 304:
                    if previous is None:
                        raise _cache_unavailable()
                    lock.assert_owned()
                    current = self._record_runtime_status(
                        previous, False, None, lock
                    )
                    return RefreshResult.from_state("not_modified", current)
                if response.status != 200:
                    raise StockError(
                        "network_error", "Не удалось получить manifest", 3
                    )

                manifest = Manifest.parse(response.body)
                cleanup_warning = self._cleanup_inactive_generations(lock)
                if cleanup_warning is not None:
                    if previous is not None:
                        lock.assert_owned()
                        current = self._record_runtime_status(
                            previous, True, cleanup_warning, lock
                        )
                        return RefreshResult.from_state(
                            "stale_cache",
                            current,
                            stale=True,
                            warning_code=cleanup_warning,
                        )
                    raise _cache_unavailable()
                lock.heartbeat()
                staged = self._download_generation(manifest, config, lock)
                try:
                    self._verify_generation(staged, manifest, lock)
                    state = self._activate(staged, manifest, response, lock)
                except BaseException:
                    self._remove_generation_if_inactive(staged.manifest.parent)
                    raise
                cleanup_warning = self._cleanup_inactive_generations(lock)
                return RefreshResult.from_state(
                    "updated", state, warning_code=cleanup_warning
                )
        except StockError as error:
            fallback = previous if previous is not None else self._load_if_readable()
            if fallback is not None:
                return self._stale_fallback(fallback, error.code)
            raise
        except OSError as error:
            failure = _cache_unavailable()
            fallback = previous if previous is not None else self._load_if_readable()
            if fallback is not None:
                return self._stale_fallback(fallback, failure.code)
            raise failure from error

    def _refresh_windows(self, config: StockConfig) -> RefreshResult:
        previous: CacheState | None = None
        lock: _WindowsCacheLock | None = None
        try:
            lock = _WindowsCacheLock.acquire(self.root)
            previous = self._load_if_readable(lock.heartbeat)
            result = self._refresh_windows_locked(config, lock, previous)
        except _WindowsRollbackUnconfirmed as error:
            if lock is not None:
                lock.release()
            raise _cache_unavailable() from error
        except StockError as error:
            fallback = self._finalize_windows_failure(
                lock, previous, error.code
            )
            if fallback is not None:
                return fallback
            raise
        except OSError as error:
            failure = _cache_unavailable()
            fallback = self._finalize_windows_failure(
                lock, previous, failure.code
            )
            if fallback is not None:
                return fallback
            raise failure from error
        except BaseException:
            if lock is not None:
                lock.release()
            raise
        release_warning = lock.release()
        if release_warning is not None:
            result = replace(result, warning_code=release_warning)
        return result

    def _finalize_windows_failure(
        self,
        lock: _WindowsCacheLock | None,
        previous: CacheState | None,
        warning_code: str,
    ) -> RefreshResult | None:
        persisted = previous
        persistence_error: StockError | None = None
        release_warning: str | None = None
        try:
            if (
                lock is not None
                and persisted is not None
                and warning_code != "cache_locked"
            ):
                try:
                    persisted = self._record_runtime_status_windows(
                        persisted, True, warning_code, lock
                    )
                except StockError as error:
                    persistence_error = error
                except OSError:
                    persistence_error = _cache_unavailable()
        finally:
            if lock is not None:
                release_warning = lock.release()
        if (
            persisted is None
            or warning_code == "cache_locked"
            or (
                persistence_error is not None
                and persistence_error.code == "cache_locked"
            )
        ):
            persisted = self._load_if_readable()
        if release_warning is not None and persisted is not None:
            return self._stale_fallback_windows(
                persisted, release_warning
            )
        if (
            persistence_error is not None
            and persistence_error.code != "cache_locked"
        ):
            raise persistence_error
        if persisted is None:
            return None
        return self._stale_fallback_windows(persisted, warning_code)

    def _refresh_windows_locked(
        self,
        config: StockConfig,
        lock: "_WindowsCacheLock",
        previous: CacheState | None,
    ) -> RefreshResult:
        response = self.client.get_manifest(
            previous.manifest_etag if previous else None,
            previous.manifest_last_modified if previous else None,
        )
        if response.status == 304:
            if previous is None:
                raise _cache_unavailable()
            return RefreshResult.from_state(
                "not_modified",
                self._record_runtime_status_windows(previous, False, None, lock),
            )
        if response.status != 200:
            raise StockError("network_error", "Не удалось получить manifest", 3)
        manifest = Manifest.parse(response.body)
        cleanup_warning = self._cleanup_inactive_generations_windows(lock)
        if cleanup_warning is not None:
            if previous is None:
                raise _cache_unavailable()
            current = self._record_runtime_status_windows(
                previous, True, cleanup_warning, lock
            )
            return RefreshResult.from_state(
                "stale_cache",
                current,
                stale=True,
                warning_code=cleanup_warning,
            )
        staged = self._download_generation_windows(manifest, config, lock)
        try:
            self._verify_generation_windows(staged, manifest, lock)
            state = self._activate_windows(staged, manifest, response, lock)
        except BaseException:
            self._remove_generation_windows_if_inactive(staged, lock)
            raise
        cleanup_warning = self._cleanup_inactive_generations_windows(lock)
        return RefreshResult.from_state(
            "updated", state, warning_code=cleanup_warning
        )

    def _record_runtime_status_windows(
        self,
        state: CacheState,
        stale: bool,
        warning_code: str | None,
        lock: "_WindowsCacheLock",
    ) -> CacheState:
        lock.assert_owned()
        session = lock.session
        pointer = self._load_current_pointer_windows_session(session)
        if (
            pointer is None
            or pointer.generation_id != state.generation_id
            or pointer.directory_name != state.directory_name
        ):
            raise StockError("cache_locked", "Активное поколение кэша изменилось", 6)
        generation_ownership = _parse_windows_generation_ownership(
            session.read_file(
                ("generations", state.directory_name),
                _WINDOWS_GENERATION_OWNER_NAME,
                _WINDOWS_GENERATION_OWNER_MAX_BYTES,
            ),
            lock.root_attestation.ownership_token,
        )
        if generation_ownership.generation_id != state.generation_id:
            raise _cache_unavailable()
        path = _runtime_status_path(self.root, state.directory_name)
        previous_payload = session.read_optional_file(
            (), path.name, _WINDOWS_RUNTIME_MAX_BYTES
        )
        if previous_payload is None:
            if state.runtime_revision is not None:
                raise StockError(
                    "cache_locked", "Активное поколение кэша изменилось", 6
                )
        else:
            current_status = _validate_windows_runtime_payload(
                previous_payload,
                expected_generation_id=state.generation_id,
                expected_directory_name=state.directory_name,
                expected_root_token=lock.root_attestation.ownership_token,
                expected_generation_ownership_token=(
                    generation_ownership.ownership_token
                ),
            )
            if (
                current_status.revision != state.runtime_revision
                or current_status.checked_at != state.checked_at
                or current_status.stale != state.stale
                or current_status.warning_code != state.warning_code
            ):
                raise StockError(
                    "cache_locked", "Активное поколение кэша изменилось", 6
                )
        value = _windows_runtime_value(
            {
                "generation_id": state.generation_id,
                "checked_at": (
                    datetime.now(timezone.utc).isoformat()
                    if not stale
                    else state.checked_at
                ),
                "stale": stale,
                "warning_code": warning_code,
                "revision": uuid.uuid4().hex,
            },
            root_ownership_token=lock.root_attestation.ownership_token,
            generation_ownership_token=generation_ownership.ownership_token,
            directory_name=state.directory_name,
        )
        expected_status = validate_runtime_status(value, state.generation_id)
        lock.assert_owned()
        payload = _json_payload(value)
        session.replace_file_cas(
            (),
            path.name,
            expected=previous_payload,
            payload=payload,
        )
        lock.assert_owned()
        committed_pointer = self._load_current_pointer_windows_session(session)
        if (
            committed_pointer is None
            or committed_pointer.generation_id != state.generation_id
            or committed_pointer.directory_name != state.directory_name
            or session.read_file((), path.name, _WINDOWS_RUNTIME_MAX_BYTES)
            != payload
        ):
            raise _cache_unavailable()
        return replace(
            state,
            checked_at=expected_status.checked_at,
            files=replace(state.files, runtime_status=path),
            runtime_revision=expected_status.revision,
            stale=expected_status.stale,
            warning_code=expected_status.warning_code,
        )

    def _stale_fallback_windows(
        self, state: CacheState, warning_code: str
    ) -> RefreshResult:
        return RefreshResult.from_state(
            "stale_cache", state, stale=True, warning_code=warning_code
        )

    def _download_generation_windows(
        self, manifest: Manifest, config: StockConfig, lock: "_WindowsCacheLock"
    ) -> _WindowsGeneration:
        session = lock.session
        lock.assert_owned()
        session.ensure_directory(("generations",))
        directory_name = f".staging-{uuid.uuid4().hex}"
        identity = session.create_directory(("generations",), directory_name)
        directory = self.root / "generations" / directory_name
        ownership_token = uuid.uuid4().hex
        ownership_payload = _json_payload(
            {
                "kind": _WINDOWS_GENERATION_OWNER_KIND,
                "schema_version": _WINDOWS_OWNER_SCHEMA_VERSION,
                "root_ownership_token": lock.root_attestation.ownership_token,
                "generation_id": manifest.generation_id,
                "ownership_token": ownership_token,
            }
        )
        staged = _WindowsGeneration(
            files=GenerationFiles.from_directory(manifest.generation_id, directory),
            identity=identity,
            ownership_token=ownership_token,
        )
        try:
            session.write_new_file(
                ("generations", directory_name),
                _WINDOWS_GENERATION_OWNER_NAME,
                ownership_payload,
            )
            session.write_new_file(
                ("generations", directory_name), "manifest.json", manifest.body
            )
            self._download_private_file_windows(
                _resolve_download_url(config.manifest_url, manifest.products.url),
                staged.files.products,
                manifest.products,
                lock,
            )
            self._download_private_file_windows(
                _resolve_download_url(config.manifest_url, manifest.offers.url),
                staged.files.offers,
                manifest.offers,
                lock,
            )
            return staged
        except BaseException:
            self._remove_generation_windows_if_inactive(staged, lock)
            raise

    def _download_private_file_windows(
        self,
        url: str,
        path: Path,
        expected: ManifestFile,
        lock: "_WindowsCacheLock",
    ) -> None:
        destination = lock.session.create_download_destination(
            ("generations", path.parent.name), path.name
        )
        try:
            self.client.download(
                url,
                destination,
                expected.bytes,
                expected.sha256,
                progress=lock.heartbeat,
            )
            destination.fsync()
        finally:
            destination.close()

    def _verify_generation_windows(
        self,
        staged: _WindowsGeneration,
        manifest: Manifest,
        lock: "_WindowsCacheLock",
    ) -> None:
        lock.assert_owned()
        parts = ("generations", staged.files.manifest.parent.name)
        try:
            lock.session.verify_file(
                parts,
                "products.jsonl",
                expected_bytes=manifest.products.bytes,
                expected_sha256=manifest.products.sha256,
                progress=lock.heartbeat,
            )
            lock.session.verify_file(
                parts,
                "offers.jsonl",
                expected_bytes=manifest.offers.bytes,
                expected_sha256=manifest.offers.sha256,
                progress=lock.heartbeat,
            )
        except StockError as error:
            if error.code == "cache_locked":
                raise
            raise StockError(
                "download_integrity_failed",
                "Проверка загруженного файла не пройдена",
                5,
            ) from error
        except OSError as error:
            raise StockError(
                "download_integrity_failed",
                "Проверка загруженного файла не пройдена",
                5,
            ) from error

    def _activate_windows(
        self,
        staged: _WindowsGeneration,
        manifest: Manifest,
        response: HttpResponse,
        lock: "_WindowsCacheLock",
    ) -> CacheState:
        checked_at = datetime.now(timezone.utc).isoformat()
        final_name = f"generation-{uuid.uuid4().hex}"
        final_directory = self.root / "generations" / final_name
        state_value = {
            "generation_id": manifest.generation_id,
            "generated_at": manifest.generated_at,
            "checked_at": checked_at,
            "manifest_etag": _header(response.headers, "etag"),
            "manifest_last_modified": _header(response.headers, "last-modified"),
            "stale": False,
            "warning_code": None,
        }
        runtime_value = _windows_runtime_value(
            {
                "generation_id": manifest.generation_id,
                "checked_at": checked_at,
                "stale": False,
                "warning_code": None,
                "revision": uuid.uuid4().hex,
            },
            root_ownership_token=lock.root_attestation.ownership_token,
            generation_ownership_token=staged.ownership_token,
            directory_name=final_name,
        )
        lock.assert_owned()
        session = lock.session
        previous_pointer = session.read_optional_file(
            (), "current.json", _WINDOWS_POINTER_MAX_BYTES
        )
        staging_name = staged.files.manifest.parent.name
        session.write_new_file(
            ("generations", staging_name),
            "state.json",
            _json_payload(state_value),
        )
        final_generation = _WindowsGeneration(
            files=GenerationFiles.from_directory(
                manifest.generation_id, final_directory
            ),
            identity=staged.identity,
            ownership_token=staged.ownership_token,
        )
        try:
            session.rename_directory(
                ("generations",),
                staging_name,
                staged.identity,
                final_name,
            )
        except BaseException:
            self._remove_generation_windows_if_inactive(final_generation, lock)
            raise
        pointer = CurrentPointer(manifest.generation_id, final_name, lock.token)
        pointer_payload = _json_payload(pointer.to_dict())
        runtime_name = _runtime_status_path(self.root, final_name).name
        runtime_payload = _json_payload(runtime_value)
        try:
            lock.assert_owned()
            session.replace_file_cas(
                (), runtime_name, expected=None, payload=runtime_payload
            )
            lock.assert_owned()
            session.replace_file_cas(
                (),
                "current.json",
                expected=previous_pointer,
                payload=pointer_payload,
            )
            lock.assert_owned()
            if (
                session.read_file((), "current.json", _WINDOWS_POINTER_MAX_BYTES)
                != pointer_payload
                or session.read_file((), runtime_name, _WINDOWS_RUNTIME_MAX_BYTES)
                != runtime_payload
            ):
                raise _cache_unavailable()
            loaded = CacheState.load(self.root, lock.heartbeat)
            if (
                loaded is None
                or loaded.generation_id != manifest.generation_id
                or loaded.directory_name != final_name
                or loaded.runtime_revision != runtime_value["revision"]
            ):
                raise _cache_unavailable()
            return loaded
        except BaseException as activation_error:
            rollback_confirmed = self._rollback_pointer_windows(
                lock,
                expected_pointer=pointer_payload,
                previous_pointer=previous_pointer,
            )
            if not rollback_confirmed:
                raise _WindowsRollbackUnconfirmed() from activation_error
            self._remove_generation_windows_if_inactive(final_generation, lock)
            raise

    def _rollback_pointer_windows(
        self,
        lock: "_WindowsCacheLock",
        *,
        expected_pointer: bytes,
        previous_pointer: bytes | None,
    ) -> bool:
        try:
            session = lock.session
            lock.root_attestation.assert_current()
            current = session.read_optional_file(
                (), "current.json", _WINDOWS_POINTER_MAX_BYTES
            )
            if current == previous_pointer:
                return True
            if current != expected_pointer:
                return False
            if previous_pointer is None:
                session.delete_file_cas((), "current.json", expected_pointer)
            else:
                session.replace_file_cas(
                    (),
                    "current.json",
                    expected=expected_pointer,
                    payload=previous_pointer,
                )
            return (
                session.read_optional_file(
                    (), "current.json", _WINDOWS_POINTER_MAX_BYTES
                )
                == previous_pointer
            )
        except (OSError, StockError, ValueError, TypeError):
            return False

    @staticmethod
    def _load_current_pointer_windows_session(session: Any) -> CurrentPointer | None:
        payload = session.read_optional_file(
            (), "current.json", _WINDOWS_POINTER_MAX_BYTES
        )
        if payload is None:
            return None
        return CurrentPointer.from_value(_parse_windows_json(payload))

    def _remove_generation_windows_if_inactive(
        self,
        generation: _WindowsGeneration,
        lock: "_WindowsCacheLock",
    ) -> None:
        try:
            lock.assert_owned()
            session = lock.session
            directory_name = generation.files.manifest.parent.name
            pointer = self._load_current_pointer_windows_session(session)
            if pointer is not None and pointer.directory_name == directory_name:
                return
            if "generations" not in session.list_directory(()) or (
                directory_name not in session.list_directory(("generations",))
            ):
                return
            observed = session.directory_identity(("generations", directory_name))
            if observed != generation.identity:
                return
            inventory = session.snapshot_flat_directory(
                ("generations", directory_name)
            )
            lock.assert_owned()
            pointer = self._load_current_pointer_windows_session(session)
            if pointer is not None and pointer.directory_name == directory_name:
                return
            removed = session.delete_flat_directory(
                ("generations",),
                directory_name,
                generation.identity,
                quarantine_name=f".{directory_name}.delete-{uuid.uuid4().hex}",
                expected_inventory=inventory,
            )
            if not removed:
                return
            runtime_name = _runtime_status_path(self.root, directory_name).name
            if runtime_name not in session.list_directory(()) or (
                (pointer := self._load_current_pointer_windows_session(session))
                is not None
                and pointer.directory_name == directory_name
            ):
                return
            runtime_identity = session.file_identity((), runtime_name)
            session.delete_file_identity((), runtime_name, runtime_identity)
        except (OSError, StockError, ValueError, TypeError):
            return

    def _cleanup_inactive_generations_windows(
        self, lock: "_WindowsCacheLock"
    ) -> str | None:
        try:
            lock.assert_owned()
            session = lock.session
            pointer = self._load_current_pointer_windows_session(session)
            root_names = session.list_directory(())
            cleanup_incomplete = False
            if "generations" in root_names:
                generation_names = session.list_directory(("generations",))
                for attempt in range(_GENERATION_QUARANTINE_SETTLE_ATTEMPTS):
                    if not any(
                        _GENERATION_DELETE_QUARANTINE.fullmatch(name)
                        for name in generation_names
                    ):
                        break
                    if attempt + 1 == _GENERATION_QUARANTINE_SETTLE_ATTEMPTS:
                        return "cache_cleanup_incomplete"
                    lock.heartbeat()
                    time.sleep(_GENERATION_QUARANTINE_SETTLE_SECONDS)
                    generation_names = session.list_directory(("generations",))
                for name in generation_names:
                    lock.assert_owned()
                    pointer = self._load_current_pointer_windows_session(session)
                    if pointer is not None and name == pointer.directory_name:
                        continue
                    if not name.startswith(("generation-", ".staging-")):
                        continue
                    try:
                        observed = session.directory_identity(
                            ("generations", name)
                        )
                        inventory = session.snapshot_flat_directory(
                            ("generations", name)
                        )
                        if _WINDOWS_GENERATION_OWNER_NAME not in inventory:
                            raise _cache_unavailable()
                        ownership_payload = session.read_file(
                            ("generations", name),
                            _WINDOWS_GENERATION_OWNER_NAME,
                            _WINDOWS_GENERATION_OWNER_MAX_BYTES,
                        )
                        ownership = _parse_windows_generation_ownership(
                            ownership_payload,
                            lock.root_attestation.ownership_token,
                        )
                        allowed_inventory = {
                            _WINDOWS_GENERATION_OWNER_NAME,
                            "manifest.json",
                            "products.jsonl",
                            "offers.jsonl",
                            "state.json",
                        }
                        if not set(inventory).issubset(allowed_inventory):
                            raise _cache_unavailable()
                        if name.startswith("generation-"):
                            if set(inventory) != allowed_inventory:
                                raise _cache_unavailable()
                            manifest = Manifest.parse(
                                session.read_file(
                                    ("generations", name),
                                    "manifest.json",
                                    _WINDOWS_MANIFEST_MAX_BYTES,
                                )
                            )
                            state_value = _parse_windows_json(
                                session.read_file(
                                    ("generations", name),
                                    "state.json",
                                    _WINDOWS_STATE_MAX_BYTES,
                                )
                            )
                            if (
                                manifest.generation_id != ownership.generation_id
                                or not isinstance(state_value, dict)
                                or state_value.get("generation_id")
                                != ownership.generation_id
                            ):
                                raise _cache_unavailable()
                        lock.assert_owned()
                        pointer = self._load_current_pointer_windows_session(session)
                        if pointer is not None and name == pointer.directory_name:
                            continue
                        if not session.delete_flat_directory(
                            ("generations",),
                            name,
                            observed,
                            quarantine_name=f".{name}.delete-{uuid.uuid4().hex}",
                            expected_inventory=inventory,
                            expected_payloads={
                                _WINDOWS_GENERATION_OWNER_NAME: ownership_payload
                            },
                        ):
                            cleanup_incomplete = True
                    except (OSError, StockError, ValueError, TypeError):
                        cleanup_incomplete = True
            for name in session.list_directory(()):
                parsed = _parse_runtime_cleanup_name(name)
                if parsed is None:
                    continue
                directory_name, temporary = parsed
                lock.assert_owned()
                pointer = self._load_current_pointer_windows_session(session)
                if (
                    not temporary
                    and pointer is not None
                    and pointer.directory_name == directory_name
                ):
                    continue
                try:
                    observed = session.file_identity((), name)
                    payload = session.read_file(
                        (), name, _WINDOWS_RUNTIME_MAX_BYTES
                    )
                    runtime_value = _parse_windows_json(payload)
                    if not isinstance(runtime_value, dict) or not isinstance(
                        runtime_value.get("generation_id"), str
                    ):
                        raise _cache_unavailable()
                    _validate_windows_runtime_payload(
                        payload,
                        expected_generation_id=runtime_value["generation_id"],
                        expected_directory_name=directory_name,
                        expected_root_token=lock.root_attestation.ownership_token,
                    )
                    lock.assert_owned()
                    pointer = self._load_current_pointer_windows_session(session)
                    if (
                        not temporary
                        and pointer is not None
                        and pointer.directory_name == directory_name
                    ):
                        continue
                    session.delete_file_identity((), name, observed)
                except (OSError, StockError, ValueError, TypeError):
                    cleanup_incomplete = True
            return "cache_cleanup_incomplete" if cleanup_incomplete else None
        except (OSError, StockError, ValueError, TypeError):
            return "cache_cleanup_incomplete"

    def current_generation(self) -> GenerationFiles:
        state = CacheState.load_for_search(self.root)
        if state is None:
            raise _cache_unavailable()
        return state.files

    def _record_runtime_status(
        self,
        state: CacheState,
        stale: bool,
        warning_code: str | None,
        lock: CacheLock,
        *,
        verify_current: bool = False,
    ) -> CacheState:
        lock.assert_owned()
        if verify_current:
            current = CacheState.load(self.root, lock.heartbeat)
            if current is None or not _same_runtime_revision(current, state):
                raise StockError(
                    "cache_locked", "Активное поколение кэша изменилось", 6
                )
        value = {
            "generation_id": state.generation_id,
            "checked_at": (
                datetime.now(timezone.utc).isoformat()
                if not stale
                else state.checked_at
            ),
            "stale": stale,
            "warning_code": warning_code,
            "revision": uuid.uuid4().hex,
        }
        validate_runtime_status(value, state.generation_id)
        try:
            lock.heartbeat()
            with RuntimeCommitLock.acquire(self.root, lock.root_attestation):
                lock.assert_owned()
                _assert_runtime_commit_expected(lock.root_attestation, state)
                lock.assert_owned()
                _write_runtime_status_atomic_attested(
                    lock.root_attestation, state.directory_name, value
                )
                lock.assert_owned()
                written = load_runtime_status_from_directory(
                    lock.root_attestation.descriptor,
                    _runtime_status_path(self.root, state.directory_name).name,
                    state.generation_id,
                )
                if (
                    written.revision != value["revision"]
                    or written.checked_at != value["checked_at"]
                    or written.stale != stale
                    or written.warning_code != warning_code
                ):
                    raise StockError(
                        "cache_locked", "Активное поколение кэша изменилось", 6
                    )
                lock.assert_owned()
        except StockError:
            raise
        except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
            raise _cache_unavailable() from error
        runtime_path = _runtime_status_path(self.root, state.directory_name)
        return replace(
            state,
            checked_at=written.checked_at,
            files=replace(state.files, runtime_status=runtime_path),
            runtime_revision=written.revision,
            stale=written.stale,
            warning_code=written.warning_code,
        )

    def _stale_fallback(
        self, state: CacheState, warning_code: str
    ) -> RefreshResult:
        persisted = state
        if warning_code != "cache_locked":
            try:
                with CacheLock.acquire(self.root) as lock:
                    persisted = self._record_runtime_status(
                        state,
                        True,
                        warning_code,
                        lock,
                        verify_current=True,
                    )
            except StockError as error:
                if error.code != "cache_locked":
                    raise
        return RefreshResult.from_state(
            "stale_cache",
            persisted,
            stale=True,
            warning_code=warning_code,
        )

    def _load_if_readable(
        self, progress: Callable[[], None] | None = None
    ) -> CacheState | None:
        try:
            return CacheState.load(self.root, progress)
        except StockError as error:
            if error.code == "cache_locked":
                raise
            return None

    def _download_generation(
        self, manifest: Manifest, config: StockConfig, lock: CacheLock
    ) -> GenerationFiles:
        generations = self.root / "generations"
        directory = generations / f".staging-{uuid.uuid4().hex}"
        staged = GenerationFiles.from_directory(manifest.generation_id, directory)
        generations_descriptor = -1
        directory_descriptor = -1
        try:
            lock.assert_owned()
            generations_descriptor = _open_or_create_private_child_directory_descriptor(
                lock.root_attestation.descriptor, "generations"
            )
            directory_descriptor = _create_private_child_directory_descriptor(
                generations_descriptor, directory.name
            )
            _write_bytes_atomic_at(
                directory_descriptor, "manifest.json", manifest.body
            )
            self._download_private_file(
                _resolve_download_url(config.manifest_url, manifest.products.url),
                staged.products,
                manifest.products.bytes,
                manifest.products.sha256,
                lock,
                directory_descriptor,
            )
            self._download_private_file(
                _resolve_download_url(config.manifest_url, manifest.offers.url),
                staged.offers,
                manifest.offers.bytes,
                manifest.offers.sha256,
                lock,
                directory_descriptor,
            )
            _fsync_directory_descriptor(directory_descriptor)
            return staged
        except BaseException:
            lock.assert_owned()
            if generations_descriptor >= 0:
                observed = _lstat_private_child_descriptor(
                    generations_descriptor, directory.name, missing_ok=True
                )
                if observed is not None and stat.S_ISDIR(observed.st_mode):
                    _remove_private_child_directory(
                        generations_descriptor,
                        generations,
                        directory.name,
                        observed,
                    )
            raise
        finally:
            _close_optional_descriptor(
                directory_descriptor if directory_descriptor >= 0 else None
            )
            _close_optional_descriptor(
                generations_descriptor if generations_descriptor >= 0 else None
            )

    def _download_private_file(
        self,
        url: str,
        path: Path,
        expected_bytes: int,
        expected_sha256: str,
        lock: CacheLock,
        parent_descriptor: int,
    ) -> None:
        destination = _PrivateDownloadDestination(
            path, lock.root_attestation, parent_descriptor
        )
        try:
            self.client.download(
                url,
                destination,
                expected_bytes,
                expected_sha256,
                progress=lock.heartbeat,
            )
            destination.fsync()
        finally:
            destination.close()

    def _verify_generation(
        self, staged: GenerationFiles, manifest: Manifest, lock: CacheLock
    ) -> None:
        generations_descriptor = -1
        directory_descriptor = -1
        try:
            generations_descriptor = _open_private_child_directory(
                lock.root_attestation.descriptor,
                self.root,
                self.root / "generations",
            )
            directory_descriptor = _open_private_child_directory(
                generations_descriptor,
                self.root / "generations",
                staged.manifest.parent,
            )
            _verify_child_file(
                directory_descriptor,
                "products.jsonl",
                manifest.products,
                lock.heartbeat,
            )
            _verify_child_file(
                directory_descriptor,
                "offers.jsonl",
                manifest.offers,
                lock.heartbeat,
            )
        finally:
            _close_optional_descriptor(
                directory_descriptor if directory_descriptor >= 0 else None
            )
            _close_optional_descriptor(
                generations_descriptor if generations_descriptor >= 0 else None
            )

    def _activate(
        self,
        staged: GenerationFiles,
        manifest: Manifest,
        response: HttpResponse,
        lock: CacheLock,
    ) -> CacheState:
        checked_at = datetime.now(timezone.utc).isoformat()
        state_value: dict[str, Any] = {
            "generation_id": manifest.generation_id,
            "generated_at": manifest.generated_at,
            "checked_at": checked_at,
            "manifest_etag": _header(response.headers, "etag"),
            "manifest_last_modified": _header(response.headers, "last-modified"),
            "stale": False,
            "warning_code": None,
        }
        final_name = f"generation-{uuid.uuid4().hex}"
        final_directory = self.root / "generations" / final_name
        current_path = self.root / "current.json"
        lock.root_attestation.assert_current()
        previous_pointer = _read_optional_child_bytes(
            lock.root_attestation.descriptor, "current.json"
        )
        pointer = CurrentPointer(
            generation_id=manifest.generation_id,
            directory_name=final_name,
            activation_token=lock.token,
        )
        runtime_value = {
            "generation_id": manifest.generation_id,
            "checked_at": checked_at,
            "stale": False,
            "warning_code": None,
            "revision": uuid.uuid4().hex,
        }
        expected_runtime = validate_runtime_status(
            runtime_value, manifest.generation_id
        )

        generations_descriptor = -1
        staged_descriptor = -1
        try:
            lock.assert_owned()
            generations_descriptor = _open_private_child_directory(
                lock.root_attestation.descriptor,
                self.root,
                self.root / "generations",
            )
            staged_descriptor = _open_private_child_directory(
                generations_descriptor,
                self.root / "generations",
                staged.manifest.parent,
            )
            _write_json_atomic_at(staged_descriptor, "state.json", state_value)
            _fsync_directory_descriptor(staged_descriptor)
            lock.assert_owned()
            os.replace(
                staged.manifest.parent.name,
                final_name,
                src_dir_fd=generations_descriptor,
                dst_dir_fd=generations_descriptor,
            )
            _fsync_directory_descriptor(generations_descriptor)
        finally:
            _close_optional_descriptor(
                staged_descriptor if staged_descriptor >= 0 else None
            )
            _close_optional_descriptor(
                generations_descriptor if generations_descriptor >= 0 else None
            )
        try:
            lock.heartbeat()
            with RuntimeCommitLock.acquire(self.root, lock.root_attestation):
                lock.assert_owned()
                if _read_optional_child_bytes(
                    lock.root_attestation.descriptor, "current.json"
                ) != previous_pointer:
                    raise StockError(
                        "cache_locked", "Активное поколение кэша изменилось", 6
                    )
                _write_runtime_status_atomic_attested(
                    lock.root_attestation, final_name, runtime_value
                )
                lock.assert_owned()
                lock.root_attestation.assert_current()
                _write_json_atomic_at(
                    lock.root_attestation.descriptor,
                    "current.json",
                    pointer.to_dict(),
                )
                lock.assert_owned()
                committed_pointer = CurrentPointer.load_from_directory(
                    lock.root_attestation.descriptor
                )
                committed_runtime = load_runtime_status_from_directory(
                    lock.root_attestation.descriptor,
                    _runtime_status_path(self.root, final_name).name,
                    manifest.generation_id,
                )
                if (
                    committed_pointer != pointer
                    or committed_runtime.revision != runtime_value["revision"]
                    or committed_runtime.checked_at != checked_at
                    or committed_runtime.stale
                    or committed_runtime.warning_code is not None
                ):
                    raise StockError(
                        "cache_locked", "Активное поколение кэша изменилось", 6
                    )
                lock.assert_owned()
            state = CacheState.load(self.root, lock.heartbeat)
            if (
                state is None
                or state.generation_id != pointer.generation_id
                or state.directory_name != pointer.directory_name
                or state.runtime_revision != runtime_value["revision"]
            ):
                raise _cache_unavailable()
            lock.assert_owned()
        except BaseException:
            self._rollback_pointer_if_owned(
                current_path,
                pointer,
                expected_runtime,
                previous_pointer,
                lock.root_attestation,
            )
            self._remove_generation_if_inactive(final_directory)
            raise
        return state

    def _rollback_pointer_if_owned(
        self,
        current_path: Path,
        expected_pointer: CurrentPointer,
        expected_runtime: RuntimeStatus,
        previous_pointer: bytes | None,
        root_attestation: CacheRootAttestation,
    ) -> None:
        try:
            root_attestation.assert_current()
            with RuntimeCommitLock.acquire(self.root, root_attestation):
                root_attestation.assert_current()
                self._rollback_pointer_if_owned_locked(
                    current_path,
                    expected_pointer,
                    expected_runtime,
                    previous_pointer,
                    root_attestation,
                )
        except StockError:
            return

    def _rollback_pointer_if_owned_locked(
        self,
        current_path: Path,
        expected_pointer: CurrentPointer,
        expected_runtime: RuntimeStatus,
        previous_pointer: bytes | None,
        root_attestation: CacheRootAttestation,
    ) -> None:
        root_attestation.assert_current()
        try:
            current = CurrentPointer.load_from_directory(
                root_attestation.descriptor
            )
        except StockError:
            return
        if current != expected_pointer:
            return
        try:
            root_attestation.assert_current()
            current_runtime = load_runtime_status_from_directory(
                root_attestation.descriptor,
                _runtime_status_path(
                    self.root, expected_pointer.directory_name
                ).name,
                expected_pointer.generation_id,
            )
        except StockError:
            return
        if current_runtime != expected_runtime:
            return
        if previous_pointer is None:
            root_attestation.assert_current()
            observed_current = _lstat_private_child(
                root_attestation.descriptor,
                self.root,
                "current.json",
                missing_ok=True,
            )
            if (
                observed_current is not None
                and stat.S_ISREG(observed_current.st_mode)
            ):
                root_attestation.assert_current()
                _unlink_private_child_regular_file(
                    root_attestation.descriptor,
                    self.root,
                    "current.json",
                    observed_current,
                )
            return
        root_attestation.assert_current()
        _write_bytes_atomic_at(
            root_attestation.descriptor, "current.json", previous_pointer
        )

    def _remove_generation_if_inactive(self, directory: Path) -> None:
        root_attestation: CacheRootAttestation | None = None
        try:
            root_attestation = _attest_cache_root(self.root, create=False)
            with RuntimeCommitLock.acquire(self.root, root_attestation):
                observed_current = _lstat_private_child(
                    root_attestation.descriptor,
                    self.root,
                    "current.json",
                    missing_ok=True,
                )
                if observed_current is None:
                    pointer = None
                elif not stat.S_ISREG(observed_current.st_mode):
                    return
                else:
                    try:
                        pointer = CurrentPointer.load_from_directory(
                            root_attestation.descriptor
                        )
                    except StockError:
                        return
                if (
                    pointer is not None
                    and pointer.directory_name == directory.name
                ):
                    return
                root_attestation.assert_current()
                generations = self.root / "generations"
                generations_descriptor = _open_private_child_directory(
                    root_attestation.descriptor, self.root, generations
                )
                try:
                    observed_generation = _lstat_private_child(
                        generations_descriptor,
                        generations,
                        directory.name,
                        missing_ok=True,
                    )
                    if observed_generation is not None and not (
                        stat.S_ISDIR(observed_generation.st_mode)
                        and _remove_private_child_directory(
                            generations_descriptor,
                            generations,
                            directory.name,
                            observed_generation,
                        )
                    ):
                        return
                finally:
                    _close_optional_descriptor(generations_descriptor)
                try:
                    status_path = _runtime_status_path(
                        self.root, directory.name
                    )
                except StockError:
                    return
                observed_status = _lstat_private_child(
                    root_attestation.descriptor,
                    self.root,
                    status_path.name,
                    missing_ok=True,
                )
                if observed_status is not None and stat.S_ISREG(
                    observed_status.st_mode
                ):
                    root_attestation.assert_current()
                    _unlink_private_child_regular_file(
                        root_attestation.descriptor,
                        self.root,
                        status_path.name,
                        observed_status,
                    )
        except (OSError, StockError):
            return
        finally:
            if root_attestation is not None:
                try:
                    root_attestation.close()
                except StockError:
                    pass

    def _cleanup_inactive_generations(self, lock: CacheLock) -> str | None:
        lock.assert_owned()
        generations = self.root / "generations"
        root_descriptor: int | None = lock.root_attestation.descriptor
        generations_descriptor: int | None = None
        try:
            observed_generations = _lstat_private_child(
                root_descriptor,
                self.root,
                "generations",
                missing_ok=True,
            )
            if observed_generations is None:
                return None
            if not stat.S_ISDIR(observed_generations.st_mode):
                return "cache_cleanup_incomplete"
            generations_descriptor = _open_private_child_directory(
                root_descriptor,
                self.root,
                generations,
            )
            generation_names = _list_private_directory(
                generations_descriptor, generations
            )
            if not _directory_path_matches_descriptor(
                generations, generations_descriptor
            ):
                return "cache_cleanup_incomplete"
            for attempt in range(_GENERATION_QUARANTINE_SETTLE_ATTEMPTS):
                if not any(
                    _GENERATION_DELETE_QUARANTINE.fullmatch(name)
                    for name in generation_names
                ):
                    break
                if attempt + 1 == _GENERATION_QUARANTINE_SETTLE_ATTEMPTS:
                    return "cache_cleanup_incomplete"
                lock.heartbeat()
                time.sleep(_GENERATION_QUARANTINE_SETTLE_SECONDS)
                generation_names = _list_private_directory(
                    generations_descriptor, generations
                )
            cleanup_incomplete = False
            for name in generation_names:
                lock.assert_owned()
                pointer = self._load_current_pointer_for_cleanup(
                    lock.root_attestation
                )
                if pointer is not None and name == pointer.directory_name:
                    continue
                if not name.startswith(("generation-", ".staging-")):
                    continue
                try:
                    observed = _lstat_private_child(
                        generations_descriptor,
                        generations,
                        name,
                        missing_ok=True,
                    )
                    if observed is None:
                        continue
                    if not stat.S_ISDIR(observed.st_mode):
                        cleanup_incomplete = True
                        continue
                    lock.assert_owned()
                    pointer = self._load_current_pointer_for_cleanup(
                        lock.root_attestation
                    )
                    if pointer is not None and name == pointer.directory_name:
                        continue
                    if not _directory_path_matches_descriptor(
                        generations, generations_descriptor
                    ):
                        cleanup_incomplete = True
                        break
                    lock.root_attestation.assert_current()
                    if not _remove_private_child_directory(
                        generations_descriptor,
                        generations,
                        name,
                        observed,
                    ):
                        cleanup_incomplete = True
                except FileNotFoundError:
                    continue
                except StockError as error:
                    if error.code == "cache_locked":
                        raise
                    cleanup_incomplete = True
                except OSError:
                    cleanup_incomplete = True
            try:
                runtime_names = _list_private_directory(
                    root_descriptor, self.root
                )
            except (OSError, StockError):
                cleanup_incomplete = True
                runtime_names = []
            for name in runtime_names:
                parsed = _parse_runtime_cleanup_name(name)
                if parsed is None:
                    continue
                directory_name, temporary = parsed
                lock.assert_owned()
                pointer = self._load_current_pointer_for_cleanup(
                    lock.root_attestation
                )
                if (
                    not temporary
                    and pointer is not None
                    and directory_name == pointer.directory_name
                ):
                    continue
                try:
                    observed = _lstat_private_child(
                        root_descriptor,
                        self.root,
                        name,
                        missing_ok=True,
                    )
                    if observed is None:
                        continue
                    if not stat.S_ISREG(observed.st_mode):
                        cleanup_incomplete = True
                        continue
                    if not _directory_path_matches_descriptor(
                        self.root, root_descriptor
                    ):
                        cleanup_incomplete = True
                        break
                    lock.root_attestation.assert_current()
                    _unlink_private_child_regular_file(
                        root_descriptor,
                        self.root,
                        name,
                        observed,
                    )
                except FileNotFoundError:
                    continue
                except StockError as error:
                    if error.code == "cache_locked":
                        raise
                    cleanup_incomplete = True
                except OSError:
                    cleanup_incomplete = True
            return "cache_cleanup_incomplete" if cleanup_incomplete else None
        except StockError as error:
            if error.code == "cache_locked":
                raise
            return "cache_cleanup_incomplete"
        except OSError:
            return "cache_cleanup_incomplete"
        finally:
            _close_optional_descriptor(generations_descriptor)

    def _load_current_pointer_for_cleanup(
        self, root_attestation: CacheRootAttestation
    ) -> CurrentPointer | None:
        try:
            return CurrentPointer.load_from_directory(root_attestation.descriptor)
        except StockError:
            if _lstat_private_child(
                root_attestation.descriptor,
                self.root,
                "current.json",
                missing_ok=True,
            ) is None:
                return None
            raise


def _manifest_file(value: object) -> ManifestFile:
    if not isinstance(value, dict):
        raise _manifest_invalid()
    url = value.get("url")
    expected_bytes = value.get("bytes")
    expected_sha256 = value.get("sha256")
    if not isinstance(url, str) or not url:
        raise _manifest_invalid()
    if (
        not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes < 0
    ):
        raise _manifest_invalid()
    if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(
        expected_sha256
    ):
        raise _manifest_invalid()
    _validate_manifest_url(url)
    return ManifestFile(url, expected_bytes, expected_sha256.lower())


def _validate_manifest_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise _manifest_invalid() from error
    if parsed.fragment or "\\" in url:
        raise _manifest_invalid()
    if parsed.scheme:
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise _manifest_invalid()
        return
    if parsed.netloc:
        raise _manifest_invalid()
    if any(part == ".." for part in parsed.path.split("/")):
        raise _manifest_invalid()


def _resolve_download_url(manifest_url: str, candidate: str) -> str:
    _validate_manifest_url(candidate)
    return urljoin(manifest_url, candidate)


def _verify_file(
    path: Path,
    expected: ManifestFile,
    progress: Callable[[], None] | None = None,
) -> None:
    digest = hashlib.sha256()
    received = 0
    descriptor = -1
    try:
        descriptor = _open_private_regular_file(path)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            while chunk := stream.read(1024 * 1024):
                received += len(chunk)
                digest.update(chunk)
                if progress is not None:
                    progress()
    except StockError as error:
        if error.code == "cache_locked":
            raise
        raise StockError(
            "download_integrity_failed",
            "Проверка загруженного файла не пройдена",
            5,
        ) from error
    except OSError as error:
        raise StockError(
            "download_integrity_failed",
            "Проверка загруженного файла не пройдена",
            5,
        ) from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as error:
                raise StockError(
                    "download_integrity_failed",
                    "Проверка загруженного файла не пройдена",
                    5,
                ) from error
    if received != expected.bytes or digest.hexdigest() != expected.sha256:
        raise StockError(
            "download_integrity_failed",
            "Проверка загруженного файла не пройдена",
            5,
        )


def _verify_child_file(
    parent_descriptor: int,
    name: str,
    expected: ManifestFile,
    progress: Callable[[], None] | None = None,
) -> None:
    digest = hashlib.sha256()
    received = 0
    descriptor = -1
    try:
        descriptor = _open_private_child_regular_file(parent_descriptor, name)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            while chunk := stream.read(1024 * 1024):
                received += len(chunk)
                digest.update(chunk)
                if progress is not None:
                    progress()
    except StockError as error:
        if error.code == "cache_locked":
            raise
        raise StockError(
            "download_integrity_failed",
            "Проверка загруженного файла не пройдена",
            5,
        ) from error
    except OSError as error:
        raise StockError(
            "download_integrity_failed",
            "Проверка загруженного файла не пройдена",
            5,
        ) from error
    finally:
        if descriptor >= 0:
            _close_optional_descriptor(descriptor)
    if received != expected.bytes or digest.hexdigest() != expected.sha256:
        raise StockError(
            "download_integrity_failed",
            "Проверка загруженного файла не пройдена",
            5,
        )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _manifest_invalid()
        value[key] = item
    return value


def _parse_json(value: str) -> object:
    parsed = json.loads(
        value,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )
    _assert_finite_json(parsed)
    return parsed


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    raise ValueError("non-finite number")


def _assert_finite_json(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite number")
    if isinstance(value, dict):
        for item in value.values():
            _assert_finite_json(item)
    elif isinstance(value, list):
        for item in value:
            _assert_finite_json(item)


def _load_optional_runtime_status(
    cache_dir: Path,
    directory_name: str,
    generation_id: str,
) -> tuple[RuntimeStatus | None, Path | None]:
    path = _runtime_status_path(cache_dir, directory_name)
    observed = _lstat_optional(path)
    if observed is None:
        return None, None
    if not stat.S_ISREG(observed.st_mode):
        raise _cache_unavailable()
    return load_runtime_status(path, generation_id), path


def _load_optional_runtime_status_attested(
    root_attestation: CacheRootAttestation,
    directory_name: str,
    generation_id: str,
) -> tuple[RuntimeStatus | None, Path | None]:
    path = _runtime_status_path(root_attestation.root, directory_name)
    try:
        observed = _lstat_private_child(
            root_attestation.descriptor,
            root_attestation.root,
            path.name,
            missing_ok=True,
        )
    except StockError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise _cache_unavailable() from error
    if observed is None:
        return None, None
    if not stat.S_ISREG(observed.st_mode):
        raise _cache_unavailable()
    return (
        load_runtime_status_from_directory(
            root_attestation.descriptor, path.name, generation_id
        ),
        path,
    )


def _runtime_status_path(cache_dir: Path, directory_name: str) -> Path:
    if (
        not _DIRECTORY_NAME.fullmatch(directory_name)
        or directory_name in {".", ".."}
    ):
        raise _cache_unavailable()
    return cache_dir / f".runtime-status-{directory_name}.json"


def _parse_runtime_cleanup_name(name: str) -> tuple[str, bool] | None:
    status = _RUNTIME_STATUS_FILE.fullmatch(name)
    if status is not None:
        return status.group("directory"), False
    temporary = _RUNTIME_STATUS_TEMP.fullmatch(name)
    if temporary is not None:
        return temporary.group("directory"), True
    return None


def _write_runtime_status_atomic(
    cache_dir: Path, directory_name: str, value: object
) -> None:
    path = _runtime_status_path(cache_dir, directory_name)
    observed = _lstat_optional(path)
    if observed is not None and not stat.S_ISREG(observed.st_mode):
        raise _cache_unavailable()
    _write_json_atomic(path, value)


def _write_runtime_status_atomic_attested(
    root_attestation: CacheRootAttestation,
    directory_name: str,
    value: object,
) -> None:
    root_attestation.assert_current()
    name = _runtime_status_path(root_attestation.root, directory_name).name
    observed = _lstat_private_child(
        root_attestation.descriptor,
        root_attestation.root,
        name,
        missing_ok=True,
    )
    if observed is not None and not stat.S_ISREG(observed.st_mode):
        raise _cache_unavailable()
    root_attestation.assert_current()
    _write_json_atomic_at(root_attestation.descriptor, name, value)


def _assert_runtime_commit_expected(
    root_attestation: CacheRootAttestation, expected: CacheState
) -> None:
    root_attestation.assert_current()
    pointer = CurrentPointer.load_from_directory(root_attestation.descriptor)
    if (
        pointer.generation_id != expected.generation_id
        or pointer.directory_name != expected.directory_name
    ):
        raise StockError("cache_locked", "Активное поколение кэша изменилось", 6)
    path = _runtime_status_path(root_attestation.root, expected.directory_name)
    observed = _lstat_private_child(
        root_attestation.descriptor,
        root_attestation.root,
        path.name,
        missing_ok=True,
    )
    if observed is None:
        if expected.runtime_revision is not None:
            raise StockError(
                "cache_locked", "Активное поколение кэша изменилось", 6
            )
        return
    if not stat.S_ISREG(observed.st_mode):
        raise _cache_unavailable()
    current = load_runtime_status_from_directory(
        root_attestation.descriptor, path.name, expected.generation_id
    )
    if (
        current.revision != expected.runtime_revision
        or current.checked_at != expected.checked_at
        or current.stale != expected.stale
        or current.warning_code != expected.warning_code
    ):
        raise StockError("cache_locked", "Активное поколение кэша изменилось", 6)


def _attest_cache_root(root: Path, *, create: bool) -> CacheRootAttestation:
    """Initialize an empty cache leaf or validate its strict ownership marker."""
    if _is_native_windows():
        return _attest_cache_root_windows(root, create=create)
    descriptor: int | None = None
    initialization_locked = False
    initialized_now = False
    try:
        if os.name != "posix":
            raise _cache_unavailable()
        observed = _lstat_optional(root)
        if observed is None:
            if not create:
                raise _cache_unavailable()
            descriptor = _open_or_create_private_directory_posix(root, parents=True)
            observed = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(observed.st_mode)
            or _is_windows_directory_reparse_point(root, observed)
        ):
            raise _cache_unavailable()
        if descriptor is None:
            descriptor = _open_directory_descriptor_without_hardening(root)
        pinned = os.fstat(descriptor)
        effective_uid = getattr(os, "geteuid", lambda: pinned.st_uid)()
        if pinned.st_uid != effective_uid:
            raise _cache_unavailable()

        marker_exists = _cache_root_marker_exists(descriptor)
        if not marker_exists and not create:
            raise _cache_unavailable()
        if create:
            _fcntl.flock(descriptor, _fcntl.LOCK_EX)
            initialization_locked = True
        if not _cache_root_marker_exists(descriptor):
            entries = _list_cache_root_names(root, descriptor)
            if entries:
                raise _cache_unavailable()
            os.fchmod(descriptor, _PRIVATE_DIRECTORY_MODE)
            hardened = os.fstat(descriptor)
            if (
                not _same_file_identity(pinned, hardened)
                or stat.S_IMODE(hardened.st_mode) != _PRIVATE_DIRECTORY_MODE
                or not _cache_root_inventory_is_initialization_safe(descriptor)
            ):
                raise _cache_unavailable()
            pinned = hardened
            published_marker = _publish_cache_root_marker(root, descriptor)
            marker_evidence = _assert_published_cache_root_marker(
                descriptor, published_marker
            )
            initialized_now = True
        else:
            published_marker = None
            marker_evidence = _read_cache_root_marker(root, descriptor)
        if initialized_now and not _cache_root_inventory_is_initialization_safe(
            descriptor
        ):
            raise _cache_unavailable()
        os.fchmod(descriptor, _PRIVATE_DIRECTORY_MODE)
        hardened = os.fstat(descriptor)
        if (
            not _same_file_identity(pinned, hardened)
            or stat.S_IMODE(hardened.st_mode) != _PRIVATE_DIRECTORY_MODE
        ):
            raise _cache_unavailable()
        pinned = hardened
        if initialized_now:
            if published_marker is None:
                raise _cache_unavailable()
            marker_evidence = _assert_published_cache_root_marker(
                descriptor, published_marker
            )
            provisional_attestation = CacheRootAttestation(
                root, descriptor, pinned, marker_evidence
            )
            provisional_attestation.assert_current()
            if not _cache_root_inventory_is_initialization_safe(descriptor):
                raise _cache_unavailable()
            marker_evidence = _assert_published_cache_root_marker(
                descriptor, published_marker
            )
            provisional_attestation.assert_current()
        if initialization_locked:
            _fcntl.flock(descriptor, _fcntl.LOCK_UN)
            initialization_locked = False
        attestation = CacheRootAttestation(
            root, descriptor, pinned, marker_evidence
        )
        attestation.assert_current()
        descriptor = None
        return attestation
    except StockError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise _cache_unavailable() from error
    finally:
        if initialization_locked and descriptor is not None:
            try:
                _fcntl.flock(descriptor, _fcntl.LOCK_UN)
            except OSError:
                pass
        _close_optional_descriptor(descriptor)


def _attest_cache_root_windows(
    root: Path, *, create: bool
) -> CacheRootAttestation:
    session: Any | None = None
    try:
        payload = (
            _cache_root_marker_payload(secrets.token_hex(16)) if create else None
        )
        session = _windows_filesystem().open_cache_root(
            os.fspath(root),
            _CACHE_ROOT_MARKER_NAME,
            payload=payload,
            maximum=_CACHE_ROOT_MARKER_MAX_BYTES,
            create=create,
        )
        evidence = session.marker_evidence
        ownership_token = _parse_cache_root_marker_payload(evidence.payload)
        marker_evidence = _PublishedCacheRootMarker(
            ownership_token=ownership_token,
            device=evidence.marker_identity.volume,
            inode=evidence.marker_identity.file_id,
            file_type=stat.S_IFREG,
            link_count=1,
        )
        if evidence.root_identity.volume != evidence.marker_identity.volume:
            raise _cache_unavailable()
        attestation = CacheRootAttestation(
            root,
            None,
            None,
            marker_evidence,
            windows_root=session,
        )
        attestation.assert_current()
        session = None
        return attestation
    except StockError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise _cache_unavailable() from error
    finally:
        if session is not None:
            try:
                session.close()
            except OSError:
                pass


def _list_cache_root_names(root: Path, descriptor: int | None) -> list[str]:
    if descriptor is not None:
        return list(os.listdir(descriptor))
    return [entry.name for entry in root.iterdir()]


def _cache_root_marker_exists(descriptor: int) -> bool:
    try:
        os.stat(
            _CACHE_ROOT_MARKER_NAME,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        return True
    except FileNotFoundError:
        return False


def _cache_root_inventory_is_initialization_safe(descriptor: int) -> bool:
    try:
        entries = os.listdir(descriptor)
    except OSError:
        return False
    for name in entries:
        if name == _CACHE_ROOT_MARKER_NAME:
            continue
        return False
    return True


def _cache_root_marker_payload(token: str) -> bytes:
    return json.dumps(
        {
            "kind": _CACHE_ROOT_MARKER_KIND,
            "schema_version": _CACHE_ROOT_MARKER_VERSION,
            "ownership_token": token,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _publish_cache_root_marker(
    root: Path, descriptor: int | None
) -> _PublishedCacheRootMarker:
    if descriptor is None:
        raise _cache_unavailable()
    token = secrets.token_hex(16)
    payload = _cache_root_marker_payload(token)
    file_descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(
            _CACHE_ROOT_MARKER_NAME,
            flags,
            _PRIVATE_FILE_MODE,
            dir_fd=descriptor,
        )
        os.fchmod(file_descriptor, _PRIVATE_FILE_MODE)
        marker_identity = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(marker_identity.st_mode)
            or marker_identity.st_nlink != 1
            or stat.S_IMODE(marker_identity.st_mode) != _PRIVATE_FILE_MODE
        ):
            raise _cache_unavailable()
        _write_all(file_descriptor, payload)
        os.fsync(file_descriptor)
        written_identity = os.fstat(file_descriptor)
        if (
            not _same_file_identity(marker_identity, written_identity)
            or not stat.S_ISREG(written_identity.st_mode)
            or written_identity.st_nlink != 1
            or written_identity.st_size != len(payload)
        ):
            raise _cache_unavailable()
        evidence = _PublishedCacheRootMarker(
            ownership_token=token,
            device=written_identity.st_dev,
            inode=written_identity.st_ino,
            file_type=stat.S_IFMT(written_identity.st_mode),
            link_count=written_identity.st_nlink,
        )
        _fsync_directory_descriptor(descriptor)
        _assert_published_cache_root_marker(descriptor, evidence)
        if not _cache_root_inventory_is_initialization_safe(descriptor):
            raise _cache_unavailable()
        os.close(file_descriptor)
        file_descriptor = -1
        return evidence
    except BaseException:
        if file_descriptor >= 0:
            try:
                os.ftruncate(file_descriptor, 0)
                os.fsync(file_descriptor)
            except OSError:
                pass
        raise
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)


def _assert_published_cache_root_marker(
    root_descriptor: int, evidence: _PublishedCacheRootMarker
) -> _PublishedCacheRootMarker:
    file_descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(
            _CACHE_ROOT_MARKER_NAME,
            flags,
            dir_fd=root_descriptor,
        )
        opened = os.fstat(file_descriptor)
        if (
            opened.st_dev != evidence.device
            or opened.st_ino != evidence.inode
            or stat.S_IFMT(opened.st_mode) != evidence.file_type
            or evidence.file_type != stat.S_IFREG
            or opened.st_nlink != evidence.link_count
            or evidence.link_count != 1
            or stat.S_IMODE(opened.st_mode) != _PRIVATE_FILE_MODE
        ):
            raise _cache_unavailable()
        effective_uid = getattr(os, "geteuid", lambda: opened.st_uid)()
        if opened.st_uid != effective_uid:
            raise _cache_unavailable()
        payload = os.read(file_descriptor, _CACHE_ROOT_MARKER_MAX_BYTES + 1)
        token = _parse_cache_root_marker_payload(payload)
        confirmed = os.fstat(file_descriptor)
        canonical = os.stat(
            _CACHE_ROOT_MARKER_NAME,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            confirmed.st_dev != evidence.device
            or confirmed.st_ino != evidence.inode
            or stat.S_IFMT(confirmed.st_mode) != evidence.file_type
            or confirmed.st_nlink != evidence.link_count
            or canonical.st_dev != evidence.device
            or canonical.st_ino != evidence.inode
            or stat.S_IFMT(canonical.st_mode) != evidence.file_type
            or canonical.st_nlink != evidence.link_count
            or stat.S_IMODE(confirmed.st_mode) != _PRIVATE_FILE_MODE
            or stat.S_IMODE(canonical.st_mode) != _PRIVATE_FILE_MODE
            or token != evidence.ownership_token
        ):
            raise _cache_unavailable()
        return evidence
    except StockError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise _cache_unavailable() from error
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "marker write made no progress")
        offset += written


def _parse_cache_root_marker_payload(payload: bytes) -> str:
    if len(payload) > _CACHE_ROOT_MARKER_MAX_BYTES:
        raise _cache_unavailable()
    if payload != payload.strip():
        raise _cache_unavailable()
    value = _parse_json(payload.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "schema_version",
        "ownership_token",
    }:
        raise _cache_unavailable()
    token = value.get("ownership_token")
    if (
        value.get("kind") != _CACHE_ROOT_MARKER_KIND
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != _CACHE_ROOT_MARKER_VERSION
        or not isinstance(token, str)
        or not _CACHE_ROOT_TOKEN.fullmatch(token)
    ):
        raise _cache_unavailable()
    return token


def _read_cache_root_marker(
    root: Path, descriptor: int | None
) -> _PublishedCacheRootMarker:
    marker_path = root / _CACHE_ROOT_MARKER_NAME
    file_descriptor = -1
    try:
        observed = (
            os.stat(
                _CACHE_ROOT_MARKER_NAME,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if descriptor is not None
            else marker_path.lstat()
        )
        if (
            not stat.S_ISREG(observed.st_mode)
            or _is_windows_directory_reparse_point(marker_path, observed)
            or observed.st_size > _CACHE_ROOT_MARKER_MAX_BYTES
            or observed.st_nlink != 1
        ):
            raise _cache_unavailable()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = (
            os.open(_CACHE_ROOT_MARKER_NAME, flags, dir_fd=descriptor)
            if descriptor is not None
            else os.open(marker_path, flags)
        )
        opened = os.fstat(file_descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file_identity(
            observed, opened
        ) or opened.st_nlink != 1:
            raise _cache_unavailable()
        if os.name == "posix":
            effective_uid = getattr(os, "geteuid", lambda: opened.st_uid)()
            if (
                opened.st_uid != effective_uid
                or stat.S_IMODE(opened.st_mode) != _PRIVATE_FILE_MODE
            ):
                raise _cache_unavailable()
        payload = os.read(file_descriptor, _CACHE_ROOT_MARKER_MAX_BYTES + 1)
        token = _parse_cache_root_marker_payload(payload)
        confirmed = os.fstat(file_descriptor)
        canonical = (
            os.stat(
                _CACHE_ROOT_MARKER_NAME,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if descriptor is not None
            else marker_path.lstat()
        )
        if (
            not _same_file_identity(opened, confirmed)
            or confirmed.st_nlink != 1
            or not stat.S_ISREG(confirmed.st_mode)
            or (
                os.name == "posix"
                and stat.S_IMODE(confirmed.st_mode) != _PRIVATE_FILE_MODE
            )
            or not _same_file_identity(confirmed, canonical)
            or stat.S_IFMT(canonical.st_mode) != stat.S_IFREG
            or canonical.st_nlink != 1
            or (
                os.name == "posix"
                and stat.S_IMODE(canonical.st_mode) != _PRIVATE_FILE_MODE
            )
        ):
            raise _cache_unavailable()
        return _PublishedCacheRootMarker(
            ownership_token=token,
            device=confirmed.st_dev,
            inode=confirmed.st_ino,
            file_type=stat.S_IFMT(confirmed.st_mode),
            link_count=confirmed.st_nlink,
        )
    except StockError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ) as error:
        raise _cache_unavailable() from error
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _cache_unavailable() from error


def _harden_private_descriptor(
    descriptor: int, expected_type: int, expected_mode: int
) -> os.stat_result:
    try:
        opened = os.fstat(descriptor)
        if stat.S_IFMT(opened.st_mode) != expected_type:
            raise _cache_unavailable()
        if os.name == "posix":
            os.fchmod(descriptor, expected_mode)
            opened = os.fstat(descriptor)
            if (
                stat.S_IFMT(opened.st_mode) != expected_type
                or stat.S_IMODE(opened.st_mode) != expected_mode
            ):
                raise _cache_unavailable()
        return opened
    except StockError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise _cache_unavailable() from error


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _is_windows_directory_reparse_point(path: object, observed: object) -> bool:
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(observed, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _open_private_directory(
    path: Path,
    *,
    create: bool = False,
    parents: bool = False,
) -> int | None:
    descriptor = -1
    try:
        if create:
            if os.name == "posix":
                return _open_or_create_private_directory_posix(path, parents=parents)
            path.mkdir(
                mode=_PRIVATE_DIRECTORY_MODE,
                parents=parents,
                exist_ok=True,
            )
        observed = path.lstat()
        if not stat.S_ISDIR(observed.st_mode) or (
            os.name == "nt"
            and _is_windows_directory_reparse_point(path, observed)
        ):
            raise _cache_unavailable()
        if os.name != "posix":
            confirmed = path.lstat()
            if not stat.S_ISDIR(confirmed.st_mode) or not _same_file_identity(
                observed, confirmed
            ):
                raise _cache_unavailable()
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not _same_file_identity(
            observed, opened
        ):
            raise _cache_unavailable()
        opened = _harden_private_descriptor(
            descriptor, stat.S_IFDIR, _PRIVATE_DIRECTORY_MODE
        )
        confirmed = path.lstat()
        if not stat.S_ISDIR(confirmed.st_mode) or not _same_file_identity(
            opened, confirmed
        ):
            raise _cache_unavailable()
        return descriptor
    except StockError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except (OSError, ValueError, TypeError) as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise _cache_unavailable() from error


def _open_or_create_private_directory_posix(
    path: Path,
    *,
    parents: bool,
) -> int:
    if _lstat_optional(path) is not None:
        descriptor = _open_private_directory(path)
        if descriptor is None:
            raise _cache_unavailable()
        return descriptor
    parent = path.parent
    if parent == path or not path.name:
        raise _cache_unavailable()
    if _lstat_optional(parent) is None:
        if not parents:
            raise _cache_unavailable()
        parent_private = _open_private_directory(parent, create=True, parents=True)
        if parent_private is None:
            raise _cache_unavailable()
        os.close(parent_private)

    parent_descriptor = _open_directory_descriptor_without_hardening(parent)
    descriptor = -1
    created: os.stat_result | None = None
    try:
        parent_identity = os.fstat(parent_descriptor)
        if not _parent_protects_private_directory_bootstrap(parent_identity):
            raise _cache_unavailable()
        try:
            os.mkdir(path.name, _PRIVATE_DIRECTORY_MODE, dir_fd=parent_descriptor)
        except FileExistsError as error:
            raise _cache_unavailable() from error
        created = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(created.st_mode):
            raise _cache_unavailable()
        effective_uid = getattr(os, "geteuid", lambda: created.st_uid)()
        if created.st_uid != effective_uid:
            raise _cache_unavailable()
        if stat.S_IMODE(created.st_mode) & (stat.S_IRUSR | stat.S_IXUSR) != (
            stat.S_IRUSR | stat.S_IXUSR
        ):
            if not _remove_created_private_directory_entry(
                parent_descriptor, path.name, created
            ):
                raise _cache_unavailable()
            created = None
            _mkdir_private_directory_without_process_umask(
                parent_descriptor, path.name
            )
            created = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(created.st_mode) or created.st_uid != effective_uid:
                raise _cache_unavailable()
        _chmod_created_private_directory(parent_descriptor, path.name, created)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not _same_file_identity(
            created, opened
        ):
            raise _cache_unavailable()
        opened = _harden_private_descriptor(
            descriptor, stat.S_IFDIR, _PRIVATE_DIRECTORY_MODE
        )
        confirmed = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(confirmed.st_mode) or not _same_file_identity(
            opened, confirmed
        ):
            raise _cache_unavailable()
        confirmed_parent = parent.lstat()
        if not stat.S_ISDIR(confirmed_parent.st_mode) or not _same_file_identity(
            parent_identity, confirmed_parent
        ):
            raise _cache_unavailable()
        confirmed_path = path.lstat()
        if not stat.S_ISDIR(confirmed_path.st_mode) or not _same_file_identity(
            opened, confirmed_path
        ):
            raise _cache_unavailable()
        return descriptor
    except StockError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _remove_created_private_directory_entry(
            parent_descriptor, path.name, created
        )
        raise
    except (OSError, ValueError, TypeError) as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _remove_created_private_directory_entry(
            parent_descriptor, path.name, created
        )
        raise _cache_unavailable() from error
    finally:
        try:
            os.close(parent_descriptor)
        except OSError:
            pass


def _remove_created_private_directory_entry(
    parent_descriptor: int,
    name: str,
    expected: os.stat_result | None,
) -> bool:
    if expected is None:
        return False
    try:
        observed = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(observed.st_mode) or not _same_file_identity(
            expected, observed
        ):
            return False
        os.rmdir(name, dir_fd=parent_descriptor)
        return True
    except OSError:
        return False


def _mkdir_private_directory_without_process_umask(
    parent_descriptor: int,
    name: str,
) -> None:
    script = (
        "import os,sys; "
        "os.umask(0); "
        "os.mkdir(sys.argv[2], 0o700, dir_fd=int(sys.argv[1]))"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, str(parent_descriptor), name],
            check=False,
            close_fds=True,
            pass_fds=(parent_descriptor,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError) as error:
        raise _cache_unavailable() from error
    if completed.returncode != 0:
        raise _cache_unavailable()


def _parent_protects_private_directory_bootstrap(
    parent: os.stat_result,
) -> bool:
    effective_uid = getattr(os, "geteuid", lambda: parent.st_uid)()
    mode = stat.S_IMODE(parent.st_mode)
    sticky = bool(parent.st_mode & stat.S_ISVTX)
    if parent.st_uid != effective_uid and not sticky:
        return False
    if mode & (stat.S_IWGRP | stat.S_IWOTH) and not sticky:
        return False
    return True


def _open_directory_descriptor_without_hardening(path: Path) -> int:
    descriptor = -1
    try:
        observed = path.lstat()
        if not stat.S_ISDIR(observed.st_mode):
            raise _cache_unavailable()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not _same_file_identity(
            observed, opened
        ):
            raise _cache_unavailable()
        confirmed = path.lstat()
        if not stat.S_ISDIR(confirmed.st_mode) or not _same_file_identity(
            opened, confirmed
        ):
            raise _cache_unavailable()
        return descriptor
    except StockError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except (OSError, ValueError, TypeError) as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise _cache_unavailable() from error


def _chmod_created_private_directory(
    parent_descriptor: int,
    name: str,
    expected: os.stat_result,
) -> None:
    identity_descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        identity_descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(identity_descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not _same_file_identity(
            expected, opened
        ):
            raise _cache_unavailable()
        os.fchmod(identity_descriptor, _PRIVATE_DIRECTORY_MODE)
        hardened = os.fstat(identity_descriptor)
        if (
            not _same_file_identity(expected, hardened)
            or stat.S_IMODE(hardened.st_mode) != _PRIVATE_DIRECTORY_MODE
        ):
            raise _cache_unavailable()
        confirmed = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(confirmed.st_mode) or not _same_file_identity(
            hardened, confirmed
        ):
            raise _cache_unavailable()
    finally:
        if identity_descriptor >= 0:
            os.close(identity_descriptor)


def _ensure_private_directory(
    path: Path,
    *,
    create: bool = False,
    parents: bool = False,
) -> None:
    descriptor = _open_private_directory(path, create=create, parents=parents)
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError as error:
        raise _cache_unavailable() from error


def _open_private_child_directory(
    parent_descriptor: int | None,
    parent: Path,
    path: Path,
) -> int | None:
    if path.parent != parent or path.name in {"", ".", ".."}:
        raise _cache_unavailable()
    if parent_descriptor is None:
        return _open_private_directory(path)
    descriptor = -1
    try:
        observed = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(observed.st_mode):
            raise _cache_unavailable()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not _same_file_identity(
            observed, opened
        ):
            raise _cache_unavailable()
        opened = _harden_private_descriptor(
            descriptor, stat.S_IFDIR, _PRIVATE_DIRECTORY_MODE
        )
        confirmed = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(confirmed.st_mode) or not _same_file_identity(
            opened, confirmed
        ):
            raise _cache_unavailable()
        if not _directory_path_matches_descriptor(parent, parent_descriptor):
            raise _cache_unavailable()
        confirmed_path = path.lstat()
        if not stat.S_ISDIR(confirmed_path.st_mode) or not _same_file_identity(
            opened, confirmed_path
        ):
            raise _cache_unavailable()
        return descriptor
    except StockError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except (OSError, ValueError, TypeError) as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise _cache_unavailable() from error


def _directory_path_matches_descriptor(
    path: Path,
    descriptor: int | None,
) -> bool:
    if descriptor is None:
        checked: int | None = None
        try:
            checked = _open_private_directory(path)
            return True
        except (OSError, StockError):
            return False
        finally:
            _close_optional_descriptor(checked)
    try:
        opened = os.fstat(descriptor)
        confirmed = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(opened.st_mode)
        and stat.S_ISDIR(confirmed.st_mode)
        and _same_file_identity(opened, confirmed)
    )


def _list_private_directory(
    descriptor: int | None,
    path: Path,
) -> list[str]:
    if descriptor is not None:
        return _list_private_directory_descriptor(descriptor)
    try:
        names = os.listdir(path)
    except (OSError, TypeError, ValueError) as error:
        raise _cache_unavailable() from error
    if any(Path(name).name != name or name in {"", ".", ".."} for name in names):
        raise _cache_unavailable()
    return names


def _list_private_directory_descriptor(descriptor: int) -> list[str]:
    try:
        names = os.listdir(descriptor)
    except (OSError, TypeError, ValueError) as error:
        raise _cache_unavailable() from error
    if any(Path(name).name != name or name in {"", ".", ".."} for name in names):
        raise _cache_unavailable()
    return names


def _lstat_private_child(
    parent_descriptor: int | None,
    parent: Path,
    name: str,
    *,
    missing_ok: bool = False,
) -> os.stat_result | None:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise _cache_unavailable()
    if parent_descriptor is not None:
        return _lstat_private_child_descriptor(
            parent_descriptor,
            name,
            missing_ok=missing_ok,
        )
    try:
        observed = (parent / name).lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise _cache_unavailable()
    except OSError as error:
        raise _cache_unavailable() from error
    if stat.S_ISDIR(observed.st_mode) and os.name == "nt" and (
        _is_windows_directory_reparse_point(parent / name, observed)
    ):
        raise _cache_unavailable()
    return observed


def _lstat_private_child_descriptor(
    parent_descriptor: int,
    name: str,
    *,
    missing_ok: bool = False,
) -> os.stat_result | None:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise _cache_unavailable()
    try:
        return os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if missing_ok:
            return None
        raise _cache_unavailable()
    except OSError as error:
        raise _cache_unavailable() from error


def _remove_private_child_directory(
    parent_descriptor: int | None,
    parent: Path,
    name: str,
    expected: os.stat_result | None = None,
) -> bool:
    observed = _lstat_private_child(
        parent_descriptor, parent, name, missing_ok=True
    )
    if observed is None:
        return True
    if not stat.S_ISDIR(observed.st_mode) or (
        expected is not None and not _same_file_identity(expected, observed)
    ):
        return False
    if not _directory_path_matches_descriptor(parent, parent_descriptor):
        return False
    if parent_descriptor is None:
        return False
    directory_descriptor = -1
    try:
        directory_descriptor = _open_private_child_directory_for_deletion(
            parent_descriptor,
            name,
            observed,
        )
        inventory = _snapshot_private_flat_directory(
            directory_descriptor,
            parent / name,
        )
        if inventory is None:
            _close_optional_descriptor(directory_descriptor)
            return False
    except (OSError, StockError):
        _close_optional_descriptor(directory_descriptor)
        return False
    quarantine_name = f".{name}.delete-{uuid.uuid4().hex}"
    try:
        os.rename(
            name,
            quarantine_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
    except FileNotFoundError:
        _close_optional_descriptor(directory_descriptor)
        return True
    except OSError:
        _close_optional_descriptor(directory_descriptor)
        raise
    try:
        moved = _lstat_private_child(
            parent_descriptor,
            parent,
            quarantine_name,
            missing_ok=True,
        )
        if (
            moved is None
            or not stat.S_ISDIR(moved.st_mode)
            or not _same_file_identity(observed, moved)
        ):
            return False
        if not _empty_private_directory_descriptor(
            directory_descriptor,
            parent / quarantine_name,
            inventory,
        ):
            return False
    except (OSError, StockError):
        return False
    finally:
        _close_optional_descriptor(directory_descriptor)

    confirmed = _lstat_private_child(
        parent_descriptor,
        parent,
        quarantine_name,
        missing_ok=True,
    )
    if (
        confirmed is None
        or not stat.S_ISDIR(confirmed.st_mode)
        or not _same_file_identity(moved, confirmed)
    ):
        return False
    try:
        os.rmdir(quarantine_name, dir_fd=parent_descriptor)
    except OSError:
        return False
    return True


def _open_private_child_directory_for_deletion(
    parent_descriptor: int,
    name: str,
    expected: os.stat_result,
) -> int:
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not _same_file_identity(
            expected, opened
        ):
            raise _cache_unavailable()
        return descriptor
    except StockError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise _cache_unavailable() from error


def _empty_private_directory_descriptor(
    descriptor: int,
    path: Path,
    inventory: dict[str, os.stat_result],
) -> bool:
    if not _directory_path_matches_descriptor(path, descriptor):
        return False
    try:
        names = _list_private_directory(descriptor, path)
    except StockError:
        return False
    if set(names) != set(inventory):
        return False
    for name, expected in inventory.items():
        observed = _lstat_private_child(descriptor, path, name, missing_ok=True)
        if (
            observed is None
            or not stat.S_ISREG(observed.st_mode)
            or not _same_file_identity(expected, observed)
        ):
            return False
    for name, expected in inventory.items():
        try:
            _unlink_private_child_regular_file(
                descriptor,
                path,
                name,
                expected,
            )
        except StockError:
            return False
    try:
        return not _list_private_directory(descriptor, path)
    except StockError:
        return False


def _snapshot_private_flat_directory(
    descriptor: int,
    path: Path,
) -> dict[str, os.stat_result] | None:
    if not _directory_path_matches_descriptor(path, descriptor):
        return None
    return _snapshot_private_flat_directory_descriptor(descriptor)


def _snapshot_private_flat_directory_descriptor(
    descriptor: int,
) -> dict[str, os.stat_result] | None:
    try:
        names = _list_private_directory_descriptor(descriptor)
        inventory: dict[str, os.stat_result] = {}
        for name in names:
            observed = _lstat_private_child_descriptor(
                descriptor,
                name,
                missing_ok=True,
            )
            if observed is None or not stat.S_ISREG(observed.st_mode):
                return None
            inventory[name] = observed
        if set(_list_private_directory_descriptor(descriptor)) != set(inventory):
            return None
        for name, expected in inventory.items():
            confirmed = _lstat_private_child_descriptor(
                descriptor,
                name,
                missing_ok=True,
            )
            if (
                confirmed is None
                or not stat.S_ISREG(confirmed.st_mode)
                or not _same_file_identity(expected, confirmed)
            ):
                return None
        return inventory
    except StockError:
        return None


def _unlink_private_child_regular_file(
    parent_descriptor: int | None,
    parent: Path,
    name: str,
    expected: os.stat_result,
) -> None:
    observed = _lstat_private_child(
        parent_descriptor, parent, name, missing_ok=True
    )
    if observed is None:
        return
    if not stat.S_ISREG(observed.st_mode) or not _same_file_identity(
        expected, observed
    ):
        raise _cache_unavailable()
    if not _directory_path_matches_descriptor(parent, parent_descriptor):
        raise _cache_unavailable()
    if parent_descriptor is None:
        raise _cache_unavailable()
    try:
        os.unlink(name, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return
    except OSError as error:
        raise _cache_unavailable() from error


def _open_private_parent_directory_for_cleanup(parent: Path) -> int | None:
    if os.name != "posix":
        return None
    return _open_directory_descriptor_without_hardening(parent)


def _unlink_private_regular_file_if_owned(
    path: Path,
    expected: os.stat_result,
) -> bool:
    parent_descriptor: int | None = None
    try:
        parent_descriptor = _open_private_parent_directory_for_cleanup(path.parent)
        if parent_descriptor is None:
            return False
        observed = _lstat_private_child(
            parent_descriptor,
            path.parent,
            path.name,
            missing_ok=True,
        )
        if observed is None:
            return True
        if not stat.S_ISREG(observed.st_mode) or not _same_file_identity(
            expected, observed
        ):
            return False
        _unlink_private_child_regular_file(
            parent_descriptor,
            path.parent,
            path.name,
            observed,
        )
        removed = (
            _lstat_private_child(
                parent_descriptor,
                path.parent,
                path.name,
                missing_ok=True,
            )
            is None
        )
        if not removed:
            return False
        _fsync_directory_descriptor(parent_descriptor)
        return True
    except (OSError, StockError):
        return False
    finally:
        _close_optional_descriptor(parent_descriptor)


def _remove_private_cache_generation(root: Path, directory: Path) -> bool:
    generations = root / "generations"
    if (
        directory.parent != generations
        or not directory.name.startswith(("generation-", ".staging-"))
    ):
        return False
    root_descriptor: int | None = None
    generations_descriptor: int | None = None
    try:
        root_descriptor = _open_private_directory(root)
        observed_generations = _lstat_private_child(
            root_descriptor,
            root,
            "generations",
            missing_ok=True,
        )
        if observed_generations is None or not stat.S_ISDIR(
            observed_generations.st_mode
        ):
            return False
        generations_descriptor = _open_private_child_directory(
            root_descriptor,
            root,
            generations,
        )
        observed = _lstat_private_child(
            generations_descriptor,
            generations,
            directory.name,
            missing_ok=True,
        )
        if observed is None:
            return True
        if not stat.S_ISDIR(observed.st_mode):
            return False
        return _remove_private_child_directory(
            generations_descriptor,
            generations,
            directory.name,
            observed,
        )
    except (OSError, StockError):
        return False
    finally:
        _close_optional_descriptor(generations_descriptor)
        _close_optional_descriptor(root_descriptor)


def _close_optional_descriptor(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError:
        return


def _open_private_regular_file(
    path: Path,
    *,
    flags: int = os.O_RDONLY,
    create: bool = False,
) -> int:
    descriptor = -1
    observed = _lstat_optional(path)
    if observed is not None and not stat.S_ISREG(observed.st_mode):
        raise _cache_unavailable()
    if observed is None and not create:
        raise _cache_unavailable()
    secure_flags = flags | getattr(os, "O_CLOEXEC", 0)
    secure_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, secure_flags, _PRIVATE_FILE_MODE)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _cache_unavailable()
        if observed is not None and not _same_file_identity(observed, opened):
            raise _cache_unavailable()
        opened = _harden_private_descriptor(
            descriptor, stat.S_IFREG, _PRIVATE_FILE_MODE
        )
        confirmed = path.lstat()
        if not stat.S_ISREG(confirmed.st_mode) or not _same_file_identity(
            opened, confirmed
        ):
            raise _cache_unavailable()
        return descriptor
    except StockError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except (OSError, ValueError, TypeError) as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise _cache_unavailable() from error


def _read_private_bytes(path: Path) -> bytes:
    descriptor = _open_private_regular_file(path)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    except (OSError, ValueError) as error:
        raise _cache_unavailable() from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as error:
                raise _cache_unavailable() from error


def _open_and_close_private_regular_file(path: Path) -> None:
    descriptor = _open_private_regular_file(path)
    try:
        os.close(descriptor)
    except OSError as error:
        raise _cache_unavailable() from error


def _read_private_child_bytes(parent_descriptor: int, name: str) -> bytes:
    descriptor = _open_private_child_regular_file(parent_descriptor, name)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    except (OSError, ValueError) as error:
        raise _cache_unavailable() from error
    finally:
        if descriptor >= 0:
            _close_optional_descriptor(descriptor)


def _open_private_child_regular_file(
    parent_descriptor: int,
    name: str,
) -> int:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise _cache_unavailable()
    descriptor = -1
    try:
        observed = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(observed.st_mode):
            raise _cache_unavailable()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file_identity(
            observed,
            opened,
        ):
            raise _cache_unavailable()
        opened = _harden_private_descriptor(
            descriptor,
            stat.S_IFREG,
            _PRIVATE_FILE_MODE,
        )
        confirmed = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(confirmed.st_mode) or not _same_file_identity(
            opened,
            confirmed,
        ):
            raise _cache_unavailable()
        return descriptor
    except StockError:
        if descriptor >= 0:
            _close_optional_descriptor(descriptor)
        raise
    except (OSError, ValueError, TypeError) as error:
        if descriptor >= 0:
            _close_optional_descriptor(descriptor)
        raise _cache_unavailable() from error


def _read_private_child_text(parent_descriptor: int, name: str) -> str:
    descriptor = _open_private_child_regular_file(parent_descriptor, name)
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            return stream.read()
    except (OSError, UnicodeError, ValueError) as error:
        raise _cache_unavailable() from error
    finally:
        if descriptor >= 0:
            _close_optional_descriptor(descriptor)


def _stat_private_child_regular_file(
    parent_descriptor: int,
    name: str,
) -> os.stat_result:
    descriptor = _open_private_child_regular_file(parent_descriptor, name)
    try:
        return os.fstat(descriptor)
    except OSError as error:
        raise _cache_unavailable() from error
    finally:
        _close_optional_descriptor(descriptor)


def _read_private_text(path: Path) -> str:
    try:
        return _read_private_bytes(path).decode("utf-8")
    except UnicodeError as error:
        raise _cache_unavailable() from error


def _stat_private_regular_file(path: Path) -> os.stat_result:
    descriptor = _open_private_regular_file(path)
    try:
        return os.fstat(descriptor)
    except OSError as error:
        raise _cache_unavailable() from error
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            raise _cache_unavailable() from error


def _touch_private_regular_file(path: Path) -> None:
    descriptor = _open_private_regular_file(path, flags=os.O_RDWR)
    try:
        opened = os.fstat(descriptor)
        if os.name == "posix":
            os.utime(descriptor, None)
        else:
            raise _cache_unavailable()
        confirmed = path.lstat()
        if not stat.S_ISREG(confirmed.st_mode) or not _same_file_identity(
            opened, confirmed
        ):
            raise _cache_unavailable()
    except StockError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise _cache_unavailable() from error
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            raise _cache_unavailable() from error


def _remove_private_directory(path: Path) -> None:
    if _is_native_windows():
        return
    parent_descriptor: int | None = None
    try:
        if os.name == "posix":
            parent_descriptor = _open_directory_descriptor_without_hardening(
                path.parent
            )
        else:
            parent_descriptor = _open_private_directory(path.parent)
        observed = _lstat_private_child(
            parent_descriptor,
            path.parent,
            path.name,
            missing_ok=True,
        )
        if observed is None or not stat.S_ISDIR(observed.st_mode):
            return
        _remove_private_child_directory(
            parent_descriptor,
            path.parent,
            path.name,
            observed,
        )
    except (OSError, StockError):
        return
    finally:
        _close_optional_descriptor(parent_descriptor)


def _lock_commit_descriptor(descriptor: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(descriptor, _fcntl.LOCK_EX)
        return
    if _msvcrt is not None:  # pragma: no cover - Windows only
        os.lseek(descriptor, 0, os.SEEK_SET)
        _msvcrt.locking(descriptor, _msvcrt.LK_LOCK, 1)
        return
    raise _cache_unavailable()


def _unlock_commit_descriptor(descriptor: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(descriptor, _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:  # pragma: no cover - Windows only
        os.lseek(descriptor, 0, os.SEEK_SET)
        _msvcrt.locking(descriptor, _msvcrt.LK_UNLCK, 1)
        return
    raise _cache_unavailable()


def _same_generation(left: CacheState, right: CacheState) -> bool:
    return (
        left.generation_id == right.generation_id
        and left.directory_name == right.directory_name
    )


def _same_runtime_revision(left: CacheState, right: CacheState) -> bool:
    return (
        _same_generation(left, right)
        and left.checked_at == right.checked_at
        and left.stale == right.stale
        and left.warning_code == right.warning_code
        and left.runtime_revision == right.runtime_revision
    )


def _header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _write_json_atomic(path: Path, value: object) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    _write_bytes_atomic(path, payload)


def _write_json_atomic_at(
    parent_descriptor: int, name: str, value: object
) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    _write_bytes_atomic_at(parent_descriptor, name, payload)


def _write_bytes_atomic_at(
    parent_descriptor: int, name: str, payload: bytes
) -> None:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise _cache_unavailable()
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            temporary,
            flags,
            _PRIVATE_FILE_MODE,
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        observed = _lstat_private_child(
            parent_descriptor,
            Path("."),
            name,
            missing_ok=True,
        )
        if observed is not None and not stat.S_ISREG(observed.st_mode):
            raise _cache_unavailable()
        os.replace(
            temporary,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary = ""
        confirmed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(confirmed.st_mode):
            raise _cache_unavailable()
        _fsync_directory_descriptor(parent_descriptor)
    except StockError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise _cache_unavailable() from error
    finally:
        if descriptor >= 0:
            _close_optional_descriptor(descriptor)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except OSError:
                pass


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    _ensure_private_directory(path.parent, create=True, parents=True)
    observed = _lstat_optional(path)
    if observed is not None:
        descriptor = _open_private_regular_file(path)
        os.close(descriptor)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_bytes_fsync(temporary, payload)
        observed = _lstat_optional(path)
        if observed is not None:
            descriptor = _open_private_regular_file(path)
            os.close(descriptor)
        os.replace(temporary, path)
        descriptor = _open_private_regular_file(path)
        os.close(descriptor)
        _fsync_directory(path.parent)
    finally:
        observed_temporary = _lstat_optional(temporary)
        if observed_temporary is not None and stat.S_ISREG(
            observed_temporary.st_mode
        ):
            _unlink_private_regular_file_if_owned(
                temporary,
                observed_temporary,
            )


def _write_bytes_fsync(path: Path, payload: bytes) -> None:
    descriptor = _open_private_regular_file(
        path,
        flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        create=True,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, ValueError) as error:
        raise _cache_unavailable() from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as error:
                raise _cache_unavailable() from error


def _fsync_file(path: Path) -> None:
    descriptor = _open_private_regular_file(path)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = _open_private_directory(path)
    except (OSError, StockError) as error:
        cause = error.__cause__ if isinstance(error, StockError) else error
        error_number = getattr(cause, "errno", None)
        if error_number in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS or (
            os.name == "nt" and error_number in {errno.EACCES, errno.EPERM}
        ):
            return
        raise
    if descriptor is None:
        return
    try:
        _fsync_directory_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory_descriptor(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            raise


def _read_optional_bytes(path: Path) -> bytes | None:
    if _lstat_optional(path) is None:
        return None
    return _read_private_bytes(path)


def _read_optional_child_bytes(
    parent_descriptor: int, name: str
) -> bytes | None:
    observed = _lstat_private_child_descriptor(
        parent_descriptor, name, missing_ok=True
    )
    if observed is None:
        return None
    descriptor = _open_private_child_regular_file(parent_descriptor, name)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    except (OSError, ValueError) as error:
        raise _cache_unavailable() from error
    finally:
        if descriptor >= 0:
            _close_optional_descriptor(descriptor)


def _manifest_invalid() -> StockError:
    return StockError("manifest_invalid", "Некорректный manifest", 3)


def _cache_unavailable() -> StockError:
    return StockError("cache_unavailable", "Проверенный кэш недоступен", 7)
