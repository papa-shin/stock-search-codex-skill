from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from papa_shin_stock.cache import CacheState, StockCache
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.http_client import DownloadReceipt, HttpResponse


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
    ) -> DownloadReceipt:
        if destination.name == "products.jsonl":
            payload = b"corrupt" if self.corrupt_products else PRODUCTS
        else:
            payload = OFFERS
            if self.interrupt_offers:
                destination.write_bytes(payload[:5])
                raise StockError("network_error", "Синтетический обрыв загрузки", 3)

        destination.write_bytes(payload)
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

    def test_active_lock_without_cache_is_cache_locked_error(self) -> None:
        lock = self.cache_root / ".refresh.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(
            json.dumps({"token": "active-writer", "created_at": time.time()}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(StockError, "cache_locked"):
            StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

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

    def test_success_removes_inactive_generation_after_activation(self) -> None:
        self.fixture.seed_generation()

        StockCache(self.cache_root, FakeHttpClient()).refresh(self.config)

        active = StockCache(self.cache_root, FakeHttpClient()).current_generation()
        self.assertEqual(active.generation_id, "generation-b")
        self.assertEqual(
            [path for path in (self.cache_root / "generations").iterdir() if path.is_dir()],
            [active.manifest.parent],
        )

    def test_cache_state_rejects_pointer_to_incomplete_generation(self) -> None:
        self.fixture.seed_generation()
        StockCache(self.cache_root, FakeHttpClient()).current_generation().offers.unlink()

        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            CacheState.load(self.cache_root)

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


if __name__ == "__main__":
    unittest.main()
