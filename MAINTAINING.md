# Maintaining Output Receipts

This guide is for whoever takes over the code. You should not need to contact the author: everything needed to run,
fix, extend and re-validate the tool is here or in the files it points to. Start with `README.md` (what it does),
then this file (how it works and how to change it), then `VALIDATION.md` (how accurate it is).

**Requirements:** Python 3.10 or newer, standard library only. No packages, no credentials, no database.
**Tests:** `python -m unittest discover tests` (69 tests, under a second). Run them after any rule change.

## 1. Repository layout

| Path | What it is |
|---|---|
| `receipts/cli.py` | Entry point and pipeline order (`run`, `reclassify`, `sample`). Read `cmd_run` top to bottom: it is the whole pipeline. |
| `receipts/fetch.py` | Cached, rate-limited HTTP client (`Http`) and the RePORTER / Europe PMC / PMC wrappers. |
| `receipts/extract.py` | Parses article XML (JATS), finds the availability statement, finds identifiers, decides own vs. reused. |
| `receipts/classify.py` | Turns what `extract.py` found into one label per paper, plus flags. |
| `receipts/resolve.py` | Checks that each identifier exists publicly (one function per repository). |
| `receipts/records.py` | Reads what the repository's own record says (papers cited, grants named, submitters, release date) and compares it with the paper. |
| `receipts/crosscheck.py` | Applies the record check to the roles `extract.py` gave, and groups accessions into studies. |
| `receipts/report.py` | Writes every output file and the HTML dashboard. |
| `validate/` | Accuracy and comparison scripts (section 6; file-by-file map in `validate/README.md`). Optional; the pipeline never calls them. |
| `site/build.py` | Builds what is published from a run's full local outputs: `index.html` (project page), `receipts.html` (receipts browser) and `public/` (candidate rows, receipts, summary). Re-run it after any new run. |
| `FLOWCHART.md` | The process as two diagrams. Update diagram 2 if the decision order in section 3 changes. |
| `HOW_IT_WORKS.md` | Plain-language explanation for non-developers. Update it if a rule changes what a label means. |
| `tests/` | Rule tests, including a regression test for each misfire that was fixed. |
| `data/cache/` | Every HTTP response, gzip-JSON, keyed by URL (about 64 MB for one cohort). Not committed; rebuilt on demand. |
| `data/work/` | Intermediate JSON: `grants.json`, `papers.json` (includes each paper's extraction), `resolution.json`, `geo_pubmed_links.json`, `run_info.json`. Not committed. |
| `data/review/` | `needs_review.jsonl` (papers the rules could not decide) and `llm_review_labels.csv` (labels for them, if a reviewer supplies any). Local; not published. |
| `data/output/` | The full local outputs of a run, including links that do not resolve and per-paper categories. Not published. |
| `public/`, `index.html`, `receipts.html` | The published layer (see `README.md`, "What it produces"): deposits that were found and whose link works, and aggregate numbers. |

## 2. How the pipeline works

`python -m receipts run --fy 2024 --activity R01 --ic NCI` runs these steps (all in `cli.cmd_run`):

1. **Awards.** `fetch.reporter_projects` queries NIH RePORTER for the cohort (fiscal year, activity code, administering
   IC, award type 1 = new).
2. **Papers.** `fetch.reporter_publications` gets RePORTER's award-to-PMID links. Papers published more than 365 days
   before the award start are dropped (the same rule the INS pipeline uses).
3. **Metadata.** Europe PMC, with PubMed `esummary` as a fallback (mostly preprints).
4. **Full text and extraction.** `fetch.fetch_fulltext` gets JATS XML from Europe PMC or the PMC Cloud Service
   (S3 open data). `extract.parse_jats` turns it into passages (paragraphs, table rows, references, with section
   headings); `extract.analyze` finds the availability statement, the identifiers and their roles.
5. **Resolution.** `resolve.resolve_all` checks each identifier a paper presents as its own or leaves unclear (one
   worker per host, at most 1 request per second per host), plus GEO series that are "reused" only by default.
   `resolve.geo_pubmed_links` records NCBI's PubMed-to-GEO links.
6. **Record cross-check.** `records.warm` reads the repository's own record for each own, unclear or
   default-reused identifier (`records.py`: GEO, BioProject, ProteomeXchange, PDB, EMDB, and DOI repositories
   through DataCite). `crosscheck.apply` then compares each record with the paper, from the cache only: see
   section 3. It runs again on every `reclassify`, so older work directories get it too.
7. **Classification and reports.** `classify.classify` labels each paper (rules first, then any review label from
   `data/review/llm_review_labels.csv`); `report.write_all` writes the outputs.

A cold run of one fiscal-year R01 cohort takes about an hour (64 minutes for FY2024; polite rate limits); a re-run from cache takes about
five minutes. `python -m receipts reclassify` re-applies rules and review labels to `data/work/` with no network.
`--offline` runs from the cache only (lookups that were never cached are skipped, so prefer a normal run).

## 3. How a decision is made (where to look when a label is wrong)

For any paper, `data/output/papers.csv` shows the label, `label_source` (`rule` or `llm-review`), the rule's reason,
the statement excerpt, and each identifier with its role and link status. The full evidence is in
`data/work/papers.json` under the paper's PMID: `extraction.identifiers[*]` has `repo`, `id`, `role`
(`generated` = own, `reused`, `unknown`), `locations` (`das` = inside the availability statement) and `evidence`
(which cue decided the role).

- **Identifier found or not:** `extract.ID_PATTERNS` (repository name, class, regex). Class `public`/`controlled`/
  `code`/`generic` decides which label an own deposit produces.
- **Own vs. reused, one mention** (`extract.mention_role`), in this order:
  1. Key-resources table row: "this paper/study" means own; a cited source (year, "ref", reuse cue) means reused.
  2. `PRIOR_DATA` ("generated for a previous study", "previously deposited/published ... data") means reused, unless
     the sentence also says data were generated in this study (`GEN_THIS`).
  3. Nearest cue in the sentence: `GEN_CUES` (own) against `REUSE_STRONG` / `REUSE_RESOURCES` (reused) and
     `REUSE_WEAK` (reused, counted 40 characters farther away). An own cue beats a sentence with only weak reuse
     phrasing such as "from the Sequence Read Archive". Own cues name *this/our/the present/the current*
     study; "described in the article" (someone else's) is not one.
  4. No cue: in the statement, "analysed in/during this study" (`ANALYSED_THIS`, used for both reuse and own
     deposits) gives `unknown`; a key-resources row with no source gives `unknown` (code rows are tools: reused);
     other availability wording in the statement (`GEN_WEAK`: "are available in", "accession number") gives own.
  5. Preceding sentence: inside the statement only, an own cue there gives own; a reuse cue gives reused anywhere.
- **Own vs. reused, one identifier** (end of `extract.analyze`): mentions in the statement or key-resources table
  outrank mentions elsewhere; among those, any own mention wins, then reused, then unknown. With no decided mention:
  in the statement, the statement-level cues decide (only own cues: own; only reuse cues: reused; else `unknown`);
  a table row saying "this paper/study" gives own; any other body mention is **reused by default**. (A body mention
  is never made own just because the statement names the same repository.)
- **Record cross-check** (`records.py` + `crosscheck.py`, after resolution). `records.record` returns what the
  repository record says: the papers it cites (PubMed IDs, DOIs), the grants it names, its submitters and its
  release date. `records.check(paper, record)` gives a tier: `cites_paper`, `names_award`, `authors` (a submitter
  is an author of the paper), `contradicted` (public more than 12 months before the paper and citing other papers;
  or by other people and either old or tied to another paper), or empty (no verdict). A contradicted own/unclear
  identifier becomes `reused`; a confirmed unclear identifier becomes own; a default-reused body mention becomes
  own only on `cites_paper` or `names_award`. The tier is kept on the identifier (`record_check`, `record_note`),
  the original role in `role_before_crosscheck`. No cached record, or a repository without a readable record
  (GitHub, dbGaP, EGA, MassIVE and others): no change. To add a repository, write one parser in `records.py` that
  fills the same fields, add it to `RECORD_REPOS`, and add a test with a shortened real response.
- **Text normalisation** (`extract._flat`): superscripts and citation links are separated from neighbouring text
  ("GSE51800<sup>41</sup>" must not read as GSE5180041) and Unicode hyphens become "-".
- **Statement cues:** `ON_REQUEST`, `IN_ARTICLE`, `NO_DATA`, `WILL_DEPOSIT` in `extract.py`.
- **Label order:** `classify.rule_label`, read top to bottom; the first matching rule wins. Papers that match no rule
  get `NEEDS_REVIEW` and go to `data/review/needs_review.jsonl`.

## 4. Common tasks

**Run another cohort.** Change the flags: `--fy`, `--activity` (any NIH activity code, e.g. U01, P01), `--ic`,
`--award-type`. Use `--data-dir data_u01_2024` to keep cohorts apart. `--limit-grants 10` is a quick smoke test.

**Add a repository.**
1. Add a line to `extract.ID_PATTERNS`: `("Name", "public"|"controlled"|"code"|"generic", regex, group)`. If the
   identifier has several written forms, normalise it in `extract._canon`.
2. Add a check to `resolve.resolve_one` (usually one `_api(...)` call against the repository's public API) and a
   landing page to `resolve.human_url`. Without a check, the identifier is tried as a URL and may be reported wrongly.
3. Add a test in `tests/test_extract.py` with a realistic sentence, then run the tests and the pipeline.

**Fix a misclassification.**
1. Find the paper in `papers.csv`, then its `extraction` in `data/work/papers.json` to see which cue decided it.
2. Adjust a cue list in `extract.py` or the rule order in `classify.rule_label`.
3. Add a regression test that reproduces the case.
4. Run the tests, then `python -m receipts reclassify` (for classification-only changes) or a normal `run`
   (for extraction changes, which need re-parsing; it uses the cache).
5. Run `python validate/rescore.py validate/out/answers.jsonl validate/out/answers_fresh.jsonl` (your own saved
   answer files from earlier `ai_judge.py` audits; this prototype's are not published) to see which audited papers
   your change fixed or broke, at no cost.

**Label the papers the rules cannot decide.** Each line of `data/review/needs_review.jsonl` has the statement and the
reason. Put one row per paper in `data/review/llm_review_labels.csv` (`pmid,label,mixed,code_shared,note`; labels as
in `HOW_IT_WORKS.md`), by any reviewer, human or model, then run `reclassify`. Those papers are reported with
`label_source = llm-review`. `validate/ai_judge.py` does this with a model (see section 6).

**Publish a new run.** After `run`, run `validate/ins_compare.py` (needs the INS tables) and then
`python site/build.py`, which rebuilds `index.html`, `receipts.html` and `public/` from the run's outputs. Commit
those together so the pages, the files and the numbers always match. What is published is positive and aggregate on
purpose: deposits whose link works, with their evidence and tier. Links that do not resolve and the per-paper
categories stay in the local outputs, for the curator and the investigator. `--example` chooses the award whose
receipt the project page shows without names. `.gitignore` lists what stays local (`data/output/`, `data/review/`
and all of `validate/out/` except the INS comparison, benchmark, scale and attribution reports).

**Re-check links over time.** Delete the resolver responses from the cache (the folders under `data/cache/` named for
repository hosts) or use a fresh `--data-dir`, then run again. Link checks are definitive answers and are cached, so a
re-check needs those entries removed.

## 5. Output for the Index of NCI Studies

`data/output/ins_candidate_datasets.tsv` uses INS-Data's own dataset-table column names for the columns it can fill:

| Column | Meaning |
|---|---|
| `type` | `dataset` for data; `resource` for code deposits (candidates for INS's resources table) |
| `dataset_uuid` | Deterministic UUID5 of `repository|accession`, so re-runs give the same IDs |
| `dataset_source_repo`, `dataset_source_id`, `dataset_source_url` | Repository, accession, landing page |
| `dataset_pmid`, `funding_source` | Papers reporting the deposit; core project numbers of their awards (`;`-separated) |
| `link_status` | Resolver result: `RESOLVES`, `NOT_FOUND`, `PRIVATE`, `RESTRICTED`, `UNVERIFIED`, `SKIPPED_API_DOWN`, `NOT_CHECKED` |
| `is_code`, `harvestable_via_ncbi_links` | Extra columns: whether it is code; for GEO, whether NCBI's PubMed link exists |
| `record_check`, `record_note` | Extra columns: the strongest record evidence among the papers reporting the deposit ("record cites this paper", "record names this award", "record is by this paper's authors", "paper only"), and the sentence saying why |
| `triage_tier` | Extra column: 1 = the link works and the record check confirms the deposit; 2 = the link works and the availability statement states the deposit explicitly (an own-deposit cue in the sentence); 3 = everything else. Set in `report.write_ins_candidates`. |
| `study_group` | Extra column: the study the accession belongs to (`repo:accession`), from `crosscheck.study_groups`: a GEO SubSeries points to its SuperSeries, SRA runs/experiments to the one BioProject cited with them, EGA datasets to the one EGA study cited with them. Rows stay accession-level; `summary.json` gives both counts (`own_data_accessions_unique`, `own_data_studies_unique`). |

Descriptive INS columns (`dataset_title`, `description`, `assay_method`, `primary_disease` and others) are left to
INS's existing enrichment and curation. Repository names differ slightly from INS's (`SRA/BioProject` here, `SRA` in
INS; `PRIDE/ProteomeXchange` here). Map them at ingestion.

`validate/ins_compare.py --ins-dir PATH` compares a run with INS-Data's published tables and writes
`validate/out/ins_missing_datasets.tsv` (the candidate rows INS does not list). Download the tables from
`github.com/CBIIT/INS-Data` under `data/02_output/` (grant, publication, geo, sra, dbgap and resources files).

## 6. Measuring accuracy again

| Script | What it does |
|---|---|
| `validate/ai_judge.py` | A fresh model session reads each paper's availability passages and labels it, never seeing the tool's label. Labels the review queue and/or audits a stratified sample (`--audit 100 --seed N`; `--exclude` earlier answer files to draw a fresh sample). |
| `validate/id_audit.py` | Blind check of a sample of candidate rows: is the deposit the paper's own? `--population validate/out/ins_missing_datasets.tsv --strata tier1=50,tier2=40,tier3=40` samples by tier and reports each tier and a population-weighted rate; `--sample-from` gives a second reviewer the same rows. |
| `validate/compare_reviewers.py` | Two reviewers on the same rows: agreement, and the share both confirm, overall and per tier. |
| `validate/ins_benchmark.py` | Runs the rules on papers INS already lists and compares with the datasets INS attributes to them (no model). |
| `validate/rescore.py` | Re-scores saved audit answers against the current rules (no model calls). |
| `validate/human_check.py` | Builds a review page from a local run: each item shows the sentence containing the identifier, with one-click answers and a timestamp per decision. `--rows FILE --n 200` builds a simple random curator sample from any candidate file; `--score FILE` compares pasted answers with a saved audit file. |
| `validate/attribution.py`, `validate/scale.py` | Award attribution and portfolio-scale figures. |

Every model call goes through `ask_model` in `validate/ai_judge.py`. Pass `--cmd` to use any model: a command
that reads the prompt on stdin and prints the answer. `validate/adapters/` has two ready-made ones (Codex command line;
any OpenAI-compatible endpoint, including local open-weight servers). Prompts, answer formats and tested models are
documented in `AI_REVIEW.md`.

## 7. Operational notes

- **Politeness and terms.** Never lower `--interval` below 1 second. NCBI allows up to 3 requests/second without a
  key; RePORTER asks for large jobs off-peak. Terms for each source are in `README.md`.
- **Outages.** If a resolver API returns errors repeatedly, its remaining identifiers are recorded as
  `SKIPPED_API_DOWN` and listed in `summary.json` under `run.api_notes`; re-run later to fill them in (cached answers
  are reused). `UNVERIFIED` means the site did not give a definitive answer; it is never counted as dead.
  The dead-link rate and the per-paper `LINK_DEAD` flag use the same set: the paper's own repository identifiers
  (`report.own_identifiers`). Other web links found in statements are reported separately (`own_url_status` in
  `summary.json`) and never flagged. Papers still `NEEDS_REVIEW` after review are counted as their own bucket
  (ambiguous), so the label shares add up to 100%.
- **Security.** Downloaded content is parsed as XML or JSON and never executed. The tool writes only under the data
  directory. No secrets are used or stored.
- **Determinism.** Rules and outputs are deterministic for a given cache. Random samples in `validate/` take fixed seeds.

## 8. Questions an adopting team is likely to ask

**How is a dataset attributed to an award?** Through NIH RePORTER's publication links (the investigator-reported
award-paper links), with INS's 365-day rule. A paper linked to several awards in the cohort associates its datasets
with each of them. `validate/attribution.py` reports how common that is (88% of papers in the FY2024 R01 cohort).

**Is AI required?** No. The labels come from the deterministic rules in `classify.py`. In this prototype a model
labelled the papers the rules could not decide at the time (a human can label those instead; any label in the
review file overrides the rules) and served as the audit reviewer.

**What does it miss?** Papers without open full text (12% of the FY2024 R01 cohort), deposits listed only in
supplementary files, links on lab websites, and identifiers beyond the first 25 per repository in one paper. See `README.md`, Limitations.

**How accurate is it?** See `VALIDATION.md`: per tier, against INS's own records, and per paper category.

**Can it run inside NCI?** Yes: standard-library Python, outbound HTTPS to public APIs and to the links named in the papers, about 70 MB of disk per
cohort including the cache.

**How do I add a data type or repository?** Section 4. Most additions are one regex, one resolver line and one test.
