from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
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
        attestation = cache_module._attest_cache_root(self.root, create=True)
        try:
            root_ownership_token = attestation.ownership_token
        finally:
            attestation.close()
        generation = self.root / "generations" / directory_name
        generation.mkdir(parents=True)
        if cache_module._is_native_windows():
            (generation / cache_module._WINDOWS_GENERATION_OWNER_NAME).write_bytes(
                cache_module._json_payload(
                    {
                        "kind": cache_module._WINDOWS_GENERATION_OWNER_KIND,
                        "schema_version": cache_module._WINDOWS_OWNER_SCHEMA_VERSION,
                        "root_ownership_token": root_ownership_token,
                        "generation_id": generation_id,
                        "ownership_token": uuid.uuid4().hex,
                    }
                )
            )
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
        cache_module._attest_cache_root(self.cache_root, create=True).close()
        self.fixture = CacheFixture(self.cache_root)
        self.config = StockConfig(
            manifest_url="https://stock.example.test/manifest.json",
            username="synthetic-user",
            password="synthetic-password",
            product_id_field="product_id",
            offer_product_id_field="product_id",
            cache_dir=self.cache_root,
        )

    def test_existing_empty_cache_root_is_initialized_with_strict_marker(self) -> None:
        root = Path(self.temp_dir.name) / "empty-cache"
        root.mkdir(mode=0o755)

        attestation = cache_module._attest_cache_root(root, create=True)
        self.addCleanup(attestation.close)

        marker = root / ".papa-shin-stock-cache-root.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(
            set(payload), {"kind", "schema_version", "ownership_token"}
        )
        self.assertEqual(payload["kind"], "papa-shin-stock-cache-root")
        self.assertEqual(payload["schema_version"], 1)
        self.assertRegex(payload["ownership_token"], r"^[0-9a-f]{32}$")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)

    def test_unmarked_nonempty_cache_root_is_rejected_without_mutation(self) -> None:
        root = Path(self.temp_dir.name) / "victim"
        root.mkdir(mode=0o755)
        victim = root / "unrelated.txt"
        victim.write_bytes(b"do-not-touch")
        before_mode = stat.S_IMODE(root.stat().st_mode)

        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            cache_module._attest_cache_root(root, create=True)

        self.assertEqual(victim.read_bytes(), b"do-not-touch")
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), before_mode)
        self.assertFalse((root / ".papa-shin-stock-cache-root.json").exists())

    def test_invalid_cache_root_markers_fail_closed(self) -> None:
        invalid_payloads = {
            "malformed": b"not-json",
            "oversized": b"{" + b" " * 513 + b"}",
            "duplicate": b'{"kind":"papa-shin-stock-cache-root","kind":"x","schema_version":1,"ownership_token":"0123456789abcdef0123456789abcdef"}',
            "extra": b'{"kind":"papa-shin-stock-cache-root","schema_version":1,"ownership_token":"0123456789abcdef0123456789abcdef","extra":true}',
            "trailing": b'{"kind":"papa-shin-stock-cache-root","schema_version":1,"ownership_token":"0123456789abcdef0123456789abcdef"}\n',
            "bool-version": b'{"kind":"papa-shin-stock-cache-root","schema_version":true,"ownership_token":"0123456789abcdef0123456789abcdef"}',
        }
        for label, payload in invalid_payloads.items():
            with self.subTest(label=label):
                root = Path(self.temp_dir.name) / f"invalid-{label}"
                root.mkdir()
                marker = root / ".papa-shin-stock-cache-root.json"
                marker.write_bytes(payload)
                os.chmod(marker, 0o600)
                with self.assertRaisesRegex(StockError, "cache_unavailable"):
                    cache_module._attest_cache_root(root, create=False)

    def test_symlink_cache_root_marker_is_rejected(self) -> None:
        root = Path(self.temp_dir.name) / "symlink-marker"
        root.mkdir()
        target = Path(self.temp_dir.name) / "marker-target"
        target.write_text(
            '{"kind":"papa-shin-stock-cache-root","schema_version":1,"ownership_token":"0123456789abcdef0123456789abcdef"}',
            encoding="utf-8",
        )
        try:
            (root / ".papa-shin-stock-cache-root.json").symlink_to(target)
        except (OSError, NotImplementedError) as error:
            self.skipTest(type(error).__name__)
        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            cache_module._attest_cache_root(root, create=False)

    def test_directory_cache_root_marker_is_rejected_without_hardening(self) -> None:
        root = Path(self.temp_dir.name) / "directory-marker"
        root.mkdir(mode=0o755)
        marker = root / ".papa-shin-stock-cache-root.json"
        marker.mkdir()
        before_mode = stat.S_IMODE(root.stat().st_mode)
        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            cache_module._attest_cache_root(root, create=False)
        self.assertTrue(marker.is_dir())
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), before_mode)

    @unittest.skipUnless(os.name == "posix", "Hard-link identity проверяется на POSIX")
    def test_hardlinked_cache_root_marker_is_rejected(self) -> None:
        root = Path(self.temp_dir.name) / "hardlink-marker"
        root.mkdir()
        source = Path(self.temp_dir.name) / "marker-source"
        source.write_bytes(cache_module._cache_root_marker_payload("a" * 32))
        os.chmod(source, 0o600)
        os.link(source, root / ".papa-shin-stock-cache-root.json")
        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            cache_module._attest_cache_root(root, create=False)

    def test_preexisting_initializer_temp_is_foreign_and_not_removed(self) -> None:
        root = Path(self.temp_dir.name) / "foreign-init"
        root.mkdir(mode=0o755)
        candidate = root / (".papa-shin-stock-cache-root.init-" + "a" * 32 + ".tmp")
        candidate.write_text("foreign", encoding="utf-8")
        before_mode = stat.S_IMODE(root.stat().st_mode)
        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            cache_module._attest_cache_root(root, create=True)
        self.assertEqual(candidate.read_text(encoding="utf-8"), "foreign")
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), before_mode)
        self.assertFalse((root / ".papa-shin-stock-cache-root.json").exists())

    def test_unrelated_insertion_before_marker_publication_blocks_hardening(self) -> None:
        root = Path(self.temp_dir.name) / "late-insertion"
        root.mkdir(mode=0o755)
        before_mode = stat.S_IMODE(root.stat().st_mode)
        real_publish = cache_module._publish_cache_root_marker

        def inject_then_publish(path: Path, descriptor: int | None) -> None:
            (root / "unrelated.txt").write_text("keep", encoding="utf-8")
            real_publish(path, descriptor)

        with patch.object(
            cache_module,
            "_publish_cache_root_marker",
            side_effect=inject_then_publish,
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                cache_module._attest_cache_root(root, create=True)
        self.assertEqual((root / "unrelated.txt").read_text(), "keep")
        self.assertEqual(before_mode, 0o755)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)

    @unittest.skipUnless(os.name == "posix", "dirfd identity проверяется на POSIX")
    def test_replaced_published_marker_with_same_token_fails_closed(self) -> None:
        root = Path(self.temp_dir.name) / "replaced-published-marker"
        root.mkdir(mode=0o755)
        marker = root / ".papa-shin-stock-cache-root.json"
        displaced = Path(self.temp_dir.name) / "owned-marker"
        real_publish = cache_module._publish_cache_root_marker
        foreign_identity: os.stat_result | None = None

        def replace_after_publish(path: Path, descriptor: int | None) -> object:
            nonlocal foreign_identity
            evidence = real_publish(path, descriptor)
            os.replace(marker, displaced)
            marker.write_bytes(displaced.read_bytes())
            os.chmod(marker, 0o600)
            foreign_identity = marker.stat()
            self.assertFalse(
                cache_module._same_file_identity(displaced.stat(), foreign_identity)
            )
            return evidence

        with patch.object(
            cache_module,
            "_publish_cache_root_marker",
            side_effect=replace_after_publish,
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                cache_module._attest_cache_root(root, create=True)

        self.assertIsNotNone(foreign_identity)
        self.assertTrue(
            cache_module._same_file_identity(marker.stat(), foreign_identity)
        )
        self.assertEqual(marker.read_bytes(), displaced.read_bytes())

    @unittest.skipUnless(os.name == "posix", "dirfd identity проверяется на POSIX")
    def test_same_token_marker_replacement_after_last_read_fails_closed(self) -> None:
        root = Path(self.temp_dir.name) / "same-token-after-last-read"
        root.mkdir()
        initial = cache_module._attest_cache_root(root, create=True)
        initial.close()
        marker = root / ".papa-shin-stock-cache-root.json"
        parked = root / ".original-cache-root-marker"
        original_payload = marker.read_bytes()
        real_read = cache_module._read_cache_root_marker
        replaced_identity: os.stat_result | None = None
        reads = 0

        def replace_after_read(path: Path, descriptor: int | None) -> object:
            nonlocal reads, replaced_identity
            evidence = real_read(path, descriptor)
            reads += 1
            if reads == 1:
                os.replace(marker, parked)
                marker.write_bytes(original_payload)
                os.chmod(marker, 0o600)
                replaced_identity = marker.stat()
            return evidence

        with patch.object(
            cache_module,
            "_read_cache_root_marker",
            side_effect=replace_after_read,
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                cache_module._attest_cache_root(root, create=False)

        self.assertEqual(marker.read_bytes(), original_payload)
        self.assertIsNotNone(replaced_identity)
        self.assertTrue(
            cache_module._same_file_identity(marker.stat(), replaced_identity)
        )

    @unittest.skipUnless(os.name == "posix", "dirfd identity проверяется на POSIX")
    def test_same_token_marker_replacement_after_attestation_fails_closed(self) -> None:
        root = Path(self.temp_dir.name) / "same-token-after-attestation"
        root.mkdir()
        attestation = cache_module._attest_cache_root(root, create=True)
        self.addCleanup(attestation.close)
        marker = root / ".papa-shin-stock-cache-root.json"
        parked = root / ".original-cache-root-marker"
        original_payload = marker.read_bytes()

        os.replace(marker, parked)
        marker.write_bytes(original_payload)
        os.chmod(marker, 0o600)
        replacement_identity = marker.stat()

        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            attestation.assert_current()

        self.assertTrue(
            cache_module._same_file_identity(marker.stat(), replacement_identity)
        )
        self.assertEqual(marker.read_bytes(), original_payload)

    def test_marker_inserted_during_initialization_is_preserved_fail_closed(
        self,
    ) -> None:
        root = Path(self.temp_dir.name) / "foreign-marker-race"
        root.mkdir(mode=0o755)
        marker = root / ".papa-shin-stock-cache-root.json"
        foreign_payload = cache_module._cache_root_marker_payload("c" * 32)

        real_publish = cache_module._publish_cache_root_marker

        def insert_conflicting_marker(path: Path, descriptor: int | None) -> str:
            marker.write_bytes(foreign_payload)
            os.chmod(marker, 0o600)
            return real_publish(path, descriptor)

        with patch.object(
            cache_module,
            "_publish_cache_root_marker",
            side_effect=insert_conflicting_marker,
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                cache_module._attest_cache_root(root, create=True)

        self.assertEqual(marker.read_bytes(), foreign_payload)
        self.assertEqual(
            list(root.glob(".papa-shin-stock-cache-root.conflict-*")), []
        )

    def test_interrupted_marker_write_is_retained_and_blocks_reinitialization(
        self,
    ) -> None:
        root = Path(self.temp_dir.name) / "interrupted-marker-write"
        root.mkdir(mode=0o755)
        marker = root / ".papa-shin-stock-cache-root.json"
        real_write = os.write
        interrupted = False

        def interrupt_after_partial_write(descriptor: int, payload: bytes) -> int:
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                real_write(descriptor, payload[:7])
                raise OSError(errno.EIO, "synthetic marker write interruption")
            return real_write(descriptor, payload)

        with patch.object(
            cache_module.os,
            "write",
            side_effect=interrupt_after_partial_write,
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                cache_module._attest_cache_root(root, create=True)

        self.assertTrue(interrupted)
        partial_payload = marker.read_bytes()
        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            cache_module._read_cache_root_marker(root, None)
        before_mode = stat.S_IMODE(root.stat().st_mode)
        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            cache_module._attest_cache_root(root, create=True)
        self.assertEqual(marker.read_bytes(), partial_payload)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), before_mode)
        self.assertEqual(list(root.iterdir()), [marker])

    def test_interrupted_marker_directory_fsync_leaves_invalid_marker(self) -> None:
        root = Path(self.temp_dir.name) / "interrupted-marker-directory-fsync"
        root.mkdir(mode=0o755)
        marker = root / ".papa-shin-stock-cache-root.json"

        with patch.object(
            cache_module,
            "_fsync_directory_descriptor",
            side_effect=OSError(errno.EIO, "synthetic directory fsync interruption"),
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                cache_module._attest_cache_root(root, create=True)

        retained = marker.read_bytes()
        self.assertEqual(retained, b"")
        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            cache_module._attest_cache_root(root, create=True)
        self.assertEqual(marker.read_bytes(), retained)

    def test_marker_publisher_retries_partial_writes(self) -> None:
        root = Path(self.temp_dir.name) / "partial-marker-writes"
        root.mkdir(mode=0o755)
        real_write = os.write
        writes = 0

        def write_small_chunks(descriptor: int, payload: bytes) -> int:
            nonlocal writes
            writes += 1
            return real_write(descriptor, payload[:5])

        with patch.object(cache_module.os, "write", side_effect=write_small_chunks):
            attestation = cache_module._attest_cache_root(root, create=True)
            attestation.close()

        self.assertGreater(writes, 1)
        self.assertRegex(
            cache_module._read_cache_root_marker(root, None).ownership_token,
            r"^[0-9a-f]{32}$",
        )

    def test_marker_replacement_during_validation_is_not_deleted(self) -> None:
        root = Path(self.temp_dir.name) / "marker-validation-replacement"
        root.mkdir(mode=0o755)
        marker = root / ".papa-shin-stock-cache-root.json"
        parked = root / ".owned-cache-root-marker"
        foreign_payload = cache_module._cache_root_marker_payload("e" * 32)
        real_assert = cache_module._assert_published_cache_root_marker
        replaced = False

        def replace_then_assert(
            descriptor: int,
            evidence: cache_module._PublishedCacheRootMarker,
        ) -> str:
            nonlocal replaced
            if not replaced and marker.exists():
                replaced = True
                os.rename(marker, parked)
                marker.write_bytes(foreign_payload)
                os.chmod(marker, 0o600)
            return real_assert(descriptor, evidence)

        with patch.object(
            cache_module,
            "_assert_published_cache_root_marker",
            side_effect=replace_then_assert,
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                cache_module._attest_cache_root(root, create=True)

        self.assertTrue(replaced)
        self.assertEqual(marker.read_bytes(), foreign_payload)
        self.assertTrue(parked.is_file())
        self.assertEqual(parked.read_bytes(), b"")

    def test_insertion_after_root_hardening_blocks_initialization(self) -> None:
        root = Path(self.temp_dir.name) / "late-post-hardening-insertion"
        root.mkdir(mode=0o755)
        victim = root / "generations" / "generation-victim"
        marker = victim / "must-survive.txt"
        real_fchmod = os.fchmod
        injected = False

        def inject_after_root_hardening(descriptor: int, mode: int) -> None:
            nonlocal injected
            real_fchmod(descriptor, mode)
            observed = os.fstat(descriptor)
            if stat.S_ISDIR(observed.st_mode) and not injected:
                injected = True
                victim.mkdir(parents=True)
                marker.write_text("safe", encoding="utf-8")

        with patch.object(
            cache_module.os,
            "fchmod",
            side_effect=inject_after_root_hardening,
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                cache_module._attest_cache_root(root, create=True)

        self.assertTrue(injected)
        self.assertEqual(marker.read_text(encoding="utf-8"), "safe")

    @unittest.skipUnless(os.name == "posix", "FD accounting проверяется на POSIX")
    def test_failed_final_attestation_does_not_leak_root_descriptor(self) -> None:
        root = Path(self.temp_dir.name) / "fd-stable"
        root.mkdir()
        before = len(list(Path("/dev/fd").iterdir()))
        with patch.object(
            cache_module.CacheRootAttestation,
            "assert_current",
            side_effect=StockError("cache_unavailable", "Проверенный кэш недоступен", 5),
        ):
            for _ in range(20):
                with self.assertRaisesRegex(StockError, "cache_unavailable"):
                    cache_module._attest_cache_root(root, create=True)
        after = len(list(Path("/dev/fd").iterdir()))
        self.assertLessEqual(after, before + 1)

    @unittest.skipUnless(os.name == "posix", "FD accounting проверяется на POSIX")
    def test_failed_generation_load_does_not_leak_child_descriptors(self) -> None:
        self.fixture.seed_generation()
        state_path = (
            self.cache_root
            / "generations"
            / "generation-existing"
            / "state.json"
        )
        state_path.write_text("not-json", encoding="utf-8")
        before = len(list(Path("/dev/fd").iterdir()))

        for _ in range(20):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                CacheState.load(self.cache_root)

        after = len(list(Path("/dev/fd").iterdir()))
        self.assertLessEqual(after, before + 1)

    def test_concurrent_empty_root_initializers_converge_on_one_marker(self) -> None:
        root = Path(self.temp_dir.name) / "concurrent-marker"
        root.mkdir()
        barrier = threading.Barrier(2)
        tokens: list[str] = []
        errors: list[BaseException] = []

        def initialize() -> None:
            try:
                barrier.wait()
                attestation = cache_module._attest_cache_root(root, create=True)
                tokens.append(attestation.ownership_token)
                attestation.close()
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=initialize) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(tokens)), 1)

    def test_root_marker_removal_before_cleanup_fails_closed(self) -> None:
        cache_module._attest_cache_root(self.cache_root, create=True).close()
        self.fixture.seed_generation()
        inactive = self.cache_root / "generations" / "generation-inactive"
        inactive.mkdir()
        (inactive / "victim.txt").write_text("keep", encoding="utf-8")
        with CacheLock.acquire(self.cache_root) as lock:
            (self.cache_root / ".papa-shin-stock-cache-root.json").unlink()
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                StockCache(
                    self.cache_root, FakeHttpClient()
                )._cleanup_inactive_generations(lock)
        self.assertTrue((inactive / "victim.txt").exists())

    def test_root_marker_swap_before_cleanup_fails_closed(self) -> None:
        self.fixture.seed_generation()
        inactive = self.cache_root / "generations" / "generation-inactive"
        inactive.mkdir()
        victim = inactive / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        marker = self.cache_root / ".papa-shin-stock-cache-root.json"
        with CacheLock.acquire(self.cache_root) as lock:
            replacement = self.cache_root / ".replacement-cache-root-marker"
            replacement.write_bytes(
                cache_module._cache_root_marker_payload("f" * 32)
            )
            os.chmod(replacement, 0o600)
            os.replace(replacement, marker)
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                StockCache(
                    self.cache_root, FakeHttpClient()
                )._cleanup_inactive_generations(lock)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

    def _expire_current_refresh_lock(self) -> None:
        self._expire_refresh_lock_at(self.cache_root)

    def _expire_refresh_lock_at(self, root: Path) -> None:
        lock_path = root / ".refresh.lock"
        owner_path = lock_path / "owner.json"
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        expired = time.time() - cache_module._LOCK_TTL_SECONDS - 1
        owner_path.write_text(
            json.dumps({"token": owner["token"], "created_at": expired}),
            encoding="utf-8",
        )
        os.utime(lock_path / f"heartbeat-{owner['token']}", (expired, expired))

    def _assert_private_cache_modes(self, root: Path) -> None:
        pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
        generation = root / "generations" / pointer["directory_name"]
        directories = [root, root / "generations", generation]
        files = [
            root / ".papa-shin-stock-cache-root.json",
            root / ".runtime-status.commit.lock",
            root / "current.json",
            root / f".runtime-status-{generation.name}.json",
            generation / "manifest.json",
            generation / "products.jsonl",
            generation / "offers.jsonl",
            generation / "state.json",
        ]
        self.assertTrue(all(path.exists() for path in directories + files))
        self.assertEqual(
            {path: stat.S_IMODE(path.stat().st_mode) for path in directories},
            {path: 0o700 for path in directories},
        )
        self.assertEqual(
            {path: stat.S_IMODE(path.stat().st_mode) for path in files},
            {path: 0o600 for path in files},
        )

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

    @unittest.skipUnless(os.name == "posix", "Точные POSIX modes доступны только на POSIX")
    def test_refresh_creates_private_cache_tree_under_umask_022(self) -> None:
        previous_umask = os.umask(0o022)
        try:
            result = StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)
        finally:
            os.umask(previous_umask)

        self.assertEqual(result.status, "updated")
        generation = StockCache(
            self.cache_root, FakeHttpClient()
        ).current_generation().manifest.parent
        directories = [
            self.cache_root,
            self.cache_root / "generations",
            generation,
        ]
        files = [
            self.cache_root / ".runtime-status.commit.lock",
            self.cache_root / "current.json",
            self.cache_root / f".runtime-status-{generation.name}.json",
            generation / "manifest.json",
            generation / "products.jsonl",
            generation / "offers.jsonl",
            generation / "state.json",
        ]

        self.assertTrue(all(path.exists() for path in directories + files))
        self.assertEqual(
            {path: stat.S_IMODE(path.stat().st_mode) for path in directories},
            {path: 0o700 for path in directories},
        )
        self.assertEqual(
            {path: stat.S_IMODE(path.stat().st_mode) for path in files},
            {path: 0o600 for path in files},
        )

    @unittest.skipUnless(os.name == "posix", "Точные POSIX modes доступны только на POSIX")
    def test_refresh_creates_private_cache_tree_under_umask_000(self) -> None:
        (self.cache_root / ".papa-shin-stock-cache-root.json").unlink()
        self.cache_root.rmdir()
        previous_umask = os.umask(0o000)
        try:
            result = StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)
        finally:
            os.umask(previous_umask)

        self.assertEqual(result.status, "updated")
        self._assert_private_cache_modes(self.cache_root)

    @unittest.skipUnless(os.name == "posix", "Точные POSIX modes доступны только на POSIX")
    def test_refresh_creates_private_cache_tree_under_umask_0777(self) -> None:
        (self.cache_root / ".papa-shin-stock-cache-root.json").unlink()
        self.cache_root.rmdir()
        previous_umask = os.umask(0o777)
        try:
            result = StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)
        finally:
            os.umask(previous_umask)

        self.assertEqual(result.status, "updated")
        self._assert_private_cache_modes(self.cache_root)

    @unittest.skipUnless(os.name == "posix", "Точные POSIX modes доступны только на POSIX")
    def test_nested_private_cache_bootstrap_succeeds_under_umask_0777(self) -> None:
        private_parent = Path(self.temp_dir.name) / "missing-private-parent"
        nested_root = private_parent / "nested-cache"
        config = StockConfig(
            manifest_url=self.config.manifest_url,
            username=self.config.username,
            password=self.config.password,
            product_id_field=self.config.product_id_field,
            offer_product_id_field=self.config.offer_product_id_field,
            cache_dir=nested_root,
        )
        previous_umask = os.umask(0o777)
        try:
            result = StockCache(nested_root, FakeHttpClient()).refresh(config)
        finally:
            os.umask(previous_umask)

        self.assertEqual(result.status, "updated")
        self.assertEqual(stat.S_IMODE(private_parent.stat().st_mode), 0o700)
        self._assert_private_cache_modes(nested_root)

    @unittest.skipUnless(os.name == "posix", "Symlink race проверяется на POSIX")
    def test_parent_swap_does_not_chmod_external_directory(self) -> None:
        safe_parent = Path(self.temp_dir.name) / "safe-parent"
        safe_parent.mkdir()
        target = safe_parent / "cache"
        target.mkdir(mode=0o700)
        parked_parent = Path(self.temp_dir.name) / "parked-parent"
        outside_parent = Path(self.temp_dir.name) / "outside-parent"
        outside_target = outside_parent / "cache"
        outside_target.mkdir(parents=True, mode=0o755)
        os.chmod(outside_target, 0o755)
        real_lstat = Path.lstat
        swapped = False

        def swap_parent_after_target_lstat(path: Path) -> os.stat_result:
            nonlocal swapped
            observed = real_lstat(path)
            if path == target and not swapped:
                swapped = True
                os.rename(safe_parent, parked_parent)
                safe_parent.symlink_to(outside_parent, target_is_directory=True)
            return observed

        with patch.object(Path, "lstat", swap_parent_after_target_lstat):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                cache_module._ensure_private_directory(target)

        self.assertTrue(swapped)
        self.assertEqual(stat.S_IMODE(outside_target.stat().st_mode), 0o755)

    @unittest.skipUnless(os.name == "posix", "Directory identity race проверяется на POSIX")
    def test_bootstrap_binds_created_directory_before_chmod(self) -> None:
        parent = Path(self.temp_dir.name) / "bootstrap-parent"
        parent.mkdir(mode=0o700)
        target = parent / "private-cache"
        parked = parent / "parked-created-cache"
        outside = parent / "outside-directory"
        outside.mkdir(mode=0o755)
        marker = outside / "must-survive.txt"
        marker.write_text("safe", encoding="utf-8")
        os.chmod(outside, 0o755)
        outside_before = outside.stat()
        outside_identity = (outside_before.st_dev, outside_before.st_ino)
        outside_mode = stat.S_IMODE(outside_before.st_mode)
        real_stat = os.stat
        swapped = False

        def swap_created_entry_before_hardening(
            path: object,
            *args: object,
            **kwargs: object,
        ) -> os.stat_result:
            nonlocal swapped
            observed = real_stat(path, *args, **kwargs)
            if (
                not swapped
                and path == target.name
                and kwargs.get("dir_fd") is not None
                and kwargs.get("follow_symlinks") is False
            ):
                parent_descriptor = kwargs["dir_fd"]
                try:
                    os.rename(
                        target.name,
                        parked.name,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                    )
                    os.rename(
                        outside.name,
                        target.name,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                    )
                except OSError as error:
                    raise AssertionError("synthetic directory swap failed") from error
                swapped = True
            return observed

        previous_umask = os.umask(0o777)
        try:
            with patch.object(
                cache_module.os,
                "stat",
                side_effect=swap_created_entry_before_hardening,
            ):
                with self.assertRaisesRegex(StockError, "cache_unavailable"):
                    cache_module._ensure_private_directory(target, create=True)
        finally:
            os.umask(previous_umask)

        self.assertTrue(swapped)
        target_after = target.stat()
        self.assertEqual(
            (target_after.st_dev, target_after.st_ino),
            outside_identity,
        )
        self.assertEqual(stat.S_IMODE(target_after.st_mode), outside_mode)
        self.assertEqual((target / marker.name).read_text(encoding="utf-8"), "safe")

    @unittest.skipUnless(os.name == "posix", "POSIX parent modes проверяются на POSIX")
    def test_bootstrap_rejects_nonsticky_world_writable_parent(self) -> None:
        unsafe_parent = Path(self.temp_dir.name) / "unsafe-parent"
        unsafe_parent.mkdir(mode=0o777)
        os.chmod(unsafe_parent, 0o777)
        root = unsafe_parent / "private-cache"

        with self.assertRaisesRegex(StockError, "cache_locked"):
            CacheLock.acquire(root)

        self.assertFalse(root.exists())

    @unittest.skipUnless(os.name == "posix", "Точные POSIX modes доступны только на POSIX")
    def test_lock_creation_uses_private_modes_under_umask_022(self) -> None:
        previous_umask = os.umask(0o022)
        try:
            lock = CacheLock.acquire(self.cache_root)
        finally:
            os.umask(previous_umask)
        self.addCleanup(lock.release)

        self.assertEqual(stat.S_IMODE(self.cache_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(lock.path.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((lock.path / "owner.json").stat().st_mode), 0o600
        )
        self.assertEqual(
            stat.S_IMODE((lock.path / f"heartbeat-{lock.token}").stat().st_mode),
            0o600,
        )

    @unittest.skipUnless(os.name == "posix", "Lock fsync semantics проверяются на POSIX")
    def test_post_publish_fsync_failure_quarantines_lock_and_allows_reacquire(
        self,
    ) -> None:
        real_fsync_directory = cache_module._fsync_directory_descriptor
        failed = False

        def fail_once_after_publish(descriptor: int) -> None:
            nonlocal failed
            if (
                cache_module._same_file_identity(
                    os.fstat(descriptor), self.cache_root.stat()
                )
                and (self.cache_root / ".refresh.lock").is_dir()
                and not failed
            ):
                failed = True
                raise OSError("synthetic post-publish fsync failure")
            real_fsync_directory(descriptor)

        with patch.object(
            cache_module,
            "_fsync_directory_descriptor",
            side_effect=fail_once_after_publish,
        ):
            with self.assertRaises((OSError, StockError)):
                CacheLock.acquire(self.cache_root)

        self.assertTrue(failed)
        self.assertFalse((self.cache_root / ".refresh.lock").exists())
        replacement = CacheLock.acquire(self.cache_root)
        self.addCleanup(replacement.release)
        replacement.assert_owned()

    def test_post_publish_attestation_failure_quarantines_owned_lock(self) -> None:
        canonical = self.cache_root / ".refresh.lock"
        real_read_owner = CacheLock._read_owner_from_directory_descriptor
        failed = False

        def fail_first_canonical_attestation(
            descriptor: int, expected: os.stat_result
        ) -> tuple[str, float] | None:
            nonlocal failed
            if canonical.is_dir() and not failed:
                failed = True
                return None
            return real_read_owner(descriptor, expected)

        with patch.object(
            CacheLock,
            "_read_owner_from_directory_descriptor",
            side_effect=fail_first_canonical_attestation,
        ):
            with self.assertRaisesRegex(StockError, "cache_locked"):
                CacheLock.acquire(self.cache_root)

        self.assertTrue(failed)
        self.assertFalse(canonical.exists())
        replacement = CacheLock.acquire(self.cache_root)
        self.addCleanup(replacement.release)
        replacement.assert_owned()

    def test_post_publish_cleanup_does_not_remove_successor_lock(self) -> None:
        canonical = self.cache_root / ".refresh.lock"
        abandoned = self.cache_root / ".refresh.lock.abandoned"
        successor_token = "successor-writer"
        real_lstat_child = cache_module._lstat_private_child
        successor_identity: tuple[int, int] | None = None
        displaced = False

        def displace_before_attestation(
            parent_descriptor: int | None,
            parent: Path,
            name: str,
            *,
            missing_ok: bool = False,
        ) -> os.stat_result | None:
            nonlocal displaced, successor_identity
            observed = real_lstat_child(
                parent_descriptor, parent, name, missing_ok=missing_ok
            )
            if (
                name == canonical.name
                and observed is not None
                and stat.S_ISDIR(observed.st_mode)
                and not displaced
            ):
                displaced = True
                os.rename(canonical, abandoned)
                canonical.mkdir(mode=0o700)
                (canonical / "owner.json").write_text(
                    json.dumps(
                        {"token": successor_token, "created_at": time.time()}
                    ),
                    encoding="utf-8",
                )
                (canonical / f"heartbeat-{successor_token}").write_bytes(b"")
                replacement = canonical.stat(follow_symlinks=False)
                successor_identity = (replacement.st_dev, replacement.st_ino)
            return observed

        with patch.object(
            cache_module,
            "_lstat_private_child",
            side_effect=displace_before_attestation,
        ):
            with self.assertRaisesRegex(StockError, "cache_locked"):
                CacheLock.acquire(self.cache_root)

        self.assertTrue(displaced)
        replacement = canonical.stat(follow_symlinks=False)
        self.assertEqual((replacement.st_dev, replacement.st_ino), successor_identity)
        owner = json.loads((canonical / "owner.json").read_text(encoding="utf-8"))
        self.assertEqual(owner["token"], successor_token)

    def test_post_publish_cleanup_failure_does_not_mask_primary_error(self) -> None:
        real_fsync_directory = cache_module._fsync_directory_descriptor

        def fail_after_publish(descriptor: int) -> None:
            if cache_module._same_file_identity(
                os.fstat(descriptor), self.cache_root.stat()
            ) and (
                self.cache_root / ".refresh.lock"
            ).is_dir():
                raise OSError("primary post-publish failure")
            real_fsync_directory(descriptor)

        with patch.object(
            cache_module,
            "_fsync_directory_descriptor",
            side_effect=fail_after_publish,
        ):
            with patch.object(
                CacheLock,
                "_discard_published_lock_if_owned_locked",
                side_effect=RuntimeError("secondary cleanup failure"),
            ):
                with self.assertRaisesRegex(OSError, "primary post-publish failure"):
                    CacheLock.acquire(self.cache_root)

    def test_post_publish_cleanup_retries_after_transient_owner_read_failure(
        self,
    ) -> None:
        canonical = self.cache_root / ".refresh.lock"
        real_fsync_directory = cache_module._fsync_directory_descriptor
        real_read_owner = CacheLock._read_owner_from_directory_descriptor
        fsync_failed = False
        owner_read_failed = False

        def fail_once_after_publish(descriptor: int) -> None:
            nonlocal fsync_failed
            if (
                cache_module._same_file_identity(
                    os.fstat(descriptor), self.cache_root.stat()
                )
                and canonical.is_dir()
                and not fsync_failed
            ):
                fsync_failed = True
                raise OSError("primary post-publish failure")
            real_fsync_directory(descriptor)

        def fail_first_cleanup_owner_read(
            directory_descriptor: int,
            expected: os.stat_result,
        ) -> tuple[str, float] | None:
            nonlocal owner_read_failed
            if fsync_failed and not owner_read_failed:
                owner_read_failed = True
                return None
            return real_read_owner(directory_descriptor, expected)

        with patch.object(
            cache_module,
            "_fsync_directory_descriptor",
            side_effect=fail_once_after_publish,
        ):
            with patch.object(
                CacheLock,
                "_read_owner_from_directory_descriptor",
                side_effect=fail_first_cleanup_owner_read,
            ):
                with self.assertRaisesRegex(OSError, "primary post-publish failure"):
                    CacheLock.acquire(self.cache_root)

        self.assertTrue(fsync_failed)
        self.assertTrue(owner_read_failed)
        self.assertFalse(canonical.exists())
        replacement = CacheLock.acquire(self.cache_root)
        self.addCleanup(replacement.release)
        replacement.assert_owned()

    @unittest.skipUnless(os.name == "posix", "Directory FD cleanup проверяется на POSIX")
    def test_post_publish_cleanup_does_not_delete_through_swapped_cache_root(
        self,
    ) -> None:
        canonical = self.cache_root / ".refresh.lock"
        parked_root = Path(self.temp_dir.name) / "parked-cache-root"
        real_fsync_directory = cache_module._fsync_directory_descriptor
        real_read_owner = CacheLock._read_owner_from_directory_descriptor
        external_marker: Path | None = None
        fsync_failed = False
        owner_reads = 0
        root_swapped = False

        def fail_once_after_publish(descriptor: int) -> None:
            nonlocal fsync_failed
            if (
                cache_module._same_file_identity(
                    os.fstat(descriptor), self.cache_root.stat()
                )
                and canonical.is_dir()
                and not fsync_failed
            ):
                fsync_failed = True
                raise OSError("primary post-publish failure")
            real_fsync_directory(descriptor)

        def swap_root_after_quarantine_attestation(
            directory_descriptor: int,
            expected: os.stat_result,
        ) -> tuple[str, float] | None:
            nonlocal external_marker, owner_reads, root_swapped
            observed = real_read_owner(directory_descriptor, expected)
            owner_reads += 1
            if (
                observed is not None
                and not root_swapped
                and owner_reads == 2
            ):
                quarantine = next(
                    self.cache_root.glob(".refresh.lock.abort-*")
                )
                root_swapped = True
                os.rename(self.cache_root, parked_root)
                self.cache_root.mkdir(mode=0o700)
                external_quarantine = self.cache_root / quarantine.name
                external_quarantine.mkdir(mode=0o700)
                external_marker = external_quarantine / "must-survive.txt"
                external_marker.write_text("safe", encoding="utf-8")
            return observed

        with patch.object(
            cache_module,
            "_fsync_directory_descriptor",
            side_effect=fail_once_after_publish,
        ):
            with patch.object(
                CacheLock,
                "_read_owner_from_directory_descriptor",
                side_effect=swap_root_after_quarantine_attestation,
            ):
                with self.assertRaisesRegex(OSError, "primary post-publish failure"):
                    CacheLock.acquire(self.cache_root)

        self.assertTrue(fsync_failed)
        self.assertTrue(root_swapped)
        self.assertIsNotNone(external_marker)
        self.assertTrue(external_marker.is_file())
        self.assertEqual(external_marker.read_text(encoding="utf-8"), "safe")
        self.assertTrue(
            any(parked_root.glob(".refresh.lock.abort-*")),
            "Owned quarantine may be safely retained after root identity loss",
        )

    @unittest.skipUnless(os.name == "posix", "Directory FD cleanup проверяется на POSIX")
    def test_abort_owner_read_does_not_harden_replaced_root_owner(self) -> None:
        lock = CacheLock.acquire(self.cache_root)
        canonical = self.cache_root / ".refresh.lock"
        os.chmod(canonical / "owner.json", 0o666)
        parked_root = Path(self.temp_dir.name) / "parked-owner-root"
        real_lstat_child = cache_module._lstat_private_child
        external_owner: Path | None = None
        root_swapped = False

        def swap_root_after_canonical_identity(
            parent_descriptor: int | None,
            parent: Path,
            name: str,
            *,
            missing_ok: bool = False,
        ) -> os.stat_result | None:
            nonlocal external_owner, root_swapped
            observed = real_lstat_child(
                parent_descriptor,
                parent,
                name,
                missing_ok=missing_ok,
            )
            if name == canonical.name and observed is not None and not root_swapped:
                root_swapped = True
                os.rename(self.cache_root, parked_root)
                self.cache_root.mkdir(mode=0o700)
                external_lock = self.cache_root / canonical.name
                external_lock.mkdir(mode=0o700)
                external_owner = external_lock / "owner.json"
                external_owner.write_text(
                    json.dumps({"token": lock.token, "created_at": time.time()}),
                    encoding="utf-8",
                )
                os.chmod(external_owner, 0o666)
                heartbeat = external_lock / f"heartbeat-{lock.token}"
                heartbeat.write_bytes(b"")
                os.chmod(heartbeat, 0o666)
            return observed

        with patch.object(
            cache_module,
            "_lstat_private_child",
            side_effect=swap_root_after_canonical_identity,
        ):
            removed = CacheLock._discard_published_lock_if_owned_locked(
                canonical,
                lock.token,
                lock.identity,
                lock.root_attestation,
            )

        self.assertFalse(removed)
        self.assertTrue(root_swapped)
        self.assertIsNotNone(external_owner)
        self.assertEqual(stat.S_IMODE(external_owner.stat().st_mode), 0o666)
        self.assertEqual(
            json.loads(external_owner.read_text(encoding="utf-8"))["token"],
            lock.token,
        )
        retained_owners = list(parked_root.glob(".refresh.lock*/owner.json"))
        self.assertEqual(len(retained_owners), 1)
        self.assertEqual(stat.S_IMODE(retained_owners[0].stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "Directory FD cleanup проверяется на POSIX")
    def test_abort_orphan_does_not_publish_replaced_quarantine(self) -> None:
        lock = CacheLock.acquire(self.cache_root)
        canonical = self.cache_root / ".refresh.lock"
        real_lstat_child = cache_module._lstat_private_child
        owned_quarantine: Path | None = None
        replacement_owner: Path | None = None
        replacement_quarantine: Path | None = None
        replacement_marker: Path | None = None

        def replace_quarantine_after_identity_check(
            parent_descriptor: int | None,
            parent: Path,
            name: str,
            *,
            missing_ok: bool = False,
        ) -> os.stat_result | None:
            nonlocal owned_quarantine, replacement_marker
            nonlocal replacement_owner, replacement_quarantine
            observed = real_lstat_child(
                parent_descriptor,
                parent,
                name,
                missing_ok=missing_ok,
            )
            if (
                observed is not None
                and replacement_quarantine is None
                and name.startswith(".refresh.lock.abort-")
            ):
                replacement_quarantine = self.cache_root / name
                owned_quarantine = self.cache_root / f"{name}.owned"
                os.rename(replacement_quarantine, owned_quarantine)
                replacement_quarantine.mkdir(mode=0o700)
                replacement_owner = replacement_quarantine / "owner.json"
                replacement_owner.write_text(
                    json.dumps(
                        {"token": "foreign-owner", "created_at": time.time()}
                    ),
                    encoding="utf-8",
                )
                os.chmod(replacement_owner, 0o666)
                replacement_marker = replacement_quarantine / "must-not-publish.txt"
                replacement_marker.write_text("safe", encoding="utf-8")
            return observed

        with patch.object(
            cache_module,
            "_lstat_private_child",
            side_effect=replace_quarantine_after_identity_check,
        ):
            removed = CacheLock._discard_published_lock_if_owned_locked(
                canonical,
                lock.token,
                lock.identity,
                lock.root_attestation,
            )

        self.assertFalse(removed)
        self.assertIsNotNone(owned_quarantine)
        self.assertIsNotNone(replacement_owner)
        self.assertIsNotNone(replacement_quarantine)
        self.assertIsNotNone(replacement_marker)
        self.assertTrue(owned_quarantine.is_dir())
        self.assertFalse(canonical.exists())
        self.assertTrue(replacement_quarantine.is_dir())
        self.assertEqual(stat.S_IMODE(replacement_owner.stat().st_mode), 0o666)
        self.assertEqual(replacement_marker.read_text(encoding="utf-8"), "safe")

    @unittest.skipUnless(os.name == "posix", "Directory FD cleanup проверяется на POSIX")
    def test_abort_owner_change_retains_quarantine(self) -> None:
        lock = CacheLock.acquire(self.cache_root)
        canonical = self.cache_root / ".refresh.lock"
        real_lstat_child = cache_module._lstat_private_child
        replacement_token = "replacement-owner"
        quarantine: Path | None = None

        def replace_owner_after_quarantine_identity_check(
            parent_descriptor: int | None,
            parent: Path,
            name: str,
            *,
            missing_ok: bool = False,
        ) -> os.stat_result | None:
            nonlocal quarantine
            observed = real_lstat_child(
                parent_descriptor,
                parent,
                name,
                missing_ok=missing_ok,
            )
            if (
                observed is not None
                and quarantine is None
                and name.startswith(".refresh.lock.abort-")
            ):
                quarantine = self.cache_root / name
                (quarantine / "owner.json").write_text(
                    json.dumps(
                        {"token": replacement_token, "created_at": time.time()}
                    ),
                    encoding="utf-8",
                )
            return observed

        with patch.object(
            cache_module,
            "_lstat_private_child",
            side_effect=replace_owner_after_quarantine_identity_check,
        ):
            removed = CacheLock._discard_published_lock_if_owned_locked(
                canonical,
                lock.token,
                lock.identity,
                lock.root_attestation,
            )

        self.assertFalse(removed)
        self.assertIsNotNone(quarantine)
        self.assertFalse(canonical.exists())
        self.assertTrue(quarantine.is_dir())
        owner = json.loads((quarantine / "owner.json").read_text(encoding="utf-8"))
        self.assertEqual(owner["token"], replacement_token)

    @unittest.skipUnless(os.name == "posix", "Directory FD cleanup проверяется на POSIX")
    def test_abort_inventory_change_retains_quarantine(self) -> None:
        lock = CacheLock.acquire(self.cache_root)
        canonical = self.cache_root / ".refresh.lock"
        real_read_owner = CacheLock._read_owner_from_directory_descriptor
        quarantine: Path | None = None
        injected_marker: Path | None = None
        owner_reads = 0

        def fail_moved_owner_after_inventory_injection(
            directory_descriptor: int,
            expected: os.stat_result,
        ) -> tuple[str, float] | None:
            nonlocal injected_marker, owner_reads, quarantine
            owner_reads += 1
            if owner_reads == 2:
                quarantine = next(
                    self.cache_root.glob(".refresh.lock.abort-*")
                )
                injected_marker = quarantine / "late-regular-file.txt"
                injected_marker.write_text("safe", encoding="utf-8")
                return None
            return real_read_owner(directory_descriptor, expected)

        with patch.object(
            CacheLock,
            "_read_owner_from_directory_descriptor",
            side_effect=fail_moved_owner_after_inventory_injection,
        ):
            removed = CacheLock._discard_published_lock_if_owned_locked(
                canonical,
                lock.token,
                lock.identity,
                lock.root_attestation,
            )

        self.assertFalse(removed)
        self.assertIsNotNone(quarantine)
        self.assertIsNotNone(injected_marker)
        self.assertFalse(canonical.exists())
        self.assertTrue(quarantine.is_dir())
        self.assertEqual(injected_marker.read_text(encoding="utf-8"), "safe")

    @unittest.skipUnless(os.name == "posix", "Directory FD cleanup проверяется на POSIX")
    def test_abort_owner_failure_never_reverse_publishes_quarantine(self) -> None:
        lock = CacheLock.acquire(self.cache_root)
        canonical = self.cache_root / ".refresh.lock"
        real_read_owner = CacheLock._read_owner_from_directory_descriptor
        real_rename = cache_module.os.rename
        owner_reads = 0
        reverse_attempted = False
        foreign_marker: Path | None = None
        parked_owned: Path | None = None

        def fail_moved_owner(
            directory_descriptor: int,
            expected: os.stat_result,
        ) -> tuple[str, float] | None:
            nonlocal owner_reads
            owner_reads += 1
            if owner_reads == 2:
                return None
            return real_read_owner(directory_descriptor, expected)

        def swap_at_reverse_publish(
            source: object,
            destination: object,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal foreign_marker, parked_owned, reverse_attempted
            source_name = os.fspath(source)
            destination_name = os.fspath(destination)
            if (
                source_name.startswith(".refresh.lock.abort-")
                and destination_name == canonical.name
            ):
                reverse_attempted = True
                quarantine = self.cache_root / source_name
                parked_owned = quarantine.with_name(f"{quarantine.name}.owned")
                real_rename(quarantine, parked_owned)
                quarantine.mkdir(mode=0o700)
                foreign_marker = quarantine / "must-not-publish.txt"
                foreign_marker.write_text("safe", encoding="utf-8")
            real_rename(source, destination, *args, **kwargs)

        with patch.object(
            CacheLock,
            "_read_owner_from_directory_descriptor",
            side_effect=fail_moved_owner,
        ):
            with patch.object(
                cache_module.os,
                "rename",
                side_effect=swap_at_reverse_publish,
            ):
                removed = CacheLock._discard_published_lock_if_owned_locked(
                    canonical,
                    lock.token,
                    lock.identity,
                    lock.root_attestation,
                )

        self.assertFalse(removed)
        self.assertFalse(reverse_attempted)
        self.assertFalse(canonical.exists())
        self.assertIsNone(foreign_marker)
        self.assertIsNone(parked_owned)
        quarantine = next(self.cache_root.glob(".refresh.lock.abort-*"))
        self.assertEqual(
            json.loads((quarantine / "owner.json").read_text(encoding="utf-8"))[
                "token"
            ],
            lock.token,
        )

    @unittest.skipUnless(os.name == "posix", "Directory FD cleanup проверяется на POSIX")
    def test_abort_attestation_never_deletes_quarantine_at_late_rmdir_swap(
        self,
    ) -> None:
        lock = CacheLock.acquire(self.cache_root)
        canonical = self.cache_root / ".refresh.lock"
        owner_bytes = (canonical / "owner.json").read_bytes()
        heartbeat_name = f"heartbeat-{lock.token}"
        heartbeat_bytes = (canonical / heartbeat_name).read_bytes()
        foreign = self.cache_root / ".foreign-abort-directory"
        foreign.mkdir(mode=0o700)
        foreign_marker = foreign / "must-survive.txt"
        foreign_marker.write_text("safe", encoding="utf-8")
        real_rename = cache_module.os.rename
        real_rmdir = cache_module.os.rmdir
        quarantine: Path | None = None
        retained: Path | None = None
        rmdir_attempted = False

        def swap_at_would_be_rmdir(
            name: object,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal foreign, foreign_marker, quarantine, retained
            nonlocal rmdir_attempted
            candidate_name = os.fspath(name)
            if candidate_name.startswith(".refresh.lock.abort-"):
                rmdir_attempted = True
                quarantine = self.cache_root / candidate_name
                retained = quarantine.with_name(f"{quarantine.name}.retained")
                real_rename(quarantine, retained)
                real_rename(foreign, quarantine)
                foreign = quarantine
                foreign_marker = foreign / "must-survive.txt"
            real_rmdir(name, *args, **kwargs)

        with patch.object(
            cache_module.os,
            "rmdir",
            side_effect=swap_at_would_be_rmdir,
        ):
            removed = CacheLock._discard_published_lock_if_owned_locked(
                canonical,
                lock.token,
                lock.identity,
                lock.root_attestation,
            )

        self.assertFalse(removed)
        self.assertFalse(rmdir_attempted)
        self.assertFalse(canonical.exists())
        if quarantine is None:
            quarantine = next(self.cache_root.glob(".refresh.lock.abort-*"))
        if retained is None:
            retained = quarantine
        self.assertTrue((retained / "owner.json").is_file())
        self.assertEqual((retained / "owner.json").read_bytes(), owner_bytes)
        self.assertTrue((retained / heartbeat_name).is_file())
        self.assertEqual((retained / heartbeat_name).read_bytes(), heartbeat_bytes)
        self.assertTrue(foreign_marker.is_file())
        self.assertEqual(foreign_marker.read_text(encoding="utf-8"), "safe")
        successor = CacheLock.acquire(self.cache_root)
        self.addCleanup(successor.release)
        successor.assert_owned()
        with self.assertRaisesRegex(StockError, "cache_locked"):
            lock.assert_owned()
        with self.assertRaisesRegex(StockError, "cache_locked"):
            lock.heartbeat()

    @unittest.skipUnless(os.name == "posix", "Точные POSIX modes доступны только на POSIX")
    def test_load_hardens_existing_private_cache_artifacts(self) -> None:
        self.fixture.seed_generation()
        generation = self.cache_root / "generations" / "generation-existing"
        directories = [self.cache_root, self.cache_root / "generations", generation]
        files = [
            self.cache_root / "current.json",
            generation / "manifest.json",
            generation / "products.jsonl",
            generation / "offers.jsonl",
            generation / "state.json",
        ]
        for path in directories:
            os.chmod(path, 0o777)
        for path in files:
            os.chmod(path, 0o666)

        state = CacheState.load(self.cache_root)

        self.assertIsNotNone(state)
        self.assertEqual(
            {path: stat.S_IMODE(path.stat().st_mode) for path in directories},
            {path: 0o700 for path in directories},
        )
        self.assertEqual(
            {path: stat.S_IMODE(path.stat().st_mode) for path in files},
            {path: 0o600 for path in files},
        )

    @unittest.skipUnless(os.name == "posix", "Symlink race проверяется на POSIX")
    def test_pointer_swap_to_symlink_fails_without_chmod_target(self) -> None:
        self.fixture.seed_generation()
        current = self.cache_root / "current.json"
        original = self.cache_root / "current.original.json"
        outside = Path(self.temp_dir.name) / "outside-current.json"
        outside.write_bytes(current.read_bytes())
        os.chmod(outside, 0o666)
        outside_mode = stat.S_IMODE(outside.stat().st_mode)
        real_open_child = cache_module._open_private_child_regular_file
        swapped = False

        def swap_before_open(parent_descriptor: int, name: str) -> int:
            nonlocal swapped
            if name == "current.json" and not swapped:
                swapped = True
                os.rename(current, original)
                os.symlink(outside, current)
            return real_open_child(parent_descriptor, name)

        with patch.object(
            cache_module,
            "_open_private_child_regular_file",
            side_effect=swap_before_open,
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                CacheState.load(self.cache_root)

        self.assertTrue(swapped)
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), outside_mode)

    @unittest.skipUnless(os.name == "posix", "Symlink semantics проверяются на POSIX")
    def test_generations_symlink_is_rejected_without_external_writes(self) -> None:
        outside = Path(self.temp_dir.name) / "outside-generations"
        outside.mkdir()
        (self.cache_root / "generations").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "Symlink semantics проверяются на POSIX")
    def test_cleanup_rejects_nonempty_generations_symlink_without_external_deletion(
        self,
    ) -> None:
        outside = Path(self.temp_dir.name) / "outside-generations"
        victim = outside / "generation-victim"
        victim.mkdir(parents=True)
        marker = victim / "must-survive.txt"
        marker.write_text("safe", encoding="utf-8")
        (self.cache_root / "generations").symlink_to(
            outside, target_is_directory=True
        )

        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertTrue(victim.is_dir())
        self.assertEqual(marker.read_text(encoding="utf-8"), "safe")

    @unittest.skipUnless(os.name == "posix", "Symlink race проверяется на POSIX")
    def test_cleanup_parent_swap_does_not_traverse_external_generations(self) -> None:
        generations = self.cache_root / "generations"
        generations.mkdir()
        internal = generations / "generation-internal"
        internal.mkdir()
        (internal / "internal.txt").write_text("internal", encoding="utf-8")
        parked = self.cache_root / "generations-parked"
        outside = Path(self.temp_dir.name) / "outside-generations"
        victim = outside / "generation-victim"
        victim.mkdir(parents=True)
        marker = victim / "must-survive.txt"
        marker.write_text("safe", encoding="utf-8")
        real_listdir = os.listdir
        swapped = False

        def swap_before_listing(target: object) -> list[str]:
            nonlocal swapped
            if not swapped and (
                target == generations
                or (
                    isinstance(target, int)
                    and os.fstat(target).st_ino == generations.stat().st_ino
                )
            ):
                swapped = True
                os.rename(generations, parked)
                generations.symlink_to(outside, target_is_directory=True)
            return real_listdir(target)

        with CacheLock.acquire(self.cache_root) as lock:
            with patch.object(cache_module.os, "listdir", swap_before_listing):
                warning = StockCache(
                    self.cache_root, FakeHttpClient()
                )._cleanup_inactive_generations(lock)

        self.assertTrue(swapped)
        self.assertEqual(warning, "cache_cleanup_incomplete")
        self.assertTrue(victim.is_dir())
        self.assertEqual(marker.read_text(encoding="utf-8"), "safe")

    @unittest.skipUnless(os.name == "posix", "Symlink semantics проверяются на POSIX")
    def test_inactive_generation_rollback_does_not_delete_external_victim(self) -> None:
        outside = Path(self.temp_dir.name) / "outside-generations"
        victim = outside / "generation-victim"
        victim.mkdir(parents=True)
        marker = victim / "must-survive.txt"
        marker.write_text("safe", encoding="utf-8")
        (self.cache_root / "generations").symlink_to(
            outside, target_is_directory=True
        )

        StockCache(self.cache_root, FakeHttpClient())._remove_generation_if_inactive(
            self.cache_root / "generations" / "generation-victim"
        )

        self.assertTrue(victim.is_dir())
        self.assertEqual(marker.read_text(encoding="utf-8"), "safe")

    @unittest.skipUnless(os.name == "posix", "Directory FD deletion проверяется на POSIX")
    def test_cleanup_does_not_delete_replaced_quarantine_directory(self) -> None:
        generations = self.cache_root / "generations"
        generations.mkdir(mode=0o700)
        generation = generations / "generation-victim"
        generation.mkdir(mode=0o700)
        (generation / "owned.txt").write_text("owned", encoding="utf-8")
        parked = generations / "parked-owned-generation"
        outside = Path(self.temp_dir.name) / "outside-generation"
        outside.mkdir(mode=0o755)
        marker = outside / "must-survive.txt"
        marker.write_text("safe", encoding="utf-8")
        real_stat = os.stat
        swapped = False

        def replace_quarantine_after_identity_check(
            path: object,
            *args: object,
            **kwargs: object,
        ) -> os.stat_result:
            nonlocal swapped
            observed = real_stat(path, *args, **kwargs)
            if (
                not swapped
                and isinstance(path, str)
                and path.startswith(".generation-victim.delete-")
                and kwargs.get("dir_fd") is not None
                and kwargs.get("follow_symlinks") is False
            ):
                swapped = True
                quarantine = generations / path
                os.rename(quarantine, parked)
                os.rename(outside, quarantine)
            return observed

        descriptor = cache_module._open_private_directory(generations)
        self.assertIsNotNone(descriptor)
        try:
            observed = cache_module._lstat_private_child(
                descriptor,
                generations,
                generation.name,
            )
            self.assertIsNotNone(observed)
            with patch.object(
                cache_module.os,
                "stat",
                side_effect=replace_quarantine_after_identity_check,
            ):
                cache_module._remove_private_child_directory(
                    descriptor,
                    generations,
                    generation.name,
                    observed,
                )
        finally:
            cache_module._close_optional_descriptor(descriptor)

        self.assertTrue(swapped)
        surviving_markers = list(Path(self.temp_dir.name).rglob(marker.name))
        self.assertEqual(len(surviving_markers), 1)
        self.assertEqual(surviving_markers[0].read_text(encoding="utf-8"), "safe")

    def test_recursive_cleanup_without_directory_fd_fails_closed(self) -> None:
        parent = self.cache_root / "windows-best-effort-parent"
        parent.mkdir(mode=0o700)
        victim = parent / "generation-victim"
        victim.mkdir(mode=0o700)
        marker = victim / "must-survive.txt"
        marker.write_text("safe", encoding="utf-8")
        observed = victim.lstat()

        removed = cache_module._remove_private_child_directory(
            None,
            parent,
            victim.name,
            observed,
        )

        self.assertFalse(removed)
        self.assertEqual(marker.read_text(encoding="utf-8"), "safe")

    def test_runtime_status_unlink_without_directory_fd_fails_closed_on_root_swap(
        self,
    ) -> None:
        root = Path(self.temp_dir.name) / "runtime-root"
        root.mkdir(mode=0o700)
        status = root / ".runtime-status-generation-a.json"
        status.write_text("owned", encoding="utf-8")
        observed = status.lstat()
        parked_root = Path(self.temp_dir.name) / "parked-runtime-root"
        external_root = Path(self.temp_dir.name) / "external-runtime-root"
        external_root.mkdir(mode=0o700)
        external_status = external_root / status.name
        external_status.write_text("safe", encoding="utf-8")
        real_unlink = Path.unlink
        swapped = False

        def swap_root_before_path_unlink(
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal swapped
            if path == status and not swapped:
                swapped = True
                os.rename(root, parked_root)
                os.rename(external_root, root)
            real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", swap_root_before_path_unlink):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                cache_module._unlink_private_child_regular_file(
                    None,
                    root,
                    status.name,
                    observed,
                )

        self.assertFalse(swapped)
        self.assertEqual(
            (root / status.name).read_text(encoding="utf-8"),
            "owned",
        )
        self.assertEqual(
            (external_root / status.name).read_text(encoding="utf-8"),
            "safe",
        )

    @unittest.skipUnless(os.name == "posix", "Directory FD fsync проверяется на POSIX")
    def test_pointer_unlink_does_not_fsync_replaced_cache_root(self) -> None:
        pointer = CurrentPointer(
            generation_id="generation-a",
            directory_name="generation-existing",
            activation_token="synthetic-activation",
        )
        runtime_value = {
            "generation_id": "generation-a",
            "checked_at": "2026-08-27T10:00:00+00:00",
            "stale": False,
            "warning_code": None,
            "revision": "a" * 32,
        }
        expected_runtime = cache_module.validate_runtime_status(
            runtime_value,
            pointer.generation_id,
        )
        current_path = self.cache_root / "current.json"
        cache_module._write_json_atomic(current_path, pointer.to_dict())
        cache_module._write_runtime_status_atomic(
            self.cache_root,
            pointer.directory_name,
            runtime_value,
        )
        attestation = cache_module._attest_cache_root(self.cache_root, create=False)
        try:
            (self.cache_root / ".papa-shin-stock-cache-root.json").unlink()
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                StockCache(
                    self.cache_root,
                    FakeHttpClient(),
                )._rollback_pointer_if_owned_locked(
                    current_path,
                    pointer,
                    expected_runtime,
                    previous_pointer=None,
                    root_attestation=attestation,
                )
        finally:
            attestation.close()

        self.assertTrue(current_path.exists())
        self.assertEqual(CurrentPointer.load(current_path), pointer)

    @unittest.skipUnless(os.name == "posix", "Directory FD fsync проверяется на POSIX")
    def test_private_unlink_fsyncs_retained_parent_descriptor(self) -> None:
        artifact = self.cache_root / "owned-cleanup.json"
        artifact.write_text("owned", encoding="utf-8")
        expected = artifact.lstat()
        expected_parent = self.cache_root.lstat()
        parked_root = Path(self.temp_dir.name) / "parked-fsync-root"
        real_fsync = os.fsync
        external_marker: Path | None = None
        root_swapped = False

        def swap_root_during_parent_fsync(descriptor: int) -> None:
            nonlocal external_marker, root_swapped
            observed = os.fstat(descriptor)
            if stat.S_ISDIR(observed.st_mode) and not root_swapped:
                self.assertTrue(
                    cache_module._same_file_identity(expected_parent, observed)
                )
                root_swapped = True
                os.rename(self.cache_root, parked_root)
                self.cache_root.mkdir(mode=0o755)
                os.chmod(self.cache_root, 0o755)
                external_marker = self.cache_root / "must-not-harden.txt"
                external_marker.write_text("safe", encoding="utf-8")
            real_fsync(descriptor)

        with patch.object(
            cache_module.os,
            "fsync",
            side_effect=swap_root_during_parent_fsync,
        ):
            removed = cache_module._unlink_private_regular_file_if_owned(
                artifact,
                expected,
            )

        self.assertTrue(removed)
        self.assertTrue(root_swapped)
        self.assertIsNotNone(external_marker)
        self.assertEqual(external_marker.read_text(encoding="utf-8"), "safe")
        self.assertEqual(stat.S_IMODE(self.cache_root.stat().st_mode), 0o755)

    @unittest.skipUnless(os.name == "posix", "Directory FD deletion проверяется на POSIX")
    def test_cleanup_rejects_directory_injected_after_quarantine_rename(
        self,
    ) -> None:
        generations = self.cache_root / "generations"
        generations.mkdir(mode=0o700)
        generation = generations / "generation-victim"
        generation.mkdir(mode=0o700)
        (generation / "owned.txt").write_text("owned", encoding="utf-8")
        outside = Path(self.temp_dir.name) / "outside-injected-generation"
        outside.mkdir(mode=0o755)
        marker = outside / "must-survive.txt"
        marker.write_text("safe", encoding="utf-8")
        real_empty = cache_module._empty_private_directory_descriptor
        injected = False

        def inject_before_walking(
            descriptor: int,
            path: Path,
            *args: object,
        ) -> bool:
            nonlocal injected
            if not injected:
                injected = True
                os.rename(outside, path / "late-external-directory")
            return real_empty(descriptor, path, *args)

        with patch.object(
            cache_module,
            "_empty_private_directory_descriptor",
            side_effect=inject_before_walking,
        ):
            removed = cache_module._remove_private_cache_generation(
                self.cache_root,
                generation,
            )

        self.assertTrue(injected)
        self.assertFalse(removed)
        surviving_markers = list(Path(self.temp_dir.name).rglob(marker.name))
        self.assertEqual(len(surviving_markers), 1)
        self.assertEqual(surviving_markers[0].read_text(encoding="utf-8"), "safe")

    def test_windows_permission_hardening_is_explicit_best_effort(self) -> None:
        artifact = self.cache_root / "windows-best-effort.json"
        artifact.write_text("{}", encoding="utf-8")
        descriptor = os.open(artifact, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)

        with patch.object(cache_module.os, "name", "nt"):
            with patch.object(
                cache_module.os,
                "fchmod",
                side_effect=AssertionError("Windows branch must not promise POSIX modes"),
            ):
                cache_module._harden_private_descriptor(
                    descriptor, stat.S_IFREG, 0o600
                )

    def test_windows_directory_validation_does_not_require_directory_fd(self) -> None:
        with patch.object(cache_module.os, "name", "nt"):
            with patch.object(
                cache_module.os,
                "open",
                side_effect=AssertionError("Windows cannot portably open directories"),
            ):
                cache_module._ensure_private_directory(self.cache_root)

    def test_download_cleanup_without_parent_directory_fd_retains_owned_file(
        self,
    ) -> None:
        path = self.cache_root / "partial-download.jsonl"
        destination = cache_module._PrivateDownloadDestination(path)
        self.addCleanup(destination.close)
        destination.write_bytes(b"partial")

        with patch.object(
            cache_module,
            "_open_private_parent_directory_for_cleanup",
            return_value=None,
            create=True,
        ):
            destination.unlink(missing_ok=True)

        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b"partial")

    def test_windows_311_directory_reparse_point_is_rejected_without_is_junction(
        self,
    ) -> None:
        path_without_is_junction = SimpleNamespace()
        observed = SimpleNamespace(
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        )

        self.assertTrue(
            cache_module._is_windows_directory_reparse_point(
                path_without_is_junction, observed
            )
        )

    @unittest.skipUnless(os.name == "posix", "Symlink semantics проверяются на POSIX")
    def test_download_symlink_swap_does_not_overwrite_external_file(self) -> None:
        outside = Path(self.temp_dir.name) / "outside-download.jsonl"
        outside.write_bytes(b"outside-safe")

        class SwappingHttpClient(FakeHttpClient):
            def download(
                nested_self,
                url: str,
                destination: Path,
                expected_bytes: int,
                expected_sha256: str,
                progress: object | None = None,
            ) -> DownloadReceipt:
                if destination.name == "products.jsonl":
                    destination_path = Path(os.fspath(destination))
                    destination_path.unlink()
                    destination_path.symlink_to(outside)
                return super().download(
                    url,
                    destination,
                    expected_bytes,
                    expected_sha256,
                    progress,
                )

        with self.assertRaises(StockError):
            StockCache(self.cache_root, SwappingHttpClient()).refresh(self.config)

        self.assertEqual(outside.read_bytes(), b"outside-safe")

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

    def test_heartbeat_during_reclaim_fences_old_owner_before_successor_publish(
        self,
    ) -> None:
        lock = CacheLock.acquire(self.cache_root)
        stale_time = time.time() - 30 * 60 - 1
        (lock.path / "owner.json").write_text(
            json.dumps({"token": lock.token, "created_at": stale_time}),
            encoding="utf-8",
        )
        heartbeat_path = lock.path / f"heartbeat-{lock.token}"
        os.utime(heartbeat_path, (stale_time, stale_time))
        real_rename = os.rename
        reclaim_rename_seen = False

        def heartbeat_then_rename(
            source: object,
            destination: object,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal reclaim_rename_seen
            if (
                not reclaim_rename_seen
                and Path(os.fspath(destination)).name.startswith(
                    ".refresh.lock.reclaim-"
                )
            ):
                reclaim_rename_seen = True
                os.utime(heartbeat_path, None)
            real_rename(source, destination, *args, **kwargs)

        with patch(
            "papa_shin_stock.cache.os.rename",
            side_effect=heartbeat_then_rename,
        ):
            successor = CacheLock.acquire(self.cache_root)
        self.addCleanup(successor.release)

        self.assertTrue(reclaim_rename_seen)
        successor.assert_owned()
        self.assertEqual(
            json.loads(
                (self.cache_root / ".refresh.lock" / "owner.json").read_text(
                    encoding="utf-8"
                )
            )["token"],
            successor.token,
        )
        with self.assertRaisesRegex(StockError, "cache_locked"):
            lock.assert_owned()
        with self.assertRaisesRegex(StockError, "cache_locked"):
            lock.heartbeat()
        retained = list(self.cache_root.glob(".refresh.lock.reclaim-*"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(
            json.loads((retained[0] / "owner.json").read_text(encoding="utf-8"))[
                "token"
            ],
            lock.token,
        )

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

    def test_release_owner_change_fences_old_owner_and_retains_orphan(self) -> None:
        lock = CacheLock.acquire(self.cache_root)
        real_read_owner = CacheLock._read_owner_from_directory_descriptor
        owner_changed = False

        def read_then_replace_owner(
            descriptor: int, expected: os.stat_result
        ) -> tuple[str, float] | None:
            nonlocal owner_changed
            owner = real_read_owner(descriptor, expected)
            if not owner_changed:
                owner_changed = True
                (lock.path / "owner.json").write_text(
                    json.dumps(
                        {"token": "replacement-writer", "created_at": time.time()}
                    ),
                    encoding="utf-8",
                )
            return owner

        with patch.object(
            CacheLock,
            "_read_owner_from_directory_descriptor",
            side_effect=read_then_replace_owner,
        ):
            lock.release()

        self.assertFalse(lock.path.exists())
        retained = list(self.cache_root.glob(".refresh.lock.release-*"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(
            json.loads(
                (retained[0] / "owner.json").read_text(encoding="utf-8")
            )["token"],
            "replacement-writer",
        )
        with self.assertRaisesRegex(StockError, "cache_locked"):
            lock.assert_owned()
        successor = CacheLock.acquire(self.cache_root)
        self.addCleanup(successor.release)
        successor.assert_owned()

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

    def test_displaced_ownerless_creator_cannot_write_successor_lock(self) -> None:
        creator_paused = threading.Event()
        allow_creator = threading.Event()
        creator_locks: list[CacheLock] = []
        creator_errors: list[BaseException] = []
        real_mkdir = os.mkdir
        paused = False

        def pause_creator_after_lock_mkdir(
            path: object,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            nonlocal paused
            real_mkdir(path, mode, dir_fd=dir_fd)
            name = Path(os.fspath(path)).name
            if (
                threading.current_thread().name == "displaced-creator"
                and name.startswith(".refresh.lock.init-")
                and not paused
            ):
                paused = True
                creator_paused.set()
                if not allow_creator.wait(timeout=5):
                    raise AssertionError("creator pause timeout")

        def acquire_as_creator() -> None:
            try:
                creator_locks.append(CacheLock.acquire(self.cache_root))
            except BaseException as error:
                creator_errors.append(error)

        with patch.object(cache_module.os, "mkdir", pause_creator_after_lock_mkdir):
            creator = threading.Thread(
                target=acquire_as_creator, name="displaced-creator"
            )
            creator.start()
            self.assertTrue(creator_paused.wait(timeout=5))
            initialized = next(
                path
                for path in self.cache_root.iterdir()
                if path.name.startswith(".refresh.lock") and path.is_dir()
            )
            stale_time = time.time() - cache_module._LOCK_TTL_SECONDS - 1
            os.utime(initialized, (stale_time, stale_time))

            successor = CacheLock.acquire(self.cache_root)
            self.addCleanup(successor.release)
            allow_creator.set()
            creator.join(timeout=5)

        self.assertFalse(creator.is_alive())
        self.assertEqual(creator_locks, [])
        self.assertEqual(len(creator_errors), 1)
        self.assertIsInstance(creator_errors[0], StockError)
        self.assertEqual(creator_errors[0].code, "cache_locked")
        successor.assert_owned()

    def test_stale_lock_init_directory_is_boundedly_reclaimed(self) -> None:
        token = "a" * 32
        candidate = self.cache_root / f".refresh.lock.init-{token}"
        candidate.mkdir()
        stale_time = time.time() - cache_module._LOCK_TTL_SECONDS - 1
        (candidate / "owner.json").write_text(
            json.dumps({"token": token, "created_at": stale_time}),
            encoding="utf-8",
        )
        heartbeat = candidate / f"heartbeat-{token}"
        heartbeat.write_bytes(b"")
        os.utime(heartbeat, (stale_time, stale_time))

        lock = CacheLock.acquire(self.cache_root)
        self.addCleanup(lock.release)

        self.assertFalse(candidate.exists())
        lock.assert_owned()

    @unittest.skipUnless(os.name == "posix", "Symlink semantics проверяются на POSIX")
    def test_unsafe_stale_lock_init_directory_fails_closed(self) -> None:
        token = "b" * 32
        candidate = self.cache_root / f".refresh.lock.init-{token}"
        candidate.mkdir()
        outside_owner = Path(self.temp_dir.name) / "outside-init-owner.json"
        outside_owner.write_text(
            json.dumps(
                {
                    "token": token,
                    "created_at": time.time() - cache_module._LOCK_TTL_SECONDS - 1,
                }
            ),
            encoding="utf-8",
        )
        (candidate / "owner.json").symlink_to(outside_owner)
        stale_time = time.time() - cache_module._LOCK_TTL_SECONDS - 1
        os.utime(candidate, (stale_time, stale_time))

        with self.assertRaisesRegex(StockError, "cache_locked"):
            CacheLock.acquire(self.cache_root)

        self.assertTrue(candidate.is_dir())
        self.assertTrue((candidate / "owner.json").is_symlink())
        self.assertTrue(outside_owner.is_file())

    @unittest.skipUnless(os.name == "posix", "Symlink semantics проверяются на POSIX")
    def test_symlink_owner_is_not_reclaimed_as_ownerless_lock(self) -> None:
        lock = self.cache_root / ".refresh.lock"
        lock.mkdir()
        outside_owner = Path(self.temp_dir.name) / "outside-owner.json"
        outside_owner.write_text(
            json.dumps(
                {
                    "token": "outside-owner",
                    "created_at": time.time() - cache_module._LOCK_TTL_SECONDS - 1,
                }
            ),
            encoding="utf-8",
        )
        (lock / "owner.json").symlink_to(outside_owner)
        stale_time = time.time() - cache_module._LOCK_TTL_SECONDS - 1
        os.utime(lock, (stale_time, stale_time))

        with self.assertRaisesRegex(StockError, "cache_locked"):
            CacheLock.acquire(self.cache_root)

        self.assertTrue(lock.is_dir())
        self.assertTrue((lock / "owner.json").is_symlink())
        self.assertTrue(outside_owner.is_file())

    @unittest.skipUnless(os.name == "posix", "Symlink semantics проверяются на POSIX")
    def test_symlink_heartbeat_is_not_reclaimed_as_stale_lock(self) -> None:
        lock = self.cache_root / ".refresh.lock"
        lock.mkdir()
        token = "stale-writer"
        stale_time = time.time() - cache_module._LOCK_TTL_SECONDS - 1
        (lock / "owner.json").write_text(
            json.dumps({"token": token, "created_at": stale_time}),
            encoding="utf-8",
        )
        outside_heartbeat = Path(self.temp_dir.name) / "outside-heartbeat"
        outside_heartbeat.write_bytes(b"")
        (lock / f"heartbeat-{token}").symlink_to(outside_heartbeat)

        with self.assertRaisesRegex(StockError, "cache_locked"):
            CacheLock.acquire(self.cache_root)

        self.assertTrue(lock.is_dir())
        self.assertTrue((lock / f"heartbeat-{token}").is_symlink())
        self.assertTrue(outside_heartbeat.is_file())

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

    def test_stale_reclaim_owner_change_retains_orphan_for_successor(self) -> None:
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

        def replace_owner_before_rename(
            source: object,
            destination: object,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal first_rename
            if first_rename:
                first_rename = False
                owner_path.write_text(
                    json.dumps(
                        {"token": "replacement-writer", "created_at": time.time()}
                    ),
                    encoding="utf-8",
                )
            real_rename(source, destination, *args, **kwargs)

        with patch("papa_shin_stock.cache.os.rename", replace_owner_before_rename):
            successor = CacheLock.acquire(self.cache_root)
        self.addCleanup(successor.release)

        successor.assert_owned()
        self.assertEqual(
            json.loads((lock / "owner.json").read_text(encoding="utf-8"))[
                "token"
            ],
            successor.token,
        )
        retained = list(self.cache_root.glob(".refresh.lock.reclaim-*"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(
            json.loads(
                (retained[0] / "owner.json").read_text(encoding="utf-8")
            )["token"],
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

        def pause_delayed_activation(
            root: Path,
            root_attestation: cache_module.CacheRootAttestation | None = None,
        ) -> object:
            nonlocal paused
            if (
                threading.current_thread().name == "delayed-activation"
                and not paused
            ):
                paused = True
                delayed_before_commit.set()
                if not allow_delayed.wait(timeout=5):
                    raise AssertionError("delayed activation timeout")
            return real_commit_acquire(root, root_attestation)

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
        second_holds_publish_lock = threading.Event()
        allow_second_publish = threading.Event()
        first_before_release = threading.Event()
        allow_first_release = threading.Event()
        second_lock_published = threading.Event()
        first_errors: list[BaseException] = []
        second_errors: list[BaseException] = []
        second_results: list[object] = []
        real_write_json = cache_module._write_json_atomic_at
        real_publish_acquire = cache_module._RefreshLockPublishLock.acquire
        real_cache_acquire = CacheLock.acquire
        real_cache_release = CacheLock.release

        def pause_first_pointer(
            parent_descriptor: int, name: str, value: object
        ) -> None:
            if (
                name == "current.json"
                and threading.current_thread().name == "first-activation"
            ):
                first_at_pointer.set()
                if not allow_first_pointer.wait(timeout=5):
                    raise AssertionError("first pointer timeout")
            real_write_json(parent_descriptor, name, value)

        def observe_second_wait(
            root: Path,
            root_attestation: cache_module.CacheRootAttestation | None = None,
        ) -> object:
            acquired = real_publish_acquire(root, root_attestation)
            if threading.current_thread().name != "second-activation":
                return acquired
            second_holds_publish_lock.set()
            if not allow_second_publish.wait(timeout=5):
                acquired.release(suppress_errors=True)
                raise AssertionError("second publish timeout")
            return acquired

        def observe_cache_acquire(root: Path) -> CacheLock:
            acquired = real_cache_acquire(root)
            if threading.current_thread().name == "second-activation":
                second_lock_published.set()
            return acquired

        def pause_first_release(lock: CacheLock) -> None:
            if threading.current_thread().name == "first-activation":
                first_before_release.set()
                if not allow_first_release.wait(timeout=5):
                    raise AssertionError("first release timeout")
            real_cache_release(lock)

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

        with patch.object(
            cache_module, "_write_json_atomic_at", side_effect=pause_first_pointer
        ):
            with patch.object(CacheLock, "acquire", side_effect=observe_cache_acquire):
                with patch.object(CacheLock, "release", pause_first_release):
                    with patch.object(
                        cache_module._RefreshLockPublishLock,
                        "acquire",
                        side_effect=observe_second_wait,
                    ):
                        first = threading.Thread(
                            target=run_first, name="first-activation"
                        )
                        first.start()
                        self.assertTrue(first_at_pointer.wait(timeout=5))
                        second = threading.Thread(
                            target=run_second, name="second-activation"
                        )
                        second.start()
                        allow_first_pointer.set()
                        self.assertTrue(second_holds_publish_lock.wait(timeout=5))
                        self.assertTrue(first_before_release.wait(timeout=5))
                        self._expire_current_refresh_lock()
                        allow_second_publish.set()
                        self.assertTrue(second_lock_published.wait(timeout=5))
                        allow_first_release.set()
                        first.join(timeout=5)
                        second.join(timeout=5)

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
        real_write_bytes = cache_module._write_bytes_atomic_at
        real_publish_acquire = cache_module._RefreshLockPublishLock.acquire

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

        def pause_rollback(
            parent_descriptor: int, name: str, payload: bytes
        ) -> None:
            if (
                name == "current.json"
                and payload == previous_pointer
                and threading.current_thread().name == "rollback-activation"
            ):
                rollback_paused.set()
                if not allow_rollback.wait(timeout=5):
                    raise AssertionError("rollback timeout")
            real_write_bytes(parent_descriptor, name, payload)

        def observe_second_wait(
            root: Path,
            root_attestation: cache_module.CacheRootAttestation | None = None,
        ) -> object:
            if threading.current_thread().name == "rollback-successor":
                second_waiting.set()
            return real_publish_acquire(root, root_attestation)

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
                    cache_module, "_write_bytes_atomic_at", side_effect=pause_rollback
            ):
                with patch.object(
                    cache_module._RefreshLockPublishLock,
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

    def test_activation_rollback_never_overwrites_successor_304_runtime_revision(
        self,
    ) -> None:
        for iteration in range(20):
            with self.subTest(iteration=iteration):
                root = Path(self.temp_dir.name) / f"revision-race-{iteration}"
                root.mkdir()
                fixture = CacheFixture(root)
                fixture.seed_generation()
                config = StockConfig(
                    manifest_url="https://stock.example.test/manifest.json",
                    username="synthetic-user",
                    password="synthetic-password",
                    product_id_field="product_id",
                    offer_product_id_field="product_id",
                    cache_dir=root,
                )
                published = threading.Event()
                allow_failed_validation = threading.Event()
                delayed_errors: list[BaseException] = []
                real_load = CacheState.load
                post_publish_failed = False

                def pause_post_publish(
                    cache_dir: Path, progress: object | None = None
                ) -> CacheState | None:
                    nonlocal post_publish_failed
                    pointer = CurrentPointer.load(cache_dir / "current.json")
                    if (
                        cache_dir == root
                        and not post_publish_failed
                        and threading.current_thread().name
                        == f"revision-race-{iteration}"
                        and pointer.generation_id == "generation-b"
                    ):
                        post_publish_failed = True
                        published.set()
                        if not allow_failed_validation.wait(timeout=5):
                            raise AssertionError("post publish timeout")
                        raise StockError(
                            "cache_unavailable",
                            "Синтетическая ошибка post-publish",
                            7,
                        )
                    return real_load(
                        cache_dir, progress if callable(progress) else None
                    )

                def run_delayed() -> None:
                    try:
                        StockCache(root, FakeHttpClient()).refresh(config)
                    except BaseException as error:
                        delayed_errors.append(error)

                with patch.object(
                    CacheState, "load", side_effect=pause_post_publish
                ):
                    delayed = threading.Thread(
                        target=run_delayed, name=f"revision-race-{iteration}"
                    )
                    delayed.start()
                    self.assertTrue(published.wait(timeout=5))
                    self._expire_refresh_lock_at(root)
                    successor = StockCache(
                        root,
                        FakeHttpClient(
                            response=HttpResponse(status=304, headers={}, body=b"")
                        ),
                    ).refresh(config)
                    successor_state = CacheState.load(root)
                    self.assertIsNotNone(successor_state)
                    allow_failed_validation.set()
                    delayed.join(timeout=5)

                self.assertFalse(delayed.is_alive())
                self.assertEqual(delayed_errors, [])
                self.assertEqual(successor.status, "not_modified")
                final = CacheState.load(root)
                self.assertIsNotNone(final)
                self.assertEqual(final.generation_id, "generation-b")
                self.assertEqual(
                    final.runtime_revision, successor_state.runtime_revision
                )
                self.assertTrue(final.files.manifest.parent.is_dir())

    def test_failed_post_publish_check_preserves_pointer_target_when_cleanup_unproven(
        self,
    ) -> None:
        self.fixture.seed_generation()
        real_load = CacheState.load
        real_commit_acquire = cache_module.RuntimeCommitLock.acquire
        publish_commit_completed = False

        def fail_persistently_after_publish(
            cache_dir: Path, progress: object | None = None
        ) -> CacheState | None:
            pointer = CurrentPointer.load(cache_dir / "current.json")
            if pointer.generation_id == "generation-b":
                raise StockError(
                    "post_publish_failure",
                    "Синтетическая ошибка post-publish",
                    7,
                )
            return real_load(cache_dir, progress if callable(progress) else None)

        def fail_rollback_and_cleanup_lock(
            root: Path,
            root_attestation: cache_module.CacheRootAttestation | None = None,
        ) -> object:
            nonlocal publish_commit_completed
            if not publish_commit_completed:
                publish_commit_completed = True
                return real_commit_acquire(root, root_attestation)
            raise StockError(
                "cache_unavailable", "Синтетическая ошибка commit lock", 7
            )

        with patch.object(CacheState, "load", side_effect=fail_persistently_after_publish):
            with patch.object(
                cache_module.RuntimeCommitLock,
                "acquire",
                side_effect=fail_rollback_and_cleanup_lock,
            ):
                with self.assertRaises(StockError) as raised:
                    StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        self.assertEqual(raised.exception.code, "post_publish_failure")
        pointer = CurrentPointer.load(self.cache_root / "current.json")
        self.assertEqual(pointer.generation_id, "generation-b")
        target = self.cache_root / "generations" / pointer.directory_name
        self.assertTrue(target.is_dir())
        self.assertTrue((target / "manifest.json").is_file())
        self.assertTrue(
            (
                self.cache_root
                / f".runtime-status-{pointer.directory_name}.json"
            ).is_file()
        )

    def test_interrupted_pointer_replace_rolls_back_to_previous_generation(self) -> None:
        self.fixture.seed_generation()
        real_replace = os.replace

        def interrupt_current_replace(
            source: object, destination: object, **kwargs: object
        ) -> None:
            if str(destination) == "current.json" or Path(destination) == self.cache_root / "current.json":
                raise OSError("synthetic pointer interruption")
            real_replace(source, destination, **kwargs)

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
        real_remove = cache_module._remove_private_child_directory

        def fail_for_previous_generation(
            parent_descriptor: int | None,
            parent: Path,
            name: str,
            expected: os.stat_result | None = None,
        ) -> bool:
            if name == "generation-existing":
                return False
            return real_remove(parent_descriptor, parent, name, expected)

        with patch.object(
            cache_module,
            "_remove_private_child_directory",
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
        real_remove = cache_module._remove_private_child_directory
        generations = self.cache_root / "generations"

        def fail_for_generation(
            parent_descriptor: int | None,
            parent: Path,
            name: str,
            expected: os.stat_result | None = None,
        ) -> bool:
            if "generation-" in name or ".staging-" in name:
                return False
            return real_remove(parent_descriptor, parent, name, expected)

        with patch.object(
            cache_module,
            "_remove_private_child_directory",
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

    def test_post_rename_generation_cleanup_failure_blocks_redownload(self) -> None:
        self.fixture.seed_generation()
        client = FakeHttpClient()
        generations = self.cache_root / "generations"
        real_empty = cache_module._empty_private_directory_descriptor

        def fail_after_generation_quarantine(
            descriptor: int,
            path: Path,
            inventory: dict[str, os.stat_result],
        ) -> bool:
            if path.name.startswith(".generation-existing.delete-"):
                return False
            return real_empty(descriptor, path, inventory)

        with patch.object(
            cache_module,
            "_empty_private_directory_descriptor",
            side_effect=fail_after_generation_quarantine,
        ):
            first = StockCache(self.cache_root, client).refresh(self.config)
            pointer_after_first = (self.cache_root / "current.json").read_bytes()
            quarantines_after_first = sorted(
                path.name
                for path in generations.iterdir()
                if path.name.startswith(".generation-existing.delete-")
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
        self.assertEqual(downloads_after_first, 2)
        self.assertEqual(len(quarantines_after_first), 1)
        self.assertEqual(
            sorted(
                path.name
                for path in generations.iterdir()
                if ".delete-" in path.name
            ),
            quarantines_after_first,
        )

    def test_repeated_staging_cleanup_failure_without_cache_is_bounded(self) -> None:
        client = FakeHttpClient(interrupt_offers=True)
        real_remove = cache_module._remove_private_child_directory
        generations = self.cache_root / "generations"

        def fail_for_staging(
            parent_descriptor: int | None,
            parent: Path,
            name: str,
            expected: os.stat_result | None = None,
        ) -> bool:
            if ".staging-" in name:
                return False
            return real_remove(parent_descriptor, parent, name, expected)

        with patch.object(
            cache_module,
            "_remove_private_child_directory",
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

    def test_invalid_manifest_identity_preserves_previous_generation(self) -> None:
        self.fixture.seed_generation()
        previous_pointer = (self.cache_root / "current.json").read_bytes()
        invalid_values = (
            ("generation_id", "g" * 257),
            ("generation_id", "\ud800"),
            ("generated_at", "2" * 257),
            ("generated_at", "\ud800"),
            ("generated_at", "not-an-iso-8601-timestamp"),
        )

        for field, invalid in invalid_values:
            with self.subTest(field=field, invalid=ascii(invalid)):
                body = json.loads(manifest_bytes("generation-c"))
                body[field] = invalid
                client = FakeHttpClient(
                    response=HttpResponse(
                        status=200,
                        headers={"ETag": '"generation-c"'},
                        body=json.dumps(body, separators=(",", ":")).encode("utf-8"),
                    )
                )

                result = StockCache(self.cache_root, client).refresh(self.config)

                self.assertEqual(result.status, "stale_cache")
                self.assertEqual(result.generation_id, "generation-a")
                self.assertEqual(result.warning_code, "manifest_invalid")
                self.assertEqual(client.download_calls, [])
                self.assertEqual(
                    (self.cache_root / "current.json").read_bytes(),
                    previous_pointer,
                )

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
