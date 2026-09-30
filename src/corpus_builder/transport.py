import hashlib
import json
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlencode

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
            headers={"User-Agent": "corpus-builder/0.1.0", "Accept-Encoding": "identity"},
        )
        self.ncbi_limiter = RateLimiter(config.requests_per_second)
        self.download_limiter = RateLimiter(config.download_requests_per_second)

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
        self, url: str, parse: Callable[[httpx.Response], T], *, params=None, data=None, ncbi=False
    ) -> T:
        if ncbi:
            data = list(data or [])
            data.extend([("tool", self.config.tool), ("email", self.config.email)])
            if self.key:
                data.append(("api_key", self.key))
        for attempt in range(self.config.max_attempts):
            retry_after = None
            (self.ncbi_limiter if ncbi else self.download_limiter).wait()
            try:
                kwargs = {"params": params}
                if data is not None:
                    kwargs.update(
                        content=urlencode(data),
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                with self.client.stream(
                    "POST" if data is not None else "GET", url, **kwargs
                ) as response:
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

    def json(self, url, *, data=None, params=None, ncbi=False):
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

        return self._perform(url, parse, data=data, params=params, ncbi=ncbi)

    def xml(self, url, *, data=None, params=None, ncbi=False):
        return self._perform(
            url, lambda r: ET.fromstring(r.read()), data=data, params=params, ncbi=ncbi
        )

    def download(self, url: str, path: Path, fmt: str, md5: str | None) -> dict:
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
            length = response.headers.get("Content-Length")
            if length and not response.headers.get("Content-Encoding") and size != int(length):
                raise RetryableError("Truncated file")
            if md5 and digest.hexdigest() != md5.lower():
                raise RetryableError("Source checksum mismatch")
            validate_file(temporary, fmt)
            temporary.replace(path)
            return {"sha256": sha.hexdigest(), "md5": digest.hexdigest(), "bytes": size}

        try:
            return self._perform(url, parse)
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


def file_hash(path: Path, algorithm="sha256") -> str:
    digest = hashlib.new(algorithm, usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
