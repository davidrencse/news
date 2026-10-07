"""Offline checks for DOI normalization and Crossref record mapping. No network."""
import unittest
import urllib.error
from unittest.mock import patch

import paper_sources as ps


class NormalizeDoiTests(unittest.TestCase):
    def test_accepts_common_paste_forms(self):
        for raw in ("10.1145/3292500.3330701",
                    "https://doi.org/10.1145/3292500.3330701",
                    "https://dx.doi.org/10.1145/3292500.3330701",
                    "doi:10.1145/3292500.3330701",
                    "  10.1145/3292500.3330701.  "):
            self.assertEqual(ps.normalize_doi(raw), "10.1145/3292500.3330701")

    def test_lowercases_and_trims_trailing_punctuation(self):
        self.assertEqual(ps.normalize_doi("10.1038/S41586-021-03819-2)"), "10.1038/s41586-021-03819-2")

    def test_rejects_non_doi(self):
        for raw in ("", "not a doi", "arXiv:1412.6980", "12345"):
            with self.assertRaises(ValueError):
                ps.normalize_doi(raw)


class EnrichTests(unittest.TestCase):
    def _record(self, overrides=None):
        message = {
            "DOI": "10.1145/3292500.3330701",
            "title": ["Optuna: A Next-generation   Hyperparameter &amp; Optimization"],
            "author": [{"given": "Takuya", "family": "Akiba"}, {"name": "ACM"}],
            "abstract": "<jats:p>We describe <jats:italic>and</jats:italic> evaluate &amp; test.</jats:p>",
            "container-title": ["Proceedings of KDD &amp; Data Mining"],
            "publisher": "ACM", "type": "proceedings-article",
            "issued": {"date-parts": [[2019, 7, 25]]},
            "is-referenced-by-count": 8571, "references-count": 40, "subject": ["ML"],
        }
        message.update(overrides or {})
        with patch.object(ps, "_request", return_value={"message": message}):
            return ps.enrich("10.1145/3292500.3330701")

    def test_decodes_entities_in_title_and_venue(self):
        r = self._record()
        self.assertEqual(r["title"], "Optuna: A Next-generation Hyperparameter & Optimization")
        self.assertEqual(r["venue"], "Proceedings of KDD & Data Mining")

    def test_strips_jats_and_decodes_abstract(self):
        self.assertEqual(self._record()["abstract"], "We describe and evaluate & test.")

    def test_authors_people_and_orgs(self):
        self.assertEqual(self._record()["authors"], ["Takuya Akiba", "ACM"])

    def test_date_year_and_stable_id(self):
        import hashlib
        r = self._record()
        self.assertEqual(r["published"], "2019-07-25")
        self.assertEqual(r["year"], 2019)
        # id is a stable 16-hex digest of the normalized DOI, so re-imports update in place.
        self.assertEqual(r["id"], hashlib.sha256(b"10.1145/3292500.3330701").hexdigest()[:16])
        self.assertEqual(r["cited_by"], 8571)

    def test_untitled_fallback(self):
        self.assertEqual(self._record({"title": []})["title"], "Untitled")

    def test_missing_doi_raises_lookup(self):
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with patch.object(ps, "_request", side_effect=err):
            with self.assertRaises(LookupError):
                ps.enrich("10.9999/does-not-exist")


class SearchTests(unittest.TestCase):
    def _payload(self, dois):
        return {"message": {"items": [
            {"DOI": d, "title": [f"Paper {d}"], "author": [{"family": "Doe", "given": "J"}],
             "container-title": ["Security & Privacy"], "issued": {"date-parts": [[2021]]},
             "type": "proceedings-article"} for d in dois]}}

    def test_maps_items_and_dedupes(self):
        payload = self._payload(["10.1/a", "10.1/b", "10.1/a"])  # duplicate DOI
        with patch.object(ps, "_request", return_value=payload):
            records = ps.search("cve vulnerability", rows=50)
        self.assertEqual([r["doi"] for r in records], ["10.1/a", "10.1/b"])
        self.assertEqual(records[0]["venue"], "Security & Privacy")  # entities decoded

    def test_skips_items_without_doi(self):
        payload = {"message": {"items": [{"title": ["No DOI"]}, {"DOI": "10.1/x", "title": ["Has DOI"]}]}}
        with patch.object(ps, "_request", return_value=payload):
            records = ps.search("x")
        self.assertEqual([r["doi"] for r in records], ["10.1/x"])

    def test_empty_query_rejected(self):
        with self.assertRaises(ValueError):
            ps.search("   ")

    def test_rows_clamped_to_100(self):
        captured = {}
        def fake(url, timeout=18, limit=None):
            captured["url"] = url
            return {"message": {"items": []}}
        with patch.object(ps, "_request", side_effect=fake):
            ps.search("cve", rows=5000)
        self.assertIn("rows=100", captured["url"])


if __name__ == "__main__":
    unittest.main()
