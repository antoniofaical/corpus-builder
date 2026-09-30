from dataclasses import replace

import httpx
import pytest

from corpus_builder.errors import RemoteError
from corpus_builder.pmc import PMC, file_source
from corpus_builder.transport import RateLimiter, Transport


def test_rate_limiter_paces_all_requests(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("corpus_builder.transport.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "corpus_builder.transport.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )
    limiter = RateLimiter(10)
    starts = []
    for _ in range(21):
        limiter.wait()
        starts.append(clock[0])
    assert all(b - a >= 0.1 - 1e-9 for a, b in zip(starts, starts[1:], strict=False))


def test_retry_after_and_429(config, monkeypatch):
    calls = []
    sleeps = []
    monkeypatch.setattr(RateLimiter, "wait", lambda self: None)
    monkeypatch.setattr("corpus_builder.transport.time.sleep", sleeps.append)

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})

    client = Transport(
        config, "", lambda *a, **kw: None, httpx.Client(transport=httpx.MockTransport(handler))
    )
    try:
        assert client.json("https://example.org")["ok"]
        assert sleeps == [7] and len(calls) == 2
    finally:
        client.close()


def test_permanent_404_is_not_retried(config, monkeypatch):
    calls = []
    monkeypatch.setattr(RateLimiter, "wait", lambda self: None)

    def handler(request):
        calls.append(request)
        return httpx.Response(404)

    client = Transport(
        config, "", lambda *a, **kw: None, httpx.Client(transport=httpx.MockTransport(handler))
    )
    try:
        with pytest.raises(RemoteError, match="404"):
            client.json("https://example.org")
        assert len(calls) == 1
    finally:
        client.close()


def test_malformed_json_is_retried(config, monkeypatch):
    calls = []
    monkeypatch.setattr(RateLimiter, "wait", lambda self: None)
    monkeypatch.setattr(Transport, "_sleep_retry", lambda *args: None)

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"invalid")

    client = Transport(
        config, "", lambda *a, **kw: None, httpx.Client(transport=httpx.MockTransport(handler))
    )
    try:
        with pytest.raises(RemoteError, match="2 attempts"):
            client.json("https://example.org")
        assert len(calls) == 2
    finally:
        client.close()


def test_xml_external_entity_is_rejected(config, monkeypatch, tmp_path):
    monkeypatch.setattr(RateLimiter, "wait", lambda self: None)
    content = b"""<!DOCTYPE article [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
        <article>&xxe;</article>"""
    client = Transport(
        replace(config, max_attempts=1),
        "",
        lambda *a, **kw: None,
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=content))),
    )
    try:
        with pytest.raises(RemoteError):
            client.download("https://example.org", tmp_path / "article.xml", "xml", None)
        assert not (tmp_path / "article.xml").exists()
    finally:
        client.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://pmc-oa-opendata.s3.amazonaws.com/PMC123.1/a.pdf",
        "https://untrusted.example/PMC123.1/a.pdf",
        "s3://pmc-oa-opendata/PMC999.1/a.pdf",
        "s3://pmc-oa-opendata/PMC123.1/../other.pdf",
        "s3://pmc-oa-opendata/PMC123.1/a.pdf?md5=broken",
    ],
)
def test_download_sources_must_match_article_and_bucket(url):
    with pytest.raises(RemoteError):
        file_source(url, "PMC123", 1)


def test_listing_pagination_and_distinct_versions():
    class HTTP:
        def __init__(self):
            self.page = 0

        def xml(self, url, params):
            from defusedxml.ElementTree import fromstring

            self.page += 1
            assert self.page < 3
            if self.page == 2:
                assert params["continuation-token"] == "next"
            suffix = (
                (
                    "<IsTruncated>true</IsTruncated><NextContinuationToken>next"
                    "</NextContinuationToken>"
                )
                if self.page == 1
                else ("<IsTruncated>false</IsTruncated>")
            )
            return fromstring(f"""<ListBucketResult
                xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{suffix}
                <CommonPrefixes><Prefix>PMC123.{self.page}/</Prefix></CommonPrefixes>
                </ListBucketResult>""")

        def json(self, url):
            number = int(url.split(".")[-2])
            return {"pmcid": "PMC123", "version": number, "pmid": 101, "is_pmc_openaccess": True}

    versions = PMC(HTTP()).versions("PMC123", "101")
    assert {v["version"] for v in versions} == {1, 2}
