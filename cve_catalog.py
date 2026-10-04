"""Full CVE List V5 mirror with background sync and SQLite-backed search."""
import json
import os
import re
import sqlite3
import shutil
import tempfile
import threading
import time
import urllib.request
import zipfile
from contextlib import contextmanager
from performance import CVE_IMPORT_BATCH_SIZE

API = "https://api.github.com/repos/CVEProject/cvelistV5/releases?per_page=100"
UA = "LibraryOfBabel/1.0 (personal research library)"
CVE_RE = re.compile(r"^CVE-(\d{4})-(\d{4,})$", re.I)


def _request(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=45)


def _description(record):
    containers = record.get("containers") or {}
    for container in [containers.get("cna") or {}, *(containers.get("adp") or [])]:
        for desc in container.get("descriptions") or []:
            if str(desc.get("lang", "en")).lower().startswith("en") and desc.get("value"):
                return desc["value"]
    return ""


class CVECatalog:
    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.lock = threading.RLock()
        self.job_lock = threading.Lock()
        self.rebuilding = False
        self.job = {"state": "idle", "phase": "", "records": 0, "bytes": 0, "total_bytes": 0,
                    "started": None, "finished": None, "error": None, "release": None}
        self._initialize()

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA cache_size=-65536")
        db.execute("PRAGMA temp_store=MEMORY")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    connect = contextmanager(connect)

    def _initialize(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, year INTEGER, state TEXT, published TEXT, updated TEXT, description TEXT, raw TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS records_year ON records(year DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS records_updated ON records(updated DESC)")
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(id UNINDEXED, description, tokenize='unicode61')")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")

    def status(self):
        with self.connect() as db:
            count = db.execute("SELECT count(*) FROM records").fetchone()[0]
            updated = db.execute("SELECT value FROM metadata WHERE key='last_sync'").fetchone()
            release = db.execute("SELECT value FROM metadata WHERE key='release'").fetchone()
        with self.lock:
            job = dict(self.job)
        return {"count": count, "last_sync": updated[0] if updated else None,
                "release": release[0] if release else None, "job": job}

    def search(self, query="", year=None, limit=40, offset=0):
        query = (query or "").strip()
        limit = max(1, min(int(limit), 100))
        offset = max(0, min(int(offset), 1_000_000))
        with self.connect() as db:
            if query.upper().startswith("CVE-"):
                where, args = "id LIKE ?", [query.upper() + "%"]
                if year:
                    where += " AND year=?"; args.append(int(year))
                total = db.execute(f"SELECT count(*) FROM records WHERE {where}", args).fetchone()[0]
                rows = db.execute(f"SELECT id, year, state, published, updated, description FROM records WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
            elif query:
                terms = [x for x in re.findall(r"[\w.-]+", query) if x]
                if not terms:
                    return {"items": [], "total": 0, "limit": limit, "offset": offset}
                match = " AND ".join('"' + t.replace('"', '""') + '"*' for t in terms)
                where, args = "records_fts MATCH ?", [match]
                if year:
                    where += " AND records.year=?"; args.append(int(year))
                total = db.execute(f"SELECT count(*) FROM records JOIN records_fts USING(id) WHERE {where}", args).fetchone()[0]
                rows = db.execute(f"SELECT records.id, records.year, records.state, records.published, records.updated, records.description FROM records JOIN records_fts USING(id) WHERE {where} ORDER BY records.id DESC LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
            elif year:
                total = db.execute("SELECT count(*) FROM records WHERE year=?", [int(year)]).fetchone()[0]
                rows = db.execute("SELECT id, year, state, published, updated, description FROM records WHERE year=? ORDER BY id DESC LIMIT ? OFFSET ?", [int(year), limit, offset]).fetchall()
            else:
                total = db.execute("SELECT count(*) FROM records").fetchone()[0]
                rows = db.execute("SELECT id, year, state, published, updated, description FROM records ORDER BY id DESC LIMIT ? OFFSET ?", [limit, offset]).fetchall()
        return {"items": [dict(x) for x in rows], "total": total, "limit": limit, "offset": offset}

    def get(self, cve_id):
        with self.connect() as db:
            row = db.execute("SELECT raw FROM records WHERE id=?", [cve_id.upper()]).fetchone()
        return json.loads(row[0]) if row else None

    def start_sync(self):
        with self.job_lock:
            if self.job.get("state") == "running":
                return False
            with self.lock:
                self.job = {"state": "running", "phase": "Checking official releases", "records": 0,
                            "bytes": 0, "total_bytes": 0, "started": time.time(), "finished": None,
                            "error": None, "release": None}
            threading.Thread(target=self._sync, daemon=True, name="cve-catalog-sync").start()
            return True

    def _set(self, **values):
        with self.lock:
            self.job.update(values)

    def _sync(self):
        tmp_path = None
        try:
            with _request(API) as response:
                releases = json.loads(response.read().decode("utf-8"))
            if not releases:
                raise RuntimeError("CVEProject has no published release assets.")
            with self.connect() as db:
                existing = db.execute("SELECT count(*) FROM records").fetchone()[0]
                previous = db.execute("SELECT value FROM metadata WHERE key='release'").fetchone()
            previous_tag = previous[0] if previous else None
            previous_index = next((i for i, r in enumerate(releases) if r.get("tag_name") == previous_tag), None)
            if existing and previous_index is not None:
                delta_assets = [(r, a) for r in releases[:previous_index] for a in r.get("assets", [])
                                if re.search(r"_delta_CVEs_at_\d{4}Z\.zip$", a.get("name", ""), re.I)]
                if not delta_assets:
                    self._set(state="complete", phase="Already current", records=existing,
                              finished=time.time(), release=previous_tag)
                    return
                assets = list(reversed(delta_assets))
                size = sum(int(a.get("size") or 0) for _, a in assets)
                release = releases[0]
                full_import = False
            else:
                full_import = True
            # The official full baseline is the only asset that contains the entire current list.
            if full_import:
                self.rebuilding = True
                candidate = None
                candidate_index = None
                for release_index, release in enumerate(releases):
                    for asset in release.get("assets", []):
                        if re.search(r"_all_CVEs_at_midnight(?:\.zip){1,2}$", asset.get("name", ""), re.I):
                            candidate = (release, asset)
                            candidate_index = release_index
                            break
                    if candidate:
                        break
                if not candidate:
                    raise RuntimeError("Could not find the official full-list baseline ZIP in recent releases.")
                baseline_release, baseline_asset = candidate
                deltas = [(r, a) for r in releases[:candidate_index + 1] for a in r.get("assets", [])
                          if re.search(r"_delta_CVEs_at_\d{4}Z\.zip$", a.get("name", ""), re.I)]
                assets = [(baseline_release, baseline_asset), *reversed(deltas)]
                size = sum(int(a.get("size") or 0) for _, a in assets)
            self._set(phase="Preparing official full snapshot" if full_import else "Preparing incremental updates",
                      total_bytes=size, release=releases[0].get("tag_name") or releases[0].get("name"))
            folder = os.path.dirname(self.db_path)
            downloaded = 0
            count = 0
            for asset_release, zip_asset in assets:
                fd, tmp_path = tempfile.mkstemp(prefix="cvelistV5-", suffix=".zip", dir=folder)
                with os.fdopen(fd, "wb") as out, _request(zip_asset["browser_download_url"]) as response:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        out.write(block); downloaded += len(block)
                        self._set(phase=f"Downloading {zip_asset['name']}", bytes=downloaded)
                self._set(phase=f"Importing {zip_asset['name']}")
                count += self._import_zip(tmp_path)
                os.remove(tmp_path); tmp_path = None
            with self.connect() as db:
                if full_import:
                    self._set(phase="Building searchable CVE text index")
                    db.execute("DELETE FROM records_fts")
                    db.execute("INSERT INTO records_fts(id,description) SELECT id,description FROM records WHERE description != ''")
                    self.rebuilding = False
                db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('last_sync',?)", [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())])
                db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('release',?)", [releases[0].get("tag_name") or releases[0].get("name") or ""])
            self._set(state="complete", phase="Full catalog ready" if full_import else "Catalog updated",
                      records=count if full_import else existing + count, finished=time.time(),
                      release=releases[0].get("tag_name") or releases[0].get("name"))
        except Exception as exc:
            self._set(state="error", phase="Catalog import stopped", error=f"{type(exc).__name__}: {exc}"[:500], finished=time.time())
        finally:
            self.rebuilding = False
            if tmp_path:
                try: os.remove(tmp_path)
                except OSError: pass

    def _import_zip(self, path):
        count, batch = 0, []
        with zipfile.ZipFile(path) as archive, self.connect() as db:
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                if name.lower().endswith(".zip") and info.file_size <= 10 * 1024 * 1024 * 1024:
                    fd, nested_path = tempfile.mkstemp(prefix="cvelistV5-nested-", suffix=".zip", dir=os.path.dirname(self.db_path))
                    try:
                        with os.fdopen(fd, "wb") as nested, archive.open(info) as entry:
                            shutil.copyfileobj(entry, nested, 1024 * 1024)
                        count += self._import_zip(nested_path)
                    finally:
                        try: os.remove(nested_path)
                        except OSError: pass
                    continue
                if not name.lower().endswith(".json"):
                    continue
                basename = name.rsplit("/", 1)[-1]
                if not CVE_RE.match(basename[:-5]) or info.file_size > 8 * 1024 * 1024:
                    continue
                try:
                    with archive.open(info) as entry:
                        record = json.load(entry)
                    meta = record.get("cveMetadata") or {}
                    cve_id = str(meta.get("cveId") or basename[:-5]).upper()
                    if not CVE_RE.fullmatch(cve_id):
                        continue
                    batch.append((cve_id, int(cve_id[4:8]), meta.get("state") or "",
                                  meta.get("datePublished"), meta.get("dateUpdated"), _description(record),
                                  json.dumps(record, ensure_ascii=False, separators=(",", ":"))))
                except (ValueError, OSError, KeyError, TypeError):
                    continue
                if len(batch) >= CVE_IMPORT_BATCH_SIZE:
                    self._write_batch(db, batch); count += len(batch); batch.clear()
                    db.commit()
                    self._set(records=count)
            if batch:
                self._write_batch(db, batch); count += len(batch)
        return count

    def _write_batch(self, db, rows):
        db.executemany("INSERT OR REPLACE INTO records(id,year,state,published,updated,description,raw) VALUES(?,?,?,?,?,?,?)", rows)
        if not self.rebuilding:
            db.executemany("DELETE FROM records_fts WHERE id=?", [(x[0],) for x in rows])
            db.executemany("INSERT INTO records_fts(id,description) VALUES(?,?)", [(x[0], x[5]) for x in rows if x[5]])
