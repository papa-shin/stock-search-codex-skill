from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
        cache_directory = self.directory / "cache" / "stock"
        config_path = self.write_env(
            "# literal configuration only\n"
            + self.complete_env(
                "PAPA_SHIN_STOCK_USERNAME='quoted user'\n"
                f"PAPA_SHIN_STOCK_CACHE_DIR={cache_directory}\n"
            )
        )

        config = StockConfig.load(config_path)

        self.assertEqual(config.username, "quoted user")
        self.assertEqual(config.cache_dir, cache_directory.resolve())

    def test_missing_config_file_has_distinct_safe_error(self) -> None:
        missing_path = self.directory / "private-secret-name.env"

        with self.assertRaisesRegex(StockError, "config_missing") as raised:
            StockConfig.load(missing_path)

        self.assertEqual(raised.exception.exit_code, 2)
        self.assertNotIn(str(missing_path), str(raised.exception))

    def test_unreadable_config_file_remains_safe_invalid_error(self) -> None:
        config_path = self.directory / "private-secret-name.env"

        with patch.object(Path, "read_text", side_effect=PermissionError(config_path)):
            with self.assertRaisesRegex(StockError, "config_invalid") as raised:
                StockConfig.load(config_path)

        self.assertNotIn(str(config_path), str(raised.exception))

    def test_manifest_url_is_validated_during_config_load(self) -> None:
        invalid_urls = (
            "http://example.test/manifest.json",
            "https://user:password@example.test/manifest.json",
            "https://example.test:invalid/manifest.json",
            "https://example.test:65536/manifest.json",
            "https://example.test/manifest\tname.json",
            "https://example.test/manifest\u0085name.json",
        )

        for manifest_url in invalid_urls:
            with self.subTest(manifest_url=manifest_url):
                config_path = self.write_env(
                    self.complete_env(
                        f"PAPA_SHIN_STOCK_MANIFEST_URL={manifest_url}\n"
                    )
                )

                with self.assertRaisesRegex(StockError, "config_invalid") as raised:
                    StockConfig.load(config_path)

                self.assertNotIn("user:password", str(raised.exception))
                self.assertNotIn(manifest_url, str(raised.exception))

    def test_relative_cache_directory_is_rejected_without_path_disclosure(self) -> None:
        config_path = self.write_env(
            self.complete_env("PAPA_SHIN_STOCK_CACHE_DIR=relative/private/cache\n")
        )

        with self.assertRaisesRegex(StockError, "config_invalid") as raised:
            StockConfig.load(config_path)

        self.assertNotIn("relative/private/cache", str(raised.exception))

    def test_cache_directory_inside_skill_package_is_rejected(self) -> None:
        private_path = SCRIPTS_DIR.parent / "private-cache"
        config_path = self.write_env(
            self.complete_env(f"PAPA_SHIN_STOCK_CACHE_DIR={private_path}\n")
        )

        with self.assertRaisesRegex(StockError, "config_invalid") as raised:
            StockConfig.load(config_path)

        self.assertNotIn(str(private_path), str(raised.exception))

    def test_cache_directory_symlink_resolving_inside_skill_is_rejected(self) -> None:
        package_alias = self.directory / "package-alias"
        try:
            package_alias.symlink_to(SCRIPTS_DIR.parent, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink недоступен: {type(error).__name__}")
        private_path = package_alias / "private-cache"
        config_path = self.write_env(
            self.complete_env(f"PAPA_SHIN_STOCK_CACHE_DIR={private_path}\n")
        )

        with self.assertRaisesRegex(StockError, "config_invalid") as raised:
            StockConfig.load(config_path)

        self.assertNotIn(str(private_path), str(raised.exception))

    def test_broad_cache_directories_are_rejected(self) -> None:
        skill_root = SCRIPTS_DIR.parent.resolve()
        broad_paths = {
            Path(skill_root.anchor),
            Path(skill_root.anchor) / "tmp",
            Path.home().resolve(),
            Path.home().resolve().parent,
            Path.home().resolve() / ".codex",
            Path.home().resolve() / ".codex" / "cache",
            skill_root,
            skill_root.parent,
            skill_root / "nested-cache",
        }
        for cache_directory in broad_paths:
            with self.subTest(cache_directory=cache_directory):
                config_path = self.write_env(
                    self.complete_env(
                        f"PAPA_SHIN_STOCK_CACHE_DIR={cache_directory}\n"
                    )
                )
                with self.assertRaisesRegex(StockError, "config_invalid"):
                    StockConfig.load(config_path)

    def test_leaf_cache_directory_under_home_is_allowed(self) -> None:
        cache_directory = Path.home() / ".codex" / "cache" / "papa-shin-stock-test"
        config = StockConfig.load(
            self.write_env(
                self.complete_env(
                    f"PAPA_SHIN_STOCK_CACHE_DIR={cache_directory}\n"
                )
            )
        )
        self.assertEqual(config.cache_dir, cache_directory.resolve())

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

        for row in (
            {},
            {"product_id": ""},
            {"product_id": None},
            {"product_id": True},
            {"product_id": 1.5},
        ):
            with self.subTest(row=row):
                with self.assertRaisesRegex(StockError, "query_invalid") as raised:
                    config.resolve_product_id(row)

                self.assertEqual(raised.exception.exit_code, 4)


if __name__ == "__main__":
    unittest.main()
