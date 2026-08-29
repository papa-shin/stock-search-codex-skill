from __future__ import annotations

import copy
import argparse
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS_DIR))

from papa_shin_stock.cache import Manifest, StockCache
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.http_client import DownloadReceipt, HttpResponse
from papa_shin_stock.query import SearchQuery
from papa_shin_stock.robotyre_v1 import offer_projection, product_projection
from papa_shin_stock.schema import StockSearcher
from tests.robotyre_v1_fixture import manifest_bytes as dynamic_manifest_bytes
from tests.robotyre_v1_fixture import payloads


GENERATION_ID = "d" * 64


def first_row(name: str) -> dict[str, object]:
    line = (FIXTURES_DIR / name).read_text(encoding="utf-8").splitlines()[0]
    value = json.loads(line)
    assert isinstance(value, dict)
    return value


class RobotyreV1ProjectionTest(unittest.TestCase):
    def product(self) -> dict[str, object]:
        return copy.deepcopy(first_row("products.jsonl"))

    def offer(self) -> dict[str, object]:
        return copy.deepcopy(first_row("offers.jsonl"))

    def test_product_projection_has_exact_public_mapping(self) -> None:
        projected = product_projection(self.product(), GENERATION_ID)

        self.assertEqual(projected.product_id, "1")
        self.assertEqual(projected.product_type, "Шины")
        self.assertEqual(projected.article, "SYN-SUM-A")
        self.assertEqual(
            projected.characteristics,
            {
                "season": "Лето",
                "all_season": "Нет",
                "spikes": "Нет",
                "run_flat": "Нет",
            },
        )
        self.assertNotIn("source_value", json.dumps(projected.characteristics))

    def test_all_seven_known_characteristics_have_exact_public_snapshot(self) -> None:
        row = self.product()
        values = {
            "season": "Лето",
            "all_season": True,
            "spikes": True,
            "run_flat": False,
            "disk_type": "Литой",
            "truck_tire_axis": "Рулевая",
            "truck_tire_construction": "Радиальная",
        }
        for name, value in values.items():
            row["characteristics"][name] = {
                "normalized_value": value,
                "normalization_status": "known",
                "source_value": value,
            }

        projected = product_projection(row, GENERATION_ID)

        self.assertEqual(
            projected.characteristics,
            {
                "season": "Лето",
                "all_season": "Да",
                "spikes": "Да",
                "run_flat": "Нет",
                "disk_type": "Литой",
                "truck_axis": "Рулевая",
                "truck_construction": "Радиальная",
            },
        )

    def test_all_supported_product_types_are_explicit(self) -> None:
        expected = {
            "172": "Шины",
            "173": "Диски",
            "12371": "Грузовые шины",
            "12372": "Грузовые диски",
            "12373": "Шины для квадроциклов",
        }
        for product_type_id, title in expected.items():
            with self.subTest(product_type_id=product_type_id):
                row = self.product()
                row["product_type_id"] = product_type_id
                self.assertEqual(
                    product_projection(row, GENERATION_ID).product_type, title
                )

    def test_name_and_article_fallbacks_do_not_use_legacy_articles(self) -> None:
        row = self.product()
        row.update(
            {
                "name": None,
                "brand": None,
                "model": None,
                "brand_articul": None,
                "articul_robotyre": "MUST-NOT-BE-PUBLIC",
            }
        )

        projected = product_projection(row, GENERATION_ID)

        self.assertEqual(projected.name, "Товар Robotyre #1")
        self.assertEqual(projected.article, "")

    def test_generation_and_version_have_distinct_safe_classification(self) -> None:
        wrong_generation = self.product()
        wrong_generation["content_generation_id"] = "e" * 64
        with self.assertRaisesRegex(StockError, "generation_mismatch"):
            product_projection(wrong_generation, GENERATION_ID)

        wrong_version = self.product()
        wrong_version["schema_version"] = "2"
        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            product_projection(wrong_version, GENERATION_ID)

    def test_product_rejects_missing_extra_and_unknown_product_type(self) -> None:
        cases = []
        missing = self.product()
        missing.pop("entity_type")
        cases.append(missing)
        extra = self.product()
        extra["private"] = "value"
        cases.append(extra)
        unknown_type = self.product()
        unknown_type["product_type_id"] = "999"
        cases.append(unknown_type)

        for row in cases:
            with self.subTest(keys=sorted(row)):
                with self.assertRaisesRegex(StockError, "manifest_invalid"):
                    product_projection(row, GENERATION_ID)

    def test_characteristic_status_triples_are_strict(self) -> None:
        invalid_items = (
            {
                "normalized_value": "Лето",
                "normalization_status": "known",
                "source_value": None,
            },
            {
                "normalized_value": "Лето",
                "normalization_status": "missing",
                "source_value": None,
            },
            {
                "normalized_value": "Лето",
                "normalization_status": "unknown",
                "source_value": "raw",
            },
            {
                "normalized_value": "Весна",
                "normalization_status": "known",
                "source_value": "raw",
            },
        )
        for item in invalid_items:
            with self.subTest(item=item):
                row = self.product()
                row["characteristics"]["season"] = item
                with self.assertRaisesRegex(StockError, "manifest_invalid"):
                    product_projection(row, GENERATION_ID)

    def test_unknown_source_value_is_never_projected(self) -> None:
        row = self.product()
        row["characteristics"]["spikes"] = {
            "normalized_value": None,
            "normalization_status": "unknown",
            "source_value": {"private": ["raw"]},
        }

        projected = product_projection(row, GENERATION_ID)

        self.assertNotIn("spikes", projected.characteristics)
        self.assertIn(
            {"product_id": "1", "characteristic": "spikes", "status": "unknown"},
            projected.unknown_characteristics,
        )
        self.assertNotIn("private", repr(projected))

    def test_nested_null_source_value_matches_canonical_json_domain(self) -> None:
        row = self.product()
        row["characteristics"]["spikes"] = {
            "normalized_value": None,
            "normalization_status": "unknown",
            "source_value": {"nested": [None, "raw"]},
        }

        projected = product_projection(row, GENERATION_ID)

        self.assertNotIn("spikes", projected.characteristics)

    def test_raw_source_uses_private_budget_and_publisher_safe_text_rules(self) -> None:
        for source in ("", "x" * 300, {"safe": [None, ""]}):
            with self.subTest(source=repr(source)[:40]):
                row = self.product()
                row["characteristics"]["spikes"] = {
                    "normalized_value": None,
                    "normalization_status": "unknown",
                    "source_value": source,
                }
                product_projection(row, GENERATION_ID)

        for source in ({"": "value"}, {"safe": "bad\nvalue"}):
            with self.subTest(invalid=repr(source)):
                row = self.product()
                row["characteristics"]["spikes"] = {
                    "normalized_value": None,
                    "normalization_status": "unknown",
                    "source_value": source,
                }
                with self.assertRaisesRegex(StockError, "manifest_invalid"):
                    product_projection(row, GENERATION_ID)

    def test_public_product_and_supplier_text_reject_control_and_format_chars(self) -> None:
        for value in ("bad\nname", "bad\u200ename"):
            row = self.product()
            row["name"] = value
            with self.assertRaisesRegex(StockError, "manifest_invalid"):
                product_projection(row, GENERATION_ID)

            offer = self.offer()
            offer["supplier_name"] = value
            with self.assertRaisesRegex(StockError, "manifest_invalid"):
                offer_projection(offer, GENERATION_ID)

    def test_offer_uses_only_positive_sale_price(self) -> None:
        projected = offer_projection(self.offer(), GENERATION_ID)
        self.assertIsNotNone(projected)
        assert projected is not None
        self.assertEqual(str(projected.price), "7000")
        self.assertEqual(projected.supplier, "Synthetic Supplier A")

        no_sale = self.offer()
        no_sale["price_sale"] = None
        no_sale["price_sale_source"] = None
        self.assertIsNone(offer_projection(no_sale, GENERATION_ID))

    def test_offer_accepts_numeric_lexeme_provenance_for_exact_prices(self) -> None:
        for source_field in ("price_input_source", "price_sale_source"):
            with self.subTest(source_field=source_field):
                row = self.offer()
                row[source_field] = "json_numeric_lexeme"

                projected = offer_projection(row, GENERATION_ID)

                self.assertIsNotNone(projected)
                assert projected is not None
                self.assertEqual(str(projected.price), "7000")

    def test_offer_does_not_fallback_to_purchase_price_or_private_article(self) -> None:
        row = self.offer()
        row["price_input"] = "1"
        row["price_sale"] = None
        row["price_sale_source"] = None
        row["supplier_article"] = "MUST-NOT-BE-PUBLIC"

        self.assertIsNone(offer_projection(row, GENERATION_ID))

    def test_offer_sale_pair_and_boundaries_are_strict(self) -> None:
        invalid_pairs = (
            (None, "json_integer"),
            ("1", None),
            ("0", "json_integer"),
            ("-1", "json_integer"),
            ("-0", "json_integer"),
            ("1e3", "json_decimal_string"),
            ("1.0", "json_decimal_string"),
            ("1", "unexpected"),
            ("1" + "0" * 129, "json_integer"),
        )
        for price, source in invalid_pairs:
            with self.subTest(price=price, source=source):
                row = self.offer()
                row["price_sale"] = price
                row["price_sale_source"] = source
                with self.assertRaisesRegex(StockError, "manifest_invalid"):
                    offer_projection(row, GENERATION_ID)

    def test_nullable_supplier_is_valid_but_not_public(self) -> None:
        row = self.offer()
        row["supplier_name"] = None
        self.assertIsNone(offer_projection(row, GENERATION_ID))

    def test_nullable_delivery_is_preserved(self) -> None:
        row = self.offer()
        row["delivery_days"] = None
        row["delivery_date"] = None
        projected = offer_projection(row, GENERATION_ID)
        self.assertIsNotNone(projected)
        assert projected is not None
        self.assertIsNone(projected.delivery_days)

    def test_supplier_article_provenance_is_strict(self) -> None:
        row = self.offer()
        row["supplier_article_source"] = "brand_articul"
        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            offer_projection(row, GENERATION_ID)

    def test_offer_exact_fields_and_canonical_timestamps_are_strict(self) -> None:
        missing = self.offer()
        missing.pop("warehouse_name")
        extra = self.offer()
        extra["private"] = "value"
        bad_timestamp = self.offer()
        bad_timestamp["modified_at"] = "2026-08-29T10:00:00.1+05:00"
        z_timestamp = self.offer()
        z_timestamp["modified_at"] = "2026-08-29T05:00:00Z"

        for row in (missing, extra, bad_timestamp, z_timestamp):
            with self.assertRaisesRegex(StockError, "manifest_invalid"):
                offer_projection(row, GENERATION_ID)


class RobotyreV1ManifestTest(unittest.TestCase):
    def manifest(self) -> dict[str, object]:
        value = json.loads(
            (FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8")
        )
        assert isinstance(value, dict)
        return value

    def parse(self, value: dict[str, object]) -> Manifest:
        return Manifest.parse(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        )

    def test_canonical_filename_keys_and_generation_are_used(self) -> None:
        manifest = self.parse(self.manifest())
        self.assertEqual(manifest.generation_id, GENERATION_ID)
        self.assertEqual(
            manifest.products.url, "/robotyre-stock/v1/products.jsonl"
        )
        self.assertEqual(manifest.offers.url, "/robotyre-stock/v1/offers.jsonl")
        self.assertEqual(manifest.archive.url, "/robotyre-stock/v1/archive.zip")

    def test_foreign_contract_or_version_has_distinct_error(self) -> None:
        for field, value in (("contract", "robotyre-stock/v2"), ("schema_version", "2")):
            with self.subTest(field=field):
                manifest = self.manifest()
                manifest[field] = value
                with self.assertRaisesRegex(StockError, "contract_unsupported"):
                    self.parse(manifest)

    def test_legacy_manifest_and_malformed_v1_are_rejected(self) -> None:
        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            Manifest.parse(b'{"generation_id":"legacy"}')

        for mutation in ("missing_file", "extra_file", "bad_archive"):
            with self.subTest(mutation=mutation):
                manifest = self.manifest()
                files = manifest["files"]
                assert isinstance(files, dict)
                if mutation == "missing_file":
                    files.pop("offers.jsonl")
                elif mutation == "extra_file":
                    files["private.jsonl"] = copy.deepcopy(files["products.jsonl"])
                else:
                    files["archive.zip"]["url"] = "https://other.test/archive.zip"
                with self.assertRaisesRegex(StockError, "manifest_invalid"):
                    self.parse(manifest)

    def test_manifest_rejects_wrong_timestamp_order_and_future_clock(self) -> None:
        reversed_time = self.manifest()
        reversed_time["generated_at"] = "2026-08-28T21:41:00+00:00"
        reversed_time["checked_at"] = "2026-08-28T21:40:00+00:00"
        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            self.parse(reversed_time)

        future = self.manifest()
        future["generated_at"] = "2999-01-01T00:00:00+00:00"
        future["checked_at"] = "2999-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            self.parse(future)

    def test_manifest_rejects_payload_size_above_download_budget(self) -> None:
        manifest = self.manifest()
        manifest["files"]["products.jsonl"]["bytes"] = 16 * 1024**3 + 1

        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            self.parse(manifest)


class RobotyreV1EndToEndTest(unittest.TestCase):
    def test_refresh_activation_and_search_use_canonical_public_projection(self) -> None:
        products, offers = payloads()
        product_rows = [
            json.loads(line) for line in products.decode("utf-8").splitlines()
        ]
        normalized_characteristic_values = {
            "season": "Лето",
            "all_season": True,
            "spikes": True,
            "run_flat": False,
            "disk_type": "Литой",
            "truck_tire_axis": "Рулевая",
            "truck_tire_construction": "Радиальная",
        }
        for name, value in normalized_characteristic_values.items():
            product_rows[0]["characteristics"][name] = {
                "normalized_value": value,
                "normalization_status": "known",
                "source_value": value,
            }
        products = (
            "\n".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                for row in product_rows
            )
            + "\n"
        ).encode("utf-8")
        offer_rows = [
            json.loads(line) for line in offers.decode("utf-8").splitlines()
        ]
        offer_rows[0]["price_input_source"] = "json_numeric_lexeme"
        offer_rows[0]["price_sale_source"] = "json_numeric_lexeme"
        offers = (
            "\n".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                for row in offer_rows
            )
            + "\n"
        ).encode("utf-8")
        manifest = dynamic_manifest_bytes(products, offers)

        class Client:
            def __init__(self) -> None:
                self.download_calls: list[str] = []

            def get_manifest(
                self, etag: str | None = None, last_modified: str | None = None
            ) -> HttpResponse:
                return HttpResponse(status=200, headers={}, body=manifest)

            def download(
                self,
                url: str,
                destination: Path,
                expected_bytes: int,
                expected_sha256: str,
                progress: object | None = None,
            ) -> DownloadReceipt:
                self.download_calls.append(url)
                payload = products if url.endswith("products.jsonl") else offers
                destination.write_bytes(payload)
                return DownloadReceipt(
                    bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                )

        with tempfile.TemporaryDirectory() as temporary:
            cache_dir = Path(temporary) / "cache"
            config = StockConfig(
                manifest_url="https://stock.example.test/robotyre-stock/v1/manifest.json",
                username="synthetic-user",
                password="synthetic-password",
                product_id_field="robotyre_product_id",
                offer_product_id_field="robotyre_product_id",
                cache_dir=cache_dir,
            )
            client = Client()
            refreshed = StockCache(cache_dir, client).refresh(config)
            with StockCache(cache_dir, object()).generation_snapshot() as snapshot:
                public = StockSearcher(snapshot, config).search(
                    SearchQuery.from_args(argparse.Namespace())
                ).to_public_dict()

        self.assertEqual(refreshed.status, "updated")
        self.assertEqual(public["status"], "ok")
        product = next(
            item for item in public["products"] if item["product_id"] == "1"
        )
        self.assertEqual(product["minimum_price"], "6500")
        self.assertEqual(
            product["characteristics"],
            {
                "season": "Лето",
                "all_season": "Да",
                "spikes": "Да",
                "run_flat": "Нет",
                "disk_type": "Литой",
                "truck_axis": "Рулевая",
                "truck_construction": "Радиальная",
            },
        )
        self.assertNotIn("source_value", json.dumps(public, ensure_ascii=False))
        self.assertEqual(len(client.download_calls), 2)
        self.assertFalse(any(url.endswith("archive.zip") for url in client.download_calls))


if __name__ == "__main__":
    unittest.main()
