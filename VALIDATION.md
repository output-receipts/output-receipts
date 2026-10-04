# Validation

How far can the results be trusted? This page lists every measurement made on the FY2024 NCI R01 run, including
the ones that show weaknesses. Scripts are in `validate/` (file-by-file map in `validate/README.md`). AI models
were used here only to check the tool; they are not part of it. The reviewers' row-by-row answers are kept with
the run's local outputs and are not published; the aggregate results are on this page and in
`public/summary.json`.

Two things to keep in mind throughout:

- **Looked-up facts are exact.** That a record exists and is public, that it cites a paper or names an award, that
  RePORTER links a paper to an award, that INS lists an accession: each can be checked in a click, and none of the
  measurements below is about them.
- **What is measured is the judgment** that a deposit is the paper's *own* rather than reused. The reference for it
  is either AI reviewers (fallible, and not a human gold standard) or NCI's own INS records.

## 1. The three tiers, checked by two blind reviewers

`validate/id_audit.py`, `validate/compare_reviewers.py`. Population: the 877 dataset rows INS does not list
(the rows whose link works are published as `public/candidates_not_in_ins.tsv`). A random 130 were drawn by tier (50, 40, 40; seed 21). Two AI models from
different developers (Anthropic Claude Opus 5.5 and OpenAI GPT-6.1) each read the paper's availability-related
passages, including every passage that mentions the identifier, and judged one identifier at a time: is it this
paper's own deposit, reused, not a deposit, or unclear? Neither was told what the tool concluded. The second
reviewer ran in an empty folder with no access to this repository.

| Tier | Rows in the tier | Both reviewers say own | At least one says own | Reviewer 1 / Reviewer 2 |
|---|---|---|---|---|
| 1: link works, repository record confirms | 637 | **50 of 50** | 50 of 50 | 50 / 50 |
| 2: link works, paper states the deposit explicitly | 118 | **32 of 40** | 40 of 40 | 39 / 33 |
| 3: weaker wording, or the link does not resolve | 122 | **29 of 40** | 30 of 40 | 30 / 29 |

- Weighted to all 877 rows: both reviewers confirm **93.5%**; at least one confirms 96.5%.
- The reviewers agree with each other on 121 of 130 rows (Cohen's kappa 0.65).
- Sampling uncertainty (95% Wilson intervals on "both confirm"): tier 1, 93-100%; tier 2, 65-90%; tier 3, 57-84%.
- Tier 2: six of the eight rows on which the reviewers split come from one paper that lists many Synapse
  accessions by species; one reviewer read them as the study's own, the other as unclear. Sampling rows means one
  long list can weigh heavily in a tier of 118.
- Tier 3: the rows judged not own are mostly reused datasets described with plain "available at" wording (dbGaP and
  GWAS Catalog studies, earlier series from the same laboratory), plus one accession that is misprinted in the
  paper itself. This is the weakness tier 3 exists to flag.

**A correction made during this audit.** In a first round, 23 of the 130 identifiers were not in the excerpt the
reviewers saw, because long passages were cut at 2,500 characters. One reviewer then inferred "own" from context and
the other answered "unclear". The excerpt now always includes the passages that mention the identifier, and those 23
rows were judged again by both reviewers; the table above is the second round. The first-round answers are kept
locally.

**Limits of this check.** The reviewers are AI models, not people. They read the same paper text the rules read, so
they share its blind spots; that is why the record check (section 2) and the INS benchmark (section 3), which use
other sources, matter. One of the two models also wrote the rules.

## 2. The record check

`receipts/records.py`. For each own or unclear identifier in a repository with a readable record, the tool compares
the record with the paper.

Of the 1,012 datasets the tool identified as papers' own:

| What the repository record says | Datasets |
|---|---|
| The record cites the paper | 532 |
| The record names the award | 7 |
| A submitter's name matches an author, and the record is not more than a year older than the paper | 205 |
| Record read, no verdict either way | 143 |
| Repository without a readable record (dbGaP, EGA, MassIVE and others) | 125 |

So 744 of 1,012 (74%) are confirmed by the repository's own record: 81% in GEO, SRA and dbGaP together, 61% in other
repositories. The check also changed the sentence rules' answer for 149 (paper, identifier) pairs: 103 were demoted
to reused because the record points to other work, and 46 unclear or default-reused ones were promoted to own
because the record points to the paper.

**Is name matching reliable?** Where GEO itself cites the paper (an independent confirmation), a submitter's name
matched an author in 438 of 443 cases (98.9%). Where GEO cites only other papers, names matched in 73 of 229: the
same laboratory's earlier data, which the release-date rule keeps from counting as a new deposit.

**Against the earlier AI audit.** Of 37 rows from an earlier 60-row audit that the record check now confirms, both
AI reviewers had called 36 own. The record check independently demoted one of the tool errors those reviewers had
found. There was no case where the record confirmed a deposit both reviewers called reused.

## 3. Benchmark against INS's own records (no AI)

`validate/ins_benchmark.py`. INS's public tables name the paper each dataset belongs to. The tool was run on 2,435
papers that INS lists for 2023 to 2025 (every such paper with an INS dataset, plus 1,000 random others); 2,057 have
open full text.

**Does the tool find what INS knows?**

| | Papers |
|---|---|
| Papers with open full text and at least one GEO series in INS | 1,221 |
| At least one of those series is named in the paper's open text | 1,169 (95.7%) |
| The tool calls at least one of them the paper's own, from the sentence rules alone | 1,070 (87.6%) |
| The same, after the record check | 1,122 (91.9%) |

Counted by dataset instead of by paper, 67% of the 2,577 (paper, GEO series) pairs in INS are named in the paper's
open text; of those, the sentence rules alone call 89% the paper's own, and 94% after the record check. The other
33% are series the text never names: INS lists every sub-series NCBI links to a paper, while a paper usually cites
one accession. **The two methods are complementary**; reading papers does not replace link harvesting. (The record
check reads the same NCBI link INS harvests, so only the "sentence rules alone" figures are independent of INS.)

**Is what the tool calls own really own?**

| Repository | Called own by the tool | INS names the same paper | INS names other papers only | Not in INS |
|---|---|---|---|---|
| GEO | 1,734 | 1,628 (93.9%) | 22 (1.3%) | 84 (4.8%) |
| dbGaP | 96 | 27 (28%) | 57 (59%) | 12 (13%) |

**dbGaP is the tool's weak spot.** Large consortium studies that a paper reused are often described with the same
wording as a deposit ("data are available in dbGaP under accession ..."), and dbGaP exposes no record the tool can
compare. In the main run, INS lists 135 of the 1,012 datasets: for 102 it names the same paper (INS and the tool
agree), and for 33 (28 of them dbGaP) it names only earlier papers, which marks them as reused data the tool
misjudged. Among the 877 candidates, all ten dbGaP rows are already in tier 3.

**What does the tool add for papers INS already covers?** 479 datasets in repositories outside GEO, SRA and dbGaP,
from 316 papers (at least 445 resolve); 235 GEO, SRA or dbGaP accessions absent from INS; and 665 code deposits.
Listed in `validate/out/ins_benchmark_added.tsv`.

## 4. Comparison of the main run with INS

`validate/ins_compare.py`, against INS-Data tables gathered 2026-01-30. INS includes 8 of the 589 awards (it is built
around ODS-curated programs) and 16 of the 1,639 award-paper links for papers published before that date. Of 1,012
own datasets, 135 are in INS and 877 are not (512 in GEO, SRA or dbGaP; 365 elsewhere); 840 of the 877 resolve.
"Not in INS" mostly reflects scope, not error: these awards are outside the programs INS curates.

## 5. Link checks

1,334 own identifiers (datasets and code): 1,283 resolve, 5 exist behind an access request, 20 are not found, 21
are private, and 5 could not be verified and are counted as neither. Dead-link rate: 41 of 1,329 definitive answers
(3.1%). Earlier versions of the tool reported a higher rate; most of the difference was the tool's own parsing
errors (citation numbers fused onto accessions, lower-cased URLs, unversioned DOIs), found by code review and now
covered by tests. One "not found" in the audit sample is an accession misprinted in the paper's own text.

## 6. Per-paper categories

`validate/ai_judge.py`, `validate/rescore.py`. The tool also sorts each paper into one category (public repository,
on request, in the article, and so on). A blind AI reviewer chose a category for 100 papers per sample without
seeing the tool's.

| Sample | Agreement | Weighted to all papers |
|---|---|---|
| Sample 1 (seed 7), used to find and fix rule problems; optimistic | 82 of 100 | 86% |
| **Sample 2 (seed 8), not used to tune the rules** | **71 of 100** | **74%** |

By category on sample 2: public repository 23/25, no data generated 10/10, controlled access 8/10, on request
11/16, in the article 6/9, no statement 9/20, reused only 4/10. These categories are the tool's least reliable
output and are offered as context only. They do not affect which deposits are found or how they are tiered.

159 papers match no category rule. An AI reviewer gave 135 of them a category with high or medium confidence
(`label_source = llm-review`; 154 papers carry such a label in all, including some the current rules would now
decide); 24 remain "needs review". Those labels are a local file and are not published: a run from this
repository alone reports all 159 as "needs review", with the same dataset rows, tiers and receipts.

## 7. Reproducibility

The whole pipeline was run again from a clean copy of the code with an empty cache, downloading everything afresh
from the public sources. It took 64 minutes. Compared with the run reported here:

- Every one of the 1,334 own-deposit rows was reproduced, with the same own-versus-reused decision and the same
  record check.
- Four more papers had open full text by then (1,837 instead of 1,833), which added four Zenodo deposits.
- Eight link checks answered differently on the day (for example, the GWAS Catalog interface was intermittently
  unavailable, giving "unverified"), which moved four rows between tiers 2 and 3.

The rules are deterministic; what changes between runs is the outside world: papers become open, records are
released, and services have bad days. Link checks are point-in-time, and "unverified" is never counted as dead.
An earlier clean-room run of an earlier version of the code matched its cached run with no extraction differences
across 1,833 papers.

## 8. Errors found so far, and what guards against them

Every error class below was found by an audit, a code review or by tracing awards by hand, and has a regression test
in `tests/`:

- reused GEO series read as own when the statement used plain availability wording (now checked against the
  repository record);
- availability wording read as reuse: "can be accessed via GitHub", "publicly available in the dbGaP repository",
  and the citation in the standard PRIDE deposit sentence;
- "data generated by the TARGET initiative" read as the paper's own;
- citation superscripts fused onto accessions; Unicode hyphens; lower-cased URLs; unversioned Code Ocean and
  Mendeley DOIs; private MassIVE and PRIDE records reported as missing;
- a group's earlier deposit promoted to "own" because its submitters are the paper's authors (now: no verdict when
  the record is more than a year older than the paper).

Known and not fixed: dbGaP (section 3); repositories without a readable record; papers whose own text is wrong
about which data are new.

## Earlier audits

An earlier audit of 60 own deposits outside GEO, SRA and dbGaP (seed 11), on an earlier version of the rules, had
both reviewers confirm 53 of 60 and agree on 57 of 60. Four of the seven not confirmed were tool errors on reused data; the record check now
catches one of them. It is kept for the record; section 1 supersedes it.
