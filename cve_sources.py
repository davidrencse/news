"""Read-only CVE enrichment from public vulnerability feeds."""
import concurrent.futures
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


UA = "LibraryOfBabel/1.0 (personal research library)"
CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.I)
NVD = "https://services.nvd.nist.gov/rest/json/cves/2.0"


def _request(url, data=None, headers=None, timeout=18):
    payload = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=payload, headers={
        "User-Agent": UA,
        "Accept": "application/json, application/xml;q=0.9, */*;q=0.5",
        **({"Content-Type": "application/json"} if payload else {}), **(headers or {}),
    })
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Source response exceeded 8 MiB")
    return raw


def _json(url, data=None, headers=None):
    return json.loads(_request(url, data, headers).decode("utf-8"))


def _nvd(cve_id):
    api_key = os.environ.get("NVD_API_KEY", "").strip()
    headers = {"apiKey": api_key} if api_key else None
    data = _json(NVD + "?" + urllib.parse.urlencode({"cveId": cve_id}), headers=headers)
    entries = data.get("vulnerabilities", [])
    if not entries:
        return None
    cve = entries[0].get("cve", {})
    metrics = cve.get("metrics", {})
    scores = []
    for key, values in metrics.items():
        for metric in values:
            cvss = metric.get("cvssData", {})
            if cvss.get("baseScore") is not None:
                scores.append({"version": cvss.get("version", key), "score": cvss["baseScore"],
                               "severity": cvss.get("baseSeverity") or metric.get("baseSeverity"),
                               "vector": cvss.get("vectorString")})
    cpes = []
    def visit(nodes):
        for node in nodes or []:
            cpes.extend(x.get("criteria") for x in node.get("cpeMatch", []) if x.get("criteria"))
            visit(node.get("children"))
            visit(node.get("nodes"))
    visit(cve.get("configurations"))
    return {"description": next((x.get("value", "") for x in cve.get("descriptions", []) if x.get("lang") == "en"), ""),
            "published": cve.get("published"), "updated": cve.get("lastModified"), "scores": scores,
            "cpes": cpes, "references": [x.get("url") for x in cve.get("references", []) if x.get("url")], "raw": cve}


def _mitre(cve_id):
    # The CVE Program publishes CVE JSON 5 records in its canonical public repository.
    year, sequence = cve_id[4:].split("-")
    block = f"{(int(sequence) // 1000)}xxx"
    path = f"https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/{year}/{block}/{cve_id}.json"
    data = _json(path)
    container = data.get("containers", {}).get("cna", {})
    desc = next((x.get("value", "") for x in container.get("descriptions", []) if x.get("lang", "en").startswith("en")), "")
    return {"description": desc, "published": data.get("cveMetadata", {}).get("datePublished"),
            "updated": data.get("cveMetadata", {}).get("dateUpdated"),
            "references": [x.get("url") for x in container.get("references", []) if x.get("url")],
            "affected": container.get("affected", []), "raw": data}


def _github(cve_id):
    url = "https://api.github.com/advisories?" + urllib.parse.urlencode({"cve_id": cve_id, "per_page": 100})
    rows = _json(url, headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    return [{"id": x.get("ghsa_id"), "summary": x.get("summary"), "description": x.get("description"),
             "url": x.get("html_url"), "severity": x.get("severity"), "published": x.get("published_at"),
             "updated": x.get("updated_at"), "cvss": x.get("cvss_severities", {}).get("cvss_v3") or x.get("cvss"),
             "packages": x.get("vulnerabilities", []), "references": x.get("references", [])} for x in rows]


def _osv(cve_id):
    return _json("https://api.osv.dev/v1/vulns/" + urllib.parse.quote(cve_id, safe="-"))


def _kev(cve_id):
    data = _json("https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json")
    hit = next((x for x in data.get("vulnerabilities", []) if x.get("cveID", "").upper() == cve_id), None)
    return {"in_catalog": bool(hit), "entry": hit, "catalog_version": data.get("catalogVersion"),
            "date_released": data.get("dateReleased")}


def _redhat(cve_id):
    return _json("https://access.redhat.com/hydra/rest/securitydata/cve/" + urllib.parse.quote(cve_id, safe="-") + ".json")


def _msrc(cve_id):
    # The MSRC Updates endpoint accepts a CVE identifier as its `key` query parameter.
    return _json("https://api.msrc.microsoft.com/cvrf/v3.0/updates?" + urllib.parse.urlencode({"key": cve_id}))


def _cisco(cve_id):
    url = "https://sec.cloudapps.cisco.com/security/center/psirtrss20/CiscoSecurityAdvisory.xml"
    root = ET.fromstring(_request(url, timeout=22))
    rows = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "item":
            continue
        fields = {x.tag.rsplit("}", 1)[-1]: (x.text or "") for x in item}
        if cve_id in " ".join(fields.values()).upper():
            rows.append({"title": fields.get("title"), "url": fields.get("link"), "published": fields.get("pubDate"),
                         "description": fields.get("description")})
    return rows


SOURCES = {
    "nvd": _nvd, "cve_org_mitre": _mitre, "github_advisories": _github,
    "osv": _osv, "cisa_kev": _kev, "redhat": _redhat, "msrc": _msrc, "cisco": _cisco,
}


def enrich(cve_id):
    cve_id = cve_id.strip().upper()
    if not CVE_RE.fullmatch(cve_id):
        raise ValueError("Enter a CVE identifier such as CVE-2024-12345.")
    results, errors = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fn, cve_id): name for name, fn in SOURCES.items()}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except urllib.error.HTTPError as exc:
                errors[name] = "Not found" if exc.code == 404 else f"HTTP {exc.code}"
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"[:240]

    nvd, mitre = results.get("nvd") or {}, results.get("cve_org_mitre") or {}
    description = nvd.get("description") or mitre.get("description") or ""
    scores = nvd.get("scores") or []
    if not scores:
        for advisory in results.get("github_advisories") or []:
            candidates = [advisory.get("cvss"), *(advisory.get("cvss_severities") or {}).values()]
            for cvss in candidates:
                if cvss and cvss.get("score") is not None:
                    vector = cvss.get("vector_string") or cvss.get("vectorString")
                    scores.append({"version": vector.split("/", 1)[0].replace("CVSS:", "") if vector else "unknown",
                        "score": cvss["score"], "severity": advisory.get("severity"), "vector": vector,
                        "source": "GitHub Advisory Database"})
    severity = next((x.get("severity") for x in scores if x.get("severity")), "")
    refs = list(dict.fromkeys((nvd.get("references") or []) + (mitre.get("references") or [])))
    all_refs = list(refs)
    for source in (results.get("github_advisories") or []):
        all_refs.extend(source.get("references", []))
        if source.get("url"):
            all_refs.append(source["url"])
    for source_name in ("osv", "redhat"):
        source = results.get(source_name) or {}
        all_refs.extend(x.get("url") for x in source.get("references", []) if x.get("url"))
        for advisory in source.get("advisories", []) or []:
            if advisory.get("url"):
                all_refs.append(advisory["url"])
    msrc_rows = results.get("msrc") or []
    if isinstance(msrc_rows, dict):
        msrc_rows = msrc_rows.get("value", [])
    for update in msrc_rows:
        for key in ("CvrfUrl", "CvrfDocumentUrl", "Url", "url"):
            if update.get(key):
                all_refs.append(update[key])
    for source in results.get("cisco") or []:
        if source.get("url"):
            all_refs.append(source["url"])
    refs = list(dict.fromkeys(u for u in all_refs if isinstance(u, str) and u.startswith(("http://", "https://"))))
    vendor_refs = [u for u in refs if any(v in u.lower() for v in ("microsoft.com", "msrc.microsoft.com", "cisco.com", "redhat.com"))]
    package_advisories = []
    for advisory in results.get("github_advisories") or []:
        for item in advisory.get("packages", []):
            package = item.get("package") or {}
            package_advisories.append({"source": "GitHub Advisory Database", "ecosystem": package.get("ecosystem"),
                "name": package.get("name"), "vulnerable": item.get("vulnerable_version_range"),
                "fixed": item.get("first_patched_version") or item.get("patched_versions"),
                "url": advisory.get("url")})
    osv = results.get("osv") or {}
    for item in osv.get("affected", []):
        package = item.get("package") or {}
        ranges = []
        for group in item.get("ranges", []):
            events = group.get("events", [])
            introduced = next((x.get("introduced") for x in events if x.get("introduced") is not None), None)
            fixed = next((x.get("fixed") for x in events if x.get("fixed") is not None), None)
            last = next((x.get("last_affected") for x in events if x.get("last_affected") is not None), None)
            if introduced is not None or fixed is not None or last is not None:
                ranges.append(f"introduced {introduced or '0'}" + (f" · fixed {fixed}" if fixed else f" · last affected {last}" if last else ""))
        package_advisories.append({"source": "OSV", "ecosystem": package.get("ecosystem"), "name": package.get("name"),
            "purl": package.get("purl"), "vulnerable": "; ".join(ranges) or ", ".join(item.get("versions", [])),
            "fixed": next((x.get("fixed") for group in item.get("ranges", []) for x in group.get("events", []) if x.get("fixed")), None)})
    record = {"id": cve_id, "title": cve_id, "description": description, "published": nvd.get("published") or mitre.get("published"),
              "updated": nvd.get("updated") or mitre.get("updated"), "severity": severity,
              "scores": scores, "cpes": nvd.get("cpes", []), "references": refs,
              "package_advisories": package_advisories,
              "affected": mitre.get("affected", []), "vendor_references": vendor_refs,
              "kev": results.get("cisa_kev", {"in_catalog": False}),
              "sources": results, "source_errors": errors}
    if not description and not any(results.get(k) for k in ("github_advisories", "osv", "redhat", "msrc", "cisco")):
        raise LookupError("No source returned this CVE. Check the identifier and try again.")
    return record
