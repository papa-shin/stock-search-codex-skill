from __future__ import annotations

import base64
import hashlib
import http.client
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
from urllib.error import HTTPError, URLError
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
        self.bytes_read = 0
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            result, self._body = self._body, b""
            self.bytes_read += len(result)
            return result
        result, self._body = self._body[:size], self._body[size:]
        self.bytes_read += len(result)
        return result

    @property
    def remaining_bytes(self) -> int:
        return len(self._body)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
        return None

    def close(self) -> None:
        self.closed = True


class TrackingBody(BytesIO):
    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.bytes_read = 0
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        result = super().read(size)
        self.bytes_read += len(result)
        return result


class RecordingOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses
        self.requests: list[Request] = []
        self.timeouts: list[float | None] = []

    def open(self, request: Request, timeout: float | None = None) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        return self._responses.pop(0)


class RaisingOpener:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.timeouts: list[float | None] = []

    def open(self, request: Request, timeout: float | None = None) -> FakeResponse:
        self.timeouts.append(timeout)
        raise self.error


class RedirectingOpener:
    def __init__(
        self,
        handler: RejectCrossOriginRedirect,
        responses: list[FakeResponse],
    ) -> None:
        self.handler = handler
        self.handler.add_parent(self)
        self._responses = list(responses)
        self.requests: list[Request] = []
        self.timeouts: list[float | None] = []

    def open(self, request: Request, timeout: float | None = None) -> FakeResponse:
        request.timeout = timeout
        self.requests.append(request)
        self.timeouts.append(timeout)
        response = self._responses.pop(0)
        if response.status in {301, 302, 303, 307, 308}:
            redirect = getattr(self.handler, f"http_error_{response.status}")
            headers = Message()
            for name, value in response.headers.items():
                headers[name] = value
            return redirect(
                request,
                response,
                response.status,
                "Synthetic redirect",
                headers,
            )
        return response


class RaisingReadResponse(FakeResponse):
    def __init__(self, error: BaseException) -> None:
        super().__init__(b"")
        self.error = error

    def read(self, size: int = -1) -> bytes:
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

    def test_download_url_rejects_userinfo_controls_and_invalid_ports(self) -> None:
        invalid_urls = (
            "https://user:password@stock.example.test/products.jsonl",
            "https://stock.example.test/products\tname.jsonl",
            "https://stock.example.test:invalid/products.jsonl",
            "https://stock.example.test:65536/products.jsonl",
            "https://stock.example.test/products\u0085name.jsonl",
        )

        for candidate_url in invalid_urls:
            with self.subTest(candidate_url=candidate_url):
                with self.assertRaisesRegex(StockError, "manifest_invalid") as raised:
                    assert_allowed_download_url(
                        self.https_config.manifest_url,
                        candidate_url,
                    )

                self.assertNotIn("user:password", str(raised.exception))
                self.assertNotIn("products", str(raised.exception))

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
        self.assertEqual(opener.timeouts, [30.0])

    def test_download_request_uses_finite_socket_timeout(self) -> None:
        payload = b"safe\n"
        opener = RecordingOpener([FakeResponse(payload)])
        client = self.client_with_opener(opener)

        client.download(
            "https://stock.example.test/products.jsonl",
            self.directory / "products.jsonl",
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )

        self.assertEqual(opener.timeouts, [30.0])

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

    def test_non_returned_http_errors_close_body_without_reading_it(self) -> None:
        for status, expected_code in (
            (401, "auth_failed"),
            (403, "auth_failed"),
            (404, "network_error"),
            (500, "network_error"),
        ):
            with self.subTest(status=status):
                body = TrackingBody(b"x" * (3 * 1024 * 1024))
                error = HTTPError(
                    self.https_config.manifest_url,
                    status,
                    "Synthetic error",
                    Message(),
                    body,
                )
                client = self.client_with_opener(RaisingOpener(error))

                with self.assertRaisesRegex(StockError, expected_code):
                    client.get_manifest()

                self.assertTrue(body.closed)
                self.assertEqual(body.bytes_read, 0)

    def test_http_error_304_remains_open_until_caller_closes_it(self) -> None:
        body = TrackingBody(b"not modified")
        error = HTTPError(
            self.https_config.manifest_url,
            304,
            "Not Modified",
            Message(),
            body,
        )
        client = self.client_with_opener(RaisingOpener(error))

        response = client._open_same_origin(self.https_config.manifest_url, {})

        self.assertIs(response, error)
        self.assertFalse(body.closed)
        response.close()
        self.assertTrue(body.closed)

    def test_auth_http_errors_have_distinct_safe_code(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                error = HTTPError(
                    "https://private.example.test/secret-path",
                    status,
                    "private response",
                    Message(),
                    BytesIO(b"private body"),
                )
                client = self.client_with_opener(RaisingOpener(error))

                with self.assertRaisesRegex(StockError, "auth_failed") as raised:
                    client.get_manifest()

                self.assertEqual(raised.exception.exit_code, 3)
                self.assertNotIn("private", str(raised.exception))

    def test_other_http_errors_are_safe_network_errors(self) -> None:
        for status in (400, 404, 429, 500):
            with self.subTest(status=status):
                error = HTTPError(
                    "https://private.example.test/secret-path",
                    status,
                    "private response",
                    Message(),
                    BytesIO(b"private body"),
                )
                client = self.client_with_opener(RaisingOpener(error))

                with self.assertRaisesRegex(StockError, "network_error") as raised:
                    client.get_manifest()

                self.assertNotIn("private", str(raised.exception))

    def test_url_lifecycle_errors_are_normalized_without_details(self) -> None:
        errors = (
            http.client.InvalidURL("private URL"),
            http.client.BadStatusLine("private response line"),
            http.client.HTTPException("private protocol state"),
            URLError("private network reason"),
            ValueError("private URL state"),
            OSError("private socket state"),
        )

        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                client = self.client_with_opener(RaisingOpener(error))

                with self.assertRaisesRegex(StockError, "network_error") as raised:
                    client.get_manifest()

                self.assertNotIn("private", str(raised.exception))

    def test_incomplete_manifest_read_is_normalized_without_details(self) -> None:
        error = http.client.IncompleteRead(
            partial=b"private partial body",
            expected=1024,
        )
        client = self.client_with_opener(
            RecordingOpener([RaisingReadResponse(error)])
        )

        with self.assertRaisesRegex(StockError, "network_error") as raised:
            client.get_manifest()

        self.assertNotIn("private", str(raised.exception))

    def test_incomplete_download_read_is_safe_and_removes_partial_file(self) -> None:
        destination = self.directory / "partial.jsonl"
        error = http.client.IncompleteRead(
            partial=b"private partial body",
            expected=1024,
        )
        client = self.client_with_opener(
            RecordingOpener([RaisingReadResponse(error)])
        )

        with self.assertRaisesRegex(StockError, "network_error") as raised:
            client.download(
                "https://stock.example.test/products.jsonl",
                destination,
                expected_bytes=1024,
                expected_sha256="0" * 64,
            )

        self.assertNotIn("private", str(raised.exception))
        self.assertFalse(destination.exists())

    def test_manifest_body_is_bounded_before_full_consumption(self) -> None:
        payload = b"x" * (3 * 1024 * 1024)
        response = FakeResponse(payload)
        client = self.client_with_opener(RecordingOpener([response]))

        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            client.get_manifest()

        self.assertEqual(response.bytes_read, 1024 * 1024 + 1)
        self.assertGreater(response.remaining_bytes, 0)

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

    def test_same_origin_redirect_preserves_method_body_and_headers(self) -> None:
        handler = RejectCrossOriginRedirect(("https", "stock.example.test", None))
        request = Request(
            "https://stock.example.test/source",
            data=b"synthetic body",
            headers={
                "Authorization": "Basic synthetic",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        redirected = handler.redirect_request(
            request,
            None,
            307,
            "Temporary Redirect",
            Message(),
            "https://stock.example.test/target",
        )

        self.assertIsNotNone(redirected)
        self.assertEqual(redirected.get_method(), "POST")
        self.assertEqual(redirected.data, b"synthetic body")
        self.assertEqual(redirected.get_header("Authorization"), "Basic synthetic")
        self.assertEqual(redirected.get_header("Content-type"), "application/json")

    def test_cross_origin_redirect_closes_body_without_reading_or_following(self) -> None:
        redirect_body = FakeResponse(
            b"x" * (3 * 1024 * 1024),
            status=302,
            headers={"Location": "https://other.example.test/manifest.json"},
        )
        final = FakeResponse(b"must not be requested")
        handler = RejectCrossOriginRedirect(("https", "stock.example.test", None))
        opener = RedirectingOpener(handler, [redirect_body, final])
        client = self.client_with_opener(opener)

        with self.assertRaisesRegex(StockError, "network_error"):
            client.get_manifest()

        self.assertEqual(len(opener.requests), 1)
        self.assertTrue(redirect_body.closed)
        self.assertEqual(redirect_body.bytes_read, 0)
        self.assertEqual(redirect_body.remaining_bytes, 3 * 1024 * 1024)

    def test_redirect_without_location_closes_body_without_reading(self) -> None:
        redirect_body = FakeResponse(b"private redirect body", status=302)
        handler = RejectCrossOriginRedirect(("https", "stock.example.test", None))
        opener = RedirectingOpener(handler, [redirect_body])
        client = self.client_with_opener(opener)

        with self.assertRaisesRegex(StockError, "network_error"):
            client.get_manifest()

        self.assertTrue(redirect_body.closed)
        self.assertEqual(redirect_body.bytes_read, 0)

    def test_manifest_redirect_does_not_drain_body_and_preserves_request(self) -> None:
        redirect_body = FakeResponse(
            b"x" * (3 * 1024 * 1024),
            status=302,
            headers={"Location": "/current/manifest.json"},
        )
        final = FakeResponse(b"{}", headers={"ETag": '"v3"'})
        handler = RejectCrossOriginRedirect(("https", "stock.example.test", None))
        opener = RedirectingOpener(handler, [redirect_body, final])
        client = self.client_with_opener(opener)

        response = client.get_manifest(etag='"v2"')

        self.assertEqual(response.body, b"{}")
        self.assertEqual(redirect_body.bytes_read, 0)
        self.assertEqual(redirect_body.remaining_bytes, 3 * 1024 * 1024)
        self.assertTrue(redirect_body.closed)
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(
            [request.get_method() for request in opener.requests],
            ["GET", "GET"],
        )
        self.assertEqual(
            [request.get_header("If-none-match") for request in opener.requests],
            ['"v2"', '"v2"'],
        )
        self.assertTrue(
            all(request.get_header("Authorization") for request in opener.requests)
        )
        self.assertEqual(opener.timeouts, [30.0, 30.0])

    def test_download_redirect_does_not_drain_body(self) -> None:
        redirect_body = FakeResponse(
            b"x" * (3 * 1024 * 1024),
            status=307,
            headers={"Location": "/current/products.jsonl"},
        )
        payload = b"safe payload\n"
        final = FakeResponse(payload)
        handler = RejectCrossOriginRedirect(("https", "stock.example.test", None))
        opener = RedirectingOpener(handler, [redirect_body, final])
        client = self.client_with_opener(opener)

        client.download(
            "https://stock.example.test/products.jsonl",
            self.directory / "products.jsonl",
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )

        self.assertEqual(redirect_body.bytes_read, 0)
        self.assertEqual(redirect_body.remaining_bytes, 3 * 1024 * 1024)
        self.assertTrue(redirect_body.closed)
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(opener.timeouts, [30.0, 30.0])

    def test_redirect_loop_is_rejected_after_second_response(self) -> None:
        first = FakeResponse(
            b"first",
            status=302,
            headers={"Location": "/next/manifest.json"},
        )
        second = FakeResponse(
            b"second",
            status=302,
            headers={"Location": self.https_config.manifest_url},
        )
        handler = RejectCrossOriginRedirect(("https", "stock.example.test", None))
        opener = RedirectingOpener(handler, [first, second])
        client = self.client_with_opener(opener)

        with self.assertRaisesRegex(StockError, "network_error"):
            client.get_manifest()

        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertEqual(first.bytes_read + second.bytes_read, 0)

    def test_redirect_count_is_finite_and_all_bodies_are_closed(self) -> None:
        redirect_count = RejectCrossOriginRedirect.max_redirections + 1
        responses = [
            FakeResponse(
                b"redirect",
                status=302,
                headers={"Location": f"/hop-{index}/manifest.json"},
            )
            for index in range(redirect_count)
        ]
        handler = RejectCrossOriginRedirect(("https", "stock.example.test", None))
        opener = RedirectingOpener(handler, responses)
        client = self.client_with_opener(opener)

        with self.assertRaisesRegex(StockError, "network_error"):
            client.get_manifest()

        self.assertEqual(len(opener.requests), redirect_count)
        self.assertTrue(all(response.closed for response in responses))
        self.assertTrue(all(response.bytes_read == 0 for response in responses))
        self.assertTrue(all(timeout == 30.0 for timeout in opener.timeouts))

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

    def test_download_stops_on_first_byte_over_expected_size(self) -> None:
        payload = b"x" * (3 * 1024 * 1024)
        response = FakeResponse(payload)
        destination = self.directory / "oversized.jsonl"
        client = self.client_with_opener(RecordingOpener([response]))

        with self.assertRaisesRegex(StockError, "download_integrity_failed"):
            client.download(
                "https://stock.example.test/oversized.jsonl",
                destination,
                expected_bytes=1,
                expected_sha256=hashlib.sha256(b"x").hexdigest(),
            )

        self.assertEqual(response.bytes_read, 2)
        self.assertEqual(response.remaining_bytes, len(payload) - 2)
        self.assertFalse(destination.exists())

    def test_download_rejects_absurd_manifest_size_before_request(self) -> None:
        opener = RecordingOpener([])
        client = self.client_with_opener(opener)

        with self.assertRaisesRegex(StockError, "manifest_invalid"):
            client.download(
                "https://stock.example.test/products.jsonl",
                self.directory / "products.jsonl",
                expected_bytes=1 << 50,
                expected_sha256="0" * 64,
            )

        self.assertEqual(opener.requests, [])

    def test_download_allows_large_commercial_dataset_size(self) -> None:
        client = self.client_with_opener(
            RaisingOpener(URLError("synthetic offline"))
        )

        with self.assertRaisesRegex(StockError, "network_error"):
            client.download(
                "https://stock.example.test/products.jsonl",
                self.directory / "products.jsonl",
                expected_bytes=4 * 1024 * 1024 * 1024,
                expected_sha256="0" * 64,
            )


class FetchStockCliTest(unittest.TestCase):
    def run_cli(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ)
            environment["PAPA_SHIN_STOCK_CONFIG"] = str(
                Path(directory) / "absent.env"
            )
            return subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "fetch_stock.py"), *arguments],
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

    def test_exact_single_help_argument_is_the_only_non_json_path(self) -> None:
        for arguments in (["-h"], ["--help"]):
            with self.subTest(arguments=arguments):
                result = self.run_cli(arguments)

                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertIn("usage:", result.stdout)
                self.assertIn("-h, --help", result.stdout)

    def test_invalid_argument_forms_print_one_safe_json_document(self) -> None:
        invalid_arguments = (
            ["--unknown"],
            ["-hvalue"],
            ["-hx"],
            ["--h"],
            ["--unknown", "--help"],
            ["--help", "--unknown"],
            ["--help", "--help"],
            ["-h", "-h"],
            ["-h", "--help"],
        )
        expected = {
            "status": "error",
            "error": {
                "code": "query_invalid",
                "message": "Некорректные параметры обновления",
            },
        }

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                result = self.run_cli(list(arguments))

                self.assertEqual(result.returncode, 4)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout.count("\n"), 1)
                self.assertEqual(
                    json.loads(result.stdout),
                    expected,
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

    def test_missing_config_prints_distinct_safe_json_envelope(self) -> None:
        result = self.run_cli([])

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.count("\n"), 1)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "status": "error",
                "error": {
                    "code": "config_missing",
                    "message": "Файл конфигурации не найден",
                },
            },
        )
        self.assertNotIn("absent.env", result.stdout)

    def test_subprocess_output_survives_non_unicode_stdout_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ)
            environment["PAPA_SHIN_STOCK_CONFIG"] = str(
                Path(directory) / "absent.env"
            )
            environment["PYTHONIOENCODING"] = "cp1252"

            help_result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "fetch_stock.py"), "--help"],
                env=environment,
                capture_output=True,
                check=False,
            )
            error_result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "fetch_stock.py")],
                env=environment,
                capture_output=True,
                check=False,
            )

        self.assertEqual(help_result.returncode, 0)
        self.assertEqual(help_result.stderr, b"")
        self.assertIn("usage:", help_result.stdout.decode("utf-8"))
        self.assertEqual(error_result.returncode, 2)
        self.assertEqual(error_result.stderr, b"")
        self.assertEqual(
            json.loads(error_result.stdout.decode("utf-8")),
            {
                "status": "error",
                "error": {
                    "code": "config_missing",
                    "message": "Файл конфигурации не найден",
                },
            },
        )
        self.assertNotIn(
            os.fsencode(directory),
            error_result.stdout + error_result.stderr,
        )

    def test_auth_error_prints_safe_json_envelope(self) -> None:
        output = StringIO()
        error = StockError("auth_failed", "Не удалось подтвердить доступ", 3)

        with patch.object(fetch_stock, "refresh_default", side_effect=error):
            with redirect_stdout(output):
                exit_code = fetch_stock.main()

        self.assertEqual(exit_code, 3)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "status": "error",
                "error": {
                    "code": "auth_failed",
                    "message": "Не удалось подтвердить доступ",
                },
            },
        )

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

    def test_surrogate_manifest_is_one_safe_json_error_without_stderr(self) -> None:
        output = StringIO()
        errors = StringIO()
        body = json.dumps(
            {
                "generation_id": "\ud800",
                "generated_at": "2026-08-27T10:00:00+00:00",
                "files": {
                    "products": {
                        "url": "products.jsonl",
                        "bytes": 0,
                        "sha256": "0" * 64,
                    },
                    "offers": {
                        "url": "offers.jsonl",
                        "bytes": 0,
                        "sha256": "0" * 64,
                    },
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")

        def parse_manifest() -> dict[str, object]:
            Manifest.parse(body)
            return {}

        with patch.object(fetch_stock, "refresh_default", side_effect=parse_manifest):
            with redirect_stdout(output), redirect_stderr(errors):
                exit_code = fetch_stock.main()

        self.assertEqual(exit_code, 3)
        self.assertEqual(output.getvalue().count("\n"), 1)
        self.assertEqual(errors.getvalue(), "")
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
