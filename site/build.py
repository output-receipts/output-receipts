"""Build the public layer: the landing page, the receipts browser and the public data files.

  python site/build.py [--example R01CA286147]

Reads the full local outputs of a run (data/output, data/work, validate/out) and writes what is published:

  index.html                          landing page (served by GitHub Pages)
  receipts.html                       browse the receipts (awards with a deposit): what their papers shared, with links
  public/candidates_not_in_ins.tsv    dataset rows INS does not list yet: INS's column names for the identifying fields, plus the
                                      evidence sentence, the record check and the triage tier
  public/candidates_all.tsv           every own deposit found (datasets and code), same columns, with in_ins
  public/receipts.json                the receipts as data
  public/summary.json                 the aggregate numbers shown on the pages

What is public is deliberately positive and aggregate: deposits that were found and whose link works. Dead, private
and unverified links, and the per-paper category labels, are counted in the aggregates but not listed by award or
paper; the tool writes those details locally (data/output) for the investigator and the curator.
Every number on the pages is computed here from the run's outputs, so the pages cannot drift from the data.
"""
import argparse
import csv
import html
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from receipts import extract  # noqa: E402

REPO = "https://github.com/output-receipts/output-receipts"
OUT = ROOT / "validate/out"
PUB = ROOT / "public"
NCBI = ("GEO", "SRA/BioProject", "dbGaP")
TIER_TEXT = {
    "1": ("The link works and the repository's own record confirms the deposit: it cites the paper, names the award, "
          "or lists a submitter whose surname and initial match an author", "Quick check: open the record"),
    "2": ("The link works and the paper states the deposit explicitly; the record is silent or cannot be read",
          "Read the quoted sentence"),
    "3": ("The wording is weaker, or the link does not resolve (dead, private, restricted or unverified)",
          "Open the paper; route link problems to the investigator"),
}
e = html.escape


def jl(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()] if path.exists() else []


def tsv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def epmc(p):
    return f"https://europepmc.org/article/PMC/{p['pmcid']}" if p.get("pmcid") else \
        f"https://europepmc.org/article/MED/{p['pmid']}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--example", default="R01CA286147", help="award whose receipt is shown, without names, as the example")
    a = ap.parse_args()

    s = json.load(open(ROOT / "data/output/summary.json", encoding="utf-8"))
    h = s["headline"]
    ins = json.load(open(OUT / "ins_compare.json", encoding="utf-8"))
    rows = tsv(ROOT / "data/output/ins_candidate_datasets.tsv")
    missing = {(r["dataset_source_repo"], r["dataset_source_id"]) for r in tsv(OUT / "ins_missing_datasets.tsv")}
    papers = json.load(open(ROOT / "data/work/papers.json", encoding="utf-8"))
    grants = json.load(open(ROOT / "data/work/grants.json", encoding="utf-8"))
    rank = {"cites_paper": 3, "names_award": 2, "authors": 1}

    # ---- public rows: the evidence sentence comes from the paper whose record evidence gave the row its tier
    def evidence(r):
        best = None
        for pm in sorted(r["dataset_pmid"].split(";")):
            for i in (papers.get(pm, {}).get("extraction") or {}).get("identifiers", []):
                if (i["repo"], i["id"], i["role"]) == (r["dataset_source_repo"], r["dataset_source_id"], "generated"):
                    k = rank.get(i.get("record_check", ""), 0)
                    if best is None or k > best[0]:
                        best = (k, pm, i["context"])
        return (best[1], best[2]) if best else ("", "")

    pub_rows = []
    for r in rows:
        if r["link_status"] != "RESOLVES":
            continue   # dead, private and unverified links are counted in the aggregates, not listed
        pm, sent = evidence(r)
        q = {k: r[k] for k in ("type", "dataset_uuid", "dataset_source_repo", "dataset_source_id", "dataset_source_url",
                               "dataset_pmid", "funding_source", "link_status", "study_group", "record_check",
                               "record_note", "triage_tier")}
        q["in_ins"] = "no" if (r["dataset_source_repo"], r["dataset_source_id"]) in missing else "yes"
        q["evidence_paper"] = epmc(papers[pm]) if pm else ""
        q["evidence_sentence"] = extract.redact_access(sent)
        pub_rows.append(q)
    PUB.mkdir(exist_ok=True)
    cols = list(pub_rows[0])
    not_in = [r for r in pub_rows if r["type"] == "dataset" and r["in_ins"] == "no"]
    for name, rs in (("candidates_all.tsv", pub_rows), ("candidates_not_in_ins.tsv", not_in)):
        with open(PUB / name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, cols, delimiter="\t")
            w.writeheader()
            w.writerows(sorted(rs, key=lambda r: (r["triage_tier"], r["dataset_source_repo"], r["dataset_source_id"])))

    # ---- receipts: per award, what its papers shared (deposits found whose link works)
    by_award = defaultdict(lambda: defaultdict(list))
    for r in pub_rows:
        for g in r["funding_source"].split(";"):
            for pm in r["dataset_pmid"].split(";"):
                if g in grants and g in papers.get(pm, {}).get("grants", []):
                    by_award[g][pm].append(r)
    receipts = []
    for g, pp in sorted(by_award.items()):
        G = grants[g]
        ps = []
        for pm, rs in sorted(pp.items(), key=lambda kv: (papers[kv[0]].get("pub_year") or "", kv[0])):
            p = papers[pm]
            ps.append({"pmid": pm, "year": p.get("pub_year") or "", "title": p.get("title") or "", "url": epmc(p),
                       "shared": [{"repo": r["dataset_source_repo"], "id": r["dataset_source_id"],
                                   "url": r["dataset_source_url"], "kind": "code" if r["type"] == "resource" else "data",
                                   "record_check": r["record_check"], "in_ins": r["in_ins"]} for r in rs]})
        receipts.append({"award": g, "title": G.get("title") or "", "org": G.get("org") or "",
                         "reporter_url": G.get("reporter_url") or "", "papers": ps})
    (PUB / "receipts.json").write_text(json.dumps(receipts, ensure_ascii=False), encoding="utf-8")

    # ---- aggregates
    miss_all = [r for r in tsv(OUT / "ins_missing_datasets.tsv") if r["type"] == "dataset"]
    tiers = Counter(r["triage_tier"] for r in miss_all)
    data_rows = [r for r in rows if r["type"] == "dataset"]
    rec_ok = sum(1 for r in data_rows if r["record_check"].startswith("record "))
    st = h["own_identifier_status"]
    dead = st.get("NOT_FOUND", 0) + st.get("PRIVATE", 0)
    definitive = dead + st.get("RESOLVES", 0) + st.get("RESTRICTED", 0)
    geo = [r for r in rows if r["dataset_source_repo"] == "GEO" and r["harvestable_via_ncbi_links"] in ("yes", "no")]
    geo_no = sum(r["harvestable_via_ncbi_links"] == "no" for r in geo)
    awards_data = len({g for r in data_rows for g in r["funding_source"].split(";") if g in grants})

    # blind review, two reviewers on the same rows, by tier
    A = {(r["pmid"], r["id"]): r for r in jl(OUT / "id_audit_tiers_v2.jsonl") if r.get("role")}
    B = {(r["pmid"], r["id"]): r for r in jl(OUT / "id_audit_tiers_v2_gpt.jsonl") if r.get("role")}
    keys = [k for k in A if k in B]
    audit = {}
    for t in ("1", "2", "3"):
        ks = [k for k in keys if A[k].get("tier") == t]
        audit[t] = {"n": len(ks), "both": sum(A[k]["role"] == "OWN" and B[k]["role"] == "OWN" for k in ks),
                    "any": sum(A[k]["role"] == "OWN" or B[k]["role"] == "OWN" for k in ks)}
    tot = sum(tiers[t] for t in audit if audit[t]["n"])
    weighted = sum(tiers[t] * audit[t]["both"] / audit[t]["n"] for t in audit if audit[t]["n"]) / tot if tot else None
    bench = json.load(open(OUT / "ins_benchmark.json", encoding="utf-8")) if (OUT / "ins_benchmark.json").exists() else None

    public_summary = {
        "generated": s.get("generated"), "cohort": s.get("query"), "ins_tables_gathered": ins["ins_snapshot"],
        "awards": ins["awards"], "awards_with_an_own_dataset": awards_data, "awards_in_ins": ins["awards_in_ins"],
        "papers_in_scope": h["papers_in_scope"], "papers_with_open_full_text": h["papers_checkable"],
        "own_datasets": ins["own_datasets"], "own_datasets_studies": h.get("own_data_studies_unique"),
        "own_datasets_confirmed_by_repository_record": rec_ok, "own_datasets_already_in_ins": ins["own_datasets_in_ins"],
        "already_in_ins_under_the_same_paper": ins["own_datasets_in_ins_same_paper"],
        "already_in_ins_under_other_papers_only": ins["own_datasets_in_ins_other_papers_only"],
        "already_in_ins_with_no_paper_named": ins["own_datasets_in_ins_no_paper_named"],
        "own_datasets_not_in_ins": ins["own_datasets_not_in_ins"], "not_in_ins_link_works": ins["not_in_ins_resolving"],
        "not_in_ins_outside_geo_sra_dbgap": ins["not_in_ins_outside_ncbi"],
        "not_in_ins_by_tier": {t: tiers[t] for t in ("1", "2", "3")}, "code_deposits": ins["code_deposits"],
        "identifiers_checked": definitive, "identifiers_not_resolving": dead, "records_still_private": st.get("PRIVATE", 0),
        "own_geo_series": len(geo), "own_geo_series_not_linked_from_pubmed": geo_no,
        "paper_categories_count": h["disposition_counts"], "datasets_by_repository": ins["datasets_by_repository"],
        "blind_review_by_tier": audit, "blind_review_both_confirm_weighted_pct": round(100 * weighted, 1) if weighted else None,
        "benchmark_against_ins_records": bench,
    }
    (PUB / "summary.json").write_text(json.dumps(public_summary, indent=1), encoding="utf-8")

    # ---- landing page
    tiles = [
        (f"{ins['own_datasets_not_in_ins']:,}", "candidate dataset records that INS does not list yet"),
        (f"{tiers['1']:,}", "of them: the link works and the repository's own record confirms the deposit"),
        (f"{ins['not_in_ins_outside_ncbi']:,}", "of them are in repositories outside GEO, SRA and dbGaP"),
        (f"{awards_data} of {ins['awards']}", "awards already have at least one dataset their papers report depositing"),
        (f"{ins['own_datasets_in_ins_same_paper']:,}", "more are already in INS under the same paper: INS and the tool agree"),
    ]
    tiles_html = "".join(f"<div class=tile><div class=big>{e(v)}</div><div>{e(t)}</div></div>" for v, t in tiles)

    def acc(t):
        x = audit[t]
        return f"{x['both']} of {x['n']}" if x["n"] else "not yet reviewed"

    n_miss = len(miss_all)
    tier_rows = "".join(
        f"<tr><td><b>{t}</b></td><td>{e(TIER_TEXT[t][0])}</td><td class=n>{tiers[t]:,} ({100 * tiers[t] / n_miss:.0f}%)</td>"
        f"<td class=n>{acc(t)}</td><td>{e(TIER_TEXT[t][1])}</td></tr>" for t in ("1", "2", "3"))

    reps = list(ins["datasets_by_repository"].items())
    top, rest = reps[:10], reps[10:]
    mx = max(v["detected"] for _, v in reps)
    bars = "".join(
        f"<tr><td>{e(k)}</td><td class=barcell><div class=bar style='width:{100 * v['detected'] / mx:.1f}%'>"
        f"<div class=hit style='width:{100 * v['in_ins'] / max(1, v['detected']):.1f}%'></div></div></td>"
        f"<td class=n>{v['detected']}</td></tr>" for k, v in top)
    if rest:
        rd, ri = sum(v["detected"] for _, v in rest), sum(v["in_ins"] for _, v in rest)
        bars += (f"<tr><td>{len(rest)} more</td><td class=barcell><div class=bar style='width:{100 * rd / mx:.1f}%'>"
                 f"<div class=hit style='width:{100 * ri / max(1, rd):.1f}%'></div></div></td><td class=n>{rd}</td></tr>")

    # example receipt, shown without the award, the institution, paper titles or identifiers
    ex = next((r for r in receipts if r["award"] == a.example), None)
    ex_html = ""
    if ex:
        lines = []
        for n, p in enumerate(ex["papers"], 1):
            items = "; ".join(f"{e(x['repo'])} ({x['kind']}) &middot; link works &middot; {e(x['record_check'])}"
                              for x in p["shared"])
            lines.append(f"<tr><td>Paper {n} ({e(p['year'])})</td><td>{items}</td></tr>")
        ex_html = f"""<h2>What a receipt looks like</h2>
<p class=muted>One award from this cohort, shown without names. The <a href="receipts.html">receipts page</a> has every
award with at least one deposit found, with links to each paper and deposit.</p>
<table><tr><th>Paper</th><th>What it shared &middot; link check &middot; record check</th></tr>{''.join(lines)}</table>"""

    bench_html = ""
    if bench and bench.get("recall_by_paper_geo", {}).get("papers"):
        b, g, ad = bench["recall_by_paper_geo"], bench.get("own_vs_ins", {}).get("GEO", {}), bench.get("added", {})
        gt = bench.get("own_vs_ins_sentence_rules_alone", {}).get("GEO", {})
        same = g.get("INS attributes it to this paper (confirmed own)", 0)
        same_t = gt.get("INS attributes it to this paper (confirmed own)", 0)
        bench_html = f"""<h2>A second check, against NCI's own records (no AI)</h2>
<p>The tool was also run on {bench['checkable']:,} papers from programs INS already curates, and compared with the
datasets INS itself attributes to those papers.</p>
<table>
<tr><td>Papers with a GEO series in INS where the tool, reading only the paper, called at least one of them the paper's own</td><td class=n><b>{b['own_text']:,} of {b['papers']:,}</b></td></tr>
<tr><td>The same, after the record check</td><td class=n><b>{b['own']:,} of {b['papers']:,}</b></td></tr>
<tr><td>GEO series the tool, reading only the paper, called a paper's own, where INS names the same paper</td><td class=n><b>{same_t:,} of {gt.get('total', 0):,}</b></td></tr>
<tr><td>The same, after the record check</td><td class=n><b>{same:,} of {g.get('total', 0):,}</b></td></tr>
<tr><td>Datasets found for those papers in repositories outside GEO, SRA and dbGaP</td><td class=n><b>{ad.get('outside_ncbi', 0):,}</b></td></tr>
</table>
<p class=muted>The record check reads the same GEO-to-PubMed link that INS harvests, so the "reading only the paper"
figures are the independent ones. The two methods are complementary: INS's link harvesting also finds series that a
paper's open text never names, and reading the paper finds deposits in repositories that publication links do not
cover.</p>"""

    cats = h["disposition_counts"]
    page = f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Output Receipts</title>
<style>{CSS}</style></head><body><main>
<h1>Output Receipts</h1>
<p class=muted>A discovery aid that connects the data NCI's awards have shared back to NCI's catalog. Public data only;
runs without AI.</p>
<p>For a cohort of NIH awards, Output Receipts follows each award to its papers, reads what each paper says it
deposited, checks that each deposit exists, compares it with the repository's own record where that record can be
read, and writes the results as candidate rows for the
<a href="https://studycatalog.cancer.gov">Index of NCI Studies (INS)</a> and as a receipt for each award with a deposit. It is a
starting point for curation, not a replacement for it. This run covers the {ins['awards']} new R01 awards NCI made in
fiscal year 2024.</p>

<h2>What this run found</h2>
<div class=tiles>{tiles_html}</div>
<p class=muted>Compared with NCI's public <a href="https://github.com/CBIIT/INS-Data">INS-Data</a> tables (gathered
{e(ins['ins_snapshot'])}; its dbGaP table is dated 2026-03-09). INS is built around curated programs, NCBI's links
from publications to GEO and SRA, and NCI's dbGaP studies; it includes {ins['awards_in_ins']} of these {ins['awards']}
awards.</p>

<h2>Three tiers: what is known, and what a curator does</h2>
<div class=tw><table><tr><th>Tier</th><th>What is true of the row</th><th>Rows</th><th>Blind review: both reviewers confirm</th><th>Curator action</th></tr>
{tier_rows}</table></div>
<p class=muted>Rows = the {n_miss:,} dataset candidates not in INS. Blind review: two AI models from different developers,
neither shown the tool's answer, judged the same {len(keys)} rows from passages of the paper's text{f"; weighted to all rows, both confirm about {100 * weighted:.0f}%" if weighted else ""}.
That is agreement with AI reviewers, not a human gold standard. Details: <a href="{REPO}/blob/main/VALIDATION.md">VALIDATION.md</a>.</p>

<div class=cols><div>
<h2>What is looked up</h2>
<ul><li>The link resolves publicly: {st.get('RESOLVES', 0):,} of {definitive:,} identifiers checked.
{st.get('RESTRICTED', 0)} more exist behind an access request; {dead} do not resolve, {st.get('PRIVATE', 0)} of them still private.</li>
<li>The repository's record cites the paper, names the award, or lists a submitter whose surname and initial
match an author ({rec_ok:,} of {len(data_rows):,} datasets).</li>
<li>NIH RePORTER links the paper to the award.</li>
<li>INS lists the dataset, or does not.</li></ul>
<p class=muted>Anyone can check each one in a click.</p>
</div><div>
<h2>What is inferred, and what is not known</h2>
<ul><li><b>Inferred from wording:</b> whether a deposit is the paper's own or data it reused. Each published candidate row quotes the paper's text.</li>
<li><b>Not determined:</b> {cats.get('NOT_CHECKABLE', 0)} of {h['papers_in_scope']:,} papers have no open full text, and the
tool does not guess.</li>
<li><b>Not claimed:</b> which award paid for a dataset. The <code>funding_source</code> column lists the awards in
this cohort that RePORTER links to the paper: an association only.</li></ul>
</div></div>

<h2>Open these first</h2>
<div class=start>
<a href="public/candidates_not_in_ins.tsv"><b>The candidate rows</b><br>datasets INS does not list, with INS's column names for the identifying fields, each with its paper, evidence sentence, record check and tier (TSV; opens in Excel)</a>
<a href="receipts.html"><b>Receipts</b><br>what each award's papers shared, with links; searchable</a>
<a href="{REPO}/blob/main/FLOWCHART.md"><b>The process on one page</b><br>two flowcharts: the pipeline, and how a deposit is judged</a>
<a href="{REPO}/blob/main/HOW_IT_WORKS.md"><b>How it works</b><br>plain language, with real examples</a>
<a href="{REPO}/blob/main/VALIDATION.md"><b>How accurate it is</b><br>every measurement, including the weak spots</a>
<a href="{REPO}"><b>Code</b><br>standard-library Python; run it yourself in about an hour</a>
</div>

<div class=cols><div>
<h2>Own datasets by repository</h2>
<p class=muted>All {len(data_rows):,} datasets found; the dark part is already in INS.</p>
<table>{bars}</table>
</div><div>
<h2>Other measurements</h2>
<table>
<tr><td>Own GEO series not linked from their paper's PubMed record</td><td class=n><b>{geo_no} of {len(geo)}</b></td></tr>
<tr><td>Papers whose deposits are all outside GEO, SRA and dbGaP</td><td class=n><b>{h['papers_whose_own_data_deposits_are_all_outside_GEO_SRA_dbGaP']}</b></td></tr>
<tr><td>Code and software deposits found (candidates for INS's resources)</td><td class=n><b>{ins['code_deposits']}</b></td></tr>
<tr><td>Papers with open full text</td><td class=n><b>{h['papers_checkable']:,} of {h['papers_in_scope']:,}</b></td></tr>
</table>
</div></div>

{bench_html}

{ex_html}

<h2>How it works</h2>
<ol>
<li><b>Awards to papers.</b> NIH RePORTER's award&ndash;publication links, with INS's rule excluding papers published more than a year before the award started.</li>
<li><b>Papers to full text.</b> Open-access XML from PubMed Central and Europe PMC.</li>
<li><b>Statements and identifiers.</b> The data availability statement, and identifiers for more than 25 repositories.</li>
<li><b>Own or reused?</b> Judged from the sentence: &ldquo;generated in this study&hellip; deposited in&rdquo; versus &ldquo;downloaded from&rdquo;, &ldquo;previously published&rdquo;, TCGA.</li>
<li><b>Link and record check.</b> Each deposit is looked up in its repository: does it exist and is it public? Where the record can be read: which paper, award and people does it name?</li>
<li><b>Outputs.</b> Candidate rows with a tier, and a receipt for each award with a deposit.</li>
</ol>

<div class=cols><div>
<h2>What it is good for</h2>
<ul><li>Finds deposits in more than 25 repositories, from the authors' own statements.</li>
<li>Hands curators rows with evidence and a stated confidence.</li>
<li>Transparent rules and public lookups: free, repeatable, no credentials, no AI needed to run.</li>
<li>Takes an NIH Institute, activity code and fiscal year as input; run so far on this one cohort.</li></ul>
</div><div>
<h2>Limits</h2>
<ul><li>Reads only open full text; not supplementary files.</li>
<li>Own-versus-reused is a judgment from wording; tier 3 is where it is weakest.</li>
<li>For some repositories (dbGaP, EGA, MassIVE, GitHub and others) the tool does not yet read a record it can
compare. dbGaP is the weakest: of the {ins['datasets_by_repository'].get('dbGaP', {}).get('detected', 0)} dbGaP studies found here, INS attributes
{ins['in_ins_other_papers_only_by_repository'].get('dbGaP', 0)} only to other papers (probably reused consortium data) and names no paper for
{ins['in_ins_no_paper_named_by_repository'].get('dbGaP', 0)}.</li>
<li>Reports what is findable, never compliance with a data sharing plan.</li>
<li>Dead and private links are counted here but listed only in the local outputs, for the investigator to fix.</li></ul>
</div></div>

<h2>Use of AI</h2>
<p>Running the tool requires no AI. An AI coding assistant helped write the code; AI models served as the blind
reviewers for the accuracy checks; and AI labelled the category of papers the rules could not categorize, which does
not affect the dataset rows. Details: <a href="{REPO}/blob/main/AI_REVIEW.md">AI_REVIEW.md</a>.</p>

<p class=muted>Code: <a href="{REPO}">{REPO.replace('https://', '')}</a> (Apache-2.0) &middot; maintainer's guide:
<a href="{REPO}/blob/main/MAINTAINING.md">MAINTAINING.md</a> &middot; data generated {e(s.get('generated', ''))} &middot;
reproduce: <code>python -m receipts run --fy 2024 --activity R01 --ic NCI</code></p>
</main></body></html>"""
    (ROOT / "index.html").write_text(page, encoding="utf-8")

    # ---- receipts browser
    n_dep = sum(len(p["shared"]) for r in receipts for p in r["papers"])
    browser = f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Output Receipts: receipts</title>
<style>{CSS}
input{{width:100%;padding:10px;font:inherit;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);box-sizing:border-box}}
.award{{background:var(--card);border-radius:10px;padding:12px;margin:10px 0}} .award h3{{margin:0 0 4px;font-size:17px}}
.paper{{margin:8px 0 0 0}} .dep{{margin:2px 0 2px 16px}} .tag{{color:var(--muted);font-size:14px}}
</style></head><body><main>
<p><a href="index.html">&larr; Output Receipts</a></p>
<h1>Receipts</h1>
<p>What each award's papers shared, as found in their open full text: {len(receipts)} awards, {n_dep:,} deposits whose
link works. Papers are tied to awards by NIH RePORTER; a deposit is listed under every award in this cohort that
RePORTER links to its paper.</p>
<p class=muted>An award appears here only when at least one deposit was found. Absence means nothing was found in
open full text; it does not mean nothing was shared. "Record check" says whether the repository's own record
confirms the deposit or the row rests on the paper's wording.</p>
<input id=q placeholder="Search by award number, institution, title, repository or accession" autofocus>
<p class=muted id=count></p>
<div id=list></div>
<script>
const R = {json.dumps(receipts, ensure_ascii=False).replace("</", "<\\/")};
const esc = s => String(s).replace(/[&<>"]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}})[c]);
function draw() {{
  const q = document.getElementById('q').value.trim().toLowerCase();
  const hit = R.filter(r => !q || JSON.stringify(r).toLowerCase().includes(q));
  document.getElementById('count').textContent = hit.length + ' of ' + R.length + ' awards' + (hit.length > 60 ? ' (showing the first 60)' : '');
  document.getElementById('list').innerHTML = hit.slice(0, 60).map(r =>
    '<div class=award><h3>' + esc(r.award) + ' &middot; ' + esc(r.title) + '</h3><div class=tag>' + esc(r.org) +
    (r.reporter_url ? ' &middot; <a href="' + esc(r.reporter_url) + '">RePORTER</a>' : '') + '</div>' +
    r.papers.map(p => '<div class=paper><a href="' + esc(p.url) + '">' + esc(p.title) + '</a> <span class=tag>(' + esc(p.year) + ')</span>' +
      p.shared.map(d => '<div class=dep>' + esc(d.repo) + ' <a href="' + esc(d.url) + '">' + esc(d.id) + '</a> <span class=tag>' +
        esc(d.kind) + ' &middot; ' + esc(d.record_check) + (d.in_ins === 'yes' ? ' &middot; in INS' : '') + '</span></div>').join('') +
      '</div>').join('') + '</div>').join('');
}}
document.getElementById('q').addEventListener('input', draw); draw();
</script>
</main></body></html>"""
    (ROOT / "receipts.html").write_text(browser, encoding="utf-8")
    print(f"wrote index.html, receipts.html ({len(receipts)} awards, {n_dep} deposits), public/ "
          f"({len(pub_rows)} rows, {len(not_in)} datasets not in INS); example {a.example}: {'found' if ex else 'NOT FOUND'}")


CSS = """
:root{--bg:#fff;--ink:#1b1f24;--muted:#5b6470;--acc:#0b5cad;--card:#f4f6f9;--bar:#c9d6e3;--line:#dde3ea}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#111418;--ink:#e6e9ee;--muted:#9aa4b0;--acc:#6aa9ff;--card:#1b2027;--bar:#33404f;--line:#2a313b}}
:root[data-theme=dark]{--bg:#111418;--ink:#e6e9ee;--muted:#9aa4b0;--acc:#6aa9ff;--card:#1b2027;--bar:#33404f;--line:#2a313b}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 "Segoe UI",system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 48px} h1{margin:0 0 4px;font-size:28px} h2{margin:28px 0 8px;font-size:20px}
a{color:var(--acc)} .muted{color:var(--muted)} .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:14px 0}
.tile{background:var(--card);border-radius:10px;padding:12px} .big{font-size:26px;font-weight:700;color:var(--acc)}
.start{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px} .start a{display:block;background:var(--card);border-radius:10px;padding:12px;text-decoration:none;color:var(--ink)}
.start b{color:var(--acc)} table{border-collapse:collapse;width:100%} td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
td.n{text-align:right;white-space:nowrap} .barcell{width:55%} .bar{background:var(--bar);height:12px;position:relative} .hit{background:var(--acc);height:12px}
ol li,ul li{margin:3px 0} .cols{display:grid;grid-template-columns:1fr 1fr;gap:20px} @media(max-width:700px){.cols{grid-template-columns:1fr} td,th{padding:4px 3px;font-size:14px} td.n{white-space:normal}} .tw{overflow-x:auto}
code{font-size:14px}
"""

if __name__ == "__main__":
    main()
