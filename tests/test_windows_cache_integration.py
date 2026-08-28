from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tests.robotyre_v1_fixture import (
    BASE_GENERATION_ID,
    manifest_bytes as robotyre_manifest_bytes,
    payloads as robotyre_payloads,
)
from tests.test_cache import CacheFixture, FakeHttpClient, manifest_bytes

from papa_shin_stock import cache as cache_module
from papa_shin_stock import schema as schema_module
from papa_shin_stock.cache import CacheState, StockCache
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.http_client import DownloadReceipt, HttpResponse
from papa_shin_stock.query import SearchQuery
from papa_shin_stock.schema import StockSearcher
from papa_shin_stock._windows_fs import (
    MarkerEvidence,
    WindowsFilesystemError,
    WindowsIdentity,
)


SEARCH_PRODUCTS, SEARCH_OFFERS = robotyre_payloads()
GENERATION_C = "c" * 64
GENERATION_D = "f" * 64


def search_manifest_bytes() -> bytes:
    return robotyre_manifest_bytes(SEARCH_PRODUCTS, SEARCH_OFFERS)


class SearchFixtureHttpClient:
    def __init__(self, response: HttpResponse | None = None) -> None:
        self.response = response or HttpResponse(
            status=200,
            headers={"ETag": f'"{BASE_GENERATION_ID}"'},
            body=search_manifest_bytes(),
        )

    def get_manifest(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> HttpResponse:
        return self.response

    def download(
        self,
        url: str,
        destination: Path,
        expected_bytes: int,
        expected_sha256: str,
        progress: object | None = None,
    ) -> DownloadReceipt:
        payload = (
            SEARCH_PRODUCTS
            if destination.name == "products.jsonl"
            else SEARCH_OFFERS
        )
        destination.write_bytes(payload)
        if callable(progress):
            progress()
        return DownloadReceipt(
            bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()
        )


class LocalWindowsRoot:
    def __init__(
        self,
        root: Path,
        marker_name: str,
        marker_evidence: MarkerEvidence,
        filesystem: "LocalWindowsFilesystem",
    ) -> None:
        self.root_path = root
        self.marker_name = marker_name
        self.marker_evidence = marker_evidence
        self.filesystem = filesystem

    @staticmethod
    def _identity(path: Path, *, directory: bool) -> WindowsIdentity:
        observed = path.lstat()
        if stat.S_ISDIR(observed.st_mode) != directory or path.is_symlink():
            raise OSError("unexpected object type")
        if not directory and observed.st_nlink != 1:
            raise OSError("unexpected hardlink")
        return WindowsIdentity(observed.st_dev, observed.st_ino)

    def _directory(self, parts: tuple[str, ...]) -> Path:
        path = self.root_path.joinpath(*parts)
        self._identity(path, directory=True)
        return path

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _record(self, name: str) -> None:
        self.filesystem.calls.append(name)

    def assert_current(self) -> None:
        observed_root = self.root_path.lstat()
        marker = self.root_path / self.marker_name
        observed_marker = marker.lstat()
        if (
            not stat.S_ISDIR(observed_root.st_mode)
            or not stat.S_ISREG(observed_marker.st_mode)
            or observed_marker.st_nlink != 1
            or WindowsIdentity(observed_root.st_dev, observed_root.st_ino)
            != self.marker_evidence.root_identity
            or WindowsIdentity(observed_marker.st_dev, observed_marker.st_ino)
            != self.marker_evidence.marker_identity
            or marker.read_bytes() != self.marker_evidence.payload
        ):
            raise OSError("root attestation changed")

    def close(self) -> None:
        self.filesystem.closed_session_count += 1

    def ensure_directory(self, parts: tuple[str, ...]) -> WindowsIdentity:
        self._record("ensure_directory")
        with self.filesystem.mutex:
            current = self.root_path
            for part in parts:
                candidate = current / part
                try:
                    candidate.mkdir()
                    self._fsync_directory(current)
                except FileExistsError:
                    pass
                self._identity(candidate, directory=True)
                current = candidate
            self.assert_current()
            return self._identity(current, directory=True)

    def create_directory(
        self, parent_parts: tuple[str, ...], name: str
    ) -> WindowsIdentity:
        self._record("create_directory")
        with self.filesystem.mutex:
            parent = self._directory(parent_parts)
            path = parent / name
            path.mkdir()
            self._fsync_directory(path)
            self._fsync_directory(parent)
            return self._identity(path, directory=True)

    def directory_identity(self, parts: tuple[str, ...]) -> WindowsIdentity:
        self._record("directory_identity")
        return self._identity(self._directory(parts), directory=True)

    def file_identity(
        self, parent_parts: tuple[str, ...], name: str
    ) -> WindowsIdentity:
        self._record("file_identity")
        return self._identity(self._directory(parent_parts) / name, directory=False)

    def snapshot_flat_directory(
        self, parts: tuple[str, ...]
    ) -> dict[str, WindowsIdentity]:
        self._record("snapshot_flat_directory")
        if (
            parts == (".refresh.lock",)
            and self.filesystem.canonical_snapshot_failures_remaining > 0
        ):
            self.filesystem.canonical_snapshot_failures_remaining -= 1
            raise OSError("synthetic post-publication attestation failure")
        with self.filesystem.mutex:
            directory = self._directory(parts)
            first = sorted(path.name for path in directory.iterdir())
            inventory = {
                name: self._identity(directory / name, directory=False)
                for name in first
            }
            repeated = sorted(path.name for path in directory.iterdir())
            if repeated != first:
                raise OSError("inventory changed")
            return inventory

    def list_directory(self, parts: tuple[str, ...]) -> list[str]:
        self._record("list_directory")
        if parts == () and self.filesystem.root_list_failures_remaining > 0:
            self.filesystem.root_list_failures_remaining -= 1
            raise OSError("synthetic root enumeration failure")
        with self.filesystem.mutex:
            return sorted(path.name for path in self._directory(parts).iterdir())

    def read_file(
        self, parent_parts: tuple[str, ...], name: str, maximum: int
    ) -> bytes:
        self._record("read_file")
        with self.filesystem.mutex:
            path = self._directory(parent_parts) / name
            self._identity(path, directory=False)
            payload = path.read_bytes()
            if len(payload) > maximum:
                raise OSError("file too large")
            self._identity(path, directory=False)
            return payload

    def read_optional_file(
        self, parent_parts: tuple[str, ...], name: str, maximum: int
    ) -> bytes | None:
        try:
            return self.read_file(parent_parts, name, maximum)
        except FileNotFoundError:
            return None

    def _replace_file_cas(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        *,
        expected: bytes | None,
        payload: bytes,
    ) -> WindowsIdentity:
        with self.filesystem.mutex:
            parent = self._directory(parent_parts)
            path = parent / name
            if expected is None:
                if path.exists() or path.is_symlink():
                    raise OSError("CAS target appeared")
            else:
                self._identity(path, directory=False)
                if path.read_bytes() != expected:
                    raise OSError("CAS target changed")
            temporary = parent / f".{name}.{uuid.uuid4().hex}.tmp"
            try:
                with temporary.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                if expected is None and (path.exists() or path.is_symlink()):
                    raise OSError("CAS target appeared")
                if expected is not None and path.read_bytes() != expected:
                    raise OSError("CAS target changed")
                os.replace(temporary, path)
                self._fsync_directory(parent)
                return self._identity(path, directory=False)
            finally:
                if temporary.exists():
                    temporary.unlink()

    def write_new_file(
        self, parent_parts: tuple[str, ...], name: str, payload: bytes
    ) -> WindowsIdentity:
        self._record("write_new_file")
        return self._replace_file_cas(
            parent_parts, name, expected=None, payload=payload
        )

    def replace_file_cas(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        *,
        expected: bytes | None,
        payload: bytes,
    ) -> WindowsIdentity:
        self._record("replace_file_cas")
        return self._replace_file_cas(
            parent_parts, name, expected=expected, payload=payload
        )

    def delete_file_cas(
        self, parent_parts: tuple[str, ...], name: str, expected: bytes
    ) -> None:
        self._record("delete_file_cas")
        with self.filesystem.mutex:
            parent = self._directory(parent_parts)
            path = parent / name
            identity = self._identity(path, directory=False)
            if path.read_bytes() != expected:
                raise OSError("CAS target changed")
            if self._identity(path, directory=False) != identity:
                raise OSError("CAS target changed")
            path.unlink()
            self._fsync_directory(parent)

    def delete_file_identity(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        expected: WindowsIdentity,
    ) -> None:
        self._record("delete_file_identity")
        with self.filesystem.mutex:
            parent = self._directory(parent_parts)
            path = parent / name
            if self._identity(path, directory=False) != expected:
                raise OSError("identity changed")
            path.unlink()
            self._fsync_directory(parent)

    def last_write_time(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        *,
        directory: bool,
        expected: WindowsIdentity | None = None,
    ) -> float:
        self._record("last_write_time")
        path = self._directory(parent_parts) / name
        identity = self._identity(path, directory=directory)
        if expected is not None and identity != expected:
            raise OSError("identity changed")
        return path.stat(follow_symlinks=False).st_mtime

    def touch_file(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        expected: WindowsIdentity,
    ) -> None:
        self._record("touch_file")
        with self.filesystem.mutex:
            parent = self._directory(parent_parts)
            path = parent / name
            if self._identity(path, directory=False) != expected:
                raise OSError("identity changed")
            if os.name == "nt":
                os.utime(path, None)
            else:
                os.utime(path, None, follow_symlinks=False)
            self._fsync_directory(parent)

    def verify_file(
        self,
        parent_parts: tuple[str, ...],
        name: str,
        *,
        expected_bytes: int,
        expected_sha256: str,
        progress: object | None = None,
    ) -> None:
        self._record("verify_file")
        payload = self.read_file(parent_parts, name, expected_bytes)
        if len(payload) != expected_bytes or hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise OSError("integrity changed")
        if callable(progress):
            progress()

    def rename_directory(
        self,
        parent_parts: tuple[str, ...],
        source_name: str,
        expected: WindowsIdentity,
        destination_name: str,
    ) -> None:
        self._record("rename_directory")
        with self.filesystem.mutex:
            parent = self._directory(parent_parts)
            source = parent / source_name
            destination = parent / destination_name
            if self._identity(source, directory=True) != expected:
                raise OSError("identity changed")
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(destination)
            os.rename(source, destination)
            if self._identity(destination, directory=True) != expected:
                raise OSError("identity changed")
            self._fsync_directory(parent)
            if (
                source_name.startswith(".refresh.lock.init-")
                and destination_name == ".refresh.lock"
                and self.filesystem.lock_publish_failures_remaining > 0
            ):
                self.filesystem.lock_publish_failures_remaining -= 1
                raise OSError("synthetic post-rename lock publication failure")
            if (
                source_name.startswith(".staging-")
                and destination_name.startswith("generation-")
                and self.filesystem.generation_publish_failures_remaining > 0
            ):
                self.filesystem.generation_publish_failures_remaining -= 1
                raise OSError("synthetic post-rename generation publication failure")

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
        self._record("delete_flat_directory")
        with self.filesystem.mutex:
            parent = self._directory(parent_parts)
            source = parent / name
            quarantine = parent / quarantine_name
            if (
                name == ".refresh.lock"
                and self.filesystem.replace_canonical_lock_before_delete
            ):
                self.filesystem.replace_canonical_lock_before_delete = False
                os.rename(source, parent / ".foreign-displaced-owned-lock")
                source.mkdir()
                (source / "foreign.txt").write_bytes(b"foreign-safe")
            if self._identity(source, directory=True) != expected:
                raise OSError("identity changed")
            inventory = {
                child.name: self._identity(child, directory=False)
                for child in source.iterdir()
            }
            if expected_inventory is not None and inventory != expected_inventory:
                raise OSError("inventory changed")
            if quarantine.exists() or quarantine.is_symlink():
                raise FileExistsError(quarantine)
            os.rename(source, quarantine)
            self._fsync_directory(parent)
            if self.filesystem.flat_delete_failures_remaining > 0:
                self.filesystem.flat_delete_failures_remaining -= 1
                raise OSError("synthetic post-rename cleanup failure")
            if (
                name.startswith(".refresh.lock.release-")
                and self.filesystem.mutate_recovery_quarantine_payload_once
            ):
                self.filesystem.mutate_recovery_quarantine_payload_once = False
                owner_path = quarantine / "owner.json"
                owner_value = json.loads(owner_path.read_text(encoding="utf-8"))
                owner_value["created_at"] = 1.0
                owner_path.write_text(json.dumps(owner_value), encoding="utf-8")
            if self._identity(quarantine, directory=True) != expected:
                raise OSError("identity changed")
            if set(path.name for path in quarantine.iterdir()) != set(inventory):
                raise OSError("inventory changed")
            if expected_payloads is not None:
                for child_name, payload in expected_payloads.items():
                    if (quarantine / child_name).read_bytes() != payload:
                        raise OSError("payload changed")
            if expected_write_times is not None:
                for child_name, timestamp in expected_write_times.items():
                    if (
                        (quarantine / child_name).stat(follow_symlinks=False).st_mtime
                        != timestamp
                    ):
                        raise OSError("timestamp changed")
            for child_name, identity in inventory.items():
                child = quarantine / child_name
                if self._identity(child, directory=False) != identity:
                    raise OSError("identity changed")
                child.unlink()
            quarantine.rmdir()
            self._fsync_directory(parent)
            return True

    def create_download_destination(
        self, parent_parts: tuple[str, ...], name: str
    ) -> "LocalWindowsDownloadDestination":
        self._record("create_download_destination")
        return LocalWindowsDownloadDestination(
            self, self._directory(parent_parts), name
        )


class LocalWindowsDownloadDestination:
    def __init__(
        self, session: LocalWindowsRoot, parent: Path, name: str
    ) -> None:
        self.session = session
        self.parent = parent
        self.path = parent / name
        self.descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        self.identity = LocalWindowsRoot._identity(self.path, directory=False)
        self.deleted = False

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def parents(self) -> object:
        return self.path.parents

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def open(self, mode: str = "r", *args: object, **kwargs: object) -> object:
        if mode != "wb" or args or kwargs or self.descriptor < 0:
            raise OSError("invalid destination open")
        duplicate = os.dup(self.descriptor)
        os.ftruncate(duplicate, 0)
        os.lseek(duplicate, 0, os.SEEK_SET)
        return os.fdopen(duplicate, "wb")

    def write_bytes(self, payload: bytes) -> int:
        with self.open("wb") as stream:
            return stream.write(payload)

    def fsync(self) -> None:
        os.fsync(self.descriptor)

    def unlink(self, missing_ok: bool = False) -> None:
        if self.deleted:
            if missing_ok:
                return
            raise FileNotFoundError(self.path)
        if LocalWindowsRoot._identity(self.path, directory=False) != self.identity:
            raise OSError("identity changed")
        self.path.unlink()
        LocalWindowsRoot._fsync_directory(self.parent)
        self.deleted = True

    def close(self) -> None:
        descriptor = self.descriptor
        self.descriptor = -1
        if descriptor >= 0:
            os.close(descriptor)


class LocalWindowsFilesystem:
    """Path-local stand-in; Win32 handle policy is covered in test_windows_fs."""

    def __init__(self) -> None:
        self.open_cache_root_calls = 0
        self.closed_session_count = 0
        self.calls: list[str] = []
        self.mutex = threading.RLock()
        self.flat_delete_failures_remaining = 0
        self.canonical_snapshot_failures_remaining = 0
        self.lock_publish_failures_remaining = 0
        self.generation_publish_failures_remaining = 0
        self.replace_canonical_lock_before_delete = False
        self.root_list_failures_remaining = 0
        self.mutate_recovery_quarantine_payload_once = False

    def open_cache_root(
        self,
        root: str,
        marker_name: str,
        *,
        payload: bytes | None,
        maximum: int,
        create: bool,
    ) -> LocalWindowsRoot:
        self.open_cache_root_calls += 1
        root_path = Path(root)
        if not root_path.exists():
            if not create:
                raise FileNotFoundError(root)
            root_path.mkdir(parents=True)
        marker = root_path / marker_name
        if not marker.exists():
            if not create or payload is None or list(root_path.iterdir()):
                raise OSError("missing marker")
            with marker.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        if len(marker.read_bytes()) > maximum:
            raise OSError("marker too large")
        root_stat = root_path.lstat()
        marker_stat = marker.lstat()
        session = LocalWindowsRoot(
            root_path,
            marker_name,
            MarkerEvidence(
                WindowsIdentity(root_stat.st_dev, root_stat.st_ino),
                WindowsIdentity(marker_stat.st_dev, marker_stat.st_ino),
                marker.read_bytes(),
            ),
            self,
        )
        session.assert_current()
        return session

    def initialize_marker(
        self, root: str, marker_name: str, payload: bytes, maximum: int
    ) -> object:
        path = Path(root) / marker_name
        if list(Path(root).iterdir()):
            raise OSError("root not empty")
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o600)
        return object()

    def delete_flat_directory(
        self,
        parent: str,
        name: str,
        expected: object,
        *,
        quarantine_name: str,
    ) -> bool:
        source = Path(parent) / name
        quarantine = Path(parent) / quarantine_name
        observed = source.lstat()
        if not stat.S_ISDIR(observed.st_mode):
            raise OSError("not directory")
        os.rename(source, quarantine)
        if self.flat_delete_failures_remaining > 0:
            self.flat_delete_failures_remaining -= 1
            raise OSError("synthetic post-rename cleanup failure")
        shutil.rmtree(quarantine)
        return True

    def delete_file(self, parent: str, name: str, expected: object) -> None:
        (Path(parent) / name).unlink()


class WindowsCacheWorkflowMockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "cache"
        self.config = StockConfig(
            manifest_url="https://stock.example.test/manifest.json",
            username="reader",
            password="secret",
            product_id_field="robotyre_product_id",
            offer_product_id_field="robotyre_product_id",
            cache_dir=self.root,
        )
        self.windows = LocalWindowsFilesystem()

    def _release_artifacts(self) -> list[Path]:
        if not self.root.exists():
            return []
        return [
            path
            for path in self.root.iterdir()
            if ".refresh.lock.release-" in path.name
        ]

    def _pointer_payload(self) -> bytes:
        return (self.root / "current.json").read_bytes()

    def _generation_c_client(self, **kwargs: object) -> FakeHttpClient:
        return FakeHttpClient(
            response=HttpResponse(
                status=200,
                headers={"ETag": f'"{GENERATION_C}"'},
                body=manifest_bytes(GENERATION_C),
            ),
            **kwargs,
        )

    def _search_config(self) -> StockConfig:
        return StockConfig(
            manifest_url="https://stock.example.test/manifest.json",
            username="synthetic-user",
            password="synthetic-password",
            product_id_field="robotyre_product_id",
            offer_product_id_field="robotyre_product_id",
            cache_dir=self.root,
        )

    def _public_search(self, config: StockConfig) -> dict[str, object]:
        files = StockCache(self.root, object()).current_generation()
        query = SearchQuery.from_args(argparse.Namespace())
        return StockSearcher(files, config).search(query).to_public_dict()

    def test_failure_then_304_is_visible_to_search_without_mutating_generation(
        self,
    ) -> None:
        config = self._search_config()

        class FailingClient:
            def get_manifest(
                self, etag: str | None = None, last_modified: str | None = None
            ) -> HttpResponse:
                raise StockError("network_error", "Синтетическая ошибка сети", 3)

        not_modified = SearchFixtureHttpClient(
            HttpResponse(status=304, headers={}, body=b"")
        )
        with patch.object(
            cache_module, "_is_native_windows", return_value=True
        ), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            StockCache(self.root, SearchFixtureHttpClient()).refresh(config)
            pointer = json.loads(self._pointer_payload())
            generation = self.root / "generations" / pointer["directory_name"]
            immutable_state = (generation / "state.json").read_bytes()

            stale = StockCache(self.root, FailingClient()).refresh(config)
            stale_public = self._public_search(config)
            fresh = StockCache(self.root, not_modified).refresh(config)
            fresh_public = self._public_search(config)

        self.assertEqual(stale.status, "stale_cache")
        self.assertTrue(stale_public["generation"]["stale"])
        self.assertEqual(stale_public["warnings"][0]["code"], "network_error")
        self.assertEqual(fresh.status, "not_modified")
        self.assertFalse(fresh_public["generation"]["stale"])
        self.assertEqual(fresh_public["warnings"], [])
        self.assertEqual((generation / "state.json").read_bytes(), immutable_state)

    def test_stale_runtime_revision_cannot_overwrite_later_304(self) -> None:
        config = self._search_config()
        with patch.object(
            cache_module, "_is_native_windows", return_value=True
        ), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            StockCache(self.root, SearchFixtureHttpClient()).refresh(config)
            original = CacheState.load(self.root)
            self.assertIsNotNone(original)
            lock = cache_module._WindowsCacheLock.acquire(self.root)
            self.addCleanup(lock.release)
            stale = StockCache(self.root, object())._record_runtime_status_windows(
                original, True, "network_error", lock
            )
            fresh = StockCache(self.root, object())._record_runtime_status_windows(
                stale, False, None, lock
            )
            with self.assertRaisesRegex(StockError, "cache_locked"):
                StockCache(self.root, object())._record_runtime_status_windows(
                    stale, True, "network_error", lock
                )
            lock.release()
            final = CacheState.load(self.root)

        self.assertIsNotNone(final)
        self.assertEqual(final.runtime_revision, fresh.runtime_revision)
        self.assertFalse(final.stale)

    def test_failure_then_304_then_failure_preserves_latest_checked_at(self) -> None:
        config = self._search_config()

        class FailingClient:
            def get_manifest(
                self, etag: str | None = None, last_modified: str | None = None
            ) -> HttpResponse:
                raise StockError("network_error", "Синтетическая ошибка сети", 3)

        with patch.object(
            cache_module, "_is_native_windows", return_value=True
        ), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            StockCache(self.root, SearchFixtureHttpClient()).refresh(config)
            initial = CacheState.load(self.root)
            self.assertIsNotNone(initial)
            first_stale = StockCache(self.root, FailingClient()).refresh(config)
            fresh = StockCache(
                self.root,
                SearchFixtureHttpClient(
                    HttpResponse(status=304, headers={}, body=b"")
                ),
            ).refresh(config)
            second_stale = StockCache(self.root, FailingClient()).refresh(config)

        self.assertEqual(first_stale.checked_at, initial.source_checked_at)
        self.assertEqual(fresh.checked_at, initial.source_checked_at)
        self.assertEqual(second_stale.checked_at, fresh.checked_at)
        self.assertTrue(second_stale.stale)

    def test_304_freshness_does_not_reread_manifest_path_after_attested_load(self) -> None:
        config = self._search_config()

        class ReplacingClient:
            def get_manifest(
                nested_self,
                etag: str | None = None,
                last_modified: str | None = None,
            ) -> HttpResponse:
                pointer = json.loads(
                    (self.root / "current.json").read_text(encoding="utf-8")
                )
                manifest_path = (
                    self.root
                    / "generations"
                    / pointer["directory_name"]
                    / "manifest.json"
                )
                manifest_path.write_bytes(
                    b"x" * (cache_module._WINDOWS_MANIFEST_MAX_BYTES + 1)
                )
                return HttpResponse(status=304, headers={}, body=b"")

        with patch.object(
            cache_module, "_is_native_windows", return_value=True
        ), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            StockCache(self.root, SearchFixtureHttpClient()).refresh(config)
            result = StockCache(self.root, ReplacingClient()).refresh(config)

        self.assertEqual(result.status, "not_modified")

    def test_malformed_windows_runtime_status_is_rejected_by_cache_and_search(
        self,
    ) -> None:
        config = self._search_config()
        with patch.object(
            cache_module, "_is_native_windows", return_value=True
        ), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            StockCache(self.root, SearchFixtureHttpClient()).refresh(config)
            state = CacheState.load(self.root)
            self.assertIsNotNone(state)
            runtime = state.files.runtime_status
            self.assertIsNotNone(runtime)
            valid = runtime.read_bytes()
            invalid_values = (
                b"[]",
                b"{malformed",
                valid.replace(
                    BASE_GENERATION_ID.encode("ascii"),
                    ("e" * 64).encode("ascii"),
                ),
            )
            for value in invalid_values:
                with self.subTest(value=value[:32]):
                    runtime.write_bytes(value)
                    with self.assertRaisesRegex(StockError, "cache_unavailable"):
                        CacheState.load(self.root)
                    with self.assertRaisesRegex(StockError, "cache_unavailable"):
                        StockCache(self.root, object()).current_generation()
                    runtime.write_bytes(valid)

    def test_runtime_write_failure_releases_lock_and_preserves_generation(
        self,
    ) -> None:
        config = self._search_config()
        original_replace = LocalWindowsRoot.replace_file_cas
        fail_runtime = True

        def replace_or_fail(
            session: LocalWindowsRoot,
            parent_parts: tuple[str, ...],
            name: str,
            *,
            expected: bytes | None,
            payload: bytes,
        ) -> WindowsIdentity:
            if fail_runtime and name.startswith(".runtime-status-"):
                raise OSError("synthetic runtime publication failure")
            return original_replace(
                session,
                parent_parts,
                name,
                expected=expected,
                payload=payload,
            )

        with patch.object(
            cache_module, "_is_native_windows", return_value=True
        ), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, SearchFixtureHttpClient()).refresh(config)
            pointer = self._pointer_payload()
            with patch.object(
                LocalWindowsRoot, "replace_file_cas", new=replace_or_fail
            ):
                with self.assertRaisesRegex(StockError, "cache_unavailable"):
                    StockCache(
                        self.root,
                        SearchFixtureHttpClient(
                            HttpResponse(status=304, headers={}, body=b"")
                        ),
                    ).refresh(config)
            fail_runtime = False
            recovered = StockCache(
                self.root,
                SearchFixtureHttpClient(
                    HttpResponse(status=304, headers={}, body=b"")
                ),
            ).refresh(config)

        self.assertEqual(first.generation_id, recovered.generation_id)
        self.assertEqual(recovered.status, "not_modified")
        self.assertEqual(self._pointer_payload(), pointer)
        self.assertFalse((self.root / ".refresh.lock").exists())

    def test_windows_304_search_verifies_and_streams_each_payload_once(self) -> None:
        config = self._search_config()
        verified_bytes = 0
        streamed_bytes = 0
        original_verify = LocalWindowsRoot.verify_file
        original_rows = schema_module._rows

        def count_verify(
            session: LocalWindowsRoot,
            parent_parts: tuple[str, ...],
            name: str,
            *,
            expected_bytes: int,
            expected_sha256: str,
            progress: object | None = None,
        ) -> None:
            nonlocal verified_bytes
            verified_bytes += expected_bytes
            original_verify(
                session,
                parent_parts,
                name,
                expected_bytes=expected_bytes,
                expected_sha256=expected_sha256,
                progress=progress,
            )

        def count_rows(path: Path, maximum_rows: int, *integrity: object) -> object:
            nonlocal streamed_bytes
            streamed_bytes += path.stat().st_size
            yield from original_rows(path, maximum_rows, *integrity)

        with patch.object(
            cache_module, "_is_native_windows", return_value=True
        ), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            StockCache(self.root, SearchFixtureHttpClient()).refresh(config)
            verified_bytes = 0
            with patch.object(
                LocalWindowsRoot, "verify_file", new=count_verify
            ), patch.object(schema_module, "_rows", side_effect=count_rows):
                refreshed = StockCache(
                    self.root,
                    SearchFixtureHttpClient(
                        HttpResponse(status=304, headers={}, body=b"")
                    ),
                ).refresh(config)
                public = self._public_search(config)

        payload_bytes = len(SEARCH_PRODUCTS) + len(SEARCH_OFFERS)
        self.assertEqual(refreshed.status, "not_modified")
        self.assertEqual(public["status"], "ok")
        self.assertEqual(verified_bytes, payload_bytes)
        self.assertEqual(streamed_bytes, payload_bytes)

    def test_local_windows_touch_omits_unsupported_symlink_argument(self) -> None:
        session = self.windows.open_cache_root(
            str(self.root),
            ".papa-shin-stock-cache-root.json",
            payload=cache_module._cache_root_marker_payload("a" * 32),
            maximum=512,
            create=True,
        )
        self.addCleanup(session.close)
        identity = session.write_new_file((), "heartbeat", b"")
        keyword_calls: list[dict[str, object]] = []

        def windows_utime(
            _path: object,
            _times: object,
            **kwargs: object,
        ) -> None:
            keyword_calls.append(kwargs)

        with patch.object(os, "name", "nt"), patch.object(
            os, "utime", side_effect=windows_utime
        ):
            session.touch_file((), "heartbeat", identity)

        self.assertEqual(keyword_calls, [{}])

    def test_local_backend_skips_unsupported_windows_directory_fsync(self) -> None:
        with patch.object(os, "name", "nt"), patch.object(
            os,
            "open",
            side_effect=PermissionError("synthetic Windows directory open"),
        ):
            try:
                LocalWindowsRoot._fsync_directory(self.root)
            except PermissionError as error:
                self.fail(f"Windows test backend attempted directory open: {error}")

    def test_two_sequential_refreshes_are_updated_and_cleanup_old_generation(self) -> None:
        first_client = FakeHttpClient()
        second_client = FakeHttpClient(
            response=HttpResponse(
                status=200,
                headers={"ETag": f'"{GENERATION_C}"'},
                body=manifest_bytes(GENERATION_C),
            )
        )

        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, first_client).refresh(self.config)
            second = StockCache(self.root, second_client).refresh(self.config)

        self.assertEqual((first.status, second.status), ("updated", "updated"))
        pointer = json.loads((self.root / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["generation_id"], GENERATION_C)
        self.assertEqual(
            [path.name for path in (self.root / "generations").iterdir()],
            [pointer["directory_name"]],
        )

    def test_normal_release_does_not_leave_release_artifacts(self) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            result = StockCache(self.root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertGreater(self.windows.open_cache_root_calls, 0)
        self.assertTrue(
            {
                "ensure_directory",
                "create_directory",
                "write_new_file",
                "create_download_destination",
                "verify_file",
                "rename_directory",
                "replace_file_cas",
                "delete_flat_directory",
            }.issubset(self.windows.calls)
        )
        self.assertFalse((self.root / ".refresh.lock").exists())
        self.assertEqual(self._release_artifacts(), [])

    def test_pre_acquire_recovery_failure_closes_retained_root_session(self) -> None:
        self.windows.root_list_failures_remaining = 1

        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            with self.assertRaisesRegex(StockError, "cache_locked"):
                StockCache(self.root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(self.windows.open_cache_root_calls, 2)
        self.assertEqual(self.windows.closed_session_count, 2)

    def test_transient_release_cleanup_failure_is_recovered(self) -> None:
        self.windows.flat_delete_failures_remaining = 1

        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            result = StockCache(self.root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertEqual(result.warning_codes, ())
        self.assertFalse((self.root / ".refresh.lock").exists())
        self.assertEqual(self._release_artifacts(), [])

    def test_success_preserves_inactive_legacy_generation_for_manual_cleanup(self) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            StockCache(self.root, FakeHttpClient()).refresh(self.config)
            pointer = json.loads((self.root / "current.json").read_text(encoding="utf-8"))
            active = self.root / "generations" / pointer["directory_name"]
            owner = json.loads(
                (active / cache_module._WINDOWS_GENERATION_OWNER_NAME).read_text(
                    encoding="utf-8"
                )
            )
            owner["generation_id"] = "legacy-generation"
            legacy = self.root / "generations" / "generation-legacy"
            legacy.mkdir()
            (legacy / cache_module._WINDOWS_GENERATION_OWNER_NAME).write_text(
                json.dumps(owner), encoding="utf-8"
            )
            (legacy / "manifest.json").write_text(
                json.dumps(
                    {
                        "generation_id": "legacy-generation",
                        "generated_at": "2026-08-27T10:00:00+00:00",
                        "files": {"products": {}, "offers": {}},
                    }
                ),
                encoding="utf-8",
            )
            for name in ("products.jsonl", "offers.jsonl", "state.json"):
                (legacy / name).write_bytes(b"{}\n")

            result = StockCache(
                self.root, self._generation_c_client()
            ).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertEqual(result.warning_codes, ())
        self.assertTrue(legacy.is_dir())

    def test_persistent_release_cleanup_failure_is_reported_and_preserved(self) -> None:
        self.windows.flat_delete_failures_remaining = 20

        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            result = StockCache(self.root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertEqual(result.warning_codes, ("cache_cleanup_incomplete",))
        self.assertFalse((self.root / ".refresh.lock").exists())
        retained = [
            path
            for path in self.root.glob(".refresh.lock.*")
            if path.is_dir() and (path / "owner.json").is_file()
        ]
        self.assertEqual(len(retained), 1)
        self.assertTrue((retained[0] / "owner.json").is_file())

    def test_recovery_never_re_attests_payload_rejected_after_quarantine_move(
        self,
    ) -> None:
        self.windows.flat_delete_failures_remaining = 1
        self.windows.mutate_recovery_quarantine_payload_once = True

        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            retained = [
                path
                for path in self.root.glob(".refresh.lock.*")
                if path.is_dir() and (path / "owner.json").is_file()
            ]
            self.assertEqual(len(retained), 1)
            retained_path = retained[0]
            retained_identity = retained_path.stat().st_ino
            retained_payload = (retained_path / "owner.json").read_bytes()

            second = StockCache(
                self.root, self._generation_c_client()
            ).refresh(self.config)

        self.assertEqual(first.warning_codes, ("cache_cleanup_incomplete",))
        self.assertEqual(second.warning_codes, ("cache_cleanup_incomplete",))
        self.assertTrue(retained_path.is_dir())
        self.assertEqual(retained_path.stat().st_ino, retained_identity)
        self.assertEqual((retained_path / "owner.json").read_bytes(), retained_payload)
        self.assertEqual(json.loads(retained_payload)["created_at"], 1.0)

    def test_foreign_canonical_lock_at_release_is_preserved_and_reported(self) -> None:
        self.windows.replace_canonical_lock_before_delete = True

        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            result = StockCache(self.root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertEqual(result.warning_codes, ("cache_cleanup_incomplete",))
        self.assertEqual(
            (self.root / ".refresh.lock" / "foreign.txt").read_bytes(),
            b"foreign-safe",
        )

    def test_post_publication_lock_failure_does_not_block_next_refresh(self) -> None:
        self.windows.canonical_snapshot_failures_remaining = 1

        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            with self.assertRaisesRegex(StockError, "cache_locked"):
                StockCache(self.root, FakeHttpClient()).refresh(self.config)
            result = StockCache(self.root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertFalse((self.root / ".refresh.lock").exists())
        self.assertEqual(self._release_artifacts(), [])

    def test_post_rename_lock_failure_does_not_leave_owned_canonical_lock(self) -> None:
        self.windows.lock_publish_failures_remaining = 1

        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            with self.assertRaisesRegex(StockError, "cache_locked"):
                StockCache(self.root, FakeHttpClient()).refresh(self.config)
            result = StockCache(self.root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertFalse((self.root / ".refresh.lock").exists())
        self.assertEqual(self._release_artifacts(), [])

    def test_download_failure_preserves_previous_pointer_and_generation(self) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            previous_pointer = self._pointer_payload()
            result = StockCache(
                self.root,
                self._generation_c_client(interrupt_offers=True),
            ).refresh(self.config)

        self.assertEqual(first.status, "updated")
        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.generation_id, first.generation_id)
        self.assertEqual(self._pointer_payload(), previous_pointer)
        pointer = json.loads(previous_pointer)
        self.assertEqual(
            [path.name for path in (self.root / "generations").iterdir()],
            [pointer["directory_name"]],
        )

    def test_failed_refresh_preserves_failure_and_lock_release_warnings(self) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            previous_pointer = self._pointer_payload()
            self.windows.flat_delete_failures_remaining = 20

            result = StockCache(
                self.root,
                self._generation_c_client(interrupt_offers=True),
            ).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(
            result.warning_codes,
            ("network_error", "cache_cleanup_incomplete"),
        )
        self.assertEqual(result.generation_id, first.generation_id)
        self.assertEqual(self._pointer_payload(), previous_pointer)
        self.assertTrue(
            any(path.is_dir() for path in self.root.glob(".refresh.lock.*"))
        )

    def test_failure_fallback_reloads_current_after_runtime_status_race(self) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            previous = cache_module.CacheState.load(self.root)
            second = StockCache(
                self.root, self._generation_c_client()
            ).refresh(self.config)
            lock = cache_module._WindowsCacheLock.acquire(self.root)
            cache = StockCache(self.root, self._generation_c_client())
            with patch.object(
                cache,
                "_record_runtime_status_windows",
                side_effect=StockError(
                    "cache_locked", "Активное поколение кэша изменилось", 6
                ),
            ):
                result = cache._finalize_windows_failure(
                    lock, previous, "network_error"
                )

        self.assertIsNotNone(previous)
        self.assertIsNotNone(result)
        self.assertNotEqual(first.generation_id, second.generation_id)
        self.assertEqual(result.generation_id, second.generation_id)

    def test_failure_finalizer_releases_lock_on_unexpected_persistence_error(
        self,
    ) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            StockCache(self.root, FakeHttpClient()).refresh(self.config)
            previous = cache_module.CacheState.load(self.root)
            lock = cache_module._WindowsCacheLock.acquire(self.root)
            cache = StockCache(self.root, self._generation_c_client())
            with patch.object(
                cache,
                "_record_runtime_status_windows",
                side_effect=RuntimeError("synthetic persistence failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "synthetic persistence failure"
                ):
                    cache._finalize_windows_failure(
                        lock, previous, "network_error"
                    )

        self.assertFalse((self.root / ".refresh.lock").exists())
        self.assertEqual(self._release_artifacts(), [])

    def test_checksum_failure_preserves_previous_pointer_and_generation(self) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            previous_pointer = self._pointer_payload()
            result = StockCache(
                self.root,
                self._generation_c_client(corrupt_products=True),
            ).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.generation_id, first.generation_id)
        self.assertEqual(self._pointer_payload(), previous_pointer)

    def test_windows_filesystem_verify_error_uses_integrity_warning(self) -> None:
        original_verify = LocalWindowsRoot.verify_file
        failed = False

        def fail_products_verification(
            session: LocalWindowsRoot,
            parent_parts: tuple[str, ...],
            name: str,
            *,
            expected_bytes: int,
            expected_sha256: str,
            progress: object | None = None,
        ) -> None:
            nonlocal failed
            if (
                len(parent_parts) == 2
                and parent_parts[1].startswith(".staging-")
                and name == "products.jsonl"
                and not failed
            ):
                failed = True
                raise WindowsFilesystemError("synthetic Windows checksum failure")
            original_verify(
                session,
                parent_parts,
                name,
                expected_bytes=expected_bytes,
                expected_sha256=expected_sha256,
                progress=progress,  # type: ignore[arg-type]
            )

        with patch.object(
            cache_module, "_is_native_windows", return_value=True
        ), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            previous_pointer = self._pointer_payload()
            with patch.object(
                LocalWindowsRoot,
                "verify_file",
                fail_products_verification,
            ):
                result = StockCache(
                    self.root,
                    self._generation_c_client(),
                ).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_codes, ("download_integrity_failed",))
        self.assertEqual(result.generation_id, first.generation_id)
        self.assertEqual(self._pointer_payload(), previous_pointer)

    def test_windows_verification_preserves_cache_locked_error(self) -> None:
        original_verify = LocalWindowsRoot.verify_file

        def lose_lock_during_verification(
            session: LocalWindowsRoot,
            parent_parts: tuple[str, ...],
            name: str,
            *,
            expected_bytes: int,
            expected_sha256: str,
            progress: object | None = None,
        ) -> None:
            if len(parent_parts) == 2 and parent_parts[1].startswith(".staging-"):
                raise StockError("cache_locked", "synthetic lost ownership", 6)
            original_verify(
                session,
                parent_parts,
                name,
                expected_bytes=expected_bytes,
                expected_sha256=expected_sha256,
                progress=progress,  # type: ignore[arg-type]
            )

        with patch.object(
            cache_module, "_is_native_windows", return_value=True
        ), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            with patch.object(
                LocalWindowsRoot,
                "verify_file",
                lose_lock_during_verification,
            ):
                result = StockCache(
                    self.root,
                    self._generation_c_client(),
                ).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_codes, ("cache_locked",))
        self.assertEqual(result.generation_id, first.generation_id)

    def test_post_rename_generation_cleanup_failure_blocks_redownload(self) -> None:
        third_client = FakeHttpClient(
            response=HttpResponse(
                status=200,
                headers={"ETag": f'"{GENERATION_D}"'},
                body=manifest_bytes(GENERATION_D),
            )
        )

        with patch.object(
            cache_module, "_is_native_windows", return_value=True
        ), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            StockCache(self.root, FakeHttpClient()).refresh(self.config)
            self.windows.flat_delete_failures_remaining = 1
            second = StockCache(
                self.root, self._generation_c_client()
            ).refresh(self.config)
            pointer_after_second = self._pointer_payload()
            quarantines_after_second = sorted(
                path.name
                for path in (self.root / "generations").iterdir()
                if ".delete-" in path.name
            )

            third = StockCache(self.root, third_client).refresh(self.config)

        self.assertEqual(second.status, "updated")
        self.assertEqual(second.warning_codes, ("cache_cleanup_incomplete",))
        self.assertEqual(third.status, "stale_cache")
        self.assertEqual(third.warning_codes, ("cache_cleanup_incomplete",))
        self.assertEqual(self._pointer_payload(), pointer_after_second)
        self.assertEqual(third_client.download_calls, [])
        self.assertEqual(len(quarantines_after_second), 1)
        self.assertEqual(
            sorted(
                path.name
                for path in (self.root / "generations").iterdir()
                if ".delete-" in path.name
            ),
            quarantines_after_second,
        )

    def test_shared_cache_fixture_preserves_previous_generation_on_corrupt_download(
        self,
    ) -> None:
        with patch.object(
            cache_module, "_is_native_windows", return_value=True
        ), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            CacheFixture(self.root).seed_generation()
            previous_pointer = self._pointer_payload()
            result = StockCache(
                self.root,
                self._generation_c_client(corrupt_products=True),
            ).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.generation_id, "a" * 64)
        self.assertEqual(self._pointer_payload(), previous_pointer)

    def test_activation_failure_preserves_previous_pointer_and_generation(self) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            previous_pointer = self._pointer_payload()
            original_rename = LocalWindowsRoot.rename_directory

            def fail_generation_publish(
                session: LocalWindowsRoot,
                parent_parts: tuple[str, ...],
                source_name: str,
                expected: WindowsIdentity,
                destination_name: str,
            ) -> None:
                if source_name.startswith(".staging-"):
                    raise OSError("synthetic activation failure")
                original_rename(
                    session,
                    parent_parts,
                    source_name,
                    expected,
                    destination_name,
                )

            with patch.object(
                LocalWindowsRoot,
                "rename_directory",
                new=fail_generation_publish,
            ):
                result = StockCache(
                    self.root, self._generation_c_client()
                ).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.generation_id, first.generation_id)
        self.assertEqual(self._pointer_payload(), previous_pointer)

    def test_pre_pointer_activation_failure_confirms_previous_pointer(self) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            previous_pointer = self._pointer_payload()
            original_replace = LocalWindowsRoot.replace_file_cas
            injected = False

            def fail_runtime_publish(
                session: LocalWindowsRoot,
                parent_parts: tuple[str, ...],
                name: str,
                *,
                expected: bytes | None,
                payload: bytes,
            ) -> WindowsIdentity:
                nonlocal injected
                if name.startswith(".runtime-status-") and not injected:
                    injected = True
                    raise OSError("synthetic runtime publication failure")
                return original_replace(
                    session,
                    parent_parts,
                    name,
                    expected=expected,
                    payload=payload,
                )

            with patch.object(
                LocalWindowsRoot,
                "replace_file_cas",
                new=fail_runtime_publish,
            ):
                result = StockCache(
                    self.root, self._generation_c_client()
                ).refresh(self.config)

        self.assertTrue(injected)
        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.generation_id, first.generation_id)
        self.assertEqual(self._pointer_payload(), previous_pointer)

    def test_post_rename_activation_failure_removes_inactive_published_generation(
        self,
    ) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            previous_pointer = self._pointer_payload()
            self.windows.generation_publish_failures_remaining = 1
            result = StockCache(
                self.root, self._generation_c_client()
            ).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.generation_id, first.generation_id)
        self.assertEqual(self._pointer_payload(), previous_pointer)
        pointer = json.loads(previous_pointer)
        self.assertEqual(
            [path.name for path in (self.root / "generations").iterdir()],
            [pointer["directory_name"]],
        )

    def test_post_publication_failure_rolls_back_previous_pointer(self) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            previous_pointer = self._pointer_payload()
            original_replace = LocalWindowsRoot.replace_file_cas
            injected = False

            def fail_after_pointer_publish(
                session: LocalWindowsRoot,
                parent_parts: tuple[str, ...],
                name: str,
                *,
                expected: bytes | None,
                payload: bytes,
            ) -> WindowsIdentity:
                nonlocal injected
                identity = original_replace(
                    session,
                    parent_parts,
                    name,
                    expected=expected,
                    payload=payload,
                )
                if name == "current.json" and not injected:
                    injected = True
                    raise OSError("synthetic post-publication failure")
                return identity

            with patch.object(
                LocalWindowsRoot,
                "replace_file_cas",
                new=fail_after_pointer_publish,
            ):
                result = StockCache(
                    self.root, self._generation_c_client()
                ).refresh(self.config)

        self.assertTrue(injected)
        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.generation_id, first.generation_id)
        self.assertEqual(self._pointer_payload(), previous_pointer)
        pointer = json.loads(previous_pointer)
        self.assertEqual(
            [path.name for path in (self.root / "generations").iterdir()],
            [pointer["directory_name"]],
        )

    def test_unconfirmed_pointer_rollback_fails_closed_without_loading_new_pointer(
        self,
    ) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            previous_pointer = json.loads(self._pointer_payload())
            previous_directory = (
                self.root / "generations" / previous_pointer["directory_name"]
            )
            original_replace = LocalWindowsRoot.replace_file_cas
            pointer_attempts = 0

            def fail_publish_then_rollback(
                session: LocalWindowsRoot,
                parent_parts: tuple[str, ...],
                name: str,
                *,
                expected: bytes | None,
                payload: bytes,
            ) -> WindowsIdentity:
                nonlocal pointer_attempts
                if name != "current.json":
                    return original_replace(
                        session,
                        parent_parts,
                        name,
                        expected=expected,
                        payload=payload,
                    )
                pointer_attempts += 1
                if pointer_attempts == 1:
                    identity = original_replace(
                        session,
                        parent_parts,
                        name,
                        expected=expected,
                        payload=payload,
                    )
                    raise OSError("synthetic post-publication failure")
                raise OSError("synthetic rollback failure")

            with patch.object(
                LocalWindowsRoot,
                "replace_file_cas",
                new=fail_publish_then_rollback,
            ):
                with self.assertRaises(StockError) as captured:
                    StockCache(
                        self.root, self._generation_c_client()
                    ).refresh(self.config)

        self.assertEqual(pointer_attempts, 2)
        self.assertEqual(captured.exception.code, "cache_unavailable")
        self.assertTrue(previous_directory.is_dir())
        self.assertEqual(first.generation_id, previous_pointer["generation_id"])
        self.assertEqual(
            json.loads(self._pointer_payload())["generation_id"],
            GENERATION_C,
        )

    def test_expired_windows_lock_is_reclaimed(self) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            StockCache(self.root, FakeHttpClient()).refresh(self.config)
            token = "0123456789abcdef0123456789abcdef"
            stale_at = time.time() - cache_module._LOCK_TTL_SECONDS - 1
            lock = self.root / ".refresh.lock"
            lock.mkdir()
            (lock / "owner.json").write_text(
                json.dumps({"token": token, "created_at": stale_at}),
                encoding="utf-8",
            )
            heartbeat = lock / f"heartbeat-{token}"
            heartbeat.write_bytes(b"")
            os.utime(heartbeat, (stale_at, stale_at))

            result = StockCache(
                self.root, self._generation_c_client()
            ).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertFalse(lock.exists())
        self.assertEqual(self._release_artifacts(), [])

    def test_cleanup_failure_preserves_foreign_nested_target(self) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            previous_pointer = self._pointer_payload()
            foreign = self.root / "generations" / "generation-foreign"
            marker = foreign / "nested" / "keep.txt"
            marker.parent.mkdir(parents=True)
            marker.write_bytes(b"foreign-safe")

            result = StockCache(
                self.root, self._generation_c_client()
            ).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_codes, ("cache_cleanup_incomplete",))
        self.assertEqual(result.generation_id, first.generation_id)
        self.assertEqual(self._pointer_payload(), previous_pointer)
        self.assertEqual(marker.read_bytes(), b"foreign-safe")

    def test_cleanup_preserves_flat_foreign_generation_without_ownership_receipt(
        self,
    ) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            previous_pointer = self._pointer_payload()
            foreign = self.root / "generations" / "generation-foreign"
            foreign.mkdir()
            marker = foreign / "keep.txt"
            marker.write_bytes(b"foreign-safe")

            result = StockCache(
                self.root, self._generation_c_client()
            ).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_codes, ("cache_cleanup_incomplete",))
        self.assertEqual(result.generation_id, first.generation_id)
        self.assertEqual(self._pointer_payload(), previous_pointer)
        self.assertEqual(marker.read_bytes(), b"foreign-safe")

    def test_cleanup_preserves_foreign_runtime_like_file_without_ownership_receipt(
        self,
    ) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            previous_pointer = self._pointer_payload()
            foreign = self.root / ".runtime-status-generation-foreign.json"
            foreign_payload = json.dumps(
                {
                    "generation_id": "generation-foreign",
                    "verified_at": "2026-08-28T00:00:00+00:00",
                    "stale": False,
                    "warning_codes": [],
                    "revision": "1" * 32,
                    "ownership": {
                        "kind": "papa-shin-stock-runtime-status",
                        "schema_version": 1,
                        "root_ownership_token": "0" * 32,
                        "generation_ownership_token": "2" * 32,
                        "directory_name": "generation-foreign",
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8")
            foreign.write_bytes(foreign_payload)

            result = StockCache(
                self.root, self._generation_c_client()
            ).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_codes, ("cache_cleanup_incomplete",))
        self.assertEqual(result.generation_id, first.generation_id)
        self.assertEqual(self._pointer_payload(), previous_pointer)
        self.assertEqual(foreign.read_bytes(), foreign_payload)

    def test_cleanup_rejects_unexpected_hardlink_without_deleting_it(self) -> None:
        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
            foreign = self.root / "generations" / "generation-hardlinked"
            foreign.mkdir()
            outside = Path(self.temporary.name) / "outside-safe"
            outside.write_bytes(b"foreign-safe")
            os.link(outside, foreign / "manifest.json")

            result = StockCache(
                self.root, self._generation_c_client()
            ).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_codes, ("cache_cleanup_incomplete",))
        self.assertEqual(result.generation_id, first.generation_id)
        self.assertEqual(outside.read_bytes(), b"foreign-safe")
        self.assertTrue((foreign / "manifest.json").exists())

    def test_two_parallel_refreshes_do_not_corrupt_cache(self) -> None:
        started = threading.Event()
        proceed = threading.Event()
        results: list[object] = []
        errors: list[BaseException] = []

        class BlockingClient(FakeHttpClient):
            def get_manifest(
                nested_self,
                etag: str | None = None,
                last_modified: str | None = None,
            ) -> HttpResponse:
                started.set()
                if not proceed.wait(5):
                    raise RuntimeError("parallel test timed out")
                return super().get_manifest(etag, last_modified)

        def refresh(client: FakeHttpClient) -> None:
            try:
                results.append(StockCache(self.root, client).refresh(self.config))
            except BaseException as error:
                errors.append(error)

        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = threading.Thread(target=refresh, args=(BlockingClient(),))
            first.start()
            self.assertTrue(started.wait(5))
            second = threading.Thread(
                target=refresh, args=(self._generation_c_client(),)
            )
            second.start()
            second.join(5)
            proceed.set()
            first.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([result.status for result in results], ["updated"])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StockError)
        self.assertEqual(errors[0].code, "cache_locked")
        pointer = json.loads(self._pointer_payload())
        self.assertEqual(
            [path.name for path in (self.root / "generations").iterdir()],
            [pointer["directory_name"]],
        )


@unittest.skipUnless(os.name == "nt", "Нативные Win32 semantics проверяются в Windows CI")
class WindowsCacheWorkflowNativeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "cache"
        self.config = StockConfig(
            manifest_url="https://stock.example.test/manifest.json",
            username="reader",
            password="secret",
            product_id_field="robotyre_product_id",
            offer_product_id_field="robotyre_product_id",
            cache_dir=self.root,
        )

    def test_two_real_refreshes_cleanup_old_generation_and_release_lock(self) -> None:
        first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
        second = StockCache(
            self.root,
            FakeHttpClient(
                response=HttpResponse(
                    status=200,
                    headers={"ETag": f'"{GENERATION_C}"'},
                    body=manifest_bytes(GENERATION_C),
                )
            ),
        ).refresh(self.config)

        self.assertEqual((first.status, second.status), ("updated", "updated"))
        pointer = json.loads((self.root / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["generation_id"], GENERATION_C)
        self.assertEqual(
            [path.name for path in (self.root / "generations").iterdir()],
            [pointer["directory_name"]],
        )
        self.assertFalse((self.root / ".refresh.lock").exists())
        self.assertEqual(list(self.root.glob(".refresh.lock.release-*")), [])

    def test_snapshot_search_survives_native_cleanup_and_successor_is_current(
        self,
    ) -> None:
        class GenerationClient:
            def __init__(self, generation_id: str) -> None:
                self.generation_id = generation_id
                self.products, self.offers = robotyre_payloads(generation_id)

            def get_manifest(
                self,
                etag: str | None = None,
                last_modified: str | None = None,
            ) -> HttpResponse:
                body = robotyre_manifest_bytes(
                    self.products,
                    self.offers,
                    self.generation_id,
                )
                return HttpResponse(status=200, headers={}, body=body)

            def download(
                self,
                url: str,
                destination: Path,
                expected_bytes: int,
                expected_sha256: str,
                progress: object | None = None,
            ) -> DownloadReceipt:
                payload = (
                    self.products
                    if destination.name == "products.jsonl"
                    else self.offers
                )
                destination.write_bytes(payload)
                if callable(progress):
                    progress()
                return DownloadReceipt(
                    bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()
                )

        config = StockConfig(
            manifest_url="https://stock.example.test/manifest.json",
            username="reader",
            password="secret",
            product_id_field="robotyre_product_id",
            offer_product_id_field="robotyre_product_id",
            cache_dir=self.root,
        )
        query = SearchQuery.from_args(argparse.Namespace())
        generation_a = "a" * 64
        generation_b = "b" * 64
        cache = StockCache(self.root, GenerationClient(generation_a))
        cache.refresh(config)

        with cache.generation_snapshot() as snapshot_a:
            StockCache(
                self.root, GenerationClient(generation_b)
            ).refresh(config)
            result_a = StockSearcher(snapshot_a, config).search(query)

        with cache.generation_snapshot() as snapshot_b:
            result_b = StockSearcher(snapshot_b, config).search(query)

        self.assertEqual(result_a.generation["id"], generation_a)
        self.assertEqual(result_b.generation["id"], generation_b)
        self.assertEqual(result_a.summary, result_b.summary)

    def test_two_real_parallel_refreshes_leave_one_generation_and_no_lock_artifacts(
        self,
    ) -> None:
        entered_manifest = threading.Event()
        continue_refresh = threading.Event()
        results: list[object] = []
        errors: list[BaseException] = []

        class BlockingClient(FakeHttpClient):
            def get_manifest(
                nested_self,
                etag: str | None = None,
                last_modified: str | None = None,
            ) -> HttpResponse:
                entered_manifest.set()
                if not continue_refresh.wait(timeout=5):
                    raise RuntimeError("synthetic parallel refresh timeout")
                return super().get_manifest(etag, last_modified)

        def refresh(client: FakeHttpClient) -> None:
            try:
                results.append(StockCache(self.root, client).refresh(self.config))
            except BaseException as error:
                errors.append(error)

        first = threading.Thread(target=refresh, args=(BlockingClient(),))
        first.start()
        self.assertTrue(entered_manifest.wait(timeout=5))
        second = threading.Thread(target=refresh, args=(FakeHttpClient(),))
        second.start()
        second.join(timeout=5)
        continue_refresh.set()
        first.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([result.status for result in results], ["updated"])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StockError)
        self.assertEqual(errors[0].code, "cache_locked")
        pointer = json.loads((self.root / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [path.name for path in (self.root / "generations").iterdir()],
            [pointer["directory_name"]],
        )
        self.assertFalse((self.root / ".refresh.lock").exists())
        self.assertEqual(list(self.root.glob(".refresh.lock.release-*")), [])


if __name__ == "__main__":
    unittest.main()
