"""HTTP access with an on-disk cache, retries and polite per-host rate limiting.

Also holds the thin API wrappers for NIH RePORTER, Europe PMC and the PMC Cloud Service.
Standard library only.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import __version__

USER_AGENT = (f"output-receipts/{__version__} (open-source research prototype that measures the findability "
              "of shared outputs from NIH-funded papers; polite 1 req/s per host)")

MAX_BYTES = 25 * 1024 * 1024


@dataclass
class Response:
    url: str
    status: int | None          # None = network error
    text: str = ""
    final_url: str = ""
    from_cache: bool = False
    error: str = ""
    headers: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    def json(self):
        return json.loads(self.text)


class RateLimiter:
    """At most one request per `interval` seconds per host (thread-safe)."""

    def __init__(self, interval: float = 1.0, overrides: dict | None = None):
        self.interval = interval
        self.overrides = overrides or {}
        self._next = {}
        self._lock = threading.Lock()

    def wait(self, host: str):
        iv = self.overrides.get(host, self.interval)
        with self._lock:
            now = time.monotonic()
            t = max(now, self._next.get(host, 0.0))
            self._next[host] = t + iv
        delay = t - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class Http:
    """GET/POST/HEAD with gzip-JSON disk cache. Definitive answers (2xx, 3xx, 4xx except 429) are cached;
    5xx, 429 and network errors are retried and never cached, so a rerun tries them again."""

    def __init__(self, cache_dir: str, interval: float = 1.0, host_intervals: dict | None = None,
                 timeout: int = 60, offline: bool = False):
        self.cache_dir = cache_dir
        self.limiter = RateLimiter(interval, host_intervals)
        self.timeout = timeout
        self.offline = offline
        self.stats = {"network": 0, "cache": 0, "errors": 0}
        self._slock = threading.Lock()

    # --- cache -----------------------------------------------------------------------------------
    def _path(self, method: str, url: str, body: bytes | None) -> str:
        h = hashlib.sha1((method + " " + url).encode() + (body or b"")).hexdigest()
        host = urllib.parse.urlsplit(url).netloc.replace(":", "_") or "nohost"
        return os.path.join(self.cache_dir, host, h[:2], h + ".json.gz")

    def _load(self, path):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _save(self, path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + f".{threading.get_ident()}.tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, path)

    def _count(self, k):
        with self._slock:
            self.stats[k] += 1

    # --- request ---------------------------------------------------------------------------------
    def request(self, url: str, method: str = "GET", json_body=None, headers: dict | None = None,
                cache: bool = True, retries: int = 3, timeout: int | None = None) -> Response:
        body = json.dumps(json_body).encode() if json_body is not None else None
        path = self._path(method, url, body)
        if cache:
            c = self._load(path)
            if c is not None:
                self._count("cache")
                return Response(url, c["status"], c.get("text", ""), c.get("final_url", url), True,
                                headers=c.get("headers", {}))
        if self.offline:
            return Response(url, None, error="offline mode: not in cache")
        hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        host = urllib.parse.urlsplit(url).netloc
        last = Response(url, None, error="not attempted")
        for attempt in range(retries + 1):
            self.limiter.wait(host)
            self._count("network")
            try:
                req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as f:
                    raw = f.read(MAX_BYTES) if method != "HEAD" else b""
                    resp = Response(url, f.status, raw.decode("utf-8", "replace"), f.geturl(),
                                    headers={k.lower(): v for k, v in f.headers.items()
                                             if k.lower() in ("content-type", "location", "retry-after")})
            except urllib.error.HTTPError as e:
                try:
                    raw = e.read(1024 * 1024) if method != "HEAD" else b""
                except Exception:
                    raw = b""
                resp = Response(url, e.code, raw.decode("utf-8", "replace"), url,
                                headers={k.lower(): v for k, v in (e.headers or {}).items()
                                         if k.lower() in ("content-type", "retry-after")})
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
                resp = Response(url, None, error=f"{type(e).__name__}: {e}")
            last = resp
            transient = resp.status is None or resp.status == 429 or resp.status >= 500
            if not transient:
                if cache:
                    self._save(path, {"url": url, "status": resp.status, "text": resp.text,
                                      "final_url": resp.final_url, "headers": resp.headers,
                                      "fetched": time.strftime("%Y-%m-%dT%H:%M:%S")})
                return resp
            if attempt < retries:
                wait = [3, 10, 30, 60][min(attempt, 3)]
                ra = resp.headers.get("retry-after", "")
                if ra.isdigit():
                    wait = min(max(wait, int(ra)), 120)
                time.sleep(wait)
        self._count("errors")
        return last


# ================================================================================================
# NIH RePORTER
# ================================================================================================
REPORTER = "https://api.reporter.nih.gov/v2"
PROJECT_FIELDS = ["ApplId", "ProjectNum", "CoreProjectNum", "ProjectTitle", "ProjectStartDate",
                  "ProjectEndDate", "AwardAmount", "Organization", "FiscalYear", "AwardType",
                  "ActivityCode", "AgencyIcAdmin", "OpportunityNumber", "ProjectDetailUrl",
                  "AwardNoticeDate"]


def reporter_projects(http: Http, fy: int, activity: str, ic: str, award_type: str = "1") -> tuple[list, int]:
    crit = {"agencies": [ic], "fiscal_years": [fy], "activity_codes": [activity], "award_types": [award_type]}
    out, offset, total = [], 0, None
    while True:
        r = http.request(f"{REPORTER}/projects/search", "POST",
                         {"criteria": crit, "include_fields": PROJECT_FIELDS, "offset": offset, "limit": 500,
                          "sort_field": "appl_id", "sort_order": "asc"})
        if not r.ok:
            raise RuntimeError(f"RePORTER projects search failed: {r.status} {r.error}")
        d = r.json()
        total = d["meta"]["total"]
        out.extend(d["results"])
        offset += 500
        if offset >= total or not d["results"]:
            break
    return out, total


def reporter_publications(http: Http, core_nums: list[str], batch: int = 50) -> list[dict]:
    """Rows of {coreproject, pmid, applid} linking core project numbers to PMIDs."""
    rows = []
    for i in range(0, len(core_nums), batch):
        chunk = core_nums[i:i + batch]
        offset = 0
        while True:
            r = http.request(f"{REPORTER}/publications/search", "POST",
                             {"criteria": {"core_project_nums": chunk}, "offset": offset, "limit": 500})
            if not r.ok:
                raise RuntimeError(f"RePORTER publications search failed: {r.status} {r.error}")
            d = r.json()
            rows.extend(d["results"])
            offset += 500
            if offset >= d["meta"]["total"] or not d["results"]:
                break
    return rows


# ================================================================================================
# Europe PMC (metadata) + full text (PMC Cloud Service first, Europe PMC fallback)
# ================================================================================================
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
PMC_S3 = "https://pmc-oa-opendata.s3.amazonaws.com"


def epmc_metadata(http: Http, pmids: list[str], batch: int = 100) -> dict[str, dict]:
    """PMID -> Europe PMC 'lite' record (pmcid, isOpenAccess, inPMC, pubType, firstPublicationDate...)."""
    out = {}
    for i in range(0, len(pmids), batch):
        chunk = pmids[i:i + batch]
        q = "(" + " OR ".join(f"EXT_ID:{p}" for p in chunk) + ") AND SRC:MED"
        url = f"{EPMC}/search?" + urllib.parse.urlencode(
            {"query": q, "resultType": "lite", "format": "json", "pageSize": 1000})
        r = http.request(url)
        if not r.ok:
            continue
        for rec in r.json().get("resultList", {}).get("result", []):
            if rec.get("pmid"):
                out[str(rec["pmid"])] = rec
    return out


def pubmed_esummary(http: Http, pmids: list[str], batch: int = 200) -> dict[str, dict]:
    """Fallback metadata from PubMed for PMIDs Europe PMC does not index under source MED (e.g. preprints
    in PMC via the NIH Preprint Pilot). Returned in the same shape as the Europe PMC 'lite' record."""
    out = {}
    for i in range(0, len(pmids), batch):
        chunk = pmids[i:i + batch]
        r = http.request("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&retmode=json&id="
                         + ",".join(chunk))
        if not r.ok:
            continue
        res = r.json().get("result", {})
        for u in res.get("uids", []):
            d = res[u]
            ids = {a["idtype"]: a["value"] for a in d.get("articleids", [])}
            date = (d.get("sortpubdate") or "")[:10].replace("/", "-")
            out[u] = {"pmid": u, "pmcid": ids.get("pmc"), "doi": ids.get("doi"), "title": d.get("title"),
                      "journalTitle": d.get("fulljournalname") or d.get("source"), "pubYear": date[:4],
                      "firstPublicationDate": date, "pubType": "; ".join(d.get("pubtype", [])),
                      "isOpenAccess": "N", "inPMC": "Y" if ids.get("pmc") else "N", "_source": "pubmed",
                      "authorString": ", ".join(a.get("name", "") for a in d.get("authors", []))}
    return out


def fetch_fulltext(http: Http, pmcid: str, is_oa: bool) -> dict:
    """Try the PMC Cloud Service (open-access subset + NIH author manuscripts, JATS XML) and Europe PMC's
    fullTextXML (open-access subset). Order alternates by OA status to spread load across the two hosts.
    Returns {source, xml} or {source: None, tried: [...]}"""
    s3 = ("pmc_cloud", f"{PMC_S3}/{pmcid}.1/{pmcid}.1.xml", 3)
    ep = ("europepmc", f"{EPMC}/{pmcid}/fullTextXML", 1)  # EPMC answers 500 for non-OA items: retry once only
    order = [ep, s3] if is_oa else [s3]
    tried = []
    for name, url, retries in order:
        r = http.request(url, retries=retries)
        tried.append(f"{name}:{r.status if r.status is not None else r.error[:60]}")
        if r.ok and "<article" in r.text[:5000]:
            return {"source": name, "xml": r.text, "tried": tried}
    return {"source": None, "xml": None, "tried": tried}
