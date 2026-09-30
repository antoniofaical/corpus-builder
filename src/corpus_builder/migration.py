"""Non-destructive import of existing run databases into a shared catalog."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from .catalog import Catalog
from .errors import ConfigurationError
from .identity import QueryContext
from .resolvers import classify


def import_run(run_dir, corpus_dir, *, context: QueryContext | None = None):
    """Import a v1/v2 run without network calls or changes to its database/reports."""
    directory = Path(run_dir).resolve()
    if directory == Path(corpus_dir).resolve():
        raise ConfigurationError("corpus_dir must differ from run_dir")
    path = directory / "state.sqlite3"
    if not path.is_file():
        raise ConfigurationError("Run database does not exist")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("BEGIN")
        values = {key: json.loads(body) for key, body in db.execute("SELECT key,value FROM kv")}
        records = [json.loads(body) for (body,) in db.execute("SELECT body FROM records")]
        events = [
            json.loads(body) for (body,) in db.execute("SELECT body FROM events ORDER BY seq")
        ]
    except (sqlite3.Error, ValueError):
        raise ConfigurationError("Cannot read run database") from None
    finally:
        db.close()
    run = values.get("run", {})
    if run.get("schema_version") not in (1, 2):
        raise ConfigurationError("Unsupported source run schema")
    query = run["identity"]["query"]
    resolved = (
        context.resolve(query) if context else run.get("context", QueryContext().resolve(query))
    )
    if context and run.get("context") and resolved != run["context"]:
        raise ConfigurationError("Import context conflicts with original run context")
    run = {**run, "context": resolved, "imported_from": str(directory)}
    missing = []
    for record in records:
        classify(record)
    state = SimpleNamespace(
        run=run,
        events=events,
        directory=directory,
        records=lambda: iter(records),
        count=lambda: len(records),
        get=lambda k, d=None: values.get(k, d),
    )
    catalog = Catalog(corpus_dir)
    try:
        catalog.register(run, resolved)
        for record in records:
            for file in record.get("files", []):
                if file.get("status") not in ("downloaded", "reused"):
                    continue
                source = (directory / file["path"]).resolve()
                if not source.is_relative_to(directory):
                    raise ConfigurationError("Imported document path escapes the run directory")
                try:
                    catalog.store_file(file, directory)
                except ConfigurationError:
                    missing.append({"pmid": record["pmid"], "file_id": file["id"]})
        catalog.ingest(state)
        report = catalog.export()
        return {
            "run_id": run["run_id"],
            "records_imported": len(records),
            "unavailable_local_files": missing,
            "catalog_dir": str(catalog.directory),
            "corpus_report": report,
        }
    finally:
        catalog.close()
