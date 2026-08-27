from __future__ import annotations

import argparse
import json
import sys

from papa_shin_stock.cache import StockCache
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.http_client import SafeHttpClient


def refresh_default() -> dict[str, object]:
    config = StockConfig.load()
    client = SafeHttpClient.for_config(config)
    return StockCache(config.cache_dir, client).refresh(config).to_public_dict()


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Обновление локального проверенного кэша"
    )


def main(argv: list[str] | None = None) -> int:
    try:
        build_parser().parse_args([] if argv is None else argv)
    except SystemExit as error:
        return int(error.code)

    try:
        result = refresh_default()
        exit_code = 0
    except StockError as error:
        result = {
            "status": "error",
            "error": {
                "code": error.code,
                "message": error.safe_message,
            },
        }
        exit_code = error.exit_code
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
