"""Independent AI judge for Output Receipts labels.

A fresh Claude session (no tools, no prior context, never shown the tool's label) reads
keyword-selected passages from each paper's cached open full text and picks one label plus
flags and a confidence. Run from the repo root:

  python validate/ai_judge.py [--audit 100] [--seed 7] [--model claude-opus-5-5] [--effort medium]

Jobs:
  review  every paper the rules could not decide (NEEDS_REVIEW)
  audit   a stratified random sample of rule-labelled checkable papers; judge vs tool = accuracy
Outputs (validate/out/): answers.jsonl, audit_report.txt, llm_review_labels.proposed.csv,
ambiguous.csv (review papers the judge marked low confidence).
"""
import argparse
import csv
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from receipts import classify, extract, fetch  # noqa: E402

OUT = ROOT / "validate" / "out"
KEY = re.compile(r"availab|deposit|accession|repositor|\bGEO\b|GSE\d|\bSRA\b|SR[PRX]\d|PRJ[NE]|dbGaP|phs\d|\bEGA"
                 r"|zenodo|figshare|dryad|github|gitlab|bitbucket|code ocean|\bcode\b|software|upon request|on request"
                 r"|reasonable request|supplement|data shar|sharing|ProteomeXchange|PXD\d|\bPDB\b|synapse|dataverse"
                 r"|mendeley data|clinicaltrials|TCGA|publicly|download|obtained from|derived from|were generated",
                 re.I)
HEAD = re.compile(r"availab|data|code|accession|resource|sharing|deposit", re.I)
CAP = 14000

PROMPT = """You are checking how a cancer research paper shares its data. Below are passages selected from the paper's open full text by a broad keyword search (anything mentioning availability, deposits, accession numbers, repositories, code, supplements, requests or data sources). Passages are in document order with their section heading. If nothing about data availability appears, the paper probably has no availability statement.

Title: {title}
Article type: {atype}

PASSAGES
{passages}

Choose exactly ONE label for what the paper says about the data it produced:
{labels}

Also judge two flags:
- MIXED: the paper both produces new data and reuses existing data from others.
- CODE_SHARED: code from this paper is shared (GitHub, GitLab, Zenodo, Code Ocean, ...).

Answer with exactly these five lines and nothing else:
LABEL: <one label from the list>
MIXED: yes|no
CODE_SHARED: yes|no
CONFIDENCE: high|medium|low
REASON: <one sentence>"""


def find_claude():
    # Windows desktop app bundles a current CLI under %APPDATA%; elsewhere (Linux/macOS) use `claude` on PATH
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return "claude"
    b = sorted(Path(appdata, "Claude", "claude-code").glob("*/*/claude.exe"),
               key=lambda p: [int(x) for x in p.parent.parent.name.split(".") if x.isdigit()])
    return str(b[-1]) if b else "claude"


SYSTEM = "You are a careful research-data curator. Answer in the exact format requested."


def ask_command(prompt, cmd):
    """Model-agnostic: run any shell command that reads the prompt on stdin and prints the model's answer.
    Examples are in validate/adapters/ (Codex CLI, any OpenAI-compatible endpoint)."""
    # Run in an empty folder, so a command-line agent that can read files sees none of this repository's outputs
    # (the reviewer must stay blind to the tool's answers). The adapters are addressed by their absolute path.
    blind = Path(tempfile.gettempdir()) / "output-receipts-blind"
    blind.mkdir(exist_ok=True)
    cmd = cmd.replace("validate/adapters/", (ROOT / "validate" / "adapters").as_posix() + "/")
    p = subprocess.run(cmd, shell=True, input=SYSTEM + "\n\n" + prompt, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=blind, timeout=900)
    if p.returncode != 0 or not p.stdout.strip():
        return f"ERROR: exit {p.returncode} {p.stderr[-300:]}"
    return p.stdout.strip()


def ask_model(prompt, a):
    """--cmd if given (any model), else the Claude Code CLI with --model/--effort."""
    return ask_command(prompt, a.cmd) if getattr(a, "cmd", None) else ask_claude(prompt, a.model, a.effort)


def ask_claude(prompt, model, effort):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    args = [find_claude(), "-p", "--output-format", "json", "--model", model, "--effort", effort,
            "--system-prompt", SYSTEM,
            "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "",
            "--no-session-persistence", "--session-id", str(uuid.uuid4())]
    p = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=OUT, env=env, timeout=900)
    try:
        ev = json.loads(p.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return f"ERROR: exit {p.returncode} {p.stderr[-300:]}"
    if ev.get("is_error") or ev.get("stop_reason") == "refusal":
        return "ERROR: " + json.dumps(ev)[:300]
    return ev.get("result") or ""


def packet(http, paper, focus=None):
    """Passages of the paper's open full text that bear on data availability, in document order, capped at CAP
    characters. focus (an identifier as printed in the paper): passages that mention it are always included, and a
    long passage is cut around the mention, so the reviewer sees what the paper says about that identifier."""
    ft = fetch.fetch_fulltext(http, paper["pmcid"], paper.get("is_oa"))
    if not ft.get("xml"):
        return None
    doc = extract.parse_jats(ft["xml"])
    foc = (focus or "").lower()
    picked, seen = [], set()
    for ps in doc["passages"]:
        t = ps["text"].strip()
        has = bool(foc) and foc in t.lower()
        if ps["kind"] == "ref" and not has:
            continue
        if t in seen or not (has or KEY.search(t) or (ps["kind"] == "p" and HEAD.search(ps["sec"] or "") and
                                                      re.search(r"availab|accession|sharing|deposit", ps["sec"] or "",
                                                                re.I))):
            continue
        seen.add(t)
        if has and len(t) > 2500:
            k = max(0, t.lower().index(foc) - 1200)
            t = ("... " if k else "") + t[k:k + 2300]
        rank = 0 if has else (1 if re.search(r"availab|sharing|accession|deposit", ps["sec"] or "", re.I) else 2)
        picked.append((rank, f"[{ps['sec'] or 'body'}] {t}"))
    # if we must cut: passages that mention the identifier first, then availability-headed ones, then the rest;
    # document order is kept in the output
    out, n = [], 0
    for _, p in sorted(picked, key=lambda x: x[0]):
        p = p[:2500]
        if n + len(p) > CAP:
            break
        out.append(p)
        n += len(p)
    order = {p[:2500]: i for i, (_, p) in enumerate(picked)}
    out.sort(key=lambda p: order.get(p, 0))
    return "\n\n".join(out) if out else "(no passages matched)"


def parse(ans):
    g = lambda k: (re.search(rf"^{k}\s*:\s*(.+)$", ans, re.M | re.I) or [None, ""])[1].strip()
    label = g("LABEL").upper().replace(" ", "_")
    return {"label": label if label in classify.LABELS else "UNPARSED", "mixed": g("MIXED").lower().startswith("y"),
            "code": g("CODE_SHARED").lower().startswith("y"), "conf": g("CONFIDENCE").lower() or "?",
            "reason": g("REASON")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", type=int, default=100)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="smoke test: only N jobs")
    ap.add_argument("--skip-review", action="store_true", help="audit only")
    ap.add_argument("--exclude", nargs="*", default=[], help="answers.jsonl files whose audited papers to skip")
    ap.add_argument("--tag", default="", help="suffix for output file names, e.g. _fresh")
    ap.add_argument("--cmd", help="use any model: a shell command that reads the prompt on stdin and prints the "
                                  "answer (see validate/adapters/); overrides --model/--effort")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    papers = json.load(open(ROOT / "data/work/papers.json", encoding="utf-8"))
    tool = {pm: classify.rule_label(p) for pm, p in papers.items()}
    review = [pm for pm, (lab, _) in tool.items() if lab == "NEEDS_REVIEW"]
    done = set()
    for f in a.exclude:
        done |= {json.loads(l)["pmid"] for l in open(f, encoding="utf-8") if l.strip()}
    strata, full = defaultdict(list), Counter()
    for pm, (lab, _) in tool.items():
        if lab not in ("NEEDS_REVIEW", "NOT_CHECKABLE"):
            full[lab] += 1
            if pm not in done:
                strata[lab].append(pm)
    rng = random.Random(a.seed)
    quota = {"PUBLIC_REPOSITORY": 25, "NO_STATEMENT": 20, "ON_REQUEST": 15, "NO_DATA_GENERATED": 10,
             "IN_ARTICLE": 10, "REUSED_ONLY": 10, "CONTROLLED_ACCESS": 10}
    scale = a.audit / sum(quota.values())
    audit = []
    for lab, pms in sorted(strata.items()):
        audit += rng.sample(sorted(pms), min(len(pms), round(quota.get(lab, 5) * scale)))
    jobs = ([] if a.skip_review else [("review", pm) for pm in review]) + [("audit", pm) for pm in audit]
    if a.limit:
        jobs = jobs[:a.limit // 2] + [j for j in jobs if j[0] == "audit"][:a.limit - a.limit // 2]

    http = fetch.Http(str(ROOT / "data/cache"), offline=True)
    labels_txt = "\n".join(f"- {k}: {v}" for k, v in classify.LABELS.items() if k != "NOT_CHECKABLE")

    def run(job):
        kind, pm = job
        p = papers[pm]
        pk = packet(http, p)
        if pk is None:
            return {"kind": kind, "pmid": pm, "tool": tool[pm][0], "error": "no cached full text"}
        ans = ask_model(PROMPT.format(title=p.get("title", ""), atype=p.get("article_type") or p.get("pub_type"),
                                      passages=pk, labels=labels_txt), a)
        r = {"kind": kind, "pmid": pm, "tool": tool[pm][0], "tool_reason": tool[pm][1], "packet_chars": len(pk)}
        r.update(parse(ans) if not ans.startswith("ERROR") else {"error": ans})
        r["raw"] = ans
        return r

    with ThreadPoolExecutor(a.workers) as pool:
        res = list(pool.map(run, jobs))
    with open(OUT / f"answers{a.tag}.jsonl", "w", encoding="utf-8") as f:
        for r in res:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---- audit report
    au = [r for r in res if r["kind"] == "audit" and "label" in r]
    who = f"command: {a.cmd}" if a.cmd else f"model {a.model}, effort {a.effort}"
    L = ["AI-judge audit (fresh model session, blind to the tool's label)", f"{who}, seed {a.seed}",
         f"audited {len(au)} rule-labelled papers; errors {sum('error' in r for r in res)}", ""]
    agree = sum(r["label"] == r["tool"] for r in au)
    L.append(f"raw agreement: {agree}/{len(au)} = {100 * agree / max(1, len(au)):.1f}%")
    w_num = w_den = 0
    L.append("\nper tool label (agreement; weight = papers with that label in the full run):")
    for lab in classify.LABELS:
        rs = [r for r in au if r["tool"] == lab]
        if not rs:
            continue
        k = sum(r["label"] == lab for r in rs)
        n_full = full[lab]
        w_num += n_full * k / len(rs)
        w_den += n_full
        miss = Counter(r["label"] for r in rs if r["label"] != lab)
        L.append(f"  {lab:18} {k:>2}/{len(rs):<2} (n={n_full:>4})  judge said instead: {dict(miss) or '-'}")
    if w_den:
        L.append(f"\nestimated accuracy over all rule-labelled checkable papers (stratum-weighted): {100 * w_num / w_den:.1f}%")
    hi = [r for r in au if r["conf"] == "high"]
    if hi:
        L.append(f"agreement where the judge was high-confidence: {sum(r['label'] == r['tool'] for r in hi)}/{len(hi)}")
    L.append("\ndisagreements:")
    for r in au:
        if r["label"] != r["tool"]:
            L.append(f"  {r['pmid']} tool={r['tool']} judge={r['label']} ({r['conf']}): {r['reason'][:160]}")
    (OUT / f"audit_report{a.tag}.txt").write_text("\n".join(L) + "\n", encoding="utf-8")

    # ---- review labels
    rv = [r for r in res if r["kind"] == "review" and "label" in r]
    if not rv:
        print("\n".join(L[:12]))
        return
    with open(OUT / "llm_review_labels.proposed.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pmid", "label", "mixed", "code_shared", "note"])
        for r in rv:
            if r["conf"] in ("high", "medium") and r["label"] in classify.LABELS:
                w.writerow([r["pmid"], r["label"], "yes" if r["mixed"] else "no", "yes" if r["code"] else "no",
                            f"AI judge ({r['conf']}): {r['reason']}"])
    with open(OUT / "ambiguous.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pmid", "title", "europepmc_link", "rule_reason", "judge_label", "judge_reason"])
        for r in rv:
            if not (r["conf"] in ("high", "medium") and r["label"] in classify.LABELS):
                p = papers[r["pmid"]]
                w.writerow([r["pmid"], p.get("title", ""), f"https://europepmc.org/article/MED/{r['pmid']}",
                            r["tool_reason"], r["label"], r["reason"]])
    print("\n".join(L[:12]))
    print(f"review: {len(rv)} judged; confidence {dict(Counter(r['conf'] for r in rv))}")


if __name__ == "__main__":
    main()
