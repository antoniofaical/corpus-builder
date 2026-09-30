"""Conservative bibliographic normalization; original values are never discarded."""

import hashlib
import re
from dataclasses import asdict, dataclass
from urllib.parse import unquote

from .errors import ConfigurationError


def normalize_doi(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    value = unquote(value).strip().lower()
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else None


def publication_year(parts):
    """Issue/book Year, then an unambiguous year in MedlineDate; never guess a range."""
    year = parts.get("Year", "")
    if re.fullmatch(r"\d{4}", year):
        return int(year)
    medline = parts.get("MedlineDate", "")
    if re.search(r"\b\d{4}\s*[-/]\s*\d{2}\b", medline):
        return None
    years = set(re.findall(r"\b(?:18|19|20|21)\d{2}\b", parts.get("MedlineDate", "")))
    return int(next(iter(years))) if len(years) == 1 else None


@dataclass(frozen=True)
class QueryContext:
    query_id: str | None = None
    query_version: str | None = None
    track: str | None = None
    technical_stratum: str | None = None
    database: str = "pubmed"

    def resolve(self, query):
        for name, value in asdict(self).items():
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ConfigurationError(f"{name} must be a nonempty string or None")
        if self.database != "pubmed":
            raise ConfigurationError("Search currently supports database='pubmed' only")
        if bool(self.query_id) != bool(self.query_version):
            raise ConfigurationError("Provide both query_id and query_version, or neither")
        digest = hashlib.sha256((self.database + "\0" + query).encode()).hexdigest()
        return {
            **asdict(self),
            "query_id": self.query_id or "sha256:" + digest,
            "expression_sha256": digest,
            "expression": query,
            "identity_origin": "user" if self.query_id else "derived",
        }
