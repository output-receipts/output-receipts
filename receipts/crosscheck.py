"""Second-pass checks that use repository records the resolver has already fetched.

Runs after resolution (cli.cmd_run) and again on every reclassify; it reads the records from the HTTP cache only
(an offline Http), so it never makes a network request. If a record is not in the cache, nothing changes.

1. demote_predating_geo: an identifier the sentence rules call "own" is demoted to "reused" when GEO's own record
   contradicts that: the series became public more than 12 months before the paper was published AND GEO links it
   to other PubMed IDs but not to this paper. This catches reused series whose sentence carried no reuse cue
   ("Public scRNA-seq data are available from GEO: GSE141445").
2. promote_linked_geo: the converse, for GEO series the sentence rules could not settle (role "unknown", or
   "reused" only by the body-text default, with no reuse cue): if GEO's record links the series to THIS paper's
   PubMed ID, the series is the paper's own deposit. GEO's citation links are entered for the submitting paper.
3. record_checks: the same idea for every repository whose record names its papers, grants or submitters
   (records.py: GEO, BioProject, ProteomeXchange, PDB, EMDB, and DOI-based repositories through DataCite). Each own
   or unclear identifier gets a record_check tier. A record that contradicts the paper demotes the identifier to
   "reused"; a record that confirms it promotes an unclear identifier to "own" (a body-text default "reused" one
   only when the record cites the paper or names the award).
4. study_groups: collapses accession-level duplicates into studies for counting (the rows stay accession-level):
   GEO SubSeries -> their SuperSeries (from the GEO record), SRA runs/experiments -> the BioProject cited with them
   in the same paper, EGA datasets -> the EGA study cited with them in the same paper.
"""
from __future__ import annotations

import datetime as dt
import re

from . import records
from .resolve import geo_record_url

DEMOTE_EVIDENCE = "GEO record predates the paper and is linked to other papers"
PROMOTE_EVIDENCE = "GEO record is linked to this paper"
BODY_DEFAULT = "accession in body text without a deposit cue (default)"
GRACE_DAYS = 365
RUN_LEVEL = re.compile(r"^[SED]R[RXS]\d+$")


def geo_record(cache_http, gse: str) -> dict | None:
    """Release date, linked PubMed IDs and SuperSeries of a GEO series, from the cached brief text record."""
    _memo = cache_http.__dict__.setdefault("_geo_records", {})
    key = gse
    if key in _memo:
        return _memo[key]
    r = cache_http.request(geo_record_url(gse))
    rec = None
    if r.ok and "!Series_title" in r.text:
        m = re.search(r"^!Series_status = Public on (\w{3} \d{1,2} \d{4})", r.text, re.M)
        try:
            public = dt.datetime.strptime(m.group(1), "%b %d %Y").date() if m else None
        except ValueError:
            public = None
        rec = {"public": public,
               "pmids": set(re.findall(r"^!Series_pubmed_id = (\d+)", r.text, re.M)),
               "superseries": sorted(set(re.findall(r"^!Series_relation = SubSeries of: (GSE\d+)", r.text, re.M)))}
    _memo[key] = rec
    return rec


def paper_date(p: dict) -> dt.date | None:
    s = p.get("first_pub_date") or ""
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        y = str(p.get("pub_year") or "")[:4]
        return dt.date(int(y), 1, 1) if y.isdigit() else None   # 1 January: the conservative choice (demotes less)


def demote_predating_geo(papers: dict, cache_http) -> int:
    """Mutates identifier roles in place; returns the number of (paper, series) pairs demoted. Idempotent."""
    n = 0
    for p in papers.values():
        x = p.get("extraction")
        pd = paper_date(p)
        if not x or pd is None:
            continue
        for i in x["identifiers"]:
            if i["repo"] != "GEO" or not i["id"].startswith("GSE") or i["role"] not in ("generated", "unknown"):
                continue
            rec = geo_record(cache_http, i["id"])
            if not rec or rec["public"] is None or not rec["pmids"] or str(p["pmid"]) in rec["pmids"]:
                continue
            if (pd - rec["public"]).days > GRACE_DAYS:
                i["role_before_crosscheck"], i["evidence_before_crosscheck"] = i["role"], i["evidence"]
                i["role"], i["evidence"] = "reused", DEMOTE_EVIDENCE
                i["record_check"], i["record_note"] = "contradicted", DEMOTE_EVIDENCE
                n += 1
    return n


def promotable(i: dict) -> bool:
    """GEO series whose role came from no cue at all (cli.cmd_run also resolves these, so their record is cached)."""
    return i["repo"] == "GEO" and i["id"].startswith("GSE") and (
        i["role"] == "unknown" or (i["role"] == "reused" and i["evidence"] == BODY_DEFAULT))


def promote_linked_geo(papers: dict, cache_http) -> int:
    """Mutates identifier roles in place; returns the number of (paper, series) pairs promoted. Idempotent."""
    n = 0
    for p in papers.values():
        x = p.get("extraction")
        if not x:
            continue
        for i in x["identifiers"]:
            if not promotable(i):
                continue
            rec = geo_record(cache_http, i["id"])
            if rec and str(p["pmid"]) in rec["pmids"]:
                i["role_before_crosscheck"], i["evidence_before_crosscheck"] = i["role"], i["evidence"]
                i["role"], i["evidence"] = "generated", PROMOTE_EVIDENCE
                n += 1
    return n


def restore(papers: dict) -> None:
    """Put back the roles the sentence rules gave, so that `apply` can be re-run after a rule change (reclassify
    works from saved papers whose roles an earlier `apply` already changed)."""
    for p in papers.values():
        for i in (p.get("extraction") or {"identifiers": []})["identifiers"]:
            if "role_before_crosscheck" in i:
                i["role"], i["evidence"] = i.pop("role_before_crosscheck"), i.pop("evidence_before_crosscheck")
            i.pop("record_check", None)
            i.pop("record_note", None)


def record_checks(papers: dict, cache_http) -> tuple[int, int]:
    """Compare every own/unclear identifier with its repository record (records.check). Sets record_check and
    record_note on the identifier; returns (demoted, promoted). Idempotent; reads the cache only."""
    n_dem = n_pro = 0
    for p in papers.values():
        x = p.get("extraction")
        if not x:
            continue
        for i in x["identifiers"]:
            if not records.wants(i):
                continue
            tier, note = records.check(p, records.record(cache_http, i["repo"], i["id"]))
            i["record_check"], i["record_note"] = tier, note
            new = None
            if tier == "contradicted" and i["role"] in ("generated", "unknown"):
                new, n_dem = "reused", n_dem + 1
            elif (tier in records.CONFIRMED and i["role"] == "unknown") or                     (tier in records.STRONG and i["role"] == "reused" and i["evidence"] == BODY_DEFAULT):
                new, n_pro = "generated", n_pro + 1
            if new:
                i.setdefault("role_before_crosscheck", i["role"])
                i.setdefault("evidence_before_crosscheck", i["evidence"])
                i["role"], i["evidence"] = new, "repository record: " + note
    return n_dem, n_pro


def apply(papers: dict, cache_http) -> tuple[int, int]:
    """The GEO checks (demotion first: a series cannot be both linked and not linked to the paper), then the record
    checks for every repository with a usable record. Returns (demoted, promoted)."""
    restore(papers)
    d1, p1 = demote_predating_geo(papers, cache_http), promote_linked_geo(papers, cache_http)
    d2, p2 = record_checks(papers, cache_http)
    return d1 + d2, p1 + p2


def study_groups(papers: dict, cache_http) -> dict[tuple[str, str], str]:
    """(repo, id) -> study group label "repo:accession" for own data identifiers that collapse into another one.
    Identifiers that do not collapse are absent (their group is themselves)."""
    out = {}
    for pm in sorted(papers, key=lambda k: int(k) if str(k).isdigit() else 0):
        x = papers[pm].get("extraction")
        if not x:
            continue
        own = [i for i in x["identifiers"] if i["role"] == "generated" and not i["is_code"] and i["cls"] != "url"]
        for i in own:
            if i["repo"] == "GEO" and i["id"].startswith("GSE"):
                rec = geo_record(cache_http, i["id"])
                if rec and rec["superseries"]:
                    out.setdefault(("GEO", i["id"]), "GEO:" + rec["superseries"][0])
        sra = [i["id"] for i in own if i["repo"] == "SRA/BioProject"]
        prj = sorted(s for s in sra if s.startswith("PRJ"))
        if len(prj) == 1:
            for s in sra:
                if RUN_LEVEL.match(s):
                    out.setdefault(("SRA/BioProject", s), "SRA/BioProject:" + prj[0])
        ega = [i["id"] for i in own if i["repo"] == "EGA"]
        studies = sorted(e for e in ega if e.startswith("EGAS"))
        if len(studies) == 1:
            for e in ega:
                if e.startswith("EGAD"):
                    out.setdefault(("EGA", e), "EGA:" + studies[0])
    return out
