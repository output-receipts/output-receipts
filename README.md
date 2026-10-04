# Output Receipts

**A discovery aid that connects the data NCI's awards have shared back to NCI's catalog, built only from public data.**

## At a glance

- **In:** a set of NIH awards (here, all 589 new R01 awards NCI made in fiscal year 2024).
- **What it does:** follows each award to its papers, reads what each paper says it deposited (in any of 25+
  repositories), separates the paper's own deposits from data it reused, and checks each deposit against the
  repository's own record: does it exist, is it public, and which paper, award and people does the record name?
- **Out:** candidate rows for the [Index of NCI Studies (INS)](https://studycatalog.cancer.gov) in INS's own column
  format, each with its evidence and a confidence tier, and a "receipt" per award listing what its papers shared.
- **What it is for:** a starting point for curation, not a replacement for it. Checking a candidate that arrives
  with its evidence is faster than searching for it.
- **Runs without AI:** transparent rules and public lookups, standard-library Python, no credentials.

## Start here

| If you want to... | Open |
|---|---|
| See the findings in two minutes | `index.html` (the project page: [output-receipts.github.io/output-receipts](https://output-receipts.github.io/output-receipts)) |
| See the datasets INS does not yet list | `public/candidates_not_in_ins.tsv` (INS dataset-table columns plus evidence and tier; opens in Excel) |
| Browse every award's receipt | `receipts.html` (searchable) |
| See the whole process on one page | [`FLOWCHART.md`](FLOWCHART.md) (two diagrams: the pipeline, and how a deposit is judged) |
| Understand how a decision is made | [`HOW_IT_WORKS.md`](HOW_IT_WORKS.md) (plain language, with real examples) |
| Know how accurate it is | [`VALIDATION.md`](VALIDATION.md) (every measurement, including the weak spots) |
| Run, change or take over the code | [`MAINTAINING.md`](MAINTAINING.md) |
| Understand where AI was used | [`AI_REVIEW.md`](AI_REVIEW.md) |

## What each row says, and how sure it is

The tool separates what it looked up from what it inferred:

- **Looked up (exact; anyone can check in a click):** the record exists and is public; the repository's record cites
  the paper, names the award, or lists a submitter whose name matches an author; NIH RePORTER links the paper to the
  award; INS lists the dataset or does not.
- **Inferred from wording (a heuristic):** whether a deposit is the paper's own or data it reused. The sentence it
  rests on is in every row.
- **Not determined:** papers without open full text are reported as not checkable, never guessed.

Every candidate row carries a tier:

| Tier | What is true of the row | What a curator does |
|---|---|---|
| 1 | The link works and the repository's own record confirms the deposit | Quick check: open the record |
| 2 | The link works and the paper states the deposit explicitly; the record is silent or cannot be read | Read the quoted sentence |
| 3 | The wording is weaker, or the link is dead, private or unverified | Open the paper; route link problems to the investigator |

The counts for this cohort and the measured accuracy of each tier are on the project page and in `VALIDATION.md`.

## What it produces

**Published files** (built by `python site/build.py`; positive and aggregate by design):

| File | What it is | Most useful to |
|---|---|---|
| `public/candidates_not_in_ins.tsv` | Dataset rows INS does not list yet, whose link works: INS-Data's dataset-table columns, plus the evidence sentence, the paper, the record check and the tier. | INS curators |
| `public/candidates_all.tsv` | Every own deposit found whose link works (datasets and code), same columns, with `in_ins`. | INS curators, program staff |
| `receipts.html`, `public/receipts.json` | One receipt per award: what its papers shared, with links. | Investigators, program staff |
| `public/summary.json` | The aggregate numbers shown on the project page. | Reporting |
| `index.html` | The project page. | First-time visitors |

**Local outputs of a run** (`data/output/`, `data/review/`, most of `validate/out/`; not published): the full
candidate table including links that do not resolve, per-award receipts with dead and private links, a per-paper
table with category labels and statement excerpts, a dashboard, and the row-by-row answers of the reviewers used
to check accuracy. These are the working files for a curator, or for an investigator who wants to fix a
link. They are kept out of the public files so that nothing here reads as a list of problems by award.

Sentences are quoted from the papers as published. Where a paper printed the access token or password it gave
its reviewers, the published files show `[removed]` in its place.

## How it works

1. **Awards to papers** through NIH RePORTER's award-publication links, with INS's rule excluding papers published
   more than a year before the award started.
2. **Papers to open full text** from PubMed Central and Europe PMC.
3. **Statement and identifiers:** the data availability statement and accession numbers by their fixed shapes.
4. **Own or reused**, judged from the sentence each identifier appears in ("generated in this study... deposited
   in" versus "downloaded from", "previously published", TCGA).
5. **Link and record check** against each repository's public interface: the record exists and is public, and it
   cites the paper, names the award or lists an author as submitter. A record that points to other work overrides
   the sentence.
6. **Outputs** as above, each row with its tier.

[`FLOWCHART.md`](FLOWCHART.md) shows this as two diagrams; [`HOW_IT_WORKS.md`](HOW_IT_WORKS.md) walks through each
step with real sentences from this cohort.

## Benefits and limitations

**Benefits**
- Finds deposits in any repository, read from the authors' own statements, including the many that repository
  metadata does not tie to a grant.
- Gives curators rows with evidence (the paper, the sentence, the link status, the record check) and a tier that
  says how much checking each row needs.
- Transparent and repeatable; free to run; works for any NIH Institute, activity code or year.
- Documented for takeover: maintainer's guide, regression tests, and validation scripts that re-measure accuracy.

**Limitations**
- **Coverage:** reads only open full text (most of this cohort's papers, rising under NIH's 2024 Public Access
  Policy); does not open supplementary files or lab websites.
- **Attribution:** papers are tied to awards by RePORTER, as in INS; most papers cite several awards, so a dataset is
  *associated with* each award its paper cites. That is not proof of which award paid for it.
- **Wording:** own-versus-reused is a judgment from the paper's sentences; tier 3 is where it is weakest.
- **Records:** some repositories (dbGaP, EGA, MassIVE, GitHub and others) do not yet expose a record the tool can
  compare, so their rows rest on the paper's wording.
- **Categories:** the per-paper categories ("on request", "in the supplement" and so on) are the least reliable
  output and are context only. See `VALIDATION.md`.
- **Link checks** are point-in-time; "resolves" means the record exists, not that it holds what was promised.
- **Findability, not compliance:** data management and sharing plans are not public, so the tool never judges
  compliance. A paper whose data are available on request may be fully consistent with its plan.

## Use of AI

This prototype was built with the help of an AI coding assistant (Anthropic's Claude), which wrote most of the
code and documentation under the author's direction. **Running the tool requires no AI.** Identifier extraction,
own-versus-reused decisions, link and record checks, tiers, the candidate rows and all counts are produced by rules
and public lookups (`receipts/`), with no model involved at run time. In this prototype's results, AI was used for
two tasks; neither is required, and a person can do both: (1) labelling the category of the papers the rules cannot
categorize (`label_source = llm-review`; this does not affect the dataset rows), and (2) measuring accuracy with
blind reviewers, here two AI models from different developers (`VALIDATION.md`). Prompts and adapters for any model
are in `AI_REVIEW.md`.

## Running it

Python 3.10 or newer, standard library only; nothing to install.

```
python -m receipts run --fy 2024 --activity R01 --ic NCI   # full pipeline (about an hour or two cold, minutes when cached)
python -m receipts run --limit-grants 10                   # quick test on 10 awards
python -m receipts reclassify                              # re-apply rules and record checks, no network
python -m unittest discover tests                          # rule tests, including audit regressions
python validate/ins_compare.py --ins-dir PATH              # compare with INS-Data's public tables
python site/build.py                                       # rebuild index.html, receipts.html and public/
```

Options: `--award-type` (default 1 = new awards), `--interval` (seconds between requests to the same host, minimum
1), `--skip-resolve`, `--offline` (cache only), `--data-dir`. See `MAINTAINING.md` for everything else.

## Relation to the Index of NCI Studies (INS)

The INS pipeline ([CBIIT/INS-Data](https://github.com/CBIIT/INS-Data)) links ODS-curated programs to grants,
publications and datasets in GEO, SRA and dbGaP (through NCBI's links) and other curated sources, followed by expert
curation. Output Receipts is designed as a complementary discovery step: it reads the papers themselves, recognizes
deposits in any repository, separates new from reused data, checks each deposit against its repository record, and
hands curators candidate rows in INS's own format. It does not replace INS's pipeline or curation.

## Data sources and terms

All sources are public and used without login, at 1 request per second or slower per host.

* **NIH RePORTER API** (api.reporter.nih.gov): public NIH award and publication-link data. NIH asks API users to
  limit request rates and run large jobs off-peak.
* **Europe PMC REST API** (EMBL-EBI): article metadata and open-access full text. Article text remains under each
  article's license; Europe PMC's terms of use apply.
* **PMC Cloud Service** (NCBI, AWS Open Data `pmc-oa-opendata`): JATS XML for the PMC open-access subset and for
  NIH author manuscripts released for text and data mining. Each article's license applies.
* **NCBI E-utilities, GEO and the dbGaP study API**: NCBI usage policies apply (no more than 3 requests per second
  without an API key; this tool uses at most 1).
* **Repository interfaces** (existence and record checks): ProteomeXchange, MassIVE, EGA, EMBL-EBI
  BioStudies/MetaboLights/EMDB, GWAS Catalog, RCSB PDB, Metabolomics Workbench, NCI PDC and GDC, Sage Synapse, OSF,
  the doi.org handle API, the DataCite REST API, GitHub/GitLab/Bitbucket (HTTP HEAD on public repository pages).
  Each service's terms apply.

The candidate rows quote the one sentence (300 characters or fewer) in which a paper reports a deposit, so that
each row can be verified; copyright in the article text stays with its rights holders.

## License

Code: Apache License 2.0 (see `LICENSE`). Generated data files are derived from the public sources above and are
offered under the same terms as those sources.
