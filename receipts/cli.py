"""Command line entry point:  python -m receipts run --fy 2024 --activity R01 --ic NCI

Subcommands
  run         fetch -> extract -> resolve -> classify -> report (all HTTP is cached under data/cache)
  reclassify  re-apply rules + review overrides to data/work/*.json and rebuild outputs (no network)
  sample      draw the stratified hand-check sample from the current outputs
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import classify, crosscheck, extract, fetch, records, report, resolve


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _paths(data_dir):
    p = {k: os.path.join(data_dir, k) for k in ("cache", "work", "output", "review")}
    for v in p.values():
        os.makedirs(v, exist_ok=True)
    return p


def _date(s):
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        try:
            return dt.date(int(s[:4]), 1, 1)
        except ValueError:
            return None


def cmd_run(a):
    P = _paths(a.data_dir)
    http = fetch.Http(P["cache"], interval=a.interval, offline=a.offline)
    run_info = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "query": {"fy": a.fy, "activity": a.activity,
                "ic": a.ic, "award_type": a.award_type}, "api_notes": []}

    # 1. grants -------------------------------------------------------------------------------------
    _log(f"RePORTER: {a.ic} FY{a.fy} {a.activity} type-{a.award_type} awards")
    projects, total = fetch.reporter_projects(http, a.fy, a.activity, a.ic, a.award_type)
    if a.limit_grants:
        projects = projects[:a.limit_grants]
    grants = {}
    for p in projects:
        core = p["core_project_num"]
        g = grants.setdefault(core, {"core_project_num": core, "project_nums": [], "appl_ids": [],
                                     "title": p.get("project_title"), "org": (p.get("organization") or {}).get("org_name"),
                                     "org_state": (p.get("organization") or {}).get("org_state"),
                                     "fiscal_year": p.get("fiscal_year"), "start_date": (p.get("project_start_date") or "")[:10],
                                     "end_date": (p.get("project_end_date") or "")[:10], "award_amount": 0,
                                     "admin_ic": (p.get("agency_ic_admin") or {}).get("abbreviation"),
                                     "opportunity_number": p.get("opportunity_number"),
                                     "reporter_url": p.get("project_detail_url")})
        g["project_nums"].append(p.get("project_num"))
        g["appl_ids"].append(p.get("appl_id"))
        g["award_amount"] += p.get("award_amount") or 0
    _log(f"  {total} awards reported, {len(projects)} fetched, {len(grants)} unique core projects")

    # 2. linked publications --------------------------------------------------------------------------
    links = fetch.reporter_publications(http, sorted(grants))
    pmid_grants = {}
    for r in links:
        if r.get("coreproject") in grants and r.get("pmid"):
            pmid_grants.setdefault(str(r["pmid"]), set()).add(r["coreproject"])
    _log(f"  {len(links)} grant-publication links, {len(pmid_grants)} unique PMIDs")

    # 3. Europe PMC metadata + early-publication filter (same 365-day rule as the INS pipeline) -------
    meta = fetch.epmc_metadata(http, sorted(pmid_grants))
    _log(f"  Europe PMC metadata for {len(meta)}/{len(pmid_grants)} PMIDs")
    missing = sorted(set(pmid_grants) - set(meta))
    if missing:
        fb = fetch.pubmed_esummary(http, missing)
        meta.update(fb)
        _log(f"  PubMed fallback metadata for {len(fb)}/{len(missing)} (mostly preprints indexed by Europe PMC "
             f"under a different source)")
    papers, excluded = {}, []
    for pmid, cores in pmid_grants.items():
        m = meta.get(pmid, {})
        pdate = _date(m.get("firstPublicationDate")) or _date(m.get("pubYear"))
        keep = set()
        for c in cores:
            st = _date(grants[c]["start_date"])
            if pdate is None or st is None or pdate >= st - dt.timedelta(days=365):
                keep.add(c)
        if not keep:
            excluded.append({"pmid": pmid, "grants": sorted(cores), "pub_date": str(pdate)})
            continue
        papers[pmid] = {"pmid": pmid, "grants": sorted(keep), "pmcid": m.get("pmcid"), "doi": m.get("doi"),
                        "title": m.get("title"), "authors": m.get("authorString") or "",
                        "journal": m.get("journalTitle"), "pub_year": m.get("pubYear"),
                        "first_pub_date": m.get("firstPublicationDate"), "pub_type": m.get("pubType"),
                        "is_oa": m.get("isOpenAccess") == "Y", "in_pmc": m.get("inPMC") == "Y",
                        "metadata_source": m.get("_source", "europepmc") if m else None,
                        "is_preprint": "preprint" in (m.get("pubType") or "").lower()}
    _log(f"  {len(papers)} PMIDs in scope, {len(excluded)} excluded as published >365 days before award start")

    # 4. open full text -> extraction -----------------------------------------------------------------
    todo = [p for p in papers.values() if p["pmcid"]]
    for p in papers.values():
        if not p["pmcid"]:
            p.update(fulltext_source=None, fulltext_note="no PMCID (not in PMC)")
    lock = threading.Lock()
    done = [0]

    def work(p):
        ft = fetch.fetch_fulltext(http, p["pmcid"], p["is_oa"])
        if ft["source"]:
            doc = extract.parse_jats(ft["xml"])
            if doc.get("error") or not doc["passages"]:
                p.update(fulltext_source=None, fulltext_note=f"full text unparseable: {doc.get('error', 'empty')}"[:120])
            else:
                p.update(fulltext_source=ft["source"], fulltext_note="", article_type=doc["article_type"],
                         is_manuscript=doc["is_manuscript"], extraction=extract.analyze(doc))
        else:
            p.update(fulltext_source=None, fulltext_note="in PMC but not in open-access or author-manuscript "
                     "text-mining sets (" + "; ".join(ft["tried"]) + ")")
        with lock:
            done[0] += 1
            if done[0] % 100 == 0:
                _log(f"  full text {done[0]}/{len(todo)}  http={http.stats}")

    oa = [p for p in todo if p["is_oa"]]
    non = [p for p in todo if not p["is_oa"]]
    _log(f"Full text: {len(todo)} with PMCID ({len(oa)} via Europe PMC first, {len(non)} via PMC Cloud Service)")
    with ThreadPoolExecutor(2) as ex:
        list(ex.map(lambda lst: [work(p) for p in lst], [oa, non]))
    n_ft = sum(1 for p in papers.values() if p.get("fulltext_source"))
    _log(f"  open full text for {n_ft}/{len(papers)} papers")

    # 5. resolution -----------------------------------------------------------------------------------
    # own and unclear identifiers, plus GEO series that are "reused" only by default (no cue): their GEO record
    # can show they belong to this paper (crosscheck.promote_linked_geo)
    items = sorted({(i["repo"], i["id"]) for p in papers.values() if p.get("extraction")
                    for i in p["extraction"]["identifiers"]
                    if i["role"] in ("generated", "unknown") or crosscheck.promotable(i) or records.wants(i)})
    resolution, down = {}, {}
    if not a.skip_resolve:
        _log(f"Resolving {len(items)} unique identifiers that papers present as their own (or unclear)")
        resolution, down = resolve.resolve_all(http, items, log=_log)
    for k, n in down.items():
        run_info["api_notes"].append(f"{k}: resolver unavailable, {n} identifiers skipped")
    # what each repository's own record says (records.py): fetched here, then read from the cache by crosscheck
    if not a.skip_resolve:
        want = sorted({(i["repo"], i["id"]) for p in papers.values() if p.get("extraction")
                       for i in p["extraction"]["identifiers"] if records.wants(i)})
        _log(f"Reading {len(want)} repository records (papers cited, grants named, submitters, release date)")
        records.warm(http, want, log=_log)
    n_dem, n_pro = crosscheck.apply(papers, fetch.Http(P["cache"], offline=True))
    _log(f"  record cross-check: {n_dem} (paper, identifier) pairs demoted to reused (the record points to other "
         f"work); {n_pro} promoted to own (the record points to this paper)")
    geo_pm = sorted({p["pmid"] for p in papers.values() if p.get("extraction") and any(
        i["repo"] == "GEO" and i["role"] == "generated" for i in p["extraction"]["identifiers"])})
    geo_links = resolve.geo_pubmed_links(http, geo_pm) if not a.skip_resolve else {}
    _log(f"  PubMed->GEO links checked for {len(geo_links)}/{len(geo_pm)} papers with their own GEO series")
    gdc_probe = http.request("https://api.gdc.cancer.gov/status", cache=False, retries=0)
    run_info["api_notes"].append(f"GDC API status at end of run: HTTP {gdc_probe.status or gdc_probe.error[:60]}")

    run_info.update(finished=time.strftime("%Y-%m-%dT%H:%M:%S"), http_stats=http.stats,
                    awards_reported=total, awards_fetched=len(projects), excluded_early=excluded)
    work_dir = P["work"]
    _dump(os.path.join(work_dir, "grants.json"), grants)
    _dump(os.path.join(work_dir, "papers.json"), papers)
    _dump(os.path.join(work_dir, "resolution.json"), resolution)
    _dump(os.path.join(work_dir, "geo_pubmed_links.json"), geo_links)
    _dump(os.path.join(work_dir, "run_info.json"), run_info)
    _log("Classifying and writing outputs")
    return _classify_and_report(P)


def _dump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=lambda o: sorted(o) if isinstance(o, set) else str(o))


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _classify_and_report(P):
    grants = _load(os.path.join(P["work"], "grants.json"))
    papers = _load(os.path.join(P["work"], "papers.json"))
    resolution = _load(os.path.join(P["work"], "resolution.json"))
    run_info = _load(os.path.join(P["work"], "run_info.json"))
    overrides = classify.load_overrides(os.path.join(P["review"], "llm_review_labels.csv"))
    # idempotent; also applies the GEO cross-check to work directories written before it existed (cache only)
    cache = fetch.Http(P["cache"], offline=True)
    crosscheck.apply(papers, cache)
    groups = crosscheck.study_groups(papers, cache)
    for p in papers.values():
        p["classification"] = classify.classify(p, resolution, overrides)
    # save the roles and labels as reported, so the validate/ scripts read the same state as the outputs
    _dump(os.path.join(P["work"], "papers.json"), papers)
    report.write_review_queue(papers, os.path.join(P["review"], "needs_review.jsonl"))
    gl_path = os.path.join(P["work"], "geo_pubmed_links.json")
    geo_links = _load(gl_path) if os.path.exists(gl_path) else {}
    summary = report.write_all(grants, papers, resolution, run_info, P["output"], geo_links, study_groups=groups)
    _log(json.dumps(summary["headline"], indent=1))
    return 0


def cmd_reclassify(a):
    return _classify_and_report(_paths(a.data_dir))


def cmd_sample(a):
    P = _paths(a.data_dir)
    papers = _load(os.path.join(P["work"], "papers.json"))
    resolution = _load(os.path.join(P["work"], "resolution.json"))
    overrides = classify.load_overrides(os.path.join(P["review"], "llm_review_labels.csv"))
    crosscheck.apply(papers, fetch.Http(P["cache"], offline=True))
    for p in papers.values():
        p["classification"] = classify.classify(p, resolution, overrides)
    n = report.write_handcheck_sample(papers, resolution, os.path.join(P["output"], "handcheck_sample.csv"),
                                      n=a.n, seed=a.seed)
    _log(f"wrote {n} rows")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m receipts", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="full pipeline")
    r.add_argument("--fy", type=int, default=2024)
    r.add_argument("--activity", default="R01")
    r.add_argument("--ic", default="NCI")
    r.add_argument("--award-type", default="1", help="NIH application type; 1 = new award")
    r.add_argument("--limit-grants", type=int, default=0, help="only the first N awards (for testing)")
    r.add_argument("--interval", type=float, default=1.0, help="seconds between requests to the same host (>=1)")
    r.add_argument("--skip-resolve", action="store_true")
    r.add_argument("--offline", action="store_true", help="use the cache only")
    r.set_defaults(func=cmd_run)
    c = sub.add_parser("reclassify", help="re-apply rules and review labels, rebuild outputs (no network)")
    c.set_defaults(func=cmd_reclassify)
    s = sub.add_parser("sample", help="draw the stratified hand-check sample")
    s.add_argument("--n", type=int, default=30)
    s.add_argument("--seed", type=int, default=2024)
    s.set_defaults(func=cmd_sample)
    a = ap.parse_args(argv)
    if getattr(a, "interval", 1.0) < 1.0:
        ap.error("--interval must be >= 1.0 (polite rate limit)")
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
