import hashlib
import ipaddress
import json
import random
import socket
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from .config import BuildConfig
from .errors import AuthenticationError, RemoteError, RetryableError

T = TypeVar("T")


class RateLimiter:
    """Paced starts, including retries; no burst allowance. One synchronous runner."""

    def __init__(self, rate: float):
        self.interval = 1 / rate
        self.next_start = 0.0

    def wait(self) -> None:
        delay = self.next_start - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_start = time.monotonic() + self.interval


class Transport:
    def __init__(self, config: BuildConfig, key: str, emit: Callable, client=None):
        self.config = config
        self.key = key
        self.emit = emit
        self.client = client or httpx.Client(
            timeout=config.timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": "corpus-builder/0.3.0", "Accept-Encoding": "identity"},
        )
        self.ncbi_limiter = RateLimiter(config.requests_per_second)
        self.download_limiter = RateLimiter(config.download_requests_per_second)
        self.resolver_limiter = RateLimiter(config.resolver_requests_per_second)

    def close(self) -> None:
        self.client.close()

    def _sleep_retry(self, attempt: int, retry_after: str | None) -> None:
        delay = min(60, self.config.backoff_seconds * 2**attempt) + random.uniform(0, 0.25)
        if retry_after:
            try:
                seconds = float(retry_after)
            except ValueError:
                try:
                    seconds = (
                        parsedate_to_datetime(retry_after) - datetime.now(UTC)
                    ).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    seconds = 0
            if seconds > 0:
                delay = max(delay, seconds)
        self.emit("http_retry", attempt=attempt + 1, wait_seconds=round(delay, 3))
        time.sleep(delay)

    def _perform(
        self,
        url: str,
        parse: Callable[[httpx.Response], T],
        *,
        params=None,
        data=None,
        ncbi=False,
        resolver=False,
        external=False,
    ) -> T:
        if ncbi:
            data = list(data or [])
            data.extend([("tool", self.config.tool), ("email", self.config.email)])
            if self.key:
                data.append(("api_key", self.key))
        if external:
            validate_public_url(url)
        for attempt in range(self.config.max_attempts):
            retry_after = None
            (
                self.ncbi_limiter
                if ncbi
                else self.resolver_limiter
                if resolver
                else self.download_limiter
            ).wait()
            try:
                kwargs = {"params": params}
                if data is not None:
                    kwargs.update(
                        content=urlencode(data),
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                if external:
                    current = url
                    for redirect in range(6):
                        validate_public_url(current)
                        validate_public_host(urlsplit(current).hostname)
                        request = self.client.build_request("GET", current)
                        response = self.client.send(request, stream=True, follow_redirects=False)
                        if response.status_code not in (301, 302, 303, 307, 308):
                            break
                        location = response.headers.get("Location")
                        response.close()
                        if not location or redirect == 5:
                            raise RemoteError("Invalid or excessive redirects")
                        current = urljoin(current, location)
                        self.download_limiter.wait()
                    manager = response
                else:
                    manager = self.client.stream(
                        "POST" if data is not None else "GET", url, **kwargs
                    )
                # httpx.Response is not a context manager; use a closing wrapper for redirects.
                with closing(manager) if external else manager as response:
                    retry_after = response.headers.get("Retry-After")
                    status = response.status_code
                    if ncbi and status in (401, 403):
                        raise AuthenticationError("NCBI rejected authentication")
                    if status == 429 or status in (408, 500, 502, 503, 504):
                        raise RetryableError(f"Remote HTTP {status}")
                    if status >= 300:
                        # NCBI can return invalid-key JSON with HTTP 400.
                        if ncbi and status == 400:
                            body = response.read().decode(errors="replace").lower()
                            if "api key" in body or "api_key" in body:
                                raise AuthenticationError("NCBI rejected the API key")
                        raise RemoteError(f"Remote HTTP {status}")
                    return parse(response)
            except (
                httpx.TransportError,
                RetryableError,
                ValueError,
                ET.ParseError,
                DefusedXmlException,
            ) as exc:
                if attempt + 1 == self.config.max_attempts:
                    raise RemoteError(
                        f"Request failed after {self.config.max_attempts} attempts "
                        f"({type(exc).__name__})"
                    ) from None
                self._sleep_retry(attempt, retry_after)
        raise AssertionError("Unreachable")

    def json(self, url, *, data=None, params=None, ncbi=False, resolver=False):
        def parse(response):
            obj = json.loads(response.read())
            if not isinstance(obj, dict):
                raise RetryableError("Expected JSON object")
            error = obj.get("error")
            if error:
                if "api key" in str(error).lower() or "api_key" in str(error).lower():
                    raise AuthenticationError("NCBI rejected the API key")
                raise RetryableError("API returned an error envelope")
            return obj

        return self._perform(url, parse, data=data, params=params, ncbi=ncbi, resolver=resolver)

    def xml(self, url, *, data=None, params=None, ncbi=False):
        return self._perform(
            url, lambda r: ET.fromstring(r.read()), data=data, params=params, ncbi=ncbi
        )

    def download(self, url: str, path: Path, fmt: str, md5: str | None, *, external=False) -> dict:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")

        def parse(response):
            digest = hashlib.md5(usedforsecurity=False)
            sha = hashlib.sha256()
            size = 0
            with temporary.open("wb") as handle:
                for chunk in response.iter_bytes(65536):
                    handle.write(chunk)
                    digest.update(chunk)
                    sha.update(chunk)
                    size += len(chunk)
                    if size > self.config.max_download_bytes:
                        raise RemoteError("Document exceeds configured max_download_bytes")
            length = response.headers.get("Content-Length")
            if length and not response.headers.get("Content-Encoding") and size != int(length):
                raise RetryableError("Truncated file")
            if md5 and digest.hexdigest() != md5.lower():
                raise RetryableError("Source checksum mismatch")
            validate_file(temporary, fmt)
            temporary.replace(path)
            return {
                "sha256": sha.hexdigest(),
                "md5": digest.hexdigest(),
                "bytes": size,
                "resolved_url": str(response.url),
                "downloaded_at": datetime.now(UTC).isoformat(),
            }

        try:
            return self._perform(url, parse, external=external)
        finally:
            temporary.unlink(missing_ok=True)


def validate_file(path: Path, fmt: str) -> None:
    if fmt == "pdf":
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise RetryableError("Not a PDF")
            handle.seek(max(0, path.stat().st_size - 2048))
            if b"%%EOF" not in handle.read():
                raise RetryableError("Incomplete PDF")
    elif fmt == "xml":
        root = ET.parse(path).getroot()
        if root.tag.rsplit("}", 1)[-1] not in ("article", "article-set", "pmc-articleset"):
            raise RetryableError("Not a full-text article XML")

    elif fmt == "html":
        parser = FullTextHTML()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        if not parser.valid():
            raise RetryableError("HTML is not a supported full-text article")
    else:
        raise RetryableError("Unsupported document format")


class FullTextHTML(HTMLParser):
    """Conservative acceptance: explicit article body, sections and substantial prose."""

    def __init__(self):
        super().__init__()
        self.body = False
        self.sections = 0
        self.words = 0
        self.blocked = False
        self.depth = 0
        self.body_depth = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in ("meta", "link", "img", "input", "br", "hr", "source", "wbr"):
            self.depth += 1
        marker = (
            attrs.get("itemprop", "") + " " + attrs.get("id", "") + " " + attrs.get("class", "")
        ).lower()
        if any(
            m in marker.split() for m in ("articlebody", "article-body", "fulltext", "full-text")
        ):
            self.body = True
            self.body_depth = self.depth
        if self.body_depth is not None and tag in ("h2", "h3", "section"):
            self.sections += 1
        if tag == "input" and attrs.get("type") == "password":
            self.blocked = True

    def handle_endtag(self, tag):
        if self.body_depth is not None and self.depth == self.body_depth:
            self.body_depth = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.body_depth is not None:
            self.words += len(data.split())

    def valid(self):
        return self.body and self.sections >= 2 and self.words >= 300 and not self.blocked


def validate_public_url(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or host.lower() == "localhost"
            or host.lower().endswith((".localhost", ".local", ".internal"))
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host:
                raise ValueError from None
        else:
            if not address.is_global:
                raise ValueError
    except (ValueError, TypeError):
        raise RemoteError("Document URL must use public HTTPS without credentials") from None


def validate_public_host(host):
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        # A SOCKS/HTTP proxy may resolve remotely. Let the HTTP transport perform
        # resolution/retries; literal private addresses were already rejected.
        return
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise RemoteError("Document host resolves to a non-public address")


def file_hash(path: Path, algorithm="sha256") -> str:
    digest = hashlib.new(algorithm, usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
