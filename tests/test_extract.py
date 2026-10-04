"""Small rule tests on synthetic JATS. Run: python -m unittest discover tests"""
import unittest

from receipts import classify, extract


def jats(body="", back=""):
    return f"""<?xml version="1.0"?><article article-type="research-article" xmlns:xlink="http://www.w3.org/1999/xlink">
<front><article-meta><title-group><article-title>T</article-title></title-group></article-meta></front>
<body>{body}</body><back>{back}</back></article>"""


def label(xml, title="T"):
    doc = extract.parse_jats(xml)
    paper = {"pmid": "1", "fulltext_source": "test", "article_type": doc["article_type"], "pub_type": "",
             "title": title, "extraction": extract.analyze(doc)}
    return classify.rule_label(paper)[0], paper


def das(text):
    return jats(back=f'<sec sec-type="data-availability"><title>Data availability</title><p>{text}</p></sec>')


def ids(paper):
    return {i["id"]: i["role"] for i in paper["extraction"]["identifiers"]}


class Rules(unittest.TestCase):
    def test_deposit_is_public(self):
        lab, p = label(jats(back='<sec sec-type="data-availability"><title>Data availability</title>'
                                  '<p>RNA-seq data generated in this study have been deposited in GEO under accession '
                                  'GSE123456.</p></sec>'))
        self.assertEqual(lab, "PUBLIC_REPOSITORY")
        self.assertEqual(p["extraction"]["identifiers"][0]["role"], "generated")

    def test_downloaded_is_reused(self):
        lab, p = label(jats(back='<sec><title>Data Availability</title><p>Publicly available data were downloaded '
                                  'from GEO (GSE654321) and TCGA.</p></sec>'))
        self.assertEqual(lab, "REUSED_ONLY")

    def test_on_request(self):
        lab, _ = label(jats(back='<sec><title>Data availability statement</title><p>The data that support the '
                                 'findings of this study are available from the corresponding author upon reasonable '
                                 'request.</p></sec>'))
        self.assertEqual(lab, "ON_REQUEST")

    def test_materials_only_is_not_data_statement(self):
        lab, _ = label(jats(body='<sec><title>Materials availability</title><p>Plasmids are available upon '
                                 'request.</p></sec>'))
        self.assertEqual(lab, "NO_STATEMENT")

    def test_krt_this_paper(self):
        lab, _ = label(jats(body='<table-wrap><caption><p>Key resources table</p></caption><table><tr><td>Raw data'
                                 '</td><td>This paper</td><td>GEO: GSE111111</td></tr></table></table-wrap>'))
        self.assertEqual(lab, "PUBLIC_REPOSITORY")

    def test_github_tool_citation_not_shared(self):
        lab, p = label(jats(body='<sec><title>Methods</title><p>Reads were aligned with STAR '
                                 '(https://github.com/alexdobin/STAR).</p></sec>'))
        self.assertEqual(lab, "NO_STATEMENT")

    def test_not_applicable(self):
        lab, _ = label(jats(back='<sec><title>Data availability</title><p>Not applicable.</p></sec>'))
        self.assertEqual(lab, "NO_DATA_GENERATED")


class AuditRegressions(unittest.TestCase):
    """Each case is a real misfire found by the blind audit (VALIDATION.md), reduced to a synthetic sentence."""

    def test_generation_cue_beats_weak_repository_phrase(self):
        # "from the Sequence Read Archive" once outweighed "generated in this study" (PMID 39177583)
        lab, p = label(das("The EM-seq methylation data generated in this study are publicly available from the "
                           "Sequence Read Archive (SRA) under accession number PRJNA1145324."))
        self.assertEqual(ids(p)["PRJNA1145324"], "generated")
        self.assertEqual(lab, "PUBLIC_REPOSITORY")

    def test_has_been_deposited_with_parenthesis(self):
        # "has been deposited (GEO: ...)" had no usable cue (PMID 38265938)
        lab, p = label(das("The RNA-sequencing data used in this study has been deposited (GEO: GSE221112)."))
        self.assertEqual(ids(p)["GSE221112"], "generated")
        self.assertEqual(lab, "PUBLIC_REPOSITORY")

    def test_access_date_is_not_a_reuse_cue(self):
        # "(accessed on ...)" read as reuse (PMID 40565890)
        lab, p = label(das("The data that support the findings of this study are openly available in the Harvard "
                           "Dataverse at https://doi.org/10.7910/DVN/RI83WG (accessed on 3 June 2025)."))
        self.assertEqual(ids(p)["10.7910/dvn/ri83wg"], "generated")
        self.assertEqual(lab, "PUBLIC_REPOSITORY")

    def test_institutional_dataverse_doi(self):
        # UVA Dataverse uses its own DOI prefix (PMID 40360762)
        _, p = label(das("MRIs from these studies are available on UVA Dataverse at 10.18130/V3/SAVMVD."))
        self.assertIn("10.18130/v3/savmvd", ids(p))

    def test_doi_stops_at_parenthesis(self):
        _, p = label(das("Data have been deposited in Harvard Dataverse, doi:10.7910/DVN/ISYQVC(csv files)."))
        self.assertIn("10.7910/dvn/isyqvc", ids(p))
        self.assertFalse(any("(" in k for k in ids(p)))

    def test_gwas_catalog_accession(self):
        _, p = label(das("GWAS summary statistics generated in this study have been deposited in the GWAS Catalog "
                         "under accession GCST90668017."))
        self.assertEqual(ids(p).get("GCST90668017"), "generated")

    def test_statement_outranks_methods_mention(self):
        # statement says reused; methods says "data generated by the TARGET initiative" (PMID 38265938)
        lab, p = label(jats(
            body="<sec><title>Methods</title><p>T-ALL RNA sequencing data were obtained from data generated by the "
                 "Therapeutically Applicable Research to Generate Effective Treatments initiative, phs000464.</p></sec>",
            back='<sec sec-type="data-availability"><title>Data availability</title><p>The RNA-sequencing data used '
                 'in this study has been deposited (GEO: GSE221112). The TARGET T-ALL datasets used for the analysis '
                 'can be obtained from dbGaP (phs000464).</p></sec>'))
        self.assertEqual(ids(p)["phs000464"], "reused")
        self.assertEqual(ids(p)["GSE221112"], "generated")
        self.assertEqual(lab, "PUBLIC_REPOSITORY")

    def test_hyphenated_reuse_phrase(self):
        # "publicly available single-cell RNA-seq data" was missed because of the hyphens (PMID 39177583)
        _, p = label(das("Stemness signatures developed in this study were obtained by analyzing publicly available "
                         "single-cell RNA-seq data from 14 patients (dbGaP database accession phs001988.v1.p1)."))
        self.assertEqual(ids(p)["phs001988"], "reused")

    def test_review_title(self):
        lab, _ = label(jats(body="<sec><title>Introduction</title><p>Renal cell carcinoma is common.</p></sec>"),
                       title="Renal Cell Carcinoma: A Review.")
        self.assertEqual(lab, "NO_DATA_GENERATED")

    def test_chart_review_is_not_a_review_article(self):
        lab, _ = label(jats(body="<sec><title>Methods</title><p>We studied 500 patients.</p></sec>"),
                       title="Outcomes after surgery: a retrospective chart review")
        self.assertEqual(lab, "NO_STATEMENT")

    def test_in_article_beats_on_request(self):
        # ON_REQUEST means ONLY by asking; some data in the supplement makes it IN_ARTICLE
        lab, _ = label(das("Source data are provided with this paper. All other data are available from the "
                           "corresponding author upon reasonable request."))
        self.assertEqual(lab, "IN_ARTICLE")


class ReviewRegressions(unittest.TestCase):
    """Cases from the pre-release code review (real sentences from the FY2024 NCI R01 cohort)."""

    def test_reference_table_in_body_is_not_own(self):
        # PMID 41791379: a table of reference datasets in the body was made "own" because the statement names GEO
        lab, p = label(jats(
            body="<sec><title>Results</title><table-wrap><caption><p>Datasets</p></caption><table>"
                 "<tr><td>MB mouse model gene expression</td><td>34,54-62</td><td>GEO: GSE65888, GSE33201</td></tr>"
                 "</table></table-wrap></sec>",
            back='<sec sec-type="data-availability"><title>Data availability</title><p>Single-nuclei RNA-seq '
                 'datasets described in this study have been deposited and are publicly available in GEO under the '
                 'accession code GSE275306.</p></sec>'))
        self.assertEqual(ids(p)["GSE275306"], "generated")
        self.assertEqual(ids(p)["GSE65888"], "reused")
        self.assertEqual(ids(p)["GSE33201"], "reused")

    def test_table_row_this_study_is_own(self):
        # PMID 40037355: a data table (not headed "key resources") whose row says "This study"
        _, p = label(jats(body="<sec><title>Methods</title><table-wrap><caption><p>Data</p></caption><table><tr>"
                               "<td>Sequencing data</td><td>This study</td><td>GEO (accession numbers: GSE283317)</td>"
                               "</tr></table></table-wrap></sec>"))
        self.assertEqual(ids(p)["GSE283317"], "generated")

    def test_analysed_during_this_study_is_reuse(self):
        # PMID 41646335 (Springer reuse template without "generated")
        lab, p = label(das("The single-cell RNA sequencing datasets analyzed during this study are publicly available "
                           "from the Gene Expression Omnibus (GEO) under accession numbers GSE149614 and GSE189903."))
        # authors use this wording for reuse and for their own deposits: unclear, not own by default (the GEO
        # cross-check settles GEO series from their records)
        self.assertEqual(ids(p)["GSE149614"], "unknown")
        self.assertNotEqual(lab, "PUBLIC_REPOSITORY")

    def test_krt_row_without_source_is_unclear(self):
        # PMID 39208801: "Raw proteomics data | ProteomeXchange Consortium | PXD051527" in a key resources table
        _, p = label(jats(body='<table-wrap><caption><p>Key resources table</p></caption><table><tr><td>Raw '
                               'proteomics data</td><td>ProteomeXchange Consortium</td><td>ProteomeXchange '
                               'Consortium: PXD051527</td></tr><tr><td>Seurat</td><td>Satija lab</td><td>'
                               'https://github.com/satijalab/seurat</td></tr></table></table-wrap>'))
        self.assertEqual(ids(p)["PXD051527"], "unknown")
        self.assertEqual(ids(p)["github.com/satijalab/seurat"], "reused")

    def test_no_data_template_is_not_reuse(self):
        # the Springer no-data template must not trip the "analysed during the current study" reuse cue
        lab, _ = label(das("Data sharing is not applicable to this article as no datasets were generated or analysed "
                           "during the current study."))
        self.assertEqual(lab, "NO_DATA_GENERATED")

    def test_generated_and_or_analysed_is_own(self):
        # PMID 41951681: the "generated and/or analysed" template stays own
        _, p = label(das("The datasets generated and/or analyzed during the current study are available in the Gene "
                         "Expression Omnibus repository, GSE314400."))
        self.assertEqual(ids(p)["GSE314400"], "generated")

    def test_prior_studies_is_reuse(self):
        # PMID 41713191
        _, p = label(das("Genomics data presented were generated in prior studies and are publicly available on the "
                         "NCBI Gene Expression Omnibus under the accession numbers GSE119619 and GSE130275."))
        self.assertEqual(ids(p)["GSE119619"], "reused")

    def test_previous_study_beats_nearer_deposit_verb(self):
        # PMID 41309628 (the superscript reference "11" after "study" is now separated by _flat)
        _, p = label(das("The DNA methylation data used in this study were generated for a previous study"
                         "<sup><xref ref-type=\"bibr\" rid=\"R11\">11</xref></sup>, and have been deposited in the NCBI "
                         "Gene Expression Omnibus database under accession number GSE197674."))
        self.assertEqual(ids(p)["GSE197674"], "reused")

    def test_previously_deposited_is_reuse(self):
        _, p = label(das("Data were previously deposited in GEO under GSE12345."))
        self.assertEqual(ids(p)["GSE12345"], "reused")

    def test_methods_previously_described_still_own(self):
        _, p = label(das("RNA-seq was performed as previously described and data have been deposited in GEO "
                         "(GSE123457)."))
        self.assertEqual(ids(p)["GSE123457"], "generated")

    def test_public_hyphenated_data_is_reuse(self):
        # PMID 40394007
        _, p = label(das("Public scRNA-seq data used in this study are available from GEO under the following "
                         "accession codes: GSE141445 (scRNA-seq data of 13 primary PCa patients)."))
        self.assertEqual(ids(p)["GSE141445"], "reused")

    def test_described_in_the_article_is_not_own(self):
        _, p = label(das("The scRNA-seq data described in the article by Smith (GSE120001) were used."))
        self.assertEqual(ids(p)["GSE120001"], "reused")

    def test_citation_superscript_not_glued_to_accession(self):
        # PMID 40319015: GSE51800<sup>41</sup> was read as GSE5180041; PMID 38553455: zenodo.10426508<sup>171</sup>
        _, p = label(das('The BRD4 ChIP-seq is available under accession code <ext-link ext-link-type="uri" '
                         'xlink:href="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE51800">GSE51800</ext-link>'
                         '<sup><xref rid="CR41" ref-type="bibr">41</xref></sup>. Code is also publicly available in '
                         'the Zenodo repository 10.5281/zenodo.10426508<sup><xref rid="CR171" ref-type="bibr">171'
                         '</xref></sup>.'))
        self.assertIn("GSE51800", ids(p))
        self.assertNotIn("GSE5180041", ids(p))
        self.assertIn("10.5281/zenodo.10426508", ids(p))
        self.assertNotIn("10.5281/zenodo.10426508171", ids(p))

    def test_unicode_hyphens(self):
        # PMID 41793187 "EMD‐73285"; PMID 41216961 "EV‐Library" was truncated to github.com/.../ev
        _, p = label(das("For VCP hexamer, PDB ID: 9YP6 and EMDB ID: EMD‐73285. Data are in the GitHub repository "
                         "https://github.com/HaradaLabMSU/EV‐Library."))
        self.assertIn("EMD-73285", ids(p))
        self.assertIn("github.com/haradalabmsu/ev-library", ids(p))
        self.assertNotIn("github.com/haradalabmsu/ev", ids(p))

    def test_url_case_is_kept(self):
        # PMID 40385416: lowercasing made https://epigenome.wustl.edu/MASLD/ a false 404
        _, p = label(das("Processed data are available at https://epigenome.wustl.edu/MASLD/."))
        self.assertIn("https://epigenome.wustl.edu/MASLD/", ids(p))

    def test_url_dedup_is_case_insensitive(self):
        _, p = label(das("Data are at http://Example.org/Data and at https://example.org/data."))
        self.assertEqual(sum(1 for k in ids(p) if "example.org" in k.lower()), 1)

    def test_code_ocean_capsule_and_doi(self):
        # PMID 40530506: the capsule number is not a DOI; DOIs need their version
        _, p = label(das("Codes used in this study are available in Code Ocean "
                         "(https://codeocean.com/capsule/7370617/tree/v1) and at 10.24433/CO.0687784.v1."))
        self.assertIn("codeocean.com/capsule/7370617", ids(p))
        self.assertIn("10.24433/CO.0687784.v1", ids(p))
        self.assertNotIn("10.24433/CO.7370617", ids(p))

    def test_mendeley_version_kept(self):
        # PMID 40578344: 10.17632/ch6vymw564 (no version) is not registered
        _, p = label(das("Original images were deposited at Mendeley Data: doi: 10.17632/ch6vymw564.1 and "
                         "https://data.mendeley.com/datasets/vf4g4tjc9j/1"))
        self.assertIn("10.17632/ch6vymw564.1", ids(p))
        self.assertIn("10.17632/vf4g4tjc9j.1", ids(p))

    # --- availability wording is not a reuse cue (found by tracing awards, 2026-10-03) -------------------------
    def test_can_be_accessed_is_not_reuse(self):
        # PMID 42393054: "accessed on" was read as "the authors accessed someone else's data"
        lab, p = label(das("RNA-seq and ATAC-seq data were generated for this study and can be accessed on the Gene "
                           "Expression Omnibus (GEO) database with accession numbers GSE305144 and GSE303985."))
        self.assertEqual(ids(p)["GSE305144"], "generated")
        self.assertEqual(lab, "PUBLIC_REPOSITORY")

    def test_code_can_be_accessed_via_github(self):
        # PMID 42239047
        lab, p = label(das("The code used to reproduce the analyses described in this manuscript can be accessed via "
                           "GitHub (https://github.com/somelab/some_project)."))
        self.assertEqual(ids(p)["github.com/somelab/some_project"], "generated")
        self.assertEqual(lab, "PUBLIC_REPOSITORY")

    def test_were_accessed_from_is_still_reuse(self):
        # PMID 41760660: past tense says what the authors did
        _, p = label(das("The following human islet scRNA-seq datasets were accessed from GEO: GSE101207, GSE81608."))
        self.assertEqual(ids(p)["GSE101207"], "reused")

    def test_repository_citation_et_al_is_not_reuse(self):
        # PMID 40161716: the standard PRIDE sentence cites the repository's own paper
        _, p = label(das("The mass spectrometry proteomics data have been deposited to the ProteomeXchange Consortium "
                         "via the PRIDE (Vizcaino et al., 2013) partner repository with the dataset identifier "
                         "PXD061273."))
        self.assertEqual(ids(p)["PXD061273"], "generated")

    def test_data_from_et_al_is_still_reuse(self):
        # PMID 40723219
        _, p = label(das("The LUAD IMC data was obtained from Sorin et al. [39] and is available on Zenodo "
                         "(DOI: 10.5281/zenodo.7760826)."))
        self.assertEqual(ids(p)["10.5281/zenodo.7760826"], "reused")

    def test_public_repository_names_the_place(self):
        # PMID 40023154: "public proteomics repository" describes MassIVE, not the data's origin
        _, p = label(das("The original mass spectra were deposited in the public proteomics repository MassIVE "
                         "under accession MSV000087743."))
        self.assertEqual(ids(p)["MSV000087743"], "generated")

    def test_publicly_available_in_repository(self):
        # PMID 41860764
        _, p = label(das("The whole exome sequencing data generated in this study are publicly available in the "
                         "database of Genotypes and Phenotypes (dbGaP) under accession number phs004441.v1.p1."))
        self.assertEqual(ids(p)["phs004441"], "generated")

    def test_datasets_used_in_this_study_is_unclear(self):
        # PMID 39947133: 19 public BioProjects listed this way must not become own deposits
        lab, p = label(das("Datasets used in this study are publicly available and can be accessed through NCBI "
                           "BioProject database using the following accession numbers: PRJNA398089, PRJNA389280."))
        self.assertNotEqual(ids(p)["PRJNA398089"], "generated")
        self.assertNotEqual(lab, "PUBLIC_REPOSITORY")

    def test_codes_used_in_this_study_is_own(self):
        # PMID 42027209: "used in this study" is ambiguous for data, not for the paper's code
        _, p = label(das("The codes used in this study are available in the GitHub repository at "
                         "https://github.com/somelab/density_model."))
        self.assertEqual(ids(p)["github.com/somelab/density_model"], "generated")

    def test_reviewer_tokens_are_removed_from_quoted_sentences(self):
        r = extract.redact_access
        self.assertEqual(r("available in GEO (GSE000001; reviewer token: abcdefghijkl)."),
                         "available in GEO (GSE000001; reviewer token: [removed]).")
        self.assertEqual(r("log-in token for reviewers: abcdefghijkl"), "log-in token for reviewers: [removed]")
        self.assertEqual(r("reviewer login MSV1_reviewer and password Abcdef1."),
                         "reviewer login MSV1_reviewer and password [removed]")
        self.assertEqual(r("https://zenodo.org/records/1?token=eyJhbGci.abc"),
                         "https://zenodo.org/records/1?token=[removed]")
        same = "The data are password protected and a token is required."
        self.assertEqual(r(same), same)


if __name__ == "__main__":
    unittest.main()
