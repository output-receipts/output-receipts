"""Compare this run against what the Index of NCI Studies (INS) actually lists.

  python validate/ins_compare.py --ins-dir PATH

PATH holds tables from NCI's public INS-Data repository (github.com/CBIIT/INS-Data, data/02_output):
grant.tsv, publication.tsv, geo_datasets.tsv, sra_datasets_curated_clean.tsv,
dbgap_datasets_merged_curated_clean.tsv, resources_*_processed.tsv. They are not redistributed here.

Reports: how many of this run's awards, papers and own deposits INS already lists, and writes
validate/out/ins_missing_datasets.tsv: the detected own deposits INS does not list.
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
csv.field_size_limit(10 ** 9)


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f, delimiter="\t")


def norm_url(u):
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://(www\.)?", "", u).rstrip("/")
    return re.sub(r"\.git$", "", u)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ins-dir", required=True)
    a = ap.parse_args()
    d = Path(a.ins_dir)

    grants = json.load(open(ROOT / "data/work/grants.json", encoding="utf-8"))
    papers = json.load(open(ROOT / "data/work/papers.json", encoding="utf-8"))
    ours_core = set(grants)

    ins_core = {r["project.project_id"].strip() for r in read(d / "grant.tsv")}
    ins_pub_pairs, ins_pmids = set(), set()
    for r in read(d / "publication.tsv"):
        ins_pmids.add(r["pmid"].strip())
        ins_pub_pairs.add((r["pmid"].strip(), r["project.project_id"].strip()))

    ins_acc, blob_parts, ins_acc_pmids = {}, [], {}
    for f in ("geo_datasets.tsv", "sra_datasets_curated_clean.tsv", "dbgap_datasets_merged_curated_clean.tsv"):
        for r in read(d / f):
            ins_acc[r["dataset_source_id"].strip().lower()] = r["dataset_source_repo"]
            # the papers INS attributes this dataset to (also under the accessions its link columns name)
            pm = {x.strip() for x in re.split(r"[;,\s]+", r.get("dataset_pmid") or "") if x.strip()}
            links = " ".join((r.get("dataset_source_url") or "", r.get("study_links") or ""))
            for acc in {r["dataset_source_id"].strip().lower()} | {
                    m.lower() for m in re.findall(r"\b(GSE\d+|PRJ[A-Z]{2}\d+|[SED]RP\d+|phs\d{6})\b", links, re.I)}:
                ins_acc_pmids.setdefault(acc, set()).update(pm)
            blob_parts.append(" ".join((r.get("dataset_source_url") or "", r.get("study_links") or "")).lower())
    # INS SRA rows are keyed by study (SRP/ERP) but often link the BioProject; search the link columns too
    ins_blob = " ".join(blob_parts)
    res_files = sorted(d.glob("resources_*_processed.tsv"))
    ins_res_urls = {norm_url(r["resource_source_url"]) for f in res_files for r in read(f)}

    # ---- awards and papers
    g_in = ours_core & ins_core
    our_pairs = {(pm, g) for pm, p in papers.items() for g in p["grants"]}
    pairs_in = {pr for pr in our_pairs if pr in ins_pub_pairs}
    pm_in = {pm for pm in papers if pm in ins_pmids}
    # INS-Data release folder 2026-01-30/gathered-2026-02-17 (its dbGaP table is dated 2026-03-09): papers published
    # after the gather date could not be listed yet
    snap = "2026-02-17"
    pre = {pm for pm, p in papers.items() if (p.get("first_pub_date") or "9999") < snap}
    pre_pairs = {(pm, g) for pm in pre for g in papers[pm]["grants"]}

    # ---- own deposits (same set as the INS-candidate file)
    rows = list(read(ROOT / "data/output/ins_candidate_datasets.tsv"))
    missing, found = [], Counter()
    # among dataset rows INS lists: does INS name one of the same papers, only other papers, or no paper at all?
    same_paper, other_papers, no_paper = [], [], []
    for r in rows:
        acc = r["dataset_source_id"].strip().lower()
        repo = r["dataset_source_repo"]
        hit = acc in ins_acc
        if not hit and repo in ("SRA/BioProject", "GEO", "dbGaP"):
            hit = re.search(rf"\b{re.escape(acc)}\b", ins_blob) is not None
        if not hit:
            hit = norm_url(r["dataset_source_url"]) in ins_res_urls or norm_url(acc) in ins_res_urls
        found[(r["type"], repo, hit)] += 1
        if not hit:
            missing.append(r)
        elif r["type"] == "dataset":
            ins_pm = ins_acc_pmids.get(acc, set())
            (same_paper if set(r["dataset_pmid"].split(";")) & ins_pm else other_papers if ins_pm else no_paper).append(r)

    ncbi = {"GEO", "SRA/BioProject", "dbGaP"}
    data_rows = [r for r in rows if r["type"] == "dataset"]
    data_missing = [r for r in missing if r["type"] == "dataset"]
    L = ["Comparison with the Index of NCI Studies (INS-Data public tables)",
         f"INS tables: {len(ins_core)} core projects, {len(ins_pmids)} PMIDs, {len(ins_acc)} GEO/SRA/dbGaP datasets, "
         f"{len(ins_res_urls)} resources", "",
         f"FY2024 NCI new R01 awards in this run: {len(ours_core)}; listed in INS: {len(g_in)}",
         f"papers in this run: {len(papers)}; PMID listed anywhere in INS: {len(pm_in)}; "
         f"(paper, award) pairs listed in INS: {len(pairs_in)} of {len(our_pairs)}",
         f"  published before the INS snapshot ({snap}): {len(pre)} papers; their (paper, award) pairs listed in "
         f"INS: {len(pre_pairs & ins_pub_pairs)} of {len(pre_pairs)}", "",
         f"own deposits detected: {len(rows)} ({len(data_rows)} datasets, {len(rows) - len(data_rows)} code/software)",
         f"  listed in INS: {len(rows) - len(missing)}",
         f"    datasets INS attributes to the same paper (INS and the tool agree): {len(same_paper)}",
         f"    datasets INS attributes only to other papers (likely reused data the wording rules misjudged): "
         f"{len(other_papers)}  " + str(dict(Counter(r['dataset_source_repo'] for r in other_papers))),
         f"    datasets INS lists without naming any paper (INS neither confirms nor contradicts the tool): "
         f"{len(no_paper)}  " + str(dict(Counter(r['dataset_source_repo'] for r in no_paper))),
         f"  NOT listed in INS: {len(missing)}  (datasets: {len(data_missing)}; of those in GEO/SRA/dbGaP: "
         f"{sum(r['dataset_source_repo'] in ncbi for r in data_missing)}, in other repositories: "
         f"{sum(r['dataset_source_repo'] not in ncbi for r in data_missing)})",
         f"  resolvable among missing datasets: {sum(r['link_status'] == 'RESOLVES' for r in data_missing)}", "",
         "by repository (listed in INS / detected):"]
    for repo, n in Counter(r["dataset_source_repo"] for r in rows).most_common():
        hit = sum(v for (t, rp, h), v in found.items() if rp == repo and h)
        L.append(f"  {repo:24} {hit:>4} / {n}")
    out = ROOT / "validate/out"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "ins_missing_datasets.tsv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, list(rows[0].keys()), delimiter="\t")
        w.writeheader()
        w.writerows(missing)
    (out / "ins_compare_report.txt").write_text("\n".join(L) + "\n", encoding="utf-8")

    # machine-readable summary (used by site/build.py for the landing page)
    by_repo = {}
    for r in data_rows:
        d = by_repo.setdefault(r["dataset_source_repo"], {"detected": 0, "in_ins": 0})
        d["detected"] += 1
    for r in data_rows:
        if r not in missing:
            by_repo[r["dataset_source_repo"]]["in_ins"] += 1
    js = {"ins_snapshot": snap, "awards": len(ours_core), "awards_in_ins": len(g_in),
          "pairs_pre_snapshot": len(pre_pairs), "pairs_pre_snapshot_in_ins": len(pre_pairs & ins_pub_pairs),
          "own_datasets": len(data_rows), "own_datasets_in_ins": len(data_rows) - len(data_missing),
          "own_datasets_not_in_ins": len(data_missing),
          "own_datasets_in_ins_same_paper": len(same_paper), "own_datasets_in_ins_other_papers_only": len(other_papers),
          "in_ins_other_papers_only_by_repository": dict(Counter(r["dataset_source_repo"] for r in other_papers)),
          "own_datasets_in_ins_no_paper_named": len(no_paper),
          "in_ins_no_paper_named_by_repository": dict(Counter(r["dataset_source_repo"] for r in no_paper)),
          "not_in_ins_resolving": sum(r["link_status"] == "RESOLVES" for r in data_missing),
          "not_in_ins_outside_ncbi": sum(r["dataset_source_repo"] not in ncbi for r in data_missing),
          "code_deposits": len(rows) - len(data_rows),
          "datasets_by_repository": dict(sorted(by_repo.items(), key=lambda kv: -kv[1]["detected"]))}
    (out / "ins_compare.json").write_text(json.dumps(js, indent=1), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    sys.exit(main())
