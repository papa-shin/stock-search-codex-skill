from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from papa_shin_stock.errors import StockError


_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_DEFAULT_CONFIG_PATH = Path.home() / ".codex" / "secrets" / "papa-shin-stock.env"
_DEFAULT_CACHE_DIR = Path.home() / ".codex" / "cache" / "papa-shin-stock"


@dataclass(frozen=True, slots=True)
class StockConfig:
    manifest_url: str
    username: str
    password: str
    product_id_field: str
    offer_product_id_field: str
    cache_dir: Path

    @classmethod
    def load(cls, path: Path | None = None) -> "StockConfig":
        config_path = path or Path(
            os.environ.get("PAPA_SHIN_STOCK_CONFIG", _DEFAULT_CONFIG_PATH)
        )
        values = parse_env_file(config_path)
        return cls(
            manifest_url=require_value(values, "PAPA_SHIN_STOCK_MANIFEST_URL"),
            username=require_value(values, "PAPA_SHIN_STOCK_USERNAME"),
            password=require_value(values, "PAPA_SHIN_STOCK_PASSWORD"),
            product_id_field=require_value(values, "PAPA_SHIN_STOCK_PRODUCT_ID_FIELD"),
            offer_product_id_field=require_value(
                values, "PAPA_SHIN_STOCK_OFFER_PRODUCT_ID_FIELD"
            ),
            cache_dir=Path(values.get("PAPA_SHIN_STOCK_CACHE_DIR", _DEFAULT_CACHE_DIR)),
        )

    def resolve_product_id(self, row: dict[str, object]) -> str:
        value = row.get(self.product_id_field)
        if not isinstance(value, (str, int)) or str(value) == "":
            raise StockError("query_invalid", "У товара отсутствует идентификатор", 4)
        return str(value)


def parse_env_file(path: Path) -> dict[str, str]:
    """Read literal KEY=VALUE lines without shell evaluation or expansion."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise StockError("config_invalid", "Не удалось прочитать конфигурацию", 2) from error

    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise StockError("config_invalid", "Некорректная строка конфигурации", 2)

        key, value = stripped.split("=", 1)
        if not _ENV_KEY.fullmatch(key):
            raise StockError("config_invalid", "Некорректная строка конфигурации", 2)

        values[key] = _parse_value(value)
    return values


def require_value(values: dict[str, str], key: str) -> str:
    value = values.get(key)
    if value is None or value == "":
        raise StockError("config_invalid", "Не задан обязательный параметр конфигурации", 2)
    return value


def _parse_value(value: str) -> str:
    if not value:
        return ""

    quote = value[0]
    if quote not in ("'", '"'):
        return value
    if len(value) < 2 or value[-1] != quote:
        raise StockError("config_invalid", "Некорректная строка конфигурации", 2)
    return value[1:-1]
