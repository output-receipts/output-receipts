"""Numbers for the essay's scale and curation estimates.

  python validate/scale.py

- awards with at least one own dataset / code deposit
- unit check: candidate rows are unique (repository, accession) datasets
- triage: how many candidate rows not in INS could be bulk-accepted (resolves + own-deposit cue inside the
  availability statement) vs. queued for a curator
- NCI's yearly volume of new awards (all activity codes), from NIH RePORTER, for a portfolio-scale estimate
Writes validate/out/scale_report.txt.
"""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from receipts import fetch  # noqa: E402

papers = json.load(open(ROOT / "data/work/papers.json", encoding="utf-8"))
grants = json.load(open(ROOT / "data/work/grants.json", encoding="utf-8"))
rows = list(csv.DictReader(open(ROOT / "data/output/ins_candidate_datasets.tsv", encoding="utf-8"), delimiter="\t"))
missing = list(csv.DictReader(open(ROOT / "validate/out/ins_missing_datasets.tsv", encoding="utf-8"), delimiter="\t"))

aw_data = {g for r in rows if r["type"] == "dataset" for g in r["funding_source"].split(";") if g in grants}
aw_any = {g for r in rows for g in r["funding_source"].split(";") if g in grants}
uniq = {(r["dataset_source_repo"], r["dataset_source_id"]) for r in rows}


# triage_tier is written by receipts/report.py with each candidate row:
# 1 = the link works and the repository's own record confirms the deposit; 2 = the link works and the availability
# statement states the deposit explicitly (paper only); 3 = everything else (curator looks first)
miss_data = [r for r in missing if r["type"] == "dataset"]
tier1 = [r for r in miss_data if r["triage_tier"] == "1"]
tier2 = [r for r in miss_data if r["triage_tier"] == "2"]
bulk = tier1 + tier2

http = fetch.Http(str(ROOT / "data/cache"), interval=1.0)
vol = {}
for fy in (2023, 2024, 2025):
    r = http.request("https://api.reporter.nih.gov/v2/projects/search", method="POST", json_body={
        "criteria": {"fiscal_years": [fy], "agencies": ["NCI"], "award_types": ["1"], "exclude_subprojects": True},
        "limit": 1})
    vol[fy] = r.json().get("meta", {}).get("total") if r.ok else f"error {r.status}"

per_award = len(miss_data) / len(grants)
L = ["Scale and curation numbers",
     f"candidate rows: {len(rows)} = unique (repository, accession) pairs: {len(uniq)}",
     f"awards (of {len(grants)}) with >=1 own dataset: {len(aw_data)}; with >=1 own dataset or code deposit: {len(aw_any)}",
     f"datasets not in INS: {len(miss_data)}",
     f"  tier 1, resolves and confirmed by the repository's own record: {len(tier1)} "
     f"({100 * len(tier1) / len(miss_data):.0f}%)",
     f"  tier 2, resolves and an explicit own-deposit sentence in the availability statement (paper only): "
     f"{len(tier2)} ({100 * len(tier2) / len(miss_data):.0f}%)",
     f"  tier 3, everything else (curator looks first): {len(miss_data) - len(bulk)} "
     f"({100 * (len(miss_data) - len(bulk)) / len(miss_data):.0f}%)",
     f"NCI new (type 1) awards, all activity codes, subprojects excluded, per fiscal year (RePORTER): {vol}",
     f"not-in-INS datasets per FY2024 R01 award: {per_award:.2f}",
     "(portfolio extrapolation is rough: other activity codes publish at different rates)"]
(ROOT / "validate/out/scale_report.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
print("\n".join(L))
