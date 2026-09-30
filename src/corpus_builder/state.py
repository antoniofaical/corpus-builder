import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .errors import ConfigurationError

SCHEMA_VERSION = 1


def now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class State:
    def __init__(self, directory: Path, query: str, config):
        self.directory = directory
        self.db = sqlite3.connect(directory / "state.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records (pmid TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS partitions (
                lo INTEGER, hi INTEGER, done INTEGER DEFAULT 0, PRIMARY KEY(lo, hi));
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL);
        """)
        previous = self.get("run")
        identity = {"query": query, "formats": sorted(config.formats), "oa_only": True}
        if previous:
            if not config.resume:
                self.db.close()
                raise ConfigurationError(
                    "Run exists; enable resume or choose a new output directory"
                )
            if previous["schema_version"] != SCHEMA_VERSION or previous["identity"] != identity:
                self.db.close()
                raise ConfigurationError("Existing run is incompatible with this query or formats")
            self.run = previous
        else:
            self.run = {
                "run_id": str(uuid.uuid4()),
                "schema_version": SCHEMA_VERSION,
                "package_version": "0.1.0",
                "identity": identity,
                "created_at": now(),
            }
        self.run["config"] = config.public_dict()
        self.run["updated_at"] = now()
        self.set("run", self.run)
        atomic_json(directory / "run.json", self.run)

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))

    def event(self, stage, **details):
        event = {
            "event_id": str(uuid.uuid4()),
            "run_id": self.run["run_id"],
            "timestamp": now(),
            "stage": stage,
            **details,
        }
        with self.db:
            self.db.execute("INSERT INTO events(body) VALUES (?)", (json.dumps(event),))
        return event

    def add_ids(self, ids, partition=None):
        with self.db:
            for pmid in ids:
                body = {
                    "pmid": pmid,
                    "status": "pending",
                    "metadata": None,
                    "pmcids": None,
                    "versions": [],
                    "files": [],
                    "errors": [],
                }
                self.db.execute(
                    "INSERT OR IGNORE INTO records VALUES (?, ?)", (pmid, json.dumps(body))
                )
            if partition:
                self.db.execute("UPDATE partitions SET done=1 WHERE lo=? AND hi=?", partition)

    def save(self, record):
        with self.db:
            self.db.execute(
                "UPDATE records SET body=? WHERE pmid=?", (json.dumps(record), record["pmid"])
            )

    def records(self):
        cursor = self.db.execute("SELECT body FROM records ORDER BY CAST(pmid AS INTEGER)")
        for (body,) in cursor:
            yield json.loads(body)

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    def reset_discovery(self):
        with self.db:
            self.db.execute("DELETE FROM partitions")
            self.db.execute("DELETE FROM records")
            self.db.execute("DELETE FROM kv WHERE key='search'")

    def pending_partition(self):
        return self.db.execute(
            "SELECT lo,hi FROM partitions WHERE done=0 ORDER BY lo LIMIT 1"
        ).fetchone()

    def split(self, lo, hi):
        mid = (lo + hi) // 2
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO partitions(lo,hi) VALUES (?,?)", (lo, mid))
            self.db.execute("INSERT OR IGNORE INTO partitions(lo,hi) VALUES (?,?)", (mid + 1, hi))
            self.db.execute("UPDATE partitions SET done=1 WHERE lo=? AND hi=?", (lo, hi))
