from __future__ import annotations

import json
import hashlib
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

from tests.test_cache import FakeHttpClient, manifest_bytes

from papa_shin_stock import cache as cache_module
from papa_shin_stock.cache import StockCache
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.http_client import HttpResponse
from papa_shin_stock._windows_fs import MarkerEvidence, WindowsIdentity


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
            product_id_field="product_id",
            offer_product_id_field="product_id",
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
                headers={"ETag": '"generation-c"'},
                body=manifest_bytes("generation-c"),
            ),
            **kwargs,
        )

    def test_two_sequential_refreshes_are_updated_and_cleanup_old_generation(self) -> None:
        first_client = FakeHttpClient()
        second_client = FakeHttpClient(
            response=HttpResponse(
                status=200,
                headers={"ETag": '"generation-c"'},
                body=manifest_bytes("generation-c"),
            )
        )

        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            first = StockCache(self.root, first_client).refresh(self.config)
            second = StockCache(self.root, second_client).refresh(self.config)

        self.assertEqual((first.status, second.status), ("updated", "updated"))
        pointer = json.loads((self.root / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["generation_id"], "generation-c")
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
        self.assertIsNone(result.warning_code)
        self.assertFalse((self.root / ".refresh.lock").exists())
        self.assertEqual(self._release_artifacts(), [])

    def test_persistent_release_cleanup_failure_is_reported_and_preserved(self) -> None:
        self.windows.flat_delete_failures_remaining = 20

        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            result = StockCache(self.root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertEqual(result.warning_code, "cache_cleanup_incomplete")
        self.assertFalse((self.root / ".refresh.lock").exists())
        retained = self._release_artifacts()
        self.assertEqual(len(retained), 1)
        self.assertTrue((retained[0] / "owner.json").is_file())

    def test_foreign_canonical_lock_at_release_is_preserved_and_reported(self) -> None:
        self.windows.replace_canonical_lock_before_delete = True

        with patch.object(cache_module, "_is_native_windows", return_value=True), patch.object(
            cache_module, "_windows_filesystem", return_value=self.windows
        ):
            result = StockCache(self.root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertEqual(result.warning_code, "cache_cleanup_incomplete")
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
        self.assertEqual(result.warning_code, "cache_cleanup_incomplete")
        self.assertEqual(result.generation_id, first.generation_id)
        self.assertEqual(self._pointer_payload(), previous_pointer)
        self.assertEqual(marker.read_bytes(), b"foreign-safe")

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
        self.assertEqual(result.warning_code, "cache_cleanup_incomplete")
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
            product_id_field="product_id",
            offer_product_id_field="product_id",
            cache_dir=self.root,
        )

    def test_two_real_refreshes_cleanup_old_generation_and_release_lock(self) -> None:
        first = StockCache(self.root, FakeHttpClient()).refresh(self.config)
        second = StockCache(
            self.root,
            FakeHttpClient(
                response=HttpResponse(
                    status=200,
                    headers={"ETag": '"generation-c"'},
                    body=manifest_bytes("generation-c"),
                )
            ),
        ).refresh(self.config)

        self.assertEqual((first.status, second.status), ("updated", "updated"))
        pointer = json.loads((self.root / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["generation_id"], "generation-c")
        self.assertEqual(
            [path.name for path in (self.root / "generations").iterdir()],
            [pointer["directory_name"]],
        )
        self.assertFalse((self.root / ".refresh.lock").exists())
        self.assertEqual(list(self.root.glob(".refresh.lock.release-*")), [])


if __name__ == "__main__":
    unittest.main()
