"""Blind audit of the headline claim: deposits OUTSIDE GEO/SRA/dbGaP that the tool says are the paper's OWN new data.

  python validate/id_audit.py [--n 60] [--seed 11] [--model claude-opus-5-5] [--effort medium]

Population = (paper, identifier) pairs where the tool marked the identifier as the paper's own new data
(role generated, not code, not a bare URL) and the repository is not GEO, SRA/BioProject or dbGaP. These are
the outputs the Index of NCI Studies pipeline does not harvest. For a random sample, a fresh model session
reads the paper's availability passages and is asked about ONE identifier, without being told what the tool
concluded. Writes validate/out/id_audit.jsonl and validate/out/id_audit_report.txt.
"""
import argparse
import json
import random
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "validate"))
from receipts import fetch  # noqa: E402
from ai_judge import OUT, ask_model, packet  # noqa: E402

NCBI = {"GEO", "SRA/BioProject", "dbGaP"}
PROMPT = """You are checking one dataset identifier cited in a cancer research paper. Below are passages selected from the paper's open full text by a broad keyword search (availability, deposits, accession numbers, repositories, code, supplements, data sources), in document order with their section heading.

Title: {title}

PASSAGES
{passages}

IDENTIFIER TO CHECK: {ident}  (repository: {repo})

Questions:
1. ROLE. Is this identifier an output of THIS paper, meaning the authors deposited it as part of this study (OWN)? Or is it data created by others or published earlier that the paper reused or cited (REUSED)? Or is it not a dataset deposit at all, for example a misread string, a reference or a tool (NOT_A_DEPOSIT)? Use UNCLEAR only if the passages genuinely do not say.
2. KIND. Does the deposit hold DATA, CODE or OTHER (e.g., a protocol or model only)?

Answer with exactly these four lines and nothing else:
ROLE: OWN|REUSED|NOT_A_DEPOSIT|UNCLEAR
KIND: DATA|CODE|OTHER
CONFIDENCE: high|medium|low
REASON: <one sentence>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--cmd", help="any model: shell command reading the prompt on stdin (see validate/adapters/)")
    ap.add_argument("--tag", default="", help="suffix for output files, e.g. _gpt")
    ap.add_argument("--sample-from", help="re-judge exactly the items in an earlier id_audit*.jsonl")
    ap.add_argument("--keep-from", help="an earlier answers file from the SAME reviewer: reuse its answer for items "
                                        "whose identifier that reviewer was already shown; re-judge the others")
    ap.add_argument("--population", help="draw from the dataset rows of a candidate TSV instead (e.g. "
                                         "validate/out/ins_missing_datasets.tsv); one (first) paper per dataset")
    ap.add_argument("--strata", help="with --population: per-stratum sample sizes, e.g. ncbi=60,other=40 "
                                     "(ncbi = GEO, SRA/BioProject, dbGaP)")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    papers = json.load(open(ROOT / "data/work/papers.json", encoding="utf-8"))
    rng = random.Random(a.seed)
    pop, tier = [], {}
    rank = {"cites_paper": 3, "names_award": 2, "authors": 1}
    if a.population:
        import csv
        for r in csv.DictReader(open(a.population, encoding="utf-8"), delimiter="\t"):
            if r["type"] == "dataset":
                # one paper per dataset: the one whose record evidence gave the row its tier (first PMID on ties)
                best = None
                for pm in sorted(r["dataset_pmid"].split(";")):
                    for i in (papers.get(pm, {}).get("extraction") or {}).get("identifiers", []):
                        if (i["repo"], i["id"], i["role"]) == (r["dataset_source_repo"], r["dataset_source_id"],
                                                               "generated"):
                            k = rank.get(i.get("record_check", ""), 0)
                            if best is None or k > best[0]:
                                best = (k, pm)
                if best:
                    key = (best[1], r["dataset_source_repo"], r["dataset_source_id"])
                    pop.append(key)
                    tier[key] = r.get("triage_tier", "")
    else:
        for pm, p in papers.items():
            if not p.get("fulltext_source"):
                continue
            for i in p["extraction"]["identifiers"]:
                if i["role"] == "generated" and not i["is_code"] and i["cls"] != "url" and i["repo"] not in NCBI:
                    pop.append((pm, i["repo"], i["id"]))
    pop = sorted(set(pop))
    if a.sample_from:  # same items as an earlier run, so two reviewers can be compared item by item
        sample = [(r["pmid"], r["repo"], r["id"]) for r in
                  (json.loads(l) for l in open(a.sample_from, encoding="utf-8") if l.strip())]
    elif a.strata:
        want = dict((k, int(v)) for k, v in (s.split("=") for s in a.strata.split(",")))
        groups = {"ncbi": [x for x in pop if x[1] in NCBI], "other": [x for x in pop if x[1] not in NCBI],
                  "tier1": [x for x in pop if tier.get(x) == "1"], "tier2": [x for x in pop if tier.get(x) == "2"],
                  "tier3": [x for x in pop if tier.get(x) == "3"]}
        sample = [x for g, n in want.items() for x in rng.sample(groups[g], min(n, len(groups[g])))]
        print("strata: " + ", ".join(f"{g} population {len(groups[g])}, sampled {min(n, len(groups[g]))}"
                                     for g, n in want.items()))
    else:
        sample = rng.sample(pop, min(a.n, len(pop)))
    http = fetch.Http(str(ROOT / "data/cache"), offline=True)
    keep = {}
    if a.keep_from:
        keep = {(r["pmid"], r["repo"], r["id"]): r for r in
                (json.loads(l) for l in open(a.keep_from, encoding="utf-8") if l.strip()) if r.get("role")}

    def run(job):
        pm, repo, ident = job
        p = papers[pm]
        # the identifier as printed in the paper: the passages that mention it are always shown to the reviewer
        raw = next((i.get("raw") or ident for i in p["extraction"]["identifiers"]
                    if (i["repo"], i["id"]) == (repo, ident)), ident)
        pk = packet(http, p, focus=raw)
        if pk is None:
            return {"pmid": pm, "repo": repo, "id": ident, "error": "no cached full text"}
        if job in keep and raw.lower() in (packet(http, p) or "").lower():
            return dict(keep[job], tier=tier.get(job, ""), kept=True)   # that reviewer already saw the mention
        ans = ask_model(PROMPT.format(title=p.get("title", ""), passages=pk, ident=ident, repo=repo), a)
        g = lambda k: (re.search(rf"^{k}\s*:\s*(.+)$", ans, re.M | re.I) or [None, ""])[1].strip()
        return {"pmid": pm, "repo": repo, "id": ident, "tier": tier.get(job, ""), "role": g("ROLE").upper(),
                "kind": g("KIND").upper(),
                "conf": g("CONFIDENCE").lower(), "reason": g("REASON"), "raw": ans}

    with ThreadPoolExecutor(a.workers) as pool:
        res = list(pool.map(run, sample))
    with open(OUT / f"id_audit{a.tag}.jsonl", "w", encoding="utf-8") as f:
        for r in res:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    ok = [r for r in res if "role" in r]
    own = sum(r["role"] == "OWN" for r in ok)
    own_data = sum(r["role"] == "OWN" and r["kind"] == "DATA" for r in ok)
    L = ["Blind audit of own new-data deposits: " + (f"dataset rows of {a.population}" if a.population else
                                                     "OUTSIDE GEO/SRA/dbGaP (the INS blind spot)"),
         f"population {len(pop)} (paper, identifier) pairs; sample {len(sample)}; seed {a.seed}; "
         + (f"command: {a.cmd}" if a.cmd else f"model {a.model}"),
         f"judged {len(ok)}; errors {len(res) - len(ok)}", "",
         f"reviewer says OWN: {own}/{len(ok)} = {100 * own / max(1, len(ok)):.0f}%",
         f"reviewer says OWN and DATA: {own_data}/{len(ok)} = {100 * own_data / max(1, len(ok)):.0f}%",
         f"roles: {dict(Counter(r['role'] for r in ok))}", f"kinds: {dict(Counter(r['kind'] for r in ok))}",
         f"by repository (own/total): " + ", ".join(
             f"{rp} {sum(r['role'] == 'OWN' for r in ok if r['repo'] == rp)}/{sum(r['repo'] == rp for r in ok)}"
             for rp, _ in Counter(r["repo"] for r in ok).most_common())]
    if a.population and any(r.get("tier") for r in ok):
        # per triage tier, and weighted to the population (each tier's rate times its share of the rows)
        L.append("by triage tier (reviewer says OWN / judged):")
        w = tot = 0
        for t in ("1", "2", "3"):
            rs = [r for r in ok if r.get("tier") == t]
            n_pop = sum(1 for x in pop if tier.get(x) == t)
            if rs:
                k = sum(r["role"] == "OWN" for r in rs)
                L.append(f"  tier {t}: {k}/{len(rs)} = {100 * k / len(rs):.0f}%   (tier population {n_pop} rows)")
                w, tot = w + n_pop * k / len(rs), tot + n_pop
        if tot:
            L.append(f"  weighted to the population of {tot} rows: {100 * w / tot:.1f}% OWN")
    L += ["", "not OWN:"]
    for r in ok:
        if r["role"] != "OWN":
            L.append(f"  {r['pmid']} {r['repo']}:{r['id']} -> {r['role']}/{r['kind']} ({r['conf']}): {r['reason'][:150]}")
    (OUT / f"id_audit_report{a.tag}.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
