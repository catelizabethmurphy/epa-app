#!/usr/bin/env python3
"""
fetch_text.py — Enrich rules/proposed rules with Federal Register text.

For documents with frDocNum (312 of 318): batch-fetches abstract, html_url,
pdf_url, full_text_xml_url, and action from the Federal Register API.
Downloads and strips the XML to plain text in static/data/text/.

For the 6 documents without frDocNum: fetches the PDF URL from the
regulations.gov document detail endpoint.

Saves:
  static/data/fr_text.json  — metadata keyed by documentId
  static/data/text/*.txt    — plain text of each document (incremental)

Usage:
    python fetch_text.py
    python fetch_text.py --skip-xml   # metadata only, no XML downloads
"""
import argparse
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("REGS_API_KEY")
DATA_DIR = Path("static/data")
TEXT_DIR = DATA_DIR / "text"

FR_FIELDS = [
    "document_number",
    "abstract",
    "html_url",
    "pdf_url",
    "full_text_xml_url",
    "action",
    "publication_date",
]
FR_BATCH_SIZE = 50


def regs_headers():
    return {"X-Api-Key": API_KEY}


def fetch_fr_batch(doc_numbers):
    """Fetch Federal Register metadata for up to FR_BATCH_SIZE document numbers."""
    url = f"https://www.federalregister.gov/api/v1/documents/{','.join(doc_numbers)}.json"
    params = [("fields[]", f) for f in FR_FIELDS]
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("results", [])


def fetch_regs_detail(doc_id):
    """Fetch regulations.gov document detail to get fileFormats for non-FR docs."""
    url = f"https://api.regulations.gov/v4/documents/{doc_id}"
    resp = requests.get(
        url,
        headers=regs_headers(),
        params={"include": "attachments"},
        timeout=30,
    )
    resp.raise_for_status()
    attrs = resp.json().get("data", {}).get("attributes", {})
    formats = attrs.get("fileFormats") or []
    pdf = next((f["fileUrl"] for f in formats if f.get("format") == "pdf"), None)
    if not pdf and formats:
        pdf = formats[0].get("fileUrl")
    return {
        "abstract": attrs.get("docAbstract"),
        "pdf_url": pdf,
        "html_url": None,
        "full_text_xml_url": None,
        "action": None,
        "publication_date": None,
    }


def xml_to_text(xml_content):
    """Strip XML tags and collapse whitespace to get readable plain text."""
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
        # Fall back to regex strip if XML is malformed
        text = re.sub(r"<[^>]+>", " ", xml_content.decode("utf-8", errors="replace"))
        return re.sub(r"\s+", " ", text).strip()


def download_xml(doc_id, xml_url, skip_existing=True):
    """Download and convert FR XML to plain text. Returns text or None."""
    txt_path = TEXT_DIR / f"{doc_id}.txt"
    if skip_existing and txt_path.exists():
        return txt_path.read_text()

    try:
        resp = requests.get(xml_url, timeout=60)
        resp.raise_for_status()
        text = xml_to_text(resp.content)
        txt_path.write_text(text, encoding="utf-8")
        return text
    except Exception as e:
        print(f"    XML download failed for {doc_id}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-xml",
        action="store_true",
        help="Fetch metadata only; skip downloading full-text XML",
    )
    args = parser.parse_args()

    TEXT_DIR.mkdir(parents=True, exist_ok=True)

    docs = json.load(open(DATA_DIR / "documents.json"))
    rules = [d for d in docs if d["documentType"] in ("Rule", "Proposed Rule")]
    print(f"Rules + Proposed Rules: {len(rules)}")

    with_fr = [d for d in rules if d.get("frDocNum")]
    without_fr = [d for d in rules if not d.get("frDocNum")]
    print(f"  with frDocNum: {len(with_fr)}")
    print(f"  without frDocNum: {len(without_fr)}")

    fr_text = {}  # documentId -> metadata dict

    # ── Federal Register batch fetch ──────────────────────────────────────────
    print(f"\n=== Fetching Federal Register metadata (batches of {FR_BATCH_SIZE}) ===")
    fr_map = {d["frDocNum"]: d["documentId"] for d in with_fr}
    fr_numbers = list(fr_map.keys())

    for i in range(0, len(fr_numbers), FR_BATCH_SIZE):
        batch = fr_numbers[i : i + FR_BATCH_SIZE]
        print(f"  batch {i // FR_BATCH_SIZE + 1}/{-(-len(fr_numbers) // FR_BATCH_SIZE)} ({len(batch)} docs)")
        try:
            results = fetch_fr_batch(batch)
            for result in results:
                num = result.get("document_number") or result.get("frDocNum")
                doc_id = fr_map.get(num)
                if doc_id:
                    fr_text[doc_id] = {
                        "abstract": result.get("abstract"),
                        "html_url": result.get("html_url"),
                        "pdf_url": result.get("pdf_url"),
                        "full_text_xml_url": result.get("full_text_xml_url"),
                        "action": result.get("action"),
                        "publication_date": result.get("publication_date"),
                    }
        except Exception as e:
            print(f"    ERROR on batch: {e}")
        time.sleep(0.5)

    # ── regulations.gov detail for non-FR docs ────────────────────────────────
    if without_fr:
        print(f"\n=== Fetching regs.gov attachments for {len(without_fr)} non-FR docs ===")
        for doc in without_fr:
            doc_id = doc["documentId"]
            print(f"  {doc_id}")
            try:
                fr_text[doc_id] = fetch_regs_detail(doc_id)
            except Exception as e:
                print(f"    ERROR: {e}")
            time.sleep(0.5)

    # ── Save metadata ─────────────────────────────────────────────────────────
    out_path = DATA_DIR / "fr_text.json"
    with open(out_path, "w") as f:
        json.dump(fr_text, f, indent=2)
    print(f"\nSaved {out_path} ({len(fr_text)} documents)")

    # ── Download full-text XML ────────────────────────────────────────────────
    if not args.skip_xml:
        xml_docs = [
            (doc_id, meta["full_text_xml_url"])
            for doc_id, meta in fr_text.items()
            if meta.get("full_text_xml_url")
        ]
        print(f"\n=== Downloading {len(xml_docs)} full-text XML files ===")
        ok, skipped, failed = 0, 0, 0
        for i, (doc_id, xml_url) in enumerate(xml_docs, 1):
            txt_path = TEXT_DIR / f"{doc_id}.txt"
            if txt_path.exists():
                skipped += 1
                continue
            print(f"  [{i}/{len(xml_docs)}] {doc_id}")
            result = download_xml(doc_id, xml_url, skip_existing=False)
            if result:
                ok += 1
            else:
                failed += 1
            time.sleep(0.3)

        print(f"\nXML downloads: {ok} saved, {skipped} already existed, {failed} failed")

    # ── Summary ───────────────────────────────────────────────────────────────
    has_abstract = sum(1 for m in fr_text.values() if m.get("abstract"))
    has_pdf = sum(1 for m in fr_text.values() if m.get("pdf_url"))
    has_html = sum(1 for m in fr_text.values() if m.get("html_url"))
    txt_files = len(list(TEXT_DIR.glob("*.txt")))

    print("\n=== Summary ===")
    print(f"Documents with abstract:  {has_abstract}")
    print(f"Documents with PDF URL:   {has_pdf}")
    print(f"Documents with HTML URL:  {has_html}")
    print(f"Plain text files saved:   {txt_files}")


if __name__ == "__main__":
    main()
