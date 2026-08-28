from __future__ import annotations

import argparse
import json
import sys

from papa_shin_stock._cli_output import write_stdout_utf8
from papa_shin_stock.cache import StockCache
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.http_client import SafeHttpClient


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _query_invalid()


def _query_invalid() -> StockError:
    return StockError(
        "query_invalid",
        "Некорректные параметры обновления",
        4,
    )


def refresh_default() -> dict[str, object]:
    config = StockConfig.load()
    client = SafeHttpClient.for_config(config)
    return StockCache(config.cache_dir, client).refresh(config).to_public_dict()


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(
        description="Обновление локального проверенного кэша",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = [] if argv is None else argv
    parser = build_parser()
    if arguments == ["-h"] or arguments == ["--help"]:
        write_stdout_utf8(parser.format_help())
        return 0

    try:
        namespace = parser.parse_args(arguments)
        if namespace.help_requested:
            raise _query_invalid()
        result = refresh_default()
        exit_code = 0
    except SystemExit:
        result = {
            "status": "error",
            "error": {
                "code": "query_invalid",
                "message": "Некорректные параметры обновления",
            },
        }
        exit_code = 4
    except StockError as error:
        result = {
            "status": "error",
            "error": {
                "code": error.code,
                "message": error.safe_message,
            },
        }
        exit_code = error.exit_code
    write_stdout_utf8(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
