"""Build a one-question-per-item human check for the identifier audit sample.

  python validate/human_check.py            -> validate/out/human_check.html (open in a browser)
  python validate/human_check.py --score FILE -> compare pasted human answers with the AI reviewer

For each (paper, identifier) in validate/out/id_audit.jsonl, shows the sentence(s) of the open full text that
contain the identifier, plus a link to the paper, and asks one question: does the text say the authors
deposited this dataset as part of this study? The reviewer is not shown the tool's or the AI's answer.
Answers stay in the browser and are copied out with one button (paste them into a text file for --score).
"""
import argparse
import html
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from receipts import extract, fetch  # noqa: E402

OUT = ROOT / "validate/out"


def sentences_with(passages, ident):
    # the tool stores some IDs canonically (10.5281/zenodo.123, 10.17632/abc); papers may write the record URL
    tail = re.split(r"[./]", ident)[-1]
    alts = [re.escape(ident)] + ([re.escape(tail)] if len(tail) >= 6 and tail != ident else [])
    pat = re.compile("|".join(alts), re.I)
    hits = []
    for ps in passages:
        if ps["kind"] == "ref" or not pat.search(ps["text"]):
            continue
        for _, s in extract.sentences(ps["text"]):
            if pat.search(s):
                hits.append((ps["sec"] or "body", s.strip()))
    seen, out = set(), []
    for sec, s in hits:
        if s not in seen:
            seen.add(s)
            out.append((sec, s))
    return out[:4]


def load_rows(source, n, seed):
    """Items to review: the audit sample (default) or a random sample of candidate rows from a TSV."""
    if not source:
        rows = [json.loads(l) for l in open(OUT / "id_audit.jsonl", encoding="utf-8") if l.strip()]
    else:
        import csv
        rows = [{"pmid": r["dataset_pmid"].split(";")[0], "repo": r["dataset_source_repo"], "id": r["dataset_source_id"]}
                for r in csv.DictReader(open(source, encoding="utf-8"), delimiter="\t") if r["type"] == "dataset"]
    random.Random(seed).shuffle(rows)
    return rows[:n] if n else rows


def build(source=None, n=0, seed=5, out_name="human_check.html"):
    rows = load_rows(source, n, seed)
    papers = json.load(open(ROOT / "data/work/papers.json", encoding="utf-8"))
    http = fetch.Http(str(ROOT / "data/cache"), offline=True)
    items = []
    for n, r in enumerate(rows, 1):
        p = papers[r["pmid"]]
        ft = fetch.fetch_fulltext(http, p["pmcid"], p.get("is_oa"))
        doc = extract.parse_jats(ft["xml"]) if ft.get("xml") else {"passages": []}
        sents = sentences_with(doc["passages"], r["id"])
        body = "".join(
            f"<p><span class=sec>{html.escape(sec[:60])}</span> "
            + re.sub("|".join(re.escape(html.escape(x)) for x in {r["id"], re.split(r"[./]", r["id"])[-1]} if len(x) >= 4),
                     lambda m: f"<mark>{m.group(0)}</mark>", html.escape(s), flags=re.I)
            + "</p>" for sec, s in sents) or "<p><i>(identifier not found verbatim; open the paper)</i></p>"
        items.append(f"""<div class=item id=i{n}><h3>{n}. <code>{html.escape(r['id'])}</code> ({html.escape(r['repo'])})</h3>
<div class=title>{html.escape(p.get('title', ''))} &middot; <a href="https://europepmc.org/article/MED/{r['pmid']}" target=_blank>open paper</a></div>
{body}
<div class=btns data-key="{r['pmid']}|{html.escape(r['id'])}">
<button data-v=OWN>Yes, deposited by these authors for this study</button>
<button data-v=REUSED>No, someone else's / earlier data</button>
<button data-v=UNSURE>Can't tell</button></div></div>""")
    page = f"""<!doctype html><html lang=en><head><meta charset=utf-8><title>Human check: own deposits</title>
<style>
:root{{--bg:#fff;--ink:#1b1f24;--muted:#666;--acc:#0b5cad;--card:#f6f8fa}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#111;--ink:#e6e6e6;--muted:#aaa;--acc:#6aa9ff;--card:#1c1f24}}}}
body{{font-family:Segoe UI,Arial,sans-serif;max-width:900px;margin:0 auto;padding:16px;background:var(--bg);color:var(--ink);line-height:1.4}}
.item{{background:var(--card);border-radius:8px;padding:10px 14px;margin:12px 0}} h3{{margin:0 0 4px;font-size:16px}}
.title{{color:var(--muted);font-size:13px;margin-bottom:6px}} .sec{{color:var(--muted);font-size:12px;margin-right:6px}}
mark{{background:#ffe08a;color:#000}} a{{color:var(--acc)}} .btns button{{margin:4px 6px 0 0;padding:6px 10px;border-radius:6px;border:1px solid #999;background:transparent;color:var(--ink);cursor:pointer}}
.btns button.on{{background:var(--acc);color:#fff;border-color:var(--acc)}} #bar{{position:sticky;top:0;background:var(--bg);padding:8px 0;border-bottom:1px solid #8884}}
</style></head><body>
<div id=bar><b>Question for each item:</b> does the highlighted text say the paper's authors deposited this dataset
<i>as part of this study</i>? Answer from the sentence; open the paper only if the sentence is unclear.
<br><span id=prog></span> &nbsp; <button id=copy>Copy my answers</button> <span id=msg></span></div>
{''.join(items)}
<script>
const K='human_check_answers'; let A={{}}; try{{A=JSON.parse(localStorage.getItem(K)||'{{}}')}}catch(e){{}}
const save=()=>{{try{{localStorage.setItem(K,JSON.stringify(A))}}catch(e){{}};document.getElementById('prog').textContent=Object.keys(A).length+' / {len(rows)} answered'}};
const val=a=>a&&typeof a==='object'?a.v:a;
document.querySelectorAll('.btns').forEach(b=>{{const k=b.dataset.key;b.querySelectorAll('button').forEach(x=>{{if(val(A[k])===x.dataset.v)x.classList.add('on');
x.onclick=()=>{{A[k]={{v:x.dataset.v,t:Date.now()}};b.querySelectorAll('button').forEach(y=>y.classList.toggle('on',y===x));save()}}}})}});
document.getElementById('copy').onclick=()=>{{const t=JSON.stringify(A);navigator.clipboard.writeText(t).then(()=>document.getElementById('msg').textContent='Copied. Paste it into the scoring step.',()=>{{document.getElementById('msg').textContent='Copy failed; select this text:';const p=document.createElement('pre');p.textContent=t;document.getElementById('bar').append(p)}})}};
save();
</script></body></html>"""
    page = page.replace("const K='human_check_answers'", f"const K='{out_name}'")
    (OUT / out_name).write_text(page, encoding="utf-8")
    print(f"wrote {OUT / out_name} with {len(rows)} items")


def score(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    human = {k: (v["v"] if isinstance(v, dict) else v) for k, v in raw.items()}
    stamps = sorted(v["t"] for v in raw.values() if isinstance(v, dict) and "t" in v)
    gaps = [(b - a) / 1000 for a, b in zip(stamps, stamps[1:]) if 0 < b - a < 10 * 60 * 1000]  # ignore long breaks
    ai = {f"{r['pmid']}|{r['id']}": r["role"] for r in
          (json.loads(l) for l in open(OUT / "id_audit.jsonl", encoding="utf-8") if l.strip())}
    keys = [k for k in human if k in ai]
    own = sum(human[k] == "OWN" for k in keys)
    decided = [k for k in keys if human[k] != "UNSURE"]
    agree = sum((human[k] == "OWN") == (ai[k] == "OWN") for k in decided)
    po = agree / max(1, len(decided))
    ph, pa = (sum(human[k] == "OWN" for k in decided) / max(1, len(decided)),
              sum(ai[k] == "OWN" for k in decided) / max(1, len(decided)))
    pe = ph * pa + (1 - ph) * (1 - pa)
    kappa = (po - pe) / (1 - pe) if pe < 1 else float("nan")
    L = [f"human answers: {len(keys)}; OWN {own}; UNSURE {len(keys) - len(decided)}",
         f"human-confirmed own (of all answered): {own}/{len(keys)} = {100 * own / max(1, len(keys)):.0f}%",
         f"human vs AI agreement on decided items: {agree}/{len(decided)} = {100 * po:.0f}%; Cohen's kappa {kappa:.2f}"]
    if gaps:
        gaps.sort()
        L.append(f"time per decision: median {gaps[len(gaps) // 2]:.0f} s (n={len(gaps)}, breaks over 10 min ignored)")
    for k in decided:
        if (human[k] == "OWN") != (ai[k] == "OWN"):
            L.append(f"  disagree {k}: human={human[k]} ai={ai[k]}")
    (OUT / "human_check_report.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", help="score pasted answers (JSON) against the AI audit")
    ap.add_argument("--rows", help="build from a candidate-row TSV instead of the audit sample, "
                                   "e.g. validate/out/ins_missing_datasets.tsv")
    ap.add_argument("--n", type=int, default=0, help="random sample size (0 = all)")
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--out", default="human_check.html")
    a = ap.parse_args()
    score(a.score) if a.score else build(a.rows, a.n, a.seed, a.out)
