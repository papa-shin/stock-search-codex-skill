from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from papa_shin_stock.cache import GenerationFiles
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.query import SearchQuery, normalize_tire_size
from papa_shin_stock.schema import StockSearcher
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
