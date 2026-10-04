"""Offline checks for pipeline tuning and CVE import batch boundaries."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import cve_catalog
from performance import setting


class PerformanceTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(setting("TEST_WORKERS", 4, 1, 16), 4)
        with patch.dict(os.environ, {"TEST_WORKERS": "8", "TEST_GAP": "0.5"}):
            self.assertEqual(setting("TEST_WORKERS", 4, 1, 16), 8)
            self.assertEqual(setting("TEST_GAP", 1.0, 0.1, 60), 0.5)

    def test_invalid_counts_are_rejected(self):
        for raw in ("0", "-1", "17", "2.5", "many", ""):
            with self.subTest(raw=raw), patch.dict(os.environ, {"TEST_WORKERS": raw}):
                with self.assertRaisesRegex(ValueError, "TEST_WORKERS"):
                    setting("TEST_WORKERS", 4, 1, 16)

    def test_nonfinite_intervals_are_rejected(self):
        for raw in ("nan", "inf", "-inf"):
            with self.subTest(raw=raw), patch.dict(os.environ, {"TEST_GAP": raw}):
                with self.assertRaisesRegex(ValueError, "TEST_GAP"):
                    setting("TEST_GAP", 1.0, 0.1, 60)

    def test_catalog_flushes_full_batch_and_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = cve_catalog.CVECatalog(str(Path(folder) / "catalog.sqlite3"))
            archive = Path(folder) / "records.zip"
            count = cve_catalog.CVE_IMPORT_BATCH_SIZE + 1
            with zipfile.ZipFile(archive, "w") as out:
                for i in range(count):
                    cve_id = f"CVE-2026-{10000 + i}"
                    out.writestr(f"{cve_id}.json", json.dumps({
                        "cveMetadata": {"cveId": cve_id, "state": "PUBLISHED"},
                        "containers": {"cna": {"descriptions": [
                            {"lang": "en", "value": f"Batch boundary record {i}"}]}}
                    }))
            with patch.object(catalog, "_write_batch", wraps=catalog._write_batch) as write:
                self.assertEqual(catalog._import_zip(archive), count)
                self.assertEqual(write.call_count, 2)
            with catalog.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM records").fetchone()[0], count)
                self.assertEqual(db.execute("SELECT count(*) FROM records_fts WHERE records_fts MATCH 'boundary'").fetchone()[0], count)
                raw = db.execute("SELECT raw FROM records WHERE id=?", [f"CVE-2026-{10000 + count - 1}"]).fetchone()[0]
                self.assertEqual(json.loads(raw)["cveMetadata"]["state"], "PUBLISHED")


if __name__ == "__main__":
    unittest.main()
