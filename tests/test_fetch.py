from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError
from papa_shin_stock.cache import Manifest
from papa_shin_stock.http_client import (
    RejectCrossOriginRedirect,
    SafeHttpClient,
    assert_allowed_download_url,
    normalized_origin,
)
import fetch_stock


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.status = status
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            result, self._body = self._body, b""
            return result
        result, self._body = self._body[:size], self._body[size:]
        return result

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class RecordingOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses
        self.requests: list[Request] = []

    def open(self, request: Request, timeout: float | None = None) -> FakeResponse:
        self.requests.append(request)
        return self._responses.pop(0)


class RaisingOpener:
    def __init__(self, error: HTTPError) -> None:
        self.error = error

    def open(self, request: Request, timeout: float | None = None) -> FakeResponse:
        raise self.error


class SafeHttpSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.directory = Path(self.temp_dir.name)
        self.https_config = StockConfig(
            manifest_url="https://stock.example.test/manifest.json",
            username="test-user",
            password="test-password",
            product_id_field="product_id",
            offer_product_id_field="offer_id",
            cache_dir=self.directory,
        )
        self.http_config = StockConfig(
            manifest_url="http://stock.example.test/manifest.json",
            username="test-user",
            password="test-password",
            product_id_field="product_id",
            offer_product_id_field="offer_id",
            cache_dir=self.directory,
        )

    def client_with_opener(self, opener: object) -> SafeHttpClient:
        client = SafeHttpClient.for_config(self.https_config)
        client._opener = opener
        return client

    def test_cross_origin_download_is_rejected(self) -> None:
        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            assert_allowed_download_url(
                "https://stock.example.test/manifest.json",
                "https://other.example.test/products.jsonl",
            )

    def test_http_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(StockError, "config_invalid"):
            SafeHttpClient.for_config(self.http_config)

    def test_direct_construction_rejects_http_config_before_credentials_can_be_sent(self) -> None:
        with self.assertRaisesRegex(StockError, "config_invalid"):
            SafeHttpClient(self.http_config)

    def test_normalized_origin_treats_default_https_port_as_same_origin(self) -> None:
        self.assertEqual(
            normalized_origin("https://STOCK.example.test:443/manifest.json"),
            ("https", "stock.example.test", None),
        )

    def test_manifest_request_uses_conditional_headers_and_origin_bound_auth(self) -> None:
        opener = RecordingOpener(
            [FakeResponse(b"", status=304, headers={"ETag": '"v2"'})]
        )
        client = self.client_with_opener(opener)

        response = client.get_manifest(etag='"v1"', last_modified="Tue, 01 Sep 2026 00:00:00 GMT")

        self.assertEqual(response.status, 304)
        self.assertEqual(response.body, b"")
        self.assertEqual(response.headers, {"ETag": '"v2"'})
        request = opener.requests[0]
        self.assertEqual(request.get_header("If-none-match"), '"v1"')
        self.assertEqual(request.get_header("If-modified-since"), "Tue, 01 Sep 2026 00:00:00 GMT")
        expected_authorization = base64.b64encode(b"test-user:test-password").decode("ascii")
        self.assertEqual(request.get_header("Authorization"), f"Basic {expected_authorization}")

    def test_manifest_returns_real_http_error_304_as_response(self) -> None:
        headers = Message()
        headers["ETag"] = '"v2"'
        error = HTTPError(
            self.https_config.manifest_url,
            304,
            "Not Modified",
            headers,
            BytesIO(b""),
        )
        client = self.client_with_opener(RaisingOpener(error))

        response = client.get_manifest()

        self.assertEqual(response.status, 304)
        self.assertEqual(response.headers, {"ETag": '"v2"'})
        self.assertEqual(response.body, b"")

    def test_cross_origin_request_does_not_reach_opener_or_send_credentials(self) -> None:
        opener = RecordingOpener([])
        client = self.client_with_opener(opener)

        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            client._open_same_origin("https://other.example.test/products.jsonl", {})

        self.assertEqual(opener.requests, [])

    def test_cross_origin_redirect_is_rejected(self) -> None:
        handler = RejectCrossOriginRedirect(("https", "stock.example.test", None))
        request = Request("https://stock.example.test/manifest.json")

        with self.assertRaisesRegex(StockError, "network_error"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://other.example.test/manifest.json",
            )

    def test_factory_opener_has_cross_origin_redirect_protection(self) -> None:
        client = SafeHttpClient.for_config(self.https_config)
        handler = next(
            handler
            for handler in client.opener.handlers
            if isinstance(handler, RejectCrossOriginRedirect)
        )

        with self.assertRaisesRegex(StockError, "network_error"):
            handler.redirect_request(
                Request(self.https_config.manifest_url),
                None,
                302,
                "Found",
                {},
                "https://other.example.test/manifest.json",
            )

    def test_download_writes_verified_payload_and_returns_receipt(self) -> None:
        payload = b'{"product_id":"A-12"}\n'
        opener = RecordingOpener([FakeResponse(payload)])
        client = self.client_with_opener(opener)
        destination = self.directory / "products.jsonl"

        receipt = client.download(
            "https://stock.example.test/products.jsonl",
            destination,
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )

        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(receipt.bytes, len(payload))
        self.assertEqual(receipt.sha256, hashlib.sha256(payload).hexdigest())

    def test_download_calls_progress_for_each_streamed_chunk(self) -> None:
        payload = b"x" * (2 * 1024 * 1024 + 1)
        opener = RecordingOpener([FakeResponse(payload)])
        client = self.client_with_opener(opener)
        destination = self.directory / "products-large.jsonl"
        heartbeats: list[None] = []

        client.download(
            "https://stock.example.test/products-large.jsonl",
            destination,
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            progress=lambda: heartbeats.append(None),
        )

        self.assertEqual(len(heartbeats), 3)

    def test_download_removes_payload_that_fails_integrity_check(self) -> None:
        destination = self.directory / "products.jsonl"
        client = self.client_with_opener(RecordingOpener([FakeResponse(b"altered")]))

        with self.assertRaisesRegex(StockError, "download_integrity_failed"):
            client.download(
                "https://stock.example.test/products.jsonl",
                destination,
                expected_bytes=8,
                expected_sha256="0" * 64,
            )

        self.assertFalse(destination.exists())


class FetchStockCliTest(unittest.TestCase):
    def test_unknown_argument_prints_one_safe_json_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ)
            environment["PAPA_SHIN_STOCK_CONFIG"] = str(
                Path(directory) / "absent.env"
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "fetch_stock.py"), "--unknown"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 4)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.count("\n"), 1)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "status": "error",
                "error": {
                    "code": "query_invalid",
                    "message": "Некорректные параметры обновления",
                },
            },
        )

    def test_help_exits_zero_without_config_or_refresh_side_effects(self) -> None:
        output = StringIO()
        errors = StringIO()

        with tempfile.TemporaryDirectory() as directory:
            absent_config = str(Path(directory) / "absent.env")
            with patch.dict(
                os.environ,
                {"PAPA_SHIN_STOCK_CONFIG": absent_config},
                clear=False,
            ):
                with patch.object(fetch_stock, "refresh_default") as refresh:
                    with redirect_stdout(output), redirect_stderr(errors):
                        exit_code = fetch_stock.main(["--help"])

        self.assertEqual(exit_code, 0)
        refresh.assert_not_called()
        self.assertIn("usage:", output.getvalue())
        self.assertIn("--help", output.getvalue())
        self.assertEqual(errors.getvalue(), "")

    def test_success_prints_single_public_json_document_and_returns_zero(self) -> None:
        public_result = {
            "status": "stale_cache",
            "generation": {
                "id": "synthetic-generation",
                "generated_at": "2026-08-27T10:00:00+00:00",
                "checked_at": "2026-08-27T10:01:00+00:00",
                "stale": True,
            },
            "warnings": [{"code": "network_error", "message": "Используется кэш"}],
        }
        output = StringIO()

        with patch.object(fetch_stock, "refresh_default", return_value=public_result):
            with redirect_stdout(output):
                exit_code = fetch_stock.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), public_result)
        self.assertEqual(output.getvalue().count("\n"), 1)

    def test_error_prints_safe_json_envelope_and_returns_error_exit_code(self) -> None:
        output = StringIO()
        error = StockError("config_invalid", "Безопасное сообщение", 2)

        with patch.object(fetch_stock, "refresh_default", side_effect=error):
            with redirect_stdout(output):
                exit_code = fetch_stock.main()

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "status": "error",
                "error": {
                    "code": "config_invalid",
                    "message": "Безопасное сообщение",
                },
            },
        )
        self.assertNotIn("synthetic-password", output.getvalue())

    def test_huge_integer_manifest_is_normalized_to_safe_json_error(self) -> None:
        output = StringIO()
        body = b'{"generation_id":' + b"9" * 5000 + b"}"

        def parse_manifest() -> dict[str, object]:
            Manifest.parse(body)
            return {}

        with patch.object(fetch_stock, "refresh_default", side_effect=parse_manifest):
            with redirect_stdout(output):
                exit_code = fetch_stock.main()

        self.assertEqual(exit_code, 3)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "status": "error",
                "error": {
                    "code": "manifest_invalid",
                    "message": "Некорректный manifest",
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
