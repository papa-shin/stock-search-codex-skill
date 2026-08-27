from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from papa_shin_stock import cache as cache_module
from papa_shin_stock.cache import (
    CacheLock,
    CacheState,
    CurrentPointer,
    StockCache,
    _fsync_directory,
)
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.http_client import DownloadReceipt, HttpResponse
import fetch_stock


PRODUCTS = b'{"product_id":"synthetic-product","content_generation_id":"generation-b"}\n'
OFFERS = b'{"product_id":"synthetic-product","content_generation_id":"generation-b"}\n'


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def manifest_bytes(generation_id: str = "generation-b") -> bytes:
    return json.dumps(
        {
            "generation_id": generation_id,
            "generated_at": "2026-08-27T10:00:00+00:00",
            "files": {
                "products": {
                    "url": "products.jsonl",
                    "bytes": len(PRODUCTS),
                    "sha256": sha256(PRODUCTS),
                },
                "offers": {
                    "url": "/offers.jsonl",
                    "bytes": len(OFFERS),
                    "sha256": sha256(OFFERS),
                },
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


class FakeHttpClient:
    def __init__(
        self,
        *,
        response: HttpResponse | None = None,
        corrupt_products: bool = False,
        interrupt_offers: bool = False,
        displace_lock: bool = False,
    ) -> None:
        self.response = response or HttpResponse(
            status=200,
            headers={
                "ETag": '"generation-b"',
                "Last-Modified": "Thu, 27 Aug 2026 10:00:00 GMT",
            },
            body=manifest_bytes(),
        )
        self.corrupt_products = corrupt_products
        self.interrupt_offers = interrupt_offers
        self.displace_lock = displace_lock
        self.manifest_calls: list[tuple[str | None, str | None]] = []
        self.download_calls: list[str] = []

    def get_manifest(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> HttpResponse:
        self.manifest_calls.append((etag, last_modified))
        return self.response

    def download(
        self,
        url: str,
        destination: Path,
        expected_bytes: int,
        expected_sha256: str,
        progress: object | None = None,
    ) -> DownloadReceipt:
        self.download_calls.append(url)
        if destination.name == "products.jsonl":
            payload = b"corrupt" if self.corrupt_products else PRODUCTS
        else:
            payload = OFFERS
            if self.interrupt_offers:
                destination.write_bytes(payload[:5])
                raise StockError("network_error", "Синтетический обрыв загрузки", 3)

        destination.write_bytes(payload)
        if callable(progress):
            progress()
        if self.displace_lock and destination.name == "offers.jsonl":
            owner_path = destination.parents[2] / ".refresh.lock" / "owner.json"
            owner_path.write_text(
                json.dumps({"token": "replacement-writer", "created_at": time.time()}),
                encoding="utf-8",
            )
        return DownloadReceipt(bytes=len(payload), sha256=sha256(payload))


class CacheFixture:
    def __init__(self, root: Path) -> None:
        self.root = root

    def seed_generation(
        self,
        generation_id: str = "generation-a",
        directory_name: str = "generation-existing",
    ) -> None:
        generation = self.root / "generations" / directory_name
        generation.mkdir(parents=True)
        (generation / "manifest.json").write_bytes(manifest_bytes(generation_id))
        (generation / "products.jsonl").write_bytes(PRODUCTS)
        (generation / "offers.jsonl").write_bytes(OFFERS)
        (generation / "state.json").write_text(
            json.dumps(
                {
                    "generation_id": generation_id,
                    "generated_at": "2026-08-26T10:00:00+00:00",
                    "checked_at": "2026-08-26T10:01:00+00:00",
                    "manifest_etag": '"generation-a"',
                    "manifest_last_modified": "Wed, 26 Aug 2026 10:00:00 GMT",
                }
            ),
            encoding="utf-8",
        )
        (self.root / "current.json").write_text(
            json.dumps(
                {
                    "generation_id": generation_id,
                    "directory_name": directory_name,
                }
            ),
            encoding="utf-8",
        )

    def current_generation_id(self) -> str:
        return json.loads((self.root / "current.json").read_text(encoding="utf-8"))[
            "generation_id"
        ]


class StockCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cache_root = Path(self.temp_dir.name) / "cache"
        self.cache_root.mkdir()
        self.fixture = CacheFixture(self.cache_root)
        self.config = StockConfig(
            manifest_url="https://stock.example.test/manifest.json",
            username="synthetic-user",
            password="synthetic-password",
            product_id_field="product_id",
            offer_product_id_field="product_id",
            cache_dir=self.cache_root,
        )

    def _expire_current_refresh_lock(self) -> None:
        lock_path = self.cache_root / ".refresh.lock"
        owner_path = lock_path / "owner.json"
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        expired = time.time() - cache_module._LOCK_TTL_SECONDS - 1
        owner_path.write_text(
            json.dumps({"token": owner["token"], "created_at": expired}),
            encoding="utf-8",
        )
        os.utime(lock_path / f"heartbeat-{owner['token']}", (expired, expired))

    def test_success_activates_verified_generation_with_plain_json_pointer(self) -> None:
        cache = StockCache(self.cache_root, FakeHttpClient())

        result = cache.refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertEqual(cache.current_generation().generation_id, "generation-b")
        self.assertEqual(cache.current_generation().products.read_bytes(), PRODUCTS)
        self.assertEqual(cache.current_generation().offers.read_bytes(), OFFERS)
        self.assertFalse((self.cache_root / "current.json").is_symlink())
        self.assertEqual(
            result.to_public_dict()["generation"]["id"], "generation-b"
        )

    def test_corrupt_download_returns_stale_cache_without_replacing_current(self) -> None:
        self.fixture.seed_generation()
        cache = StockCache(
            self.cache_root, FakeHttpClient(corrupt_products=True)
        )

        result = cache.refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_code, "download_integrity_failed")
        self.assertEqual(self.fixture.current_generation_id(), "generation-a")
        self.assertEqual(
            result.to_public_dict()["warnings"][0]["code"],
            "download_integrity_failed",
        )

    def test_corrupt_download_without_readable_cache_is_nonzero_error(self) -> None:
        cache = StockCache(
            self.cache_root, FakeHttpClient(corrupt_products=True)
        )

        with self.assertRaisesRegex(StockError, "download_integrity_failed") as raised:
            cache.refresh(self.config)

        self.assertNotEqual(raised.exception.exit_code, 0)
        self.assertFalse((self.cache_root / "current.json").exists())

    def test_interrupted_download_preserves_previous_generation(self) -> None:
        self.fixture.seed_generation()
        cache = StockCache(
            self.cache_root, FakeHttpClient(interrupt_offers=True)
        )

        result = cache.refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_code, "network_error")
        self.assertEqual(self.fixture.current_generation_id(), "generation-a")
        self.assertEqual(
            sorted(path.name for path in (self.cache_root / "generations").iterdir()),
            ["generation-existing"],
        )

    def test_not_modified_uses_cached_validators_and_generation(self) -> None:
        self.fixture.seed_generation()
        client = FakeHttpClient(
            response=HttpResponse(status=304, headers={}, body=b"")
        )
        cache = StockCache(self.cache_root, client)

        result = cache.refresh(self.config)

        self.assertEqual(result.status, "not_modified")
        self.assertEqual(
            client.manifest_calls,
            [('"generation-a"', "Wed, 26 Aug 2026 10:00:00 GMT")],
        )
        self.assertEqual(self.fixture.current_generation_id(), "generation-a")

    def test_not_modified_without_readable_cache_fails_closed(self) -> None:
        cache = StockCache(
            self.cache_root,
            FakeHttpClient(response=HttpResponse(status=304, headers={}, body=b"")),
        )

        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            cache.refresh(self.config)

    def test_active_lock_returns_stale_cache_when_previous_is_readable(self) -> None:
        self.fixture.seed_generation()
        lock = self.cache_root / ".refresh.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(
            json.dumps({"token": "active-writer", "created_at": time.time()}),
            encoding="utf-8",
        )

        result = StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_code, "cache_locked")
        self.assertEqual(self.fixture.current_generation_id(), "generation-a")

    def test_active_lock_warning_is_not_persisted_into_active_generation(self) -> None:
        self.fixture.seed_generation()
        lock = self.cache_root / ".refresh.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(
            json.dumps({"token": "active-writer", "created_at": time.time()}),
            encoding="utf-8",
        )

        result = StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)
        current = CacheState.load(self.cache_root)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_code, "cache_locked")
        self.assertIsNotNone(current)
        self.assertFalse(current.stale)
        self.assertIsNone(current.warning_code)
        self.assertFalse(
            (self.cache_root / ".runtime-status-generation-existing.json").exists()
        )

    def test_error_status_cas_does_not_mark_concurrent_generation_stale(self) -> None:
        self.fixture.seed_generation()
        client = FakeHttpClient()
        real_release = CacheLock.release
        activated = False

        def release_then_activate(lock: CacheLock) -> None:
            nonlocal activated
            real_release(lock)
            if not activated:
                activated = True
                self.fixture.seed_generation(
                    generation_id="generation-concurrent",
                    directory_name="generation-concurrent",
                )

        with patch.object(
            client,
            "get_manifest",
            side_effect=StockError(
                "network_error", "Синтетическая ошибка обновления", 3
            ),
        ):
            with patch.object(CacheLock, "release", release_then_activate):
                result = StockCache(self.cache_root, client).refresh(self.config)

        current = CacheState.load(self.cache_root)
        self.assertTrue(activated)
        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.generation_id, "generation-a")
        self.assertEqual(result.warning_code, "network_error")
        self.assertIsNotNone(current)
        self.assertEqual(current.generation_id, "generation-concurrent")
        self.assertFalse(current.stale)
        self.assertIsNone(current.warning_code)
        self.assertFalse(
            (self.cache_root / ".runtime-status-generation-concurrent.json").exists()
        )

    def test_fallback_lock_root_mkdir_error_stays_safe_json(self) -> None:
        self.fixture.seed_generation()
        client = FakeHttpClient()
        real_mkdir = Path.mkdir
        root_mkdir_calls = 0

        def fail_second_root_mkdir(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal root_mkdir_calls
            if path == self.cache_root:
                root_mkdir_calls += 1
                if root_mkdir_calls == 2:
                    raise PermissionError("/private/cache/root")
            real_mkdir(path, *args, **kwargs)

        def refresh_with_fallback() -> dict[str, object]:
            return (
                StockCache(self.cache_root, client)
                .refresh(self.config)
                .to_public_dict()
            )

        output = StringIO()
        errors = StringIO()
        with patch.object(
            client,
            "get_manifest",
            side_effect=StockError(
                "network_error", "Синтетическая ошибка обновления", 3
            ),
        ):
            with patch.object(Path, "mkdir", fail_second_root_mkdir):
                with patch.object(
                    fetch_stock, "refresh_default", side_effect=refresh_with_fallback
                ):
                    with redirect_stdout(output), redirect_stderr(errors):
                        exit_code = fetch_stock.main()

        public = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(public["status"], "stale_cache")
        self.assertEqual(public["warnings"][0]["code"], "network_error")
        self.assertEqual(output.getvalue().count("\n"), 1)
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn("/private/", output.getvalue())

    def test_cleanup_removes_only_exact_runtime_status_orphan_names(self) -> None:
        self.fixture.seed_generation()
        exact_status = self.cache_root / ".runtime-status-generation-old.json"
        exact_temp = (
            self.cache_root
            / "..runtime-status-generation-old.json.0123456789abcdef0123456789abcdef.tmp"
        )
        ambiguous = (
            self.cache_root / "..runtime-status-generation-old.json.short.tmp"
        )
        unrelated = self.cache_root / ".runtime-status-generation-old.json.backup"
        for path in (exact_status, exact_temp, ambiguous, unrelated):
            path.write_text("synthetic", encoding="utf-8")

        with CacheLock.acquire(self.cache_root) as lock:
            warning = StockCache(
                self.cache_root, FakeHttpClient()
            )._cleanup_inactive_generations(lock)

        self.assertIsNone(warning)
        self.assertFalse(exact_status.exists())
        self.assertFalse(exact_temp.exists())
        self.assertTrue(ambiguous.exists())
        self.assertTrue(unrelated.exists())

    def test_commit_lock_uses_windows_one_byte_locking_backend(self) -> None:
        class FakeMsvcrt:
            LK_LOCK = 1
            LK_UNLCK = 2

            def __init__(self) -> None:
                self.calls: list[tuple[int, int, int]] = []

            def locking(self, descriptor: int, mode: int, length: int) -> None:
                self.calls.append((descriptor, mode, length))

        backend = FakeMsvcrt()
        with patch.object(cache_module, "_fcntl", None):
            with patch.object(cache_module, "_msvcrt", backend):
                with patch.object(cache_module.os, "lseek", return_value=0) as seek:
                    cache_module._lock_commit_descriptor(17)
                    cache_module._unlock_commit_descriptor(17)

        self.assertEqual(
            backend.calls,
            [(17, backend.LK_LOCK, 1), (17, backend.LK_UNLCK, 1)],
        )
        self.assertEqual(seek.call_count, 2)

    def test_active_lock_without_cache_is_cache_locked_error(self) -> None:
        lock = self.cache_root / ".refresh.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(
            json.dumps({"token": "active-writer", "created_at": time.time()}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(StockError, "cache_locked"):
            StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

    def test_heartbeat_prevents_reclaim_after_original_timestamp_expires(self) -> None:
        lock = CacheLock.acquire(self.cache_root)
        (lock.path / "owner.json").write_text(
            json.dumps(
                {"token": lock.token, "created_at": time.time() - 30 * 60 - 1}
            ),
            encoding="utf-8",
        )
        stale_time = time.time() - 30 * 60 - 1
        os.utime(lock.path / f"heartbeat-{lock.token}", (stale_time, stale_time))

        lock.heartbeat()

        self.assertFalse(CacheLock._reclaim_stale(lock.path))
        lock.assert_owned()
        lock.release()

    def test_heartbeat_during_reclaim_restores_moved_lock(self) -> None:
        lock = CacheLock.acquire(self.cache_root)
        stale_time = time.time() - 30 * 60 - 1
        (lock.path / "owner.json").write_text(
            json.dumps({"token": lock.token, "created_at": stale_time}),
            encoding="utf-8",
        )
        heartbeat_path = lock.path / f"heartbeat-{lock.token}"
        os.utime(heartbeat_path, (stale_time, stale_time))
        real_rename = os.rename
        first_rename = True

        def heartbeat_then_rename(source: Path, destination: Path) -> None:
            nonlocal first_rename
            if first_rename:
                first_rename = False
                os.utime(heartbeat_path, None)
            real_rename(source, destination)

        with patch(
            "papa_shin_stock.cache.os.rename",
            side_effect=heartbeat_then_rename,
        ):
            reclaimed = CacheLock._reclaim_stale(lock.path)

        self.assertFalse(reclaimed)
        lock.assert_owned()
        lock.release()

    def test_previous_cache_hashing_emits_lock_heartbeats(self) -> None:
        self.fixture.seed_generation()
        beats: list[str] = []

        def record_heartbeat(lock: CacheLock) -> None:
            beats.append(lock.token)

        with patch.object(
            CacheLock, "heartbeat", record_heartbeat, create=True
        ):
            result = StockCache(
                self.cache_root,
                FakeHttpClient(
                    response=HttpResponse(status=304, headers={}, body=b"")
                ),
            ).refresh(self.config)

        self.assertEqual(result.status, "not_modified")
        self.assertGreaterEqual(len(beats), 2)

    def test_displacement_during_previous_hashing_stops_before_http(self) -> None:
        self.fixture.seed_generation()
        client = FakeHttpClient(
            response=HttpResponse(status=304, headers={}, body=b"")
        )
        real_heartbeat = CacheLock.heartbeat
        displaced = False

        def displace_on_heartbeat(lock: CacheLock) -> None:
            nonlocal displaced
            if not displaced:
                displaced = True
                (lock.path / "owner.json").write_text(
                    json.dumps(
                        {"token": "replacement-writer", "created_at": time.time()}
                    ),
                    encoding="utf-8",
                )
            real_heartbeat(lock)

        with patch.object(CacheLock, "heartbeat", displace_on_heartbeat):
            result = StockCache(self.cache_root, client).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_code, "cache_locked")
        self.assertEqual(client.manifest_calls, [])

    def test_displaced_owner_cannot_return_not_modified_success(self) -> None:
        self.fixture.seed_generation()
        cache_root = self.cache_root

        class DisplacingManifestClient(FakeHttpClient):
            def get_manifest(
                self, etag: str | None = None, last_modified: str | None = None
            ) -> HttpResponse:
                response = super().get_manifest(etag, last_modified)
                (cache_root / ".refresh.lock" / "owner.json").write_text(
                    json.dumps(
                        {"token": "replacement-writer", "created_at": time.time()}
                    ),
                    encoding="utf-8",
                )
                return response

        result = StockCache(
            self.cache_root,
            DisplacingManifestClient(
                response=HttpResponse(status=304, headers={}, body=b"")
            ),
        ).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_code, "cache_locked")
        self.assertEqual(self.fixture.current_generation_id(), "generation-a")

    def test_release_does_not_delete_lock_whose_owner_changed_after_read(self) -> None:
        lock = CacheLock.acquire(self.cache_root)
        real_read_owner = CacheLock._read_owner
        owner_changed = False

        def read_then_replace_owner(path: Path) -> tuple[str, float] | None:
            nonlocal owner_changed
            owner = real_read_owner(path)
            if path == lock.path and not owner_changed:
                owner_changed = True
                (path / "owner.json").write_text(
                    json.dumps(
                        {"token": "replacement-writer", "created_at": time.time()}
                    ),
                    encoding="utf-8",
                )
            return owner

        with patch.object(CacheLock, "_read_owner", side_effect=read_then_replace_owner):
            lock.release()

        self.assertTrue(lock.path.is_dir())
        self.assertEqual(
            json.loads((lock.path / "owner.json").read_text(encoding="utf-8"))[
                "token"
            ],
            "replacement-writer",
        )

    def test_lock_older_than_thirty_minutes_is_reclaimed(self) -> None:
        lock = self.cache_root / ".refresh.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(
            json.dumps(
                {"token": "stale-writer", "created_at": time.time() - 30 * 60 - 1}
            ),
            encoding="utf-8",
        )

        result = StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertFalse(lock.exists())

    def test_ownerless_lock_older_than_thirty_minutes_is_reclaimed(self) -> None:
        lock = self.cache_root / ".refresh.lock"
        lock.mkdir()
        stale_time = time.time() - 30 * 60 - 1
        os.utime(lock, (stale_time, stale_time))

        result = StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertFalse(lock.exists())

    def test_nan_owner_timestamp_does_not_bypass_directory_ttl(self) -> None:
        lock = self.cache_root / ".refresh.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(
            json.dumps({"token": "corrupt-writer", "created_at": float("nan")}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(StockError, "cache_locked"):
            StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertTrue(lock.is_dir())

    def test_infinite_owner_timestamp_uses_stale_directory_ttl(self) -> None:
        lock = self.cache_root / ".refresh.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(
            json.dumps({"token": "corrupt-writer", "created_at": float("inf")}),
            encoding="utf-8",
        )
        stale_time = time.time() - 30 * 60 - 1
        os.utime(lock, (stale_time, stale_time))

        result = StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertFalse(lock.exists())

    def test_far_future_owner_timestamp_uses_stale_directory_ttl(self) -> None:
        lock = self.cache_root / ".refresh.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(
            json.dumps(
                {"token": "corrupt-writer", "created_at": time.time() + 24 * 60 * 60}
            ),
            encoding="utf-8",
        )
        stale_time = time.time() - 30 * 60 - 1
        os.utime(lock, (stale_time, stale_time))

        result = StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertFalse(lock.exists())

    def test_huge_numeric_owner_timestamp_is_fail_closed_before_directory_ttl(
        self,
    ) -> None:
        lock = self.cache_root / ".refresh.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(
            '{"token":"corrupt-writer","created_at":' + "9" * 400 + "}",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(StockError, "cache_locked"):
            StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertTrue(lock.is_dir())

    def test_huge_numeric_owner_timestamp_uses_stale_directory_ttl(self) -> None:
        lock = self.cache_root / ".refresh.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(
            '{"token":"corrupt-writer","created_at":' + "9" * 400 + "}",
            encoding="utf-8",
        )
        stale_time = time.time() - 30 * 60 - 1
        os.utime(lock, (stale_time, stale_time))

        result = StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertFalse(lock.exists())

    def test_stale_reclaim_does_not_delete_changed_owner_token(self) -> None:
        lock = self.cache_root / ".refresh.lock"
        lock.mkdir()
        owner_path = lock / "owner.json"
        owner_path.write_text(
            json.dumps(
                {"token": "stale-writer", "created_at": time.time() - 30 * 60 - 1}
            ),
            encoding="utf-8",
        )
        real_rename = os.rename
        first_rename = True

        def replace_owner_before_rename(source: Path, destination: Path) -> None:
            nonlocal first_rename
            if first_rename:
                first_rename = False
                owner_path.write_text(
                    json.dumps(
                        {"token": "replacement-writer", "created_at": time.time()}
                    ),
                    encoding="utf-8",
                )
            real_rename(source, destination)

        with patch("papa_shin_stock.cache.os.rename", replace_owner_before_rename):
            with self.assertRaisesRegex(StockError, "cache_locked"):
                StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertTrue(lock.is_dir())
        self.assertEqual(
            json.loads(owner_path.read_text(encoding="utf-8"))["token"],
            "replacement-writer",
        )

    def test_displaced_writer_cannot_activate_generation(self) -> None:
        self.fixture.seed_generation()
        cache = StockCache(
            self.cache_root, FakeHttpClient(displace_lock=True)
        )

        result = cache.refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_code, "cache_locked")
        self.assertEqual(self.fixture.current_generation_id(), "generation-a")

    def test_delayed_activation_before_commit_cannot_overwrite_successful_writer(
        self,
    ) -> None:
        self.fixture.seed_generation()
        delayed_before_commit = threading.Event()
        allow_delayed = threading.Event()
        delayed_results: list[object] = []
        delayed_errors: list[BaseException] = []
        real_commit_acquire = cache_module.RuntimeCommitLock.acquire
        paused = False

        def pause_delayed_activation(root: Path) -> object:
            nonlocal paused
            if (
                threading.current_thread().name == "delayed-activation"
                and not paused
            ):
                paused = True
                delayed_before_commit.set()
                if not allow_delayed.wait(timeout=5):
                    raise AssertionError("delayed activation timeout")
            return real_commit_acquire(root)

        def run_delayed() -> None:
            try:
                delayed_results.append(
                    StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)
                )
            except BaseException as error:
                delayed_errors.append(error)

        current_client = FakeHttpClient(
            response=HttpResponse(
                status=200,
                headers={"ETag": '"generation-c"'},
                body=manifest_bytes("generation-c"),
            )
        )
        with patch.object(
            cache_module.RuntimeCommitLock,
            "acquire",
            side_effect=pause_delayed_activation,
        ):
            delayed = threading.Thread(
                target=run_delayed, name="delayed-activation"
            )
            delayed.start()
            reached_commit = delayed_before_commit.wait(timeout=1)
            if not reached_commit:
                allow_delayed.set()
                delayed.join(timeout=5)
            self.assertTrue(reached_commit)

            self._expire_current_refresh_lock()
            current_result = StockCache(
                self.cache_root, current_client
            ).refresh(self.config)
            allow_delayed.set()
            delayed.join(timeout=5)

        self.assertFalse(delayed.is_alive())
        self.assertEqual(delayed_errors, [])
        self.assertEqual(len(delayed_results), 1)
        self.assertEqual(current_result.status, "updated")
        self.assertEqual(self.fixture.current_generation_id(), "generation-c")
        active = CacheState.load(self.cache_root)
        self.assertIsNotNone(active)
        self.assertTrue(active.files.manifest.parent.is_dir())

    def test_activation_holding_commit_orders_later_writer_after_it(self) -> None:
        self.fixture.seed_generation()
        first_at_pointer = threading.Event()
        allow_first_pointer = threading.Event()
        second_waiting = threading.Event()
        second_done = threading.Event()
        first_errors: list[BaseException] = []
        second_errors: list[BaseException] = []
        second_results: list[object] = []
        real_write_json = cache_module._write_json_atomic
        real_commit_acquire = cache_module.RuntimeCommitLock.acquire

        def pause_first_pointer(path: Path, value: object) -> None:
            if (
                path == self.cache_root / "current.json"
                and threading.current_thread().name == "first-activation"
            ):
                first_at_pointer.set()
                if not allow_first_pointer.wait(timeout=5):
                    raise AssertionError("first pointer timeout")
            real_write_json(path, value)

        def observe_second_wait(root: Path) -> object:
            if threading.current_thread().name == "second-activation":
                second_waiting.set()
            return real_commit_acquire(root)

        def run_first() -> None:
            try:
                StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)
            except BaseException as error:
                first_errors.append(error)

        current_client = FakeHttpClient(
            response=HttpResponse(
                status=200,
                headers={"ETag": '"generation-c"'},
                body=manifest_bytes("generation-c"),
            )
        )

        def run_second() -> None:
            try:
                second_results.append(
                    StockCache(self.cache_root, current_client).refresh(self.config)
                )
            except BaseException as error:
                second_errors.append(error)
            finally:
                second_done.set()

        with patch.object(
            cache_module, "_write_json_atomic", side_effect=pause_first_pointer
        ):
            with patch.object(
                cache_module.RuntimeCommitLock,
                "acquire",
                side_effect=observe_second_wait,
            ):
                first = threading.Thread(target=run_first, name="first-activation")
                first.start()
                self.assertTrue(first_at_pointer.wait(timeout=5))
                self._expire_current_refresh_lock()
                second = threading.Thread(
                    target=run_second, name="second-activation"
                )
                second.start()
                self.assertTrue(second_waiting.wait(timeout=5))
                second_completed_while_first_paused = second_done.wait(timeout=1)
                allow_first_pointer.set()
                first.join(timeout=5)
                second.join(timeout=5)

        self.assertFalse(second_completed_while_first_paused)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual(second_errors, [])
        self.assertEqual(len(second_results), 1)
        self.assertEqual(second_results[0].status, "updated")
        self.assertEqual(self.fixture.current_generation_id(), "generation-c")

    def test_activation_rollback_cas_cannot_restore_over_successful_writer(
        self,
    ) -> None:
        self.fixture.seed_generation()
        previous_pointer = (self.cache_root / "current.json").read_bytes()
        rollback_paused = threading.Event()
        allow_rollback = threading.Event()
        second_waiting = threading.Event()
        second_done = threading.Event()
        delayed_errors: list[BaseException] = []
        second_errors: list[BaseException] = []
        second_results: list[object] = []
        real_load = CacheState.load
        real_write_bytes = cache_module._write_bytes_atomic
        real_commit_acquire = cache_module.RuntimeCommitLock.acquire

        def fail_delayed_validation(
            cache_dir: Path, progress: object | None = None
        ) -> CacheState | None:
            pointer = CurrentPointer.load(cache_dir / "current.json")
            if (
                threading.current_thread().name == "rollback-activation"
                and pointer.generation_id == "generation-b"
            ):
                raise StockError(
                    "cache_unavailable", "Синтетическая ошибка validation", 7
                )
            return real_load(cache_dir, progress if callable(progress) else None)

        def pause_rollback(path: Path, payload: bytes) -> None:
            if (
                path == self.cache_root / "current.json"
                and payload == previous_pointer
                and threading.current_thread().name == "rollback-activation"
            ):
                rollback_paused.set()
                if not allow_rollback.wait(timeout=5):
                    raise AssertionError("rollback timeout")
            real_write_bytes(path, payload)

        def observe_second_wait(root: Path) -> object:
            if threading.current_thread().name == "rollback-successor":
                second_waiting.set()
            return real_commit_acquire(root)

        def run_delayed() -> None:
            try:
                StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)
            except BaseException as error:
                delayed_errors.append(error)

        current_client = FakeHttpClient(
            response=HttpResponse(
                status=200,
                headers={"ETag": '"generation-c"'},
                body=manifest_bytes("generation-c"),
            )
        )

        def run_second() -> None:
            try:
                second_results.append(
                    StockCache(self.cache_root, current_client).refresh(self.config)
                )
            except BaseException as error:
                second_errors.append(error)
            finally:
                second_done.set()

        with patch.object(CacheState, "load", side_effect=fail_delayed_validation):
            with patch.object(
                cache_module, "_write_bytes_atomic", side_effect=pause_rollback
            ):
                with patch.object(
                    cache_module.RuntimeCommitLock,
                    "acquire",
                    side_effect=observe_second_wait,
                ):
                    delayed = threading.Thread(
                        target=run_delayed, name="rollback-activation"
                    )
                    delayed.start()
                    self.assertTrue(rollback_paused.wait(timeout=5))
                    self._expire_current_refresh_lock()
                    second = threading.Thread(
                        target=run_second, name="rollback-successor"
                    )
                    second.start()
                    self.assertTrue(second_waiting.wait(timeout=5))
                    second_completed_while_rollback_paused = second_done.wait(
                        timeout=1
                    )
                    allow_rollback.set()
                    delayed.join(timeout=5)
                    second.join(timeout=5)

        self.assertFalse(second_completed_while_rollback_paused)
        self.assertFalse(delayed.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(delayed_errors, [])
        self.assertEqual(second_errors, [])
        self.assertEqual(len(second_results), 1)
        self.assertEqual(second_results[0].status, "updated")
        self.assertEqual(self.fixture.current_generation_id(), "generation-c")
        active = CacheState.load(self.cache_root)
        self.assertIsNotNone(active)
        self.assertTrue(active.files.manifest.parent.is_dir())

    def test_interrupted_pointer_replace_rolls_back_to_previous_generation(self) -> None:
        self.fixture.seed_generation()
        real_replace = os.replace

        def interrupt_current_replace(source: Path, destination: Path) -> None:
            if Path(destination) == self.cache_root / "current.json":
                raise OSError("synthetic pointer interruption")
            real_replace(source, destination)

        with patch("papa_shin_stock.cache.os.replace", interrupt_current_replace):
            result = StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_code, "cache_unavailable")
        self.assertEqual(self.fixture.current_generation_id(), "generation-a")
        self.assertEqual(
            sorted(path.name for path in (self.cache_root / "generations").iterdir()),
            ["generation-existing"],
        )

    def test_post_replace_validation_failure_restores_previous_pointer(self) -> None:
        self.fixture.seed_generation()
        real_load = CacheState.load

        def reject_new_current(
            cache_dir: Path,
            progress: object | None = None,
        ) -> CacheState | None:
            pointer = json.loads(
                (cache_dir / "current.json").read_text(encoding="utf-8")
            )
            if pointer["generation_id"] == "generation-b":
                raise StockError(
                    "cache_unavailable", "Синтетическая ошибка validation", 7
                )
            return real_load(cache_dir, progress if callable(progress) else None)

        with patch.object(CacheState, "load", side_effect=reject_new_current):
            result = StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_code, "cache_unavailable")
        self.assertEqual(self.fixture.current_generation_id(), "generation-a")
        self.assertEqual(
            sorted(path.name for path in (self.cache_root / "generations").iterdir()),
            ["generation-existing"],
        )

    def test_success_removes_inactive_generation_after_activation(self) -> None:
        self.fixture.seed_generation()

        StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        active = StockCache(self.cache_root, FakeHttpClient()).current_generation()
        self.assertEqual(active.generation_id, "generation-b")
        self.assertEqual(
            [path for path in (self.cache_root / "generations").iterdir() if path.is_dir()],
            [active.manifest.parent],
        )

    def test_inactive_cleanup_failure_is_observable_without_rollback(self) -> None:
        self.fixture.seed_generation()
        real_rmtree = shutil.rmtree

        def fail_for_previous_generation(path: Path, *args: object, **kwargs: object) -> None:
            if Path(path).name == "generation-existing":
                raise PermissionError("synthetic Windows cleanup failure")
            real_rmtree(path, *args, **kwargs)

        with patch(
            "papa_shin_stock.cache.shutil.rmtree",
            side_effect=fail_for_previous_generation,
        ):
            result = StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertEqual(result.warning_code, "cache_cleanup_incomplete")
        self.assertEqual(
            result.to_public_dict()["warnings"][0]["code"],
            "cache_cleanup_incomplete",
        )
        self.assertEqual(self.fixture.current_generation_id(), "generation-b")
        self.assertTrue(
            (self.cache_root / "generations" / "generation-existing").is_dir()
        )

    def test_repeated_cleanup_failure_does_not_accumulate_generations(self) -> None:
        self.fixture.seed_generation()
        client = FakeHttpClient()
        real_rmtree = shutil.rmtree
        generations = self.cache_root / "generations"

        def fail_for_generation(path: Path, *args: object, **kwargs: object) -> None:
            if Path(path).parent == generations:
                raise PermissionError("synthetic persistent cleanup failure")
            real_rmtree(path, *args, **kwargs)

        with patch(
            "papa_shin_stock.cache.shutil.rmtree",
            side_effect=fail_for_generation,
        ):
            first = StockCache(self.cache_root, client).refresh(self.config)
            pointer_after_first = (self.cache_root / "current.json").read_bytes()
            directories_after_first = sorted(
                path.name for path in generations.iterdir() if path.is_dir()
            )
            downloads_after_first = len(client.download_calls)

            second = StockCache(self.cache_root, client).refresh(self.config)

        self.assertEqual(first.status, "updated")
        self.assertEqual(first.warning_code, "cache_cleanup_incomplete")
        self.assertEqual(second.status, "stale_cache")
        self.assertEqual(second.warning_code, "cache_cleanup_incomplete")
        self.assertEqual(
            (self.cache_root / "current.json").read_bytes(), pointer_after_first
        )
        self.assertEqual(len(client.download_calls), downloads_after_first)
        self.assertEqual(
            sorted(path.name for path in generations.iterdir() if path.is_dir()),
            directories_after_first,
        )
        self.assertEqual(len(directories_after_first), 2)

    def test_repeated_staging_cleanup_failure_without_cache_is_bounded(self) -> None:
        client = FakeHttpClient(interrupt_offers=True)
        real_rmtree = shutil.rmtree
        generations = self.cache_root / "generations"

        def fail_for_staging(path: Path, *args: object, **kwargs: object) -> None:
            candidate = Path(path)
            if candidate.parent == generations and candidate.name.startswith(
                ".staging-"
            ):
                raise PermissionError("synthetic persistent staging cleanup failure")
            real_rmtree(path, *args, **kwargs)

        with patch(
            "papa_shin_stock.cache.shutil.rmtree",
            side_effect=fail_for_staging,
        ):
            for _ in range(3):
                with self.assertRaises(StockError) as raised:
                    StockCache(self.cache_root, client).refresh(self.config)
                self.assertNotEqual(raised.exception.exit_code, 0)

        staging = sorted(
            path.name
            for path in generations.iterdir()
            if path.is_dir() and path.name.startswith(".staging-")
        )
        self.assertFalse((self.cache_root / "current.json").exists())
        self.assertEqual(len(staging), 1)
        self.assertEqual(len(client.download_calls), 2)

    def test_displaced_writer_cannot_cleanup_new_current_generation(self) -> None:
        self.fixture.seed_generation()
        cache = StockCache(self.cache_root, FakeHttpClient())
        original_cleanup = cache._cleanup_inactive_generations

        def displace_before_cleanup(*args: object) -> str | None:
            current = json.loads(
                (self.cache_root / "current.json").read_text(encoding="utf-8")
            )
            current_directory = self.cache_root / "generations" / current["directory_name"]
            replacement_name = "generation-new-owner"
            replacement = self.cache_root / "generations" / replacement_name
            shutil.copytree(current_directory, replacement)
            (replacement / "manifest.json").write_bytes(manifest_bytes("generation-c"))
            state_path = replacement / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["generation_id"] = "generation-c"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            (self.cache_root / "current.json").write_text(
                json.dumps(
                    {
                        "generation_id": "generation-c",
                        "directory_name": replacement_name,
                        "activation_token": "replacement-writer",
                    }
                ),
                encoding="utf-8",
            )
            (self.cache_root / ".refresh.lock" / "owner.json").write_text(
                json.dumps(
                    {"token": "replacement-writer", "created_at": time.time()}
                ),
                encoding="utf-8",
            )
            return original_cleanup(*args)

        with patch.object(
            cache,
            "_cleanup_inactive_generations",
            side_effect=displace_before_cleanup,
        ):
            result = cache.refresh(self.config)

        current = CacheState.load(self.cache_root)
        self.assertIsNotNone(current)
        self.assertEqual(current.generation_id, "generation-c")
        self.assertEqual(result.status, "stale_cache")
        self.assertEqual(result.warning_code, "cache_locked")

    def test_cache_state_rejects_pointer_to_incomplete_generation(self) -> None:
        self.fixture.seed_generation()
        StockCache(self.cache_root, FakeHttpClient()).current_generation().offers.unlink()

        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            CacheState.load(self.cache_root)

    def test_cache_state_rejects_malformed_stored_manifest(self) -> None:
        self.fixture.seed_generation()
        generation = self.cache_root / "generations" / "generation-existing"
        (generation / "manifest.json").write_text("not-json", encoding="utf-8")

        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            CacheState.load(self.cache_root)

    def test_cache_state_rejects_stored_manifest_generation_mismatch(self) -> None:
        self.fixture.seed_generation()
        generation = self.cache_root / "generations" / "generation-existing"
        (generation / "manifest.json").write_bytes(manifest_bytes("generation-c"))

        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            CacheState.load(self.cache_root)

    def test_cache_state_rejects_readable_file_with_wrong_checksum(self) -> None:
        self.fixture.seed_generation()
        generation = self.cache_root / "generations" / "generation-existing"
        (generation / "products.jsonl").write_bytes(b"readable-but-corrupt\n")

        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            CacheState.load(self.cache_root)

    def test_corrupt_readable_previous_cache_cannot_be_stale_fallback(self) -> None:
        self.fixture.seed_generation()
        generation = self.cache_root / "generations" / "generation-existing"
        (generation / "products.jsonl").write_bytes(b"readable-but-corrupt\n")
        client = FakeHttpClient()

        with patch.object(
            client,
            "get_manifest",
            side_effect=StockError("network_error", "Синтетическая ошибка сети", 3),
        ):
            with self.assertRaisesRegex(StockError, "network_error") as raised:
                StockCache(self.cache_root, client).refresh(self.config)

        self.assertNotEqual(raised.exception.exit_code, 0)

    def test_refresh_recovers_from_invalid_pointer_when_new_download_succeeds(self) -> None:
        (self.cache_root / "current.json").write_text("not-json", encoding="utf-8")

        result = StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(result.status, "updated")
        self.assertEqual(self.fixture.current_generation_id(), "generation-b")

    def test_manifest_path_traversal_is_rejected_before_download(self) -> None:
        body = json.loads(manifest_bytes())
        body["files"]["products"]["url"] = "../../outside.jsonl"
        client = FakeHttpClient(
            response=HttpResponse(
                status=200,
                headers={},
                body=json.dumps(body).encode("utf-8"),
            )
        )

        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            StockCache(self.cache_root, client).refresh(self.config)

        self.assertEqual(client.manifest_calls, [(None, None)])

    def test_directory_fsync_propagates_storage_failures(self) -> None:
        real_open = os.open

        for error_number in (errno.EIO, errno.ENOSPC):
            with self.subTest(errno=error_number):
                with patch(
                    "papa_shin_stock.cache.os.open",
                    side_effect=lambda path, flags: real_open(path, flags),
                ):
                    with patch(
                        "papa_shin_stock.cache.os.fsync",
                        side_effect=OSError(error_number, "synthetic fsync failure"),
                    ):
                        with self.assertRaises(OSError) as raised:
                            _fsync_directory(self.cache_root)

                self.assertEqual(raised.exception.errno, error_number)


if __name__ == "__main__":
    unittest.main()
