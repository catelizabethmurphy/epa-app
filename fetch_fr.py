#!/usr/bin/env python3
"""
fetch_fr.py — Fetch PFAS/TSCA rules directly from the Federal Register API.

The Federal Register API is free (no key required) and provides
richer metadata than regulations.gov: abstracts, action field,
effective dates, and full-text XML.

Fetches rules, proposed rules, and notices from EPA mentioning
PFAS or TSCA since January 20, 2025.

Usage:
    python3 fetch_fr.py
    python3 fetch_fr.py --skip-xml   # metadata only

Writes:
    static/data/fr_documents.json   — metadata keyed by document_number
    static/data/text/FR-*.txt       — plain-text files (incremental)
"""
import argparse
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

FR_API = "https://www.federalregister.gov/api/v1"
START_DATE = "2017-01-20"   # Full Trump 1 → Biden → Trump 2 regulatory arc
DATA_DIR = Path("static/data")
TEXT_DIR = DATA_DIR / "text"

SEARCH_TERMS = [
    # Current shorthand — standard from ~2018 onward
    "PFAS",
    "PFOA",
    "PFOS",
    "PFNA",
    "PFHxS",
    "PFBS",
    "perfluoroalkyl",
    "polyfluoroalkyl",
    "GenX",
    "HFPO-DA",
    "forever chemicals",
    # Full chemical names — used in older Trump 1 / Obama-era documents
    "perfluorooctanoic acid",
    "perfluorooctane sulfonate",
    "perfluorooctane sulfonic acid",
    # Program / source terms — still PFAS-specific in context
    "PFAS drinking water",
    "PFAS TSCA",
    "PFAS cleanup",
    "PFAS reporting",
    "PFAS strategic roadmap",
    # Firefighting foam — primary legacy PFAS source
    "aqueous film-forming foam",
    "AFFF",
    # Monitoring
    "UCMR PFAS",
    "unregulated contaminant monitoring PFAS",
]

FR_FIELDS = [
    "document_number",
    "title",
    "abstract",
    "type",
    "action",
    "publication_date",
    "effective_on",
    "comments_close_on",
    "html_url",
    "pdf_url",
    "full_text_xml_url",
    "docket_ids",
    "regulation_id_number_info",
]

PER_PAGE = 100


def fetch_fr_page(term, page):
    param_list = [
        ("conditions[term]", term),
        ("conditions[agencies][]", "environmental-protection-agency"),
        ("conditions[publication_date][gte]", START_DATE),
        # Only Rules, Proposed Rules, Notices — no litigation/consent decrees
        ("conditions[type][]", "RULE"),
        ("conditions[type][]", "PRORULE"),
        ("conditions[type][]", "NOTICE"),
        ("per_page", PER_PAGE),
        ("page", page),
        ("order", "newest"),
    ]
    param_list += [("fields[]", f) for f in FR_FIELDS]

    for attempt in range(3):
        resp = requests.get(f"{FR_API}/documents.json", params=param_list, timeout=30)
        if resp.status_code == 429:
            print(f"  FR API rate limited — waiting 60s")
            time.sleep(60)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"FR API failed for '{term}' page {page} after 3 retries")


def fetch_all_fr_documents():
    seen = {}

    for term in SEARCH_TERMS:
        print(f"\nFetching FR: '{term}'")
        page = 1
        while True:
            data = fetch_fr_page(term, page)
            results = data.get("results", [])
            total_pages = data.get("total_pages", 1)

            for r in results:
                num = r.get("document_number")
                if not num:
                    continue
                if num not in seen:
                    rin_info = r.get("regulation_id_number_info") or {}
                    rin = list(rin_info.keys())[0] if rin_info else None
                    seen[num] = {
                        "documentNumber": num,
                        "title": r.get("title"),
                        "abstract": r.get("abstract"),
                        "type": r.get("type"),          # RULE, PRORULE, NOTICE, etc.
                        "action": r.get("action"),
                        "publicationDate": r.get("publication_date"),
                        "effectiveDate": r.get("effective_on"),
                        "commentsCloseDate": r.get("comments_close_on"),
                        "htmlUrl": r.get("html_url"),
                        "pdfUrl": r.get("pdf_url"),
                        "fullTextXmlUrl": r.get("full_text_xml_url"),
                        "docketIds": r.get("docket_ids") or [],
                        "rin": rin,
                        "searchTerms": [term],
                    }
                elif term not in seen[num]["searchTerms"]:
                    seen[num]["searchTerms"].append(term)

            print(f"  page {page}/{total_pages} — {len(results)} results ({len(seen)} unique)")

            if page >= total_pages:
                break
            page += 1
            time.sleep(0.4)

        time.sleep(0.5)

    return seen


def xml_to_text(xml_content):
    try:
        root = ET.fromstring(xml_content)
        parts = []
        for elem in root.iter():
            if elem.text and elem.text.strip():
                parts.append(elem.text.strip())
            if elem.tail and elem.tail.strip():
                parts.append(elem.tail.strip())
        return "\n\n".join(parts)
    except ET.ParseError:
        text = re.sub(r"<[^>]+>", " ", xml_content.decode("utf-8", errors="replace"))
        return re.sub(r"\s+", " ", text).strip()


def download_xml(doc_num, xml_url):
    safe_name = doc_num.replace("/", "-")
    txt_path = TEXT_DIR / f"FR-{safe_name}.txt"
    if txt_path.exists():
        return True
    try:
        resp = requests.get(xml_url, timeout=60)
        resp.raise_for_status()
        text = xml_to_text(resp.content)
        txt_path.write_text(text, encoding="utf-8")
        return True
    except Exception as e:
        print(f"    XML failed for {doc_num}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-xml", action="store_true", help="Skip full-text XML download")
    args = parser.parse_args()

    TEXT_DIR.mkdir(parents=True, exist_ok=True)

    print("=== Fetching PFAS/TSCA documents from Federal Register API ===")
    docs = fetch_all_fr_documents()
    print(f"\n{len(docs)} unique FR documents found")

    out_path = DATA_DIR / "fr_documents.json"
    with open(out_path, "w") as f:
        json.dump(docs, f, indent=2)
    print(f"Saved {out_path}")

    if not args.skip_xml:
        xml_docs = [(num, meta["fullTextXmlUrl"])
                    for num, meta in docs.items()
                    if meta.get("fullTextXmlUrl")]
        print(f"\n=== Downloading {len(xml_docs)} full-text XML files ===")
        ok = skipped = failed = 0
        for i, (num, xml_url) in enumerate(xml_docs, 1):
            safe = num.replace("/", "-")
            if (TEXT_DIR / f"FR-{safe}.txt").exists():
                skipped += 1
                continue
            print(f"  [{i}/{len(xml_docs)}] {num}")
            if download_xml(num, xml_url):
                ok += 1
            else:
                failed += 1
            time.sleep(0.3)
        print(f"\nXML: {ok} saved, {skipped} skipped, {failed} failed")

    has_abstract = sum(1 for m in docs.values() if m.get("abstract"))
    has_xml = sum(1 for m in docs.values() if m.get("fullTextXmlUrl"))
    txt_files = len(list(TEXT_DIR.glob("FR-*.txt")))
    print(f"\nWith abstract:      {has_abstract}")
    print(f"With full-text XML: {has_xml}")
    print(f"Text files on disk: {txt_files}")


if __name__ == "__main__":
    main()
