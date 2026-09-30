import re

import pytest

from corpus_builder import pubmed
from corpus_builder.errors import DiscoveryError
from corpus_builder.pubmed import PubMed
from corpus_builder.state import State


def install_search(monkeypatch, ids, *, limit=9999, upper=20000):
    monkeypatch.setattr(pubmed, "SEARCH_LIMIT", limit)
    monkeypatch.setattr(pubmed, "UID_MAX", upper)
    calls = []

    def search(query, webenv=None, limit=limit):
        calls.append(query)
        match = re.search(r"(\d+):(\d+)\[UID\]", query)
        selected = ids
        if match:
            lo, hi = map(int, match.groups())
            selected = [p for p in ids if lo <= int(p) <= hi]
        return {
            "count": len(selected),
            "idlist": selected[:limit],
            "querykey": "1",
            "webenv": "frozen",
            "querytranslation": query,
        }

    client = PubMed(None)
    client.search = search
    return client, calls


def test_more_than_ten_thousand_is_complete(monkeypatch, config, tmp_path):
    ids = [str(i) for i in range(1, 10004)]
    client, calls = install_search(monkeypatch, ids)
    state = State(tmp_path, "fixture", config)
    try:
        client.discover("fixture", state, lambda *a, **kw: None)
        assert state.count() == 10003
        assert state.get("search")["complete"] is True
        assert {r["pmid"] for r in state.records()} == set(ids)
        assert any("[UID]" in call for call in calls)
    finally:
        state.close()


def test_partition_resume_does_not_refetch_completed_leaves(monkeypatch, config, tmp_path):
    client, calls = install_search(monkeypatch, [str(i) for i in range(1, 11)], limit=3, upper=16)
    state = State(tmp_path, "fixture", config)
    original = client.search
    completed = []

    def emit(stage, **details):
        if stage == "discovery_partition_completed":
            completed.append(f"#1 AND {details['lo']}:{details['hi']}[UID]")
            raise KeyboardInterrupt

    try:
        with pytest.raises(KeyboardInterrupt):
            client.discover("fixture", state, emit)
        assert state.count() > 0
        calls.clear()
        client.search = original
        client.discover("fixture", state, lambda *a, **kw: None)
        assert state.count() == 10 and state.get("search")["complete"]
        assert not set(completed) & set(calls)
    finally:
        state.close()


def test_history_expiration_restarts_without_mixing_sets(monkeypatch, config, tmp_path):
    client, _ = install_search(monkeypatch, ["10", "11"])
    state = State(tmp_path, "fixture", config)
    state.set("search", {"count": 3, "complete": False, "querykey": "1", "webenv": "old"})
    state.add_ids(["2"])
    events = []
    try:
        client.discover("fixture", state, lambda stage, **kw: events.append(stage))
        assert "discovery_restarted" in events
        assert {r["pmid"] for r in state.records()} == {"10", "11"}
    finally:
        state.close()


def test_incomplete_partition_reconciliation_is_not_success(monkeypatch, config, tmp_path):
    client, _ = install_search(monkeypatch, [str(i) for i in range(1, 11)], limit=3, upper=16)
    original = client.search

    def omit(query, webenv=None, limit=3):
        result = original(query, webenv, limit)
        if query == "#1 AND 9:16[UID]":
            result.update(count=1, idlist=["9"])
        return result

    client.search = omit
    state = State(tmp_path, "fixture", config)
    try:
        with pytest.raises(DiscoveryError, match="original search count"):
            client.discover("fixture", state, lambda *a, **kw: None)
        assert not state.get("search")["complete"]
    finally:
        state.close()
