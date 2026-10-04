# How Output Receipts works

A plain-language walkthrough for readers who are not developers. The same process as two diagrams is in
`FLOWCHART.md`. For code-level detail see `MAINTAINING.md`; for
accuracy see `VALIDATION.md`.

## The problem it solves

NIH asks funded researchers to share their data, and NCI's Index of NCI Studies (INS) aims to show, in one place,
what NCI's investment produced. But there is no central list of which datasets each grant created. Repository
records rarely say which grant paid for them (just 1% of datasets published since 2015 carry both a funder and a
grant number, per an OpenAlex analysis of DataCite's metadata, July 2026). The connection is often written down in
the paper. Many journals now require a short **data availability statement**, for example, from this cohort:

> "The RNA-sequencing data used in this study has been deposited (GEO: GSE221112)."

Reading thousands of these by hand is slow, but they are formulaic: authors reuse standard phrases, and dataset
identifiers have fixed formats. Output Receipts reads them automatically.

## The six steps

1. **Awards to papers.** NIH RePORTER lists the papers each award's investigators have linked to it. Papers
   published more than a year before the award started are dropped, the same rule INS uses.
2. **Papers to full text.** Open-access papers are available as structured text (JATS XML) from PubMed Central and
   Europe PMC, with every paragraph tagged by section. Papers without open full text are reported as
   "not checkable", never guessed.
3. **Find the statement and the identifiers.** The tool looks for sections headed "Data availability",
   "Data and code availability", "Availability of data and materials" and similar, and for identifiers by their
   shape: GEO series look like `GSE` plus digits, SRA projects like `PRJNA` plus digits, dbGaP studies like `phs`
   plus six digits, code like `github.com/owner/repo`, and so on for more than 25 repositories.
4. **Own or reused?** The same kind of identifier can be the paper's own deposit or someone else's data it
   analysed, so each mention is judged from its sentence (step-by-step examples below).
5. **Link and record check.** Every identifier a paper presents as its own is looked up in the repository's public
   interface: does it exist and is it public? For the repositories whose records the tool reads: which paper, award
   and people does the record itself name?
6. **Outputs.** Candidate rows for INS, a receipt for each award with a deposit, a table per paper, and a dashboard.

## How "own or reused" is decided: real examples from this cohort

The tool looks for cue phrases near each identifier. Cues for **own** include "generated in this study",
"have been deposited in", "we deposited", "data reported in this paper". Cues for **reused** include
"downloaded from", "obtained from", "publicly available datasets", "public scRNA-seq data", "previously published",
"generated in prior studies", "re-analysed", and names of large public resources such as TCGA, GTEx or DepMap.
Wording about how a reader gets the data ("can be accessed via GitHub", "publicly available in the dbGaP
repository") is not a reuse cue, and neither is a citation of the repository itself ("via the PRIDE (Vizcaino et al.,
2013) partner repository"); "were accessed from GEO", in the past tense, says what the authors did and is one.
The decision, in order:

1. **A sentence that says the data come from an earlier study** ("generated for a previous study, and deposited in
   GEO") is reuse, even if a deposit verb sits closer to the identifier, unless it also says data were generated in
   this study.
2. Otherwise **the cue nearest the identifier wins**, except that an explicit own-deposit cue outranks a vague phrase
   such as "from the Sequence Read Archive".
3. **No cue at all.** Inside the availability statement, plain availability wording ("are available in GEO under
   accession ...") counts as own, because that is what statements are for. Two phrasings are left **unclear**
   instead: "data analysed (or used) in this study are available in ..." (authors use it both for their own deposits
   and for public data they analysed) and a key-resources table row that names no source. Outside the statement, an
   identifier with no cue counts as **reused**: a series in a methods paragraph, figure legend or reference table is
   not a new output just because the statement mentions the same repository. A table row that says "this study" or
   "this paper" counts as own.
4. **A statement outranks the methods.** If the statement says an identifier is reused, a passing mention in the
   methods cannot make it own, and the reverse.
5. **The repository's own record has the last word.** Steps 1 to 4 read the paper. The tool then reads the record
   in the repository (for GEO, BioProject, ProteomeXchange/PRIDE, PDB, EMDB, and DOI-based repositories such as
   Zenodo, Figshare and Dryad) and compares it with the paper:
   - the record **cites this paper**, **names the award**, or lists one of the paper's authors as a submitter:
     the deposit is confirmed, and an unclear identifier becomes **own**;
   - the record became public more than a year before the paper and cites other papers, or it is by other people
     and is old or tied to another paper: the identifier is **reused**, whatever the sentence said;
   - the record says nothing either way: the call rests on the paper's wording, and the row is marked
     "paper only".

   Each candidate row carries this as its `record_check`, so a curator can see at a glance which rows the
   repository's record supports.

| The paper says | Decision |
|---|---|
| "The EM-seq whole genome methylation data **generated in this study** are publicly available from the Sequence Read Archive (SRA) under accession number PRJNA1145324." | **Own** deposit in a public repository |
| "Stemness signatures ... were **obtained by analyzing publicly available** single-cell RNA-seq data ... (dbGaP phs001988)" | **Reused**: someone else's data, not counted as this paper's output |
| "The RNA-sequencing data used in this study **has been deposited** (GEO: GSE221112)." | **Own** (an earlier version of the rules missed this phrasing; the audits caught it, and a test now guards it) |
| "The data underlying this article are available on request from the corresponding author." | No deposit: **available on request** |
| "Source data are provided with this paper. All other data are available from the corresponding author upon reasonable request." | **In the article**: some data are openly in the paper, so it is not "only on request" |
| "Public scRNA-seq data used in this study are available from GEO under the following accession codes: GSE141445 ..." | **Reused** ("public ... data"; GEO also links GSE141445 to an earlier paper) |
| "The DNA methylation data used in this study were generated for a previous study, and have been deposited in the NCBI Gene Expression Omnibus database under accession number GSE197674." | **Reused**: an earlier study's data, although "deposited in" is closer |
| "RNA-seq, PRO-seq, ATAC-seq, and Cut&Run data were generated for this study and can be accessed on the Gene Expression Omnibus (GEO) database with accession numbers GSE305144 ..." | **Own** ("generated for this study"; "can be accessed on" only says how to get it. An earlier version read it as reuse; a test now guards it) |
| "The single-cell RNA sequencing datasets analyzed during this study are publicly available from GEO under accession numbers GSE149614 and GSE189903." | **Unclear**, not counted as own (in this paper, GSE149614 has been public since 2020 and belongs to other papers). For GEO series, GEO's record then decides when it can |

## The label each paper gets

After the identifiers are judged, each paper gets one label. The rules are applied in a fixed order and the first
match wins:

| Label | Meaning |
|---|---|
| Public repository | New data or code from this paper in an open repository, with an identifier |
| Controlled access | New data in a controlled-access repository (dbGaP, EGA, and similar) |
| Available on request | New data only available by asking the authors |
| In the article | Data in the paper or its supplementary files, no repository deposit |
| Reused only | The paper only analyses data created by others |
| No data generated | The statement says so, or the article is a review, editorial or comment (also recognised from titles such as "A Review") |
| No statement | Open full text, but no availability statement or deposit found |
| Not checkable | No open full text |

About 8% of papers match no category rule cleanly (for example, an unusual link or a promise to deposit later).
They are marked **needs review**. In this prototype an AI model gave most of them a category, with a one-line reason
each and `label_source = llm-review`; a person can do the same, and the tool runs without either. These categories
describe the paper; they do not change which deposits are found or how they are tiered.

## What the link check reports

| Status | Meaning |
|---|---|
| Resolves | The link resolves: the record, or for DOI-based repositories its registered DOI, exists and is public. Controlled-access studies in dbGaP and EGA with a public study page count as resolving |
| Not found | The repository says there is no such record (often a typo or a withdrawn record) |
| Private | The record exists but is not yet public (for GEO, a release usually still pending after publication) |
| Restricted | Exists, but the page itself is behind an access request |
| Unverified | The site did not give a definitive answer; never counted as dead |

The dead-link rate counts only repository identifiers a paper presents as its own (accessions, DOIs, code
repositories). Other web links in statements (lab pages, portals) are listed and checked, but reported separately.

## What the tool knows, and how sure it is

Every statement the tool makes is one of three kinds, and it says which:

- **Looked up.** The link resolves; the repository's record cites the paper, names the award, or lists a submitter
  whose surname and initial match an author; NIH RePORTER links the paper to the award; INS lists the dataset or
  does not. Anyone can check each one in a click.
- **Inferred from wording.** Whether a deposit is the paper's own or data it reused. This is a judgment from the
  sentence, which is why each published candidate row quotes the paper's text.
- **Not determined.** A paper without open full text is "not checkable"; a paper the rules cannot categorize is
  "needs review". The tool does not guess.

Each candidate row then gets a **tier**, which tells a curator how much checking it needs:

| Tier | What is true of the row | What a curator does |
|---|---|---|
| 1 | The link works and the repository's own record confirms the deposit | Quick check: open the record |
| 2 | The link works and the paper states the deposit explicitly; the record is silent or cannot be read | Read the quoted sentence |
| 3 | The wording is weaker, or the link does not resolve (dead, private, restricted or unverified) | Open the paper; route link problems to the investigator |

How often each tier is right, as measured, is in `VALIDATION.md`.

## What the outputs mean

- **Candidate rows** (published: `public/candidates_not_in_ins.tsv` for datasets INS does not list yet, and
  `public/candidates_all.tsv`): one row per distinct deposit, with its repository, accession, landing page, the
  papers that report it, and the cohort awards RePORTER links to those papers, under INS's own column names; plus a
  quote from the paper (up to 300 characters), the record check and the tier. Descriptive fields (title, assay,
  disease) are not filled in.
  The published files list deposits whose link works; the full table, including links that do not resolve, is a
  local output (`data/output/ins_candidate_datasets.tsv`).
- **Receipt** (one for each award with at least one deposit found; published in `receipts.html` and
  `public/receipts.json`): what the award's papers shared and where. The local version (`data/output/receipts.json`)
  also lists links that do not work; it is meant to be seen by the investigator first, who can fix a dead link or
  release a private record.
- **Per-paper table** (local: `data/output/papers.csv`): every paper's category, the reason, the identifiers found
  and a short excerpt of its statement, so any category can be checked against the paper.

## How a dataset is tied to an award

Through NIH RePORTER's award-paper links, which investigators report. Most papers are linked to several awards
(88% in this cohort), so a dataset is associated with every award in the cohort that RePORTER links to its paper.
That means "associated with", not proof of which award paid for it.

## What it does not do

- It does not judge compliance with a data management and sharing plan. Plans are not public, and data "available
  on request" may be fully consistent with a plan. It reports what a reader can find.
- It reads only open full text, not supplementary files or lab websites.
- It needs no AI to run. Where AI was used in this prototype, and how to use any model or none, is in
  `AI_REVIEW.md`.
