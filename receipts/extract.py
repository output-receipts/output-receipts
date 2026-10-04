"""Turn a JATS full-text XML document into:
  * the data/code availability statement (section-based, or inline sentences when no section exists),
  * repository identifiers with their sentence context, location and a generated-vs-reused role,
  * paper-level cues (on request, in the article, no data, will deposit, controlled access, ...).

Everything here is transparent rules (regular expressions); see classify.py for how the cues become a label.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# ================================================================================================
# 1. JATS -> passages
# ================================================================================================
_XML_ENT = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)[A-Za-z][A-Za-z0-9]*;")
_SKIP_TEXT = {"inline-formula", "disp-formula", "math", "tex-math", "alternatives", "graphic", "inline-graphic",
              "object-id", "label"}
_LINK_TAGS = {"ext-link", "uri", "self-uri", "related-object", "element-citation", "mixed-citation"}
XLINK = "{http://www.w3.org/1999/xlink}href"
# Elements whose text must not run into the neighbouring text: a citation superscript right after an accession
# ("GSE51800<sup><xref>41</xref></sup>") would otherwise become a different accession ("GSE5180041").
_SEPARATE = {"xref", "sup"}
# Unicode hyphens and dashes (U+2010-U+2013) -> "-": "EMD‐73285", "EV‐Library" must match the ASCII forms.
_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-"})


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _flat(el) -> str:
    """Flatten element text; append link targets (xlink:href) that are not already in the visible text."""
    parts = []

    def walk(e, top=False):
        t = _local(e.tag)
        if not top and t in _SKIP_TEXT:
            if e.tail:
                parts.append(e.tail)
            return
        sep = not top and t in _SEPARATE
        if sep:
            parts.append(" ")
        if e.text:
            parts.append(e.text)
        for c in e:
            walk(c)
        href = e.get(XLINK) or (e.get("href") if t in _LINK_TAGS else None)
        if href and t in _LINK_TAGS | {"xref", "named-content", "data-title", "pub-id", "uri"}:
            vis = "".join(e.itertext()).translate(_DASHES)
            if href.strip() and href.strip().translate(_DASHES) not in vis:
                parts.append(f" <{href.strip()}> ")
        if sep:
            parts.append(" ")
        if not top and e.tail:
            parts.append(e.tail)

    walk(el, top=True)
    return re.sub(r"\s+", " ", "".join(parts).translate(_DASHES)).strip()


DAS_HEADING = re.compile(
    r"\b(?:data|code|software|datasets?|sequencing data|data sets?)\b[^.]{0,40}?"
    r"\b(?:availability|available|access(?:ibility)?|sharing|deposition|statement)\b"
    r"|\bavailability of (?:the )?(?:data|supporting data|datasets?|code|materials? and data|data and materials?|"
    r"supporting materials|research data)\b"
    r"|\bdata[- ]sharing\b|\baccession (?:numbers?|codes?|ids?)\b|\bdeposited data\b|\bdata deposition\b"
    r"|\bdata and code\b|\bcode and data\b", re.I)
NOT_DAS_HEADING = re.compile(
    r"^\s*(?:materials?|reagents?|resources?|biological materials?|key resources?(?: table)?|lead contact|"
    r"technical contact|contact for reagent)\b(?:\s+(?:and\s+resource\s+)?(?:availability|sharing|table))?\s*$"
    r"|\blead contact\b|\bmaterials? availability\b(?!.*\bdata\b)", re.I)
DAS_TYPE = re.compile(r"data[-_ ]?avail|availability|data[-_ ]?sharing|code[-_ ]?avail", re.I)
INLINE_DAS_START = re.compile(
    r"^\s*(?:data|code|software|data and code|code and data)\s+(?:and\s+(?:code|materials?)\s+)?"
    r"(?:availability|sharing|access)(?:\s+statement)?\s*[:.\-–—]", re.I)
KRT_HEADING = re.compile(r"key resources?|resources? table|deposited data", re.I)


def parse_jats(xml: str) -> dict:
    xml = re.sub(r"<!DOCTYPE[^>]*(?:\[[^\]]*\])?>", "", xml, count=1)
    xml = _XML_ENT.sub(" ", xml)
    try:
        root = ET.fromstring(xml.encode("utf-8"))
    except ET.ParseError as e:
        return {"error": f"xml parse error: {e}", "passages": []}
    parent = {c: p for p in root.iter() for c in p}

    def ancestors(e):
        while e in parent:
            e = parent[e]
            yield e

    def heading_chain(e):
        """Nearest-first list of (heading_text, type_attr, tag)."""
        out = []
        for a in ancestors(e):
            t = _local(a.tag)
            if t in ("sec", "notes", "fn-group", "fn", "app", "boxed-text", "ack", "glossary", "statement"):
                title = a.find("title")
                if title is None:
                    title = next((c for c in a if _local(c.tag) == "title"), None)
                ttxt = _flat(title) if title is not None else ""
                typ = a.get("sec-type") or a.get("notes-type") or a.get("fn-type") or a.get("content-type") or ""
                out.append((ttxt, typ, t))
            elif t in ("table-wrap", "fig", "supplementary-material", "abstract", "ref-list", "back", "front",
                       "trans-abstract", "sub-article"):
                cap = ""
                if t == "table-wrap":
                    c = next((x for x in a.iter() if _local(x.tag) == "caption"), None)
                    cap = _flat(c) if c is not None else ""
                out.append((cap, t, t))
        return out

    def is_das(chain, text=""):
        for ttxt, typ, tag in chain:
            if tag in ("table-wrap", "fig", "supplementary-material", "abstract", "ref-list", "front",
                       "trans-abstract"):
                if tag in ("abstract", "trans-abstract", "ref-list", "front"):
                    return False
                continue
            if typ and DAS_TYPE.search(typ):
                return True
            if ttxt:
                if NOT_DAS_HEADING.search(ttxt):
                    return False
                if DAS_HEADING.search(ttxt):
                    return True
        return bool(text and INLINE_DAS_START.match(text))

    def in_krt(chain):
        return any(tag == "table-wrap" and KRT_HEADING.search(t or "") for t, _, tag in chain) or \
            any(KRT_HEADING.search(t or "") for t, _, tag in chain[:2])

    passages = []
    for e in root.iter():
        t = _local(e.tag)
        if t == "p" or t == "title":
            anc = list(ancestors(e))
            atags = {_local(a.tag) for a in anc}
            if atags & {"td", "th", "tr", "ref", "p", "title"}:
                continue
            if "article-meta" in atags and t == "title":
                continue
            chain = heading_chain(e)
            text = _flat(e)
            if not text:
                continue
            xrefs = [x.get("rid") for x in e.iter() if _local(x.tag) == "xref" and x.get("ref-type") == "bibr"]
            passages.append({"kind": t, "chain": [c[0] for c in chain if c[0]][:4],
                             "das": is_das(chain, text if t == "p" else ""),
                             "krt": False, "sec": " > ".join(reversed([c[0] or c[1] for c in chain][:4])),
                             "text": text, "xrefs": [x for x in xrefs if x], "ref_id": None})
        elif t == "tr":
            chain = heading_chain(e)
            cells = [_flat(c) for c in e if _local(c.tag) in ("td", "th")]
            text = " | ".join(c for c in cells if c)
            if text:
                passages.append({"kind": "tr", "chain": [c[0] for c in chain if c[0]][:4], "das": is_das(chain),
                                 "krt": in_krt(chain), "sec": " > ".join(reversed([c[0] or c[1] for c in chain][:4])),
                                 "text": text, "xrefs": [], "ref_id": None})
        elif t == "ref":
            passages.append({"kind": "ref", "chain": [], "das": False, "krt": False, "sec": "references",
                             "text": _flat(e), "xrefs": [], "ref_id": e.get("id")})
        elif t == "custom-meta":
            name = next((c for c in e if _local(c.tag) == "meta-name"), None)
            val = next((c for c in e if _local(c.tag) == "meta-value"), None)
            ntxt = _flat(name) if name is not None else ""
            if val is not None and DAS_HEADING.search(ntxt):
                passages.append({"kind": "meta", "chain": [ntxt], "das": True, "krt": False, "sec": ntxt,
                                 "text": _flat(val), "xrefs": [], "ref_id": None})
    art = root if _local(root.tag) == "article" else next((x for x in root.iter() if _local(x.tag) == "article"), root)
    ids = {(x.get("pub-id-type") or ""): (x.text or "") for x in root.iter() if _local(x.tag) == "article-id"}
    is_ms = "manuscript" in ids or any("nihms" in v.lower() for v in ids.values())
    return {"passages": passages, "article_type": art.get("article-type", ""), "is_manuscript": is_ms}


# ================================================================================================
# 2. Identifier patterns
# ================================================================================================
# cls: public | controlled | code | generic (data OR code, open) | url (other link in a statement)
def _doi(prefix_re):
    # stop at "(" too: "10.7910/DVN/ISYQVC(csv)" must yield the DOI, not "...isyqvc(csv"
    return re.compile(r"(?:doi\.org/|doi:\s?|\b)(" + prefix_re + r"[^\s\"<>|,;()\]]*)", re.I)


ID_PATTERNS = [
    # repo, cls, regex, group index for canonical id
    ("GEO", "public", re.compile(r"\b(G(?:SE|DS)\d{3,7})\b"), 1),
    ("SRA/BioProject", "public", re.compile(r"\b(PRJ(?:NA|EB|DB)\d{3,}|[SED]RP\d{6,})\b"), 1),
    ("SRA run", "public", re.compile(r"\b([SED]R[RXS]\d{6,})\b"), 1),
    ("dbGaP", "controlled", re.compile(r"\b(phs\d{6})(?:\.v\d+(?:\.p\d+)?)?\b", re.I), 1),
    ("EGA", "controlled", re.compile(r"\b(EGA[SDC]\d{11})\b"), 1),
    ("PRIDE/ProteomeXchange", "public", re.compile(r"\b(PXD\d{6})\b"), 1),
    ("MassIVE", "public", re.compile(r"\b(MSV\d{9})\b"), 1),
    ("ArrayExpress", "public", re.compile(r"\b(E-[A-Z]{4}-\d+)\b"), 1),
    ("BioStudies", "public", re.compile(r"\b(S-BSST\d+)\b"), 1),
    ("MetaboLights", "public", re.compile(r"\b(MTBLS\d+)\b"), 1),
    ("Metabolomics Workbench", "public", re.compile(r"\b(ST\d{6})\b"), 1),
    ("PDB", "public", re.compile(r"\b(?:PDB|Protein Data Bank)\b(?:[^.;]{0,40}?\b(?:IDs?|codes?|entr(?:y|ies)|"
                                 r"accessions?(?: (?:numbers?|codes?))?|identifiers?))?\s*[:#]?\s*([0-9][A-Za-z0-9]{3})\b"), 1),
    ("EMDB", "public", re.compile(r"\b(EMD-\d{4,5})\b"), 1),
    ("PDC", "public", re.compile(r"\b(PDC\d{6})\b"), 1),
    ("GDC project", "controlled", re.compile(
        r"\b(TCGA-[A-Z]{3,5}|TARGET-(?:ALL-P[1-3]|AML|NBL|OS|WT|RT|CCSK)|CPTAC-[23]|HCMI-CMDC|CGCI-[A-Z]+(?:-[A-Z]+)?|"
        r"MMRF-COMMPASS|BEATAML1\.0-COHORT)\b"), 1),
    ("Zenodo", "generic", re.compile(r"(?:\b10\.5281/zenodo\.|zenodo\.org/(?:record|records|doi/10\.5281/zenodo\.)/?)(\d{4,})", re.I), 1),
    ("Figshare", "generic", _doi(r"10\.6084/m9\.figshare\.\d+"), 1),
    ("Figshare", "generic", re.compile(r"((?:[\w-]+\.)?figshare\.com/(?:articles|s|projects|collections|ndownloader)/[^\s\"<>|,;)\]]+)", re.I), 1),
    ("Dryad", "generic", _doi(r"10\.5061/dryad\.[0-9a-z]+"), 1),
    ("OSF", "generic", re.compile(r"(?:10\.17605/)?\bosf\.io/([0-9a-z]{5})\b", re.I), 1),
    # keep the version (".1" or "/1"): Mendeley Data registers versioned DOIs; the bare form is not always registered
    ("Mendeley Data", "generic", re.compile(r"(?:10\.17632/|data\.mendeley\.com/datasets/)([0-9a-z]{10}(?:[./]\d+\b)?)",
                                            re.I), 1),
    ("Synapse", "generic", re.compile(r"\b(syn\d{7,9})\b"), 1),
    ("Dataverse", "generic", _doi(r"10\.7910/dvn/[0-9a-z]{6}"), 1),
    # institutional Dataverse instances use their own DOI prefixes (e.g. UVA 10.18130): require the word nearby
    ("Dataverse", "generic", re.compile(r"\bDataverse\b[^.;]{0,80}?(?:doi\.org/|doi:\s?)?\b(10\.\d{4,5}/[^\s\"<>|,;()\]]+)",
                                        re.I), 1),
    ("GWAS Catalog", "public", re.compile(r"\b(GCST\d{6,})\b"), 1),
    # Code Ocean DOIs are versioned (10.24433/CO.1234567.v1; the bare form is not registered), and a capsule URL's
    # number is not a DOI: keep the version on DOIs and check capsule URLs as URLs
    ("Code Ocean", "code", re.compile(r"\b(10\.24433/co\.\d{5,}(?:\.v\d+)?)", re.I), 1),
    ("Code Ocean", "code", re.compile(r"(codeocean\.com/capsule/\d{5,})", re.I), 1),
    ("TCIA", "public", _doi(r"10\.7937/[0-9a-z]"), 1),
    ("TCIA/IDC link", "public", re.compile(r"((?:www\.)?(?:cancerimagingarchive\.net|portal\.imaging\.datacommons\.cancer\.gov|imaging\.datacommons\.cancer\.gov)/[^\s\"<>|,;)\]]*)", re.I), 1),
    ("CELLxGENE", "public", re.compile(r"(cellxgene\.cziscience\.com/(?:collections|e|d)/[0-9a-f-]{20,})", re.I), 1),
    ("GitHub", "code", re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", re.I), 1),
    ("GitLab", "code", re.compile(r"gitlab\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", re.I), 1),
    ("Bitbucket", "code", re.compile(r"bitbucket\.org/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", re.I), 1),
]
GITHUB_NON_REPO = {"orgs", "features", "topics", "about", "sponsors", "marketplace", "site", "settings",
                   "collections", "pricing", "login", "join", "explore", "search", "apps"}
URL_RE = re.compile(r"\b((?:https?://|www\.)[^\s\"<>|]+)", re.I)
DOI_REPO_PREFIX = {"Zenodo": "10.5281/zenodo.", "Code Ocean": "10.24433/CO."}


def _canon(repo, raw):
    raw = raw.rstrip(".,;:)]}'\"")
    if repo in ("GitHub", "GitLab", "Bitbucket"):
        owner, _, name = raw.partition("/")
        name = re.sub(r"\.git$", "", name.rstrip("."))
        if owner.lower() in GITHUB_NON_REPO or not name:
            return None
        host = {"GitHub": "github.com", "GitLab": "gitlab.com", "Bitbucket": "bitbucket.org"}[repo]
        return f"{host}/{owner}/{name}".lower()
    if repo == "Zenodo":
        return f"10.5281/zenodo.{raw}"
    if repo == "Code Ocean":
        if raw.lower().startswith("10."):
            return "10.24433/CO." + raw.split(".", 2)[2].lower()   # "10.24433/CO.1234567.v1"
        return raw.lower()                                          # "codeocean.com/capsule/1234567"
    if repo == "OSF":
        return f"osf.io/{raw.lower()}"
    if repo == "Mendeley Data":
        return f"10.17632/{raw.lower().replace('/', '.')}"
    if repo in ("Figshare", "Dryad", "Dataverse", "TCIA") and raw.lower().startswith("10."):
        return raw.lower()
    if repo in ("dbGaP",):
        return raw.lower()
    if repo == "PDB":
        return None if raw.isdigit() else raw.upper()
    return raw


# ================================================================================================
# 3. Cue lexicons
# ================================================================================================
def _rx(*parts):
    return re.compile("|".join(parts), re.I)


GEN_CUES = _rx(
    r"\b(?:have|has|had) (?:also )?been (?:deposited|submitted|uploaded|archived|registered|"
    r"made (?:publicly |freely |openly )?(?:available|accessible)|released|published) (?:in|to|at|on|into|with|under|through|via)\b",
    r"\b(?:were|was|are|is) (?:also )?(?:deposited|submitted|uploaded|archived)\b",
    # not "previously deposited in": someone's earlier deposit (the reuse cue would lose on distance otherwise)
    r"(?<!previously )(?<!already )(?<!originally )\bdeposited (?:in|to|at|into|with|on|under)\b",
    # "this/our/the present/the current" only: "described in the article by Smith" is someone else's work
    r"\b(?:generated|produced|created|collected|acquired|developed|reported|presented|described|sequenced|"
    r"performed|analy[sz]ed and generated|generated and(?:/or)? analy[sz]ed|generated or analy[sz]ed) "
    r"(?:in|during|for|by|within|as part of) (?:this|the (?:present|current)|our) (?:study|work|paper|article|"
    r"manuscript|project|research|publication)\b",
    r"\bwe (?:have )?(?:deposited|uploaded|submitted|archived|released|made (?:\w+ )?(?:publicly |freely )?available)\b",
    r"\b(?:our|the|all|custom|in-house|source) (?:source |analysis |custom |computational |R |Python )?"
    r"(?:codes?|scripts?|software|pipelines?|packages?|notebooks?)\b[^.]{0,100}?\b(?:is|are|has been|have been|can be) "
    r"(?:freely |publicly |openly |also )?(?:available|accessible|hosted|deposited|provided|released|found|accessed|downloaded|"
    r"included|shared|uploaded|archived)",
    r"\b(?:codes?|scripts?|software|source codes?)\s+(?:used|developed|written|generated|needed|required|to reproduce)\b"
    r"[^.]{0,80}?\b(?:available|accessible|deposited|provided|found|hosted|accessed|included|shared|uploaded|archived)",
    r"\bnewly (?:generated|produced|created|sequenced)\b",
    # "by the" is deliberately excluded: "data generated by the TARGET initiative" is someone else's data
    r"\b(?:datasets?|data sets?|data) (?:presented|generated|reported|produced|described) (?:in|by) (?:this|our)\b",
    r"\b(?:datasets?|data sets?|data) (?:presented|generated|reported|produced|described) in the (?:present|current) "
    r"(?:study|work)\b",
    r"\bthe names? of the repositor(?:y|ies) and accession numbers?\b",
    r"\bdata reported in this paper\b",
    r"\b(?:have|has) (?:also )?been deposited\b",
    r"\bdata (?:that|which) supports? (?:the )?(?:findings|results|conclusions) of this (?:study|work|paper|article)\b",
)
GEN_WEAK = re.compile(
    r"\b(?:are|is|can be|were|was) (?:\w+ ){0,2}?(?:available|accessible|deposited|hosted|found|accessed|downloaded|"
    r"viewed|retrieved)\b(?: \w+){0,3}? (?:in|at|from|through|via|under|on|with)\b"
    r"|\baccession (?:numbers?|codes?|nos?\.?|IDs?)\b|\bavailable (?:in|at|from|through|via|under|on)\b", re.I)
KRT_GEN = re.compile(r"\bthis (?:paper|study|work|manuscript|article)\b", re.I)
REUSE_STRONG = _rx(
    r"\b(?:downloaded|obtained|retrieved|acquired|extracted|accessed|sourced|requested|pulled|imported|"
    r"queried|mined) (?:\w+ ){0,3}?(?:from|via|through|at|using|on)\b"
    r"(?!\s+(?:the\s+)?(?:corresponding|senior|lead|first|last|study|contact)?\s*(?:authors?|investigators?|PI|"
    r"lead contact|reasonable|request))",
    r"\bobtained by (?:re-?)?analy[sz]ing\b",
    # [\w-]+ so hyphenated words ("single-cell RNA-seq") do not break the pattern
    # the words in between may not be prepositions: "publicly available in the GEO data repository" says where the
    # data are, not where they came from
    r"\bpublicly[- ]available (?:(?!(?:in|at|on|via|through|from|under|to|for|and|with)\b)[\w-]+ ){0,3}?"
    r"(?:data|datasets?|data ?sets?|cohorts?|resources?|samples|profiles|studies|sources?)\b",
    r"\b(?:previous(?:ly)?|already) (?:been )?(?:published|described|reported|generated|deposited|released|"
    r"characteri[sz]ed|available|made available)\b",
    # "generated in prior studies", "generated for a previous study", "from our earlier publication"
    r"\b(?:in|for|from|by|during) (?:a |an |the |our |these |two |several )?(?:prior|previous|earlier|original|"
    r"published) (?:[\w-]+ )?(?:stud(?:y|ies)|works?|publications?|papers?|reports?|articles?|experiments?)\b",
    r"\bre-?analy[sz](?:ed|is|ing|e)\b",
    # [\w-]+ so that "public scRNA-seq data" is recognised
    r"\b(?:existing|external|third[- ]party|publicly accessible|public|published|open[- ]access) "
    r"(?:[\w-]+ ){0,1}?(?:data ?sets?|datasets?|data|cohorts?|resources?)\b",
    r"\b(?:provided|shared|made available|generated) by (?:the )?(?:[A-Z][\w-]+ ){0,4}?(?:et al|consortium|group|"
    r"investigators|project|program|network|study|laboratory|lab)\b",
    r"\bcourtesy of\b",
    r"\bby the original (?:authors?|investigators?|stud(?:y|ies))\b",
    r"\bet al\.?",
)
# "datasets analysed during the current study are available in GEO (GSE...)" (without "generated"): the Springer
# reuse template, but authors also use it for their own deposits (GEO links some of these series to the same paper).
# Inside a statement it makes the identifier "unknown" instead of own by default. Not after "generated or"/"and"
# (the own template) or "no ... were" (the no-data template).
ANALYSED_THIS = re.compile(
    r"(?<!or )(?<!and )(?<!and/or )(?<!nor )(?<!not )(?<!no )(?<!were )(?<!was )"
    r"\b(?:analy[sz]ed|referenced|re-?used|used|utili[sz]ed) (?:in|during|for|by) (?:this|the (?:present|current)) (?:study|work|paper|"
    r"article|manuscript)\b", re.I)
# Data provenance from an earlier study (narrow on purpose: "as previously described" for a method is not here)
PRIOR_DATA = _rx(
    r"\b(?:generated|collected|produced|sequenced|obtained|published|reported) (?:in|for|from|by|during) "
    r"(?:a |an |the |our |these |two |several )?(?:prior|previous|earlier|original|published) (?:[\w-]+ )?"
    r"(?:stud(?:y|ies)|works?|publications?|papers?|reports?|articles?)\b",
    r"\bprevious(?:ly)? (?:published|generated|deposited|released) (?:[\w-]+ ){0,3}?(?:data|datasets?|data ?sets?|"
    r"series|samples|profiles)\b",
    r"\b(?:data|datasets?|data ?sets?) (?:[\w-]+ ){0,3}?(?:were|was|have been|has been|are|is) (?:also )?previously "
    r"(?:published|generated|deposited|released)\b",
)
# Data generated in THIS study (used to keep PRIOR_DATA from overriding a mixed sentence)
GEN_THIS = _rx(
    r"\b(?:generated|produced|created|collected|acquired|sequenced)\b[^.]{0,40}?\b(?:in|during|for|by|within|"
    r"as part of) (?:this|the (?:present|current)|our) (?:study|work|paper|article|manuscript|project|research|"
    r"publication)\b",
    r"\bnewly (?:generated|produced|created|sequenced)\b",
)
REUSE_WEAK = _rx(
    r"\b(?:were|was) (?:analy[sz]ed|used|utili[sz]ed|leveraged|examined|interrogated|evaluated|included)\b",
    r"\bfrom the (?:\w+ ){0,4}?(?:database|portal|repository|consortium|atlas|archive|program|initiative)\b",
    r"\b(?:using|used|use of|utili[sz]ing|leveraging|analy[sz]ing|analysis of|from) (?:[\w-]+ ){0,3}?"
    r"(?:data|datasets?|data ?sets?|cohorts?)\b",
    r"\b(?:validation|independent|training|test|discovery) (?:cohort|dataset|data set)s?\b",
    # "a public repository", "publicly available databases": says what kind of place it is, so it is only a weak
    # reuse cue ("deposited in the public repository MassIVE" is an own deposit)
    r"\b(?:public|publicly[- ](?:available|accessible)|open[- ]access) (?:[\w-]+ ){0,1}?(?:databases?|repositor(?:y|ies))\b",
)
ON_REQUEST = _rx(
    r"\b(?:upon|on|by|with|after|following|via|through|per|at|under) (?:a |the )?(?:reasonable|justified|written|"
    r"formal|appropriate|bona fide|legitimate|academic|scientific|direct|specific|motivated|reasonably|qualified|"
    r"valid|request(?:ed)?)?\s*requests?\b",
    r"\brequests? (?:for|to access|regarding) (?:the |these |this |such |any )?(?:data|datasets?|code|information)[^.]{0,80}?"
    r"\b(?:directed|addressed|sent|made|submitted|forwarded)\b",
    r"\b(?:contact(?:ing)?|from|by|to) the (?:corresponding|senior|lead|first|last|study|principal) "
    r"(?:author|investigator|contact)",
    r"\bavailable from (?:the )?(?:corresponding |senior |lead |contact )?authors?\b",
    r"\b(?:upon|on|after) (?:approval|request)\b",
    r"\bcan be (?:requested|obtained|made available) (?:from|by|through) (?:the )?(?:authors?|corresponding|PI|lead|study)",
)
DATA_WORD = re.compile(r"\b(?:data|datasets?|data ?sets?|codes?|scripts?|software|results|information|sequenc\w*|"
                       r"files?|images?|raw|findings|analyses|outputs?|materials? and data|metadata|records)\b", re.I)
MATERIALS_ONLY = re.compile(r"^\s*(?:all )?(?:unique |new |other )?(?:materials?|reagents?|plasmids?|cell lines?|"
                            r"antibod(?:y|ies)|constructs?|mice|mouse lines?|organoids?|strains?)\b", re.I)
IN_ARTICLE = _rx(
    r"\b(?:are|is|were|have been|has been|can be)\s+(?:\w+\s+){0,2}?(?:included|provided|contained|available|presented|"
    r"reported|found|shown|listed|described)\s+(?:with)?in\s+(?:the|this)\s+(?:published\s+|main\s+)?(?:article|paper|"
    r"manuscript|main text|text|figures?|report)\b",
    r"\bwithin (?:the|this) (?:published )?(?:article|paper|manuscript)\b",
    r"\b(?:are|is|were|have been|has been|can be)\s+(?:\w+\s+){0,3}?(?:included|provided|contained|available|presented|"
    r"reported|found|shown|listed)\b[^.]{0,60}?\b(?:supplementa(?:ry|l)|supporting|additional|extended data|online)\s+"
    r"(?:information|materials?|data|files?|tables?|figures?)",
    r"\bsource data (?:are|is) provided\b",
    r"\bprovided with this paper\b",
)
ACCESS_DATE = re.compile(r"\(?\b(?:last )?(?:accessed|retrieved)(?: on)? (?:\d{1,2} \w+ \d{4}|\w+ \d{1,2},? \d{4}|"
                         r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\)?", re.I)
NEGATED_AVAIL = re.compile(r"\bnot (?:\w+ )?(?:publicly |openly |freely )?(?:available|accessible|shared)\b", re.I)
AVAIL_VERB = re.compile(r"\b(?:available|included|provided|contained|presented|found|reported|shown|within|accessible)\b", re.I)
NO_DATA = _rx(
    r"\bnot applicable\b",
    r"\bno (?:new |original |primary |novel |additional )?(?:data|datasets?|data sets?|code) (?:were|was|have been|has been|are|is) "
    r"(?:generated|created|produced|collected|analy[sz]ed|used|associated|reported|shared)",
    r"\bdata sharing (?:is )?not applicable\b",
    r"\b(?:does|did) not (?:contain|include|report|present|involve|use|generate|produce|have) (?:any )?(?:new |original |"
    r"primary |associated |underlying )?(?:data|datasets?)\b",
    r"\bno (?:new |original |novel )?(?:data|datasets?) (?:to (?:share|report|deposit)|associated|available)\b",
    r"\bno datasets? (?:was|were) (?:generated|analy[sz]ed)",
    r"\bthere (?:is|are) no (?:new |original )?(?:data|datasets?)\b",
)
WILL_DEPOSIT = _rx(
    r"\bwill be (?:deposited|made (?:\w+ )?(?:publicly |freely |openly )?(?:available|accessible)|released|shared|uploaded|"
    r"submitted|posted|archived|published)\b(?![^.]{0,60}\brequest)",
    r"\b(?:upon|after|following|at the time of) (?:publication|acceptance)\b",
    r"\bprior to publication\b|\bin the process of (?:being )?(?:deposit|submit|upload)",
    r"\bare being (?:deposited|submitted|uploaded)\b|\b(?:submission|deposition) (?:is )?(?:in progress|pending|underway)\b",
)
CONTROLLED_NAMES = re.compile(
    r"\b(?:dbGaP|database of Genotypes and Phenotypes|EGA|European Genome[- ]?phenome Archive|Vivli|YODA|"
    r"Project Data Sphere|NCTN(?:/NCORP)? Data Archive|Cancer Data Service|controlled[- ]access|"
    r"data access committee|AnVIL|Genomic Data Commons|GDC)\b", re.I)
REPO_NAMES = re.compile(
    r"\b(?:GEO|Gene Expression Omnibus|SRA|Sequence Read Archive|dbGaP|EGA|European Genome[- ]?phenome Archive|PRIDE|"
    r"ProteomeXchange|MassIVE|Zenodo|figshare|Dryad|OSF|Open Science Framework|Synapse|GitHub|GitLab|Bitbucket|"
    r"GDC|Genomic Data Commons|PDC|Proteomic Data Commons|TCIA|Cancer Imaging Archive|IDC|Imaging Data Commons|"
    r"Mendeley Data|ArrayExpress|BioStudies|MetaboLights|Metabolomics Workbench|ENA|European Nucleotide Archive|"
    r"BioProject|Dataverse|Code ?Ocean|NCTN Data Archive|Vivli|YODA|Project Data Sphere|Cancer Data Service|"
    r"HTAN|cBioPortal|Protein Data Bank|PDB|CELLxGENE|Single Cell Portal|Open Science|ICPSR|NDA|Kaggle|"
    r"Hugging ?Face|Software Heritage|PyPI|CRAN|Bioconductor)\b", re.I)
REUSE_RESOURCES = re.compile(
    r"\b(?:TCGA|[Tt]he Cancer Genome Atlas|GTEx|CCLE|Cancer Cell Line Encyclopedia|DepMap|cBioPortal|UK Biobank|SEER|"
    r"Surveillance,? [Ee]pidemiology,? and End Results|ICGC|GDSC|METABRIC|PCAWG|Human Protein Atlas|NHANES|"
    r"1000 Genomes|gnomAD|ClinVar|COSMIC|LINCS|ENCODE|CPTAC|Kaplan[- ]Meier Plotter|GEPIA2?|TIMER2?(?:\.0)?|UALCAN|"
    r"MSigDB|NCDB|National Cancer Data ?base|All of Us|Million Veteran|PLCO|Women's Health Initiative|BRFSS|NHIS|"
    r"MEPS|SEER-Medicare|Medicare|Medicaid|Flatiron|TriNetX|Optum|MarketScan|HINTS|NLST|Project GENIE|AACR GENIE|"
    r"TARGET|Human Cell Atlas|Tabula Sapiens|STRING|KEGG|Reactome|Gene Ontology|UniProt|Ensembl|UCSC|"
    r"NCI-60|NCI60|PRISM|Connectivity Map|CMap|DrugBank|ChEMBL|PubChem|ImmPort|Pan-Cancer Atlas|PanCancer Atlas|"
    r"Xena|UCSC Xena|LinkedOmics|TISCH|[Cc]ancer [Rr]egistr(?:y|ies)|[Ee]lectronic [Hh]ealth [Rr]ecords?|EHR)\b")
CODE_WORD = re.compile(r"\b(?:code|codes|scripts?|software|pipelines?|packages?|notebooks?|source|implementation|"
                       r"tool|algorithm|model weights|programs?|repository for the analysis)\b", re.I)

_ABBR = re.compile(r"\b(et al|e\.g|i\.e|Fig|Figs|no|No|vs|ca|approx|Ref|Refs|Suppl|Supp|Inc|Ltd|Dr|St|Ver|v)\.", re.I)


def sentences(text: str) -> list[tuple[int, str]]:
    """Split into sentences; returns (offset, sentence)."""
    prot = _ABBR.sub(lambda m: m.group(1) + "․", text)
    out, start = [], 0
    for m in re.finditer(r"(?<=[.;!?])\s+(?=[A-Z(\[“\"])|(?<=[a-z0-9)]\.)(?=[A-Z][a-z]{2,})", prot):
        out.append((start, text[start:m.start()]))
        start = m.end()
    out.append((start, text[start:]))
    return [(o, s) for o, s in out if s.strip()]


_ACCESS_VERB = re.compile(r"(?:downloaded|obtained|retrieved|acquired|extracted|accessed|sourced|requested|pulled|"
                          r"imported|queried|mined)\b", re.I)
_MODAL_BEFORE = re.compile(r"\b(?:can|may|could|will|should) (?:also |now |then )?be (?:\w+ly )?$", re.I)
_REPO_CITE_BEFORE = re.compile(r"(?:" + REPO_NAMES.pattern + r")[^().;]{0,40}\(\s*[^()]{0,60}$", re.I)


def strong_reuse(sent: str) -> list:
    """REUSE_STRONG matches that say where data CAME from. Two kinds of match are availability wording instead, and
    are dropped:
      * "can be accessed via GitHub", "may be downloaded from GEO": how a reader gets the data;
      * "et al." in a citation of the repository itself: "via the PRIDE (Vizcaino et al., 2013) partner repository"."""
    out = []
    for m in REUSE_STRONG.finditer(sent):
        before = sent[:m.start()]
        if _ACCESS_VERB.match(m.group(0)) and _MODAL_BEFORE.search(before[-40:]):
            continue
        if m.group(0).lower().startswith("et al") and _REPO_CITE_BEFORE.search(before[-160:]):
            continue
        out.append(m)
    return out


def _nearest(cue_re, sent, pos):
    """cue_re: a compiled pattern, or a list of match objects."""
    best = None
    for m in (cue_re if isinstance(cue_re, list) else cue_re.finditer(sent)):
        d = 0 if m.start() <= pos <= m.end() else min(abs(pos - m.start()), abs(pos - m.end()))
        best = d if best is None else min(best, d)
    return best


def mention_role(sent: str, pos: int, krt: bool = False, prev: str = "", das: bool = False) -> tuple[str, str]:
    """Role of an identifier mention at `pos` in `sent`: generated | reused | unknown | none, with evidence."""
    if krt:
        if KRT_GEN.search(sent):
            return "generated", "key-resources row says 'this paper/study'"
        if strong_reuse(sent) or re.search(r"\(\d{4}\)|\b(?:19|20)\d{2}\b|\bref", sent, re.I):
            return "reused", "key-resources row cites a source"
    # "(accessed on 3 June 2025)" is a citation date, not a reuse cue: blank it out, keeping offsets
    sent = ACCESS_DATE.sub(lambda m: " " * len(m.group()), sent)
    # a sentence that says its data come from an earlier study is reuse even if a deposit verb sits nearer the
    # identifier ("data used in this study were generated for a previous study, and have been deposited in GEO"),
    # unless it also says data were generated in THIS study (then the nearest cue decides, below)
    if PRIOR_DATA.search(sent) and not GEN_THIS.search(sent):
        return "reused", "sentence says the data come from an earlier study"
    g = _nearest(GEN_CUES, sent, pos)
    rs = _nearest(strong_reuse(sent), sent, pos)
    rr = _nearest(REUSE_RESOURCES, sent, pos)
    if rr is not None:
        rs = rr if rs is None else min(rs, rr)
    rw = _nearest(REUSE_WEAK, sent, pos)
    r = min([x for x in (rs, None if rw is None else rw + 40) if x is not None], default=None)
    if g is not None and (r is None or g <= r):
        return "generated", "generation cue in sentence"
    if g is not None and rs is None:
        # only weak reuse phrasing ("from the Sequence Read Archive") competes with an explicit
        # generation cue in the same sentence: the generation cue wins
        return "generated", "generation cue in sentence (outweighs weak reuse phrasing)"
    if r is not None:
        return "reused", "reuse cue in sentence"
    if das and ANALYSED_THIS.search(sent):
        return "unknown", "statement says 'analysed in this study' (own or reused is unclear)"
    if krt:
        return "unknown", "key-resources row without 'this paper' or a cited source"
    if das and GEN_WEAK.search(sent):
        return "generated", "availability phrasing inside the statement, no reuse cue"
    if prev:
        g2 = GEN_CUES.search(prev)
        r2 = strong_reuse(prev) or REUSE_WEAK.search(prev)
        if g2 and not r2 and das:   # outside the statement, an own cue must be in the identifier's own sentence
            return "generated", "generation cue in preceding sentence"
        if r2 and not g2:
            return "reused", "reuse cue in preceding sentence"
    return "none", ""


# ================================================================================================
# 4. Paper-level extraction
# ================================================================================================
def _clip(s, n=300):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _sentence_has(rx, s):
    return bool(rx.search(s))


# Some papers print the private access token or password they gave reviewers. Published files quote the sentence
# without it.
_ACCESS_SECRET = re.compile(
    r"\b((?:token|password|passcode)\b(?: for reviewers)?(?:\s*[:=“\"'(]\s*|\s+(?=\S*\d)))([^\s”\"')<>;,]{4,})", re.I)


def redact_access(s: str) -> str:
    return _ACCESS_SECRET.sub(lambda m: m.group(1) + "[removed]", s or "")


def analyze(doc: dict) -> dict:
    """doc = output of parse_jats. Returns identifiers, statement and cues."""
    ps = doc.get("passages", [])
    das_ps = [p for p in ps if p["das"] and p["kind"] != "title"]
    das_titles = sorted({p["chain"][0] for p in ps if p["das"] and p["chain"]})[:5]
    das_xrefs = {x for p in das_ps for x in p["xrefs"]}

    # --- statement sentences (DAS section first, otherwise inline statement sentences) -------------
    das_sents = [s for p in das_ps for _, s in sentences(p["text"])]
    inline = []
    if not das_ps:
        for p in ps:
            if p["kind"] not in ("p",) or "abstract" in p["sec"].lower():
                continue
            for _, s in sentences(p["text"]):
                if (ON_REQUEST.search(s) and DATA_WORD.search(s) and not MATERIALS_ONLY.search(s)) or \
                        (GEN_CUES.search(s) and (REPO_NAMES.search(s) or URL_RE.search(s)) and DATA_WORD.search(s)):
                    inline.append(s)
    stmt_sents = das_sents or inline
    statement = " ".join(p["text"] for p in das_ps) if das_ps else " ".join(inline)

    # --- identifiers -------------------------------------------------------------------------------
    found: dict[tuple, dict] = {}

    def add(repo, cls, cid, raw, role, ev, loc, ctx, code_ctx, this_row=False, dkey=None):
        """this_row: the mention is a table row that says "this paper/study". dkey: deduplication key when it differs
        from the identifier itself (URLs: case-insensitive key, original spelling kept for the link check)."""
        key = (repo if repo not in ("SRA run",) else "SRA/BioProject", dkey or cid)
        rec = found.get(key)
        if rec is None:
            rec = found[key] = {"repo": key[0], "cls": cls, "id": cid, "raw": raw, "roles": [], "locations": set(),
                                "context": "", "code_context": False, "evidence": ""}
        rec["roles"].append((role, ev, loc, this_row))
        rec["locations"].add(loc)
        rec["code_context"] |= code_ctx
        pri = {"das": 0, "krt": 1, "body": 2, "ref": 3}
        if not rec["context"] or pri[loc] < pri.get(rec.get("_ctxloc", "ref"), 3):
            rec["context"], rec["_ctxloc"] = _clip(ctx, 300), loc

    for p in ps:
        if p["kind"] == "title":
            continue
        loc = "das" if p["das"] else ("krt" if p["krt"] else ("ref" if p["kind"] == "ref" else "body"))
        if p["kind"] == "ref" and p["ref_id"] in das_xrefs:
            loc = "das"
        sents = sentences(p["text"]) if p["kind"] != "ref" else [(0, p["text"])]
        prev = ""
        for _, s in sents:
            claimed = []
            this_row = p["kind"] == "tr" and bool(KRT_GEN.search(s))
            for repo, cls, rx, gi in ID_PATTERNS:
                for m in rx.finditer(s):
                    if any(a <= m.start() < b for a, b in claimed):
                        continue
                    cid = _canon(repo, m.group(gi))
                    if not cid:
                        continue
                    claimed.append((m.start(), m.end()))
                    if loc == "ref":
                        role, ev = "reused", "cited in reference list"
                    else:
                        role, ev = mention_role(s, m.start(), krt=(loc == "krt"), prev=prev, das=(loc == "das"))
                        if role == "unknown" and loc == "krt" and cls == "code":
                            role, ev = "none", ""   # software rows ("Seurat | Satija lab | github...") are tools
                    add(repo, cls, cid, m.group(0), role, ev, loc, s, bool(CODE_WORD.search(s)), this_row)
            if loc == "das":   # other links inside the statement
                for m in URL_RE.finditer(s):
                    if any(a <= m.start() < b for a, b in claimed):
                        continue
                    u = m.group(1).rstrip(".,;:)]}'\"")
                    if re.search(r"doi\.org/10\.|creativecommons|orcid\.org|ncbi\.nlm\.nih\.gov/(?:pmc|pubmed)|"
                                 r"urldefense\.|safelinks\.protection|scicrunch\.org/resolver|identifiers\.org/RRID",
                                 u, re.I):
                        continue
                    if any(rx.search(u) for _, _, rx, _ in ID_PATTERNS):
                        continue
                    role, ev = mention_role(s, m.start(), prev=prev, das=True)
                    # URL paths are case-sensitive (GitHub Pages, Google Drive, YouTube): keep the original spelling
                    # for the link check; only scheme and host are normalised, and the dedup key is case-insensitive
                    uu = re.sub(r"^(?:https?://)?([^/?#]+)", lambda h: "https://" + h.group(1).lower(), u, count=1,
                                flags=re.I)
                    add("Other link", "url", uu, u, role, ev, "das", s, bool(CODE_WORD.search(s)), dkey=uu.lower())
            prev = s

    # --- statement-level cues ----------------------------------------------------------------------
    st = " ".join(stmt_sents)
    gen_stmt = any(GEN_CUES.search(s) for s in stmt_sents)
    reuse_stmt = any(strong_reuse(s) or REUSE_RESOURCES.search(s) for s in stmt_sents)

    cues = {
        "on_request": any(ON_REQUEST.search(s) and DATA_WORD.search(s) and not MATERIALS_ONLY.search(s)
                          for s in stmt_sents),
        "in_article": any(IN_ARTICLE.search(s) and not NEGATED_AVAIL.search(s) for s in das_sents),
        "no_data": any(NO_DATA.search(s) for s in das_sents),
        "will_deposit": any(WILL_DEPOSIT.search(s) for s in stmt_sents),
        "controlled_gen": any(CONTROLLED_NAMES.search(s) and (GEN_CUES.search(s) or (s in das_sents and not
                              strong_reuse(s) and not REUSE_RESOURCES.search(s))) for s in stmt_sents),
        "reuse_in_statement": reuse_stmt,
        "gen_in_statement": gen_stmt,
        "code_in_statement": any(CODE_WORD.search(s) and (GEN_CUES.search(s) or URL_RE.search(s)) for s in stmt_sents),
        "privacy_restriction": bool(re.search(r"\b(?:privacy|confidential|HIPAA|IRB|institutional review board|"
                                              r"data use agreement|DUA|consent|identifiable|protected health)\b", st, re.I)),
    }

    # --- resolve identifier roles ------------------------------------------------------------------
    ids = []
    for rec in found.values():
        roles = rec.pop("roles")
        rec.pop("_ctxloc", None)
        locs = rec["locations"]
        # What the availability statement (or key-resources table) says about an identifier outranks a passing
        # mention in the methods: "TARGET datasets ... can be obtained from dbGaP (phs000464)" in the statement
        # beats "data generated by the TARGET initiative, phs000464" in the body.
        decided = [r for r in roles if r[2] in ("das", "krt") and r[0] in ("generated", "reused")]
        pool = decided or roles
        gen = [r for r in pool if r[0] == "generated"]
        reu = [r for r in pool if r[0] == "reused"]
        unk = [r for r in pool if r[0] == "unknown"]
        if gen:
            role, ev = "generated", gen[0][1]
        elif reu:
            role, ev = "reused", reu[0][1]
        elif unk:
            role, ev = "unknown", unk[0][1]
        elif "das" in locs:
            if gen_stmt and not reuse_stmt:
                role, ev = "generated", "listed in a statement that only describes new outputs"
            elif reuse_stmt and not gen_stmt:
                role, ev = "reused", "listed in a statement that only describes reused data"
            else:
                role, ev = "unknown", "listed in statement without a usable cue"
        elif any(r[3] for r in roles):
            # a table outside a key-resources heading whose row says "this paper/study" (e.g. "Sequencing data |
            # This study | GEO: GSE283317"). A plain body mention never becomes own just because the statement names
            # the same repository: that default turned reused series in methods/legends into own deposits.
            role, ev = "generated", "table row says 'this paper/study'"
        elif rec["cls"] == "code":
            role, ev = "reused", "code link in body text (tool citation) without a sharing cue"
        else:
            role, ev = "reused", "accession in body text without a deposit cue (default)"
        rec["role"], rec["evidence"] = role, ev
        rec["locations"] = sorted(locs)
        rec["is_code"] = rec["cls"] == "code" or (rec["cls"] in ("generic", "url") and rec["code_context"])
        ids.append(rec)

    # cap very long accession lists (e.g. run lists) per repository
    capped, per = [], {}
    for r in sorted(ids, key=lambda r: ({"generated": 0, "unknown": 1, "reused": 2}[r["role"]], r["repo"], r["id"])):
        per[r["repo"]] = per.get(r["repo"], 0) + 1
        if per[r["repo"]] <= 25:
            capped.append(r)
    return {
        "das_found": bool(das_ps),
        "das_headings": das_titles,
        "statement_kind": "section" if das_ps else ("inline" if inline else "none"),
        "statement": statement[:4000],
        "identifiers": capped,
        "n_identifiers_total": len(ids),
        "cues": cues,
        "reuse_resources": sorted({m.group(0) for s in stmt_sents for m in REUSE_RESOURCES.finditer(s)})[:10],
    }
