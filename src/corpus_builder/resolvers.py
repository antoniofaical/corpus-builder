"""Additional legitimate OA locations. Discovery remains exclusively PubMed."""

import hashlib
import html
import re
from pathlib import Path
from urllib.parse import quote

from .errors import RemoteError
from .identity import normalize_doi
from .state import now
from .transport import file_hash, validate_file, validate_public_url

EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UNPAYWALL = "https://api.unpaywall.org/v2/"


def _location(provider, url, fmt, **extra):
    validate_public_url(url)
    return {"provider": provider, "url": url, "format": fmt, "observed_at": now(), **extra}


def unpaywall(record, transport, email):
    doi = normalize_doi((record.get("metadata") or {}).get("doi"))
    if not doi:
        return {
            "provider": "unpaywall",
            "status": "not_applicable",
            "locations": [],
            "observed_at": now(),
            "reason": "no_valid_doi",
        }
    try:
        obj = transport.json(
            UNPAYWALL + quote(doi, safe=""), params={"email": email}, resolver=True
        )
    except RemoteError as exc:
        if str(exc) != "Remote HTTP 404":
            raise
        return {
            "provider": "unpaywall",
            "status": "not_found",
            "locations": [],
            "observed_at": now(),
        }
    if normalize_doi(obj.get("doi")) != doi or type(obj.get("is_oa")) is not bool:
        raise RemoteError("Unpaywall returned mismatched identity or invalid access status")
    locations, rejected = [], []
    for item in obj.get("oa_locations") or []:
        if not isinstance(item, dict):
            raise RemoteError("Unpaywall returned an invalid location")
        for fmt, key in (("pdf", "url_for_pdf"), ("html", "url_for_landing_page")):
            if not item.get(key):
                continue
            try:
                location = _location(
                    "unpaywall",
                    item[key],
                    fmt,
                    doi=doi,
                    license=item.get("license"),
                    version=item.get("version"),
                    host_type=item.get("host_type"),
                    evidence=item.get("evidence"),
                )
            except RemoteError:
                rejected.append({"format": fmt, "reason": "unsupported_or_unsafe_url"})
            else:
                locations.append(location)
    return {
        "provider": "unpaywall",
        "status": "resolved",
        "observed_at": now(),
        "access_status": "open_access" if obj["is_oa"] else "closed",
        "locations": locations if obj["is_oa"] else [],
        "rejected_locations": rejected,
        "metadata": obj,
        "source_url": UNPAYWALL + quote(doi, safe=""),
    }


def europe_pmc(record, transport):
    obj = transport.json(
        EUROPE_PMC + "/search",
        resolver=True,
        params={
            "query": f"EXT_ID:{record['pmid']} AND SRC:MED",
            "format": "json",
            "resultType": "core",
            "pageSize": 2,
        },
    )
    results = obj.get("resultList", {}).get("result")
    if not isinstance(results, list) or "hitCount" not in obj:
        raise RemoteError("Europe PMC returned an invalid search response")
    if not results and int(obj["hitCount"]) == 0:
        return {
            "provider": "europe_pmc",
            "status": "not_found",
            "locations": [],
            "observed_at": now(),
        }
    if (
        len(results) != 1
        or str(results[0].get("id")) != record["pmid"]
        or results[0].get("source") != "MED"
    ):
        raise RemoteError("Europe PMC returned mismatched identity")
    result = results[0]
    locations = []
    pmcid = result.get("pmcid")
    if pmcid and result.get("isOpenAccess") == "Y":
        if not re.fullmatch(r"PMC[1-9][0-9]*", pmcid):
            raise RemoteError("Europe PMC returned an invalid PMCID")
        locations.append(
            _location(
                "europe_pmc",
                f"{EUROPE_PMC}/{pmcid}/fullTextXML",
                "xml",
                pmcid=pmcid,
                license=result.get("license"),
                version="unspecified",
            )
        )
    # Only links explicitly classified OA by the provider are eligible.
    for item in result.get("fullTextUrlList", {}).get("fullTextUrl", []):
        fmt = (item.get("documentStyle") or "").lower()
        if item.get("availabilityCode") != "OA" or fmt not in ("pdf", "html"):
            continue
        try:
            locations.append(
                _location(
                    "europe_pmc",
                    item["url"],
                    fmt,
                    license=result.get("license"),
                    version="unspecified",
                )
            )
        except (KeyError, RemoteError):
            continue
    return {
        "provider": "europe_pmc",
        "status": "resolved",
        "observed_at": now(),
        "access_status": "open_access" if result.get("isOpenAccess") == "Y" else "unknown",
        "locations": locations,
        "metadata": result,
        "source_url": f"https://europepmc.org/article/MED/{record['pmid']}",
    }


def supplement_metadata(record, resolution):
    """Fill missing fields only; retain raw provider responses and field attribution."""
    obj = resolution.get("metadata") or {}
    if not obj:
        return
    provider = resolution["provider"]
    if provider == "europe_pmc":
        authors = [
            a.get("fullName")
            for a in obj.get("authorList", {}).get("author", [])
            if a.get("fullName")
        ]
        abstract = html.unescape(re.sub(r"<[^>]*>", "", obj.get("abstractText") or ""))
        values = {
            "doi": obj.get("doi"),
            "title": obj.get("title"),
            "authors": authors,
            "journal": obj.get("journalInfo", {}).get("journal", {}).get("title"),
            "year": int(obj["pubYear"])
            if re.fullmatch(r"\d{4}", str(obj.get("pubYear", "")))
            else None,
            "abstract": [{"label": None, "text": abstract}] if abstract else [],
            "keywords": obj.get("keywordList", {}).get("keyword", []),
            "publication_types": obj.get("pubTypeList", {}).get("pubType", []),
        }
    else:
        values = {
            "doi": obj.get("doi"),
            "title": obj.get("title"),
            "year": obj.get("year"),
            "journal": obj.get("journal_name"),
        }
    metadata = record.get("metadata") or {}
    provenance = metadata.setdefault("field_provenance", {})
    for key, value in values.items():
        if not metadata.get(key) and value:
            metadata[key] = value
            provenance[key] = {
                "source": provider,
                "source_url": resolution.get("source_url"),
                "retrieved_at": resolution["observed_at"],
            }
    metadata["doi_normalized"] = normalize_doi(metadata.get("doi"))
    record["metadata"] = metadata


def get_external_file(location, record, state, transport, catalog):
    fmt, url = location["format"], location["url"]
    identifier = hashlib.sha256((url + "\0" + fmt).encode()).hexdigest()
    relative = Path("articles") / "external" / (identifier + "." + fmt)
    path = state.directory / relative
    file = {
        **location,
        "id": identifier,
        "path": relative.as_posix(),
        "source": location["provider"],
    }
    old = next((f for f in record["files"] if f["id"] == identifier), {})
    if path.is_file() and old.get("sha256") == file_hash(path):
        try:
            validate_file(path, fmt)
        except (RemoteError, ValueError):
            pass
        else:
            return {**old, **file, "status": "reused", "verified_at": now()}
    cached = catalog.restore(url, path, fmt)
    if cached:
        return {**file, **cached, "status": "reused", "verified_at": now()}
    try:
        info = transport.download(url, path, fmt, None, external=True)
    except RemoteError as exc:
        return {**file, "status": "error", "error": str(exc)}
    return {**file, **info, "status": "downloaded", "verified_at": now()}


def classify(record):
    evidence = list(record.get("resolutions", {}).values())
    for version in record.get("versions", []):
        evidence.append(
            {
                "provider": "pmc",
                "observed_at": version.get("retrieved_at"),
                "source_url": version.get("metadata_url"),
                "access_status": "open_access" if version.get("is_pmc_openaccess") else "unknown",
                "license": version.get("license_code"),
                "version": version.get("version"),
            }
        )
    record["access_evidence"] = [
        {k: v for k, v in e.items() if k not in ("metadata", "locations")} for e in evidence
    ]
    statuses = {e.get("access_status") for e in evidence}
    record["access_status"] = next(
        (s for s in ("open_access", "closed") if s in statuses), "unknown"
    )
    files = record.get("files", [])
    record["retrieval_status"] = (
        "downloaded"
        if any(f["status"] in ("downloaded", "reused") for f in files)
        else "download_error"
        if any(f["status"] == "error" for f in files)
        else "pending"
        if record.get("errors") or record.get("status") == "pending"
        else "not_found"
    )
    record["retrieval_has_errors"] = any(f["status"] == "error" for f in files)


def collect_additional(transport, config, state, emit, catalog):
    for record in state.records():
        resolutions = record.setdefault("resolutions", {})
        providers = []
        if config.europe_pmc:
            providers.append(("europe_pmc", lambda record=record: europe_pmc(record, transport)))
        if config.unpaywall:
            providers.append(
                (
                    "unpaywall",
                    lambda record=record: unpaywall(record, transport, config.unpaywall_email),
                )
            )
        for name, resolve in providers:
            fingerprint = (
                normalize_doi((record.get("metadata") or {}).get("doi"))
                if name == "unpaywall"
                else record["pmid"]
            )
            if name in resolutions and resolutions[name].get("input_id") == fingerprint:
                supplement_metadata(record, resolutions[name])
                continue
            try:
                try:
                    resolution = {**resolve(), "input_id": fingerprint}
                    supplement_metadata(record, resolution)
                except (ValueError, TypeError, KeyError, AttributeError):
                    raise RemoteError("Malformed provider response") from None
                resolutions[name] = resolution
                emit(
                    "access_resolved",
                    pmid=record["pmid"],
                    provider=name,
                    access_status=resolutions[name].get("access_status", "unknown"),
                )
            except RemoteError as exc:
                record["errors"].append(
                    {"stage": "resolver", "provider": name, "message": str(exc)}
                )
                emit(
                    "record_error",
                    pmid=record["pmid"],
                    error_stage="resolver",
                    provider=name,
                    message=str(exc),
                )
            state.save(record)
        locations = {}
        for resolution in resolutions.values():
            for location in resolution.get("locations", []):
                if location["format"] in config.formats:
                    locations[(location["url"], location["format"])] = location
        for location in locations.values():
            file = get_external_file(location, record, state, transport, catalog)
            record["files"] = [f for f in record["files"] if f["id"] != file["id"]] + [file]
            if file["status"] == "error":
                record["errors"].append(
                    {"stage": "download", "file": file["id"], "message": file["error"]}
                )
            else:
                catalog.store_file(file, state.directory)
            state.save(record)
            emit("file_processed", pmid=record["pmid"], file_id=file["id"], status=file["status"])
        if record["errors"]:
            record["status"] = "error"
        elif any(f["status"] in ("downloaded", "reused") for f in record["files"]):
            # Preserve existing PMC-specific partial-format diagnostics.
            if record["status"] != "downloaded_with_unavailable_formats":
                record["status"] = "downloaded"
        classify(record)
        state.save(record)
