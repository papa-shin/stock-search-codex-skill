from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from papa_shin_stock.cache import GenerationFiles
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.query import SearchQuery, normalize_tire_size


_PRODUCT_FILTERS = (
    "product_type",
    "season",
    "spikes",
    "run_flat",
    "disk_type",
    "truck_axis",
    "truck_construction",
)
_UNKNOWN_STATUSES = {"unknown", "missing"}
_MAX_CANDIDATES = 10_000


def assert_generation(row: dict[str, object], expected: str) -> None:
    if row.get("content_generation_id") != expected:
        raise StockError("generation_mismatch", "Поколение данных не согласовано", 5)


@dataclass(frozen=True, slots=True)
class Offer:
    supplier: str
    price: Decimal
    delivery_days: int
    quantity: int

    def sort_key(self) -> tuple[Decimal, int, int]:
        return self.price, self.delivery_days, self.quantity

    def to_public_dict(self) -> dict[str, object]:
        return {
            "supplier": self.supplier,
            "price": _decimal_text(self.price),
            "delivery_days": self.delivery_days,
            "quantity": self.quantity,
        }


@dataclass(frozen=True, slots=True)
class Product:
    product_id: str
    name: str
    article: str
    product_type: str
    characteristics: dict[str, object]
    total_quantity: int
    unknown_characteristics: tuple[dict[str, str], ...]

    def to_public_dict(self, offers: tuple[Offer, ...]) -> dict[str, object]:
        return {
            "product_id": self.product_id,
            "name": self.name,
            "article": self.article,
            "product_type": self.product_type,
            "characteristics": self.characteristics,
            "total_quantity": self.total_quantity,
            "minimum_price": _decimal_text(offers[0].price) if offers else None,
            "offers": [offer.to_public_dict() for offer in offers],
        }


@dataclass(frozen=True, slots=True)
class SearchSummary:
    sku_count: int
    total_quantity: int

    def to_public_dict(self) -> dict[str, int]:
        return {"sku_count": self.sku_count, "total_quantity": self.total_quantity}


@dataclass(frozen=True, slots=True)
class SearchResult:
    generation: dict[str, object]
    filters: dict[str, object]
    summary: SearchSummary
    products: tuple[Product, ...]
    offers: dict[str, tuple[Offer, ...]]
    unknown_characteristics: tuple[dict[str, str], ...]
    warnings: tuple[dict[str, str], ...] = ()
    status: str = "ok"

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "generation": self.generation,
            "filters": self.filters,
            "summary": self.summary.to_public_dict(),
            "products": [
                product.to_public_dict(self.offers.get(product.product_id, ()))
                for product in self.products
            ],
            "unknown_characteristics": list(self.unknown_characteristics),
            "warnings": list(self.warnings),
        }


class StockSearcher:
    def __init__(self, files: GenerationFiles, config: StockConfig) -> None:
        self.files = files
        self.config = config

    def search(self, query: SearchQuery) -> SearchResult:
        generation = _read_generation_metadata(self.files)
        candidates = self._read_matching_products(query)
        offers = self._read_matching_offers(candidates, query)
        return self._build_result(query, generation, candidates, offers)

    def _read_matching_products(self, query: SearchQuery) -> dict[str, Product]:
        candidates: dict[str, Product] = {}
        for row in _jsonl_rows(self.files.products):
            assert_generation(row, self.files.generation_id)
            product_id = self.config.resolve_product_id(row)
            product = _product_from_row(row, product_id)
            if not _matches_product(product, row, query):
                continue
            if product_id in candidates:
                raise StockError("manifest_invalid", "Некорректные данные товаров", 3)
            if len(candidates) >= _MAX_CANDIDATES:
                raise StockError("query_invalid", "Слишком много товаров по заданным фильтрам", 4)
            candidates[product_id] = product
        return candidates

    def _read_matching_offers(
        self, candidates: dict[str, Product], query: SearchQuery
    ) -> dict[str, tuple[Offer, ...]]:
        selected: dict[str, list[Offer]] = {product_id: [] for product_id in candidates}
        for row in _jsonl_rows(self.files.offers):
            assert_generation(row, self.files.generation_id)
            product_id = _resolve_offer_product_id(row, self.config.offer_product_id_field)
            offers = selected.get(product_id)
            if offers is None:
                continue
            offer = _offer_from_row(row)
            if not _matches_offer(offer, query):
                continue
            offers.append(offer)
            offers.sort(key=Offer.sort_key)
            del offers[query.offers_limit:]
        return {product_id: tuple(values) for product_id, values in selected.items()}

    def _build_result(
        self,
        query: SearchQuery,
        generation: dict[str, object],
        candidates: dict[str, Product],
        offers: dict[str, tuple[Offer, ...]],
    ) -> SearchResult:
        requires_matching_offer = any(
            value is not None
            for value in (query.supplier, query.max_price, query.max_delivery_days)
        )
        products = [
            product
            for product_id, product in candidates.items()
            if not requires_matching_offer or offers[product_id]
        ]
        products.sort(key=lambda product: _product_sort_key(product, offers[product.product_id]))
        products = products[: query.limit]
        selected_ids = {product.product_id for product in products}
        selected_offers = {
            product_id: values
            for product_id, values in offers.items()
            if product_id in selected_ids
        }
        unknown_characteristics = tuple(
            unknown
            for product in products
            for unknown in product.unknown_characteristics
        )
        return SearchResult(
            generation=generation,
            filters=query.public_filters(),
            summary=SearchSummary(
                sku_count=len(products),
                total_quantity=sum(product.total_quantity for product in products),
            ),
            products=tuple(products),
            offers=selected_offers,
            unknown_characteristics=unknown_characteristics,
        )


def _jsonl_rows(path: Path):
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    row = _parse_json(line)
                except (json.JSONDecodeError, ValueError, TypeError, RecursionError) as error:
                    raise StockError("manifest_invalid", "Некорректные машинные данные", 3) from error
                if not isinstance(row, dict):
                    raise StockError("manifest_invalid", "Некорректные машинные данные", 3)
                yield row
    except StockError:
        raise
    except (OSError, UnicodeError) as error:
        raise StockError("cache_unavailable", "Проверенный кэш недоступен", 7) from error


def _read_generation_metadata(files: GenerationFiles) -> dict[str, object]:
    try:
        manifest = _parse_json(files.manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        raise StockError("cache_unavailable", "Проверенный кэш недоступен", 7) from error
    if not isinstance(manifest, dict):
        raise StockError("manifest_invalid", "Некорректный manifest", 3)
    generated_at = manifest.get("generated_at")
    if manifest.get("generation_id") != files.generation_id or not isinstance(generated_at, str):
        raise StockError("generation_mismatch", "Поколение данных не согласовано", 5)
    checked_at = generated_at
    state_path = files.manifest.parent / "state.json"
    if state_path.is_file() and not state_path.is_symlink():
        try:
            state = _parse_json(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise StockError("cache_unavailable", "Проверенный кэш недоступен", 7) from error
        if not isinstance(state, dict) or state.get("generation_id") != files.generation_id:
            raise StockError("generation_mismatch", "Поколение данных не согласовано", 5)
        stored_checked_at = state.get("checked_at")
        if not isinstance(stored_checked_at, str):
            raise StockError("cache_unavailable", "Проверенный кэш недоступен", 7)
        checked_at = stored_checked_at
    return {
        "id": files.generation_id,
        "generated_at": generated_at,
        "checked_at": checked_at,
        "stale": False,
    }


def _product_from_row(row: dict[str, object], product_id: str) -> Product:
    name = _required_text(row, "name", "Некорректные данные товаров")
    article = _required_text(row, "article", "Некорректные данные товаров")
    product_type = _required_text(row, "product_type", "Некорректные данные товаров")
    characteristics = row.get("characteristics", {})
    if not isinstance(characteristics, dict):
        raise StockError("manifest_invalid", "Некорректные данные товаров", 3)
    return Product(
        product_id=product_id,
        name=name,
        article=article,
        product_type=product_type,
        characteristics=_public_characteristics(characteristics),
        total_quantity=_nonnegative_int(row.get("total_quantity"), "Некорректные данные товаров"),
        unknown_characteristics=_unknown_characteristics(product_id, row, characteristics),
    )


def _matches_product(product: Product, row: dict[str, object], query: SearchQuery) -> bool:
    if product.total_quantity < query.min_total_quantity:
        return False
    if query.size is not None:
        size = _known_text(row.get("size"))
        if size is None or normalize_tire_size(size) != query.size:
            return False
    for field in _PRODUCT_FILTERS:
        expected = getattr(query, field)
        if expected is None:
            continue
        actual = product.product_type if field == "product_type" else _known_text(row.get(field))
        if actual != expected:
            return False
    return True


def _resolve_offer_product_id(row: dict[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, (str, int)) or isinstance(value, bool) or str(value) == "":
        raise StockError("query_invalid", "У предложения отсутствует идентификатор товара", 4)
    return str(value)


def _offer_from_row(row: dict[str, object]) -> Offer:
    price = _decimal(row.get("price"), "Некорректные данные предложений")
    if price < 0:
        raise StockError("manifest_invalid", "Некорректные данные предложений", 3)
    return Offer(
        supplier=_required_text(row, "supplier", "Некорректные данные предложений"),
        price=price,
        delivery_days=_nonnegative_int(row.get("delivery_days"), "Некорректные данные предложений"),
        quantity=_nonnegative_int(row.get("quantity"), "Некорректные данные предложений"),
    )


def _matches_offer(offer: Offer, query: SearchQuery) -> bool:
    if query.supplier is not None and offer.supplier != query.supplier:
        return False
    if query.max_price is not None and offer.price > query.max_price:
        return False
    if query.max_delivery_days is not None and offer.delivery_days > query.max_delivery_days:
        return False
    return True


def _product_sort_key(product: Product, offers: tuple[Offer, ...]) -> tuple[bool, Decimal, int, str]:
    if not offers:
        return True, Decimal(0), -product.total_quantity, product.product_id
    return False, offers[0].price, -product.total_quantity, product.product_id


def _unknown_characteristics(
    product_id: str, row: dict[str, object], characteristics: dict[str, object]
) -> tuple[dict[str, str], ...]:
    unknown: list[dict[str, str]] = []
    for field in ("spikes", "run_flat", "disk_type", "truck_axis", "truck_construction"):
        status = _status(row.get(field))
        if status is not None:
            unknown.append({"product_id": product_id, "characteristic": field, "status": status})
    for field, value in characteristics.items():
        status = _status(value)
        if status is not None:
            unknown.append({"product_id": product_id, "characteristic": str(field), "status": status})
    return tuple(unknown)


def _public_characteristics(value: dict[str, object]) -> dict[str, object]:
    return {str(key): item for key, item in value.items()}


def _status(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    return status if isinstance(status, str) and status in _UNKNOWN_STATUSES else None


def _known_text(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value


def _required_text(row: dict[str, object], field: str, message: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise StockError("manifest_invalid", message, 3)
    return value


def _nonnegative_int(value: object, message: str) -> int:
    if isinstance(value, bool):
        raise StockError("manifest_invalid", message, 3)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise StockError("manifest_invalid", message, 3) from error
    if parsed < 0:
        raise StockError("manifest_invalid", message, 3)
    return parsed


def _decimal(value: object, message: str) -> Decimal:
    if isinstance(value, bool):
        raise StockError("manifest_invalid", message, 3)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise StockError("manifest_invalid", message, 3) from error
    if not parsed.is_finite():
        raise StockError("manifest_invalid", message, 3)
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != value.to_integral() else str(value.quantize(Decimal(1)))


def _parse_json(value: str) -> object:
    return json.loads(value, parse_constant=_reject_nonfinite_json)


def _reject_nonfinite_json(value: str) -> object:
    raise ValueError(f"Unsupported JSON constant: {value}")
