import json
import socket
from dataclasses import replace

import httpx
import pytest
from conftest import PDF, XML, read_manifest

from corpus_builder import build_corpus
from corpus_builder.errors import RemoteError
from corpus_builder.transport import Transport, validate_public_url


@pytest.fixture(autouse=True)
def public_fixture_hosts(monkeypatch):
    def lookup(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)


def provider_hook(request):
    if request.url.host == "www.ebi.ac.uk":
        if request.url.path.endswith("/search"):
            pmid = request.url.params["query"].split()[0].split(":")[1]
            return httpx.Response(
                200,
                json={
                    "hitCount": 1,
                    "resultList": {
                        "result": [
                            {
                                "id": pmid,
                                "source": "MED",
                                "pmcid": "PMC987",
                                "isOpenAccess": "Y",
                                "title": "Supplemental title",
                                "license": "CC BY",
                                "keywordList": {"keyword": ["sensor"]},
                            }
                        ]
                    },
                },
            )
        return httpx.Response(200, content=XML)
    if request.url.path.endswith("efetch.fcgi"):
        return httpx.Response(
            200,
            text="""<PubmedArticleSet><PubmedArticle><MedlineCitation>
        <PMID>102</PMID><Article><ArticleTitle>Example</ArticleTitle></Article>
        <KeywordList><Keyword>biosignal</Keyword></KeywordList><MeshHeadingList><MeshHeading>
        <DescriptorName UI="D000001">Device</DescriptorName><QualifierName>methods</QualifierName>
        </MeshHeading></MeshHeadingList></MedlineCitation><PubmedData><ArticleIdList>
        <ArticleId IdType="doi">10.1234/example</ArticleId></ArticleIdList></PubmedData>
        </PubmedArticle></PubmedArticleSet>""",
        )
    if request.url.host == "api.unpaywall.org":
        return httpx.Response(
            200,
            json={
                "doi": "10.1234/example",
                "is_oa": True,
                "oa_locations": [
                    {
                        "url_for_pdf": "https://repository.example/article.pdf",
                        "license": "cc-by",
                        "version": "acceptedVersion",
                        "host_type": "repository",
                    }
                ],
            },
        )
    if request.url.host == "repository.example":
        return httpx.Response(200, content=PDF)


def test_non_pmc_article_downloaded_via_unpaywall(fake, config, tmp_path):
    fake.ids = ["102"]
    fake.hook = provider_hook
    config = replace(config, unpaywall=True, unpaywall_email="test@example.org")
    result = build_corpus("fixture", tmp_path, config)
    record = read_manifest(tmp_path)[0]
    assert result.status == "completed"
    assert record["pmcids"] == []
    assert record["access_status"] == "open_access" and record["retrieval_status"] == "downloaded"
    assert record["files"][0]["version"] == "acceptedVersion"
    assert record["metadata"]["keywords"] == ["biosignal"]
    assert record["metadata"]["mesh_terms"][0]["ui"] == "D000001"
    assert record["resolutions"]["unpaywall"]["metadata"]["doi"] == "10.1234/example"
    fake.calls.clear()
    second = build_corpus("fixture", tmp_path, config)
    assert not fake.calls and second.counts["files_reused"] == 1


def test_europe_pmc_fallback_keeps_discovery_source(fake, config, tmp_path):
    fake.ids = ["102"]
    fake.hook = provider_hook
    result = build_corpus("fixture", tmp_path, replace(config, europe_pmc=True))
    record = read_manifest(tmp_path)[0]
    assert result.status == "completed" and record["files"][0]["source"] == "europe_pmc"
    article = json.loads((tmp_path / "catalog/articles.jsonl").read_text())
    assert article["databases"] == ["pubmed"]
    assert article["resolutions"]["europe_pmc"]["metadata"]["title"] == "Supplemental title"


def test_oa_failed_download_stays_oa(fake, config, tmp_path):
    fake.ids = ["102"]
    fake.hook = lambda r: (
        httpx.Response(503) if r.url.host == "repository.example" else provider_hook(r)
    )
    result = build_corpus(
        "fixture", tmp_path, replace(config, unpaywall=True, unpaywall_email="a@b.org")
    )
    record = read_manifest(tmp_path)[0]
    assert result.status == "partial"
    assert (
        record["access_status"] == "open_access" and record["retrieval_status"] == "download_error"
    )
    assert record["errors"][0]["stage"] == "download"


def test_closed_has_dated_evidence_and_keeps_metadata(fake, config, tmp_path):
    fake.ids = ["102"]

    def hook(r):
        if r.url.host == "api.unpaywall.org":
            return httpx.Response(
                200, json={"doi": "10.1234/example", "is_oa": False, "oa_locations": []}
            )
        return provider_hook(r)

    fake.hook = hook
    build_corpus("fixture", tmp_path, replace(config, unpaywall=True, unpaywall_email="a@b.org"))
    record = read_manifest(tmp_path)[0]
    assert record["access_status"] == "closed" and record["metadata"]["title"] == "Example"
    assert record["access_evidence"][0]["observed_at"]
    assert record["retrieval_status"] == "not_found"


def test_resolver_failure_is_not_a_closed_article(fake, config, tmp_path):
    fake.ids = ["102"]
    fake.hook = lambda r: httpx.Response(503) if r.url.host == "www.ebi.ac.uk" else provider_hook(r)
    result = build_corpus("fixture", tmp_path, replace(config, europe_pmc=True))
    record = read_manifest(tmp_path)[0]
    assert result.status == "partial"
    assert record["access_status"] == "unknown" and record["retrieval_status"] == "pending"


def test_external_redirects_and_html_validation(config, tmp_path, monkeypatch):
    monkeypatch.setattr("corpus_builder.transport.RateLimiter.wait", lambda s: None)
    text = (
        '<html><div itemprop="articleBody"><h2>Methods</h2><p>'
        + ("word " * 350)
        + "</p><h2>Results</h2></div></html>"
    )

    def handler(r):
        if r.url.path == "/start":
            return httpx.Response(302, headers={"Location": "https://repository.example/full"})
        return httpx.Response(200, text=text)

    transport = Transport(
        config, "secret", lambda *a, **k: None, httpx.Client(transport=httpx.MockTransport(handler))
    )
    try:
        info = transport.download(
            "https://publisher.example/start",
            tmp_path / "article.html",
            "html",
            None,
            external=True,
        )
        assert info["resolved_url"] == "https://repository.example/full"
        assert (tmp_path / "article.html").exists()
    finally:
        transport.close()


@pytest.mark.parametrize(
    "content", [b"<html>Abstract only</html>", b"<html><input type='password'></html>"]
)
def test_landing_or_login_html_is_not_saved(config, tmp_path, content, monkeypatch):
    monkeypatch.setattr("corpus_builder.transport.RateLimiter.wait", lambda s: None)
    transport = Transport(
        replace(config, max_attempts=1),
        "",
        lambda *a, **k: None,
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=content))),
    )
    try:
        with pytest.raises(RemoteError):
            transport.download(
                "https://publisher.example", tmp_path / "a.html", "html", None, external=True
            )
        assert not (tmp_path / "a.html").exists()
        assert not list(tmp_path.glob("*.part"))
    finally:
        transport.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/a",
        "https://127.0.0.1/a",
        "https://[::1]/a",
        "file:///etc/passwd",
        "http://example.com/a",
        "https://user:password@example.com/a",
    ],
)
def test_reject_non_public_document_urls(url):
    with pytest.raises(RemoteError):
        validate_public_url(url)


def test_resolver_identity_mismatch_cannot_download(fake, config, tmp_path):
    fake.ids = ["102"]

    def hook(r):
        if r.url.host == "api.unpaywall.org":
            return httpx.Response(200, json={"doi": "10.1234/wrong", "is_oa": True})
        return provider_hook(r)

    fake.hook = hook
    result = build_corpus(
        "fixture", tmp_path, replace(config, unpaywall=True, unpaywall_email="a@b.org")
    )
    assert result.status == "partial"
    assert not any(r.url.host == "repository.example" for r in fake.calls)
    record = read_manifest(tmp_path)[0]
    assert record["access_status"] == "unknown" and not record["files"]


def test_unpaywall_404_is_unknown_not_closed(fake, config, tmp_path):
    fake.ids = ["102"]
    fake.hook = lambda r: (
        httpx.Response(404) if r.url.host == "api.unpaywall.org" else provider_hook(r)
    )
    result = build_corpus(
        "fixture", tmp_path, replace(config, unpaywall=True, unpaywall_email="a@b.org")
    )
    assert result.status == "completed"
    record = read_manifest(tmp_path)[0]
    assert record["access_status"] == "unknown" and record["retrieval_status"] == "not_found"


def test_supplement_fills_absent_field_with_provenance(fake, config, tmp_path):
    fake.ids = ["102"]

    def hook(r):
        response = provider_hook(r)
        if r.url.host == "www.ebi.ac.uk" and r.url.path.endswith("/search"):
            obj = json.loads(response.content)
            obj["resultList"]["result"][0]["abstractText"] = "<p>Open abstract.</p>"
            return httpx.Response(200, json=obj)
        return response

    fake.hook = hook
    build_corpus("fixture", tmp_path, replace(config, europe_pmc=True))
    meta = read_manifest(tmp_path)[0]["metadata"]
    assert meta["title"] == "Example"
    assert meta["field_provenance"]["title"]["source"] == "pubmed"
    assert meta["abstract"][0]["text"] == "Open abstract."
    assert meta["field_provenance"]["abstract"]["source"] == "europe_pmc"


def test_private_redirect_never_requested(config, tmp_path, monkeypatch):
    monkeypatch.setattr("corpus_builder.transport.RateLimiter.wait", lambda s: None)
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/secret"})

    transport = Transport(
        config, "", lambda *a, **k: None, httpx.Client(transport=httpx.MockTransport(handler))
    )
    try:
        with pytest.raises(RemoteError, match="public HTTPS"):
            transport.download(
                "https://publisher.example", tmp_path / "a.pdf", "pdf", None, external=True
            )
        assert len(calls) == 1
    finally:
        transport.close()


def test_download_size_limit_cleans_partial_file(config, tmp_path, monkeypatch):
    monkeypatch.setattr("corpus_builder.transport.RateLimiter.wait", lambda s: None)
    transport = Transport(
        replace(config, max_download_bytes=10),
        "",
        lambda *a, **k: None,
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=PDF))),
    )
    try:
        with pytest.raises(RemoteError, match="max_download_bytes"):
            transport.download(
                "https://publisher.example", tmp_path / "a.pdf", "pdf", None, external=True
            )
        assert not (tmp_path / "a.pdf").exists() and not list(tmp_path.glob("*.part"))
    finally:
        transport.close()


def test_config_requires_contact_for_unpaywall(config):
    from corpus_builder import ConfigurationError

    with pytest.raises(ConfigurationError, match="unpaywall_email"):
        replace(config, unpaywall=True).validate()


def test_malformed_provider_data_is_reported_and_retried_on_resume(fake, config, tmp_path):
    fake.ids = ["102"]
    fake.hook = lambda r: (
        httpx.Response(200, json={"hitCount": 1, "resultList": None})
        if r.url.host == "www.ebi.ac.uk"
        else provider_hook(r)
    )
    config = replace(config, europe_pmc=True)
    result = build_corpus("fixture", tmp_path, config)
    assert result.status == "partial"
    assert "europe_pmc" not in read_manifest(tmp_path)[0]["resolutions"]
    fake.hook = provider_hook
    assert build_corpus("fixture", tmp_path, config).status == "completed"


def test_public_name_resolving_to_private_address_is_rejected(monkeypatch):
    from corpus_builder.transport import validate_public_host

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))],
    )
    with pytest.raises(RemoteError, match="non-public"):
        validate_public_host("repository.example")
