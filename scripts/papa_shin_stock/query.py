from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from papa_shin_stock.errors import StockError


_TIRE_SIZE = re.compile(r"^(?P<width>\d{3})\D*(?P<profile>\d{2,3})\D*[Rr]?\D*(?P<rim>\d{2})$")
_MAX_LIMIT = 100
_MAX_OFFERS_LIMIT = 25


def normalize_tire_size(value: str) -> str:
    if not isinstance(value, str):
        raise StockError("query_invalid", "Некорректный типоразмер", 4)
    match = _TIRE_SIZE.fullmatch(value.strip())
    if match is None:
        raise StockError("query_invalid", "Некорректный типоразмер", 4)
    return f"{match['width']}/{match['profile']}R{match['rim']}"


@dataclass(frozen=True, slots=True)
class SearchQuery:
    product_type: str | None
    size: str | None
    season: str | None
    spikes: str | None
    run_flat: str | None
    disk_type: str | None
    truck_axis: str | None
    truck_construction: str | None
    supplier: str | None
    min_total_quantity: int
    max_price: Decimal | None
    max_delivery_days: int | None
    limit: int
    offers_limit: int

    @classmethod
    def from_args(cls, namespace: argparse.Namespace) -> "SearchQuery":
        size = _optional_text(getattr(namespace, "size", None))
        return cls(
            product_type=_optional_text(getattr(namespace, "product_type", None)),
            size=normalize_tire_size(size) if size is not None else None,
            season=_optional_text(getattr(namespace, "season", None)),
            spikes=_optional_text(getattr(namespace, "spikes", None)),
            run_flat=_optional_text(getattr(namespace, "run_flat", None)),
            disk_type=_optional_text(getattr(namespace, "disk_type", None)),
            truck_axis=_optional_text(getattr(namespace, "truck_axis", None)),
            truck_construction=_optional_text(
                getattr(namespace, "truck_construction", None)
            ),
            supplier=_optional_text(getattr(namespace, "supplier", None)),
            min_total_quantity=_nonnegative_int(
                getattr(namespace, "min_total_quantity", 4), "минимальный остаток"
            ),
            max_price=_price_or_none(getattr(namespace, "max_price", None)),
            max_delivery_days=_optional_nonnegative_int(
                getattr(namespace, "max_delivery_days", None), "срок доставки"
            ),
            limit=_positive_bounded_int(
                getattr(namespace, "limit", 10), "лимит товаров", _MAX_LIMIT
            ),
            offers_limit=_positive_bounded_int(
                getattr(namespace, "offers_limit", 5),
                "лимит предложений",
                _MAX_OFFERS_LIMIT,
            ),
        )

    def public_filters(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for key in (
            "product_type",
            "size",
            "season",
            "spikes",
            "run_flat",
            "disk_type",
            "truck_axis",
            "truck_construction",
            "supplier",
        ):
            value = getattr(self, key)
            if value is not None:
                values[key] = value
        values["min_total_quantity"] = self.min_total_quantity
        if self.max_price is not None:
            values["max_price"] = _decimal_text(self.max_price)
        if self.max_delivery_days is not None:
            values["max_delivery_days"] = self.max_delivery_days
        values["limit"] = self.limit
        values["offers_limit"] = self.offers_limit
        return values


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise StockError("query_invalid", "Некорректный фильтр поиска", 4)
    return value.strip()


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise StockError("query_invalid", f"Некорректный {label}", 4)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise StockError("query_invalid", f"Некорректный {label}", 4) from error
    if parsed < 0:
        raise StockError("query_invalid", f"Некорректный {label}", 4)
    return parsed


def _optional_nonnegative_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, label)


def _positive_bounded_int(value: object, label: str, maximum: int) -> int:
    parsed = _nonnegative_int(value, label)
    if parsed == 0 or parsed > maximum:
        raise StockError("query_invalid", f"Некорректный {label}", 4)
    return parsed


def _price_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise StockError("query_invalid", "Некорректная цена", 4)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise StockError("query_invalid", "Некорректная цена", 4) from error
    if not parsed.is_finite() or parsed < 0:
        raise StockError("query_invalid", "Некорректная цена", 4)
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != value.to_integral() else str(value.quantize(Decimal(1)))
