from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
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
from tests.robotyre_v1_fixture import (
    manifest_bytes as v1_manifest_bytes,
    payloads as v1_payloads,
)


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
EXTREME_JSON_FLOAT = "1e99999999999999999999999999999999999999999999"


class StockSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.generation_dir = Path(self.temp_dir.name) / "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
        self.generation_dir.mkdir()
        products, offers = v1_payloads()
        (self.generation_dir / "manifest.json").write_bytes(
            v1_manifest_bytes(products, offers)
        )
        (self.generation_dir / "products.jsonl").write_bytes(products)
        (self.generation_dir / "offers.jsonl").write_bytes(offers)

        self.files = GenerationFiles.from_directory(
            "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", self.generation_dir
        )
        self.config = StockConfig(
            manifest_url="https://stock.example.test/manifest.json",
            username="synthetic-user",
            password="synthetic-password",
            product_id_field="robotyre_product_id",
            offer_product_id_field="robotyre_product_id",
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

    def test_public_query_filters_reject_lone_surrogates(self) -> None:
        with self.assertRaisesRegex(StockError, "query_invalid"):
            self.search(supplier="\ud800")

    def test_tiny_decimal_exponent_is_safe_query_error(self) -> None:
        with self.assertRaisesRegex(StockError, "query_invalid"):
            self.search(max_price="1e-600000")

    def test_search_distinguishes_sku_and_quantity(self) -> None:
        result = self.search(season="Лето")

        self.assertEqual(result.summary.sku_count, 2)
        self.assertEqual(result.summary.total_quantity, 24)

    def test_all_season_filter_uses_structured_all_season_characteristic(self) -> None:
        result = self.search(season="Всесезонная", min_total_quantity=0)

        self.assertEqual(
            [product.product_id for product in result.products],
            ["4"],
        )
        self.assertEqual(result.products[0].characteristics["season"], "Лето")
        self.assertEqual(result.products[0].characteristics["all_season"], "Да")

    def test_results_sort_by_minimum_price_then_total_quantity(self) -> None:
        result = self.search(season="Лето")

        self.assertEqual(
            [product.product_id for product in result.products],
            ["2", "1"],
        )

    def test_offer_filters_remove_products_without_matching_offer(self) -> None:
        result = self.search(supplier="Synthetic Supplier B")

        self.assertEqual([product.product_id for product in result.products], ["1"])
        self.assertEqual(result.summary.sku_count, 1)
        self.assertEqual(result.summary.total_quantity, 12)

    def test_no_results_is_successful_and_preserves_normalized_filters(self) -> None:
        with self.assertRaisesRegex(StockError, "query_unsupported"):
            self.search(size="195/65R15")

    def test_unknown_and_missing_characteristics_are_reported_separately(self) -> None:
        result = self.search(min_total_quantity=0)

        self.assertIn(
            {"product_id": "4", "characteristic": "spikes", "status": "unknown"},
            result.unknown_characteristics,
        )
        self.assertIn(
            {"product_id": "4", "characteristic": "run_flat", "status": "missing"},
            result.unknown_characteristics,
        )

    def test_unknown_and_missing_size_and_season_are_reported(self) -> None:
        with self.assertRaisesRegex(StockError, "query_unsupported"):
            self.search(size="205/55R16", min_total_quantity=0)

    def test_product_generation_mismatch_fails_closed(self) -> None:
        products = self.files.products
        products.write_bytes((FIXTURES_DIR / "products-generation-mismatch.jsonl").read_bytes())

        with self.assertRaisesRegex(StockError, "generation_mismatch"):
            self.search()

    def test_offer_generation_mismatch_fails_closed(self) -> None:
        offers = self.files.offers
        offers.write_text(
            offers.read_text(encoding="utf-8").replace(
                '"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"', '"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"', 1
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(StockError, "generation_mismatch"):
            self.search()

    def test_search_uses_same_generated_at_contract_as_refresh(self) -> None:
        manifest_path = self.files.manifest
        baseline = json.loads(manifest_path.read_text(encoding="utf-8"))

        for invalid in ("2" * 257, "\ud800", "not-an-iso-8601-timestamp"):
            with self.subTest(invalid=ascii(invalid)):
                manifest = dict(baseline)
                manifest["generated_at"] = invalid
                manifest_path.write_bytes(
                    json.dumps(manifest, separators=(",", ":")).encode("utf-8")
                )

                with self.assertRaisesRegex(StockError, "manifest_invalid"):
                    self.search()

    def test_public_jsonl_strings_reject_lone_surrogates(self) -> None:
        baseline = {
            "name": "Synthetic",
            "article": "SYN",
            "product_type": "Шины",
            "total_quantity": 4,
            "characteristics": {"load_index": "91"},
        }

        for field in ("name", "article", "product_type"):
            with self.subTest(field=field):
                row = dict(baseline)
                row[field] = "\ud800"
                with self.assertRaisesRegex(StockError, "manifest_invalid"):
                    schema_module._product(row, "synthetic-product")

        row = dict(baseline)
        row["characteristics"] = {"load_index": "\ud800"}
        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            schema_module._product(row, "synthetic-product")

        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            schema_module._product(baseline, "\ud800")

        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            schema_module._offer(
                {
                    "supplier": "\ud800",
                    "price": "1",
                    "delivery_days": 1,
                    "quantity": 1,
                }
            )

    def test_non_finite_json_value_is_rejected_before_public_serialization(self) -> None:
        products = self.files.products
        products.write_text(
            products.read_text(encoding="utf-8").replace(
                '"source_value":"Летняя"', '"source_value":NaN', 1
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            self.search()

    def test_numeric_json_prices_preserve_exact_filter_sort_and_output(self) -> None:
        offers = self.files.offers
        offers.write_text(
            offers.read_text(encoding="utf-8").replace(
                '"price_sale":"7000"', '"price_sale":0.10000000000000001', 1
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            self.search()

    def test_numeric_json_price_underflow_is_rejected(self) -> None:
        self.files.offers.write_text(
            '{"robotyre_product_id":"synthetic-summer-a",'
            '"content_generation_id":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
            '"supplier":"Underflow","price":1e-9999,'
            '"delivery_days":1,"quantity":1}\n',
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

    def test_offer_queries_use_composite_index_without_repeated_offer_scans(self) -> None:
        with schema_module._Spool() as spool:
            connection = spool._connection()
            spool.add_product(Product("synthetic", "N", "A", "T", {}, 4, ()))
            statements: list[str] = []
            connection.set_trace_callback(statements.append)
            spool.add_offer(
                "synthetic",
                Offer("Synthetic", Decimal("1"), 1, 1),
                5,
            )
            spool.result(
                SearchQuery.from_args(argparse.Namespace(supplier="Synthetic"))
            )
            connection.set_trace_callback(None)
            relevant = tuple(
                statement
                for statement in statements
                if statement.startswith(("DELETE", "SELECT"))
            )
            plans = tuple(
                (
                    statement,
                    tuple(
                        row[3]
                        for row in connection.execute(
                            "EXPLAIN QUERY PLAN " + statement
                        )
                    ),
                )
                for statement in relevant
            )

        self.assertTrue(relevant)
        for statement, details in plans:
            with self.subTest(statement=statement, details=details):
                self.assertFalse(
                    any(detail.startswith("SCAN o") for detail in details)
                )
                self.assertNotIn("USE TEMP B-TREE FOR ORDER BY", details)
        trim_plan = next(
            details for statement, details in plans if statement.startswith("DELETE")
        )
        self.assertTrue(
            any("USING COVERING INDEX" in detail for detail in trim_plan)
        )
        product_plan = next(
            details
            for statement, details in plans
            if statement.startswith("SELECT id,n,a,t,ch,q,u")
        )
        self.assertTrue(
            any("c_by_offer_price_quantity" in detail for detail in product_plan)
        )

    def test_offer_trim_vm_work_stays_bounded_as_unrelated_rows_grow(self) -> None:
        def measured_steps(unrelated_rows: int) -> int:
            with schema_module._Spool() as spool:
                connection = spool._connection()
                connection.executemany(
                    "INSERT INTO o VALUES(?,?,?,?,?)",
                    (
                        (f"other-{index}", "S", "1", 1, 1)
                        for index in range(unrelated_rows)
                    ),
                )
                steps = 0

                def count_step() -> int:
                    nonlocal steps
                    steps += 1
                    return 0

                connection.set_progress_handler(count_step, 1)
                try:
                    spool.add_offer(
                        "target",
                        Offer("S", Decimal("1"), 1, 1),
                        5,
                    )
                finally:
                    connection.set_progress_handler(None, 0)
                return steps

        small = measured_steps(20)
        large = measured_steps(2_000)

        self.assertLess(large, small * 4)

    def test_main_and_temp_page_budgets_are_applied_and_temp_growth_is_capped(
        self,
    ) -> None:
        maximum_pages = 16
        with patch.object(
            schema_module,
            "MAX_SPOOL_BYTES",
            maximum_pages * 4096,
        ):
            with schema_module._Spool() as spool:
                connection = spool._connection()
                main_limit = connection.execute(
                    "PRAGMA main.max_page_count"
                ).fetchone()
                temp_limit = connection.execute(
                    "PRAGMA temp.max_page_count"
                ).fetchone()
                temp_page_size = connection.execute(
                    "PRAGMA temp.page_size"
                ).fetchone()

                self.assertEqual(main_limit, (maximum_pages,))
                self.assertEqual(temp_limit, (maximum_pages,))
                self.assertEqual(temp_page_size, (4096,))

                connection.execute("CREATE TEMP TABLE temp_growth(value BLOB)")
                with self.assertRaises(sqlite3.Error):
                    connection.execute(
                        "INSERT INTO temp_growth VALUES(zeroblob(?))",
                        (128 * 1024,),
                    )

    def test_more_than_ten_thousand_products_returns_exact_top_result(self) -> None:
        base_product = json.loads(
            (FIXTURES_DIR / "products.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        product_rows = []
        offer_rows = []
        base_offer = json.loads(
            (FIXTURES_DIR / "offers.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        for index in range(10_001):
            product_id = str(index + 1)
            product = dict(base_product)
            product["robotyre_product_id"] = product_id
            product["name"] = f"Synthetic {product_id}"
            product_rows.append(json.dumps(product, separators=(",", ":")))
            offer = dict(base_offer)
            offer["robotyre_product_id"] = product_id
            offer["price_sale"] = str(20_000 - index)
            offer_rows.append(json.dumps(offer, separators=(",", ":")))
        self.files.products.write_text(
            "\n".join(product_rows) + "\n", encoding="utf-8"
        )
        self.files.offers.write_text(
            "\n".join(offer_rows) + "\n",
            encoding="utf-8",
        )

        result = self.search(season="Лето", limit=1)

        self.assertEqual(result.products[0].product_id, "10001")

    def test_duplicate_json_keys_fail_closed(self) -> None:
        self.files.products.write_text(
            '{"robotyre_product_id":"synthetic-a","content_generation_id":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
            '"content_generation_id":"other-generation"}\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            self.search()

    def test_nested_overflow_json_number_fails_closed(self) -> None:
        products = self.files.products
        products.write_text(
            products.read_text(encoding="utf-8").replace(
                '"source_value":"Летняя"', '"source_value":{"bad":1e400}', 1
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            self.search()

    def test_unapproved_large_nested_characteristic_is_not_public(self) -> None:
        products = self.files.products
        row = json.loads(products.read_text(encoding="utf-8").splitlines()[0])
        row["characteristics"]["private_nested"] = {"payload": "x" * 1024}
        products.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            self.search(season="Лето")

    def test_offer_tie_prefers_higher_quantity(self) -> None:
        offers = self.files.offers
        base = json.loads(offers.read_text(encoding="utf-8").splitlines()[0])
        base["robotyre_product_id"] = "2"
        base["price_sale"] = "6000"
        low = dict(base)
        low["supplier_name"] = "Low"
        low["quantity"] = 1
        high = dict(base)
        high["supplier_name"] = "High"
        high["quantity"] = 99
        offers.write_text(
            offers.read_text(encoding="utf-8")
            + json.dumps(low, separators=(",", ":")) + "\n"
            + json.dumps(high, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        public = self.search(season="Лето").to_public_dict()
        product = next(item for item in public["products"] if item["product_id"] == "2")

        self.assertEqual(product["offers"][0]["supplier"], "High")

    def test_persisted_stale_status_is_returned_by_later_search(self) -> None:
        (self.generation_dir / "state.json").write_text(
            json.dumps(
                {
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "verified_at": "2026-08-27T10:01:00+00:00",
                    "stale": True,
                    "warning_codes": ["network_error"],
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
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "verified_at": "2026-08-27T10:01:00+00:00",
                    "stale": True,
                    "warning_codes": [],
                }
            ),
            json.dumps(
                {
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "verified_at": "2026-08-27T10:01:00+00:00",
                    "stale": True,
                    "warning_codes": "",
                }
            ),
            json.dumps(
                {
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "verified_at": "2026-08-27T10:01:00+00:00",
                    "stale": False,
                    "warning_codes": ["network_error"],
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
            '{"generation_id":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
            '"verified_at":"2026-08-27T10:01:00+00:00",'
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
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "verified_at": "2026-08-27T10:01:00+00:00",
                    "stale": False,
                    "warning_codes": [],
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
        result = self.search(season="Лето", limit=2, offers_limit=3)
        public = result.to_public_dict()
        serialized = json.dumps(public, ensure_ascii=False)

        self.assertEqual(len(public["products"]), 2)
        summer_a = next(
            product for product in public["products"]
            if product["product_id"] == "1"
        )
        self.assertEqual(len(summer_a["offers"]), 3)
        self.assertEqual(set(summer_a), {
            "product_id", "name", "article", "product_type", "characteristics",
            "total_quantity", "minimum_price", "offers",
        })
        self.assertNotIn("robotyre_product_id", serialized)
        self.assertNotIn("robotyre_product_id", serialized)
        self.assertNotIn("source_note", serialized)

    def test_actual_boundary_removes_previous_tail_before_truncation_warning(self) -> None:
        maximum = 512 * 1024
        generation = {
            "id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
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


@unittest.skipUnless(
    os.name == "posix",
    "POSIX backend white-box: CacheLock, runtime-status dirfd и atomic write hooks",
)
class SearchFreshnessIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cache_root = Path(self.temp_dir.name) / "cache"
        cache_module._attest_cache_root(self.cache_root, create=True).close()
        self.generation = self.cache_root / "generations" / "generation-existing"
        self.generation.mkdir(parents=True)
        products, offers = v1_payloads()
        manifest_payload = v1_manifest_bytes(products, offers)
        manifest = json.loads(manifest_payload)
        (self.generation / "manifest.json").write_bytes(manifest_payload)
        (self.generation / "products.jsonl").write_bytes(products)
        (self.generation / "offers.jsonl").write_bytes(offers)
        (self.generation / "state.json").write_text(
            json.dumps(
                {
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "generated_at": manifest["generated_at"],
                    "verified_at": "2026-08-27T10:01:00+00:00",
                    "manifest_etag": '"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"',
                    "manifest_last_modified": "Thu, 27 Aug 2026 10:00:00 GMT",
                    "stale": False,
                    "warning_codes": [],
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        (self.cache_root / "current.json").write_text(
            json.dumps(
                {
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "directory_name": "generation-existing",
                }
            ),
            encoding="utf-8",
        )
        self.config = StockConfig(
            manifest_url="https://stock.example.test/manifest.json",
            username="synthetic-user",
            password="synthetic-password",
            product_id_field="robotyre_product_id",
            offer_product_id_field="robotyre_product_id",
            cache_dir=self.cache_root,
        )

    def public_search(self) -> dict[str, object]:
        files = StockCache(self.cache_root, object()).current_generation()
        query = SearchQuery.from_args(argparse.Namespace())
        return StockSearcher(files, self.config).search(query).to_public_dict()

    def _write_generation(self, directory_name: str, generation_id: str) -> Path:
        directory = self.cache_root / "generations" / directory_name
        directory.mkdir()
        products, offers = v1_payloads(generation_id)
        manifest_payload = v1_manifest_bytes(products, offers, generation_id)
        manifest = json.loads(manifest_payload)
        (directory / "manifest.json").write_bytes(manifest_payload)
        (directory / "products.jsonl").write_bytes(products)
        (directory / "offers.jsonl").write_bytes(offers)
        (directory / "state.json").write_text(
            json.dumps(
                {
                    "generation_id": generation_id,
                    "generated_at": manifest["generated_at"],
                    "verified_at": "2026-08-27T11:01:00+00:00",
                    "manifest_etag": None,
                    "manifest_last_modified": None,
                    "stale": False,
                    "warning_codes": [],
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return directory

    def test_generation_snapshot_survives_cleanup_and_new_search_uses_successor(
        self,
    ) -> None:
        cache = StockCache(self.cache_root, object())
        query = SearchQuery.from_args(argparse.Namespace())

        with cache.generation_snapshot() as snapshot_a:
            generation_b = self._write_generation(
                "generation-successor", "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
            )
            (self.cache_root / "current.json").write_text(
                json.dumps(
                    {
                        "generation_id": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                        "directory_name": generation_b.name,
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            shutil.rmtree(self.generation)

            result_a = StockSearcher(snapshot_a, self.config).search(query)

        with cache.generation_snapshot() as snapshot_b:
            result_b = StockSearcher(snapshot_b, self.config).search(query)

        self.assertEqual(result_a.generation["id"], "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd")
        self.assertEqual(result_b.generation["id"], "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")
        self.assertEqual(result_a.summary, result_b.summary)

    def test_generation_snapshot_closes_every_partial_open_on_failure(self) -> None:
        opened: list[int] = []
        real_open = cache_module._open_private_child_regular_file

        def fail_after_products(parent_descriptor: int, name: str) -> int:
            if name == "offers.jsonl":
                raise OSError("synthetic snapshot open failure")
            descriptor = real_open(parent_descriptor, name)
            opened.append(descriptor)
            return descriptor

        with patch.object(
            cache_module,
            "_open_private_child_regular_file",
            side_effect=fail_after_products,
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                StockCache(self.cache_root, object()).generation_snapshot()

        self.assertGreaterEqual(len(opened), 2)
        for descriptor in opened:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_generation_snapshot_rejects_oversized_pointer_before_parsing(self) -> None:
        (self.cache_root / "current.json").write_text(
            json.dumps(
                {
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "directory_name": "g" * (cache_module._WINDOWS_POINTER_MAX_BYTES + 1),
                }
            ),
            encoding="utf-8",
        )

        with patch.object(
            cache_module.CurrentPointer,
            "from_value",
            side_effect=AssertionError("oversized pointer was parsed"),
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable"):
                StockCache(self.cache_root, object()).generation_snapshot()

    @unittest.skipUnless(hasattr(os, "link"), "hard links are required")
    def test_generation_snapshot_rejects_hardlinked_payload_without_chmod(self) -> None:
        external = Path(self.temp_dir.name) / "external-products.jsonl"
        external.write_bytes((self.generation / "products.jsonl").read_bytes())
        external.chmod(0o644)
        original_mode = stat.S_IMODE(external.stat().st_mode)
        products = self.generation / "products.jsonl"
        products.unlink()
        os.link(external, products)

        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            StockCache(self.cache_root, object()).generation_snapshot()

        self.assertEqual(stat.S_IMODE(external.stat().st_mode), original_mode)

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

    def test_304_then_search_reads_each_payload_at_most_twice(self) -> None:
        class NotModifiedClient:
            def get_manifest(
                self, etag: str | None, modified: str | None
            ) -> HttpResponse:
                return HttpResponse(status=304, headers={}, body=b"")

        payload_bytes = self.filesystem_payload_bytes()
        verified_bytes = 0
        streamed_bytes = 0
        real_verify = cache_module._verify_child_file
        real_rows = schema_module._rows

        def count_verification(
            parent_descriptor: int,
            name: str,
            expected: cache_module.ManifestFile,
            progress: object | None = None,
        ) -> None:
            nonlocal verified_bytes
            verified_bytes += expected.bytes
            real_verify(
                parent_descriptor,
                name,
                expected,
                progress if callable(progress) else None,
            )

        def count_stream(
            path: Path, maximum_rows: int, *integrity: object
        ) -> object:
            nonlocal streamed_bytes
            streamed_bytes += path.stat().st_size
            yield from real_rows(path, maximum_rows, *integrity)

        with patch.object(
            cache_module, "_verify_child_file", side_effect=count_verification
        ), patch.object(schema_module, "_rows", side_effect=count_stream):
            refreshed = StockCache(
                self.cache_root, NotModifiedClient()
            ).refresh(self.config)
            files = StockCache(self.cache_root, object()).current_generation()
            query = SearchQuery.from_args(argparse.Namespace())
            public = StockSearcher(files, self.config).search(query).to_public_dict()

        self.assertEqual(refreshed.status, "not_modified")
        self.assertIsNotNone(files.integrity)
        self.assertEqual(public["status"], "ok")
        self.assertEqual(verified_bytes, payload_bytes)
        self.assertEqual(streamed_bytes, payload_bytes)
        self.assertEqual(verified_bytes + streamed_bytes, payload_bytes * 2)

    def test_search_stream_rejects_payload_changed_after_generation_selection(
        self,
    ) -> None:
        files = StockCache(self.cache_root, object()).current_generation()
        products = self.generation / "products.jsonl"
        products.write_bytes(
            products.read_bytes().replace(
                b"Synthetic Summer A", b"Synthetic Summer X", 1
            )
        )
        query = SearchQuery.from_args(argparse.Namespace())

        with self.assertRaisesRegex(StockError, "cache_unavailable"):
            StockSearcher(files, self.config).search(query)

    def filesystem_payload_bytes(self) -> int:
        return sum(
            (self.generation / name).stat().st_size
            for name in ("products.jsonl", "offers.jsonl")
        )

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
        self.assertEqual(fresh["generation"]["checked_at"], initial_checked_at)
        self.assertFalse(fresh["generation"]["stale"])
        self.assertEqual(
            second_stale["generation"]["checked_at"],
            fresh["generation"]["checked_at"],
        )
        self.assertTrue(second_stale["generation"]["stale"])

    def test_delayed_failure_cannot_overwrite_later_same_generation_304(self) -> None:
        entered_fallback = threading.Event()
        allow_fallback = threading.Event()
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
        first.start()
        self.assertTrue(entered_fallback.wait(timeout=5))

        second = StockCache(self.cache_root, NotModifiedClient()).refresh(self.config)
        allow_fallback.set()
        first.join(timeout=5)
        if "cache_locked" in second.warning_codes:
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
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "verified_at": "2026-08-27T11:00:00+00:00",
                    "stale": True,
                    "warning_codes": ["network_error"],
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
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "verified_at": state.verified_at,
                    "stale": False,
                    "warning_codes": [],
                }
            ),
            encoding="utf-8",
        )
        original = runtime.with_suffix(".original")
        real_write = cache_module._write_runtime_status_atomic_attested

        def swap_before_write(
            attestation: cache_module.CacheRootAttestation,
            directory_name: str,
            value: object,
        ) -> None:
            if cache_module._runtime_status_path(
                self.cache_root, directory_name
            ) == runtime:
                runtime.rename(original)
                runtime.symlink_to(outside)
            real_write(attestation, directory_name, value)

        with patch.object(
            cache_module,
            "_write_runtime_status_atomic_attested",
            swap_before_write,
        ):
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
        real_write = cache_module._write_runtime_status_atomic_attested

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
                root_attestation: cache_module.CacheRootAttestation,
                directory_name: str,
                value: object,
            ) -> None:
                owner_state = cache_module.CacheLock._read_owner(lock.path)
                self.assertIsNotNone(owner_state)
                self.assertLess(cache_module.time.time() - owner_state[1], 5)
                real_write(root_attestation, directory_name, value)

            with patch.object(
                cache_module,
                "_write_runtime_status_atomic_attested",
                side_effect=assert_fresh_lease_then_write,
            ):
                updated = cache._record_runtime_status(
                    state, True, "network_error", lock
                )

        self.assertTrue(updated.stale)
        self.assertIn("network_error", updated.warning_codes)

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

        def pause_delayed_commit(
            root: Path,
            root_attestation: cache_module.CacheRootAttestation | None = None,
        ) -> object:
            if threading.current_thread().name == "delayed-runtime-writer":
                delayed_at_commit.set()
                if not allow_delayed_commit.wait(timeout=5):
                    raise AssertionError("delayed commit timeout")
            return real_commit_acquire(root, root_attestation)

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
        real_write = cache_module._write_runtime_status_atomic_attested
        real_publish_acquire = cache_module._RefreshLockPublishLock.acquire

        def pause_first_write(
            root_attestation: cache_module.CacheRootAttestation,
            directory_name: str,
            value: object,
        ) -> None:
            if threading.current_thread().name == "first-runtime-writer":
                first_in_replace.set()
                if not allow_first_replace.wait(timeout=5):
                    raise AssertionError("first replace timeout")
            real_write(root_attestation, directory_name, value)

        def observe_second_wait(
            root: Path,
            root_attestation: cache_module.CacheRootAttestation | None = None,
        ) -> object:
            if threading.current_thread().name == "second-runtime-writer":
                second_waiting.set()
            return real_publish_acquire(root, root_attestation)

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
            cache_module,
            "_write_runtime_status_atomic_attested",
            side_effect=pause_first_write,
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
                "_write_runtime_status_atomic_attested",
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
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "verified_at": "2026-08-27T10:01:00+00:00",
                    "stale": False,
                    "warning_codes": [],
                }
            ),
            encoding="utf-8",
        )
        outside = Path(self.temp_dir.name) / "outside-runtime.json"
        outside.write_text(
            json.dumps(
                {
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "verified_at": "2026-08-27T11:00:00+00:00",
                    "stale": True,
                    "warning_codes": ["network_error"],
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
                                runtime, "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
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
        real_lstat = cache_module._lstat_private_child

        def fail_runtime_lstat(
            parent_descriptor: int | None,
            parent: Path,
            name: str,
            *,
            missing_ok: bool = False,
        ) -> object:
            if name == runtime.name:
                raise PermissionError("/private/cache/runtime")
            return real_lstat(
                parent_descriptor, parent, name, missing_ok=missing_ok
            )

        with patch.object(
            cache_module, "_lstat_private_child", fail_runtime_lstat
        ):
            with self.assertRaisesRegex(StockError, "cache_unavailable") as raised:
                CacheState.load(self.cache_root)

        self.assertNotIn("/private/", str(raised.exception))

    def test_runtime_fstat_and_close_failures_stay_safe(self) -> None:
        runtime = self.runtime_status_path()
        runtime.write_text(
            json.dumps(
                {
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "verified_at": "2026-08-27T10:02:00+00:00",
                    "stale": True,
                    "warning_codes": ["network_error"],
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
            "_write_runtime_status_atomic_attested",
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
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "verified_at": "2026-08-27T10:02:00+00:00",
                    "stale": True,
                    "warning_codes": ["network_error"],
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
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "verified_at": "2026-08-27T10:02:00+00:00",
                    "stale": True,
                    "warning_codes": "",
                }
            ),
            json.dumps(
                {
                    "generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "verified_at": "2026-08-27T10:02:00+00:00",
                    "stale": False,
                    "warning_codes": ["network_error"],
                }
            ),
            json.dumps(
                {
                    "generation_id": "other-generation",
                    "verified_at": "2026-08-27T10:02:00+00:00",
                    "stale": False,
                    "warning_codes": [],
                }
            ),
            '{"generation_id":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
            '"verified_at":"2026-08-27T10:02:00+00:00",'
            '"stale":false,"nested":' + "[" * 2_000 + "0" + "]" * 2_000 + "}",
            '{"generation_id":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
            '"verified_at":"2026-08-27T10:02:00+00:00",'
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
        self.generation_dir = Path(self.temp_dir.name) / "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
        self.generation_dir.mkdir()
        for name in ("manifest.json", "products.jsonl", "offers.jsonl"):
            (self.generation_dir / name).write_bytes((FIXTURES_DIR / name).read_bytes())
        self.files = GenerationFiles.from_directory(
            "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", self.generation_dir
        )
        self.config = StockConfig(
            manifest_url="https://stock.example.test/manifest.json",
            username="synthetic-user",
            password="synthetic-password",
            product_id_field="robotyre_product_id",
            offer_product_id_field="robotyre_product_id",
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

    def test_pending_cleanup_registry_is_bounded(self) -> None:
        registered: list[Path] = []
        with patch.object(schema_module, "MAX_PENDING_SPOOL_CLEANUPS", 2):
            for index in range(3):
                path = Path(self.temp_dir.name) / f"pending-{index}"
                registered.append(path)
                schema_module._register_spool_cleanup(path, object())

        self.assertEqual(len(schema_module._PENDING_SPOOL_CLEANUPS), 2)
        for path in registered:
            schema_module._PENDING_SPOOL_CLEANUPS.pop(path, None)

    def test_atexit_terminal_failures_are_safe_once_and_clear_registry(self) -> None:
        observer = (
            "import atexit, os\n"
            "def observe():\n"
            "    from papa_shin_stock import schema\n"
            "    os.write(1, (str(len(schema._PENDING_SPOOL_CLEANUPS)) + '\\n').encode())\n"
            "atexit.register(observe)\n"
            "from pathlib import Path\n"
            "from papa_shin_stock import schema\n"
            "class Temporary:\n"
            "    def cleanup(self):\n"
            "        raise PermissionError('/private/cleanup')\n"
            "for name in ('first', 'second'):\n"
            "    schema._register_spool_cleanup(Path('/private') / name, Temporary())\n"
            "schema._remove_spool_directory = lambda path: False\n"
        )
        environment = os.environ.copy()
        environment["PYTHONWARNINGS"] = "error"
        environment["PYTHONPATH"] = str(SCRIPTS_DIR)

        completed = subprocess.run(
            [sys.executable, "-c", observer],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "0\n")
        self.assertEqual(
            completed.stderr,
            "papa-shin-stock: temporary search cleanup incomplete\n",
        )
        self.assertNotIn("Traceback", completed.stderr)
        self.assertNotIn("/private", completed.stderr)

    def test_atexit_cleanup_does_not_raise_when_stderr_is_unavailable(self) -> None:
        path = Path(self.temp_dir.name) / "terminal-failure"
        schema_module._register_spool_cleanup(path, object())

        with patch.object(
            schema_module,
            "_remove_spool_directory",
            return_value=False,
        ):
            with patch.object(
                schema_module.os,
                "write",
                side_effect=OSError("/private/stderr"),
            ):
                schema_module._cleanup_spools_at_exit()

        self.assertEqual(schema_module._PENDING_SPOOL_CLEANUPS, {})


class SearchStockCliTest(unittest.TestCase):
    def test_help_subprocess_writes_utf8_under_legacy_stdout_encoding(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "cp1252"

        completed = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "search_stock.py"), "--help"],
            env=environment,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        help_text = completed.stdout.decode("utf-8")
        self.assertIn("usage:", help_text)
        self.assertIn("Поиск по локальному проверенному кэшу", help_text)

    def _run_generation_cli(
        self,
        *,
        manifest: bytes,
        products: bytes,
        offers: bytes,
        arguments: list[str] | None = None,
    ) -> tuple[int, str, str, str]:
        with tempfile.TemporaryDirectory() as temporary:
            generation = Path(temporary) / "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
            generation.mkdir()
            (generation / "manifest.json").write_bytes(manifest)
            (generation / "products.jsonl").write_bytes(products)
            (generation / "offers.jsonl").write_bytes(offers)
            files = GenerationFiles.from_directory(
                "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", generation
            )
            config = StockConfig(
                manifest_url="https://stock.example.test/manifest.json",
                username="synthetic-user",
                password="synthetic-password",
                product_id_field="robotyre_product_id",
                offer_product_id_field="robotyre_product_id",
                cache_dir=Path(temporary) / "cache",
            )
            output = StringIO()
            errors = StringIO()

            with patch.object(search_stock.StockConfig, "load", return_value=config):
                with patch.object(
                    search_stock.StockCache,
                    "generation_snapshot",
                    return_value=nullcontext(files),
                ):
                    with redirect_stdout(output), redirect_stderr(errors):
                        exit_code = search_stock.main(arguments or [])

            return exit_code, output.getvalue(), errors.getvalue(), temporary

    def test_size_query_is_rejected_before_source_validation(
        self,
    ) -> None:
        product = json.loads(
            (FIXTURES_DIR / "products.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        product["size"] = "not-a-size"

        exit_code, output, errors, private_path = self._run_generation_cli(
            manifest=(FIXTURES_DIR / "manifest.json").read_bytes(),
            products=(json.dumps(product, separators=(",", ":")) + "\n").encode(),
            offers=b"",
            arguments=["--size", "205/55R16"],
        )

        self.assertEqual(exit_code, 4)
        self.assertEqual(errors, "")
        self.assertNotIn(private_path, output)
        self.assertEqual(
            json.loads(output),
            {
                "status": "error",
                "error": {
                    "code": "query_unsupported",
                    "message": "Источник не публикует структурированный типоразмер",
                },
            },
        )

    def test_missing_offer_product_id_is_manifest_error(self) -> None:
        offer = json.loads(
            (FIXTURES_DIR / "offers.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        offer.pop("robotyre_product_id")

        exit_code, output, errors, private_path = self._run_generation_cli(
            manifest=(FIXTURES_DIR / "manifest.json").read_bytes(),
            products=(FIXTURES_DIR / "products.jsonl").read_bytes(),
            offers=(json.dumps(offer, separators=(",", ":")) + "\n").encode(),
        )

        self.assertEqual(exit_code, 3)
        self.assertEqual(errors, "")
        self.assertNotIn(private_path, output)
        self.assertEqual(json.loads(output)["error"]["code"], "manifest_invalid")

    def test_success_writes_one_public_json_document(self) -> None:
        output = StringIO()
        public_result = {
            "status": "ok",
            "generation": {"id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"},
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

    def test_surrogate_jsonl_string_is_one_safe_json_error_without_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            generation = Path(temporary) / "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
            generation.mkdir()
            (generation / "manifest.json").write_bytes(
                (FIXTURES_DIR / "manifest.json").read_bytes()
            )
            product = {
                "robotyre_product_id": "synthetic-product",
                "content_generation_id": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                "name": "\ud800",
                "article": "SYN",
                "product_type": "Шины",
                "total_quantity": 4,
                "characteristics": {},
            }
            (generation / "products.jsonl").write_bytes(
                json.dumps(product, separators=(",", ":")).encode("utf-8") + b"\n"
            )
            (generation / "offers.jsonl").write_bytes(b"")
            files = GenerationFiles.from_directory(
                "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", generation
            )
            config = StockConfig(
                manifest_url="https://stock.example.test/manifest.json",
                username="synthetic-user",
                password="synthetic-password",
                product_id_field="robotyre_product_id",
                offer_product_id_field="robotyre_product_id",
                cache_dir=Path(temporary) / "cache",
            )
            output = StringIO()
            errors = StringIO()

            with patch.object(search_stock.StockConfig, "load", return_value=config):
                with patch.object(
                    search_stock.StockCache,
                    "generation_snapshot",
                    return_value=nullcontext(files),
                ):
                    with redirect_stdout(output), redirect_stderr(errors):
                        exit_code = search_stock.main([])

        self.assertEqual(exit_code, 3)
        self.assertEqual(output.getvalue().count("\n"), 1)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "status": "error",
                "error": {
                    "code": "manifest_invalid",
                    "message": "Некорректные машинные данные",
                },
            },
        )

    def test_extreme_numeric_jsonl_price_is_one_safe_json_error_without_stderr(
        self,
    ) -> None:
        offer = (
            '{"robotyre_product_id":"synthetic-summer-a",'
            '"content_generation_id":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
            '"supplier":"Synthetic","price":'
            f"{EXTREME_JSON_FLOAT},"
            '"delivery_days":1,"quantity":1}\n'
        ).encode("ascii")

        exit_code, output, errors, private_path = self._run_generation_cli(
            manifest=(FIXTURES_DIR / "manifest.json").read_bytes(),
            products=(FIXTURES_DIR / "products.jsonl").read_bytes(),
            offers=offer,
        )

        self.assertEqual(exit_code, 3)
        self.assertEqual(output.count("\n"), 1)
        self.assertEqual(errors, "")
        self.assertNotIn("Traceback", output)
        self.assertNotIn(private_path, output)
        self.assertEqual(
            json.loads(output),
            {
                "status": "error",
                "error": {
                    "code": "manifest_invalid",
                    "message": "Некорректные машинные данные",
                },
            },
        )

    def test_extreme_numeric_manifest_extra_field_is_one_safe_json_error_without_stderr(
        self,
    ) -> None:
        manifest = (
            '{"generation_id":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
            '"generated_at":"2026-08-27T10:00:00+00:00",'
            f'"extra":{EXTREME_JSON_FLOAT}}}'
        ).encode("ascii")

        exit_code, output, errors, private_path = self._run_generation_cli(
            manifest=manifest,
            products=(FIXTURES_DIR / "products.jsonl").read_bytes(),
            offers=(FIXTURES_DIR / "offers.jsonl").read_bytes(),
        )

        self.assertEqual(exit_code, 3)
        self.assertEqual(output.count("\n"), 1)
        self.assertEqual(errors, "")
        self.assertNotIn("Traceback", output)
        self.assertNotIn(private_path, output)
        self.assertEqual(
            json.loads(output),
            {
                "status": "error",
                "error": {
                    "code": "manifest_invalid",
                    "message": "Некорректный manifest",
                },
            },
        )

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
            generation = Path(temporary) / "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
            generation.mkdir()
            for name in ("manifest.json", "products.jsonl", "offers.jsonl"):
                (generation / name).write_bytes((FIXTURES_DIR / name).read_bytes())
            files = GenerationFiles.from_directory(
                "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", generation
            )
            config = StockConfig(
                manifest_url="https://stock.example.test/manifest.json",
                username="synthetic-user",
                password="synthetic-password",
                product_id_field="robotyre_product_id",
                offer_product_id_field="robotyre_product_id",
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
                            "generation_snapshot",
                            return_value=nullcontext(files),
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
