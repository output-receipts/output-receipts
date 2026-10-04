"""Compare two blind reviewers on the same deposit-audit items (e.g. Claude vs GPT).

  python validate/compare_reviewers.py validate/out/id_audit.jsonl validate/out/id_audit_gpt.jsonl [TAG]

Reports each reviewer's OWN rate, item-by-item agreement, Cohen's kappa, the consensus result (items both call OWN),
and every disagreement; for a tier-stratified audit also the consensus per triage tier, weighted to the population
(TIER_POP below is read from validate/out/ins_missing_datasets.tsv). Writes validate/out/reviewer_agreement[TAG].txt.
"""
import json
import sys
from pathlib import Path


def load(path):
    return {(r["pmid"], r["id"]): r for r in (json.loads(l) for l in open(path, encoding="utf-8") if l.strip())
            if "role" in r}


a_path, b_path = sys.argv[1], sys.argv[2]
A, B = load(a_path), load(b_path)
keys = sorted(set(A) & set(B))
own = lambda r: r["role"] == "OWN"
na, nb = sum(own(A[k]) for k in keys), sum(own(B[k]) for k in keys)
agree = sum(own(A[k]) == own(B[k]) for k in keys)
both = sum(own(A[k]) and own(B[k]) for k in keys)
n = len(keys)
po = agree / n
pa, pb = na / n, nb / n
pe = pa * pb + (1 - pa) * (1 - pb)
kappa = (po - pe) / (1 - pe) if pe < 1 else float("nan")
L = [f"items judged by both: {n}",
     f"{Path(a_path).name}: OWN {na}/{n} ({100 * pa:.0f}%)",
     f"{Path(b_path).name}: OWN {nb}/{n} ({100 * pb:.0f}%)",
     f"agreement: {agree}/{n} ({100 * po:.0f}%); Cohen's kappa {kappa:.2f}",
     f"both reviewers say OWN: {both}/{n} ({100 * both / n:.0f}%)"]
if any(A[k].get("tier") for k in keys):
    import csv
    from collections import Counter
    tsv = Path(a_path).parent / "ins_missing_datasets.tsv"
    pop = Counter(r["triage_tier"] for r in csv.DictReader(open(tsv, encoding="utf-8"), delimiter=chr(9))
                  if r["type"] == "dataset")
    L.append("by triage tier (both reviewers say OWN / judged; at least one says OWN):")
    w = w_any = tot = 0
    for t in ("1", "2", "3"):
        ks = [k for k in keys if A[k].get("tier") == t]
        if ks:
            b = sum(own(A[k]) and own(B[k]) for k in ks)
            e = sum(own(A[k]) or own(B[k]) for k in ks)
            L.append(f"  tier {t}: {b}/{len(ks)} = {100 * b / len(ks):.0f}%; {e}/{len(ks)} = {100 * e / len(ks):.0f}%"
                     f"   (tier population {pop[t]} rows)")
            w, w_any, tot = w + pop[t] * b / len(ks), w_any + pop[t] * e / len(ks), tot + pop[t]
    if tot:
        L.append(f"  weighted to the population of {tot} rows: both {100 * w / tot:.1f}%; at least one "
                 f"{100 * w_any / tot:.1f}%")
L += ["", "disagreements:"]
for k in keys:
    if own(A[k]) != own(B[k]):
        L.append(f"  {k[0]} {k[1]}: A={A[k]['role']} B={B[k]['role']}")
        L.append(f"     A: {A[k]['reason'][:150]}")
        L.append(f"     B: {B[k]['reason'][:150]}")
(Path(a_path).parent / f"reviewer_agreement{sys.argv[3] if len(sys.argv) > 3 else ''}.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
print("\n".join(L))
