from __future__ import annotations

import json

from papa_shin_stock.cache import StockCache
from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.http_client import SafeHttpClient


def refresh_default() -> dict[str, object]:
    config = StockConfig.load()
    client = SafeHttpClient.for_config(config)
    return StockCache(config.cache_dir, client).refresh(config).to_public_dict()


def main() -> int:
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
    raise SystemExit(main())
