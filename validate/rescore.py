"""Re-score saved AI-judge audit answers against the CURRENT rule labels (no new AI calls).

  python validate/rescore.py [answers.jsonl ...]

The judge never saw the tool's label, so its answers stay valid after rule changes. Prints
per-label agreement, a stratum-weighted estimate, and which papers changed status.
Also lists papers that are NEEDS_REVIEW now but have no review label yet.
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from receipts import classify  # noqa: E402

files = sys.argv[1:] or [str(ROOT / "validate/out/answers.jsonl")]
papers = json.load(open(ROOT / "data/work/papers.json", encoding="utf-8"))
now = {pm: classify.rule_label(p)[0] for pm, p in papers.items()}
strata = Counter(l for l in now.values() if l not in ("NEEDS_REVIEW", "NOT_CHECKABLE"))
au = []
for f in files:
    au += [json.loads(l) for l in open(f, encoding="utf-8") if l.strip()]
au = [r for r in au if r["kind"] == "audit" and "label" in r]

ok_before = sum(r["label"] == r["tool"] for r in au)
ok_now = sum(r["label"] == now[r["pmid"]] for r in au)
print(f"audit papers: {len(au)}   agreement before fixes: {ok_before}   now: {ok_now}")
fixed = [r for r in au if r["label"] != r["tool"] and r["label"] == now[r["pmid"]]]
broke = [r for r in au if r["label"] == r["tool"] and r["label"] != now[r["pmid"]]]
moved = [r for r in au if r["tool"] != now[r["pmid"]] and r not in fixed and r not in broke]
print(f"fixed {len(fixed)}, broken {len(broke)}, changed but still disagree {len(moved)}")
for tag, rs in (("BROKE", broke), ("MOVED", moved)):
    for r in rs:
        print(f"  {tag} {r['pmid']} before={r['tool']} now={now[r['pmid']]} judge={r['label']}")

# stratum-weighted estimate by CURRENT label (sample was drawn by old label; this is approximate)
by = defaultdict(list)
for r in au:
    by[now[r["pmid"]]].append(r["label"] == now[r["pmid"]])
num = den = 0
print("\nper current label:")
for lab, v in sorted(by.items()):
    if lab in ("NEEDS_REVIEW", "NOT_CHECKABLE"):
        print(f"  {lab:18} {len(v)} audit papers now go to review")
        continue
    print(f"  {lab:18} {sum(v):>2}/{len(v):<2} (n={strata[lab]})")
    num += strata[lab] * sum(v) / len(v)
    den += strata[lab]
if den:
    print(f"stratum-weighted estimate: {100 * num / den:.1f}%")

ov = classify.load_overrides(str(ROOT / "data/review/llm_review_labels.csv"))
todo = [pm for pm, l in now.items() if l == "NEEDS_REVIEW" and pm not in ov]
print(f"\nNEEDS_REVIEW now: {sum(l == 'NEEDS_REVIEW' for l in now.values())}; without a review label: {len(todo)}")
(ROOT / "validate/out/unreviewed.txt").write_text("\n".join(todo), encoding="utf-8")
