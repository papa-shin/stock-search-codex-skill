from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from papa_shin_stock.errors import StockError
from papa_shin_stock.validation import is_bounded_unicode_scalar


SCHEMA_VERSION = "1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_POSITIVE_ID = re.compile(r"[1-9][0-9]*\Z")
_NONNEGATIVE_ID = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_POSITIVE_DECIMAL = re.compile(
    r"(?:0\.[0-9]*[1-9]|[1-9][0-9]*(?:\.[0-9]*[1-9])?)\Z"
)
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_CANONICAL_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{2}:[0-9]{2}\Z"
)
_MAX_INTEGER = 2**63 - 1
_MAX_DECIMAL_TEXT = 512
_MAX_SOURCE_DEPTH = 32
_MAX_SOURCE_NODES = 4096
_MAX_SOURCE_TEXT = 2 * 1024 * 1024

PRODUCT_TYPE_NAMES = {
    "172": "Шины",
    "173": "Диски",
    "12371": "Грузовые шины",
    "12372": "Грузовые диски",
    "12373": "Шины для квадроциклов",
}

_CHARACTERISTIC_DOMAINS: dict[str, object] = {
    "season": {"Лето", "Зима"},
    "all_season": bool,
    "spikes": bool,
    "run_flat": bool,
    "disk_type": {"Кованый", "Литой", "Стальной"},
    "truck_tire_axis": str,
    "truck_tire_construction": {"Диагональная", "Радиальная"},
}
_PUBLIC_CHARACTERISTIC_NAMES = {
    "season": "season",
    "all_season": "all_season",
    "spikes": "spikes",
    "run_flat": "run_flat",
    "disk_type": "disk_type",
    "truck_tire_axis": "truck_axis",
    "truck_tire_construction": "truck_construction",
}

_PRODUCT_KEYS = {
    "schema_version",
    "content_generation_id",
    "robotyre_product_id",
    "entity_type",
    "product_type_id",
    "brand",
    "model",
    "name",
    "brand_articul",
    "articul_robotyre",
    "characteristics",
    "offer_count",
    "total_quantity",
    "min_price_input",
    "min_price_input_source",
    "min_price_sale",
    "min_price_sale_source",
    "suppliers_all_updated_at",
    "suppliers_all_checked_at",
    "snapshot_source",
}
_OFFER_KEYS = {
    "schema_version",
    "content_generation_id",
    "robotyre_product_id",
    "supplier_article",
    "supplier_article_source",
    "supplier_name",
    "warehouse_name",
    "quantity",
    "price_input",
    "price_input_source",
    "price_sale",
    "price_sale_source",
    "is_sale",
    "delivery_days",
    "delivery_date",
    "organization_supplier_id",
    "warehouse_external_id",
    "supplier_id",
    "price_last_updated_at",
    "modified_at",
    "snapshot_source",
}


@dataclass(frozen=True, slots=True)
class ProductProjection:
    product_id: str
    name: str
    article: str
    product_type: str
    characteristics: dict[str, str]
    total_quantity: int
    unknown_characteristics: tuple[dict[str, str], ...]
    filter_values: dict[str, str]


@dataclass(frozen=True, slots=True)
class OfferProjection:
    product_id: str
    supplier: str
    price: Decimal
    delivery_days: int | None
    quantity: int


def product_projection(
    row: dict[str, object], expected_generation_id: str
) -> ProductProjection:
    _envelope(row, _PRODUCT_KEYS, expected_generation_id)
    product_id = _positive_id(row["robotyre_product_id"])
    _required_text(row["entity_type"])
    product_type_id = _positive_id(row["product_type_id"])
    try:
        product_type = PRODUCT_TYPE_NAMES[product_type_id]
    except KeyError as error:
        raise _invalid() from error

    brand = _nullable_text(row["brand"])
    model = _nullable_text(row["model"])
    source_name = _nullable_text(row["name"])
    brand_article = _nullable_text(row["brand_articul"])
    _nullable_text(row["articul_robotyre"])
    offer_count = _positive_integer(row["offer_count"])
    total_quantity = _positive_integer(row["total_quantity"])
    if offer_count < 1 or total_quantity < 1:
        raise _invalid()
    _positive_decimal_text(row["min_price_input"])
    if row["min_price_input_source"] != "canonical_exact_minimum":
        raise _invalid()
    _nullable_decimal_pair(
        row["min_price_sale"],
        row["min_price_sale_source"],
        {"canonical_exact_minimum"},
        require_positive=False,
    )
    _nullable_timestamp(row["suppliers_all_updated_at"])
    _nullable_timestamp(row["suppliers_all_checked_at"])
    if row["snapshot_source"] != "robotyre_products.suppliers_all":
        raise _invalid()

    characteristics, filters, unknown = _characteristics(
        row["characteristics"], product_id
    )
    if source_name is not None:
        name = source_name
    else:
        name = " ".join(part for part in (brand, model) if part is not None)
        if not name:
            name = f"Товар Robotyre #{product_id}"
    return ProductProjection(
        product_id=product_id,
        name=name,
        article=brand_article or "",
        product_type=product_type,
        characteristics=characteristics,
        total_quantity=total_quantity,
        unknown_characteristics=unknown,
        filter_values=filters,
    )


def offer_projection(
    row: dict[str, object], expected_generation_id: str
) -> OfferProjection | None:
    _envelope(row, _OFFER_KEYS, expected_generation_id)
    product_id = _positive_id(row["robotyre_product_id"])
    supplier_article = _nullable_text(row["supplier_article"])
    supplier_article_source = row["supplier_article_source"]
    if supplier_article is None:
        if supplier_article_source is not None:
            raise _invalid()
    elif supplier_article_source != "product_supplier_articul":
        raise _invalid()
    supplier = _nullable_text(row["supplier_name"])
    _nullable_text(row["warehouse_name"])
    quantity = _positive_integer(row["quantity"])
    _positive_decimal_text(row["price_input"])
    if row["price_input_source"] not in {"json_integer", "json_decimal_string"}:
        raise _invalid()
    sale = _nullable_decimal_pair(
        row["price_sale"],
        row["price_sale_source"],
        {"json_integer", "json_decimal_string"},
        require_positive=True,
    )
    if row["is_sale"] is not None and type(row["is_sale"]) is not bool:
        raise _invalid()
    delivery_days = row["delivery_days"]
    if delivery_days is not None:
        delivery_days = _nonnegative_integer(delivery_days)
    _nullable_date(row["delivery_date"])
    for name in (
        "organization_supplier_id",
        "warehouse_external_id",
        "supplier_id",
    ):
        _nullable_nonnegative_id(row[name])
    _nullable_timestamp(row["price_last_updated_at"])
    _nullable_timestamp(row["modified_at"])
    if row["snapshot_source"] != "robotyre_products.suppliers_all":
        raise _invalid()
    if supplier is None or sale is None:
        return None
    return OfferProjection(product_id, supplier, sale, delivery_days, quantity)


def _envelope(
    row: dict[str, object], expected_keys: set[str], expected_generation_id: str
) -> None:
    if set(row) != expected_keys or row.get("schema_version") != SCHEMA_VERSION:
        raise _invalid()
    generation_id = row.get("content_generation_id")
    if not isinstance(generation_id, str) or not _SHA256.fullmatch(generation_id):
        raise _invalid()
    if generation_id != expected_generation_id:
        raise StockError(
            "generation_mismatch", "Поколение данных не согласовано", 5
        )


def _characteristics(
    value: object, product_id: str
) -> tuple[dict[str, str], dict[str, str], tuple[dict[str, str], ...]]:
    if not isinstance(value, dict) or set(value) != set(_CHARACTERISTIC_DOMAINS):
        raise _invalid()
    public: dict[str, str] = {}
    filters: dict[str, str] = {}
    unknown: list[dict[str, str]] = []
    for source_name, domain in _CHARACTERISTIC_DOMAINS.items():
        item = value[source_name]
        if not isinstance(item, dict) or set(item) != {
            "normalized_value",
            "normalization_status",
            "source_value",
        }:
            raise _invalid()
        normalized = item["normalized_value"]
        status = item["normalization_status"]
        source = item["source_value"]
        public_name = _PUBLIC_CHARACTERISTIC_NAMES[source_name]
        if status == "known":
            _known_characteristic(normalized, domain)
            if source is None:
                raise _invalid()
            _source_value(source)
            projected = _project_characteristic(normalized)
            public[public_name] = projected
            filters[public_name] = projected
        elif status == "missing":
            if normalized is not None or source is not None:
                raise _invalid()
            unknown.append(
                {
                    "product_id": product_id,
                    "characteristic": public_name,
                    "status": "missing",
                }
            )
        elif status == "unknown":
            if normalized is not None or source is None:
                raise _invalid()
            _source_value(source)
            unknown.append(
                {
                    "product_id": product_id,
                    "characteristic": public_name,
                    "status": "unknown",
                }
            )
        else:
            raise _invalid()
    return public, filters, tuple(unknown)


def _known_characteristic(value: object, domain: object) -> None:
    if domain is bool:
        if type(value) is not bool:
            raise _invalid()
        return
    if domain is str:
        _required_text(value)
        return
    if not isinstance(domain, set) or not isinstance(value, str) or value not in domain:
        raise _invalid()


def _project_characteristic(value: object) -> str:
    if type(value) is bool:
        return "Да" if value else "Нет"
    return _required_text(value)


def _source_value(value: object) -> None:
    remaining = [(value, 0)]
    nodes = 0
    while remaining:
        current, depth = remaining.pop()
        nodes += 1
        if nodes > _MAX_SOURCE_NODES or depth > _MAX_SOURCE_DEPTH:
            raise _invalid()
        if current is None:
            continue
        if type(current) in (bool, int):
            continue
        if isinstance(current, str):
            _source_text(current, allow_empty=True)
            continue
        if isinstance(current, list):
            remaining.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            for key, item in current.items():
                _source_text(key, allow_empty=False)
                remaining.append((item, depth + 1))
            continue
        raise _invalid()


def _required_text(value: object) -> str:
    if not is_bounded_unicode_scalar(value) or _has_control_or_format(value):
        raise _invalid()
    assert isinstance(value, str)
    return value


def _source_text(value: object, *, allow_empty: bool) -> str:
    if (
        not is_bounded_unicode_scalar(
            value,
            maximum=_MAX_SOURCE_TEXT,
            allow_empty=allow_empty,
        )
        or _has_control_or_format(value)
    ):
        raise _invalid()
    assert isinstance(value, str)
    return value


def _has_control_or_format(value: object) -> bool:
    return isinstance(value, str) and any(
        unicodedata.category(character) in {"Cc", "Cf"}
        for character in value
    )


def _nullable_text(value: object) -> str | None:
    return None if value is None else _required_text(value)


def _positive_id(value: object) -> str:
    if not isinstance(value, str) or not _POSITIVE_ID.fullmatch(value):
        raise _invalid()
    return value


def _nullable_nonnegative_id(value: object) -> None:
    if value is not None and (
        not isinstance(value, str) or not _NONNEGATIVE_ID.fullmatch(value)
    ):
        raise _invalid()


def _positive_integer(value: object) -> int:
    if type(value) is not int or value < 1 or value > _MAX_INTEGER:
        raise _invalid()
    return value


def _nonnegative_integer(value: object) -> int:
    if type(value) is not int or value < 0 or value > _MAX_INTEGER:
        raise _invalid()
    return value


def _positive_decimal_text(value: object) -> Decimal:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_DECIMAL_TEXT
        or not _POSITIVE_DECIMAL.fullmatch(value)
    ):
        raise _invalid()
    return _bounded_decimal(value, require_positive=True)


def _nullable_decimal_pair(
    value: object,
    source: object,
    sources: set[str],
    *,
    require_positive: bool,
) -> Decimal | None:
    if value is None:
        if source is not None:
            raise _invalid()
        return None
    if (
        not isinstance(value, str)
        or len(value) > _MAX_DECIMAL_TEXT
        or not _DECIMAL.fullmatch(value)
        or value == "-0"
        or source not in sources
    ):
        raise _invalid()
    return _bounded_decimal(value, require_positive=require_positive)


def _bounded_decimal(value: str, *, require_positive: bool) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise _invalid() from error
    if (
        not parsed.is_finite()
        or (require_positive and parsed <= 0)
        or (not parsed.is_zero() and abs(parsed.adjusted()) > 128)
    ):
        raise _invalid()
    return parsed


def _nullable_timestamp(value: object) -> None:
    if value is None:
        return
    try:
        if not isinstance(value, str) or not _CANONICAL_TIMESTAMP.fullmatch(value):
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.isoformat(timespec="seconds") != value:
            raise ValueError
    except ValueError as error:
        raise _invalid() from error


def _nullable_date(value: object) -> None:
    if value is None:
        return
    try:
        if not isinstance(value, str) or not _DATE.fullmatch(value):
            raise ValueError
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise _invalid() from error


def _invalid() -> StockError:
    return StockError("manifest_invalid", "Некорректные машинные данные", 3)
