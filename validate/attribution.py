"""Attribution checks: how firmly does each paper (and its datasets) belong to its FY2024 award?

  python validate/attribution.py

Uses NIH RePORTER's publication search by PMID (through the tool's cache-backed HTTP client, 1 request/second)
to count ALL awards each paper is linked to, not only the awards in this cohort. Also counts papers published
before their award's start date. Writes validate/out/attribution_report.txt.
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from receipts import fetch  # noqa: E402

papers = json.load(open(ROOT / "data/work/papers.json", encoding="utf-8"))
grants = json.load(open(ROOT / "data/work/grants.json", encoding="utf-8"))
http = fetch.Http(str(ROOT / "data/cache"), interval=1.0)

pm_list = sorted(papers)
links = {}
for i in range(0, len(pm_list), 50):
    batch = [int(x) for x in pm_list[i:i + 50]]
    offset = 0
    while True:
        r = http.request("https://api.reporter.nih.gov/v2/publications/search", method="POST",
                         json_body={"criteria": {"pmids": batch}, "offset": offset, "limit": 500})
        if not r.ok:
            print("RePORTER error", r.status, r.error)
            break
        js = r.json()
        for row in js.get("results", []):
            links.setdefault(str(row["pmid"]), set()).add(row["coreproject"])
        offset += 500
        if offset >= js.get("meta", {}).get("total", 0):
            break

n_awards = Counter(len(links.get(pm, set()) or {1}) for pm in papers)
multi = sum(1 for pm in papers if len(links.get(pm, set())) > 1)
own_data_papers = [pm for pm, p in papers.items() if p.get("extraction") and any(
    i["role"] == "generated" and not i["is_code"] and i["cls"] != "url" for i in p["extraction"]["identifiers"])]
multi_od = sum(1 for pm in own_data_papers if len(links.get(pm, set())) > 1)
before = [pm for pm, p in papers.items()
          if all((p.get("first_pub_date") or "9999") < grants[g]["start_date"] for g in p["grants"] if g in grants)]
before_od = [pm for pm in before if pm in own_data_papers]
L = ["Attribution of papers to FY2024 NCI new R01 awards",
     f"papers: {len(papers)}; RePORTER returned links for {len(links)}",
     f"papers linked to >1 award (any IC, any year): {multi} ({100 * multi / len(papers):.0f}%)",
     f"distribution of linked-award counts: {dict(sorted(n_awards.items())[:10])}",
     f"papers with own data deposits: {len(own_data_papers)}; of those linked to >1 award: {multi_od} "
     f"({100 * multi_od / max(1, len(own_data_papers)):.0f}%)",
     f"papers published before their FY2024 award started (kept: within the INS 365-day rule): {len(before)} "
     f"({100 * len(before) / len(papers):.0f}%); with own data deposits: {len(before_od)}"]
(ROOT / "validate/out/attribution_report.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
print("\n".join(L))
