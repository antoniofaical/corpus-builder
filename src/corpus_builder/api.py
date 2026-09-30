import logging
from collections.abc import Callable
from pathlib import Path

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import ParseError
from filelock import FileLock, Timeout

from .catalog import Catalog
from .config import BuildConfig
from .errors import ConfigurationError, CorpusBuilderError, RemoteError, RetryableError
from .identity import QueryContext
from .models import BuildResult
from .pmc import PMC, file_source
from .pubmed import PubMed, batches
from .reporting import write_reports
from .resolvers import classify, collect_additional
from .state import State, atomic_json, now
from .transport import Transport, file_hash, validate_file

logger = logging.getLogger(__name__)


def _enrich(pubmed, state, emit):
    for batch in batches(
        r
        for r in state.records()
        if (r["metadata"] or {}).get("metadata_schema_version") != 2 or r["pmcids"] is None
    ):
        need_metadata = [
            r["pmid"] for r in batch if (r["metadata"] or {}).get("metadata_schema_version") != 2
        ]
        need_links = [r["pmid"] for r in batch if r["pmcids"] is None]
        for key, ids, fetch in (
            ("metadata", need_metadata, pubmed.metadata),
            ("pmcids", need_links, pubmed.links),
        ):
            if not ids:
                continue
            try:
                found = fetch(ids)
            except RemoteError as exc:
                found = {}
                error = str(exc)
            else:
                error = "Remote response omitted this PMID"
            for record in batch:
                if record["pmid"] not in ids:
                    continue
                if record["pmid"] in found:
                    record[key] = found[record["pmid"]]
                else:
                    record["errors"].append({"stage": key, "message": error})
                    record["status"] = "error"
                    emit("record_error", pmid=record["pmid"], error_stage=key, message=error)
                state.save(record)
        emit("metadata_batch_completed", records=len(batch))


def _get_file(version, fmt, old, directory, transport, catalog=None):
    pmcid, number = version["pmcid"], version["version"]
    identifier = f"{pmcid}.{number}.{fmt}"
    file = {
        "id": identifier,
        "pmcid": pmcid,
        "version": number,
        "format": fmt,
        "source": "pmc",
        "license": version.get("license_code"),
        "metadata_url": version.get("metadata_url"),
    }
    source = version.get(fmt + "_url")
    if not source:
        return {**file, "status": "unavailable"}
    url, md5 = file_source(source, pmcid, number)
    relative = Path("articles") / f"{pmcid}.{number}" / identifier
    path = directory / relative
    file.update(url=url, source_md5=md5, path=relative.as_posix())
    if path.is_file():
        try:
            sha = file_hash(path)
            valid = (
                file_hash(path, "md5") == md5
                if md5
                else (old.get("sha256") == sha and old.get("url") == url)
            )
            if valid:
                validate_file(path, fmt)
                return {
                    **file,
                    "status": "reused",
                    "sha256": sha,
                    "md5": file_hash(path, "md5"),
                    "bytes": path.stat().st_size,
                    "verified_at": now(),
                    "downloaded_at": old.get("downloaded_at"),
                }
        except (RetryableError, ParseError, DefusedXmlException):
            pass
    if catalog:
        cached = catalog.restore(url, path, fmt, md5)
        if cached:
            return {**file, **cached, "status": "reused", "verified_at": now()}
    try:
        info = transport.download(url, path, fmt, md5)
    except RemoteError as exc:
        return {**file, "status": "error", "error": str(exc)}
    return {**file, **info, "status": "downloaded", "verified_at": now()}


def _collect(pmc, transport, config, state, emit, catalog=None):
    for record in state.records():
        if record["pmcids"] is None:
            record["status"] = "error"
            state.save(record)
            continue
        if not record["pmcids"]:
            record["status"] = "error" if record["errors"] else "no_pmc"
            state.save(record)
            continue
        # Per-PMCID completion markers distinguish a real empty listing from an unfinished call.
        resolved = record.setdefault("resolved_pmcids", [])
        for pmcid in record["pmcids"]:
            if pmcid in resolved:
                continue
            try:
                versions = pmc.versions(pmcid, record["pmid"])
            except RemoteError as exc:
                record["errors"].append({"stage": "pmc", "pmcid": pmcid, "message": str(exc)})
                emit("record_error", pmid=record["pmid"], error_stage="pmc", message=str(exc))
            else:
                record["versions"].extend(versions)
                resolved.append(pmcid)
            state.save(record)
        eligible = [v for v in record["versions"] if v["is_pmc_openaccess"]]
        for version in eligible:
            for fmt in config.formats:
                identifier = f"{version['pmcid']}.{version['version']}.{fmt}"
                old = next((f for f in record["files"] if f["id"] == identifier), {})
                try:
                    file = _get_file(version, fmt, old, state.directory, transport, catalog)
                except RemoteError as exc:
                    file = {
                        "id": identifier,
                        "pmcid": version["pmcid"],
                        "version": version["version"],
                        "format": fmt,
                        "status": "error",
                        "error": str(exc),
                    }
                record["files"] = [f for f in record["files"] if f["id"] != identifier] + [file]
                if catalog:
                    catalog.store_file(file, state.directory)
                if file["status"] == "error":
                    record["errors"].append(
                        {"stage": "download", "file": identifier, "message": file["error"]}
                    )
                    emit(
                        "record_error",
                        pmid=record["pmid"],
                        error_stage="download",
                        file_id=identifier,
                        message=file["error"],
                    )
                state.save(record)
                emit(
                    "file_processed", pmid=record["pmid"], file_id=identifier, status=file["status"]
                )
        if record["errors"]:
            status = "error"
        elif not record["versions"]:
            status = "not_in_pmc_distribution"
        elif not eligible:
            status = "not_in_oa_subset"
        elif all(f["status"] == "unavailable" for f in record["files"]):
            status = "format_unavailable"
        elif any(f["status"] == "unavailable" for f in record["files"]):
            status = "downloaded_with_unavailable_formats"
        else:
            status = "downloaded"
        record["status"] = status
        state.save(record)
        emit("article_processed", pmid=record["pmid"], status=status)


def build_corpus(
    query: str,
    output_dir: str | Path,
    config: BuildConfig | None = None,
    *,
    on_event: Callable[[dict], None] | None = None,
    context: QueryContext | None = None,
    corpus_dir: str | Path | None = None,
) -> BuildResult:
    """Discover one PubMed query and retrieve texts from configured OA sources.

    A supplied callback receives committed events; callback exceptions do not fail a run.
    Configuration errors raise ConfigurationError. Operational failures return a result
    with status='partial' or 'failed'. KeyboardInterrupt persists reports then propagates.
    """
    if not isinstance(query, str) or not query.strip():
        raise ConfigurationError("query must be a nonempty PubMed search string")
    if context is not None and not isinstance(context, QueryContext):
        raise ConfigurationError("context must be a QueryContext")
    if context:
        context.resolve(query)
    config = config or BuildConfig()
    key = config.resolve_key()
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(directory / ".run.lock", timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise ConfigurationError("Another process is using this output directory") from None
    state = transport = catalog = None
    try:
        if not (directory / "state.sqlite3").exists() and any(
            p.name != ".run.lock" for p in directory.iterdir()
        ):
            raise ConfigurationError(
                "Output directory contains unrelated files; choose an empty one"
            )
        state = State(directory, query, config, context)
        catalog_path = Path(
            corpus_dir or state.run.get("catalog_dir") or directory / "catalog"
        ).resolve()
        if catalog_path == directory:
            raise ConfigurationError("corpus_dir must differ from output_dir")
        catalog = Catalog(catalog_path)
        catalog.register(state.run, state.run["context"])
        state.run["catalog_dir"] = str(catalog_path)
        state.set("run", state.run)
        atomic_json(directory / "run.json", state.run)

        def emit(stage, **details):
            event = state.event(stage, **details)
            logger.info("%s: %s", stage, details)
            if on_event:
                try:
                    on_event(event)
                except Exception:
                    # Never log the exception: a caller can include arbitrary secrets in it.
                    state.event("callback_failed", source_event_id=event["event_id"])

        errors = []
        status = "failed"
        interrupted = False
        emit("run_started")
        try:
            transport = Transport(config, key, emit)
            pubmed = PubMed(transport)
            pubmed.discover(query, state, emit)
            # Errors are per attempt; their history remains in committed events/reports.
            for record in state.records():
                record["errors"] = []
                record["status"] = "pending"
                state.save(record)
            _enrich(pubmed, state, emit)
            catalog.ingest(state)
            emit("downloads_started")
            _collect(PMC(transport), transport, config, state, emit, catalog)
            collect_additional(transport, config, state, emit, catalog)
            failed = sum(r["status"] == "error" for r in state.records())
            status = "partial" if failed else "completed"
            if failed:
                errors.append(f"{failed} record(s) have unresolved technical errors; see manifest")
        except ConfigurationError as exc:
            errors.append(str(exc))
            status = "failed"
            raise
        except CorpusBuilderError as exc:
            errors.append(str(exc))
            status = "failed" if not state.get("search", {}).get("complete") else "partial"
        except KeyboardInterrupt:
            status = "interrupted"
            interrupted = True
        except Exception:
            # Persist a truthful failed report while keeping programming errors visible to callers.
            errors.append("Unexpected internal or filesystem error; execution did not complete")
            status = "failed"
            raise
        finally:
            emit("run_finished", status=status, errors=errors)
            for record in state.records():
                classify(record)
                state.save(record)
            catalog.ingest(state, status)
            catalog.export()
            result = write_reports(state, status, errors)
        if interrupted:
            raise KeyboardInterrupt
        return result
    finally:
        if transport:
            transport.close()
        if state:
            state.close()
        if catalog:
            catalog.close()
        lock.release()
