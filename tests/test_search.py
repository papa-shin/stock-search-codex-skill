from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
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

    def test_source_integer_fields_accept_only_canonical_nonnegative_integers(self) -> None:
        accepted = ((0, 0), (7, 7), ("0", 0), ("7", 7), ("+7", 7))

        for field in ("total_quantity", "delivery_days", "quantity"):
            for raw, expected in accepted:
                with self.subTest(field=field, raw=raw):
                    if field == "total_quantity":
                        product = schema_module._product(
                            {
                                "name": "Synthetic",
                                "article": "SYN",
                                "product_type": "Шины",
                                "total_quantity": raw,
                                "characteristics": {},
                            },
                            "synthetic-product",
                        )
                        actual = product.total_quantity
                    else:
                        row = {
                            "supplier": "Synthetic",
                            "price": "1",
                            "delivery_days": 0,
                            "quantity": 0,
                            field: raw,
                        }
                        offer = schema_module._offer(row)
                        actual = getattr(offer, field)

                    self.assertEqual(actual, expected)

    def test_source_integer_fields_reject_noncanonical_or_noninteger_values(self) -> None:
        invalid = (
            True,
            False,
            1.0,
            1.5,
            Decimal("1"),
            Decimal("1.5"),
            "",
            "01",
            "+01",
            "-0",
            "-1",
            " 1",
            "1 ",
            "1.0",
            "1e0",
            "--1",
        )

        for field in ("total_quantity", "delivery_days", "quantity"):
            for raw in invalid:
                with self.subTest(field=field, raw=repr(raw)):
                    with self.assertRaisesRegex(StockError, "manifest_invalid"):
                        if field == "total_quantity":
                            schema_module._product(
                                {
                                    "name": "Synthetic",
                                    "article": "SYN",
                                    "product_type": "Шины",
                                    "total_quantity": raw,
                                    "characteristics": {},
                                },
                                "synthetic-product",
                            )
                        else:
                            schema_module._offer(
                                {
                                    "supplier": "Synthetic",
                                    "price": "1",
                                    "delivery_days": 0,
                                    "quantity": 0,
                                    field: raw,
                                }
                            )

    def test_jsonl_reader_accepts_record_at_byte_limit(self) -> None:
        record = b'{"a":1}'
        path = self.generation_dir / "boundary.jsonl"
        path.write_bytes(record + b"\r\n")

        with patch.object(schema_module, "MAX_JSONL_LINE_BYTES", len(record)):
            self.assertEqual(list(schema_module._rows(path, 1)), [{"a": 1}])

    def test_jsonl_reader_rejects_record_above_byte_limit_without_full_read(self) -> None:
        record = b'{"a":1}'
        path = self.generation_dir / "oversized.jsonl"
        path.write_bytes(record + b"x" * 1_000_000)

        with patch.object(schema_module, "MAX_JSONL_LINE_BYTES", len(record) - 1):
            with self.assertRaisesRegex(StockError, "manifest_invalid"):
                list(schema_module._rows(path, 1))

    def test_product_and_offer_row_limits_accept_boundary_and_reject_overflow(self) -> None:
        with patch.object(schema_module, "MAX_PRODUCT_ROWS", 4):
            with patch.object(schema_module, "MAX_OFFER_ROWS", 9):
                self.search()

        for constant, value in (("MAX_PRODUCT_ROWS", 3), ("MAX_OFFER_ROWS", 8)):
            with self.subTest(constant=constant):
                with patch.object(schema_module, constant, value):
                    with self.assertRaisesRegex(StockError, "manifest_invalid"):
                        self.search()

    def test_spool_record_limit_accepts_boundary_and_rejects_overflow(self) -> None:
        with patch.object(schema_module, "MAX_SPOOL_RECORDS", 10):
            self.search()

        with patch.object(schema_module, "MAX_SPOOL_RECORDS", 9):
            with self.assertRaisesRegex(StockError, "cache_unavailable") as raised:
                self.search()

        self.assertEqual(
            raised.exception.safe_message,
            "Временное хранилище поиска недоступно",
        )

    def test_spool_page_limit_rejects_growth_without_large_file(self) -> None:
        small = Product("small", "N", "A", "T", {}, 1, ())
        oversized = Product("x" * (128 * 1024), "N", "A", "T", {}, 1, ())

        with patch.object(schema_module, "MAX_SPOOL_BYTES", 16 * 4096):
            with schema_module._Spool() as spool:
                spool.add_product(small)
                with self.assertRaisesRegex(
                    StockError, "cache_unavailable"
                ) as raised:
                    spool.add_product(oversized)

        self.assertNotIn(str(self.generation_dir), str(raised.exception))

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

    def runtime_status_path(self) -> Path:
        return self.cache_root / ".runtime-status-generation-existing.json"

    def test_failure_then_304_is_visible_to_search_with_read_only_generation(self) -> None:
        class FailingClient:
            def get_manifest(
                self, etag: str | None, modified: str | None
            ) -> HttpResponse:
                raise StockError("network_error", "Не удалось обновить данные", 3)

        class NotModifiedClient:
            def get_manifest(
                self, etag: str | None, modified: str | None
            ) -> HttpResponse:
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

    def test_failure_then_304_then_failure_keeps_latest_checked_at(self) -> None:
        class FailingClient:
            def get_manifest(self, etag: str | None, modified: str | None) -> HttpResponse:
                raise StockError("network_error", "Не удалось обновить данные", 3)

        class NotModifiedClient:
            def get_manifest(self, etag: str | None, modified: str | None) -> HttpResponse:
                return HttpResponse(status=304, headers={}, body=b"")

        initial_checked_at = self.public_search()["generation"]["checked_at"]
        StockCache(self.cache_root, FailingClient()).refresh(self.config)
        first_stale = self.public_search()
        StockCache(self.cache_root, NotModifiedClient()).refresh(self.config)
        fresh = self.public_search()
        StockCache(self.cache_root, FailingClient()).refresh(self.config)
        second_stale = self.public_search()

        self.assertEqual(first_stale["generation"]["checked_at"], initial_checked_at)
        self.assertNotEqual(fresh["generation"]["checked_at"], initial_checked_at)
        self.assertFalse(fresh["generation"]["stale"])
        self.assertEqual(
            second_stale["generation"]["checked_at"],
            fresh["generation"]["checked_at"],
        )
        self.assertTrue(second_stale["generation"]["stale"])

    def test_delayed_failure_cannot_overwrite_later_same_generation_304(self) -> None:
        entered_fallback = threading.Event()
        allow_fallback = threading.Event()
        real_datetime = cache_module.datetime

        class FixedDateTime:
            @classmethod
            def now(cls, tz: object) -> object:
                return real_datetime.fromisoformat("2026-08-27T10:01:00+00:00")

        class FailingClient:
            def get_manifest(self, etag: str | None, modified: str | None) -> HttpResponse:
                raise StockError("network_error", "Не удалось обновить данные", 3)

        class NotModifiedClient:
            def get_manifest(self, etag: str | None, modified: str | None) -> HttpResponse:
                return HttpResponse(status=304, headers={}, body=b"")

        class DelayedFallbackCache(StockCache):
            def _stale_fallback(
                self, state: CacheState, warning_code: str, *args: object
            ) -> object:
                entered_fallback.set()
                if not allow_fallback.wait(timeout=5):
                    raise AssertionError("fallback release timeout")
                return super()._stale_fallback(state, warning_code, *args)

        first_results: list[object] = []
        first_errors: list[BaseException] = []

        def run_first_refresh() -> None:
            try:
                first_results.append(
                    DelayedFallbackCache(self.cache_root, FailingClient()).refresh(
                        self.config
                    )
                )
            except BaseException as error:
                first_errors.append(error)

        first = threading.Thread(target=run_first_refresh)
        with patch.object(cache_module, "datetime", FixedDateTime):
            first.start()
            self.assertTrue(entered_fallback.wait(timeout=5))

            second = StockCache(self.cache_root, NotModifiedClient()).refresh(self.config)
            allow_fallback.set()
            first.join(timeout=5)
            if second.warning_code == "cache_locked":
                second = StockCache(self.cache_root, NotModifiedClient()).refresh(
                    self.config
                )

        self.assertFalse(first.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual(len(first_results), 1)

        final = self.public_search()
        self.assertEqual(second.status, "not_modified")
        self.assertFalse(final["generation"]["stale"])
        self.assertEqual(final["warnings"], [])

    def test_runtime_status_swap_to_symlink_cannot_read_outside_cache_root(self) -> None:
        outside = Path(self.temp_dir.name) / "outside-runtime.json"
        outside.write_text(
            json.dumps(
                {
                    "generation_id": "synthetic-generation",
                    "checked_at": "2026-08-27T11:00:00+00:00",
                    "stale": True,
                    "warning_code": "network_error",
                }
            ),
            encoding="utf-8",
        )

        runtime = self.runtime_status_path()
        runtime.write_text(outside.read_text(encoding="utf-8"), encoding="utf-8")
        original = runtime.with_suffix(".original")
        real_open = cache_module.os.open

        def swap_before_open(path: object, flags: int, *args: object) -> int:
            if Path(path) == runtime:
                runtime.rename(original)
                runtime.symlink_to(outside)
            return real_open(path, flags, *args)

        with patch.object(cache_module.os, "open", swap_before_open):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                CacheState.load(self.cache_root)

        self.assertIn("2026-08-27T11:00:00+00:00", outside.read_text(encoding="utf-8"))

    def test_runtime_status_swap_to_symlink_fails_closed_on_write(self) -> None:
        state = CacheState.load(self.cache_root)
        self.assertIsNotNone(state)
        outside = Path(self.temp_dir.name) / "outside-runtime.json"
        outside.write_text("sentinel", encoding="utf-8")
        runtime = self.runtime_status_path()
        runtime.write_text(
            json.dumps(
                {
                    "generation_id": "synthetic-generation",
                    "checked_at": state.checked_at,
                    "stale": False,
                    "warning_code": None,
                }
            ),
            encoding="utf-8",
        )
        original = runtime.with_suffix(".original")
        real_write = cache_module._write_json_atomic

        def swap_before_write(path: Path, value: object) -> None:
            if path == runtime:
                runtime.rename(original)
                runtime.symlink_to(outside)
            real_write(path, value)

        with patch.object(cache_module, "_write_json_atomic", swap_before_write):
            with cache_module.CacheLock.acquire(self.cache_root) as lock:
                with self.assertRaisesRegex(StockError, "cache_unavailable"):
                    StockCache(self.cache_root, object())._record_runtime_status(
                        state, True, "network_error", lock
                    )

        self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel")
        self.assertTrue(runtime.is_file())
        self.assertTrue(runtime.is_symlink())

    def test_aged_writer_heartbeats_before_runtime_status_write(self) -> None:
        state = CacheState.load(self.cache_root)
        self.assertIsNotNone(state)
        cache = StockCache(self.cache_root, object())
        real_write = cache_module._write_runtime_status_atomic

        with cache_module.CacheLock.acquire(self.cache_root) as lock:
            owner = lock.path / "owner.json"
            owner.write_text(
                json.dumps(
                    {
                        "token": lock.token,
                        "created_at": cache_module.time.time()
                        - cache_module._LOCK_TTL_SECONDS
                        - 1,
                    }
                ),
                encoding="utf-8",
            )
            heartbeat = lock.path / f"heartbeat-{lock.token}"
            expired = (
                cache_module.time.time()
                - cache_module._LOCK_TTL_SECONDS
                - 1
            )
            cache_module.os.utime(heartbeat, (expired, expired))

            def assert_fresh_lease_then_write(
                root: Path, directory_name: str, value: object
            ) -> None:
                owner_state = cache_module.CacheLock._read_owner(lock.path)
                self.assertIsNotNone(owner_state)
                self.assertLess(cache_module.time.time() - owner_state[1], 5)
                real_write(root, directory_name, value)

            with patch.object(
                cache_module,
                "_write_runtime_status_atomic",
                side_effect=assert_fresh_lease_then_write,
            ):
                updated = cache._record_runtime_status(
                    state, True, "network_error", lock
                )

        self.assertTrue(updated.stale)
        self.assertEqual(updated.warning_code, "network_error")

    def test_reclaimed_writer_before_commit_lock_cannot_overwrite_later_commit(
        self,
    ) -> None:
        state = CacheState.load(self.cache_root)
        self.assertIsNotNone(state)
        delayed_lock = cache_module.CacheLock.acquire(self.cache_root)
        self.addCleanup(delayed_lock.release)
        delayed_at_commit = threading.Event()
        allow_delayed_commit = threading.Event()
        delayed_errors: list[StockError] = []
        real_commit_acquire = cache_module.RuntimeCommitLock.acquire

        def pause_delayed_commit(root: Path) -> object:
            if threading.current_thread().name == "delayed-runtime-writer":
                delayed_at_commit.set()
                if not allow_delayed_commit.wait(timeout=5):
                    raise AssertionError("delayed commit timeout")
            return real_commit_acquire(root)

        def run_delayed() -> None:
            try:
                StockCache(self.cache_root, object())._record_runtime_status(
                    state, True, "network_error", delayed_lock
                )
            except StockError as error:
                delayed_errors.append(error)

        with patch.object(
            cache_module.RuntimeCommitLock,
            "acquire",
            side_effect=pause_delayed_commit,
        ):
            delayed = threading.Thread(
                target=run_delayed, name="delayed-runtime-writer"
            )
            delayed.start()
            self.assertTrue(delayed_at_commit.wait(timeout=5))
            expired = (
                cache_module.time.time()
                - cache_module._LOCK_TTL_SECONDS
                - 1
            )
            owner = delayed_lock.path / "owner.json"
            owner.write_text(
                json.dumps({"token": delayed_lock.token, "created_at": expired}),
                encoding="utf-8",
            )
            cache_module.os.utime(
                delayed_lock.path / f"heartbeat-{delayed_lock.token}",
                (expired, expired),
            )

            with cache_module.CacheLock.acquire(self.cache_root) as current_lock:
                current = CacheState.load(self.cache_root)
                self.assertIsNotNone(current)
                committed = StockCache(
                    self.cache_root, object()
                )._record_runtime_status(current, False, None, current_lock)

            allow_delayed_commit.set()
            delayed.join(timeout=5)

        self.assertFalse(delayed.is_alive())
        self.assertEqual([error.code for error in delayed_errors], ["cache_locked"])
        final = CacheState.load(self.cache_root)
        self.assertIsNotNone(final)
        self.assertEqual(final.runtime_revision, committed.runtime_revision)
        self.assertFalse(final.stale)

    def test_writer_holding_commit_lock_orders_later_reclaimer_after_it(self) -> None:
        state = CacheState.load(self.cache_root)
        self.assertIsNotNone(state)
        first_lock = cache_module.CacheLock.acquire(self.cache_root)
        first_in_replace = threading.Event()
        allow_first_replace = threading.Event()
        second_waiting = threading.Event()
        second_acquired = threading.Event()
        first_errors: list[BaseException] = []
        second_errors: list[BaseException] = []
        second_results: list[CacheState] = []
        real_write = cache_module._write_runtime_status_atomic
        real_publish_acquire = cache_module._RefreshLockPublishLock.acquire

        def pause_first_write(
            root: Path, directory_name: str, value: object
        ) -> None:
            if threading.current_thread().name == "first-runtime-writer":
                first_in_replace.set()
                if not allow_first_replace.wait(timeout=5):
                    raise AssertionError("first replace timeout")
            real_write(root, directory_name, value)

        def observe_second_wait(root: Path) -> object:
            if threading.current_thread().name == "second-runtime-writer":
                second_waiting.set()
            return real_publish_acquire(root)

        def run_first() -> None:
            try:
                StockCache(self.cache_root, object())._record_runtime_status(
                    state, True, "network_error", first_lock
                )
            except BaseException as error:
                first_errors.append(error)
            finally:
                first_lock.release()

        def run_second() -> None:
            try:
                with cache_module.CacheLock.acquire(self.cache_root) as lock:
                    second_acquired.set()
                    current = CacheState.load(self.cache_root)
                    self.assertIsNotNone(current)
                    second_results.append(
                        StockCache(self.cache_root, object())._record_runtime_status(
                            current, False, None, lock
                        )
                    )
            except BaseException as error:
                second_errors.append(error)

        with patch.object(
            cache_module, "_write_runtime_status_atomic", side_effect=pause_first_write
        ):
            with patch.object(
                cache_module._RefreshLockPublishLock,
                "acquire",
                side_effect=observe_second_wait,
            ):
                first = threading.Thread(
                    target=run_first, name="first-runtime-writer"
                )
                first.start()
                self.assertTrue(first_in_replace.wait(timeout=5))
                expired = (
                    cache_module.time.time()
                    - cache_module._LOCK_TTL_SECONDS
                    - 1
                )
                (first_lock.path / "owner.json").write_text(
                    json.dumps(
                        {"token": first_lock.token, "created_at": expired}
                    ),
                    encoding="utf-8",
                )
                cache_module.os.utime(
                    first_lock.path / f"heartbeat-{first_lock.token}",
                    (expired, expired),
                )

                second = threading.Thread(
                    target=run_second, name="second-runtime-writer"
                )
                second.start()
                self.assertTrue(second_waiting.wait(timeout=5))
                self.assertFalse(second_acquired.is_set())
                allow_first_replace.set()
                first.join(timeout=5)
                second.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual(second_errors, [])
        self.assertEqual(len(second_results), 1)
        final = CacheState.load(self.cache_root)
        self.assertIsNotNone(final)
        self.assertEqual(final.runtime_revision, second_results[0].runtime_revision)
        self.assertFalse(final.stale)

    def test_commit_lock_is_released_when_runtime_replace_fails(self) -> None:
        state = CacheState.load(self.cache_root)
        self.assertIsNotNone(state)
        with cache_module.CacheLock.acquire(self.cache_root) as lock:
            with patch.object(
                cache_module,
                "_write_runtime_status_atomic",
                side_effect=PermissionError("/private/cache/runtime-status"),
            ):
                with self.assertRaisesRegex(StockError, "cache_unavailable"):
                    StockCache(self.cache_root, object())._record_runtime_status(
                        state, True, "network_error", lock
                    )

            with cache_module.RuntimeCommitLock.acquire(self.cache_root):
                pass

    def test_runtime_read_rejects_zero_identity_without_nofollow(self) -> None:
        runtime = self.runtime_status_path()
        runtime.write_text(
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
        outside = Path(self.temp_dir.name) / "outside-runtime.json"
        outside.write_text(
            json.dumps(
                {
                    "generation_id": "synthetic-generation",
                    "checked_at": "2026-08-27T11:00:00+00:00",
                    "stale": True,
                    "warning_code": "network_error",
                }
            ),
            encoding="utf-8",
        )
        identity = SimpleNamespace(
            st_mode=cache_module.stat.S_IFREG,
            st_dev=0,
            st_ino=0,
        )
        real_open = cache_module.os.open

        def open_outside(path: object, flags: int, *args: object) -> int:
            if Path(path) == runtime:
                return real_open(outside, flags, *args)
            return real_open(path, flags, *args)

        with patch.object(cache_module, "_lstat_optional", return_value=identity):
            with patch.object(cache_module.os, "O_NOFOLLOW", 0, create=True):
                with patch.object(cache_module.os, "open", open_outside):
                    with patch.object(cache_module.os, "fstat", return_value=identity):
                        with self.assertRaisesRegex(
                            StockError, "cache_unavailable"
                        ):
                            cache_module.load_runtime_status(
                                runtime, "synthetic-generation"
                            )

    def test_dangling_runtime_status_symlink_is_rejected(self) -> None:
        status = self.runtime_status_path()
        try:
            status.symlink_to(self.cache_root / "missing-status.json")
        except OSError as error:
            self.skipTest(f"symlink недоступен: {error.__class__.__name__}")

        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            CacheState.load(self.cache_root)

    def test_runtime_status_symlink_is_rejected_before_write(self) -> None:
        state = CacheState.load(self.cache_root)
        self.assertIsNotNone(state)
        outside = Path(self.temp_dir.name) / "outside-status.json"
        outside.write_text("sentinel", encoding="utf-8")
        status = self.runtime_status_path()
        try:
            status.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"symlink недоступен: {error.__class__.__name__}")

        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            CacheState.load(self.cache_root)
        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            with cache_module.CacheLock.acquire(self.cache_root) as lock:
                StockCache(self.cache_root, object())._record_runtime_status(
                    state, True, "network_error", lock
                )

        self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel")

    def test_runtime_lstat_failure_is_safe_cache_error(self) -> None:
        runtime = self.runtime_status_path()
        real_lstat = Path.lstat

        def fail_runtime_lstat(path: Path) -> object:
            if path == runtime:
                raise PermissionError("/private/cache/runtime")
            return real_lstat(path)

        with patch.object(Path, "lstat", fail_runtime_lstat):
            with self.assertRaisesRegex(StockError, "cache_unavailable") as raised:
                CacheState.load(self.cache_root)

        self.assertNotIn("/private/", str(raised.exception))

    def test_runtime_fstat_and_close_failures_stay_safe(self) -> None:
        runtime = self.runtime_status_path()
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
        real_close = cache_module.os.close

        def close_then_fail(descriptor: int) -> None:
            real_close(descriptor)
            raise PermissionError("/private/cache/runtime/close")

        with patch.object(
            cache_module.os,
            "fstat",
            side_effect=PermissionError("/private/cache/runtime/fstat"),
        ):
            with patch.object(cache_module.os, "close", side_effect=close_then_fail):
                with self.assertRaisesRegex(
                    StockError, "cache_unavailable"
                ) as raised:
                    CacheState.load(self.cache_root)

        self.assertNotIn("/private/", str(raised.exception))

    def test_runtime_status_write_failure_is_safe(self) -> None:
        state = CacheState.load(self.cache_root)
        self.assertIsNotNone(state)
        cache = StockCache(self.cache_root, object())

        with patch.object(
            cache_module,
            "_write_runtime_status_atomic",
            side_effect=PermissionError("/private/cache/runtime/status.json"),
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable") as raised:
                with cache_module.CacheLock.acquire(self.cache_root) as lock:
                    cache._record_runtime_status(state, True, "network_error", lock)

        self.assertNotIn("/private/", str(raised.exception))

    def test_runtime_status_read_failure_is_safe(self) -> None:
        runtime = self.runtime_status_path()
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
        real_open = cache_module.os.open

        def fail_runtime_read(path: object, flags: int, *args: object) -> int:
            if Path(path) == runtime:
                raise PermissionError("/private/cache/runtime/status.json")
            return real_open(path, flags, *args)

        with patch.object(cache_module.os, "open", fail_runtime_read):
            with self.assertRaisesRegex(StockError, "cache_unavailable") as raised:
                CacheState.load(self.cache_root)

        self.assertNotIn("/private/", str(raised.exception))

    def test_cache_and_search_share_strict_runtime_status_validation(self) -> None:
        runtime = self.runtime_status_path()
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
            json.dumps(
                {
                    "generation_id": "other-generation",
                    "checked_at": "2026-08-27T10:02:00+00:00",
                    "stale": False,
                    "warning_code": None,
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

    def test_cleanup_retries_are_bounded_and_remove_directory(self) -> None:
        directory = Path(self.temp_dir.name) / "retry-cleanup"
        directory.mkdir()
        real_rmtree = schema_module.shutil.rmtree
        attempts = 0

        def fail_twice_then_remove(path: Path) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise PermissionError("/private/retry cleanup")
            real_rmtree(path)

        with patch.object(schema_module, "_SPOOL_CLEANUP_ATTEMPTS", 3):
            with patch.object(
                schema_module.shutil,
                "rmtree",
                side_effect=fail_twice_then_remove,
            ):
                removed = schema_module._remove_spool_directory(directory)

        self.assertTrue(removed)
        self.assertEqual(attempts, 3)
        self.assertFalse(directory.exists())

    def test_primary_and_double_cleanup_failure_register_reliable_retry(self) -> None:
        real_temporary_directory = tempfile.TemporaryDirectory
        temporary_directories: list[object] = []

        class CleanupFailureDirectory:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self._temporary = real_temporary_directory(*args, **kwargs)
                self.name = self._temporary.name
                self.cleanup_calls = 0
                temporary_directories.append(self)

            def cleanup(self) -> None:
                self.cleanup_calls += 1
                if self.cleanup_calls == 1:
                    raise PermissionError("/private/temporary cleanup")
                self._temporary.cleanup()

        real_connect = sqlite3.connect

        def connect(path: str) -> SqliteFailureNormalizationTest.ConnectionProxy:
            return self.ConnectionProxy(real_connect(path), fail_close=True)

        self.files.products.write_bytes(
            (FIXTURES_DIR / "products-generation-mismatch.jsonl").read_bytes()
        )
        with patch.object(
            schema_module.tempfile,
            "TemporaryDirectory",
            CleanupFailureDirectory,
        ):
            with patch.object(schema_module.sqlite3, "connect", side_effect=connect):
                with patch.object(
                    schema_module,
                    "_remove_spool_directory",
                    return_value=False,
                ):
                    with self.assertRaisesRegex(
                        StockError, "generation_mismatch"
                    ) as raised:
                        self.search()

        directory = Path(temporary_directories[0].name)
        self.assertEqual(raised.exception.code, "generation_mismatch")
        self.assertNotIn(str(directory), str(raised.exception))
        self.assertTrue(directory.exists())
        self.assertIn(directory, schema_module._PENDING_SPOOL_CLEANUPS)

        schema_module._retry_pending_spool_cleanups()

        self.assertFalse(directory.exists())
        self.assertNotIn(directory, schema_module._PENDING_SPOOL_CLEANUPS)


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
        errors = StringIO()

        with redirect_stdout(output), redirect_stderr(errors):
            exit_code = search_stock.main(["--limit", "nope"])

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["error"]["code"], "query_invalid")
        self.assertEqual(errors.getvalue(), "")

    def test_only_sole_exact_help_forms_write_help_and_exit_zero(self) -> None:
        for argument in ("-h", "--help"):
            with self.subTest(argument=argument):
                output = StringIO()
                errors = StringIO()
                with patch.object(search_stock, "search_default") as search:
                    with redirect_stdout(output), redirect_stderr(errors):
                        exit_code = search_stock.main([argument])

                self.assertEqual(exit_code, 0)
                self.assertIn("usage:", output.getvalue())
                self.assertEqual(errors.getvalue(), "")
                search.assert_not_called()

    def test_attached_abbreviated_mixed_duplicate_and_separator_help_are_json_errors(self) -> None:
        invalid_arguments = (
            ["-hfoo"],
            ["--he"],
            ["--help=value"],
            ["--help", "--limit", "1"],
            ["-h", "--help"],
            ["--"],
            ["--", "--help"],
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                output = StringIO()
                errors = StringIO()
                with patch.object(search_stock, "search_default") as search:
                    with redirect_stdout(output), redirect_stderr(errors):
                        exit_code = search_stock.main(arguments)

                public = json.loads(output.getvalue())
                self.assertEqual(exit_code, 4)
                self.assertEqual(public["error"]["code"], "query_invalid")
                self.assertEqual(output.getvalue().count("\n"), 1)
                self.assertEqual(errors.getvalue(), "")
                search.assert_not_called()

    def test_decimal_exponent_boundaries_write_json_without_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            generation = Path(temporary) / "synthetic-generation"
            generation.mkdir()
            for name in ("manifest.json", "products.jsonl", "offers.jsonl"):
                (generation / name).write_bytes((FIXTURES_DIR / name).read_bytes())
            files = GenerationFiles.from_directory(
                "synthetic-generation", generation
            )
            config = StockConfig(
                manifest_url="https://stock.example.test/manifest.json",
                username="synthetic-user",
                password="synthetic-password",
                product_id_field="private_product_key",
                offer_product_id_field="private_offer_product_key",
                cache_dir=Path(temporary) / "cache",
            )
            boundaries = (
                ("1e128", "1" + "0" * 128),
                ("1e-128", "0." + "0" * 127 + "1"),
            )

            for raw, expected in boundaries:
                with self.subTest(raw=raw):
                    stdout = StringIO()
                    stderr = StringIO()
                    with patch.object(search_stock.StockConfig, "load", return_value=config):
                        with patch.object(
                            search_stock.StockCache,
                            "current_generation",
                            return_value=files,
                        ):
                            with redirect_stdout(stdout), redirect_stderr(stderr):
                                exit_code = search_stock.main(["--max-price", raw])

                    public = json.loads(stdout.getvalue())
                    self.assertEqual(exit_code, 0)
                    self.assertEqual(stderr.getvalue(), "")
                    self.assertEqual(public["filters"]["max_price"], expected)
                    self.assertEqual(stdout.getvalue().count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
