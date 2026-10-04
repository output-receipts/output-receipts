"""Resolver statuses, the GEO cross-check, study grouping and the dead-link definition, with a fake HTTP client
that returns real (shortened) responses recorded in the FY2024 NCI R01 run. No network.
Run: python -m unittest discover tests"""
import unittest

from receipts import classify, crosscheck, records, report, resolve
from receipts.fetch import Response


class FakeHttp:
    """url (or url prefix) -> (status, text); anything else answers 404. Records the calls."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def request(self, url, method="GET", **kw):
        self.calls.append((method, url))
        for k, v in self.table.items():
            if url == k or url.startswith(k):
                st, txt = v(method) if callable(v) else v
                return Response(url, st, txt)
        return Response(url, 404, "")


def geo_text(gse, public, pmids=(), sub_of=()):
    return (200, f"^SERIES = {gse}\r\n!Series_title = t\r\n!Series_geo_accession = {gse}\r\n"
                 f"!Series_status = Public on {public}\r\n"
                 + "".join(f"!Series_pubmed_id = {p}\r\n" for p in pmids)
                 + "".join(f"!Series_relation = SubSeries of: {s}\r\n" for s in sub_of))


class ResolverStatuses(unittest.TestCase):
    def test_massive_private_dataset_is_private(self):
        # MSV000100954: QueryMSV answers 200 with a page titled "MassIVE Private Dataset"
        h = FakeHttp({"https://massive.ucsd.edu/": (200, "<html><head><title>MassIVE Private Dataset</title>")})
        self.assertEqual(resolve.resolve_one(h, "MassIVE", "MSV000100954")["status"], "PRIVATE")

    def test_pride_reserved_is_private(self):
        # PXD066950: ProteomeCentral answers 404 "has been reserved but it not yet accessible" (code 1003)
        h = FakeHttp({"https://proteomecentral.proteomexchange.org/": (
            404, "result=ERROR message=The identifier 'PXD066950' has been reserved but it not yet accessible "
                 "code=1003")})
        self.assertEqual(resolve.resolve_one(h, "PRIDE/ProteomeXchange", "PXD066950")["status"], "PRIVATE")

    def test_dbgap_latest_version_without_data_falls_back(self):
        # phs002371: summary API 404 "No data found for study with accession phs002371.v10"; the study is public
        h = FakeHttp({
            f"{resolve.NCBI}/gap/sstr/api/v1/study/phs002371/summary": (
                404, '{"error":{"error_message":"No data found for study with accession phs002371.v10"}}'),
            f"{resolve.EUTILS}/esearch.fcgi?db=gap": (200, '{"esearchresult":{"count":"3"}}')})
        res = resolve.resolve_one(h, "dbGaP", "phs002371")
        self.assertEqual(res["status"], "RESOLVES")
        self.assertIn("E-utilities", res["note"])

    def test_dbgap_really_missing_stays_not_found(self):
        h = FakeHttp({
            f"{resolve.NCBI}/gap/sstr/api/v1/study/phs999999/summary": (404, '{"error":{}}'),
            f"{resolve.EUTILS}/esearch.fcgi?db=gap": (200, '{"esearchresult":{"count":"0"}}')})
        self.assertEqual(resolve.resolve_one(h, "dbGaP", "phs999999")["status"], "NOT_FOUND")

    def test_head_404_is_confirmed_with_get(self):
        h = FakeHttp({"https://example.org/": lambda m: (404, "") if m == "HEAD" else (200, "ok")})
        self.assertEqual(resolve.resolve_one(h, "Other link", "https://example.org/Page")["status"], "RESOLVES")
        self.assertEqual([m for m, _ in h.calls], ["HEAD", "GET"])

    def test_code_ocean_capsule_checked_as_url(self):
        h = FakeHttp({"https://codeocean.com/capsule/7370617": (200, "")})
        self.assertEqual(resolve.resolve_one(h, "Code Ocean", "codeocean.com/capsule/7370617")["status"], "RESOLVES")


def paper(pmid, date, ids):
    return {"pmid": pmid, "first_pub_date": date, "fulltext_source": "x", "grants": ["G"],
            "extraction": {"identifiers": [dict(repo=r, id=i, role=role, cls=c, is_code=False, evidence="e",
                                                locations=["das"], context="", raw=i)
                                           for r, i, role, c in ids], "cues": {}, "reuse_resources": []}}


class GeoCrossCheck(unittest.TestCase):
    def test_predating_series_linked_elsewhere_is_demoted(self):
        # PMID 40394007 (2025-05-20) listed GSE141445 (public 2020-10-22, linked to PMID 33420488) as if own
        h = FakeHttp({resolve.geo_record_url("GSE141445"): geo_text("GSE141445", "Oct 22 2020", ["33420488"])})
        ps = {"40394007": paper("40394007", "2025-05-20", [("GEO", "GSE141445", "generated", "public")])}
        self.assertEqual(crosscheck.demote_predating_geo(ps, h), 1)
        i = ps["40394007"]["extraction"]["identifiers"][0]
        self.assertEqual((i["role"], i["evidence"]), ("reused", crosscheck.DEMOTE_EVIDENCE))
        self.assertEqual(crosscheck.demote_predating_geo(ps, h), 0)   # idempotent

    def test_series_linked_to_this_paper_or_recent_is_kept(self):
        h = FakeHttp({resolve.geo_record_url("GSE1"): geo_text("GSE1", "Jan 05 2020", ["111"]),
                      resolve.geo_record_url("GSE2"): geo_text("GSE2", "Jan 05 2025", ["999"]),
                      resolve.geo_record_url("GSE3"): geo_text("GSE3", "Jan 05 2020", [])})
        ps = {"111": paper("111", "2025-06-01", [("GEO", "GSE1", "generated", "public"),
                                                 ("GEO", "GSE2", "generated", "public"),
                                                 ("GEO", "GSE3", "generated", "public"),
                                                 ("GEO", "GSE4", "generated", "public")])}   # GSE4 not cached
        self.assertEqual(crosscheck.demote_predating_geo(ps, h), 0)

    def test_unclear_series_linked_to_this_paper_is_promoted(self):
        # PMID 39731910: a data table "Gene expression data | GEO | GEO: GSE274992" (no "this study"); GEO links
        # the series to this paper. PMID 40951926: "RNA-seq data analyzed in this study are available ... GSE303643"
        h = FakeHttp({resolve.geo_record_url("GSE274992"): geo_text("GSE274992", "Nov 01 2024", ["39731910"]),
                      resolve.geo_record_url("GSE303643"): geo_text("GSE303643", "Jul 01 2025", ["40951926"]),
                      resolve.geo_record_url("GSE5"): geo_text("GSE5", "Jul 01 2025", ["39731910"])})
        p1 = paper("39731910", "2024-12-27", [("GEO", "GSE274992", "reused", "public"),
                                              ("GEO", "GSE5", "reused", "public")])
        p1["extraction"]["identifiers"][0]["evidence"] = crosscheck.BODY_DEFAULT
        p1["extraction"]["identifiers"][1]["evidence"] = "reuse cue in sentence"     # an explicit cue is kept
        p2 = paper("40951926", "2025-09-01", [("GEO", "GSE303643", "unknown", "public")])
        ps = {"39731910": p1, "40951926": p2}
        self.assertEqual(crosscheck.apply(ps, h), (0, 2))
        self.assertEqual([i["role"] for i in p1["extraction"]["identifiers"]], ["generated", "reused"])
        self.assertEqual(p2["extraction"]["identifiers"][0]["evidence"], crosscheck.PROMOTE_EVIDENCE)

    def test_study_groups(self):
        # PMID 40970704 cites PRJNA549513 and its run SRR33570754; a GEO SubSeries of a cited SuperSeries
        h = FakeHttp({resolve.geo_record_url("GSE291118"): geo_text("GSE291118", "Mar 01 2025", [], ["GSE291121"])})
        ps = {"1": paper("1", "2025-06-01", [("SRA/BioProject", "PRJNA549513", "generated", "public"),
                                             ("SRA/BioProject", "SRR33570754", "generated", "public"),
                                             ("GEO", "GSE291118", "generated", "public"),
                                             ("GEO", "GSE291121", "generated", "public"),
                                             ("EGA", "EGAS00001006425", "generated", "controlled"),
                                             ("EGA", "EGAD00001009066", "generated", "controlled")])}
        g = crosscheck.study_groups(ps, h)
        self.assertEqual(g[("SRA/BioProject", "SRR33570754")], "SRA/BioProject:PRJNA549513")
        self.assertEqual(g[("GEO", "GSE291118")], "GEO:GSE291121")
        self.assertEqual(g[("EGA", "EGAD00001009066")], "EGA:EGAS00001006425")
        self.assertNotIn(("SRA/BioProject", "PRJNA549513"), g)


class RecordChecks(unittest.TestCase):
    """records.py: what the repository's own record says, compared with the paper (shortened real responses)."""

    PDB = ('{"rcsb_accession_info": {"initial_release_date": "2026-03-18T00:00:00.000+00:00"}, '
           '"rcsb_primary_citation": {"pdbx_database_id_PubMed": 41793187, "pdbx_database_id_DOI": '
           '"10.1002/advs.202520545", "rcsb_authors": ["Tamayo-Jaramillo, D.", "Shen, P.S."]}, '
           '"audit_author": [{"name": "Tamayo-Jaramillo, D."}], '
           '"pdbx_audit_support": [{"grant_number": "R01 CA293084"}]}')

    def test_names(self):
        self.assertEqual(records.person("Tamayo-Jaramillo, D."), ("tamayojaramillo", "d"))
        self.assertEqual(records.person("Hisham,,Mohammed", "geo"), ("mohammed", "h"))
        self.assertEqual(records.person("Aaron Storey"), ("storey", "a"))
        self.assertEqual(records.person("Park laboratory"), ("park", ""))
        self.assertEqual(records.paper_people("Mohammed H, Hernando-Herraez I, Reik W."),
                         {("mohammed", "h"), ("hernandoherraez", "i"), ("reik", "w")})
        self.assertTrue(records.people_match({("park", "")}, {("park", "j")}))
        self.assertFalse(records.people_match({("wang", "y")}, {("wang", "x")}))

    def test_record_cites_this_paper(self):
        h = FakeHttp({"https://data.rcsb.org/rest/v1/core/entry/10QQ": (200, self.PDB)})
        p = paper("41793187", "2026-03-01", [("PDB", "10QQ", "unknown", "public")])
        self.assertEqual(records.check(p, records.record(h, "PDB", "10QQ"))[0], "cites_paper")
        self.assertEqual(crosscheck.record_checks({"41793187": p}, h), (0, 1))     # unclear -> own
        self.assertEqual(p["extraction"]["identifiers"][0]["role"], "generated")
        self.assertEqual(crosscheck.record_checks({"41793187": p}, h), (0, 0))     # idempotent

    def test_record_names_the_award(self):
        h = FakeHttp({"https://data.rcsb.org/rest/v1/core/entry/10QQ": (200, self.PDB)})
        p = paper("5", "2026-06-01", [("PDB", "10QQ", "generated", "public")])
        p["grants"] = ["R01CA293084"]
        self.assertEqual(records.check(p, records.record(h, "PDB", "10QQ"))[0], "names_award")

    def test_old_record_citing_other_papers_contradicts(self):
        # PMID 38064712 listed PDB 1P5Z (released 2003, another paper's structure) in "data generated in this study"
        old = self.PDB.replace("2026-03-18", "2003-07-08")
        h = FakeHttp({"https://data.rcsb.org/rest/v1/core/entry/1P5Z": (200, old)})
        p = paper("38064712", "2023-12-08", [("PDB", "1P5Z", "generated", "public")])
        self.assertEqual(crosscheck.record_checks({"38064712": p}, h), (1, 0))
        i = p["extraction"]["identifiers"][0]
        self.assertEqual((i["role"], i["record_check"], i["role_before_crosscheck"]),
                         ("reused", "contradicted", "generated"))

    def test_submitter_is_an_author(self):
        # a GEO series with no paper listed yet, submitted by one of the paper's authors
        geo = (200, "^SERIES = GSE7\r\n!Series_title = t\r\n!Series_status = Public on Jan 05 2025\r\n"
                    "!Series_contributor = Wolf,,Reik\r\n!Series_contact_name = Hisham,,Mohammed\r\n")
        h = FakeHttp({resolve.geo_record_url("GSE7"): geo})
        p = paper("7", "2025-06-01", [("GEO", "GSE7", "generated", "public")])
        p["authors"] = "Mohammed H, Reik W."
        self.assertEqual(records.check(p, records.record(h, "GEO", "GSE7"))[0], "authors")
        p["authors"] = "Smith J, Jones K."       # other people, record is recent and cites nothing: no verdict
        self.assertEqual(records.check(p, records.record(h, "GEO", "GSE7"))[0], "")

    def test_other_people_and_another_paper_contradicts(self):
        geo = (200, "^SERIES = GSE8\r\n!Series_title = t\r\n!Series_status = Public on Jan 05 2025\r\n"
                    "!Series_pubmed_id = 999\r\n!Series_contributor = Wolf,,Reik\r\n")
        h = FakeHttp({resolve.geo_record_url("GSE8"): geo})
        p = paper("8", "2025-06-01", [("GEO", "GSE8", "generated", "public")])
        p["authors"] = "Smith J, Jones K."
        self.assertEqual(records.check(p, records.record(h, "GEO", "GSE8"))[0], "contradicted")
        p["authors"] = "Reik W, Jones K."        # the same group's companion paper: not contradicted
        self.assertEqual(records.check(p, records.record(h, "GEO", "GSE8"))[0], "authors")

    def test_datacite_creators(self):
        dc = ('{"data": {"attributes": {"creators": [{"name": "Xiao, Yujie", "familyName": "Xiao", "givenName": '
              '"Yujie"}], "created": "2025-04-17T00:00:00Z", "dates": [{"date": "2025", "dateType": "Issued"}], '
              '"relatedIdentifiers": [{"relationType": "IsVersionOf", "relatedIdentifierType": "DOI", '
              '"relatedIdentifier": "10.5281/zenodo.1"}], "fundingReferences": []}}}')
        h = FakeHttp({records.datacite_url("10.5281/zenodo.2"): (200, dc)})
        p = paper("9", "2025-06-01", [("Zenodo", "10.5281/zenodo.2", "generated", "generic")])
        p["authors"] = "Xiao Y, Chen A."
        self.assertEqual(records.check(p, records.record(h, "Zenodo", "10.5281/zenodo.2"))[0], "authors")
        p["authors"] = "Smith J."                # DataCite related identifiers are never evidence against a paper
        self.assertEqual(records.check(p, records.record(FakeHttp({records.datacite_url("10.5281/zenodo.2"):
                                                                   (200, dc)}), "Zenodo", "10.5281/zenodo.2"))[0], "")

    def test_old_record_by_the_same_people_is_no_verdict(self):
        # PMID 41791379 listed GSE65888 (the group's 2015 series, no paper in GEO) in a resources table:
        # the same people, but possibly their earlier work. It must not be promoted to an own deposit.
        geo = (200, "^SERIES = GSE65888\r\n!Series_title = t\r\n!Series_status = Public on Feb 12 2015\r\n"
                    "!Series_contributor = Wolf,,Reik\r\n")
        h = FakeHttp({resolve.geo_record_url("GSE65888"): geo})
        p = paper("41791379", "2026-03-01", [("GEO", "GSE65888", "unknown", "public")])
        p["authors"] = "Reik W, Jones K."
        self.assertEqual(crosscheck.apply({"41791379": p}, h), (0, 0))
        self.assertEqual(p["extraction"]["identifiers"][0]["role"], "unknown")

    def test_username_creators_cannot_contradict(self):
        # zenodo.13871131: creators "bkim6" and "Park laboratory"; the paper's authors include Park JI
        dc = ('{"data": {"attributes": {"creators": [{"name": "bkim6", "familyName": "bkim6"}, {"name": '
              '"Park laboratory", "familyName": "Park laboratory"}], "created": "2024-10-01T00:00:00Z", '
              '"dates": [{"date": "2023-10-09", "dateType": "Issued"}]}}}')
        url = records.datacite_url("10.5281/zenodo.13871131")
        p = paper("39448571", "2024-12-01", [("Zenodo", "10.5281/zenodo.13871131", "generated", "generic")])
        p["authors"] = "Kim B, Park JI."
        self.assertEqual(records.check(p, records.record(FakeHttp({url: (200, dc)}), "Zenodo",
                                                         "10.5281/zenodo.13871131"))[0], "")   # old: no verdict
        p["authors"] = "Smith J."
        self.assertEqual(records.check(p, records.record(FakeHttp({url: (200, dc)}), "Zenodo",
                                                         "10.5281/zenodo.13871131"))[0], "")   # names unreliable

    def test_apply_can_be_rerun_after_a_rule_change(self):
        old = self.PDB.replace("2026-03-18", "2003-07-08")
        h = FakeHttp({"https://data.rcsb.org/rest/v1/core/entry/1P5Z": (200, old)})
        p = paper("38064712", "2023-12-08", [("PDB", "1P5Z", "generated", "public")])
        self.assertEqual(crosscheck.apply({"38064712": p}, h), (1, 0))
        self.assertEqual(crosscheck.apply({"38064712": p}, h), (1, 0))      # recomputed from the sentence-rule role
        self.assertEqual(p["extraction"]["identifiers"][0]["role_before_crosscheck"], "generated")

    def test_no_record_is_no_verdict(self):
        p = paper("1", "2025-01-01", [("MassIVE", "MSV000000001", "generated", "public")])
        self.assertEqual(crosscheck.record_checks({"1": p}, FakeHttp({})), (0, 0))
        self.assertNotIn("record_check", p["extraction"]["identifiers"][0])


class DeadLinkDefinition(unittest.TestCase):
    def test_flag_uses_own_repository_identifiers_only(self):
        p = paper("1", "2025-01-01", [("Other link", "https://x.org/A", "generated", "url"),
                                      ("GEO", "GSE5", "unknown", "public"),
                                      ("Zenodo", "10.5281/zenodo.1", "generated", "generic")])
        res = {"Other link|https://x.org/A": {"status": "NOT_FOUND"}, "GEO|GSE5": {"status": "NOT_FOUND"},
               "Zenodo|10.5281/zenodo.1": {"status": "RESOLVES"}}
        self.assertEqual(classify.flags_for(p, "PUBLIC_REPOSITORY", res)["LINK_DEAD"], [])
        self.assertEqual([i["id"] for i in report.own_identifiers(p)], ["10.5281/zenodo.1"])

    def test_needs_review_is_a_reported_bucket(self):
        self.assertIn("NEEDS_REVIEW", report.LABEL_ORDER)
        self.assertEqual(report.LABEL_ORDER[:len(classify.LABELS)], list(classify.LABELS))  # dashboard indices


if __name__ == "__main__":
    unittest.main()
