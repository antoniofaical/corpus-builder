import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .errors import RemoteError
from .state import now

BUCKET = "pmc-oa-opendata"
HOST = BUCKET + ".s3.amazonaws.com"
BASE = "https://" + HOST
NS = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}


def file_source(value: str, pmcid: str, version: int) -> tuple[str, str | None]:
    if not isinstance(value, str):
        raise RemoteError("Invalid PMC file URL")
    parsed = urlsplit(value)
    valid = (parsed.scheme == "s3" and parsed.netloc == BUCKET) or (
        parsed.scheme == "https" and parsed.netloc == HOST
    )
    prefix = f"/{pmcid}.{version}/"
    if not valid or not parsed.path.startswith(prefix) or "/../" in parsed.path:
        raise RemoteError("PMC file URL points outside the expected article version")
    checksum = parse_qs(parsed.query).get("md5", [None])[0]
    if checksum is not None and not re.fullmatch(r"[a-fA-F0-9]{32}", checksum):
        raise RemoteError("Invalid PMC MD5 checksum")
    return urlunsplit(("https", HOST, parsed.path, "", "")), checksum


class PMC:
    def __init__(self, transport):
        self.http = transport

    def versions(self, pmcid: str, pmid: str) -> list[dict]:
        if not re.fullmatch(r"PMC[1-9][0-9]*", pmcid):
            raise RemoteError("Invalid PMCID")
        params = {"list-type": "2", "prefix": pmcid + ".", "delimiter": "/"}
        prefixes = set()
        seen_tokens = set()
        while True:
            root = self.http.xml(BASE + "/", params=params)
            if root.tag != f"{{{NS['s']}}}ListBucketResult":
                raise RemoteError("PMC bucket returned an invalid listing")
            for node in root.findall("s:CommonPrefixes/s:Prefix", NS):
                value = node.text or ""
                if not re.fullmatch(re.escape(pmcid) + r"\.[1-9][0-9]*/", value):
                    raise RemoteError("Unexpected article version in PMC listing")
                prefixes.add(value)
            truncated = root.findtext("s:IsTruncated", namespaces=NS)
            if truncated == "false":
                break
            if truncated != "true":
                raise RemoteError("Missing PMC listing pagination indicator")
            token = root.findtext("s:NextContinuationToken", namespaces=NS)
            if not token or token in seen_tokens:
                raise RemoteError("Invalid PMC pagination token")
            seen_tokens.add(token)
            params["continuation-token"] = token
        result = []
        for prefix in sorted(prefixes):
            version = int(prefix.rstrip("/").split(".")[-1])
            metadata_url = f"{BASE}/metadata/{pmcid}.{version}.json"
            obj = self.http.json(metadata_url)
            if obj.get("pmcid") != pmcid or obj.get("version") != version:
                raise RemoteError("PMC metadata identity mismatch")
            if obj.get("pmid") is not None and str(obj["pmid"]) != pmid:
                raise RemoteError("PMC metadata does not match the originating PMID")
            if type(obj.get("is_pmc_openaccess")) is not bool:
                raise RemoteError("PMC metadata lacks a valid OA classification")
            # Keep all returned versions in provenance, including excluded non-OA versions.
            result.append({**obj, "metadata_url": metadata_url, "retrieved_at": now()})
        return result
