"""Does each identifier resolve publicly? Uses the official resolver API where one exists, else HTTP HEAD/GET.

Statuses:
  RESOLVES          the record/page exists and is public
  NOT_FOUND         the resolver says it does not exist (typo, withdrawn, never released, deleted repo)
  PRIVATE           exists but is not yet public (GEO private series, OSF private project)
  RESTRICTED        exists, access needs approval (expected for controlled-access records)
  UNVERIFIED        could not tell (bot protection, unusual response, timeout)
  SKIPPED_API_DOWN  the resolver API was unavailable during the run (recorded, not counted as dead)
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
from collections import defaultdict

from .fetch import Http

NCBI = "https://www.ncbi.nlm.nih.gov"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _st(status, via, code=None, note=""):
    return {"status": status, "via": via, "http": code, "note": note}


def _url_check(http: Http, url: str) -> dict:
    if not url.startswith("http"):
        url = "https://" + url
    r = http.request(url, "HEAD", retries=1, timeout=30)
    # some servers answer HEAD with 404 (or 403/405) although GET works: confirm with one GET before calling it dead
    if r.status in (403, 404, 405, 400, 501) or r.status is None:
        r = http.request(url, "GET", retries=1, timeout=30)
    if r.status is None:
        if re.search(r"getaddrinfo|Name or service|nodename|No address|Errno 11001", r.error):
            return _st("NOT_FOUND", url, None, "domain does not resolve")
        return _st("UNVERIFIED", url, None, r.error[:80])
    if 200 <= r.status < 400:
        return _st("RESOLVES", url, r.status)
    if r.status in (404, 410):
        return _st("NOT_FOUND", url, r.status)
    if r.status in (401, 403, 429):
        return _st("UNVERIFIED", url, r.status, "access denied to automated check")
    if r.status >= 500:
        return _st("UNVERIFIED", url, r.status, "server error")
    return _st("UNVERIFIED", url, r.status)


def _doi_check(http: Http, doi: str) -> dict:
    url = "https://doi.org/api/handles/" + urllib.parse.quote(doi, safe="/.:-_()")
    r = http.request(url, retries=2)
    if r.status is None or r.status >= 500:
        return _st("SKIPPED_API_DOWN", url, r.status)
    try:
        code = json.loads(r.text).get("responseCode")
    except ValueError:
        return _st("UNVERIFIED", url, r.status)
    if code == 1:
        return _st("RESOLVES", url, r.status)
    if code in (100, 200):
        return _st("NOT_FOUND", url, r.status, "DOI not registered")
    return _st("UNVERIFIED", url, r.status, f"handle responseCode {code}")


def _api(http, url, found, notfound_codes=(404,), private=None, method="GET"):
    r = http.request(url, method, retries=2)
    if r.status is None or r.status >= 500:
        return _st("SKIPPED_API_DOWN", url, r.status, r.error[:80])
    if private and private(r):
        return _st("PRIVATE", url, r.status)
    if found(r):
        return _st("RESOLVES", url, r.status)
    if r.status in notfound_codes or r.status == 200 or r.status == 400:
        return _st("NOT_FOUND", url, r.status)
    if r.status in (401, 403):
        return _st("RESTRICTED", url, r.status)
    return _st("UNVERIFIED", url, r.status)


def geo_record_url(gse: str) -> str:
    """GEO's brief text record for a series (title, release date, PubMed IDs, SuperSeries relations). The resolver
    fetches it, so after a run it is in the cache; crosscheck.py reads it from there."""
    return f"{NCBI}/geo/query/acc.cgi?acc={gse}&targ=self&form=text&view=brief"


def resolve_one(http: Http, repo: str, ident: str) -> dict:
    i = ident
    if repo == "GEO":
        if i.startswith("GDS"):
            return _api(http, f"{EUTILS}/esearch.fcgi?db=gds&retmode=json&term={i}[ACCN]",
                        lambda r: r.ok and int(json.loads(r.text)["esearchresult"].get("count", "0")) > 0)
        url = geo_record_url(i)
        return _api(http, url, lambda r: r.ok and "!Series_title" in r.text,
                    private=lambda r: "is currently private" in r.text)
    if repo == "SRA/BioProject":
        db = "bioproject" if i.startswith("PRJ") else "sra"
        return _api(http, f"{EUTILS}/esearch.fcgi?db={db}&retmode=json&term={i}",
                    lambda r: r.ok and int(json.loads(r.text)["esearchresult"].get("count", "0")) > 0)
    if repo == "dbGaP":
        url = f"{NCBI}/gap/sstr/api/v1/study/{i}/summary"
        res = _api(http, url, lambda r: r.ok and '"study"' in r.text)
        if res["status"] == "NOT_FOUND":
            # the summary API answers 404 "No data found for study with accession phsNNNNNN.vK" when its latest
            # version K has no released data yet, although earlier versions are public: ask the dbGaP E-utilities
            # index, which is version-independent, before calling it dead
            alt = _api(http, f"{EUTILS}/esearch.fcgi?db=gap&retmode=json&term={i}",
                       lambda r: r.ok and int(json.loads(r.text)["esearchresult"].get("count", "0")) > 0)
            if alt["status"] == "RESOLVES":
                alt["note"] = "summary API had no data for the latest version; study found in dbGaP E-utilities"
                return alt
        return res
    if repo == "EGA":
        kind = {"S": "studies", "D": "datasets", "C": "dacs"}[i[3]]
        return _api(http, f"https://ega-archive.org/{kind}/{i}", lambda r: r.ok and i in r.text)
    if repo == "PRIDE/ProteomeXchange":
        url = f"https://proteomecentral.proteomexchange.org/cgi/GetDataset?ID={i}&outputMode=JSON"
        # unreleased datasets: "The identifier 'PXD...' has been reserved but it not yet accessible" (HTTP 404)
        return _api(http, url, lambda r: r.ok and i in r.text,
                    private=lambda r: bool(re.search(r"not (?:yet )?(?:been )?(?:released|public|accessible)|"
                                                     r"has been reserved|private", r.text, re.I))
                    and "assigned to a repository" not in r.text)
    if repo == "MassIVE":
        return _api(http, f"https://massive.ucsd.edu/ProteoSAFe/QueryMSV?id={i}",
                    lambda r: r.ok and "Dataset Summary" in r.text, notfound_codes=(400, 404),
                    private=lambda r: "MassIVE Private Dataset" in r.text)
    if repo in ("ArrayExpress", "BioStudies"):
        return _api(http, f"https://www.ebi.ac.uk/biostudies/api/v1/studies/{i}", lambda r: r.ok and '"accno"' in r.text)
    if repo == "MetaboLights":
        return _api(http, f"https://www.ebi.ac.uk/metabolights/ws/studies/{i}", lambda r: r.ok)
    if repo == "Metabolomics Workbench":
        return _api(http, f"https://www.metabolomicsworkbench.org/rest/study/study_id/{i}/summary",
                    lambda r: r.ok and '"study_id"' in r.text)
    if repo == "PDB":
        return _api(http, f"https://data.rcsb.org/rest/v1/core/entry/{i}", lambda r: r.ok)
    if repo == "EMDB":
        return _api(http, f"https://www.ebi.ac.uk/emdb/api/entry/{i}", lambda r: r.ok)
    if repo == "PDC":
        q = urllib.parse.quote('{study(pdc_study_id:"%s"){pdc_study_id}}' % i)
        return _api(http, f"https://pdc.cancer.gov/graphql?query={q}", lambda r: r.ok and i in r.text)
    if repo == "GDC project":
        return _api(http, f"https://api.gdc.cancer.gov/projects/{i}", lambda r: r.ok and '"project_id"' in r.text,
                    notfound_codes=(400, 404))
    if repo == "Synapse":
        return _api(http, f"https://repo-prod.prod.sagebase.org/repo/v1/entity/{i}", lambda r: r.ok)
    if repo == "OSF":
        guid = i.split("/")[-1]
        return _api(http, f"https://api.osf.io/v2/guids/{guid}/", lambda r: r.ok,
                    notfound_codes=(404, 410), private=lambda r: r.status in (401, 403))
    if repo == "GWAS Catalog":
        return _api(http, f"https://www.ebi.ac.uk/gwas/rest/api/studies/{i}", lambda r: r.ok and i in r.text)
    if repo in ("GitHub", "GitLab", "Bitbucket"):
        return _url_check(http, "https://" + i)
    if i.lower().startswith("10."):
        return _doi_check(http, i)
    return _url_check(http, i)


def human_url(repo: str, i: str) -> str:
    """Landing page a person would open for this identifier."""
    m = {
        "GEO": f"{NCBI}/geo/query/acc.cgi?acc={i}",
        "SRA/BioProject": f"{NCBI}/bioproject/{i}" if i.startswith("PRJ") else f"{NCBI}/sra/{i}",
        "dbGaP": f"{NCBI}/projects/gap/cgi-bin/study.cgi?study_id={i}",
        "EGA": f"https://ega-archive.org/{ {'S': 'studies', 'D': 'datasets', 'C': 'dacs'}.get(i[3:4], 'studies') }/{i}",
        "PRIDE/ProteomeXchange": f"https://proteomecentral.proteomexchange.org/cgi/GetDataset?ID={i}",
        "MassIVE": f"https://massive.ucsd.edu/ProteoSAFe/QueryMSV?id={i}",
        "ArrayExpress": f"https://www.ebi.ac.uk/biostudies/studies/{i}",
        "BioStudies": f"https://www.ebi.ac.uk/biostudies/studies/{i}",
        "MetaboLights": f"https://www.ebi.ac.uk/metabolights/{i}",
        "Metabolomics Workbench": f"https://www.metabolomicsworkbench.org/data/DRCCMetadata.php?Mode=Study&StudyID={i}",
        "PDB": f"https://www.rcsb.org/structure/{i}",
        "EMDB": f"https://www.ebi.ac.uk/emdb/{i}",
        "PDC": f"https://pdc.cancer.gov/pdc/study/{i}",
        "GDC project": f"https://portal.gdc.cancer.gov/projects/{i}",
        "Synapse": f"https://www.synapse.org/#!Synapse:{i}",
        "GWAS Catalog": f"https://www.ebi.ac.uk/gwas/studies/{i}",
    }
    if repo in m:
        return m[repo]
    if i.lower().startswith("10."):
        return f"https://doi.org/{i}"
    return i if i.startswith("http") else "https://" + i


def geo_pubmed_links(http: Http, pmids: list[str], batch: int = 100) -> dict[str, list[str]]:
    """PMID -> GEO series linked to it in NCBI (the PubMed->GEO link that E-utilities-based harvesters such as the
    INS pipeline rely on). Used to measure how many of a paper's own GEO deposits such a harvester would miss."""
    out = {}
    for k in range(0, len(pmids), batch):
        chunk = pmids[k:k + batch]
        url = f"{EUTILS}/elink.fcgi?dbfrom=pubmed&db=gds&retmode=json&" + "&".join(f"id={p}" for p in chunk)
        r = http.request(url)
        if not r.ok:
            continue
        try:
            sets = json.loads(r.text).get("linksets", [])
        except ValueError:
            continue
        for ls in sets:
            pm = str(ls.get("ids", [""])[0])
            gse = []
            for db in ls.get("linksetdbs", []):
                if db.get("linkname") == "pubmed_gds":
                    gse += [f"GSE{int(u) - 200000000}" for u in db.get("links", []) if 200000000 < int(u) < 300000000]
            out[pm] = sorted(set(gse))
    return out


def host_key(repo: str, ident: str) -> str:
    if repo in ("GEO", "dbGaP"):
        return "ncbi-www" if not ident.startswith("GDS") else "ncbi-eutils"
    if repo == "SRA/BioProject":
        return "ncbi-eutils"
    if ident.lower().startswith("10.") and repo not in ("OSF",):
        return "doi.org"
    if repo in ("Other link", "Figshare", "TCIA/IDC link", "CELLxGENE"):
        try:
            return urllib.parse.urlsplit(ident if ident.startswith("http") else "https://" + ident).netloc or "misc"
        except ValueError:
            return "misc"
    return repo


def resolve_all(http: Http, items: list[tuple[str, str]], log=print) -> tuple[dict, dict]:
    """items: unique (repo, id). One worker thread per resolver host; the Http limiter keeps <=1 req/s per host.
    If a resolver API looks down (3 consecutive SKIPPED_API_DOWN), the rest of that queue is skipped and recorded."""
    queues = defaultdict(list)
    for repo, ident in items:
        queues[host_key(repo, ident)].append((repo, ident))
    results, down = {}, {}
    lock = threading.Lock()

    def worker(key, q):
        streak = 0
        for n, (repo, ident) in enumerate(q):
            if streak >= 3:
                res = _st("SKIPPED_API_DOWN", key, None, "resolver unavailable during run")
                down[key] = down.get(key, 0) + 1
            else:
                try:
                    res = resolve_one(http, repo, ident)
                except Exception as e:  # never let one odd identifier kill the run
                    res = _st("UNVERIFIED", repo, None, f"{type(e).__name__}: {e}"[:80])
                streak = streak + 1 if res["status"] == "SKIPPED_API_DOWN" else 0
            with lock:
                results[f"{repo}|{ident}"] = res
            if n and n % 100 == 0:
                log(f"  resolver {key}: {n}/{len(q)}")

    threads = [threading.Thread(target=worker, args=(k, q), daemon=True) for k, q in queues.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, down
