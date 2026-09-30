import json
from dataclasses import replace
from pathlib import Path

import pytest
from filelock import FileLock

from corpus_builder import (
    BuildConfig,
    ConfigurationError,
    QueryContext,
    QuerySpec,
    batch,
    load_queries,
    run_batch,
)
from corpus_builder.errors import AuthenticationError, DiscoveryError
from corpus_builder.models import BuildResult
from corpus_builder.orchestrator import main


def stub_result(directory, status="completed"):
    return BuildResult(
        "test-run",
        status,
        True,
        {"records": 1},
        Path(directory),
        Path(directory) / "report.json",
        Path(directory) / "manifest.jsonl",
        (),
    )


@pytest.fixture
def runner(monkeypatch):
    calls = []
    outcomes = []

    def call(query, directory, config, **kwargs):
        calls.append({"query": query, "directory": directory, "config": config, **kwargs})
        outcome = outcomes.pop(0) if outcomes else "completed"
        if isinstance(outcome, BaseException):
            raise outcome
        kwargs["on_event"]({"event_id": "core-id", "stage": "record_processed"})
        return stub_result(directory, outcome)

    monkeypatch.setattr(batch, "build_corpus", call)
    monkeypatch.setattr(batch.RateLimiter, "wait", lambda self: None)
    return calls, outcomes


def test_json_preserves_exact_queries_and_context(tmp_path):
    path = tmp_path / "queries.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "queries": [
                    ' ("biosensors"[MeSH Terms])\nAND microneedle* ',
                    {
                        "query": "second OR third",
                        "query_id": "Q2",
                        "query_version": "v1",
                        "track": "T2",
                        "technical_stratum": "hardware",
                    },
                ],
            }
        ),
        encoding="utf-8-sig",
    )
    specs = load_queries(path)
    assert specs[0].query == ' ("biosensors"[MeSH Terms])\nAND microneedle* '
    assert specs[1].context == QueryContext("Q2", "v1", "T2", "hardware")


@pytest.mark.parametrize(
    "obj",
    [
        [],
        {"queries": []},
        {"schema_version": True, "queries": []},
        {"schema_version": 2, "queries": []},
        {"schema_version": 1, "queries": "one"},
        {"schema_version": 1, "queries": [""]},
        {"schema_version": 1, "queries": [{"query": "a", "query_id": "missing-version"}]},
        {"schema_version": 1, "queries": [{"query": "a", "typo": "b"}]},
        {"schema_version": 1, "queries": [{"query": "a", "database": "scopus"}]},
    ],
)
def test_invalid_input_rejected(tmp_path, obj):
    path = tmp_path / "queries.json"
    path.write_text(json.dumps(obj))
    with pytest.raises(ConfigurationError):
        load_queries(path)


def test_duplicate_json_keys_rejected(tmp_path):
    path = tmp_path / "queries.json"
    path.write_text('{"schema_version":1,"queries":[],"queries":["a"]}')
    with pytest.raises(ConfigurationError, match="Repeated JSON"):
        load_queries(path)


def test_exactly_n_ordered_calls_including_repeated_queries(runner, config, tmp_path):
    calls, _ = runner
    inputs = ["one", "two OR three", "one"]
    result = run_batch(inputs, tmp_path, config)
    assert [c["query"] for c in calls] == inputs
    assert len({c["directory"] for c in calls}) == 3
    assert {c["corpus_dir"] for c in calls} == {tmp_path / "catalog"}
    assert result.status == "completed" and result.counts["completed"] == 3
    assert all(len(j["attempts"]) == 1 for j in result.jobs)
    events = [json.loads(s) for s in (tmp_path / "batch_events.jsonl").read_text().splitlines()]
    assert [e["index"] for e in events if e["stage"] == "query_started"] == [1, 2, 3]
    assert any(e["stage"] == "core_event" and e["entry_id"] == "q000001" for e in events)


def test_failures_do_not_stop_remaining_queries(runner, config, tmp_path):
    calls, outcomes = runner
    outcomes.extend(["partial", DiscoveryError("invalid query response"), "completed"])
    result = run_batch(["a", "b", "c"], tmp_path, config)
    assert len(calls) == 3 and result.status == "partial"
    assert [j["status"] for j in result.jobs] == ["partial", "failed", "completed"]
    assert result.jobs[1]["attempts"][0]["error"] == "invalid query response"


def test_resume_retries_only_unfinished_and_remembers_catalog(runner, config, tmp_path):
    calls, outcomes = runner
    outcomes.extend(["completed", "partial", "failed"])
    run = tmp_path / "batch"
    first = run_batch(["a", "b", "c"], run, config, corpus_dir=tmp_path / "shared")
    calls.clear()
    second = run_batch(["a", "b", "c"], run, config)
    assert second.batch_id == first.batch_id and second.status == "completed"
    assert [c["query"] for c in calls] == ["b", "c"]
    assert {c["corpus_dir"] for c in calls} == {tmp_path / "shared"}
    assert second.counts["attempted_this_invocation"] == 2
    assert second.counts["skipped_completed"] == 1
    assert [len(j["attempts"]) for j in second.jobs] == [1, 2, 2]


def test_recheck_completed_calls_core_again(runner, config, tmp_path):
    calls, _ = runner
    run_batch(["a", "b"], tmp_path, config)
    calls.clear()
    result = run_batch(["a", "b"], tmp_path, config, recheck_completed=True)
    assert len(calls) == 2 and result.counts["skipped_completed"] == 0


def test_interrupt_persists_checkpoint_and_resumes(runner, config, tmp_path):
    calls, outcomes = runner
    outcomes.extend(["completed", KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        run_batch(["a", "b", "c"], tmp_path, config)
    report = json.loads((tmp_path / "batch_report.json").read_text())
    assert report["status"] == "interrupted"
    assert [j["status"] for j in report["jobs"]] == ["completed", "interrupted", "pending"]
    calls.clear()
    result = run_batch(["a", "b", "c"], tmp_path, config)
    assert result.status == "completed" and [c["query"] for c in calls] == ["b", "c"]


def test_fatal_auth_stops_batch_and_records_pending(runner, config, tmp_path):
    calls, outcomes = runner
    outcomes.append(AuthenticationError("NCBI rejected API key"))
    result = run_batch(["a", "b"], tmp_path, config)
    assert len(calls) == 1 and result.status == "failed"
    assert result.counts["pending"] == 1
    assert "Authentication rejected" in result.jobs[0]["attempts"][0]["error"]


def test_unexpected_exception_is_not_hidden_and_reported(runner, config, tmp_path):
    calls, outcomes = runner
    outcomes.append(ValueError("secret-caller-details"))
    with pytest.raises(ValueError):
        run_batch(["a", "b"], tmp_path, config)
    assert len(calls) == 1
    report = (tmp_path / "batch_report.json").read_text()
    assert json.loads(report)["status"] == "failed"
    assert "secret-caller-details" not in report


def test_empty_batch_completes_without_key(runner, tmp_path):
    calls, _ = runner
    result = run_batch([], tmp_path, BuildConfig(api_key_env="NO_KEY"))
    assert not calls and result.status == "completed" and result.counts["total"] == 0


def test_all_input_validated_before_first_call(runner, config, tmp_path):
    calls, _ = runner
    with pytest.raises(ConfigurationError):
        run_batch(["valid", " "], tmp_path / "new", config)
    assert not calls and not (tmp_path / "new").exists()
    with pytest.raises(ConfigurationError, match="Conflicting"):
        run_batch(
            [QuerySpec("a", QueryContext("Q1", "1")), QuerySpec("b", QueryContext("Q1", "1"))],
            tmp_path,
            config,
        )
    assert not calls


def test_changed_list_does_not_overwrite_existing_batch(runner, config, tmp_path):
    calls, _ = runner
    run_batch(["a", "b"], tmp_path, config)
    before = (tmp_path / "batch_state.json").read_bytes()
    calls.clear()
    for queries, cfg in (
        (["b", "a"], config),
        (["a", "b", "c"], config),
        (["a", "b"], replace(config, formats=("xml",))),
        (["a", "b"], replace(config, resume=False)),
    ):
        with pytest.raises(ConfigurationError):
            run_batch(queries, tmp_path, cfg)
    assert not calls and (tmp_path / "batch_state.json").read_bytes() == before


def test_batch_lock_and_unrelated_files(runner, config, tmp_path):
    calls, _ = runner
    with FileLock(tmp_path / ".batch.lock"):
        with pytest.raises(ConfigurationError, match="Another process"):
            run_batch(["a"], tmp_path, config)
    (tmp_path / "notes.txt").write_text("preserve")
    with pytest.raises(ConfigurationError, match="unrelated"):
        run_batch(["a"], tmp_path, config)
    assert not calls and (tmp_path / "notes.txt").read_text() == "preserve"


def test_callback_failure_does_not_stop_batch(runner, config, tmp_path):
    def callback(event):
        raise ValueError("private exception")

    assert run_batch(["a"], tmp_path, config, on_event=callback).status == "completed"
    events = (tmp_path / "batch_events.jsonl").read_text()
    assert "callback_failed" in events and "private exception" not in events


def test_interrupt_before_core_call_has_consistent_state(runner, config, tmp_path):
    def callback(event):
        if event["stage"] == "query_started":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_batch(["a"], tmp_path, config, on_event=callback)
    state = json.loads((tmp_path / "batch_state.json").read_text())
    assert state["status"] == state["jobs"][0]["status"] == "interrupted"


def test_real_core_multiple_queries_preserve_hits_and_reuse_files(fake, config, tmp_path):
    result = run_batch(
        [
            QuerySpec("a", QueryContext("Q1", "1", "T1")),
            QuerySpec("b", QueryContext("Q2", "1", "T2")),
        ],
        tmp_path,
        config,
    )
    assert result.status == "completed"
    assert result.jobs[1]["result"]["counts"]["files_reused"] == 2
    report = json.loads((tmp_path / "catalog/corpus_report.json").read_text())
    assert report["queries"] == 2 and report["query_hits"] == 4
    fake.calls.clear()
    resumed = run_batch(
        [
            QuerySpec("a", QueryContext("Q1", "1", "T1")),
            QuerySpec("b", QueryContext("Q2", "1", "T2")),
        ],
        tmp_path,
        config,
    )
    assert resumed.counts["skipped_completed"] == 2 and not fake.calls


def test_cli_uses_json_and_returns_machine_readable_summary(fake, tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[ncbi]\nrequire_api_key=false\nrequests_per_second=3\n")
    queries = tmp_path / "queries.json"
    queries.write_text('{"schema_version":1,"queries":["a","b"]}')
    code = main(
        [
            "--queries",
            str(queries),
            "--output-dir",
            str(tmp_path / "batch"),
            "--config",
            str(cfg),
            "--quiet",
        ]
    )
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["counts"]["completed"] == result["counts"]["total"] == 2


def test_corrupt_checkpoint_cannot_redirect_a_job(runner, config, tmp_path):
    run_batch(["a"], tmp_path, config)
    path = tmp_path / "batch_state.json"
    state = json.loads(path.read_text())
    state["jobs"][0]["run_dir"] = "../../outside"
    path.write_text(json.dumps(state))
    with pytest.raises(ConfigurationError, match="Invalid job"):
        run_batch(["a"], tmp_path, config, recheck_completed=True)


def test_query_boundaries_are_paced_when_http_clients_are_recreated(
    runner, config, tmp_path, monkeypatch
):
    scheduled = []
    monkeypatch.setattr(batch.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(batch.RateLimiter, "wait", lambda self: scheduled.append(self.next_start))
    run_batch(["a", "b", "c"], tmp_path, replace(config, europe_pmc=True))
    assert scheduled == [0.0, 11.0, 11.0]


def test_hard_stop_running_marker_is_recovered(runner, config, tmp_path):
    calls, _ = runner
    run_batch(["a"], tmp_path, config)
    path = tmp_path / "batch_state.json"
    state = json.loads(path.read_text())
    state["jobs"][0]["status"] = "running"
    state["jobs"][0]["attempts"][-1]["status"] = "running"
    path.write_text(json.dumps(state))
    calls.clear()
    result = run_batch(["a"], tmp_path, config)
    assert len(calls) == 1 and result.status == "completed"
    assert [a["status"] for a in result.jobs[0]["attempts"]] == ["interrupted", "completed"]


def test_completed_batch_can_be_opened_without_key(runner, config, tmp_path):
    calls, _ = runner
    run_batch(["a"], tmp_path, config)
    calls.clear()
    result = run_batch(
        ["a"], tmp_path, replace(config, require_api_key=True, api_key_env="ABSENT_KEY")
    )
    assert not calls and result.counts["skipped_completed"] == 1
