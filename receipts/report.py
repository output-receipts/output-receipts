"""Outputs: per-grant receipts (JSON + CSV), per-paper table, summary, review queue, hand-check sample,
and a single-file static HTML dashboard (no external dependencies)."""
from __future__ import annotations

import csv
import html
import json
import os
import random
import re
import time
from collections import Counter, defaultdict

from . import extract, records
from .resolve import human_url
from .classify import FLAGS, LABELS

# NEEDS_REVIEW is not a label the rules assign on purpose: it is the bucket of papers that neither the rules nor a
# confident review label settled ("ambiguous"). It is counted explicitly so that the shares add up to 100%.
LABEL_DESC = dict(LABELS, NEEDS_REVIEW="Ambiguous: the rules could not decide and no confident review label exists.")
LABEL_ORDER = list(LABEL_DESC)
DEFINITIVE = ("RESOLVES", "NOT_FOUND", "PRIVATE", "RESTRICTED")


def epmc_link(p):
    return f"https://europepmc.org/article/PMC/{p['pmcid']}" if p.get("pmcid") else \
        f"https://europepmc.org/article/MED/{p['pmid']}"


def snippet(p, n=300) -> str:
    x = p.get("extraction")
    if not x:
        return ""
    st = x["statement"]
    if not st:
        ctx = [i["context"] for i in x["identifiers"] if i["role"] in ("generated", "unknown")]
        return extract._clip(ctx[0], n) if ctx else ""
    if len(st) <= n:
        return st
    keys = [extract.ON_REQUEST, extract.GEN_CUES, extract.GEN_WEAK, extract.IN_ARTICLE, extract.NO_DATA,
            extract.REUSE_STRONG, extract.WILL_DEPOSIT, extract.CONTROLLED_NAMES] + [rx for _, _, rx, _ in extract.ID_PATTERNS]
    picked, used = [], 0
    for _, s in extract.sentences(st):
        if any(k.search(s) for k in keys):
            picked.append(s.strip())
            used += len(s) + 1
            if used >= n:
                break
    out = " ".join(picked) or st
    return extract._clip(out, n)


def _ids(p, roles, resolution=None):
    x = p.get("extraction") or {"identifiers": []}
    out = []
    for i in x["identifiers"]:
        if i["role"] in roles:
            s = f"{i['repo']}:{i['id']}"
            if resolution is not None:
                st = resolution.get(f"{i['repo']}|{i['id']}", {}).get("status")
                if st:
                    s += f" [{st}]"
            out.append(s)
    return out


def _new_data_deposit(p):
    x = p.get("extraction") or {"identifiers": []}
    return any(i["role"] == "generated" and not i["is_code"] and i["cls"] in ("public", "generic", "controlled")
               for i in x["identifiers"])


# ================================================================================================
def write_review_queue(papers, path):
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for p in papers.values():
            c = p["classification"]
            if c["label"] == "NEEDS_REVIEW":
                x = p["extraction"]
                f.write(json.dumps({
                    "pmid": p["pmid"], "pmcid": p.get("pmcid"), "title": p.get("title"),
                    "article_type": p.get("article_type"), "pub_type": p.get("pub_type"),
                    "rule_reason": c["reason"], "statement_kind": x["statement_kind"],
                    "statement": x["statement"][:2500], "cues": x["cues"],
                    "identifiers": [{k: i[k] for k in ("repo", "id", "role", "evidence", "locations", "context")}
                                    for i in x["identifiers"] if i["role"] in ("generated", "unknown") or
                                    "das" in i["locations"]][:12],
                }, ensure_ascii=False) + "\n")
                n += 1
    return n


# ================================================================================================
def own_identifiers(p) -> list[dict]:
    """The identifiers a paper presents as its own outputs that count for link checks and the dead-link rate:
    repository identifiers (not 'Other link' URLs, which are reported separately). Same set as classify.flags_for."""
    x = p.get("extraction") or {"identifiers": []}
    return [i for i in x["identifiers"] if i["role"] == "generated" and i["cls"] != "url"]


def write_all(grants, papers, resolution, run_info, out_dir, geo_links=None, study_groups=None):
    os.makedirs(out_dir, exist_ok=True)
    gp = defaultdict(list)
    for p in papers.values():
        for g in p["grants"]:
            gp[g].append(p)

    # ---- papers.csv ------------------------------------------------------------------------------
    pcols = ["pmid", "pmcid", "doi", "pub_year", "journal", "title", "is_preprint", "grants", "fulltext_source",
             "is_author_manuscript", "label", "label_source", "flags", "reason", "statement_kind",
             "statement_snippet", "identifiers_generated", "identifiers_unclear", "identifiers_reused",
             "dead_identifiers", "europepmc_url"]
    with open(os.path.join(out_dir, "papers.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, pcols)
        w.writeheader()
        for p in sorted(papers.values(), key=lambda p: int(p["pmid"])):
            c = p["classification"]
            x = p.get("extraction") or {}
            w.writerow({"pmid": p["pmid"], "pmcid": p.get("pmcid") or "", "doi": p.get("doi") or "",
                        "pub_year": p.get("pub_year") or "", "journal": p.get("journal") or "",
                        "title": (p.get("title") or "")[:300], "grants": ";".join(p["grants"]),
                        "is_preprint": "yes" if p.get("is_preprint") else "no",
                        "fulltext_source": p.get("fulltext_source") or "",
                        "is_author_manuscript": "yes" if p.get("is_manuscript") else ("no" if p.get("fulltext_source") else ""),
                        "label": c["label"], "label_source": c["label_source"], "flags": ";".join(c["flags"]),
                        "reason": c["reason"][:300], "statement_kind": x.get("statement_kind", ""),
                        "statement_snippet": snippet(p),
                        "identifiers_generated": "; ".join(_ids(p, ("generated",), resolution)),
                        "identifiers_unclear": "; ".join(_ids(p, ("unknown",), resolution)),
                        "identifiers_reused": "; ".join(_ids(p, ("reused",))[:15]),
                        "dead_identifiers": "; ".join(c["dead_identifiers"]),
                        "europepmc_url": epmc_link(p)})

    # ---- per-grant receipts ----------------------------------------------------------------------
    receipts = []
    for core, g in sorted(grants.items()):
        ps = gp.get(core, [])
        labs = Counter(p["classification"]["label"] for p in ps)
        chk = [p for p in ps if p.get("fulltext_source")]
        rec = {
            "core_project_num": core, "project_nums": g["project_nums"], "title": g["title"], "org": g["org"],
            "org_state": g.get("org_state"), "fiscal_year": g["fiscal_year"], "start_date": g["start_date"],
            "award_amount": g["award_amount"], "reporter_url": g["reporter_url"],
            "n_papers": len(ps), "n_checkable": len(chk),
            "coverage_pct": round(100 * len(chk) / len(ps), 1) if ps else None,
            "label_counts": {l: labs.get(l, 0) for l in LABEL_ORDER},
            "n_new_data_deposit": sum(1 for p in ps if _new_data_deposit(p)),
            "n_code_shared": sum(1 for p in ps if "CODE_SHARED" in p["classification"]["flags"]),
            "n_mixed": sum(1 for p in ps if "MIXED" in p["classification"]["flags"]),
            "n_link_dead": sum(1 for p in ps if "LINK_DEAD" in p["classification"]["flags"]),
            "dead_identifiers": sorted({d for p in ps for d in p["classification"]["dead_identifiers"]}),
            "repositories": sorted({i["repo"] for p in ps if p.get("extraction")
                                    for i in p["extraction"]["identifiers"] if i["role"] == "generated"}),
            "any_public_deposit": labs.get("PUBLIC_REPOSITORY", 0) > 0,
            "papers": [{"pmid": p["pmid"], "pmcid": p.get("pmcid"), "year": p.get("pub_year"),
                        "title": (p.get("title") or "")[:160], "label": p["classification"]["label"],
                        "label_source": p["classification"]["label_source"], "flags": p["classification"]["flags"],
                        "own_identifiers": _ids(p, ("generated",), resolution)[:12],
                        "unclear_identifiers": _ids(p, ("unknown",), resolution)[:6],
                        "dead_identifiers": p["classification"]["dead_identifiers"]}
                       for p in sorted(ps, key=lambda p: p.get("first_pub_date") or "")],
        }
        receipts.append(rec)
    with open(os.path.join(out_dir, "receipts.json"), "w", encoding="utf-8") as f:
        json.dump({"generated": time.strftime("%Y-%m-%d"), "query": run_info["query"],
                   "labels": LABEL_DESC, "flags": FLAGS,
                   "framing": "Observable sharing (findability) from open full text. Not a compliance judgment: "
                              "sharing plans are not public.", "receipts": receipts}, f, indent=1)
    gcols = ["core_project_num", "project_nums", "title", "org", "org_state", "fiscal_year", "start_date",
             "award_amount", "n_papers", "n_checkable", "coverage_pct"] + [f"n_{l}" for l in LABEL_ORDER] + \
            ["n_new_data_deposit", "n_code_shared", "n_mixed", "n_link_dead", "dead_identifiers", "repositories",
             "any_public_deposit", "reporter_url"]
    with open(os.path.join(out_dir, "receipts.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, gcols)
        w.writeheader()
        for r in receipts:
            row = {k: r.get(k) for k in gcols if not k.startswith("n_") or k in r}
            row.update({f"n_{l}": r["label_counts"][l] for l in LABEL_ORDER})
            row["project_nums"] = ";".join(r["project_nums"])
            row["dead_identifiers"] = "; ".join(r["dead_identifiers"])
            row["repositories"] = ";".join(r["repositories"])
            row["coverage_pct"] = "" if r["coverage_pct"] is None else r["coverage_pct"]
            w.writerow(row)

    summary = build_summary(grants, papers, receipts, resolution, run_info, geo_links or {}, study_groups or {})
    write_ins_candidates(papers, resolution, geo_links or {}, os.path.join(out_dir, "ins_candidate_datasets.tsv"),
                         study_groups or {})
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    write_dashboard(summary, receipts, os.path.join(out_dir, "dashboard.html"))
    return summary


def _pct(a, b):
    return round(100 * a / b, 1) if b else None


def study_group(repo, ident, groups):
    return groups.get((repo, ident), f"{repo}:{ident}")


def build_summary(grants, papers, receipts, resolution, run_info, geo_links, groups=None):
    groups = groups or {}
    P = list(papers.values())
    ft = [p for p in P if p.get("fulltext_source")]
    labs = Counter(p["classification"]["label"] for p in P)
    src = Counter(p["classification"]["label_source"] for p in P)
    lab_src = Counter((p["classification"]["label"], p["classification"]["label_source"]) for p in P)
    g_with_pubs = [r for r in receipts if r["n_papers"]]
    g_chk = [r for r in receipts if r["n_checkable"]]
    # identifiers presented as the papers' own outputs (repository identifiers; other web links reported separately)
    own = [(i["repo"], i["id"]) for p in ft for i in own_identifiers(p)]
    own_u = sorted(set(own))
    url_u = sorted({i["id"] for p in ft for i in p["extraction"]["identifiers"]
                    if i["role"] == "generated" and i["cls"] == "url"})
    url_st = Counter(resolution.get(f"Other link|{u}", {}).get("status", "NOT_CHECKED") for u in url_u)
    st = Counter(resolution.get(f"{r}|{i}", {}).get("status", "NOT_CHECKED") for r, i in own_u)
    st_by_repo = defaultdict(Counter)
    for r, i in own_u:
        st_by_repo[r][resolution.get(f"{r}|{i}", {}).get("status", "NOT_CHECKED")] += 1
    definitive = sum(st[s] for s in DEFINITIVE)
    dead = st["NOT_FOUND"] + st["PRIVATE"]
    repo_papers = Counter()
    for p in ft:
        for repo in {i["repo"] for i in p["extraction"]["identifiers"] if i["role"] == "generated"}:
            repo_papers[repo] += 1   # includes "Other link": where papers point, not the link-check denominator
    research = [p for p in ft if p["classification"]["label"] not in ("NO_DATA_GENERATED",)]
    # findability by an E-utilities harvester (the INS method): own GEO series linked from the paper's PubMed record
    geo_own = {(p["pmid"], i["id"]) for p in ft for i in p["extraction"]["identifiers"]
               if i["repo"] == "GEO" and i["role"] == "generated" and i["id"].startswith("GSE")}
    geo_checked = [(pm, g) for pm, g in geo_own if pm in geo_links]
    geo_linked = [(pm, g) for pm, g in geo_checked if g in set(geo_links.get(pm, []))]
    own_data = [(p, i) for p in ft for i in p["extraction"]["identifiers"]
                if i["role"] == "generated" and not i["is_code"] and i["cls"] != "url"]
    ncbi = {"GEO", "SRA/BioProject", "dbGaP"}
    papers_own_data = {p["pmid"] for p, _ in own_data}
    papers_non_ncbi_only = {pm for pm in papers_own_data
                            if all(i["repo"] not in ncbi for p, i in own_data if p["pmid"] == pm)}
    code = sum(1 for p in ft if "CODE_SHARED" in p["classification"]["flags"])
    funnel = [
        ["Awards (RePORTER)", len(grants)],
        ["Awards with >=1 linked paper", len(g_with_pubs)],
        ["Linked papers (unique PMIDs, after early-pub filter)", len(P)],
        ["Papers with a PMCID", sum(1 for p in P if p.get("pmcid"))],
        ["Papers with open full text (checkable)", len(ft)],
        ["Checkable papers with an availability statement", sum(1 for p in ft if p["extraction"]["statement_kind"] != "none")],
        ["Checkable papers with a public deposit (PUBLIC_REPOSITORY)", labs.get("PUBLIC_REPOSITORY", 0)],
    ]
    headline = {
        "awards": len(grants),
        "awards_with_linked_papers": len(g_with_pubs),
        "papers_in_scope": len(P),
        "papers_excluded_early": len(run_info.get("excluded_early", [])),
        "preprints_in_scope": sum(1 for p in P if p.get("is_preprint")),
        "papers_checkable": len(ft),
        "checkable_pct": _pct(len(ft), len(P)),
        "checkable_by_source": dict(Counter(p["fulltext_source"] for p in ft)),
        "checkable_author_manuscripts": sum(1 for p in ft if p.get("is_manuscript")),
        "statement_found_pct_of_checkable": _pct(sum(1 for p in ft if p["extraction"]["statement_kind"] != "none"), len(ft)),
        "disposition_counts": {l: labs.get(l, 0) for l in LABEL_ORDER},
        "disposition_pct_of_checkable": {l: _pct(labs.get(l, 0), len(ft)) for l in LABEL_ORDER if l != "NOT_CHECKABLE"},
        "label_source_counts": dict(src),
        "papers_with_new_data_deposit": sum(1 for p in ft if _new_data_deposit(p)),
        "code_shared_papers": code,
        "code_shared_pct_of_checkable": _pct(code, len(ft)),
        "code_shared_pct_of_checkable_research": _pct(code, len(research)),
        "mixed_papers": sum(1 for p in ft if "MIXED" in p["classification"]["flags"]),
        "papers_with_dead_link": sum(1 for p in ft if "LINK_DEAD" in p["classification"]["flags"]),
        "own_identifiers_unique": len(own_u),
        "papers_with_own_data_deposit": len(papers_own_data),
        # accession-level vs study-level: GEO SubSeries -> SuperSeries, SRA runs -> their BioProject, EGA datasets ->
        # their EGA study (crosscheck.study_groups)
        "own_data_accessions_unique": len({(i["repo"], i["id"]) for _, i in own_data}),
        "own_data_studies_unique": len({study_group(i["repo"], i["id"], groups) for _, i in own_data}),
        "papers_whose_own_data_deposits_are_all_outside_GEO_SRA_dbGaP": len(papers_non_ncbi_only),
        "own_data_identifiers_outside_GEO_SRA_dbGaP": len({(i["repo"], i["id"]) for _, i in own_data if i["repo"] not in ncbi}),
        "own_geo_series_paper_pairs": len(geo_own),
        "own_geo_series_linked_from_pubmed_pct": _pct(len(geo_linked), len(geo_checked)),
        "own_identifier_status": dict(st),
        # what the repository's own record says about each own identifier (records.py): cites_paper, names_award,
        # authors, "" (record read, says nothing either way), not_read (repository without a readable record)
        "own_identifier_record_check": dict(Counter(record_tiers(papers).values())),
        "own_identifier_dead_rate_pct": _pct(dead, definitive),
        "own_url_links_unique": len(url_u),
        "own_url_status": dict(url_st),
        "own_url_dead_rate_pct": _pct(url_st["NOT_FOUND"] + url_st["PRIVATE"], sum(url_st[s] for s in DEFINITIVE)),
        "grants_with_checkable_paper": len(g_chk),
        "grants_with_public_deposit_pct_of_grants_with_checkable": _pct(sum(1 for r in g_chk if r["any_public_deposit"]), len(g_chk)),
        "grants_with_public_deposit_pct_of_all_grants": _pct(sum(1 for r in receipts if r["any_public_deposit"]), len(receipts)),
        "grants_with_new_data_deposit_pct_of_grants_with_checkable": _pct(sum(1 for r in g_chk if r["n_new_data_deposit"]), len(g_chk)),
        "grants_with_code_shared_pct_of_grants_with_checkable": _pct(sum(1 for r in g_chk if r["n_code_shared"]), len(g_chk)),
        "grants_with_dead_link": sum(1 for r in receipts if r["n_link_dead"]),
        "grants_only_on_request_or_no_statement_pct_of_grants_with_checkable": _pct(sum(
            1 for r in g_chk if r["n_checkable"] and r["label_counts"]["ON_REQUEST"] + r["label_counts"]["NO_STATEMENT"]
            == r["n_checkable"]), len(g_chk)),
    }
    return {
        "generated": time.strftime("%Y-%m-%d"), "query": run_info["query"], "run": {
            k: run_info.get(k) for k in ("started", "finished", "awards_reported", "awards_fetched", "api_notes",
                                         "http_stats")},
        "headline": headline, "funnel": funnel,
        "disposition_by_label_source": {f"{l}|{s}": n for (l, s), n in sorted(lab_src.items())},
        "repositories_new_outputs_papers": dict(repo_papers.most_common()),
        "own_identifier_status_by_repo": {r: dict(c) for r, c in sorted(st_by_repo.items())},
        "labels": LABEL_DESC, "flags": FLAGS,
        "notes": [
            "Findability, not compliance: grants' data management and sharing plans are not public.",
            "Checkable = open full text in the PMC open-access subset or NIH author-manuscript text-mining set.",
            "Own identifiers = identifiers the paper presents as its own new outputs (role 'generated').",
            "Dead-link rate = (NOT_FOUND + PRIVATE) / own repository identifiers with a definitive resolver answer. "
            "Other web links in statements are reported separately (own_url_status) and not counted in it; the "
            "per-paper LINK_DEAD flag uses the same identifier set as the rate.",
            "NEEDS_REVIEW = ambiguous papers (no rule matched, no confident review label); counted so shares sum to 100%.",
        ],
    }


# ================================================================================================
_TIER_RANK = {"cites_paper": 3, "names_award": 2, "authors": 1}


def record_tiers(papers) -> dict:
    """(repo, id) -> strongest record tier among the papers that present it as their own."""
    best = {}
    for p in papers.values():
        for i in own_identifiers(p):
            k = (i["repo"], i["id"])
            t = i.get("record_check", "") if i["repo"] in records.RECORD_REPOS else "not_read"
            if k not in best or _TIER_RANK.get(t, 0) > _TIER_RANK.get(best[k], 0):
                best[k] = t
    return best


def write_ins_candidates(papers, resolution, geo_links, path, groups=None):
    """Own data/code deposits shaped like INS-Data 'dataset' rows (mapping described in README.md).
    dataset_uuid is a deterministic UUID5 of repository + accession (namespace local to this tool)."""
    import uuid
    ns = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/output-receipts")
    rows = {}
    for p in papers.values():
        x = p.get("extraction")
        if not x:
            continue
        for i in x["identifiers"]:
            if i["role"] != "generated" or i["cls"] == "url":
                continue
            key = (i["repo"], i["id"])
            res = resolution.get(f"{i['repo']}|{i['id']}", {})
            r = rows.setdefault(key, {
                "type": "dataset" if not i["is_code"] else "resource",
                "dataset_uuid": str(uuid.uuid5(ns, f"{i['repo']}|{i['id']}")),
                "dataset_source_repo": i["repo"], "dataset_source_id": i["id"],
                "dataset_source_url": human_url(i["repo"], i["id"]), "dataset_pmid": set(), "funding_source": set(),
                "link_status": res.get("status", "NOT_CHECKED"), "is_code": "yes" if i["is_code"] else "no",
                "harvestable_via_ncbi_links": "", "study_group": study_group(i["repo"], i["id"], groups or {}),
                "_tier": "", "record_note": "", "_explicit": False})
            # an explicit own-deposit sentence inside the availability statement ("... have been deposited in ...")
            wording = i.get("evidence_before_crosscheck") or i["evidence"]
            r["_explicit"] |= "das" in i["locations"] and wording.startswith("generation cue")
            # the strongest record evidence among the papers that report this deposit (records.py)
            if _TIER_RANK.get(i.get("record_check", ""), 0) > _TIER_RANK.get(r["_tier"], 0):
                r["_tier"], r["record_note"] = i["record_check"], i.get("record_note", "")
            r["dataset_pmid"].add(p["pmid"])
            r["funding_source"].update(p["grants"])
            if i["repo"] == "GEO" and p["pmid"] in geo_links:
                linked = i["id"] in set(geo_links[p["pmid"]])
                r["harvestable_via_ncbi_links"] = "yes" if linked or r["harvestable_via_ncbi_links"] == "yes" else "no"
    cols = ["type", "dataset_uuid", "dataset_source_repo", "dataset_source_id", "dataset_source_url", "dataset_pmid",
            "funding_source", "link_status", "is_code", "harvestable_via_ncbi_links", "study_group", "record_check",
            "record_note", "triage_tier"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: (r["dataset_source_repo"], r["dataset_source_id"])):
            r = dict(r)
            r["dataset_pmid"] = ";".join(sorted(r["dataset_pmid"]))
            r["funding_source"] = ";".join(sorted(r["funding_source"]))
            r["record_check"] = records.TIER_TEXT[r["_tier"]] if r["dataset_source_repo"] in records.RECORD_REPOS \
                else "paper only (repository record not read)"
            # 1: the link works and the repository record confirms the deposit; 2: the link works and the paper
            # states the deposit explicitly; 3: everything else (weaker wording, or a dead/private/unverified link)
            ok = r["link_status"] == "RESOLVES"
            r["triage_tier"] = 1 if ok and r["_tier"] in records.CONFIRMED else (2 if ok and r["_explicit"] else 3)
            w.writerow(r)
    return len(rows)


# ================================================================================================
def write_handcheck_sample(papers, resolution, path, n=30, seed=2024):
    rng = random.Random(seed)
    by = defaultdict(list)
    for p in sorted(papers.values(), key=lambda p: int(p["pmid"])):
        lab = p["classification"]["label"]
        if lab != "NEEDS_REVIEW":
            by[lab].append(p)
    labels = [l for l in LABEL_ORDER if by[l]]
    quota = {l: min(2, len(by[l])) for l in labels}
    remaining = n - sum(quota.values())
    weights = {l: len(by[l]) for l in labels if l != "NOT_CHECKABLE"}
    tot = sum(weights.values())
    extra = {l: int(remaining * w / tot) for l, w in weights.items()}
    left = remaining - sum(extra.values())
    for l in sorted(weights, key=lambda l: -(remaining * weights[l] / tot - extra[l]))[:left]:
        extra[l] += 1
    picked = []
    for l in labels:
        k = min(len(by[l]), quota[l] + extra.get(l, 0))
        # make sure llm-review labels are represented where they exist
        pool = by[l][:]
        rng.shuffle(pool)
        llm = [p for p in pool if p["classification"]["label_source"] == "llm-review"]
        chosen = llm[:max(1, k // 3)] if llm else []
        chosen += [p for p in pool if p not in chosen][:k - len(chosen)]
        picked += chosen
    rng.shuffle(picked)
    cols = ["sample_id", "pmid", "pmcid", "europepmc_link", "title", "statement_snippet", "tool_label", "tool_flags",
            "identifiers", "reviewer_label", "notes", "label_source"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        for k, p in enumerate(picked, 1):
            c = p["classification"]
            ids = _ids(p, ("generated",), resolution) + [s + " (unclear)" for s in _ids(p, ("unknown",), resolution)]
            w.writerow({"sample_id": k, "pmid": p["pmid"], "pmcid": p.get("pmcid") or "",
                        "europepmc_link": epmc_link(p), "title": (p.get("title") or "")[:150],
                        "statement_snippet": snippet(p) or ("(no open full text)" if not p.get("fulltext_source") else
                                                            "(no statement found)"),
                        "tool_label": c["label"], "tool_flags": ";".join(c["flags"]),
                        "identifiers": "; ".join(ids[:8]), "reviewer_label": "", "notes": "",
                        "label_source": c["label_source"]})
    return len(picked)


# ================================================================================================
def write_dashboard(summary, receipts, path):
    grants = [{"c": r["core_project_num"], "t": (r["title"] or "")[:140], "o": r["org"] or "", "s": r.get("org_state") or "",
               "n": r["n_papers"], "k": r["n_checkable"], "cov": r["coverage_pct"],
               "lc": [r["label_counts"][l] for l in LABEL_ORDER], "dd": r["n_new_data_deposit"],
               "cs": r["n_code_shared"], "dl": r["n_link_dead"], "pub": r["any_public_deposit"],
               "u": r["reporter_url"], "repos": r["repositories"],
               "p": [[p["pmid"], p["pmcid"] or "", p["year"] or "", p["title"][:120], p["label"], p["label_source"],
                      p["flags"], p["own_identifiers"][:6], p["dead_identifiers"]] for p in r["papers"]]}
              for r in receipts]
    data = {"summary": summary, "labels": LABEL_ORDER, "grants": grants}
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    q = summary["query"]
    title = f"Output Receipts: {q['ic']} FY{q['fy']} {q['activity']} awards"
    with open(path, "w", encoding="utf-8") as f:
        f.write(DASHBOARD.replace("__TITLE__", html.escape(title)).replace("__DATA__", payload))


DASHBOARD = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Output Receipts</title>
<style>
:root{color-scheme:light;--bg:#f7f7f5;--surface:#fcfcfb;--line:#e4e3df;--ink:#0b0b0b;--ink2:#52514e;--muted:#7a7974;
--bar:#2a78d6;--bar2:#86b6ef;--good:#0ca30c;--crit:#d03b3b;--chip:#eef3fb;--focus:#2a78d6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#121211;--surface:#1a1a19;
--line:#2e2e2c;--ink:#fff;--ink2:#c3c2b7;--muted:#97968e;--bar:#3987e5;--bar2:#184f95;--chip:#1f2a3a}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#121211;--surface:#1a1a19;--line:#2e2e2c;--ink:#fff;--ink2:#c3c2b7;
--muted:#97968e;--bar:#3987e5;--bar2:#184f95;--chip:#1f2a3a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 10px}p.sub{color:var(--ink2);margin:0 0 18px;max-width:900px}
.note{background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--bar);padding:10px 14px;border-radius:6px;color:var(--ink2);margin-bottom:18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:18px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.tile .v{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}.tile .l{color:var(--ink2);font-size:12.5px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}@media(max-width:820px){.grid2{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px 16px;min-width:0}
.bars .row{display:grid;grid-template-columns:minmax(120px,42%) 1fr auto;gap:8px;align-items:center;margin:5px 0;cursor:default}
.bars .lab{color:var(--ink2);font-size:12.5px;overflow:hidden;text-overflow:ellipsis}
.bars .track{height:14px;position:relative}.bars .fill{height:14px;background:var(--bar);border-radius:0 4px 4px 0;min-width:2px}
.bars .row:hover .fill{filter:brightness(1.12)}.bars .num{font-variant-numeric:tabular-nums;font-size:12.5px;min-width:76px;text-align:right}
.small{font-size:12px;color:var(--muted)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{position:sticky;top:0;background:var(--surface);cursor:pointer;user-select:none;color:var(--ink2);font-weight:600;white-space:nowrap}
td.n{text-align:right;font-variant-numeric:tabular-nums}tr.g:hover{background:var(--chip)}tr.g{cursor:pointer}
.tablewrap{overflow:auto;max-height:640px;border:1px solid var(--line);border-radius:8px}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 10px}input,select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--ink)}
input:focus,select:focus{outline:2px solid var(--focus)}
.chip{display:inline-block;padding:1px 6px;border-radius:10px;background:var(--chip);font-size:11.5px;margin:1px 2px 1px 0;white-space:nowrap}
.dead{color:var(--crit);font-weight:600}.ok{color:var(--good)}
.papers td{font-size:12.5px}.papers{margin:4px 0 8px}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--surface);padding:6px 8px;border-radius:6px;font-size:12px;max-width:340px;display:none;z-index:9}
a{color:var(--bar)}footer{margin-top:28px;color:var(--muted);font-size:12px}
</style></head><body><main>
<h1>__TITLE__</h1>
<p class="sub">A per-grant receipt of <b>observable</b> research-output sharing: grant &rarr; linked papers (NIH RePORTER) &rarr; open full text (PMC / Europe PMC) &rarr; data and code availability statements and repository identifiers &rarr; does each identifier resolve?</p>
<p class="small"><a href="../../index.html">&larr; Project page: findings and comparison with the Index of NCI Studies</a></p>
<div class="note"><b>Findability, not compliance.</b> Data management and sharing plans are not public, so nothing here judges whether a grant met its plan. It reports what a reader can find, with coverage shown at every step. NCI holds the plans and can compare them internally.</div>
<div class="tiles" id="tiles"></div>
<div class="grid2"><div class="card"><h2>Where papers put their new outputs</h2><div class="bars" id="repos"></div><div class="small">Papers with at least one identifier presented as their own output, by repository.</div></div>
<div class="card"><h2>Do the papers' own identifiers resolve?</h2><div class="bars" id="res"></div><div class="small" id="resnote"></div></div></div>
<div class="grid2"><div class="card"><h2>Coverage funnel</h2><div class="bars" id="funnel"></div><div class="small" id="funnote"></div></div>
<div class="card"><h2>What papers say about their data</h2><div class="bars" id="disp"></div><div class="small">Share of all in-scope papers. Hover a bar for the definition.</div></div></div>
<div class="card"><h2>Grant receipts</h2>
<div class="controls"><input id="q" type="search" placeholder="Search grant number, title, institution" size="36" aria-label="Search grants">
<select id="f" aria-label="Filter grants"><option value="">All grants</option><option value="pub">With a public deposit</option><option value="nopub">Checkable, no public deposit</option><option value="dead">With a dead link</option><option value="code">With shared code</option><option value="nochk">No checkable paper</option></select>
<span class="small" id="count"></span></div>
<div class="tablewrap"><table id="gt"><thead><tr>
<th data-k="c">Grant</th><th data-k="t">Title / institution</th><th data-k="n" class="n">Papers</th><th data-k="k" class="n">Checkable</th>
<th data-k="pr" class="n">Public repo</th><th data-k="ca" class="n">Controlled</th><th data-k="or" class="n">On request</th><th data-k="ns" class="n">No statement</th>
<th data-k="cs" class="n">Code</th><th data-k="dl" class="n">Dead links</th></tr></thead><tbody></tbody></table></div>
<div class="small" style="margin-top:6px">Click a grant to open its receipt (papers, labels, identifiers). Labels marked * were assigned by AI review of the statement, not by a rule.</div></div>
<footer id="foot"></footer></main><div id="tip" role="tooltip"></div>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent),S=D.summary,H=S.headline,L=D.labels;
const fmt=n=>n==null?'n/a':n.toLocaleString(),pc=n=>n==null?'n/a':n+'%';
const tip=document.getElementById('tip');
function tipOn(e,t){tip.textContent=t;tip.style.display='block';mv(e)}function mv(e){tip.style.left=Math.min(e.clientX+12,innerWidth-350)+'px';tip.style.top=(e.clientY+14)+'px'}
function tipOff(){tip.style.display='none'}addEventListener('scroll',tipOff,{passive:true});
function bars(el,rows,tot,fmtNum){const m=Math.max(...rows.map(r=>r[1]),1);el.innerHTML='';rows.forEach(r=>{const d=document.createElement('div');d.className='row';
d.innerHTML=`<div class="lab">${r[0]}</div><div class="track"><div class="fill" style="width:${100*r[1]/m}%;${r[1]?'':'min-width:0'}"></div></div><div class="num">${fmtNum?fmtNum(r[1]):fmt(r[1])}${tot?' · '+(100*r[1]/tot).toFixed(1)+'%':''}</div>`;
d.onmouseenter=e=>tipOn(e,r[2]||`${r[0]}: ${fmt(r[1])}`);d.onmousemove=mv;d.onmouseleave=tipOff;el.appendChild(d)})}
const tiles=[[fmt(H.awards),'awards in scope'],[fmt(H.papers_in_scope),'linked papers'],[pc(H.checkable_pct),'papers checkable (open full text)'],
[pc(H.grants_with_public_deposit_pct_of_grants_with_checkable),'of grants with a checkable paper show at least one public deposit'],
[pc(H.code_shared_pct_of_checkable),'of checkable papers share code'],[pc(H.own_identifier_dead_rate_pct),'of the papers’ own identifiers do not resolve publicly']];
document.getElementById('tiles').innerHTML=tiles.map(t=>`<div class="tile"><div class="v">${t[0]}</div><div class="l">${t[1]}</div></div>`).join('');
bars(document.getElementById('funnel'),S.funnel.map(f=>[f[0],f[1]]),0);
document.getElementById('funnote').textContent=`${fmt(H.papers_excluded_early)} linked papers published more than 365 days before the award start were excluded (the INS pipeline uses the same rule).`;
const tot=H.papers_in_scope;bars(document.getElementById('disp'),L.map(l=>[l.replace(/_/g,' ').toLowerCase(),H.disposition_counts[l]||0,S.labels[l]]),tot);
const rp=Object.entries(S.repositories_new_outputs_papers).slice(0,12);bars(document.getElementById('repos'),rp.map(r=>[r[0],r[1]]),0);
const st=H.own_identifier_status,order=['RESOLVES','NOT_FOUND','PRIVATE','RESTRICTED','UNVERIFIED','SKIPPED_API_DOWN','NOT_CHECKED'];
const sdesc={RESOLVES:'Record exists and is public',NOT_FOUND:'Resolver says it does not exist',PRIVATE:'Exists but not yet public',RESTRICTED:'Exists; access requires approval',UNVERIFIED:'Could not tell (bot protection, odd response)',SKIPPED_API_DOWN:'Resolver API unavailable during the run',NOT_CHECKED:'Not checked'};
bars(document.getElementById('res'),order.filter(k=>st[k]).map(k=>[k.replace(/_/g,' ').toLowerCase(),st[k],sdesc[k]]),H.own_identifiers_unique);
const us=H.own_url_status||{};document.getElementById('resnote').textContent=`${fmt(H.own_identifiers_unique)} unique repository identifiers. Dead-link rate = (not found + private) / definitive answers = ${pc(H.own_identifier_dead_rate_pct)}. Other web links in statements, not counted here: ${fmt(H.own_url_links_unique||0)} (${fmt((us.NOT_FOUND||0)+(us.PRIVATE||0))} not found).`;
const G=D.grants.map(g=>Object.assign(g,{pr:g.lc[0],ca:g.lc[1],or:g.lc[2],ns:g.lc[6]}));let sk='c',sd=1;
const tb=document.querySelector('#gt tbody');
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function render(){const q=document.getElementById('q').value.toLowerCase(),f=document.getElementById('f').value;
let rows=G.filter(g=>(!q||(g.c+' '+g.t+' '+g.o).toLowerCase().includes(q))&&(!f||(f=='pub'&&g.pub)||(f=='nopub'&&g.k&&!g.pub)||(f=='dead'&&g.dl)||(f=='code'&&g.cs)||(f=='nochk'&&!g.k)));
rows.sort((a,b)=>(a[sk]>b[sk]?1:a[sk]<b[sk]?-1:0)*sd);document.getElementById('count').textContent=`${rows.length} grants`;
tb.innerHTML=rows.map(g=>`<tr class="g" data-c="${g.c}"><td><a href="${esc(g.u)}" target="_blank" rel="noopener">${g.c}</a></td><td>${esc(g.t)}<div class="small">${esc(g.o)}${g.s?', '+g.s:''}</div></td>
<td class="n">${g.n}</td><td class="n">${g.k}</td><td class="n">${g.pr||''}</td><td class="n">${g.ca||''}</td><td class="n">${g.or||''}</td><td class="n">${g.ns||''}</td><td class="n">${g.cs||''}</td><td class="n ${g.dl?'dead':''}">${g.dl||''}</td></tr>`).join('');}
tb.addEventListener('click',e=>{if(e.target.tagName=='A')return;const tr=e.target.closest('tr.g');if(!tr)return;const nx=tr.nextElementSibling;
if(nx&&nx.classList.contains('det')){nx.remove();return}const g=G.find(x=>x.c==tr.dataset.c);const d=document.createElement('tr');d.className='det';
d.innerHTML=`<td colspan="10">${g.p.length?`<table class="papers"><tr><th>PMID</th><th>Year</th><th>Title</th><th>Disposition</th><th>Flags</th><th>Own identifiers</th></tr>`+
g.p.map(p=>`<tr><td><a target="_blank" rel="noopener" href="https://europepmc.org/article/${p[1]?'PMC/'+p[1]:'MED/'+p[0]}">${p[0]}</a></td><td>${p[2]}</td><td>${esc(p[3])}</td><td>${p[4].replace(/_/g,' ').toLowerCase()}${p[5]=='llm-review'?' *':''}</td><td>${p[6].map(f=>`<span class="chip">${f.replace('_',' ').toLowerCase()}</span>`).join('')}</td><td>${p[7].map(i=>`<span class="chip ${/NOT_FOUND|PRIVATE/.test(i)?'dead':''}">${esc(i)}</span>`).join('')}</td></tr>`).join('')+'</table>':'<span class="small">No linked papers in RePORTER yet.</span>'}</td>`;tr.after(d)});
document.querySelectorAll('#gt th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;sd=(sk==k)?-sd:(['c','t'].includes(k)?1:-1);sk=k;render()});
document.getElementById('q').oninput=render;document.getElementById('f').onchange=render;render();
document.getElementById('foot').innerHTML=`Generated ${S.generated}. Query: ${S.query.ic} FY${S.query.fy} ${S.query.activity}, award type ${S.query.award_type}. Labels: ${Object.entries(H.label_source_counts).map(e=>e[0]+' '+e[1]).join(', ')}. API notes: ${esc((S.run.api_notes||[]).join('; '))}. Data: NIH RePORTER, Europe PMC, PMC Cloud Service, repository resolver APIs. Code: Apache-2.0.`;
</script></body></html>
"""
