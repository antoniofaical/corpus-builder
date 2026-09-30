import csv
import json
import sqlite3
from dataclasses import replace

import httpx
import pytest
from conftest import read_manifest
from filelock import FileLock

from corpus_builder import ConfigurationError, QueryContext, build_corpus, import_run
from corpus_builder.identity import normalize_doi, publication_year


def rows(path):
    return list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))


def test_queries_share_identity_and_files_without_overwriting_runs(fake, config, tmp_path):
    catalog = tmp_path / "corpus"
    first = tmp_path / "first"
    second = tmp_path / "second"
    a = build_corpus(
        "alpha",
        first,
        config,
        corpus_dir=catalog,
        context=QueryContext("Q1", "v1", "T1", "hardware"),
    )
    before = (first / "manifest.jsonl").read_bytes()
    fake.calls.clear()
    b = build_corpus(
        "beta",
        second,
        config,
        corpus_dir=catalog,
        context=QueryContext("Q2", "v2", "T2", "firmware"),
    )
    assert a.run_id != b.run_id
    assert b.counts["files_reused"] == 2
    assert not any(r.url.path.endswith((".pdf", ".xml")) for r in fake.calls)
    assert (first / "manifest.jsonl").read_bytes() == before
    hits = rows(catalog / "query_hits.csv")
    assert len(hits) == 4
    assert len({h["article_id"] for h in hits}) == 2
    assert {h["query_id"] for h in hits} == {"Q1", "Q2"}
    report = json.loads((catalog / "corpus_report.json").read_text())
    assert report["bibliographic_deduplication"] is False
    assert report["query_overlap"][0]["articles"] == 2
    assert len(rows(catalog / "articles.csv")) == 2
    assert not list(catalog.glob("*duplicate*"))


def test_same_doi_is_not_merged(fake, config, tmp_path):
    def hook(request):
        if request.url.path.endswith("efetch.fcgi"):
            return httpx.Response(
                200,
                text="<PubmedArticleSet>"
                + "".join(
                    f"<PubmedArticle><MedlineCitation><PMID>{p}</PMID><Article>"
                    "<ArticleTitle>Identical title</ArticleTitle></Article></MedlineCitation>"
                    '<PubmedData><ArticleIdList><ArticleId IdType="doi">10.1234/same</ArticleId>'
                    "</ArticleIdList></PubmedData></PubmedArticle>"
                    for p in fake.ids
                )
                + "</PubmedArticleSet>",
            )

    fake.hook = hook
    build_corpus("fixture", tmp_path, config)
    articles = rows(tmp_path / "catalog/articles.csv")
    assert len(articles) == 2 and len({a["article_id"] for a in articles}) == 2
    assert {a["doi"] for a in articles} == {"10.1234/same"}


def test_query_version_conflict_before_network(fake, config, tmp_path):
    catalog = tmp_path / "corpus"
    context = QueryContext("Q", "1", "T1", "technical")
    build_corpus("original", tmp_path / "one", config, context=context, corpus_dir=catalog)
    original = (catalog / "queries.csv").read_bytes()
    fake.calls.clear()
    with pytest.raises(ConfigurationError, match="different expression"):
        build_corpus("changed", tmp_path / "two", config, context=context, corpus_dir=catalog)
    assert not fake.calls
    assert (catalog / "queries.csv").read_bytes() == original
    build_corpus(
        "changed",
        tmp_path / "three",
        config,
        context=replace(context, query_version="2"),
        corpus_dir=catalog,
    )
    assert len(rows(catalog / "queries.csv")) == 2


def test_resume_context_cannot_silently_change(fake, config, tmp_path):
    first = build_corpus("fixture", tmp_path, config, context=QueryContext("Q", "1", "T1"))
    resumed = build_corpus("fixture", tmp_path, config)
    assert resumed.run_id == first.run_id
    with pytest.raises(ConfigurationError, match="different query context"):
        build_corpus("fixture", tmp_path, config, context=QueryContext("Q", "1", "T2"))


def test_import_v1_is_readonly_and_idempotent(fake, config, tmp_path):
    source, dest = tmp_path / "old", tmp_path / "new"
    original = build_corpus("fixture", source, config)
    db = sqlite3.connect(source / "state.sqlite3")
    run = json.loads(db.execute("SELECT value FROM kv WHERE key='run'").fetchone()[0])
    run["schema_version"] = 1
    run.pop("context")
    with db:
        db.execute("UPDATE kv SET value=? WHERE key='run'", (json.dumps(run),))
    db.close()
    before_db = (source / "state.sqlite3").read_bytes()
    before_report = (source / "manifest.jsonl").read_bytes()
    fake.calls.clear()
    for _ in range(2):
        result = import_run(source, dest)
        assert result["run_id"] == original.run_id
        assert not result["unavailable_local_files"]
    assert not fake.calls
    assert (source / "state.sqlite3").read_bytes() == before_db
    assert (source / "manifest.jsonl").read_bytes() == before_report
    assert len(rows(dest / "query_hits.csv")) == 2
    imported = rows(dest / "queries.csv")[0]
    assert imported["track"] == imported["technical_stratum"] == imported["query_version"] == ""


def test_resume_v1_makes_backup(fake, config, tmp_path):
    first = build_corpus("fixture", tmp_path, config)
    db = sqlite3.connect(tmp_path / "state.sqlite3")
    run = json.loads(db.execute("SELECT value FROM kv WHERE key='run'").fetchone()[0])
    run["schema_version"] = 1
    run.pop("context")
    with db:
        db.execute("UPDATE kv SET value=? WHERE key='run'", (json.dumps(run),))
    db.close()
    resumed = build_corpus("fixture", tmp_path, config)
    assert resumed.run_id == first.run_id
    backup = sqlite3.connect(tmp_path / "state.v1.backup.sqlite3")
    assert (
        json.loads(backup.execute("SELECT value FROM kv WHERE key='run'").fetchone()[0])[
            "schema_version"
        ]
        == 1
    )
    backup.close()


def test_catalog_lock_prevents_two_writers(fake, config, tmp_path):
    catalog = tmp_path / "corpus"
    catalog.mkdir()
    with FileLock(catalog / ".catalog.lock"):
        with pytest.raises(ConfigurationError, match="corpus catalog"):
            build_corpus("fixture", tmp_path / "run", config, corpus_dir=catalog)
    assert not fake.calls


def test_metadata_fields_and_closed_candidates_remain(fake, config, tmp_path):
    build_corpus("fixture", tmp_path, config)
    row = rows(tmp_path / "manifest.csv")[1]
    assert row["year"] == "2025"
    assert "Example result" in row["abstract"]
    assert row["access_status"] == "unknown" and row["retrieval_status"] == "not_found"
    assert read_manifest(tmp_path)[1]["metadata"]["field_provenance"]["title"]["source"] == "pubmed"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://doi.org/10.1234/AbC", "10.1234/abc"),
        (" DOI:10.1234/AbC ", "10.1234/abc"),
        ("not a doi", None),
        ("10.1234/abc.", "10.1234/abc."),
    ],
)
def test_doi_normalization_preserves_suffix(value, expected):
    assert normalize_doi(value) == expected


def test_year_is_not_guessed_for_ranges():
    assert publication_year({"MedlineDate": "2020 Dec-2021 Jan"}) is None
    assert publication_year({"MedlineDate": "2020 Spring"}) == 2020
    assert publication_year({"Year": "2019", "MedlineDate": "2020"}) == 2019


def test_resume_uses_recorded_catalog_without_repeating_argument(fake, config, tmp_path):
    run = tmp_path / "run"
    shared = tmp_path / "shared"
    build_corpus("fixture", run, config, corpus_dir=shared)
    resumed = build_corpus("fixture", run, config)
    assert resumed.catalog_dir == shared
    assert not (run / "catalog").exists()


def test_source_set_cannot_change_during_resume(fake, config, tmp_path):
    build_corpus("fixture", tmp_path, config)
    fake.calls.clear()
    with pytest.raises(ConfigurationError, match="sources"):
        build_corpus("fixture", tmp_path, replace(config, europe_pmc=True))
    assert not fake.calls


def test_corrupt_cache_and_run_file_force_download(fake, config, tmp_path):
    build_corpus("fixture", tmp_path, config)
    file = read_manifest(tmp_path)[0]["files"][0]
    (tmp_path / file["path"]).write_bytes(b"broken")
    (tmp_path / "catalog/objects" / file["sha256"][:2] / file["sha256"]).write_bytes(b"broken")
    fake.calls.clear()
    result = build_corpus("fixture", tmp_path, config)
    assert result.counts["files_downloaded"] == 1 and result.counts["files_reused"] == 1
    assert len(fake.calls) == 1


def test_cli_import_needs_no_key_or_config(fake, config, tmp_path, capsys):
    from corpus_builder.cli import main

    run = tmp_path / "run"
    build_corpus("fixture", run, config)
    fake.calls.clear()
    assert (
        main(
            [
                "--import-run",
                str(run),
                "--corpus-dir",
                str(tmp_path / "shared"),
                "--config",
                str(tmp_path / "missing.toml"),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["records_imported"] == 2 and not fake.calls
