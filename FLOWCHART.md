# Output Receipts: process at a glance

Two diagrams: what the tool does (top), and how each identifier is judged "own" or "reused" (bottom).
Plain-language details are in `HOW_IT_WORKS.md`; code-level details in `MAINTAINING.md`.

## 1. The pipeline

```mermaid
flowchart TD
    A["NIH RePORTER<br/>awards for a cohort<br/>(e.g. NCI FY2024 new R01s: 589)"] --> B["Award to paper links<br/>(investigator-reported, as INS uses)"]
    B --> C{"Paper published more than<br/>1 year before the award started?"}
    C -- yes --> X1["Excluded<br/>(INS's own rule)"]
    C -- no --> D["Papers in scope<br/>(2,086)"]
    D --> E{"Open full text in<br/>PubMed Central / Europe PMC?"}
    E -- no --> X2["Reported as<br/>NOT CHECKABLE"]
    E -- yes --> F["Parse the article XML<br/>(paragraphs, tables, sections)"]
    F --> G["Find the data availability statement<br/>(headings like 'Data availability')"]
    F --> H["Find identifiers by shape<br/>GSE..., PRJNA..., phs..., EGA..., PXD...,<br/>DOIs, github.com/...  (25+ repositories)"]
    G --> I
    H --> I["Judge each identifier from the paper's wording:<br/>OWN output or REUSED data?<br/>(diagram 2, parts A and B)"]
    I --> J["Ask each repository:<br/>'does this record exist and is it public?'<br/>and 'which paper, award and people does it name?'<br/>(diagram 2, part C)"]
    J --> K["Label each paper<br/>(public repository, controlled access, on request,<br/>in article, reused only, no data, no statement)"]
    K --> L{"Rules undecided?<br/>(about 8%)"}
    L -- yes --> M["NEEDS REVIEW<br/>(a person, or optionally an AI model)"]
    L -- no --> N
    M --> N["Outputs"]
    N --> O1["INS candidate rows<br/>(INS dataset-table columns)"]
    N --> O2["Receipt per award"]
    N --> O3["Per-paper table"]
    N --> O4["Summary + dashboard"]
    O1 --> P["Compare with INS's public tables<br/>(validate/ins_compare.py)"]
    P --> Q["Rows INS does not list yet<br/>= candidates for curation"]

    classDef ext fill:#eef4fb,stroke:#0b5cad;
    classDef out fill:#eefaf0,stroke:#2e7d32;
    classDef drop fill:#fbeeee,stroke:#b23b3b;
    class A,B ext;
    class O1,O2,O3,O4,Q out;
    class X1,X2 drop;
```

No AI runs anywhere in this pipeline. The optional AI step is only "NEEDS REVIEW" (a person can do it) and the
separate accuracy audits in `validate/`.

## 2. Own or reused? (one identifier in one paper)

An identifier can be mentioned several times in a paper. Each mention is judged from its own sentence (part A), the
mentions are combined into one answer per identifier (part B), and the answer is then compared with what the
repository's own record says (part C). Parts A and B read the paper; part C reads the repository.

```mermaid
flowchart TD
    S["A. ONE MENTION<br/>an identifier in one sentence"] --> R0{"In the reference list?"}
    R0 -- yes --> mREU["mention says REUSED"]
    R0 -- no --> PR{"Sentence says the data come from<br/>an earlier study?<br/>('generated for a previous study')"}
    PR -- yes --> mREU
    PR -- no --> CUE{"Cue phrases in the sentence:<br/>which is nearest the identifier?"}
    CUE -- "own cue<br/>('generated in this study',<br/>'have been deposited in')" --> mOWN["mention says OWN"]
    CUE -- "reuse cue<br/>('downloaded from', 'publicly<br/>available data', 'previously<br/>published', TCGA, 'et al.')" --> mREU
    CUE -- "no cue" --> WHERE{"Where is the mention?"}
    WHERE -- "availability statement:<br/>plain 'are available in GEO under ...'" --> mOWN
    WHERE -- "availability statement:<br/>'data analysed in this study ...'" --> mUNK["mention is UNCLEAR"]
    WHERE -- "key-resources table row:<br/>says 'this paper'" --> mOWN
    WHERE -- "key-resources table row:<br/>cites a source or year" --> mREU
    WHERE -- "key-resources table row:<br/>neither" --> mUNK
    WHERE -- "body text" --> mNONE["no opinion"]

    mOWN --> B
    mREU --> B
    mUNK --> B
    mNONE --> B
    B["B. COMBINE THE MENTIONS<br/>statement and table mentions outrank body text"] --> B1{"Any deciding mention says OWN?"}
    B1 -- yes --> OWN["OWN"]
    B1 -- no --> B2{"Any says REUSED?"}
    B2 -- yes --> REU["REUSED"]
    B2 -- no --> B3{"Any UNCLEAR?"}
    B3 -- yes --> UNK["UNCLEAR"]
    B3 -- "no opinion at all" --> B4{"Listed in the statement?"}
    B4 -- "yes, and the statement only<br/>describes new outputs" --> OWN
    B4 -- "yes, and the statement only<br/>describes reused data" --> REU
    B4 -- "yes, mixed or no cue" --> UNK
    B4 -- "no: body text only" --> REU

    OWN --> C0{"C. THE REPOSITORY'S OWN RECORD<br/>(GEO, BioProject, ProteomeXchange, PDB, EMDB,<br/>Zenodo, Figshare, Dryad and other DOI repositories)<br/>What does the record say?"}
    UNK --> C0
    REU --> C4{"REUSED only by the body-text default,<br/>and the record cites this paper<br/>or names its award?"}
    C0 -- "points to other work:<br/>public more than a year before the paper and cites other papers,<br/>or by other people and old or tied to another paper" --> FREU["REUSED<br/>(not counted)"]
    C0 -- "confirms: cites this paper,<br/>names this award, or a submitter<br/>is an author of the paper" --> FOWN["OWN: counted as the paper's output,<br/>link-checked, written as a candidate row<br/>with its confirmation tier"]
    C0 -- "says nothing either way,<br/>or the repository has no readable record" --> C2{"Was it OWN from the wording?"}
    C2 -- yes --> FOWN2["OWN, tier 'paper only':<br/>rests on the paper's wording alone"]
    C2 -- "no (UNCLEAR)" --> FUNK["UNCLEAR: the paper goes to<br/>NEEDS REVIEW unless it has another own deposit"]
    C4 -- yes --> FOWN
    C4 -- no --> FREU

    classDef own fill:#eefaf0,stroke:#2e7d32;
    classDef reu fill:#fbeeee,stroke:#b23b3b;
    classDef unk fill:#fff8e1,stroke:#b28704;
    class mOWN,OWN,FOWN,FOWN2 own;
    class mREU,REU,FREU reu;
    class mUNK,UNK,FUNK unk;
```

"Nearest cue" has two refinements: an own cue beats vague reuse wording such as "from the Sequence Read Archive" at
any distance, and inside the statement a cue in the sentence just before the identifier's sentence is used when its
own sentence has none. The code is `mention_role` and `analyze` in `receipts/extract.py` (parts A and B), and
`receipts/records.py` with `receipts/crosscheck.py` (part C).
