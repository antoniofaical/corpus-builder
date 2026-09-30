import csv
import json
from collections import Counter

from .models import BuildResult
from .state import atomic_json, now


def write_reports(state, status, errors) -> BuildResult:
    directory = state.directory
    counts = Counter(
        records=state.count(),
        files_downloaded=0,
        files_reused=0,
        files_failed=0,
        formats_unavailable=0,
        articles_with_files=0,
        versions_oa=0,
        versions_seen=0,
    )
    seen_files = set()
    seen_versions = set()
    manifest_tmp = directory / "manifest.jsonl.tmp"
    csv_tmp = directory / "manifest.csv.tmp"
    fields = ["pmid", "title", "doi", "pmcids", "status", "file_count", "errors"]
    with (
        manifest_tmp.open("w", encoding="utf-8") as jsonl,
        csv_tmp.open("w", encoding="utf-8-sig", newline="") as tabular,
    ):
        writer = csv.DictWriter(tabular, fieldnames=fields)
        writer.writeheader()
        for record in state.records():
            jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
            counts["records_" + record["status"]] += 1
            file_count = 0
            for version in record["versions"]:
                identifier = (version["pmcid"], version["version"])
                if identifier not in seen_versions:
                    seen_versions.add(identifier)
                    counts["versions_seen"] += 1
                    counts["versions_oa"] += int(version["is_pmc_openaccess"])
            for file in record["files"]:
                if file["status"] in ("downloaded", "reused"):
                    file_count += 1
                if file["id"] in seen_files:
                    continue
                seen_files.add(file["id"])
                if file["status"] == "unavailable":
                    counts["formats_unavailable"] += 1
                elif file["status"] == "error":
                    counts["files_failed"] += 1
                else:
                    counts["files_" + file["status"]] += 1
            counts["articles_with_files"] += int(file_count > 0)
            metadata = record.get("metadata") or {}
            row = {
                "pmid": record["pmid"],
                "title": metadata.get("title", ""),
                "doi": metadata.get("doi") or "",
                "pmcids": ";".join(record["pmcids"] or []),
                "status": record["status"],
                "file_count": file_count,
                "errors": json.dumps(record["errors"], ensure_ascii=False),
            }
            # Avoid spreadsheet formula execution in titles or other provider text.
            writer.writerow(
                {
                    k: "'" + v
                    if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r"))
                    else v
                    for k, v in row.items()
                }
            )
    manifest_tmp.replace(directory / "manifest.jsonl")
    csv_tmp.replace(directory / "manifest.csv")
    events_tmp = directory / "events.jsonl.tmp"
    with events_tmp.open("w", encoding="utf-8") as handle:
        for (body,) in state.db.execute("SELECT body FROM events ORDER BY seq"):
            handle.write(body + "\n")
    events_tmp.replace(directory / "events.jsonl")
    search = state.get("search", {})
    complete = bool(search.get("complete")) and counts["records"] == search.get("count")
    counts["expected_records"] = search.get("count", 0)
    counts["files_verified"] = counts["files_downloaded"] + counts["files_reused"]
    result = BuildResult(
        state.run["run_id"],
        status,
        complete,
        dict(counts),
        directory,
        directory / "report.json",
        directory / "manifest.jsonl",
        tuple(errors),
    )
    report = {
        **result.to_dict(),
        "generated_at": now(),
        "query": state.run["identity"]["query"],
        "search": search,
        "scope": "PubMed query; PMC Open Access Subset; requested formats when available",
        "source": "NIH NLM NCBI PubMed Central Article Datasets",
        "source_url": "https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/",
    }
    # History handles stay in SQLite; they are operational, not bibliographic provenance.
    report["search"] = {k: v for k, v in search.items() if k not in ("webenv", "querykey")}
    atomic_json(directory / "report.json", report)
    lines = [
        "# Corpus Builder",
        "",
        f"- Run: `{result.run_id}`",
        f"- Status: **{status}**",
        f"- Discovery complete: **{complete}**",
        "",
        "## Counts",
        "",
        "| Metric | Count |",
        "|---|---:|",
    ]
    lines.extend(f"| {key} | {value} |" for key, value in sorted(counts.items()))
    lines.extend(
        [
            "",
            "## Search",
            "",
            "```text",
            report["query"],
            "```",
            "",
            "Unavailable text or formats are not scientific exclusion decisions.",
            "Technical errors and incomplete discovery remain explicitly recorded.",
            "'completed' means all available requested files were verified; "
            "it does not mean every PubMed record has downloadable full text.",
            "",
            "## Errors",
            "",
        ]
    )
    lines.extend(f"- {error}" for error in errors)
    if not errors:
        lines.append("None at run level; see manifest for per-record outcomes.")
    lines.extend(
        [
            "",
            "Source: NIH NLM NCBI PubMed Central Article Datasets.",
            "https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/",
            "",
        ]
    )
    tmp = directory / "report.md.tmp"
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(directory / "report.md")
    return result
