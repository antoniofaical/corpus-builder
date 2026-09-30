"""Shared, auditable corpus. Callers hold the catalog lock for all mutations."""

import csv
import hashlib
import json
import shutil
import sqlite3
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import ParseError
from filelock import FileLock, Timeout

from .errors import ConfigurationError, RetryableError
from .identity import normalize_doi, publication_year
from .state import atomic_json, now
from .transport import file_hash, validate_file


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def write_csv(path, rows, fields):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            result = {}
            for key in fields:
                value = row.get(key)
                if isinstance(value, (list, dict)):
                    value = encoded(value)
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                    value = "'" + value
                result[key] = value
            writer.writerow(result)
    temporary.replace(path)


class Catalog:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(self.directory / ".catalog.lock", timeout=0)
        try:
            self.lock.acquire()
        except Timeout:
            raise ConfigurationError("Another process is using this corpus catalog") from None
        try:
            self.db = sqlite3.connect(self.directory / "catalog.sqlite3")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ConfigurationError("Unsupported catalog schema version")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS queries (
                    id TEXT, version TEXT, body TEXT NOT NULL, PRIMARY KEY(id,version));
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, query_id TEXT, query_version TEXT, body TEXT NOT NULL,
                    FOREIGN KEY(query_id,query_version) REFERENCES queries(id,version));
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, article_id TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hits (
                    run_id TEXT REFERENCES runs(id), source_id TEXT REFERENCES sources(id),
                    PRIMARY KEY(run_id,source_id));
                CREATE TABLE IF NOT EXISTS observations (
                    run_id TEXT, source_id TEXT, digest TEXT, observed_at TEXT, body TEXT,
                    PRIMARY KEY(run_id,source_id,digest));
                CREATE TABLE IF NOT EXISTS run_history (
                    run_id TEXT, digest TEXT, body TEXT, PRIMARY KEY(run_id,digest));
                CREATE TABLE IF NOT EXISTS documents (
                    url TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, run_id TEXT, body TEXT NOT NULL);
                PRAGMA user_version=1;
            """)
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.lock.release()
            raise

    def close(self):
        self.db.close()
        self.lock.release()

    def register(self, run, context):
        key = (context["query_id"], context["query_version"] or "")
        previous = self.db.execute(
            "SELECT body FROM queries WHERE id=? AND version=?", key
        ).fetchone()
        if previous and json.loads(previous[0]) != context:
            raise ConfigurationError("Query ID/version already has different expression or context")
        previous_run = self.db.execute(
            "SELECT query_id,query_version FROM runs WHERE id=?", (run["run_id"],)
        ).fetchone()
        if previous_run and tuple(previous_run) != key:
            raise ConfigurationError("Run already belongs to another query/version")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO queries VALUES (?,?,?)", (*key, encoded(context))
            )
            self.db.execute(
                "INSERT OR IGNORE INTO runs VALUES (?,?,?,?)", (run["run_id"], *key, encoded(run))
            )

    def ingest(self, state, status=None):
        """Persist source snapshots and memberships, including records lacking metadata."""
        run = {
            **state.run,
            "search": {
                k: v for k, v in state.get("search", {}).items() if k not in ("webenv", "querykey")
            },
            "output_dir": str(state.directory),
            "recovered_count": state.count(),
        }
        if status:
            run["status"] = status
        body = encoded(run)
        with self.db:
            self.db.execute("UPDATE runs SET body=? WHERE id=?", (body, run["run_id"]))
            self.db.execute(
                "INSERT OR IGNORE INTO run_history VALUES (?,?,?)",
                (run["run_id"], hashlib.sha256(body.encode()).hexdigest(), body),
            )
            if hasattr(state, "db"):
                events = [
                    json.loads(b)
                    for (b,) in state.db.execute("SELECT body FROM events ORDER BY seq")
                ]
            else:
                events = getattr(state, "events", [])
            for event in events:
                self.db.execute(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?)",
                    (event["event_id"], run["run_id"], encoded(event)),
                )
            for record in state.records():
                source_id = "pubmed:" + record["pmid"]
                content = {**record, "source_id": source_id, "database": "pubmed"}
                body = encoded(content)
                self.db.execute(
                    "INSERT OR IGNORE INTO sources VALUES (?,?,?)",
                    (source_id, str(uuid.uuid4()), body),
                )
                # An unsuccessful enrichment must not erase earlier metadata.
                previous = json.loads(
                    self.db.execute("SELECT body FROM sources WHERE id=?", (source_id,)).fetchone()[
                        0
                    ]
                )
                if content.get("metadata") is None:
                    content["metadata"] = previous.get("metadata")
                self.db.execute(
                    "UPDATE sources SET body=? WHERE id=?", (encoded(content), source_id)
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO hits VALUES (?,?)", (run["run_id"], source_id)
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?)",
                    (
                        run["run_id"],
                        source_id,
                        hashlib.sha256(body.encode()).hexdigest(),
                        now(),
                        body,
                    ),
                )

    def sources(self):
        return {i: json.loads(body) for i, body in self.db.execute("SELECT id,body FROM sources")}

    def record_ids(self):
        # Source identity only: no bibliographic matching or merging.
        return dict(self.db.execute("SELECT id,article_id FROM sources"))

    def restore(self, url, path, fmt, md5=None):
        row = self.db.execute("SELECT body FROM documents WHERE url=?", (url,)).fetchone()
        if not row:
            return None
        info = json.loads(row[0])
        blob = self.directory / info["blob_path"]
        try:
            if not blob.is_file() or file_hash(blob) != info["sha256"]:
                return None
            if md5 and file_hash(blob, "md5") != md5:
                return None
            validate_file(blob, fmt)
        except (RetryableError, ValueError, ParseError, DefusedXmlException):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".part")
        shutil.copyfile(blob, temp)
        temp.replace(path)
        return {k: info.get(k) for k in ("sha256", "md5", "bytes", "resolved_url", "downloaded_at")}

    def store_file(self, file, directory):
        if file.get("status") not in ("downloaded", "reused"):
            return
        path = (Path(directory) / file["path"]).resolve()
        if not path.is_relative_to(Path(directory).resolve()):
            raise ConfigurationError("Document path escapes its run directory")
        if not path.is_file() or file_hash(path) != file["sha256"]:
            raise ConfigurationError("Cannot cache missing or changed document")
        try:
            validate_file(path, file["format"])
        except (RetryableError, ValueError, ParseError, DefusedXmlException):
            raise ConfigurationError("Cannot cache an invalid document") from None
        relative = Path("objects") / file["sha256"][:2] / file["sha256"]
        target = self.directory / relative
        if not target.is_file() or file_hash(target) != file["sha256"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            shutil.copyfile(path, tmp)
            tmp.replace(target)
        body = {**file, "blob_path": relative.as_posix()}
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO documents VALUES (?,?)", (file["url"], encoded(body))
            )

    def export(self):
        mapping, sources = self.record_ids(), self.sources()
        groups = defaultdict(list)
        for sid, article_id in mapping.items():
            groups[article_id].append(sid)
        run_queries = {
            rid: (qid, ver, json.loads(body))
            for rid, qid, ver, body in self.db.execute(
                "SELECT id,query_id,query_version,body FROM runs"
            )
        }
        hits = [
            {
                "run_id": rid,
                "source_id": sid,
                "article_id": mapping[sid],
                "query_id": run_queries[rid][0],
                "query_version": run_queries[rid][1] or None,
                "database": sources[sid]["database"],
            }
            for rid, sid in self.db.execute("SELECT run_id,source_id FROM hits ORDER BY 1,2")
        ]
        hits_by_article = defaultdict(list)
        for hit in hits:
            hits_by_article[hit["article_id"]].append(hit)
        articles, documents = [], []
        # Keep every observation available; preferred metadata is merely a reproducible view.
        for article_id, ids in sorted(groups.items()):
            members = [sources[s] for s in sorted(ids)]
            preferred = max(
                members, key=lambda r: sum(bool(v) for v in (r.get("metadata") or {}).values())
            )
            meta = dict(preferred.get("metadata") or {})
            meta.setdefault("year", publication_year(meta.get("publication_date", {})))
            member_hits = hits_by_article[article_id]
            access = [r.get("access_status", "unknown") for r in members]
            retrieval = [r.get("retrieval_status", "pending") for r in members]
            article = {
                **meta,
                "article_id": article_id,
                "source_ids": sorted(ids),
                "dois": sorted(
                    {
                        d
                        for r in members
                        if (d := normalize_doi((r.get("metadata") or {}).get("doi")))
                    }
                ),
                "pmids": sorted({r["pmid"] for r in members if r.get("pmid")}),
                "pmcids": sorted({p for r in members for p in (r.get("pmcids") or [])}),
                "databases": sorted({r["database"] for r in members}),
                "query_ids": sorted({h["query_id"] for h in member_hits}),
                "queries": sorted({(h["query_id"], h["query_version"] or "") for h in member_hits}),
                "preferred_metadata_source_id": preferred["source_id"],
                "access_evidence": preferred.get("access_evidence", []),
                "resolutions": preferred.get("resolutions", {}),
                "files": preferred.get("files", []),
                "errors": preferred.get("errors", []),
                "status_scope": "latest_observation; historical observations are preserved",
                "metadata_variants": [
                    {"source_id": r["source_id"], "metadata": r.get("metadata")} for r in members
                ],
                "access_status": next(
                    (x for x in ("open_access", "closed") if x in access), "unknown"
                ),
                "retrieval_status": next(
                    (x for x in ("downloaded", "download_error", "not_found") if x in retrieval),
                    "pending",
                ),
            }
            articles.append(article)
        for rid, sid, digest, observed_at, body in self.db.execute("SELECT * FROM observations"):
            record = json.loads(body)
            for file in record.get("files", []):
                documents.append(
                    {
                        **file,
                        "article_id": mapping[sid],
                        "source_id": sid,
                        "run_id": rid,
                        "observation_id": digest,
                        "observed_at": observed_at,
                    }
                )
        fields = [
            "article_id",
            "title",
            "authors",
            "authors_structured",
            "year",
            "journal",
            "doi",
            "dois",
            "pmids",
            "pmcids",
            "abstract",
            "keywords",
            "mesh_terms",
            "publication_types",
            "databases",
            "source_ids",
            "queries",
            "query_ids",
            "access_status",
            "retrieval_status",
            "preferred_metadata_source_id",
            "source_url",
            "field_provenance",
            "access_evidence",
            "errors",
        ]
        write_csv(self.directory / "articles.csv", articles, fields)
        write_csv(
            self.directory / "query_hits.csv",
            hits,
            ["article_id", "source_id", "database", "query_id", "query_version", "run_id"],
        )
        queries = [
            json.loads(b)
            for (b,) in self.db.execute("SELECT body FROM queries ORDER BY id,version")
        ]
        write_csv(
            self.directory / "queries.csv",
            queries,
            [
                "query_id",
                "query_version",
                "track",
                "technical_stratum",
                "database",
                "expression",
                "expression_sha256",
                "identity_origin",
            ],
        )
        runs = [r[2] for r in run_queries.values()]
        write_csv(
            self.directory / "runs.csv",
            runs,
            [
                "run_id",
                "created_at",
                "updated_at",
                "status",
                "recovered_count",
                "search",
                "context",
            ],
        )
        for name, rows in (
            ("articles", articles),
            ("documents", documents),
            (
                "observations",
                [
                    {
                        "run_id": r,
                        "source_id": s,
                        "observation_id": d,
                        "observed_at": t,
                        "record": json.loads(b),
                    }
                    for r, s, d, t, b in self.db.execute(
                        "SELECT * FROM observations ORDER BY rowid"
                    )
                ],
            ),
        ):
            temporary = self.directory / (name + ".jsonl.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(encoded(row) + "\n")
            temporary.replace(self.directory / (name + ".jsonl"))
        overlap = Counter()
        for article in articles:
            for n, q in enumerate(article["queries"]):
                for other in article["queries"][n + 1 :]:
                    overlap[encoded([q, other])] += 1
        per_query = []
        for q in queries:
            selected = [
                h
                for h in hits
                if h["query_id"] == q["query_id"] and h["query_version"] == q["query_version"]
            ]
            ids = {h["source_id"] for h in selected}
            per_query.append(
                {
                    "query_id": q["query_id"],
                    "query_version": q["query_version"],
                    "runs": len({h["run_id"] for h in selected}),
                    "query_hits": len(selected),
                    "distinct_source_records": len(ids),
                    "access": dict(
                        Counter(sources[i].get("access_status", "unknown") for i in ids)
                    ),
                    "retrieval": dict(
                        Counter(sources[i].get("retrieval_status", "pending") for i in ids)
                    ),
                }
            )
        for filename, sql in (
            ("events.jsonl", "SELECT body FROM events ORDER BY rowid"),
            ("run_history.jsonl", "SELECT body FROM run_history ORDER BY rowid"),
        ):
            temporary = self.directory / (filename + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                for (body,) in self.db.execute(sql):
                    handle.write(body + "\n")
            temporary.replace(self.directory / filename)
        report = {
            "generated_at": now(),
            "per_query": per_query,
            "runs": len(runs),
            "queries": len(queries),
            "query_hits": len(hits),
            "source_records": len(sources),
            "distinct_source_records": len(articles),
            "bibliographic_deduplication": False,
            "access": dict(Counter(a["access_status"] for a in articles)),
            "retrieval": dict(Counter(a["retrieval_status"] for a in articles)),
            "query_overlap": [
                {"queries": json.loads(k), "articles": v} for k, v in overlap.items()
            ],
        }
        atomic_json(self.directory / "corpus_report.json", report)
        text = [
            "# Corpus report",
            "",
            "No bibliographic deduplication has been applied.",
            "",
            f"- Queries: {len(queries)}",
            f"- Runs: {len(runs)}",
            f"- Query hits: {len(hits)}",
            f"- Distinct source records: {len(articles)}",
            "",
            "## Access (latest observation)",
            "",
            encoded(report["access"]),
            "",
            "## Retrieval (latest observation)",
            "",
            encoded(report["retrieval"]),
            "",
            "Overlap is based exclusively on the same source identifier.",
            "",
        ]
        temporary = self.directory / "corpus_report.md.tmp"
        temporary.write_text("\n".join(text), encoding="utf-8")
        temporary.replace(self.directory / "corpus_report.md")
        return report
