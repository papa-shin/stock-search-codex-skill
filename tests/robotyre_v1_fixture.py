from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
BASE_GENERATION_ID = "d" * 64
ARCHIVE = b"synthetic-archive"


def payloads(generation_id: str = BASE_GENERATION_ID) -> tuple[bytes, bytes]:
    products = (FIXTURES_DIR / "products.jsonl").read_bytes()
    offers = (FIXTURES_DIR / "offers.jsonl").read_bytes()
    if generation_id != BASE_GENERATION_ID:
        products = products.replace(BASE_GENERATION_ID.encode(), generation_id.encode())
        offers = offers.replace(BASE_GENERATION_ID.encode(), generation_id.encode())
    return products, offers


def manifest_bytes(
    products: bytes,
    offers: bytes,
    generation_id: str = BASE_GENERATION_ID,
    *,
    generated_at: str | None = None,
    checked_at: str | None = None,
) -> bytes:
    now = datetime.now(timezone.utc)
    generated_at = generated_at or (now - timedelta(minutes=2)).isoformat()
    checked_at = checked_at or (now - timedelta(minutes=1)).isoformat()
    report_date = (datetime.fromisoformat(checked_at.replace("Z", "+00:00")) + timedelta(hours=5)).date().isoformat()
    value = {
        "contract": "robotyre-stock/v1",
        "schema_version": "1",
        "content_generation_id": generation_id,
        "report_date": report_date,
        "timezone": "Asia/Yekaterinburg",
        "product_type_sku_counts": {"172": 1},
        "offer_count": max(offers.count(b"\n"), 0),
        "warnings": [],
        "generated_at": generated_at,
        "checked_at": checked_at,
        "stale_after_seconds": 5400,
        "files": {
            "products.jsonl": _file(
                "/robotyre-stock/v1/products.jsonl",
                "application/x-ndjson",
                products,
                "1",
            ),
            "offers.jsonl": _file(
                "/robotyre-stock/v1/offers.jsonl",
                "application/x-ndjson",
                offers,
                "2",
            ),
            "archive.zip": _file(
                "/robotyre-stock/v1/archive.zip",
                "application/zip",
                ARCHIVE,
                "3",
            ),
        },
    }
    return json.dumps(value, separators=(",", ":")).encode()


def _file(url: str, media_type: str, payload: bytes, suffix: str) -> dict[str, object]:
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "url": url,
        "media_type": media_type,
        "bytes": len(payload),
        "sha256": digest,
        "etag": f'"{len(payload):x}-{suffix}"',
        "last_modified": "Fri, 28 Aug 2026 21:00:00 GMT",
    }
