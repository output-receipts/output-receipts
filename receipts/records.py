"""What the repository's own record says about a deposit, compared with the paper that mentions it.

The sentence rules (extract.py) read the PAPER. This module reads the RECORD in the repository: the papers it cites,
the grants it names, the people who submitted it and when it became public. Where the two agree, the "own deposit"
call no longer rests on wording alone; where they disagree, the record wins.

    record(http, repo, ident)   -> the record's facts, or None (repository without a usable record, or not cached)
    check(paper, rec)           -> (tier, note)

Tiers
    cites_paper    the record cites this paper (PubMed ID or DOI)
    names_award    the record names a grant this paper is linked to
    authors        a submitter/contributor of the record is an author of this paper, and the record did not become
                   public more than a year before the paper (an older record by the same people may be the group's
                   earlier work: no verdict)
    contradicted   the record points away from this paper: it became public more than a year before the paper and
                   cites other papers, or it is by other people and either old or tied to another paper
    ""             the record says nothing either way (the call rests on the paper's wording alone)

Records come from the same public interfaces the link check uses, plus DataCite (for DOI-based repositories) and
NCBI E-utilities (BioProject). All requests go through the cached, rate-limited Http client: `warm` fetches them
during a run; everything else reads the cache only.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import unicodedata
import urllib.parse
from collections import defaultdict

from .resolve import EUTILS, geo_record_url

GRACE_DAYS = 365
DATACITE = "https://api.datacite.org/dois/"
DATACITE_REPOS = ("Zenodo", "Figshare", "Dryad", "Mendeley Data", "Dataverse", "TCIA", "Code Ocean", "Synapse")
RECORD_REPOS = ("GEO", "SRA/BioProject", "PRIDE/ProteomeXchange", "PDB", "EMDB") + DATACITE_REPOS
STRONG = ("cites_paper", "names_award")
CONFIRMED = STRONG + ("authors",)
TIER_TEXT = {"cites_paper": "record cites this paper", "names_award": "record names this award",
             "authors": "record is by this paper's authors", "contradicted": "record points to other work",
             "": "paper only"}
_GRANT = re.compile(r"\b([A-Z]\d{2})[\s-]?([A-Z]{2})[\s-]?(\d{6})\b")
_LAB = re.compile(r"\b(?:lab|laboratory|group|team)\b", re.I)


# ================================================================================================
# names
# ================================================================================================
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def _key(surname: str, given: str = "") -> tuple[str, str] | None:
    s = _norm(surname)
    return (s, _norm(given)[:1]) if len(s) >= 2 else None


def person(name: str, style: str = "auto") -> tuple[str, str] | None:
    """(surname, first initial) from one name. Styles: "last_initials" (Smith JB), "first_last" (Jane B Smith),
    "geo" (Jane,B,Smith), "auto" (comma -> Last, First; lab names -> surname only; otherwise first_last)."""
    name = (name or "").strip().strip(".")
    if not name:
        return None
    if style == "geo":
        parts = [x.strip() for x in name.split(",")]
        return _key(parts[-1], parts[0]) if len(parts) >= 2 else person(name, "auto")
    if style == "last_initials":
        toks = name.split()
        if len(toks) >= 2 and toks[-1].replace("-", "").isalpha() and toks[-1].replace("-", "").isupper() \
                and len(toks[-1]) <= 4:
            return _key(" ".join(toks[:-1]), toks[-1])
        return _key(name)
    if "," in name:
        last, _, first = name.partition(",")
        return _key(last, first)
    if _LAB.search(name):   # "Park laboratory", "Ludewig Lab": the surname only
        rest = _LAB.sub("", name).split()
        return _key(rest[-1]) if rest else None
    toks = name.split()
    return _key(toks[-1], toks[0]) if len(toks) >= 2 else _key(name)


def paper_people(authors: str) -> set:
    """Europe PMC / PubMed author strings: "Mohammed H, Hernando-Herraez I, Reik W." """
    return {k for a in (authors or "").split(",") if (k := person(a, "last_initials"))}


def people_match(a: set, b: set) -> bool:
    for s1, i1 in a:
        for s2, i2 in b:
            if s1 == s2 and (not i1 or not i2 or i1 == i2):
                return True
    return False


def grants_in(text: str) -> set:
    return {"".join(m.groups()) for m in _GRANT.finditer(text or "")}


def _date(s) -> dt.date | None:
    m = re.match(r"(\d{4})[-/](\d{2})[-/](\d{2})", str(s or ""))
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    except ValueError:
        return None


def _doi(s) -> str:
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)\s*", "", str(s or "").strip(), flags=re.I).lower()


def _new(source: str) -> dict:
    """pubs_complete: the record's list of papers can be used as evidence AGAINST a paper that is not on it.
    people_complete: likewise for its list of people (false when the names may be usernames or organisations)."""
    return {"source": source, "date": None, "pmids": set(), "dois": set(), "people": set(), "grants": set(),
            "pubs_complete": False, "people_complete": True}


# ================================================================================================
# one parser per record type
# ================================================================================================
def _geo(http, gse):
    r = http.request(geo_record_url(gse))
    if not r.ok or "!Series_title" not in r.text:
        return None
    rec = _new("GEO record")
    m = re.search(r"^!Series_status = Public on (\w{3} \d{1,2} \d{4})", r.text, re.M)
    try:
        rec["date"] = dt.datetime.strptime(m.group(1), "%b %d %Y").date() if m else None
    except ValueError:
        pass
    rec["pmids"] = set(re.findall(r"^!Series_pubmed_id = (\d+)", r.text, re.M))
    rec["people"] = {k for n in re.findall(r"^!Series_(?:contributor|contact_name) = (.+?)\s*$", r.text, re.M)
                     if (k := person(n, "geo"))}
    rec["pubs_complete"] = True
    return rec


def _json(http, url):
    r = http.request(url)
    if not r.ok:
        return None
    try:
        return json.loads(r.text)
    except ValueError:
        return None


def bioproject_urls(uid: str) -> dict:
    return {"summary": f"{EUTILS}/esummary.fcgi?db=bioproject&retmode=json&id={uid}",
            "pubmed": f"{EUTILS}/elink.fcgi?dbfrom=bioproject&db=pubmed&retmode=json&id={uid}",
            "gds": f"{EUTILS}/elink.fcgi?dbfrom=bioproject&db=gds&retmode=json&id={uid}"}


def _links(d, linkname):
    out = []
    for ls in (d or {}).get("linksets", []):
        for db in ls.get("linksetdbs", []):
            if db.get("linkname") == linkname:
                out += [str(u) for u in db.get("links", [])]
    return out


def _bioproject(http, acc):
    if not acc.upper().startswith("PRJ"):
        return None
    d = _json(http, f"{EUTILS}/esearch.fcgi?db=bioproject&retmode=json&term={acc}")   # cached by the link check
    ids = (d or {}).get("esearchresult", {}).get("idlist", [])
    if len(ids) != 1:
        return None
    u = bioproject_urls(ids[0])
    s = _json(http, u["summary"])
    if s is None:
        return None
    rec = _new("BioProject record")
    rec["date"] = _date((s.get("result", {}).get(ids[0], {}) or {}).get("registration_date"))
    rec["pmids"] = set(_links(_json(http, u["pubmed"]), "bioproject_pubmed"))
    # a BioProject that mirrors a GEO series: GEO's record names the submitters and the paper
    for uid in _links(_json(http, u["gds"]), "bioproject_gds"):
        if uid.isdigit() and 200000000 < int(uid) < 300000000:
            g = _geo(http, f"GSE{int(uid) - 200000000}")
            if g:
                rec["pmids"] |= g["pmids"]
                rec["people"] |= g["people"]
                rec["date"] = min([x for x in (rec["date"], g["date"]) if x], default=None)
    rec["pubs_complete"] = bool(rec["pmids"])
    return rec


def _pride(http, pxd):
    d = _json(http, f"https://proteomecentral.proteomexchange.org/cgi/GetDataset?ID={pxd}&outputMode=JSON")
    if not isinstance(d, dict):
        return None
    rec = _new("ProteomeXchange record")
    for pub in d.get("publications") or []:
        for t in pub.get("terms", []):
            if t.get("name") == "PubMed identifier" and str(t.get("value", "")).isdigit():
                rec["pmids"].add(str(t["value"]))
            elif t.get("name") == "Digital Object Identifier (DOI)" and t.get("value"):
                rec["dois"].add(_doi(t["value"]))
    for c in d.get("contacts") or []:
        for t in c.get("terms", []):
            if t.get("name") == "contact name" and (k := person(t.get("value", ""))):
                rec["people"].add(k)
    rec["pubs_complete"] = bool(rec["pmids"])
    return rec


def _pdb(http, pid):
    d = _json(http, f"https://data.rcsb.org/rest/v1/core/entry/{pid}")
    if not isinstance(d, dict):
        return None
    rec = _new("PDB record")
    rec["date"] = _date((d.get("rcsb_accession_info") or {}).get("initial_release_date"))
    cit = d.get("rcsb_primary_citation") or {}
    if cit.get("pdbx_database_id_PubMed"):
        rec["pmids"].add(str(cit["pdbx_database_id_PubMed"]))
    if cit.get("pdbx_database_id_DOI"):
        rec["dois"].add(_doi(cit["pdbx_database_id_DOI"]))
    names = list(cit.get("rcsb_authors") or []) + [a.get("name", "") for a in d.get("audit_author") or []]
    rec["people"] = {k for n in names if (k := person(n))}
    rec["grants"] = {g for s in d.get("pdbx_audit_support") or [] for g in grants_in(s.get("grant_number") or "")}
    rec["pubs_complete"] = bool(rec["pmids"])
    return rec


def _emdb(http, eid):
    d = _json(http, f"https://www.ebi.ac.uk/emdb/api/entry/{eid}")
    if not isinstance(d, dict):
        return None
    rec = _new("EMDB record")
    admin = d.get("admin") or {}
    rec["date"] = _date((admin.get("key_dates") or {}).get("header_release"))
    cit = (((d.get("crossreferences") or {}).get("citation_list") or {}).get("primary_citation") or {}) \
        .get("citation_type") or {}
    for ref in cit.get("external_references") or []:
        if ref.get("type_") == "PUBMED":
            rec["pmids"].add(str(ref.get("valueOf_")))
        elif ref.get("type_") == "DOI":
            rec["dois"].add(_doi(ref.get("valueOf_")))
    names = [a.get("valueOf_", "") for a in cit.get("author") or []] + \
            [a.get("valueOf_", "") for a in (admin.get("authors_list") or {}).get("author") or []]
    rec["people"] = {k for n in names if (k := person(n, "last_initials"))}
    rec["grants"] = {g for r in (admin.get("grant_support") or {}).get("grant_reference") or []
                     for g in grants_in(r.get("code") or "")}
    rec["pubs_complete"] = bool(rec["pmids"])
    return rec


def datacite_doi(repo: str, ident: str) -> str | None:
    if repo == "Synapse":
        return "10.7303/" + ident
    return ident if ident.lower().startswith("10.") else None


def datacite_url(doi: str) -> str:
    return DATACITE + urllib.parse.quote(doi, safe="/.:-_()")


def _datacite(http, repo, ident):
    doi = datacite_doi(repo, ident)
    if not doi:
        return None
    d = _json(http, datacite_url(doi))
    if d is None and repo == "Mendeley Data" and re.search(r"\.\d+$", doi):
        d = _json(http, datacite_url(re.sub(r"\.\d+$", "", doi)))
    a = ((d or {}).get("data") or {}).get("attributes")
    if not a:
        return None
    rec = _new("DataCite record")
    dates = [_date(x.get("date")) for x in a.get("dates") or [] if x.get("dateType") in ("Issued", "Created", "Available")]
    rec["date"] = min([x for x in dates + [_date(a.get("created"))] if x], default=None)
    for c in a.get("creators") or []:
        # creators are free text: "Xiao, Yujie" (a person), but also "bkim6" or "Park laboratory"
        if c.get("familyName") and c.get("givenName"):
            k = _key(c["familyName"], c["givenName"])
        else:
            k, rec["people_complete"] = person(c.get("name") or ""), False
        if k:
            rec["people"].add(k)
    for r in a.get("relatedIdentifiers") or []:
        if r.get("relatedIdentifierType") == "DOI":
            rec["dois"].add(_doi(r.get("relatedIdentifier")))
        elif r.get("relatedIdentifierType") == "PMID":
            rec["pmids"].add(re.sub(r"\D", "", str(r.get("relatedIdentifier"))))
    rec["grants"] = {g for f in a.get("fundingReferences") or [] for g in grants_in(f.get("awardNumber") or "")}
    return rec   # related identifiers also list versions and code: never used as evidence against a paper


def record(http, repo: str, ident: str) -> dict | None:
    memo = http.__dict__.setdefault("_records", {})
    key = (repo, ident)
    if key not in memo:
        try:
            if repo == "GEO":
                rec = _geo(http, ident) if ident.startswith("GSE") else None
            elif repo == "SRA/BioProject":
                rec = _bioproject(http, ident)
            elif repo == "PRIDE/ProteomeXchange":
                rec = _pride(http, ident)
            elif repo == "PDB":
                rec = _pdb(http, ident)
            elif repo == "EMDB":
                rec = _emdb(http, ident)
            elif repo in DATACITE_REPOS:
                rec = _datacite(http, repo, ident)
            else:
                rec = None
        except (KeyError, TypeError, AttributeError, ValueError):   # an oddly shaped record is "no record"
            rec = None
        memo[key] = rec
    return memo[key]


# ================================================================================================
# paper vs record
# ================================================================================================
def paper_date(p: dict) -> dt.date | None:
    d = _date(p.get("first_pub_date"))
    if d:
        return d
    y = str(p.get("pub_year") or "")[:4]
    return dt.date(int(y), 1, 1) if y.isdigit() else None   # 1 January: the conservative choice


def check(paper: dict, rec: dict | None) -> tuple[str, str]:
    if not rec:
        return "", ""
    src = rec["source"]
    doi = _doi(paper.get("doi"))
    if str(paper.get("pmid")) in rec["pmids"] or (doi and doi in rec["dois"]):
        return "cites_paper", f"{src} cites this paper"
    award = sorted(rec["grants"] & set(paper.get("grants") or []))
    if award:
        return "names_award", f"{src} names award {award[0]}"
    pd = paper_date(paper)
    old = bool(rec["date"] and pd and (pd - rec["date"]).days > GRACE_DAYS)
    other = rec["pubs_complete"] and bool(rec["pmids"] or rec["dois"])
    authors = paper_people(paper.get("authors") or "")
    comparable = bool(rec["people"] and authors)
    same = comparable and people_match(rec["people"], authors)
    if old and other:
        return "contradicted", f"{src} became public more than a year before the paper and cites other papers"
    if comparable and not same and rec["people_complete"] and (old or other):
        return "contradicted", f"{src} is by other people and " + \
            ("became public more than a year before the paper" if old else "cites another paper")
    if same and not old:
        return "authors", f"{src} lists an author of this paper as a submitter"
    return "", ""


def wants(i: dict) -> bool:
    """Identifiers whose record is worth reading: own or unclear ones, and ones that are "reused" only by the
    body-text default (their record can show they belong to the paper)."""
    return i["repo"] in RECORD_REPOS and i["cls"] != "url" and (
        i["role"] in ("generated", "unknown") or "record_check" in i
        or i["evidence"] == "accession in body text without a deposit cue (default)")


def warm_urls(http, repo: str, ident: str) -> None:
    """Fetch (and cache) everything `record` reads for one identifier."""
    record(http, repo, ident)


def warm(http, items: list[tuple[str, str]], log=print) -> None:
    """One worker per host group; the Http rate limiter keeps <=1 request/second per host."""
    queues = defaultdict(list)
    for repo, ident in items:
        queues["ncbi" if repo in ("GEO", "SRA/BioProject") else ("datacite" if repo in DATACITE_REPOS else repo)] \
            .append((repo, ident))

    def worker(q):
        for n, (repo, ident) in enumerate(q):
            warm_urls(http, repo, ident)
            if n and n % 100 == 0:
                log(f"  records {repo}: {n}/{len(q)}")

    threads = [threading.Thread(target=worker, args=(q,), daemon=True) for q in queues.values()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
