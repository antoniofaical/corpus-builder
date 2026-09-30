import re
from itertools import islice

from .errors import DiscoveryError, RemoteError
from .state import now

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
SEARCH_LIMIT = 9999
UID_MAX = 2147483647


def batches(iterable, size=100):
    iterator = iter(iterable)
    while batch := list(islice(iterator, size)):
        yield batch


def text(element):
    return "".join(element.itertext()).strip() if element is not None else ""


class PubMed:
    def __init__(self, transport):
        self.http = transport

    def search(self, query, webenv=None, limit=SEARCH_LIMIT):
        data = [
            ("db", "pubmed"),
            ("term", query),
            ("retmode", "json"),
            ("retmax", str(limit)),
            ("usehistory", "y"),
        ]
        if webenv:
            data.append(("WebEnv", webenv))
        obj = self.http.json(EUTILS + "esearch.fcgi", data=data, ncbi=True)
        result = obj.get("esearchresult")
        if not isinstance(result, dict) or "count" not in result or result.get("ERROR"):
            raise DiscoveryError("ESearch returned an invalid result or expired history")
        if result.get("errorlist", {}).get("fieldsnotfound"):
            raise DiscoveryError("PubMed did not recognize a search field")
        try:
            count = int(result["count"])
        except (ValueError, TypeError):
            raise DiscoveryError("Invalid ESearch count") from None
        ids = result.get("idlist", [])
        if (
            count < 0
            or not isinstance(ids, list)
            or any(not isinstance(i, str) or not re.fullmatch(r"[1-9][0-9]*", i) for i in ids)
        ):
            raise DiscoveryError("Invalid ESearch identifiers")
        if len(set(ids)) != len(ids):
            raise DiscoveryError("ESearch returned duplicate identifiers")
        return {**result, "count": count}

    def discover(self, query, state, emit):
        saved = state.get("search")
        if saved and saved.get("complete"):
            if state.count() != saved["count"]:
                raise DiscoveryError("Saved identifiers do not reconcile with search count")
            return
        if saved:
            # Reuse the original server-side set; do not mix fresh and saved results.
            try:
                check = self.search(f"#{saved['querykey']}", saved["webenv"], limit=0)
                valid = check["count"] == saved["count"]
            except DiscoveryError:
                valid = False
            if not valid:
                emit("discovery_restarted", reason="history_expired_or_changed")
                state.reset_discovery()
                saved = None
            elif saved["count"] <= SEARCH_LIMIT:
                # Recover a crash between committing the search descriptor and its IDs.
                result = self.search(f"#{saved['querykey']}", saved["webenv"])
                if result["count"] != saved["count"] or len(result["idlist"]) != saved["count"]:
                    raise DiscoveryError("Saved small search cannot be recovered completely")
                state.add_ids(result["idlist"])
        if not saved:
            root = self.search(query)
            saved = {
                "count": root["count"],
                "querytranslation": root.get("querytranslation"),
                "warnings": root.get("warninglist", {}),
                "query_errors": root.get("errorlist", {}),
                "searched_at": now(),
                "querykey": root.get("querykey"),
                "webenv": root.get("webenv"),
                "complete": False,
            }
            state.set("search", saved)
            emit("discovery_started", expected=root["count"])
            if root["count"] <= SEARCH_LIMIT:
                if len(root["idlist"]) != root["count"]:
                    raise DiscoveryError("ESearch truncated a supposedly complete result")
                state.add_ids(root["idlist"])
            else:
                if not saved["querykey"] or not saved["webenv"]:
                    raise DiscoveryError("Large search requires NCBI history identifiers")
                # Numeric UID partitioning avoids gaps caused by missing/ambiguous dates.
                bounds = self.search(
                    f"#{saved['querykey']} AND 1:{UID_MAX}[UID]", saved["webenv"], limit=0
                )
                if bounds["count"] != saved["count"]:
                    raise DiscoveryError("UID partition range does not cover the original search")
        if saved["count"] > SEARCH_LIMIT:
            # Also recover a crash before the initial partition was created.
            with state.db:
                state.db.execute(
                    "INSERT OR IGNORE INTO partitions(lo,hi) VALUES (?,?)", (1, UID_MAX)
                )
        while partition := state.pending_partition():
            lo, hi = partition
            result = self.search(f"#{saved['querykey']} AND {lo}:{hi}[UID]", saved["webenv"])
            if result["count"] > SEARCH_LIMIT:
                if lo == hi:
                    raise DiscoveryError("Cannot subdivide oversized search partition")
                state.split(lo, hi)
            else:
                ids = result["idlist"]
                if len(ids) != result["count"] or any(not lo <= int(i) <= hi for i in ids):
                    raise DiscoveryError("Partition identifiers do not reconcile")
                state.add_ids(ids, partition)
                emit("discovery_partition_completed", lo=lo, hi=hi, records=len(ids))
        if state.count() != saved["count"]:
            raise DiscoveryError("Enumerated identifiers do not match the original search count")
        saved["complete"] = True
        saved["completed_at"] = now()
        state.set("search", saved)
        emit("discovery_completed", records=state.count())

    def metadata(self, pmids):
        root = self.http.xml(
            EUTILS + "efetch.fcgi",
            ncbi=True,
            data=[
                ("db", "pubmed"),
                ("id", ",".join(pmids)),
                ("retmode", "xml"),
                ("rettype", "abstract"),
            ],
        )
        if root.tag != "PubmedArticleSet":
            raise RemoteError("EFetch did not return PubMed records")
        records = {}
        for entry in root:
            citation = entry.find("MedlineCitation")
            if citation is None:
                citation = entry.find("BookDocument")
            if citation is None:
                continue
            pmid = text(citation.find("PMID"))
            if pmid not in pmids or pmid in records:
                raise RemoteError("EFetch returned unexpected or duplicate PMID")
            article = citation.find("Article")
            if article is None:
                article = citation
            identifiers = {i.get("IdType"): text(i) for i in entry.findall(".//ArticleId")}
            authors = []
            for author in article.findall("AuthorList/Author"):
                collective = text(author.find("CollectiveName"))
                authors.append(
                    collective
                    or " ".join(
                        filter(None, [text(author.find("ForeName")), text(author.find("LastName"))])
                    )
                )
            abstract = [
                {"label": a.get("Label"), "text": text(a)}
                for a in article.findall("Abstract/AbstractText")
            ]
            date = article.find("Journal/JournalIssue/PubDate")
            if date is None:
                date = article.find("Book/PubDate")
            date_parts = {part.tag: text(part) for part in date} if date is not None else {}
            records[pmid] = {
                "title": text(article.find("ArticleTitle")),
                "authors": authors,
                "journal": text(article.find("Journal/Title")),
                "publication_date": date_parts,
                "doi": identifiers.get("doi"),
                "abstract": abstract,
                "publication_types": [text(p) for p in article.findall("PublicationTypeList/*")],
                "source_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "retrieved_at": now(),
            }
        return records

    def links(self, pmids):
        # Repeated id parameters retain the source-to-target mapping; comma joining does not.
        obj = self.http.json(
            EUTILS + "elink.fcgi",
            ncbi=True,
            data=[
                ("dbfrom", "pubmed"),
                ("db", "pmc"),
                ("linkname", "pubmed_pmc"),
                ("retmode", "json"),
                *(("id", p) for p in pmids),
            ],
        )
        if not isinstance(obj.get("linksets"), list):
            raise RemoteError("ELink returned an invalid response")
        records = {}
        for linkset in obj["linksets"]:
            ids = linkset.get("ids", [])
            if len(ids) != 1 or str(ids[0]) not in pmids or linkset.get("error"):
                raise RemoteError("ELink did not preserve the PMID-to-PMCID mapping")
            pmid = str(ids[0])
            if pmid in records:
                raise RemoteError("ELink returned duplicate source records")
            targets = []
            for links in linkset.get("linksetdbs", []):
                if links.get("linkname") == "pubmed_pmc":
                    for value in links.get("links", []):
                        if not re.fullmatch(r"[1-9][0-9]*", str(value)):
                            raise RemoteError("ELink returned invalid PMCID")
                        targets.append("PMC" + str(value))
            records[pmid] = sorted(set(targets))
        return records
