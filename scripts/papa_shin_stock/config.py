from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from papa_shin_stock.errors import StockError


_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_DEFAULT_CONFIG_PATH = Path.home() / ".codex" / "secrets" / "papa-shin-stock.env"
_DEFAULT_CACHE_DIR = Path.home() / ".codex" / "cache" / "papa-shin-stock"
_SKILL_ROOT = Path(__file__).resolve().parents[2]


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
        manifest_url = _validated_manifest_url(
            require_value(values, "PAPA_SHIN_STOCK_MANIFEST_URL")
        )
        cache_dir = _validated_cache_dir(
            Path(values.get("PAPA_SHIN_STOCK_CACHE_DIR", _DEFAULT_CACHE_DIR))
        )
        return cls(
            manifest_url=manifest_url,
            username=require_value(values, "PAPA_SHIN_STOCK_USERNAME"),
            password=require_value(values, "PAPA_SHIN_STOCK_PASSWORD"),
            product_id_field=require_value(values, "PAPA_SHIN_STOCK_PRODUCT_ID_FIELD"),
            offer_product_id_field=require_value(
                values, "PAPA_SHIN_STOCK_OFFER_PRODUCT_ID_FIELD"
            ),
            cache_dir=cache_dir,
        )

    def resolve_product_id(self, row: dict[str, object]) -> str:
        value = row.get(self.product_id_field)
        if type(value) not in (str, int) or str(value) == "":
            raise StockError(
                "manifest_invalid", "Некорректные машинные данные", 3
            )
        return str(value)


def parse_env_file(path: Path) -> dict[str, str]:
    """Read literal KEY=VALUE lines without shell evaluation or expansion."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise StockError("config_missing", "Файл конфигурации не найден", 2) from error
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


def _validated_manifest_url(value: str) -> str:
    if _has_unsafe_url_character(value):
        raise _invalid_config()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError, UnicodeError) as error:
        raise _invalid_config() from error

    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
    ):
        raise _invalid_config()
    return value


def _validated_cache_dir(path: Path) -> Path:
    if not path.is_absolute():
        raise _invalid_config()
    try:
        resolved = path.resolve(strict=False)
        skill_root = _SKILL_ROOT.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise _invalid_config() from error
    home = Path.home().resolve(strict=False)
    default_cache = _DEFAULT_CACHE_DIR.resolve(strict=False)
    anchor = Path(resolved.anchor)
    if (
        resolved == anchor
        or len(resolved.parts) <= 3
        or resolved == home
        or resolved in home.parents
        or (resolved != default_cache and resolved in default_cache.parents)
        or resolved == skill_root
        or skill_root in resolved.parents
        or resolved in skill_root.parents
    ):
        raise _invalid_config()
    return resolved


def _has_unsafe_url_character(value: str) -> bool:
    return any(
        ord(character) <= 32 or 127 <= ord(character) <= 159
        for character in value
    )


def _invalid_config() -> StockError:
    return StockError("config_invalid", "Некорректная конфигурация", 2)
