"""Benchmark against NCI's own records: run the rules on papers the Index of NCI Studies (INS) already lists, and
compare with the datasets INS attributes to those papers. No AI is involved.

  python validate/ins_benchmark.py --ins-dir PATH [--from 2023-01-01 --to 2025-12-31 --extra 1000 --seed 7]
  python validate/ins_benchmark.py --ins-dir PATH --report-only      (recompute the report from the saved state)

PATH holds the public INS-Data tables (see ins_compare.py). Every INS dataset row names the paper(s) it belongs to
(dataset_pmid), gathered by NCI's pipeline from NCBI's publication-to-dataset links and then curated. That makes
(paper, accession) pairs an independent answer key for two questions the cohort run cannot answer:

  RECALL     of the datasets INS attributes to a paper, how many does the tool find, and call the paper's own?
  ADDED      for papers of programs INS already curates, which own deposits does the tool find that INS lacks?

Papers: every INS paper published in the window that has at least one INS dataset, plus --extra random INS papers
from the same window without one. Reads open full text exactly as the main run does (same cache, same rate limits).

Two recall figures are reported for GEO: with the sentence rules alone, and after the GEO record cross-check
(crosscheck.py). The cross-check reads the same NCBI link INS harvests, so only the first is independent of INS.
Writes validate/out/ins_benchmark_report.txt, ins_benchmark.json and ins_benchmark_added.tsv.
"""
import argparse
import csv
import json
import random
import re
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from receipts import crosscheck, extract, fetch, records, resolve  # noqa: E402

csv.field_size_limit(10 ** 9)
OUT = ROOT / "validate/out"
WORK = ROOT / "data/work/ins_benchmark.json"
ACC = re.compile(r"\b(GSE\d+|PRJ[A-Z]{2}\d+|[SED]RP\d+|phs\d{6})\b", re.I)
NCBI = ("GEO", "SRA/BioProject", "dbGaP")


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f, delimiter="\t")


def ins_tables(d: Path):
    """pub date per PMID; (pmid) -> list of {repo, id, alts}; all accessions INS lists anywhere."""
    dates = {}
    for r in read(d / "publication.tsv"):
        dates.setdefault(r["pmid"].strip(), r["publication_date"].strip()[:10])
    by_pmid, everywhere = defaultdict(list), {}
    for f, repo in (("geo_datasets.tsv", "GEO"), ("sra_datasets_curated_clean.tsv", "SRA/BioProject"),
                    ("dbgap_datasets_merged_curated_clean.tsv", "dbGaP")):
        for r in read(d / f):
            acc = r["dataset_source_id"].strip()
            if acc.upper().startswith("GDS"):
                continue   # GEO DataSets are NCBI-curated derivatives; papers cite the series (GSE)
            links = " ".join((r.get("dataset_source_url") or "", r.get("study_links") or ""))
            alts = {m.lower() for m in ACC.findall(links)} | {acc.lower()}
            pmids = [x.strip() for x in re.split(r"[;,\s]+", r.get("dataset_pmid") or "") if x.strip()]
            for a in alts:
                everywhere.setdefault(a, set()).update(pmids)
            for pm in pmids:
                by_pmid[pm].append({"repo": repo, "id": acc, "alts": sorted(alts)})
    return dates, by_pmid, everywhere


def gather(a, dates, by_pmid):
    window = [pm for pm, dt in dates.items() if a.date_from <= dt <= a.date_to]
    with_ds = sorted(pm for pm in window if pm in by_pmid)
    rest = sorted(set(window) - set(with_ds))
    random.Random(a.seed).shuffle(rest)
    chosen = with_ds + sorted(rest[:a.extra])
    print(f"INS papers in window: {len(window)}; with >=1 INS dataset: {len(with_ds)}; random others: "
          f"{min(a.extra, len(rest))}", flush=True)

    http = fetch.Http(str(ROOT / "data/cache"), interval=1.0)
    meta = fetch.epmc_metadata(http, chosen)
    missing = sorted(set(chosen) - set(meta))
    if missing:
        meta.update(fetch.pubmed_esummary(http, missing))
    papers = {}
    for pm in chosen:
        m = meta.get(pm, {})
        papers[pm] = {"pmid": pm, "pmcid": m.get("pmcid"), "is_oa": m.get("isOpenAccess") == "Y",
                      "title": m.get("title"), "doi": m.get("doi"), "authors": m.get("authorString") or "",
                      "first_pub_date": m.get("firstPublicationDate") or dates[pm],
                      "pub_year": m.get("pubYear") or dates[pm][:4], "has_ins_dataset": pm in by_pmid}
    todo = [p for p in papers.values() if p["pmcid"]]
    lock, done = threading.Lock(), [0]

    def work(p):
        ft = fetch.fetch_fulltext(http, p["pmcid"], p["is_oa"])
        if ft["source"]:
            doc = extract.parse_jats(ft["xml"])
            if not doc.get("error") and doc["passages"]:
                p["fulltext_source"] = ft["source"]
                p["extraction"] = extract.analyze(doc)
        with lock:
            done[0] += 1
            if done[0] % 200 == 0:
                print(f"  full text {done[0]}/{len(todo)}  http={http.stats}", flush=True)

    oa, non = [p for p in todo if p["is_oa"]], [p for p in todo if not p["is_oa"]]
    with ThreadPoolExecutor(2) as ex:
        list(ex.map(lambda lst: [work(p) for p in lst], [oa, non]))
    for p in papers.values():   # roles from the sentence rules alone, before any repository record is consulted
        if p.get("extraction"):
            for i in p["extraction"]["identifiers"]:
                i["role_text"] = i["role"]
    items = sorted({(i["repo"], i["id"]) for p in papers.values() if p.get("extraction")
                    for i in p["extraction"]["identifiers"]
                    if i["cls"] != "url" and (i["role"] in ("generated", "unknown") or crosscheck.promotable(i)
                                              or records.wants(i))})
    print(f"resolving {len(items)} identifiers", flush=True)
    resolution, _ = resolve.resolve_all(http, items, log=lambda m: print(m, flush=True))
    read_records(http, papers)
    save(a, papers, resolution)
    return papers, resolution


def read_records(http, papers):
    """Fetch the repository records the record check reads (records.py), then apply it from the cache."""
    want = sorted({(i["repo"], i["id"]) for p in papers.values() if p.get("extraction")
                   for i in p["extraction"]["identifiers"] if records.wants(i)})
    print(f"reading {len(want)} repository records", flush=True)
    records.warm(http, want, log=lambda m: print(m, flush=True))
    crosscheck.apply(papers, fetch.Http(str(ROOT / "data/cache"), offline=True))


def save(a, papers, resolution):
    WORK.parent.mkdir(parents=True, exist_ok=True)
    with open(WORK, "w", encoding="utf-8") as f:
        json.dump({"args": {"from": a.date_from, "to": a.date_to, "extra": a.extra, "seed": a.seed},
                   "papers": papers, "resolution": resolution}, f,
                  default=lambda o: sorted(o) if isinstance(o, set) else str(o))


def report(papers, resolution, by_pmid, everywhere, args):
    chk = {pm: p for pm, p in papers.items() if p.get("extraction")}
    L = ["Benchmark against INS's own paper-to-dataset records (no AI)",
         f"window {args['from']} to {args['to']}; papers: {len(papers)} "
         f"({sum(p['has_ins_dataset'] for p in papers.values())} with an INS dataset + "
         f"{sum(not p['has_ins_dataset'] for p in papers.values())} random others); "
         f"open full text: {len(chk)} ({100 * len(chk) / max(1, len(papers)):.1f}%)", ""]

    # ---- RECALL: INS (paper, dataset) pairs on checkable papers
    rec = defaultdict(Counter)
    for pm, p in chk.items():
        ids = {i["id"].lower(): i for i in p["extraction"]["identifiers"]}
        for d in by_pmid.get(pm, []):
            hit = [ids[x] for x in d["alts"] if x in ids]
            c = rec[d["repo"]]
            c["pairs"] += 1
            if not hit:
                c["not in the open full text"] += 1
                continue
            c["found in text"] += 1
            c["own, sentence rules alone"] += any(h.get("role_text") == "generated" for h in hit)
            c["own, after the record check"] += any(h["role"] == "generated" for h in hit)
            c["unclear (sent to review)"] += (not any(h["role"] == "generated" for h in hit)
                                              and any(h["role"] == "unknown" for h in hit))
            c["called reused"] += all(h["role"] == "reused" for h in hit)
    # paper level: INS often lists every sub-series NCBI links to a paper, while the paper cites one accession,
    # so "did the tool find at least one of this paper's INS datasets" is the fairer question
    pl = Counter()
    for pm, p in chk.items():
        geo = [d for d in by_pmid.get(pm, []) if d["repo"] == "GEO"]
        if not geo:
            continue
        ids = {i["id"].lower(): i for i in p["extraction"]["identifiers"]}
        hit = [ids[x] for d in geo for x in d["alts"] if x in ids]
        pl["papers"] += 1
        pl["named"] += bool(hit)
        pl["own_text"] += any(h.get("role_text") == "generated" for h in hit)
        pl["own"] += any(h["role"] == "generated" for h in hit)
    if pl["papers"]:
        n = pl["papers"]
        L += [f"RECALL BY PAPER: {n} papers with open full text and at least one GEO series in INS",
              f"  at least one of them is named in the open full text      {pl['named']:>5}  {100 * pl['named'] / n:.1f}%",
              f"  at least one called the paper's own, sentence rules alone {pl['own_text']:>5}  {100 * pl['own_text'] / n:.1f}%",
              f"  at least one called the paper's own, after the record check {pl['own']:>3}  {100 * pl['own'] / n:.1f}%", ""]
    L.append("RECALL BY DATASET: datasets INS attributes to a paper (paper has open full text)")
    js = {"args": args, "papers": len(papers), "checkable": len(chk), "recall_by_paper_geo": dict(pl), "recall": {}}
    for repo in NCBI:
        c = rec[repo]
        if not c["pairs"]:
            continue
        L.append(f"  {repo}: {c['pairs']} (paper, dataset) pairs")
        for k in ("found in text", "own, sentence rules alone", "own, after the record check",
                  "unclear (sent to review)", "called reused", "not in the open full text"):
            L.append(f"    {k:32} {c[k]:>5}  {100 * c[k] / c['pairs']:.1f}% of pairs" +
                     (f"; {100 * c[k] / c['found in text']:.1f}% of those found in text"
                      if k.startswith(("own", "unclear", "called")) and c["found in text"] else ""))
        js["recall"][repo] = dict(c)

    # ---- PRECISION SIGNAL + ADDED: what the tool calls own
    own = [(pm, i) for pm, p in chk.items() for i in p["extraction"]["identifiers"]
           if i["role"] == "generated" and i["cls"] != "url" and not i["is_code"]]
    code = [(pm, i) for pm, p in chk.items() for i in p["extraction"]["identifiers"]
            if i["role"] == "generated" and i["cls"] != "url" and i["is_code"]]
    def ins_says(pm, acc):
        where = everywhere.get(acc)
        if where is None:
            return "not in INS at all"
        if pm in where:
            return "INS attributes it to this paper (confirmed own)"
        if where:
            return "INS attributes it to OTHER papers only (likely reused)"
        return "INS lists it without naming any paper (no signal)"

    sig = defaultdict(Counter)
    added = []
    for pm, i in own:
        acc = i["id"].lower()
        st = resolution.get(f"{i['repo']}|{i['id']}", {}).get("status", "")
        if i["repo"] in NCBI:
            k = ins_says(pm, acc)
            sig[i["repo"]][k] += 1
            sig[i["repo"]]["total"] += 1
            if k == "not in INS at all":
                added.append((pm, i, st))
        else:
            added.append((pm, i, st))
    # the same table from the sentence rules alone: the record check reads the GEO-to-PubMed link that INS also
    # harvests, so only this version is independent of INS's source
    sig_text = defaultdict(Counter)
    for pm, p in chk.items():
        for i in p["extraction"]["identifiers"]:
            if i.get("role_text") == "generated" and i["cls"] != "url" and not i["is_code"] and i["repo"] in NCBI:
                sig_text[i["repo"]][ins_says(pm, i["id"].lower())] += 1
                sig_text[i["repo"]]["total"] += 1
    for title, table in (("WHAT THE TOOL CALLS OWN, checked against INS (own data deposits in GEO/SRA/dbGaP)", sig),
                         ("THE SAME, FROM THE SENTENCE RULES ALONE (before any repository record is read)", sig_text)):
        L += ["", title]
        for repo in NCBI:
            c = table[repo]
            if c["total"]:
                L.append(f"  {repo}: {c['total']}")
                for k, v in c.most_common():
                    if k != "total":
                        L.append(f"    {k:55} {v:>5}  {100 * v / c['total']:.1f}%")
    js["own_vs_ins"] = {k: dict(v) for k, v in sig.items()}
    js["own_vs_ins_sentence_rules_alone"] = {k: dict(v) for k, v in sig_text.items()}

    other = [(pm, i, st) for pm, i, st in added if i["repo"] not in NCBI]
    ncbi_new = [(pm, i, st) for pm, i, st in added if i["repo"] in NCBI]
    uniq_other = {(i["repo"], i["id"]) for _, i, _ in other}
    uniq_ncbi = {(i["repo"], i["id"]) for _, i, _ in ncbi_new}
    res_other = {(i["repo"], i["id"]) for _, i, st in other if st == "RESOLVES"}
    L += ["", "ADDED: own data deposits the tool finds for these INS papers that INS does not list",
          f"  in repositories outside GEO/SRA/dbGaP: {len(uniq_other)} datasets from "
          f"{len({pm for pm, _, _ in other})} papers ({len(res_other)} resolve)",
          f"  in GEO/SRA/dbGaP but absent from INS:   {len(uniq_ncbi)} datasets from "
          f"{len({pm for pm, _, _ in ncbi_new})} papers",
          f"  code deposits (INS resources candidates): {len({(i['repo'], i['id']) for _, i in code})}",
          "  outside GEO/SRA/dbGaP, by repository: " +
          ", ".join(f"{r} {n}" for r, n in Counter(r for r, _ in uniq_other).most_common())]
    js["added"] = {"outside_ncbi": len(uniq_other), "outside_ncbi_resolving": len(res_other),
                   "outside_ncbi_papers": len({pm for pm, _, _ in other}), "ncbi_not_in_ins": len(uniq_ncbi),
                   "code": len({(i["repo"], i["id"]) for _, i in code}),
                   "outside_ncbi_by_repo": dict(Counter(r for r, _ in uniq_other).most_common())}
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "ins_benchmark_added.tsv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["pmid", "repository", "accession", "url", "link_status", "evidence", "sentence"])
        for pm, i, st in sorted(added, key=lambda t: (t[1]["repo"], t[1]["id"], t[0])):
            if st != "RESOLVES":
                continue   # links that do not resolve are counted above, not listed
            w.writerow([pm, i["repo"], i["id"], resolve.human_url(i["repo"], i["id"]), st, i["evidence"],
                        extract.redact_access(i["context"])])
    (OUT / "ins_benchmark_report.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    (OUT / "ins_benchmark.json").write_text(json.dumps(js, indent=1), encoding="utf-8")
    print("\n".join(L))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ins-dir", required=True)
    ap.add_argument("--from", dest="date_from", default="2023-01-01")
    ap.add_argument("--to", dest="date_to", default="2025-12-31")
    ap.add_argument("--extra", type=int, default=1000, help="random INS papers without an INS dataset")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--records-only", action="store_true",
                    help="reuse the saved papers; (re)read the repository records, re-apply the record check, report")
    a = ap.parse_args()
    dates, by_pmid, everywhere = ins_tables(Path(a.ins_dir))
    if a.report_only or a.records_only:
        st = json.load(open(WORK, encoding="utf-8"))
        papers, resolution, args = st["papers"], st["resolution"], st["args"]
        if a.records_only:
            a.date_from, a.date_to, a.extra, a.seed = args["from"], args["to"], args["extra"], args["seed"]
            read_records(fetch.Http(str(ROOT / "data/cache"), interval=1.0), papers)
            save(a, papers, resolution)
        else:
            crosscheck.apply(papers, fetch.Http(str(ROOT / "data/cache"), offline=True))
    else:
        papers, resolution = gather(a, dates, by_pmid)
        args = {"from": a.date_from, "to": a.date_to, "extra": a.extra, "seed": a.seed}
    report(papers, resolution, by_pmid, everywhere, args)


if __name__ == "__main__":
    sys.exit(main())
