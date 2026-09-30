"""Sequential orchestration of the single-query core; no bibliographic decisions."""

import json
import logging
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from filelock import FileLock, Timeout

from .api import build_corpus
from .config import BuildConfig
from .errors import AuthenticationError, ConfigurationError, CorpusBuilderError
from .identity import QueryContext
from .state import atomic_json, now
from .transport import RateLimiter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuerySpec:
    query: str
    context: QueryContext = field(default_factory=QueryContext)

    def normalized(self):
        if not isinstance(self.query, str) or not self.query.strip():
            raise ConfigurationError("Each query must be a nonempty string")
        if not isinstance(self.context, QueryContext):
            raise ConfigurationError("Query context must be a QueryContext")
        return {"query": self.query, "context": self.context.resolve(self.query)}


@dataclass(frozen=True)
class BatchResult:
    batch_id: str
    status: str
    counts: dict[str, int]
    output_dir: Path
    corpus_dir: Path
    report_path: Path
    jobs: tuple[dict, ...]

    def to_dict(self):
        return {
            "batch_id": self.batch_id,
            "status": self.status,
            "counts": self.counts,
            "output_dir": str(self.output_dir),
            "corpus_dir": str(self.corpus_dir),
            "report_path": str(self.report_path),
            "jobs": list(self.jobs),
        }


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"Repeated JSON field: {key}")
        result[key] = value
    return result


def load_queries(path: str | Path) -> list[QuerySpec]:
    """Load the strict, versioned JSON input. Strings and contextual objects may coexist."""
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_keys)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigurationError("Cannot read queries JSON") from None
    if (
        not isinstance(obj, dict)
        or set(obj) != {"schema_version", "queries"}
        or type(obj["schema_version"]) is not int
        or obj["schema_version"] != 1
        or not isinstance(obj["queries"], list)
    ):
        raise ConfigurationError("Expected schema_version=1 and a queries array")
    allowed = {"query", "query_id", "query_version", "track", "technical_stratum", "database"}
    queries = []
    for index, item in enumerate(obj["queries"], 1):
        if isinstance(item, str):
            spec = QuerySpec(item)
        elif isinstance(item, dict) and "query" in item and not set(item) - allowed:
            spec = QuerySpec(
                item["query"], QueryContext(**{k: v for k, v in item.items() if k != "query"})
            )
        else:
            raise ConfigurationError(f"Invalid query entry at position {index}")
        spec.normalized()
        queries.append(spec)
    _normalize_queries(queries)
    return queries


def _normalize_queries(queries):
    if not isinstance(queries, Sequence) or isinstance(queries, (str, bytes)):
        raise ConfigurationError("queries must be a list/sequence of strings or QuerySpec objects")
    specs, normalized, identities = [], [], {}
    for index, item in enumerate(queries, 1):
        spec = QuerySpec(item) if isinstance(item, str) else item
        if not isinstance(spec, QuerySpec):
            raise ConfigurationError(f"Invalid query entry at position {index}")
        value = spec.normalized()
        ctx = value["context"]
        key = (ctx["query_id"], ctx["query_version"])
        if key in identities and identities[key] != ctx:
            raise ConfigurationError(f"Conflicting query ID/version at position {index}")
        identities[key] = ctx
        specs.append(spec)
        normalized.append(value)
    return specs, normalized


def run_batch(
    queries: Sequence[str | QuerySpec],
    output_dir: str | Path,
    config: BuildConfig | None = None,
    *,
    corpus_dir: str | Path | None = None,
    on_event: Callable[[dict], None] | None = None,
    recheck_completed: bool = False,
) -> BatchResult:
    """Run the core once per entry, in order. Resume skips successful entries by default.

    Failed/partial/interrupted entries are retried once per invocation; the core owns
    HTTP retries. Authentication, filesystem errors and unexpected exceptions halt
    the batch. Ctrl+C saves checkpoints and propagates KeyboardInterrupt.
    """
    specs, normalized = _normalize_queries(queries)
    if type(recheck_completed) is not bool:
        raise ConfigurationError("recheck_completed must be boolean")
    config = config or BuildConfig()
    config.validate()
    directory = Path(output_dir).resolve()
    # An existing batch remembers its corpus location even when omitted on resume.
    state_path = directory / "batch_state.json"
    directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(directory / ".batch.lock", timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise ConfigurationError("Another process is using this batch directory") from None
    try:
        previous = None
        if state_path.exists():
            try:
                previous = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise ConfigurationError("Cannot read batch checkpoint") from None
            if (
                not isinstance(previous, dict)
                or not {"identity", "jobs", "batch_id", "corpus_dir"} <= set(previous)
                or not isinstance(previous["jobs"], list)
            ):
                raise ConfigurationError("Invalid batch checkpoint structure")
            if not config.resume:
                raise ConfigurationError("Batch exists; enable resume or use a new directory")
        elif any(p.name != ".batch.lock" for p in directory.iterdir()):
            raise ConfigurationError("Batch output directory contains unrelated files")
        catalog = Path(
            corpus_dir or (previous or {}).get("corpus_dir") or directory / "catalog"
        ).resolve()
        if catalog == directory or catalog.is_relative_to(directory / "runs"):
            raise ConfigurationError(
                "corpus_dir must not be the batch directory or a query run directory"
            )
        identity = {
            "schema_version": 1,
            "queries": normalized,
            "formats": sorted(config.formats),
            "sources": ["pmc"]
            + (["europe_pmc"] if config.europe_pmc else [])
            + (["unpaywall"] if config.unpaywall else []),
            "corpus_dir": str(catalog),
        }
        if previous and previous.get("identity") != identity:
            raise ConfigurationError(
                "Existing batch has different queries, order, formats, sources or corpus"
            )
        if previous:
            if len(previous["jobs"]) != len(normalized):
                raise ConfigurationError("Batch checkpoint has an invalid job count")
            for index, (job, value) in enumerate(zip(previous["jobs"], normalized, strict=True), 1):
                if (
                    not isinstance(job, dict)
                    or job.get("index") != index
                    or job.get("entry_id") != f"q{index:06d}"
                    or job.get("run_dir") != f"runs/{index:06d}"
                    or job.get("query") != value["query"]
                    or job.get("context") != value["context"]
                    or job.get("status")
                    not in ("pending", "running", "completed", "partial", "failed", "interrupted")
                    or not isinstance(job.get("attempts"), list)
                    or any(not isinstance(a, dict) for a in job["attempts"])
                ):
                    raise ConfigurationError("Invalid job in batch checkpoint")
        state = previous or {
            "batch_id": str(uuid.uuid4()),
            "identity": identity,
            "created_at": now(),
            "corpus_dir": str(catalog),
            "jobs": [
                {
                    "entry_id": f"q{index:06d}",
                    "index": index,
                    **value,
                    "run_dir": f"runs/{index:06d}",
                    "status": "pending",
                    "attempts": [],
                }
                for index, value in enumerate(normalized, 1)
            ],
        }
        state["package_version"] = "0.3.0"
        state["config"] = config.public_dict()
        jobs = state["jobs"]
        # No credentials are needed when no remote-capable work remains.
        if any(j["status"] != "completed" or recheck_completed for j in jobs):
            config.resolve_key()

        def save(status):
            state.update(status=status, updated_at=now())
            atomic_json(state_path, state)

        def emit(stage, **details):
            event = {
                "event_id": str(uuid.uuid4()),
                "batch_id": state["batch_id"],
                "timestamp": now(),
                "stage": stage,
                **details,
            }
            with (directory / "batch_events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            if on_event:
                try:
                    on_event(event)
                except Exception:
                    with (directory / "batch_events.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "event_id": str(uuid.uuid4()),
                                    "batch_id": state["batch_id"],
                                    "timestamp": now(),
                                    "stage": "callback_failed",
                                    "source_event_id": event["event_id"],
                                }
                            )
                            + "\n"
                        )

        rates = [config.requests_per_second, config.download_requests_per_second]
        if config.europe_pmc or config.unpaywall:
            rates.append(config.resolver_requests_per_second)
        boundary = RateLimiter(min(rates))
        attempted, skipped = 0, 0
        status = "failed"
        fatal_error = None
        save("running")
        try:
            emit("batch_started", total=len(jobs))
            for spec, job in zip(specs, jobs, strict=True):
                if job["status"] == "completed" and not recheck_completed:
                    skipped += 1
                    emit(
                        "query_skipped",
                        entry_id=job["entry_id"],
                        index=job["index"],
                        reason="already_completed",
                    )
                    continue
                if job["status"] == "running" and job["attempts"]:
                    job["attempts"][-1].update(
                        status="interrupted",
                        finished_at=now(),
                        error="Previous process ended without a checkpoint",
                    )
                job.pop("result", None)
                attempt = {"started_at": now(), "status": "running"}
                job["attempts"].append(attempt)
                job["status"] = "running"
                attempted += 1
                save("running")
                logger.info("[%s/%s] %s", job["index"], len(jobs), job["context"]["query_id"])
                emit(
                    "query_started",
                    entry_id=job["entry_id"],
                    index=job["index"],
                    query_id=job["context"]["query_id"],
                )
                try:
                    boundary.wait()
                    result = build_corpus(
                        spec.query,
                        directory / job["run_dir"],
                        config,
                        context=spec.context,
                        corpus_dir=catalog,
                        on_event=lambda e, job=job: emit(
                            "core_event", entry_id=job["entry_id"], index=job["index"], event=e
                        ),
                    )
                    if result.status == "completed" and not result.discovery_complete:
                        raise CorpusBuilderError(
                            "Core reported completion without complete discovery"
                        )
                    job["status"] = result.status
                    attempt.update(status=result.status, result=result.to_dict())
                    job["result"] = result.to_dict()
                except AuthenticationError:
                    job["status"] = "failed"
                    fatal_error = "Authentication rejected; remaining queries were not started"
                    attempt.update(status="failed", error=fatal_error)
                except CorpusBuilderError as exc:
                    job["status"] = "failed"
                    attempt.update(status="failed", error=str(exc))
                except KeyboardInterrupt:
                    job["status"] = "interrupted"
                    attempt.update(status="interrupted", error="Interrupted by user")
                    raise
                except Exception:
                    job["status"] = "failed"
                    attempt.update(status="failed", error="Unexpected internal or filesystem error")
                    raise
                finally:
                    # Each core call creates fresh HTTP limiters. Pace the boundary too,
                    # so sequential queries cannot reset a provider's rate allowance.
                    boundary.next_start = time.monotonic() + boundary.interval
                    attempt["finished_at"] = now()
                    save("running")
                    emit(
                        "query_finished",
                        entry_id=job["entry_id"],
                        index=job["index"],
                        status=job["status"],
                    )
                if fatal_error:
                    break
            if fatal_error:
                status = "failed"
            elif all(j["status"] == "completed" for j in jobs):
                status = "completed"
            elif any(j["status"] in ("completed", "partial") for j in jobs):
                status = "partial"
            else:
                status = "failed"
        except KeyboardInterrupt:
            status = "interrupted"
            raise
        finally:
            for job in jobs:
                if job["status"] == "running":
                    job["status"] = "interrupted" if status == "interrupted" else "failed"
                    job["attempts"][-1].update(
                        status=job["status"],
                        finished_at=now(),
                        error="Execution stopped before the core returned",
                    )
            counts = {
                name: 0
                for name in ("pending", "running", "completed", "partial", "failed", "interrupted")
            }
            counts.update(Counter(j["status"] for j in jobs))
            counts.update(
                total=len(jobs), attempted_this_invocation=attempted, skipped_completed=skipped
            )
            state["counts"] = counts
            state["fatal_error"] = fatal_error
            save(status)
            result = BatchResult(
                state["batch_id"],
                status,
                counts,
                directory,
                catalog,
                directory / "batch_report.json",
                tuple(jobs),
            )
            atomic_json(
                result.report_path,
                {**result.to_dict(), "generated_at": now(), "fatal_error": fatal_error},
            )
            lines = [
                "# Corpus batch",
                "",
                f"- Batch: `{result.batch_id}`",
                f"- Status: **{status}**",
                f"- Entries: {len(jobs)}",
                "",
                "| Entry | Status | Attempts | Run |",
                "|---|---|---:|---|",
            ]
            lines.extend(
                f"| {j['entry_id']} | {j['status']} | {len(j['attempts'])} | {j['run_dir']} |"
                for j in jobs
            )
            temporary = directory / "batch_report.md.tmp"
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            temporary.replace(directory / "batch_report.md")
            emit("batch_finished", status=status, counts=counts)
        return result
    finally:
        lock.release()
