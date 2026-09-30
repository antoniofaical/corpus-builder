import json
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import PDF, read_manifest
from filelock import FileLock

from corpus_builder import BuildConfig, ConfigurationError, build_corpus
from corpus_builder.errors import AuthenticationError
from corpus_builder.state import State


def test_end_to_end_and_resume_without_network(fake, config, tmp_path):
    result = build_corpus('"biomedical sensors"[Title/Abstract]', tmp_path, config)
    assert result.status == "completed" and result.discovery_complete
    assert result.counts["records"] == 2
    assert result.counts["records_no_pmc"] == 1
    assert result.counts["files_verified"] == 2
    assert result.counts["articles_with_files"] == 1
    records = read_manifest(tmp_path)
    assert records[0]["metadata"]["title"] == "Fixture title 101"
    assert records[0]["metadata"]["authors"] == ["Ana Example"]
    assert records[0]["metadata"]["abstract"][0]["label"] == "RESULTS"
    fake.calls.clear()
    second = build_corpus('"biomedical sensors"[Title/Abstract]', tmp_path, config)
    assert second.run_id == result.run_id
    assert second.counts["files_reused"] == 2
    assert not fake.calls
    assert (
        len(
            {
                e["event_id"]
                for e in map(json.loads, (tmp_path / "events.jsonl").read_text().splitlines())
            }
        )
        > 1
    )


def test_missing_format_is_not_a_transport_failure(fake, config, tmp_path):
    fake.no_pdf = True
    result = build_corpus("fixture", tmp_path, config)
    assert result.status == "completed"
    assert result.counts["formats_unavailable"] == 1
    assert result.counts["files_failed"] == 0
    assert read_manifest(tmp_path)[0]["status"] == "downloaded_with_unavailable_formats"


def test_pdf_only_no_format(fake, config, tmp_path):
    fake.no_pdf = True
    result = build_corpus("fixture", tmp_path, replace(config, formats=("pdf",)))
    assert result.counts["records_format_unavailable"] == 1
    assert result.counts["articles_with_files"] == 0


def test_non_oa_versions_are_preserved_but_not_downloaded(fake, config, tmp_path):
    fake.oa = False
    result = build_corpus("fixture", tmp_path, config)
    assert result.counts["records_not_in_oa_subset"] == 1
    assert result.counts["files_verified"] == 0
    assert read_manifest(tmp_path)[0]["versions"][0]["is_pmc_openaccess"] is False


def test_empty_distribution_is_distinct_from_non_oa(fake, config, tmp_path):
    fake.hook = lambda r: (
        httpx.Response(
            200,
            text="""<ListBucketResult
        xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
        <IsTruncated>false</IsTruncated></ListBucketResult>""",
        )
        if r.url.path == "/"
        else None
    )
    result = build_corpus("fixture", tmp_path, config)
    assert result.counts["records_not_in_pmc_distribution"] == 1


def test_corrupted_download_is_rejected_and_retried(fake, config, tmp_path):
    fake.hook = lambda r: (
        httpx.Response(200, content=b"<html>Blocked</html>")
        if r.url.path.endswith(".pdf")
        else None
    )
    result = build_corpus("fixture", tmp_path, config)
    assert result.status == "partial"
    assert result.counts["files_failed"] == 1
    assert not (tmp_path / "articles/PMC123.1/PMC123.1.pdf").exists()
    assert not list(tmp_path.rglob("*.part"))
    assert sum(r.url.path.endswith(".pdf") for r in fake.calls) == 2
    fake.hook = None
    resumed = build_corpus("fixture", tmp_path, config)
    assert resumed.status == "completed" and resumed.counts["files_reused"] == 1


def test_tampered_file_is_downloaded_again(fake, config, tmp_path):
    build_corpus("fixture", tmp_path, config)
    pdf = tmp_path / "articles/PMC123.1/PMC123.1.pdf"
    pdf.write_bytes(b"broken")
    fake.calls.clear()
    result = build_corpus("fixture", tmp_path, config)
    assert result.counts["files_downloaded"] == 1
    assert result.counts["files_reused"] == 1
    assert len(fake.calls) == 1 and pdf.read_bytes() == PDF


def test_interrupt_preserves_completed_files(fake, config, tmp_path):
    def interrupt(request):
        if request.url.path.endswith(".xml"):
            raise KeyboardInterrupt

    fake.hook = interrupt
    with pytest.raises(KeyboardInterrupt):
        build_corpus("fixture", tmp_path, config)
    assert json.loads((tmp_path / "report.json").read_text())["status"] == "interrupted"
    assert (tmp_path / "articles/PMC123.1/PMC123.1.pdf").exists()
    fake.hook = None
    result = build_corpus("fixture", tmp_path, config)
    assert result.status == "completed" and result.counts["files_reused"] == 1


def test_omitted_link_record_is_error_not_no_pmc(fake, config, tmp_path):
    fake.hook = lambda r: (
        httpx.Response(200, json={"linksets": []}) if r.url.path.endswith("elink.fcgi") else None
    )
    result = build_corpus("fixture", tmp_path, config)
    assert result.status == "partial"
    assert result.counts["records_error"] == 2
    assert result.counts.get("records_no_pmc", 0) == 0


def test_elink_repeated_ids_preserve_mapping(fake, config, tmp_path):
    build_corpus("fixture", tmp_path, config)
    request = next(r for r in fake.calls if r.url.path.endswith("elink.fcgi"))
    assert parse_qs(request.content.decode())["id"] == ["101", "102"]


def test_invalid_identity_is_error(fake, config, tmp_path):
    fake.hook = lambda r: (
        httpx.Response(200, json={"pmcid": "PMC999", "version": 1})
        if r.url.path.endswith(".json")
        else None
    )
    result = build_corpus("fixture", tmp_path, config)
    assert result.status == "partial" and result.counts["files_verified"] == 0


def test_empty_search_is_successful_zero_result(fake, config, tmp_path):
    fake.ids = []
    result = build_corpus("fixture", tmp_path, config)
    assert result.status == "completed" and result.discovery_complete
    assert result.counts["records"] == 0


def test_truncated_small_search_is_failed(fake, config, tmp_path):
    fake.hook = lambda r: (
        httpx.Response(
            200,
            json={
                "esearchresult": {
                    "count": "3",
                    "idlist": ["101"],
                    "querykey": "1",
                    "webenv": "fixture",
                }
            },
        )
        if r.url.path.endswith("esearch.fcgi")
        else None
    )
    result = build_corpus("fixture", tmp_path, config)
    assert result.status == "failed" and not result.discovery_complete
    assert not any(r.url.path.endswith("efetch.fcgi") for r in fake.calls)


def test_incompatible_resume_preserves_existing_artifacts(fake, config, tmp_path):
    build_corpus("fixture", tmp_path, config)
    original = (tmp_path / "manifest.jsonl").read_bytes()
    for query, cfg in (
        ("another query", config),
        ("fixture", replace(config, formats=("pdf",))),
        ("fixture", replace(config, resume=False)),
    ):
        with pytest.raises(ConfigurationError):
            build_corpus(query, tmp_path, cfg)
        assert (tmp_path / "manifest.jsonl").read_bytes() == original


def test_output_lock(fake, config, tmp_path):
    with FileLock(tmp_path / ".run.lock"):
        with pytest.raises(ConfigurationError, match="Another process"):
            build_corpus("fixture", tmp_path, config)


def test_unrelated_files_are_preserved(fake, config, tmp_path):
    (tmp_path / "notes.txt").write_text("preserve")
    with pytest.raises(ConfigurationError):
        build_corpus("fixture", tmp_path, config)
    assert (tmp_path / "notes.txt").read_text() == "preserve"


def test_configured_key_is_only_in_ncbi_requests(fake, config, tmp_path, monkeypatch):
    monkeypatch.setenv("MY_PUBMED_KEY", "TEST_SECRET_DO_NOT_LOG")
    cfg = replace(config, api_key_env="MY_PUBMED_KEY", require_api_key=True, requests_per_second=10)
    build_corpus("fixture", tmp_path, cfg)
    for request in fake.calls:
        assert (b"TEST_SECRET_DO_NOT_LOG" in request.content) == (
            request.url.host == "eutils.ncbi.nlm.nih.gov"
        )
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"TEST_SECRET_DO_NOT_LOG" not in path.read_bytes()


def test_authentication_failure_is_fatal_and_reported(fake, config, tmp_path):
    fake.hook = lambda r: httpx.Response(400, json={"error": "API key invalid"})
    with pytest.raises(AuthenticationError):
        build_corpus("fixture", tmp_path, config)
    assert json.loads((tmp_path / "report.json").read_text())["status"] == "failed"
    assert len(fake.calls) == 1


def test_callback_failure_does_not_break_collection(fake, config, tmp_path):
    def callback(event):
        raise ValueError("sensitive caller details")

    result = build_corpus("fixture", tmp_path, config, on_event=callback)
    assert result.status == "completed"
    events = (tmp_path / "events.jsonl").read_text()
    assert "callback_failed" in events and "sensitive caller details" not in events


def test_missing_key_fails_before_creating_run(monkeypatch, tmp_path):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="missing or empty"):
        build_corpus("fixture", tmp_path / "run", BuildConfig(api_key_env="MISSING_KEY"))
    assert not (tmp_path / "run").exists()


def test_small_search_resume_recovers_ids(fake, config, tmp_path):
    state = State(tmp_path, "fixture", config)
    state.set("search", {"count": 2, "complete": False, "querykey": "1", "webenv": "fixture"})
    state.close()
    result = build_corpus("fixture", tmp_path, config)
    assert result.status == "completed" and result.discovery_complete
