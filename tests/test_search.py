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

    def test_too_many_matching_products_are_rejected_before_offer_pass(self) -> None:
        product = (
            '{"private_product_key":"synthetic-%s","content_generation_id":"synthetic-generation",'
            '"name":"Synthetic","article":"SYN","product_type":"Шины",'
            '"size":"205/55R16","season":"Лето","spikes":"Нет","run_flat":"Нет",'
            '"total_quantity":4,"characteristics":{}}\n'
        )
        self.files.products.write_text(
            "".join(product % index for index in range(10_001)), encoding="utf-8"
        )

        with self.assertRaisesRegex(StockError, "query_invalid"):
            self.search(size="205/55R16", season="Лето")

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


if __name__ == "__main__":
    unittest.main()
