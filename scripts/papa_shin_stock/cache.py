from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urljoin, urlsplit

from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.http_client import HttpResponse, SafeHttpClient


_LOCK_TTL_SECONDS = 30 * 60
_LOCK_FUTURE_SKEW_SECONDS = 5 * 60
_DIRECTORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = {
    errno.EBADF,
    errno.EINVAL,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}


@dataclass(frozen=True, slots=True)
class GenerationFiles:
    generation_id: str
    manifest: Path
    products: Path
    offers: Path

    @classmethod
    def from_directory(
        cls, generation_id: str, directory: Path
    ) -> "GenerationFiles":
        return cls(
            generation_id=generation_id,
            manifest=directory / "manifest.json",
            products=directory / "products.jsonl",
            offers=directory / "offers.jsonl",
        )

    def assert_readable(self) -> None:
        directory = self.manifest.parent
        if directory.is_symlink() or not directory.is_dir():
            raise _cache_unavailable()
        for path in (self.manifest, self.products, self.offers):
            if path.is_symlink() or not path.is_file():
                raise _cache_unavailable()
            try:
                with path.open("rb") as stream:
                    stream.read(1)
            except OSError as error:
                raise _cache_unavailable() from error


@dataclass(frozen=True, slots=True)
class CurrentPointer:
    generation_id: str
    directory_name: str
    activation_token: str | None = None

    @classmethod
    def load(cls, path: Path) -> "CurrentPointer":
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise _cache_unavailable() from error
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

    @classmethod
    def load(
        cls,
        cache_dir: Path,
        progress: Callable[[], None] | None = None,
    ) -> "CacheState | None":
        pointer_path = cache_dir / "current.json"
        if not pointer_path.exists():
            return None
        if pointer_path.is_symlink():
            raise _cache_unavailable()

        pointer = CurrentPointer.load(pointer_path)
        generation = cache_dir / "generations" / pointer.directory_name
        files = GenerationFiles.from_directory(pointer.generation_id, generation)
        files.assert_readable()
        try:
            manifest = Manifest.parse(files.manifest.read_bytes())
            if manifest.generation_id != pointer.generation_id:
                raise _cache_unavailable()
            _verify_file(files.products, manifest.products, progress)
            _verify_file(files.offers, manifest.offers, progress)
        except StockError as error:
            if error.code == "cache_locked":
                raise
            raise _cache_unavailable() from error
        except OSError as error:
            raise _cache_unavailable() from error

        state_path = generation / "state.json"
        if state_path.is_symlink() or not state_path.is_file():
            raise _cache_unavailable()
        try:
            value = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
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
        if not isinstance(generated_at, str) or not generated_at:
            raise _cache_unavailable()
        if not isinstance(checked_at, str) or not checked_at:
            raise _cache_unavailable()
        if manifest_etag is not None and not isinstance(manifest_etag, str):
            raise _cache_unavailable()
        if manifest_last_modified is not None and not isinstance(
            manifest_last_modified, str
        ):
            raise _cache_unavailable()
        return cls(
            generation_id=generation_id,
            generated_at=generated_at,
            checked_at=checked_at,
            manifest_etag=manifest_etag,
            manifest_last_modified=manifest_last_modified,
            directory_name=pointer.directory_name,
            files=files,
        )


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
        if not isinstance(generation_id, str) or not generation_id:
            raise _manifest_invalid()
        if not isinstance(generated_at, str) or not generated_at:
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
            stale=stale,
            warning_code=warning_code,
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


class CacheLock:
    def __init__(self, path: Path, token: str) -> None:
        self.path = path
        self.token = token

    @classmethod
    def acquire(cls, root: Path) -> "CacheLock":
        root.mkdir(parents=True, exist_ok=True)
        path = root / ".refresh.lock"
        token = uuid.uuid4().hex
        for _ in range(2):
            try:
                path.mkdir()
            except FileExistsError:
                if not cls._reclaim_stale(path):
                    raise StockError(
                        "cache_locked", "Обновление кэша уже выполняется", 6
                    )
                continue
            except OSError as error:
                raise StockError(
                    "cache_locked", "Не удалось установить блокировку кэша", 6
                ) from error

            owner = {"token": token, "created_at": time.time()}
            try:
                _write_json_atomic(path / "owner.json", owner)
                _write_bytes_fsync(path / f"heartbeat-{token}", b"")
                _fsync_directory(path)
            except OSError as error:
                shutil.rmtree(path, ignore_errors=True)
                raise StockError(
                    "cache_locked", "Не удалось установить блокировку кэша", 6
                ) from error
            return cls(path, token)
        raise StockError("cache_locked", "Обновление кэша уже выполняется", 6)

    @classmethod
    def _reclaim_stale(cls, path: Path) -> bool:
        observed = cls._read_owner(path)
        if observed is None:
            return cls._reclaim_stale_ownerless(path)
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
            os.rename(path, quarantine)
        except OSError:
            return False

        moved = cls._read_owner(quarantine)
        if (
            moved is None
            or moved[0] != observed_token
            or time.time() - moved[1] <= _LOCK_TTL_SECONDS
        ):
            try:
                if not path.exists():
                    os.rename(quarantine, path)
            except OSError:
                pass
            return False
        shutil.rmtree(quarantine, ignore_errors=True)
        return True

    @classmethod
    def _reclaim_stale_ownerless(cls, path: Path) -> bool:
        observed = cls._directory_identity(path)
        if observed is None or time.time_ns() - observed[2] <= int(
            _LOCK_TTL_SECONDS * 1_000_000_000
        ):
            return False
        if cls._directory_identity(path) != observed:
            return False

        quarantine = path.with_name(f"{path.name}.reclaim-{uuid.uuid4().hex}")
        try:
            os.rename(path, quarantine)
        except OSError:
            return False
        if cls._directory_identity(quarantine) != observed:
            try:
                if not path.exists():
                    os.rename(quarantine, path)
            except OSError:
                pass
            return False
        shutil.rmtree(quarantine, ignore_errors=True)
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
    def _read_owner(path: Path) -> tuple[str, float] | None:
        try:
            value = json.loads((path / "owner.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
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
            heartbeat = heartbeat_path.stat(follow_symlinks=False)
        except OSError:
            heartbeat = None
        if heartbeat is not None:
            if not stat.S_ISREG(heartbeat.st_mode):
                return None
            heartbeat_timestamp = heartbeat.st_mtime
            if (
                not math.isfinite(heartbeat_timestamp)
                or heartbeat_timestamp > time.time() + _LOCK_FUTURE_SKEW_SECONDS
            ):
                return None
            timestamp = max(timestamp, heartbeat_timestamp)
        return token, timestamp

    def assert_owned(self) -> None:
        owner = self._read_owner(self.path)
        if owner is None or owner[0] != self.token:
            raise StockError(
                "cache_locked", "Право на обновление кэша было утрачено", 6
            )

    def heartbeat(self) -> None:
        self.assert_owned()
        try:
            os.utime(self.path / f"heartbeat-{self.token}", None)
        except OSError as error:
            raise StockError(
                "cache_locked", "Право на обновление кэша было утрачено", 6
            ) from error
        self.assert_owned()

    def release(self) -> None:
        owner = self._read_owner(self.path)
        if owner is None or owner[0] != self.token:
            return

        quarantine = self.path.with_name(
            f"{self.path.name}.release-{self.token}-{uuid.uuid4().hex}"
        )
        try:
            os.rename(self.path, quarantine)
        except OSError:
            return

        moved = self._read_owner(quarantine)
        if moved is not None and moved[0] == self.token:
            shutil.rmtree(quarantine, ignore_errors=True)
            return
        try:
            if not self.path.exists():
                os.rename(quarantine, self.path)
        except OSError:
            pass

    def __enter__(self) -> "CacheLock":
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
                    current = CacheState.load(self.root, lock.heartbeat)
                    if (
                        current is None
                        or current.generation_id != previous.generation_id
                        or current.directory_name != previous.directory_name
                    ):
                        raise StockError(
                            "cache_locked", "Активное поколение кэша изменилось", 6
                        )
                    lock.assert_owned()
                    return RefreshResult.from_state("not_modified", current)
                if response.status != 200:
                    raise StockError(
                        "network_error", "Не удалось получить manifest", 3
                    )

                manifest = Manifest.parse(response.body)
                cleanup_warning = self._cleanup_inactive_generations(lock)
                if cleanup_warning is not None:
                    if previous is not None:
                        current = CacheState.load(self.root, lock.heartbeat)
                        if current is None:
                            raise _cache_unavailable()
                        lock.assert_owned()
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
            previous = self._load_if_readable()
            if previous is not None:
                return RefreshResult.from_state(
                    "stale_cache",
                    previous,
                    stale=True,
                    warning_code=error.code,
                )
            raise
        except OSError as error:
            failure = _cache_unavailable()
            previous = self._load_if_readable()
            if previous is not None:
                return RefreshResult.from_state(
                    "stale_cache",
                    previous,
                    stale=True,
                    warning_code=failure.code,
                )
            raise failure from error

    def current_generation(self) -> GenerationFiles:
        state = CacheState.load(self.root)
        if state is None:
            raise _cache_unavailable()
        return state.files

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
        generations.mkdir(parents=True, exist_ok=True)
        directory = generations / f".staging-{uuid.uuid4().hex}"
        directory.mkdir()
        staged = GenerationFiles.from_directory(manifest.generation_id, directory)
        try:
            _write_bytes_fsync(staged.manifest, manifest.body)
            self.client.download(
                _resolve_download_url(config.manifest_url, manifest.products.url),
                staged.products,
                manifest.products.bytes,
                manifest.products.sha256,
                progress=lock.heartbeat,
            )
            self.client.download(
                _resolve_download_url(config.manifest_url, manifest.offers.url),
                staged.offers,
                manifest.offers.bytes,
                manifest.offers.sha256,
                progress=lock.heartbeat,
            )
            for path in (staged.products, staged.offers):
                _fsync_file(path)
            _fsync_directory(directory)
            return staged
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def _verify_generation(
        self, staged: GenerationFiles, manifest: Manifest, lock: CacheLock
    ) -> None:
        _verify_file(staged.products, manifest.products, lock.heartbeat)
        _verify_file(staged.offers, manifest.offers, lock.heartbeat)

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
        }
        _write_json_atomic(staged.manifest.parent / "state.json", state_value)
        _fsync_directory(staged.manifest.parent)

        final_name = f"generation-{uuid.uuid4().hex}"
        final_directory = self.root / "generations" / final_name
        current_path = self.root / "current.json"
        previous_pointer = _read_optional_bytes(current_path)
        pointer = CurrentPointer(
            generation_id=manifest.generation_id,
            directory_name=final_name,
            activation_token=lock.token,
        )

        lock.assert_owned()
        os.replace(staged.manifest.parent, final_directory)
        try:
            _fsync_directory(final_directory.parent)
            lock.assert_owned()
            _write_json_atomic(current_path, pointer.to_dict())
            lock.assert_owned()
            state = CacheState.load(self.root, lock.heartbeat)
            if state is None:
                raise _cache_unavailable()
            lock.assert_owned()
        except BaseException:
            self._rollback_pointer_if_owned(
                current_path, pointer.activation_token, previous_pointer
            )
            self._remove_generation_if_inactive(final_directory)
            raise
        return state

    def _rollback_pointer_if_owned(
        self,
        current_path: Path,
        activation_token: str | None,
        previous_pointer: bytes | None,
    ) -> None:
        try:
            current = CurrentPointer.load(current_path)
        except StockError:
            return
        if current.activation_token != activation_token:
            return
        if previous_pointer is None:
            current_path.unlink(missing_ok=True)
            _fsync_directory(current_path.parent)
            return
        _write_bytes_atomic(current_path, previous_pointer)

    def _remove_generation_if_inactive(self, directory: Path) -> None:
        try:
            state = CacheState.load(self.root)
        except StockError:
            state = None
        if state is not None and state.files.manifest.parent == directory:
            return
        shutil.rmtree(directory, ignore_errors=True)

    def _cleanup_inactive_generations(self, lock: CacheLock) -> str | None:
        lock.assert_owned()
        generations = self.root / "generations"
        try:
            entries = list(generations.iterdir())
        except FileNotFoundError:
            return None
        except OSError:
            return "cache_cleanup_incomplete"
        cleanup_incomplete = False
        for entry in entries:
            lock.assert_owned()
            pointer = self._load_current_pointer_for_cleanup()
            if (
                pointer is not None and entry.name == pointer.directory_name
            ) or entry.is_symlink():
                continue
            if entry.name.startswith(("generation-", ".staging-")):
                lock.assert_owned()
                pointer = self._load_current_pointer_for_cleanup()
                if pointer is not None and entry.name == pointer.directory_name:
                    continue
                try:
                    shutil.rmtree(entry)
                except FileNotFoundError:
                    continue
                except OSError:
                    cleanup_incomplete = True
        return "cache_cleanup_incomplete" if cleanup_incomplete else None

    def _load_current_pointer_for_cleanup(self) -> CurrentPointer | None:
        current_path = self.root / "current.json"
        try:
            return CurrentPointer.load(current_path)
        except StockError:
            if not current_path.exists():
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
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                received += len(chunk)
                digest.update(chunk)
                if progress is not None:
                    progress()
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


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _manifest_invalid()
        value[key] = item
    return value


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


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_bytes_fsync(temporary, payload)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_bytes_fsync(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS or (
            os.name == "nt" and error.errno in {errno.EACCES, errno.EPERM}
        ):
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            raise
    finally:
        os.close(descriptor)


def _read_optional_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _manifest_invalid() -> StockError:
    return StockError("manifest_invalid", "Некорректный manifest", 3)


def _cache_unavailable() -> StockError:
    return StockError("cache_unavailable", "Проверенный кэш недоступен", 7)
