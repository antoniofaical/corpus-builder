import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest

from corpus_builder import BuildConfig, api
from corpus_builder.transport import RateLimiter, Transport

PDF = b"%PDF-1.4\nfixture bytes only, not a real scientific article\n%%EOF\n"
XML = b"<article><body><p>Fixture full text</p></body></article>"


class FakeService:
    def __init__(self):
        self.ids = ["101", "102"]
        self.calls = []
        self.hook = None
        self.no_pdf = False
        self.oa = True

    def __call__(self, request):
        self.calls.append(request)
        if self.hook:
            response = self.hook(request)
            if response is not None:
                return response
        fields = parse_qs(request.content.decode())
        path = request.url.path
        if path.endswith("esearch.fcgi"):
            return httpx.Response(
                200,
                json={
                    "esearchresult": {
                        "count": str(len(self.ids)),
                        "idlist": self.ids,
                        "querytranslation": "fixture query",
                        "querykey": "1",
                        "webenv": "fixture",
                    }
                },
            )
        if path.endswith("efetch.fcgi"):
            ids = fields["id"][0].split(",")
            records = "".join(
                f"""<PubmedArticle><MedlineCitation><PMID>{p}</PMID>
                <Article><ArticleTitle>Fixture <i>title</i> {p}</ArticleTitle>
                <Journal><Title>Fixture journal</Title><JournalIssue><PubDate><Year>2025</Year>
                </PubDate></JournalIssue></Journal><AuthorList><Author><ForeName>Ana</ForeName>
                <LastName>Example</LastName></Author></AuthorList>
                <Abstract><AbstractText Label="RESULTS">Example result.</AbstractText></Abstract>
                </Article></MedlineCitation><PubmedData><ArticleIdList>
                <ArticleId IdType="doi">10.0/{p}</ArticleId></ArticleIdList></PubmedData>
                </PubmedArticle>"""
                for p in ids
            )
            return httpx.Response(200, text=f"<PubmedArticleSet>{records}</PubmedArticleSet>")
        if path.endswith("elink.fcgi"):
            return httpx.Response(
                200,
                json={
                    "linksets": [
                        {
                            "ids": [p],
                            "linksetdbs": (
                                [{"linkname": "pubmed_pmc", "links": ["123"]}] if p == "101" else []
                            ),
                        }
                        for p in fields["id"]
                    ]
                },
            )
        if path == "/":
            return httpx.Response(
                200,
                text="""<ListBucketResult
                xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
                <IsTruncated>false</IsTruncated><CommonPrefixes>
                <Prefix>PMC123.1/</Prefix></CommonPrefixes></ListBucketResult>""",
            )
        if path == "/metadata/PMC123.1.json":
            obj = {
                "pmcid": "PMC123",
                "version": 1,
                "pmid": 101,
                "is_pmc_openaccess": self.oa,
                "is_manuscript": False,
                "license_code": "CC BY",
            }
            for fmt, content in (("pdf", PDF), ("xml", XML)):
                obj[fmt + "_url"] = (
                    f"s3://pmc-oa-opendata/PMC123.1/PMC123.1.{fmt}?md5="
                    + hashlib.md5(content).hexdigest()
                )
            if self.no_pdf:
                obj["pdf_url"] = None
            return httpx.Response(200, json=obj)
        if path.endswith(".pdf"):
            return httpx.Response(200, content=PDF)
        if path.endswith(".xml"):
            return httpx.Response(200, content=XML)
        raise AssertionError(f"Unexpected fixture route: {path}")


@pytest.fixture
def config():
    return BuildConfig(require_api_key=False, requests_per_second=3, max_attempts=2)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    service = FakeService()
    monkeypatch.setattr(RateLimiter, "wait", lambda self: None)
    monkeypatch.setattr(Transport, "_sleep_retry", lambda *args: None)
    monkeypatch.setattr(
        api,
        "Transport",
        lambda config, key, emit: Transport(
            config, key, emit, client=httpx.Client(transport=httpx.MockTransport(service))
        ),
    )
    return service


def read_manifest(directory):
    return [json.loads(line) for line in (directory / "manifest.jsonl").read_text().splitlines()]
