from __future__ import annotations

import base64
import hashlib
import http.client
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urljoin, urlsplit

from papa_shin_stock.config import StockConfig
from papa_shin_stock.errors import StockError


Origin = tuple[str, str, int | None]
_SOCKET_TIMEOUT_SECONDS = 30.0
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


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
    if not isinstance(url, str) or _has_unsafe_url_character(url):
        raise _invalid_file_url()
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError, UnicodeError) as error:
        raise _invalid_file_url() from error

    if (
        scheme not in {"http", "https"}
        or hostname is None
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
    ):
        raise _invalid_file_url()

    normalized_port = None if port in {None, 80 if scheme == "http" else 443} else port
    return scheme, hostname.lower(), normalized_port


def assert_allowed_download_url(manifest_url: str, candidate_url: str) -> str:
    if normalized_origin(manifest_url) != normalized_origin(candidate_url):
        raise StockError("manifest_invalid", "Загрузка с другого сервера запрещена", 3)
    return candidate_url


class _FollowRedirect(Exception):
    def __init__(self, request: urllib.request.Request) -> None:
        self.request = request
        super().__init__()


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
        if code not in {301, 302, 303, 307, 308}:
            raise _network_error()
        try:
            redirect_origin = normalized_origin(newurl)
        except StockError as error:
            raise _network_error() from error
        if redirect_origin != self.allowed_origin:
            raise StockError("network_error", "Перенаправление на другой сервер запрещено", 3)
        return urllib.request.Request(
            newurl,
            data=req.data,
            headers=dict(req.header_items()),
            origin_req_host=req.origin_req_host,
            unverifiable=True,
            method=req.get_method(),
        )

    def http_error_302(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
    ) -> None:
        try:
            location = headers.get("Location") or headers.get("URI")
            if (
                not isinstance(location, str)
                or not location
                or _has_unsafe_url_character(location)
            ):
                raise _network_error()
            try:
                newurl = urljoin(req.full_url, location)
            except (TypeError, ValueError, UnicodeError) as error:
                raise _network_error() from error
            redirected = self.redirect_request(
                req,
                fp,
                code,
                msg,
                headers,
                newurl,
            )
            if redirected is None:
                raise _network_error()
            raise _FollowRedirect(redirected)
        finally:
            _close_response(fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


class SafeHttpClient:
    def __init__(self, config: StockConfig) -> None:
        self.config = config
        try:
            self.origin = normalized_origin(config.manifest_url)
        except StockError as error:
            raise StockError("config_invalid", "Некорректная конфигурация", 2) from error
        if self.origin[0] != "https":
            raise StockError("config_invalid", "Для загрузки требуется HTTPS", 2)
        self._opener = urllib.request.build_opener(
            RejectCrossOriginRedirect(self.origin)
        )

    @classmethod
    def for_config(cls, config: StockConfig) -> "SafeHttpClient":
        return cls(config=config)

    @property
    def opener(self) -> urllib.request.OpenerDirector:
        """Expose the constructed opener for diagnostics without accepting injections."""
        return self._opener

    def get_manifest(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> HttpResponse:
        try:
            response = self._open_same_origin(
                self.config.manifest_url,
                self._conditional_headers(etag, last_modified),
            )
            with response:
                status = _response_status(response)
                _raise_for_http_status(status, allow_not_modified=True)
                headers = dict(response.headers.items())
                body = response.read(_MAX_MANIFEST_BYTES + 1)
        except StockError:
            raise
        except _NETWORK_EXCEPTIONS as error:
            raise _network_error() from error
        if len(body) > _MAX_MANIFEST_BYTES:
            raise StockError("manifest_invalid", "Некорректный manifest", 3)
        return HttpResponse(status=status, headers=headers, body=body)

    def download(
        self,
        url: str,
        destination: Path,
        expected_bytes: int,
        expected_sha256: str,
        progress: Callable[[], None] | None = None,
    ) -> DownloadReceipt:
        if (
            type(expected_bytes) is not int
            or expected_bytes < 0
            or expected_bytes > _MAX_DOWNLOAD_BYTES
        ):
            raise StockError("manifest_invalid", "Некорректный manifest", 3)
        resolved = assert_allowed_download_url(self.config.manifest_url, url)
        digest = hashlib.sha256()
        received = 0
        try:
            with self._open_same_origin(resolved, {}) as response, destination.open("wb") as output:
                _raise_for_http_status(
                    _response_status(response),
                    allow_not_modified=False,
                )
                while received < expected_bytes:
                    chunk = response.read(
                        min(_DOWNLOAD_CHUNK_BYTES, expected_bytes - received)
                    )
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if progress is not None:
                        progress()
                if received == expected_bytes and response.read(1):
                    raise _download_integrity_error()
        except StockError:
            destination.unlink(missing_ok=True)
            raise
        except _NETWORK_EXCEPTIONS as error:
            destination.unlink(missing_ok=True)
            raise _network_error() from error

        if received != expected_bytes or digest.hexdigest() != expected_sha256:
            destination.unlink(missing_ok=True)
            raise _download_integrity_error()
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
        visited = {request.full_url}
        redirect_count = 0
        while True:
            try:
                return self._opener.open(request, timeout=_SOCKET_TIMEOUT_SECONDS)
            except _FollowRedirect as redirect:
                next_request = redirect.request
                if (
                    redirect_count >= RejectCrossOriginRedirect.max_redirections
                    or next_request.full_url in visited
                ):
                    raise _network_error()
                redirect_count += 1
                visited.add(next_request.full_url)
                request = next_request
            except urllib.error.HTTPError as error:
                if error.code == 304:
                    return error
                _close_response(error)
                if error.code in {401, 403}:
                    raise StockError(
                        "auth_failed",
                        "Не удалось подтвердить доступ",
                        3,
                    ) from error
                raise _network_error() from error
            except StockError:
                raise
            except _NETWORK_EXCEPTIONS as error:
                raise _network_error() from error


_NETWORK_EXCEPTIONS = (
    OSError,
    ValueError,
    urllib.error.URLError,
    http.client.HTTPException,
)


def _response_status(response: object) -> int:
    status = getattr(response, "status", None)
    if status is None:
        status = response.getcode()
    if type(status) is not int:
        raise _network_error()
    return status


def _close_response(response: object) -> None:
    try:
        response.close()
    except StockError:
        raise
    except _NETWORK_EXCEPTIONS as error:
        raise _network_error() from error


def _raise_for_http_status(status: int, *, allow_not_modified: bool) -> None:
    if 200 <= status < 300 or (allow_not_modified and status == 304):
        return
    if status in {401, 403}:
        raise StockError("auth_failed", "Не удалось подтвердить доступ", 3)
    raise _network_error()


def _has_unsafe_url_character(value: str) -> bool:
    return any(
        ord(character) <= 32 or 127 <= ord(character) <= 159
        for character in value
    )


def _invalid_file_url() -> StockError:
    return StockError("manifest_invalid", "Некорректный адрес файла", 3)


def _network_error() -> StockError:
    return StockError("network_error", "Не удалось выполнить сетевой запрос", 3)


def _download_integrity_error() -> StockError:
    return StockError(
        "download_integrity_failed",
        "Проверка загруженного файла не пройдена",
        5,
    )
