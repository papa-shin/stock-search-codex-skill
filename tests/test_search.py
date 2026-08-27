from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from papa_shin_stock import cache as cache_module
from papa_shin_stock import schema as schema_module
from papa_shin_stock.cache import CacheState, GenerationFiles, StockCache
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.http_client import HttpResponse
from papa_shin_stock.query import SearchQuery, normalize_tire_size
from papa_shin_stock.schema import (
    Offer,
    Product,
    SearchResult,
    SearchSummary,
    StockSearcher,
)
import search_stock


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class StockSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.generation_dir = Path(self.temp_dir.name) / "synthetic-generation"
        self.generation_dir.mkdir()
        for name in ("manifest.json", "products.jsonl", "offers.jsonl"):
            target = self.generation_dir / name
            target.write_bytes((FIXTURES_DIR / name).read_bytes())

        self.files = GenerationFiles.from_directory(
            "synthetic-generation", self.generation_dir
        )
        self.config = StockConfig(
            manifest_url="https://stock.example.test/manifest.json",
            username="synthetic-user",
            password="synthetic-password",
            product_id_field="private_product_key",
            offer_product_id_field="private_offer_product_key",
            cache_dir=Path(self.temp_dir.name) / "cache",
        )
        self.searcher = StockSearcher(self.files, self.config)

    def search(self, **overrides: object):
        values: dict[str, object] = {
            "product_type": None,
            "size": None,
            "season": None,
            "spikes": None,
            "run_flat": None,
            "disk_type": None,
            "truck_axis": None,
            "truck_construction": None,
            "supplier": None,
            "min_total_quantity": 4,
            "max_price": None,
            "max_delivery_days": None,
            "limit": 10,
            "offers_limit": 5,
        }
        values.update(overrides)
        return self.searcher.search(SearchQuery.from_args(argparse.Namespace(**values)))

    def test_size_variants_are_equivalent(self) -> None:
        self.assertEqual(normalize_tire_size("205/55 16"), "205/55R16")
        self.assertEqual(normalize_tire_size("205 55 R16"), "205/55R16")

    def test_size_rejects_unapproved_separators(self) -> None:
        with self.assertRaisesRegex(StockError, "query_invalid"):
            normalize_tire_size("205evil55junk16")

    def test_size_rejects_non_ascii_whitespace(self) -> None:
        for value in ("205\t55R16", "205\v55R16", "205\f55R16", "205\u00a055R16"):
            with self.assertRaisesRegex(StockError, "query_invalid"):
                normalize_tire_size(value)

    def test_tiny_decimal_exponent_is_safe_query_error(self) -> None:
        with self.assertRaisesRegex(StockError, "query_invalid"):
            self.search(max_price="1e-600000")

    def test_search_distinguishes_sku_and_quantity(self) -> None:
        result = self.search(size="205/55R16", season="Лето")

        self.assertEqual(result.summary.sku_count, 2)
        self.assertEqual(result.summary.total_quantity, 24)

    def test_results_sort_by_minimum_price_then_total_quantity(self) -> None:
        result = self.search(size="205/55R16", season="Лето")

        self.assertEqual(
            [product.product_id for product in result.products],
            ["synthetic-summer-b", "synthetic-summer-a"],
        )

    def test_offer_filters_remove_products_without_matching_offer(self) -> None:
        result = self.search(supplier="Synthetic Supplier B")

        self.assertEqual([product.product_id for product in result.products], ["synthetic-summer-a"])
        self.assertEqual(result.summary.sku_count, 1)
        self.assertEqual(result.summary.total_quantity, 12)

    def test_no_results_is_successful_and_preserves_normalized_filters(self) -> None:
        result = self.search(size="195/65R15")

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.products, ())
        self.assertEqual(result.summary.sku_count, 0)
        self.assertEqual(result.filters["size"], "195/65R15")

    def test_unknown_and_missing_characteristics_are_reported_separately(self) -> None:
        result = self.search(size="205/55R16", min_total_quantity=0)

        self.assertEqual(
            result.unknown_characteristics,
            (
                {
                    "product_id": "synthetic-unknown",
                    "characteristic": "spikes",
                    "status": "unknown",
                },
                {
                    "product_id": "synthetic-unknown",
                    "characteristic": "run_flat",
                    "status": "missing",
                },
                {
                    "product_id": "synthetic-unknown",
                    "characteristic": "load_index",
                    "status": "unknown",
                },
                {
                    "product_id": "synthetic-unknown",
                    "characteristic": "speed_index",
                    "status": "missing",
                },
            ),
        )

    def test_unknown_and_missing_size_and_season_are_reported(self) -> None:
        products = self.files.products
        products.write_text(
            products.read_text(encoding="utf-8").replace(
                '"size":"205/55R16","season":"Лето","spikes":{"status":"unknown"}',
                '"size":{"status":"unknown"},"season":{"status":"missing"},"spikes":{"status":"unknown"}',
            ),
            encoding="utf-8",
        )

        result = self.search(min_total_quantity=0)

        self.assertIn(
            {"product_id": "synthetic-unknown", "characteristic": "size", "status": "unknown"},
            result.unknown_characteristics,
        )
        self.assertIn(
            {"product_id": "synthetic-unknown", "characteristic": "season", "status": "missing"},
            result.unknown_characteristics,
        )

    def test_product_generation_mismatch_fails_closed(self) -> None:
        products = self.files.products
        products.write_bytes((FIXTURES_DIR / "products-generation-mismatch.jsonl").read_bytes())

        with self.assertRaisesRegex(StockError, "generation_mismatch"):
            self.search()

    def test_offer_generation_mismatch_fails_closed(self) -> None:
        offers = self.files.offers
        offers.write_text(
            offers.read_text(encoding="utf-8").replace(
                '"synthetic-generation"', '"other-synthetic-generation"', 1
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(StockError, "generation_mismatch"):
            self.search()

    def test_non_finite_json_value_is_rejected_before_public_serialization(self) -> None:
        products = self.files.products
        products.write_text(
            products.read_text(encoding="utf-8").replace('"load_index":"91"', '"load_index":NaN', 1),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            self.search()

    def test_more_than_ten_thousand_products_returns_exact_top_result(self) -> None:
        product = (
            '{"private_product_key":"synthetic-%s","content_generation_id":"synthetic-generation",'
            '"name":"Synthetic","article":"SYN","product_type":"Шины",'
            '"size":"205/55R16","season":"Лето","spikes":"Нет","run_flat":"Нет",'
            '"total_quantity":4,"characteristics":{}}\n'
        )
        self.files.products.write_text(
            "".join(product % index for index in range(10_001)), encoding="utf-8"
        )
        self.files.offers.write_text(
            "".join(
                '{"private_offer_product_key":"synthetic-%s","content_generation_id":"synthetic-generation",'
                '"supplier":"Synthetic","price":"%s","delivery_days":1,"quantity":1}\n'
                % (index, 20_000 - index)
                for index in range(10_001)
            ),
            encoding="utf-8",
        )

        result = self.search(size="205/55R16", season="Лето", limit=1)

        self.assertEqual(result.products[0].product_id, "synthetic-10000")

    def test_duplicate_json_keys_fail_closed(self) -> None:
        self.files.products.write_text(
            '{"private_product_key":"synthetic-a","content_generation_id":"synthetic-generation",'
            '"content_generation_id":"other-generation"}\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            self.search()

    def test_nested_overflow_json_number_fails_closed(self) -> None:
        products = self.files.products
        products.write_text(
            products.read_text(encoding="utf-8").replace('"load_index":"91"', '"load_index":{"bad":1e400}', 1),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            self.search()

    def test_unapproved_large_nested_characteristic_is_not_public(self) -> None:
        products = self.files.products
        products.write_text(
            products.read_text(encoding="utf-8").replace(
                '"characteristics":{"load_index":"91","speed_index":"V"}',
                '"characteristics":{"load_index":"91","private_nested":{"payload":"' + "x" * 1_100_000 + '"}}',
                1,
            ),
            encoding="utf-8",
        )

        public = self.search(size="205/55R16", season="Лето").to_public_dict()
        serialized = json.dumps(public, ensure_ascii=False, separators=(",", ":"))

        self.assertNotIn("private_nested", serialized)
        self.assertLessEqual(len(serialized.encode("utf-8")), 512 * 1024)

    def test_offer_tie_prefers_higher_quantity(self) -> None:
        offers = self.files.offers
        offers.write_text(
            offers.read_text(encoding="utf-8")
            + '{"private_offer_product_key":"synthetic-summer-b","content_generation_id":"synthetic-generation","supplier":"Low","price":"6000","delivery_days":4,"quantity":1}\n'
            + '{"private_offer_product_key":"synthetic-summer-b","content_generation_id":"synthetic-generation","supplier":"High","price":"6000","delivery_days":4,"quantity":99}\n',
            encoding="utf-8",
        )

        public = self.search(size="205/55R16", season="Лето").to_public_dict()
        product = next(item for item in public["products"] if item["product_id"] == "synthetic-summer-b")

        self.assertEqual(product["offers"][0]["supplier"], "High")

    def test_persisted_stale_status_is_returned_by_later_search(self) -> None:
        (self.generation_dir / "state.json").write_text(
            json.dumps(
                {
                    "generation_id": "synthetic-generation",
                    "checked_at": "2026-08-27T10:01:00+00:00",
                    "stale": True,
                    "warning_code": "network_error",
                }
            ),
            encoding="utf-8",
        )

        public = self.search().to_public_dict()

        self.assertTrue(public["generation"]["stale"])
        self.assertEqual(public["warnings"][0]["code"], "network_error")

    def test_state_rejects_falsey_malformed_and_inconsistent_values(self) -> None:
        state_path = self.generation_dir / "state.json"
        invalid_values = (
            "[]",
            "{}",
            "false",
            "{malformed",
            json.dumps(
                {
                    "generation_id": "synthetic-generation",
                    "checked_at": "2026-08-27T10:01:00+00:00",
                    "stale": True,
                    "warning_code": None,
                }
            ),
            json.dumps(
                {
                    "generation_id": "synthetic-generation",
                    "checked_at": "2026-08-27T10:01:00+00:00",
                    "stale": True,
                    "warning_code": "",
                }
            ),
            json.dumps(
                {
                    "generation_id": "synthetic-generation",
                    "checked_at": "2026-08-27T10:01:00+00:00",
                    "stale": False,
                    "warning_code": "network_error",
                }
            ),
        )

        for value in invalid_values:
            with self.subTest(value=value[:40]):
                state_path.write_text(value, encoding="utf-8")
                with self.assertRaisesRegex(StockError, "cache_unavailable"):
                    self.search()

    def test_recursive_state_is_normalized_to_safe_cache_error(self) -> None:
        nested = "[" * 2_000 + "0" + "]" * 2_000
        (self.generation_dir / "state.json").write_text(
            '{"generation_id":"synthetic-generation",'
            '"checked_at":"2026-08-27T10:01:00+00:00",'
            '"stale":false,"nested":' + nested + "}",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(StockError, "cache_unavailable") as raised:
            self.search()

        self.assertEqual(raised.exception.safe_message, "Проверенный кэш недоступен")

    def test_state_parser_normalizes_overflow_error(self) -> None:
        (self.generation_dir / "state.json").write_text(
            json.dumps(
                {
                    "generation_id": "synthetic-generation",
                    "checked_at": "2026-08-27T10:01:00+00:00",
                    "stale": False,
                    "warning_code": None,
                }
            ),
            encoding="utf-8",
        )
        with patch.object(
            cache_module,
            "_parse_json",
            side_effect=OverflowError("synthetic internal path"),
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable") as raised:
                self.search()

        self.assertNotIn("internal path", str(raised.exception))

    def test_output_is_bounded_and_uses_only_neutral_product_id(self) -> None:
        result = self.search(size="205/55R16", season="Лето", limit=2, offers_limit=3)
        public = result.to_public_dict()
        serialized = json.dumps(public, ensure_ascii=False)

        self.assertEqual(len(public["products"]), 2)
        summer_a = next(
            product for product in public["products"]
            if product["product_id"] == "synthetic-summer-a"
        )
        self.assertEqual(len(summer_a["offers"]), 3)
        self.assertEqual(set(summer_a), {
            "product_id", "name", "article", "product_type", "characteristics",
            "total_quantity", "minimum_price", "offers",
        })
        self.assertNotIn("private_product_key", serialized)
        self.assertNotIn("private_offer_product_key", serialized)
        self.assertNotIn("source_note", serialized)

    def test_actual_boundary_removes_previous_tail_before_truncation_warning(self) -> None:
        maximum = 512 * 1024
        generation = {
            "id": "synthetic-generation",
            "generated_at": "2026-08-27T10:00:00+00:00",
            "checked_at": "2026-08-27T10:01:00+00:00",
            "stale": True,
        }
        initial_warnings = (
            {
                "code": "network_error",
                "message": "Используется предыдущее поколение",
            },
        )

        def make_product(
            index: int, supplier_length: int, offer_count: int, name_length: int = 256
        ) -> tuple[Product, tuple[Offer, ...]]:
            product_id = f"synthetic-boundary-{index}"
            product = Product(
                product_id=product_id,
                name="N" * name_length,
                article="A" * 256,
                product_type="T" * 256,
                characteristics={"load_index": "9" * 256, "speed_index": "V" * 256},
                total_quantity=4,
                unknown_characteristics=(
                    {
                        "product_id": product_id,
                        "characteristic": "speed_index",
                        "status": "unknown",
                    },
                ),
            )
            offers = tuple(
                Offer(
                    supplier="S" * supplier_length,
                    price=Decimal("12345.67"),
                    delivery_days=3,
                    quantity=4,
                )
                for _ in range(offer_count)
            )
            return product, offers

        products: list[Product] = []
        offers: dict[str, tuple[Offer, ...]] = {}
        while True:
            product, product_offers = make_product(len(products), 256, 25)
            candidate_products = products + [product]
            candidate_offers = {**offers, product.product_id: product_offers}
            candidate = SearchResult(
                generation,
                {},
                SearchSummary(999, 3_996),
                tuple(candidate_products),
                candidate_offers,
                initial_warnings,
            )
            public = {
                "status": candidate.status,
                "generation": generation,
                "filters": {},
                "summary": candidate.summary.to_public_dict(),
                "products": [
                    item.to_public_dict(candidate_offers[item.product_id])
                    for item in candidate_products
                ],
                "unknown_characteristics": [
                    unknown
                    for item in candidate_products
                    for unknown in item.unknown_characteristics
                ],
                "warnings": list(initial_warnings),
            }
            if len(json.dumps(public, ensure_ascii=False, separators=(",", ":")).encode()) > maximum:
                break
            products = candidate_products
            offers = candidate_offers

        self.assertTrue(products)
        full_summary = SearchSummary(len(products) + 2, (len(products) + 2) * 4)
        base_public = {
            "status": "ok",
            "generation": generation,
            "filters": {},
            "summary": full_summary.to_public_dict(),
            "products": [
                item.to_public_dict(offers[item.product_id]) for item in products
            ],
            "unknown_characteristics": [
                unknown for item in products for unknown in item.unknown_characteristics
            ],
            "warnings": list(initial_warnings),
        }
        base_size = len(
            json.dumps(base_public, ensure_ascii=False, separators=(",", ":")).encode()
        )
        warning_increment = len(
            json.dumps(
                {"code": "output_truncated", "message": "Вывод ограничен"},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ) + 1
        gap = maximum - base_size
        tuned: tuple[Product, tuple[Offer, ...]] | None = None
        for offer_count in range(1, 26):
            for supplier_length in range(1, 257):
                product, product_offers = make_product(
                    len(products), supplier_length, offer_count, 1
                )
                product_increment = len(
                    json.dumps(
                        product.to_public_dict(product_offers),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ) + 1
                unknown_increment = len(
                    json.dumps(
                        product.unknown_characteristics[0],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ) + 1
                for name_length in range(1, 257):
                    increment = product_increment + unknown_increment + name_length - 1
                    if increment <= gap and gap - increment < warning_increment:
                        tuned = make_product(
                            len(products), supplier_length, offer_count, name_length
                        )
                        break
                if tuned is not None:
                    break
            if tuned is not None:
                break

        self.assertIsNotNone(tuned, "synthetic boundary must be tunable")
        tail, tail_offers = tuned
        products.append(tail)
        offers[tail.product_id] = tail_offers
        overflowing, overflowing_offers = make_product(len(products), 256, 25)
        products.append(overflowing)
        offers[overflowing.product_id] = overflowing_offers
        result = SearchResult(
            generation,
            {},
            full_summary,
            tuple(products),
            offers,
            initial_warnings,
        )

        public = result.to_public_dict()
        serialized = json.dumps(public, ensure_ascii=False, separators=(",", ":"))
        returned_ids = {item["product_id"] for item in public["products"]}

        self.assertLessEqual(len(serialized.encode("utf-8")), maximum)
        self.assertEqual(public["summary"]["sku_count"], len(products))
        self.assertEqual(public["warnings"][-1]["code"], "output_truncated")
        self.assertNotIn(tail.product_id, returned_ids)
        self.assertNotIn(overflowing.product_id, returned_ids)
        self.assertTrue(
            all(
                unknown["product_id"] in returned_ids
                for unknown in public["unknown_characteristics"]
            )
        )


class SearchFreshnessIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cache_root = Path(self.temp_dir.name) / "cache"
        self.generation = self.cache_root / "generations" / "generation-existing"
        self.generation.mkdir(parents=True)
        products = (FIXTURES_DIR / "products.jsonl").read_bytes()
        offers = (FIXTURES_DIR / "offers.jsonl").read_bytes()
        manifest = {
            "generation_id": "synthetic-generation",
            "generated_at": "2026-08-27T10:00:00+00:00",
            "files": {
                "products": {
                    "url": "products.jsonl",
                    "bytes": len(products),
                    "sha256": hashlib.sha256(products).hexdigest(),
                },
                "offers": {
                    "url": "offers.jsonl",
                    "bytes": len(offers),
                    "sha256": hashlib.sha256(offers).hexdigest(),
                },
            },
        }
        (self.generation / "manifest.json").write_text(
            json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
        )
        (self.generation / "products.jsonl").write_bytes(products)
        (self.generation / "offers.jsonl").write_bytes(offers)
        (self.generation / "state.json").write_text(
            json.dumps(
                {
                    "generation_id": "synthetic-generation",
                    "generated_at": "2026-08-27T10:00:00+00:00",
                    "checked_at": "2026-08-27T10:01:00+00:00",
                    "manifest_etag": '"synthetic-generation"',
                    "manifest_last_modified": "Thu, 27 Aug 2026 10:00:00 GMT",
                    "stale": False,
                    "warning_code": None,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        (self.cache_root / "current.json").write_text(
            json.dumps(
                {
                    "generation_id": "synthetic-generation",
                    "directory_name": "generation-existing",
                }
            ),
            encoding="utf-8",
        )
        self.config = StockConfig(
            manifest_url="https://stock.example.test/manifest.json",
            username="synthetic-user",
            password="synthetic-password",
            product_id_field="private_product_key",
            offer_product_id_field="private_offer_product_key",
            cache_dir=self.cache_root,
        )

    def public_search(self) -> dict[str, object]:
        files = StockCache(self.cache_root, object()).current_generation()
        query = SearchQuery.from_args(argparse.Namespace())
        return StockSearcher(files, self.config).search(query).to_public_dict()

    def test_failure_then_304_is_visible_to_search_with_read_only_generation(self) -> None:
        class FailingClient:
            def get_manifest(self, etag: str | None, modified: str | None) -> HttpResponse:
                raise StockError("network_error", "Не удалось обновить данные", 3)

        class NotModifiedClient:
            def get_manifest(self, etag: str | None, modified: str | None) -> HttpResponse:
                return HttpResponse(status=304, headers={}, body=b"")

        immutable_state = (self.generation / "state.json").read_bytes()
        real_write = cache_module._write_json_atomic

        def reject_generation_write(path: Path, value: object) -> None:
            if self.generation == path.parent:
                raise PermissionError("/private/generation/state.json")
            real_write(path, value)

        with patch.object(
            cache_module, "_write_json_atomic", side_effect=reject_generation_write
        ):
            stale_result = StockCache(self.cache_root, FailingClient()).refresh(
                self.config
            )
            stale_public = self.public_search()
            fresh_result = StockCache(self.cache_root, NotModifiedClient()).refresh(
                self.config
            )
            fresh_public = self.public_search()

        self.assertEqual(stale_result.status, "stale_cache")
        self.assertTrue(stale_public["generation"]["stale"])
        self.assertEqual(stale_public["warnings"][0]["code"], "network_error")
        self.assertEqual(fresh_result.status, "not_modified")
        self.assertFalse(fresh_public["generation"]["stale"])
        self.assertEqual(fresh_public["warnings"], [])
        self.assertEqual((self.generation / "state.json").read_bytes(), immutable_state)

    def test_runtime_status_write_failure_is_safe(self) -> None:
        state = CacheState.load(self.cache_root)
        self.assertIsNotNone(state)
        cache = StockCache(self.cache_root, object())

        with patch.object(
            cache_module,
            "_write_json_atomic",
            side_effect=PermissionError("/private/cache/runtime/status.json"),
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable") as raised:
                cache._record_runtime_status(state, True, "network_error")

        self.assertNotIn("/private/", str(raised.exception))

    def test_runtime_status_read_failure_is_safe(self) -> None:
        runtime = self.cache_root / "runtime" / "generation-existing.json"
        runtime.parent.mkdir()
        runtime.write_text(
            json.dumps(
                {
                    "generation_id": "synthetic-generation",
                    "checked_at": "2026-08-27T10:02:00+00:00",
                    "stale": True,
                    "warning_code": "network_error",
                }
            ),
            encoding="utf-8",
        )
        real_read_text = Path.read_text

        def fail_runtime_read(path: Path, *args: object, **kwargs: object) -> str:
            if path == runtime:
                raise PermissionError("/private/cache/runtime/status.json")
            return real_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", fail_runtime_read):
            with self.assertRaisesRegex(StockError, "cache_unavailable") as raised:
                CacheState.load(self.cache_root)

        self.assertNotIn("/private/", str(raised.exception))

    def test_cache_and_search_share_strict_runtime_status_validation(self) -> None:
        runtime = self.cache_root / "runtime" / "generation-existing.json"
        runtime.parent.mkdir()
        invalid_values = (
            "[]",
            "{}",
            "false",
            "{malformed",
            json.dumps(
                {
                    "generation_id": "synthetic-generation",
                    "checked_at": "2026-08-27T10:02:00+00:00",
                    "stale": True,
                    "warning_code": "",
                }
            ),
            json.dumps(
                {
                    "generation_id": "synthetic-generation",
                    "checked_at": "2026-08-27T10:02:00+00:00",
                    "stale": False,
                    "warning_code": "network_error",
                }
            ),
            '{"generation_id":"synthetic-generation",'
            '"checked_at":"2026-08-27T10:02:00+00:00",'
            '"stale":false,"nested":' + "[" * 2_000 + "0" + "]" * 2_000 + "}",
            '{"generation_id":"synthetic-generation",'
            '"checked_at":"2026-08-27T10:02:00+00:00",'
            '"stale":false,"nested":1e400}',
        )

        for value in invalid_values:
            with self.subTest(value=value[:40]):
                runtime.write_text(value, encoding="utf-8")
                with self.assertRaisesRegex(StockError, "cache_unavailable"):
                    CacheState.load(self.cache_root)


class SqliteFailureNormalizationTest(unittest.TestCase):
    class ConnectionProxy:
        def __init__(
            self,
            connection: sqlite3.Connection,
            *,
            fail_schema: bool = False,
            fail_query: bool = False,
            fail_close: bool = False,
        ) -> None:
            self.connection = connection
            self.fail_schema = fail_schema
            self.fail_query = fail_query
            self.fail_close = fail_close

        def create_collation(self, *args: object) -> None:
            self.connection.create_collation(*args)

        def executescript(self, script: str) -> object:
            if self.fail_schema:
                raise sqlite3.OperationalError("/private/schema failure")
            return self.connection.executescript(script)

        def execute(self, sql: str, parameters: object = ()) -> object:
            if self.fail_query and sql.startswith("INSERT INTO c"):
                raise sqlite3.OperationalError("/private/query failure")
            return self.connection.execute(sql, parameters)

        def close(self) -> None:
            self.connection.close()
            if self.fail_close:
                raise sqlite3.OperationalError("/private/close failure")

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.generation_dir = Path(self.temp_dir.name) / "synthetic-generation"
        self.generation_dir.mkdir()
        for name in ("manifest.json", "products.jsonl", "offers.jsonl"):
            (self.generation_dir / name).write_bytes((FIXTURES_DIR / name).read_bytes())
        self.files = GenerationFiles.from_directory(
            "synthetic-generation", self.generation_dir
        )
        self.config = StockConfig(
            manifest_url="https://stock.example.test/manifest.json",
            username="synthetic-user",
            password="synthetic-password",
            product_id_field="private_product_key",
            offer_product_id_field="private_offer_product_key",
            cache_dir=Path(self.temp_dir.name) / "cache",
        )

    def search(self):
        namespace = argparse.Namespace(
            product_type=None,
            size=None,
            season=None,
            spikes=None,
            run_flat=None,
            disk_type=None,
            truck_axis=None,
            truck_construction=None,
            supplier=None,
            min_total_quantity=4,
            max_price=None,
            max_delivery_days=None,
            limit=10,
            offers_limit=5,
        )
        return StockSearcher(self.files, self.config).search(
            SearchQuery.from_args(namespace)
        )

    def run_with_proxy(self, **failures: bool) -> tuple[StockError, list[Path]]:
        real_connect = sqlite3.connect
        directories: list[Path] = []

        def connect(path: str) -> SqliteFailureNormalizationTest.ConnectionProxy:
            directories.append(Path(path).parent)
            return self.ConnectionProxy(real_connect(path), **failures)

        with patch.object(schema_module.sqlite3, "connect", side_effect=connect):
            with self.assertRaises(StockError) as raised:
                self.search()
        return raised.exception, directories

    def test_connect_failure_is_safe_and_removes_temp_directory(self) -> None:
        directories: list[Path] = []
        real_temporary_directory = tempfile.TemporaryDirectory

        def temporary_directory(*args: object, **kwargs: object):
            value = real_temporary_directory(*args, **kwargs)
            directories.append(Path(value.name))
            return value

        with patch.object(
            schema_module.tempfile,
            "TemporaryDirectory",
            side_effect=temporary_directory,
        ):
            with patch.object(
                schema_module.sqlite3,
                "connect",
                side_effect=sqlite3.OperationalError("/private/connect failure"),
            ):
                with self.assertRaisesRegex(StockError, "cache_unavailable") as raised:
                    self.search()

        self.assertNotIn("/private/", str(raised.exception))
        self.assertTrue(directories)
        self.assertFalse(directories[0].exists())

    def test_schema_and_query_failures_are_safe_and_remove_temp_directory(self) -> None:
        for failure in ("fail_schema", "fail_query"):
            with self.subTest(failure=failure):
                error, directories = self.run_with_proxy(**{failure: True})
                self.assertEqual(error.code, "cache_unavailable")
                self.assertNotIn("/private/", str(error))
                self.assertTrue(directories)
                self.assertFalse(directories[0].exists())

    def test_filesystem_failure_is_safe(self) -> None:
        with patch.object(
            schema_module.tempfile,
            "TemporaryDirectory",
            side_effect=PermissionError("/private/temp root"),
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable") as raised:
                self.search()

        self.assertNotIn("/private/", str(raised.exception))

    def test_sqlite_integer_overflow_is_safe_and_removes_temp_directory(self) -> None:
        products = self.files.products
        products.write_text(
            products.read_text(encoding="utf-8").replace(
                '"total_quantity":12', '"total_quantity":' + "9" * 100, 1
            ),
            encoding="utf-8",
        )
        real_temporary_directory = tempfile.TemporaryDirectory
        directories: list[Path] = []

        def temporary_directory(*args: object, **kwargs: object):
            value = real_temporary_directory(*args, **kwargs)
            directories.append(Path(value.name))
            return value

        with patch.object(
            schema_module.tempfile,
            "TemporaryDirectory",
            side_effect=temporary_directory,
        ):
            with self.assertRaises(StockError) as raised:
                self.search()

        self.assertIn(raised.exception.code, {"manifest_invalid", "cache_unavailable"})
        self.assertTrue(directories)
        self.assertFalse(directories[0].exists())

    def test_close_failure_is_safe_and_cleanup_does_not_mask_primary_error(self) -> None:
        close_error, directories = self.run_with_proxy(fail_close=True)

        self.assertEqual(close_error.code, "cache_unavailable")
        self.assertNotIn("/private/", str(close_error))
        self.assertFalse(directories[0].exists())

        self.files.products.write_bytes(
            (FIXTURES_DIR / "products-generation-mismatch.jsonl").read_bytes()
        )
        primary_error, directories = self.run_with_proxy(fail_close=True)

        self.assertEqual(primary_error.code, "generation_mismatch")
        self.assertFalse(directories[0].exists())


class SearchStockCliTest(unittest.TestCase):
    def test_success_writes_one_public_json_document(self) -> None:
        output = StringIO()
        public_result = {
            "status": "ok",
            "generation": {"id": "synthetic-generation"},
            "filters": {},
            "summary": {"sku_count": 0, "total_quantity": 0},
            "products": [],
            "unknown_characteristics": [],
            "warnings": [],
        }

        with patch.object(search_stock, "search_default", return_value=public_result):
            with redirect_stdout(output):
                exit_code = search_stock.main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), public_result)

    def test_invalid_argument_writes_safe_json_envelope(self) -> None:
        output = StringIO()

        with redirect_stdout(output):
            exit_code = search_stock.main(["--limit", "nope"])

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["error"]["code"], "query_invalid")


if __name__ == "__main__":
    unittest.main()
