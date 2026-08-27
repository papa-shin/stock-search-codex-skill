from __future__ import annotations

import argparse
import json

from papa_shin_stock.cache import StockCache
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.http_client import SafeHttpClient
from papa_shin_stock.query import SearchQuery
from papa_shin_stock.schema import StockSearcher


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Поиск по локальному проверенному кэшу")
    parser.add_argument("--product-type")
    parser.add_argument("--size")
    parser.add_argument("--season")
    parser.add_argument("--spikes")
    parser.add_argument("--run-flat")
    parser.add_argument("--disk-type")
    parser.add_argument("--truck-axis")
    parser.add_argument("--truck-construction")
    parser.add_argument("--supplier")
    parser.add_argument("--min-total-quantity", type=int, default=4)
    parser.add_argument("--max-price")
    parser.add_argument("--max-delivery-days", type=int)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--offers-limit", type=int, default=5)
    return parser


def search_default(namespace: argparse.Namespace) -> dict[str, object]:
    config = StockConfig.load()
    files = StockCache(config.cache_dir, SafeHttpClient.for_config(config)).current_generation()
    query = SearchQuery.from_args(namespace)
    return StockSearcher(files, config).search(query).to_public_dict()


def main(argv: list[str] | None = None) -> int:
    namespace = build_parser().parse_args(argv)
    try:
        result = search_default(namespace)
        exit_code = 0
    except StockError as error:
        result = {
            "status": "error",
            "error": {"code": error.code, "message": error.safe_message},
        }
        exit_code = error.exit_code
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
