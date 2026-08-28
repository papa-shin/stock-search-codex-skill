from __future__ import annotations

import argparse
import json
import sys

from papa_shin_stock.cache import StockCache
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.http_client import SafeHttpClient
from papa_shin_stock.query import SearchQuery
from papa_shin_stock.schema import StockSearcher


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _query_invalid()


def _query_invalid() -> StockError:
    return StockError("query_invalid", "Некорректные параметры поиска", 4)


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(
        description="Поиск по локальному проверенному кэшу",
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument(
        "-h",
        "--help",
        action="store_true",
        dest="help_requested",
        help="show this help message and exit",
    )
    parser.add_argument("--product-type")
    parser.add_argument("--size")
    parser.add_argument("--season")
    parser.add_argument("--spikes")
    parser.add_argument("--run-flat")
    parser.add_argument("--disk-type")
    parser.add_argument("--truck-axis")
    parser.add_argument("--truck-construction")
    parser.add_argument("--supplier")
    parser.add_argument("--min-total-quantity", default=4)
    parser.add_argument("--max-price")
    parser.add_argument("--max-delivery-days")
    parser.add_argument("--limit", default=10)
    parser.add_argument("--offers-limit", default=5)
    return parser


def search_default(namespace: argparse.Namespace) -> dict[str, object]:
    query = SearchQuery.from_args(namespace)
    config = StockConfig.load()
    files = StockCache(config.cache_dir, SafeHttpClient.for_config(config)).current_generation()
    return StockSearcher(files, config).search(query).to_public_dict()


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    if arguments == ["-h"] or arguments == ["--help"]:
        parser.print_help()
        return 0

    try:
        namespace = parser.parse_args(arguments)
        if namespace.help_requested:
            raise _query_invalid()
        result = search_default(namespace)
        exit_code = 0
    except SystemExit:
        result = {
            "status": "error",
            "error": {
                "code": "query_invalid",
                "message": "Некорректные параметры поиска",
            },
        }
        exit_code = 4
    except StockError as error:
        result = {
            "status": "error",
            "error": {"code": error.code, "message": error.safe_message},
        }
        exit_code = error.exit_code
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
