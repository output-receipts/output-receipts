"""Per-paper disposition rules.

One primary label per paper, plus flags. Rules first; papers the rules cannot decide are labelled by a
reviewer (in this prototype, an AI coding agent reading the statement text) through an overrides file,
data/review/llm_review_labels.csv. Every paper records label_source = "rule" or "llm-review".

This is about what can be OBSERVED in the open full text. It never judges compliance with a sharing plan.
"""
from __future__ import annotations

import csv
import os
import re

LABELS = {
    "PUBLIC_REPOSITORY": "New data or code from this paper deposited in an open repository (identifier given).",
    "CONTROLLED_ACCESS": "New data deposited in a controlled-access repository (dbGaP, EGA, GDC controlled tier, "
                         "Vivli, NCTN Data Archive, ...).",
    "ON_REQUEST": "New data only available by asking the authors (with or without conditions such as a DUA/IRB).",
    "IN_ARTICLE": "Statement says the data are in the article or its supplementary files; no repository deposit.",
    "REUSED_ONLY": "The paper only analyzes pre-existing data created by others (e.g., TCGA, public GEO series).",
    "NO_DATA_GENERATED": "Statement says no data were generated or sharing is not applicable, or the article is a "
                         "review/editorial/comment with no data.",
    "NO_STATEMENT": "Open full text available, but no availability statement and no deposit identifiers found "
                    "(or only a promise of a future deposit with no identifier).",
    "NOT_CHECKABLE": "No open full text (not in the PMC open-access subset or author-manuscript set).",
}
FLAGS = {
    "LINK_DEAD": "A repository identifier the paper gives for its own outputs does not resolve publicly "
                 "(not found, or still private). Other web links are reported separately, not flagged.",
    "CODE_SHARED": "Code from this paper is shared (GitHub, GitLab, Zenodo, Code Ocean, ...).",
    "MIXED": "The paper both produces new data and reuses existing data.",
}
NON_RESEARCH_TYPES = ("review", "editorial", "comment", "letter", "news", "erratum", "correction", "retraction",
                      "case-report", "case report", "introduction", "book-review", "obituary", "meeting-report", "abstract",
                      "brief-report-review", "expression of concern")
OPEN_CLASSES = ("public", "generic", "code")


def load_overrides(path: str) -> dict:
    out = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("pmid") and r.get("label") in LABELS:
                    out[r["pmid"].strip()] = r
    return out


def _is_non_research(paper) -> bool:
    at = (paper.get("article_type") or "").lower()
    pt = (paper.get("pub_type") or "").lower()
    if "research-article" in at or "research-article" in pt:
        return False
    return any(t in at for t in NON_RESEARCH_TYPES) or any(t in pt for t in NON_RESEARCH_TYPES)


# Reviews are often typed "research-article" in JATS; the title gives them away. Excludes chart/record
# reviews and meta-analyses, which do analyse data.
REVIEW_TITLE = re.compile(
    r"\b(?:narrative|literature|scoping|comprehensive|critical|mini|state[- ]of[- ]the[- ]art|historical|updated?|"
    r"brief|focused|integrative|umbrella)[- ]review\b|\ba review\b|:\s*(?:an? )?review\b|\breview\s*\.?\s*$|"
    r"\breview of (?:the )?(?:literature|current|recent|evidence|emerging|advances|progress|clinical guidelines)\b|"
    r"\bpast,? present,? and (?:the )?future\b|\bwhat(?:'s| is) new in\b", re.I)
NOT_REVIEW_TITLE = re.compile(r"\b(?:chart|records?|retrospective|medical|meta-analys[ie]s|pooled analysis)\b", re.I)


def _is_review_title(paper) -> bool:
    t = paper.get("title") or ""
    return bool(REVIEW_TITLE.search(t)) and not NOT_REVIEW_TITLE.search(t)


def rule_label(paper: dict) -> tuple[str, str]:
    """Return (label or 'NEEDS_REVIEW', reason)."""
    if not paper.get("fulltext_source"):
        return "NOT_CHECKABLE", paper.get("fulltext_note") or "no open full text"
    x = paper["extraction"]
    c = x["cues"]
    ids = x["identifiers"]
    gen = [i for i in ids if i["role"] == "generated"]
    gen_open = [i for i in gen if i["cls"] in OPEN_CLASSES]
    gen_ctrl = [i for i in gen if i["cls"] == "controlled"]
    gen_url = [i for i in gen if i["cls"] == "url"]
    unknown = [i for i in ids if i["role"] == "unknown" and i["cls"] != "url"]
    reused_in_stmt = [i for i in ids if i["role"] == "reused" and "das" in i["locations"]]
    has_stmt = x["statement_kind"] != "none"

    if gen_open:
        return "PUBLIC_REPOSITORY", "generated identifier(s) in open repository: " + \
            ", ".join(f"{i['repo']}:{i['id']}" for i in gen_open[:4])
    if gen_ctrl:
        return "CONTROLLED_ACCESS", "generated identifier(s) in controlled repository: " + \
            ", ".join(f"{i['repo']}:{i['id']}" for i in gen_ctrl[:4])
    if c["controlled_gen"] and not c["on_request"]:
        return "CONTROLLED_ACCESS", "statement names a controlled-access repository for the study's data"
    if _is_review_title(paper):
        return "NO_DATA_GENERATED", "title indicates a review article (no own deposits found)"
    if unknown:
        return "NEEDS_REVIEW", "identifier(s) in statement with unclear generated/reused role: " + \
            ", ".join(f"{i['repo']}:{i['id']}" for i in unknown[:4])
    if gen_url:
        return "NEEDS_REVIEW", "statement points to a non-repository link: " + gen_url[0]["id"][:80]
    if c["controlled_gen"]:
        return "NEEDS_REVIEW", "controlled-access repository and on-request language both present"
    if c["will_deposit"] and not c["on_request"]:
        return "NEEDS_REVIEW", "statement promises a future deposit"
    # Some data openly in the article/supplement beats "only on request" (ON_REQUEST means ONLY by asking)
    if c["on_request"] and not c["in_article"]:
        return "ON_REQUEST", "statement says data are available on request"
    if c["in_article"]:
        return "IN_ARTICLE", "statement says data are within the article/supplement" + \
            ("; the rest on request" if c["on_request"] else "")
    if c["no_data"]:
        if c["reuse_in_statement"] or reused_in_stmt:
            return "REUSED_ONLY", "statement says no new data and names reused sources"
        return "NO_DATA_GENERATED", "statement says no data generated / not applicable"
    if has_stmt and (c["reuse_in_statement"] or reused_in_stmt) and not c["gen_in_statement"]:
        return "REUSED_ONLY", "statement only describes reused/public sources"
    if has_stmt:
        return "NEEDS_REVIEW", "statement present but no rule matched"
    if _is_non_research(paper):
        return "NO_DATA_GENERATED", f"article type '{paper.get('article_type') or paper.get('pub_type')}' with no statement"
    return "NO_STATEMENT", "no availability statement or deposit identifiers found"


def flags_for(paper: dict, label: str, resolution: dict) -> dict:
    x = paper.get("extraction") or {"identifiers": [], "cues": {}, "reuse_resources": []}
    ids = x["identifiers"]
    dead = []
    for i in ids:
        # same identifier set as the summary's dead-link rate (report.own_identifiers): own, repository identifiers
        if i["role"] == "generated" and i["cls"] != "url":
            st = resolution.get(f"{i['repo']}|{i['id']}", {}).get("status")
            if st in ("NOT_FOUND", "PRIVATE"):
                dead.append(f"{i['id']} ({st.lower()})")
    code = any(i["role"] == "generated" and i["is_code"] for i in ids)
    new_data = label in ("PUBLIC_REPOSITORY", "CONTROLLED_ACCESS", "ON_REQUEST", "IN_ARTICLE")
    if label == "PUBLIC_REPOSITORY" and not any(i["role"] == "generated" and not i["is_code"] for i in ids) \
            and not x["cues"].get("on_request") and not x["cues"].get("in_article"):
        new_data = False  # code-only deposit; no evidence of new data
    reused = any(i["role"] == "reused" and not i["is_code"] and i["cls"] != "url" and
                 not i["evidence"].startswith("cited in reference") for i in ids) or bool(x.get("reuse_resources"))
    return {"LINK_DEAD": dead, "CODE_SHARED": code, "MIXED": bool(new_data and reused)}


def classify(paper: dict, resolution: dict, overrides: dict) -> dict:
    label, reason = rule_label(paper)
    source = "rule"
    ov = overrides.get(paper["pmid"])
    if ov:
        label, reason, source = ov["label"], (ov.get("note") or "reviewed").strip(), "llm-review"
    fl = flags_for(paper, label, resolution)
    if ov:
        for k, col in (("MIXED", "mixed"), ("CODE_SHARED", "code_shared")):
            v = (ov.get(col) or "").strip().lower()
            if v in ("1", "true", "yes", "y"):
                fl[k] = True
            elif v in ("0", "false", "no", "n"):
                fl[k] = False
    return {"label": label, "label_source": source, "reason": reason,
            "flags": [k for k in ("LINK_DEAD", "CODE_SHARED", "MIXED") if fl[k]],
            "dead_identifiers": fl["LINK_DEAD"]}
