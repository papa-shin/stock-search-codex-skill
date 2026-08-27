from __future__ import annotations

import base64
import hashlib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError


Origin = tuple[str, str, int | None]


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class DownloadReceipt:
    bytes: int
    sha256: str


def normalized_origin(url: str) -> Origin:
    """Return a canonical URL origin without preserving credentials or paths."""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise StockError("manifest_invalid", "Некорректный адрес файла", 3) from error

    if scheme not in {"http", "https"} or hostname is None:
        raise StockError("manifest_invalid", "Некорректный адрес файла", 3)

    normalized_port = None if port in {None, 80 if scheme == "http" else 443} else port
    return scheme, hostname.lower(), normalized_port


def assert_allowed_download_url(manifest_url: str, candidate_url: str) -> str:
    if normalized_origin(manifest_url) != normalized_origin(candidate_url):
        raise StockError("manifest_invalid", "Загрузка с другого сервера запрещена", 3)
    return candidate_url


class RejectCrossOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_origin: Origin) -> None:
        self.allowed_origin = allowed_origin

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        if normalized_origin(newurl) != self.allowed_origin:
            raise StockError("network_error", "Перенаправление на другой сервер запрещено", 3)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SafeHttpClient:
    def __init__(self, config: StockConfig, opener: object) -> None:
        self.config = config
        self.opener = opener
        self.origin = normalized_origin(config.manifest_url)

    @classmethod
    def for_config(cls, config: StockConfig) -> "SafeHttpClient":
        origin = normalized_origin(config.manifest_url)
        if origin[0] != "https":
            raise StockError("config_invalid", "Для загрузки требуется HTTPS", 2)
        redirect_handler = RejectCrossOriginRedirect(origin)
        return cls(config=config, opener=urllib.request.build_opener(redirect_handler))

    def get_manifest(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> HttpResponse:
        response = self._open_same_origin(
            self.config.manifest_url, self._conditional_headers(etag, last_modified)
        )
        with response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            headers = dict(response.headers.items())
            body = response.read()
        return HttpResponse(status=status, headers=headers, body=body)

    def download(
        self,
        url: str,
        destination: Path,
        expected_bytes: int,
        expected_sha256: str,
    ) -> DownloadReceipt:
        resolved = assert_allowed_download_url(self.config.manifest_url, url)
        digest = hashlib.sha256()
        received = 0
        try:
            with self._open_same_origin(resolved, {}) as response, destination.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
        except StockError:
            destination.unlink(missing_ok=True)
            raise
        except OSError as error:
            destination.unlink(missing_ok=True)
            raise StockError("network_error", "Не удалось загрузить файл", 3) from error

        if received != expected_bytes or digest.hexdigest() != expected_sha256:
            destination.unlink(missing_ok=True)
            raise StockError(
                "download_integrity_failed",
                "Проверка загруженного файла не пройдена",
                5,
            )
        return DownloadReceipt(bytes=received, sha256=digest.hexdigest())

    @staticmethod
    def _conditional_headers(
        etag: str | None, last_modified: str | None
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        return headers

    def _open_same_origin(
        self, url: str, headers: Mapping[str, str]
    ) -> object:
        assert_allowed_download_url(self.config.manifest_url, url)
        request_headers = dict(headers)
        credentials = f"{self.config.username}:{self.config.password}".encode("utf-8")
        request_headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
        request = urllib.request.Request(url, headers=request_headers)
        try:
            return self.opener.open(request)
        except urllib.error.HTTPError as error:
            if error.code == 304:
                return error
            raise StockError("network_error", "Не удалось выполнить сетевой запрос", 3) from error
        except (OSError, urllib.error.URLError) as error:
            raise StockError("network_error", "Не удалось выполнить сетевой запрос", 3) from error
