from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError


class StockConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.directory = Path(self.temp_dir.name)

    def write_env(self, contents: str) -> Path:
        config_path = self.directory / "stock.env"
        config_path.write_text(contents, encoding="utf-8")
        return config_path

    def complete_env(self, extra: str = "") -> str:
        return (
            "PAPA_SHIN_STOCK_MANIFEST_URL=https://example.test/manifest.json\n"
            "PAPA_SHIN_STOCK_USERNAME=test-user\n"
            "PAPA_SHIN_STOCK_PASSWORD=test-password\n"
            "PAPA_SHIN_STOCK_PRODUCT_ID_FIELD=product_id\n"
            "PAPA_SHIN_STOCK_OFFER_PRODUCT_ID_FIELD=offer_id\n"
            + extra
        )

    def test_load_does_not_expand_shell_syntax(self) -> None:
        config_path = self.write_env(
            self.complete_env('PAPA_SHIN_STOCK_USERNAME="$(unsafe)"\n')
        )

        config = StockConfig.load(config_path)

        self.assertEqual(config.username, "$(unsafe)")

    def test_missing_required_mapping_is_safe_error(self) -> None:
        config_path = self.write_env(
            "PAPA_SHIN_STOCK_MANIFEST_URL=https://example.test/manifest.json\n"
        )

        with self.assertRaisesRegex(StockError, "config_invalid") as raised:
            StockConfig.load(config_path)

        self.assertNotIn("test-user", str(raised.exception))
        self.assertNotIn("test-password", str(raised.exception))

    def test_load_accepts_comments_quotes_and_optional_cache_directory(self) -> None:
        config_path = self.write_env(
            "# literal configuration only\n"
            + self.complete_env(
                "PAPA_SHIN_STOCK_USERNAME='quoted user'\n"
                "PAPA_SHIN_STOCK_CACHE_DIR=cache/stock\n"
            )
        )

        config = StockConfig.load(config_path)

        self.assertEqual(config.username, "quoted user")
        self.assertEqual(config.cache_dir, Path("cache/stock"))

    def test_load_rejects_non_assignment_line_with_safe_error(self) -> None:
        config_path = self.write_env(self.complete_env("source unsafe.env\n"))

        with self.assertRaisesRegex(StockError, "config_invalid"):
            StockConfig.load(config_path)

    def test_resolve_product_id_accepts_string_or_integer(self) -> None:
        config = StockConfig.load(self.write_env(self.complete_env()))

        self.assertEqual(config.resolve_product_id({"product_id": "A-12"}), "A-12")
        self.assertEqual(config.resolve_product_id({"product_id": 42}), "42")

    def test_resolve_product_id_rejects_missing_or_empty_value(self) -> None:
        config = StockConfig.load(self.write_env(self.complete_env()))

        for row in ({}, {"product_id": ""}, {"product_id": None}):
            with self.subTest(row=row):
                with self.assertRaisesRegex(StockError, "query_invalid") as raised:
                    config.resolve_product_id(row)

                self.assertEqual(raised.exception.exit_code, 4)


if __name__ == "__main__":
    unittest.main()
