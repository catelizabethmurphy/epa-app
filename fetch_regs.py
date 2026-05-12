#!/usr/bin/env python3
"""
fetch_regs.py — Pull PFAS and TSCA documents from regulations.gov.

Covers PFOA/PFOS, GenX, PFAS broadly, TSCA reporting rules,
CERCLA designations, drinking water MCLs, and related actions
since January 20, 2025.

Usage:
    python3 fetch_regs.py

Requires REGS_API_KEY in .env
"""
import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("REGS_API_KEY")
BASE_URL = "https://api.regulations.gov/v4"
START_DATE = "2017-01-20"   # Full Trump 1 → Biden → Trump 2 regulatory arc
DATA_DIR = Path("static/data")

SEARCH_TERMS = [
    # Compound names — PFAS/PFOS/forever chemicals
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
    # Full chemical names — common in Trump 1 / pre-2018 filings
    "perfluorooctanoic acid",
    "perfluorooctane sulfonate",
    "perfluorooctane sulfonic acid",
    # Qualified program terms
    "PFAS drinking water",
    "PFAS reporting",
    "PFAS cleanup",
    "PFAS hazardous",
    "PFAS TSCA",
    # Firefighting foam
    "aqueous film-forming foam",
    "AFFF PFAS",
]

# Dockets always fetched regardless of keyword match — the three anchor rules.
SEED_DOCKETS = {
    "EPA-HQ-OW-2022-0114",    # PFAS drinking water MCLs (Biden rule, Trump reconsideration)
    "EPA-HQ-OPPT-2020-0549",  # TSCA Section 8(a)(7) PFAS reporting rule
    "EPA-HQ-OLEM-2019-0341",  # PFOA/PFOS CERCLA hazardous substance designation
}


def api_headers():
    return {"X-Api-Key": API_KEY}


SUBSTANTIVE_TYPES = {"Rule", "Proposed Rule", "Notice"}


def should_include_docket(docket_id):
    """Include water, TSCA, land/remediation, and OAR dockets.
    Exclude pesticides (OPP), litigation (OGC), and unrelated offices."""
    if not docket_id:
        return False
    if docket_id in SEED_DOCKETS:
        return True
    if docket_id == "EPA_FRDOC_0001":
        return True
    for office in ("-OPP-", "-OGC-"):
        if office in docket_id:
            return False
    for office in ("-OW-", "-OPPT-", "-OLEM-", "-OAR-"):
        if office in docket_id:
            return True
    return False


def fetch_page(search_term, page_num):
    params = {
        "filter[agencyId]": "EPA",
        "filter[postedDate][ge]": START_DATE,
        "filter[searchTerm]": search_term,
        "page[size]": 250,
        "page[number]": page_num,
        "sort": "postedDate",
    }
    for attempt in range(3):
        resp = requests.get(
            f"{BASE_URL}/documents", headers=api_headers(), params=params, timeout=30
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 120))
            wait = max(retry_after, 60)
            print(f"  rate limited — waiting {wait // 60}m {wait % 60}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Failed after 3 retries for '{search_term}' page {page_num}")


def fetch_all_documents():
    seen = {}

    for term in SEARCH_TERMS:
        print(f"\nFetching: '{term}'")
        page = 1
        while True:
            data = fetch_page(term, page)
            items = data.get("data", [])
            meta = data.get("meta", {})
            total_pages = meta.get("totalPages", "?")

            for item in items:
                doc_id = item["id"]
                attrs = item.get("attributes", {})
                docket_id = attrs.get("docketId", "")

                if not should_include_docket(docket_id):
                    continue

                doc_type = attrs.get("documentType", "")
                if doc_type not in SUBSTANTIVE_TYPES:
                    continue

                if doc_id in seen:
                    if term not in seen[doc_id]["searchTerms"]:
                        seen[doc_id]["searchTerms"].append(term)
                else:
                    seen[doc_id] = {
                        "documentId": doc_id,
                        "docketId": docket_id,
                        "title": attrs.get("title"),
                        "documentType": attrs.get("documentType"),
                        "postedDate": attrs.get("postedDate"),
                        "lastModifiedDate": attrs.get("lastModifiedDate"),
                        "commentStartDate": attrs.get("commentStartDate"),
                        "commentEndDate": attrs.get("commentEndDate"),
                        "commentCount": attrs.get("commentCount", 0),
                        "openForComment": attrs.get("openForComment", False),
                        "withdrawn": attrs.get("withdrawn", False),
                        "frDocNum": attrs.get("frDocNum"),
                        "searchTerms": [term],
                    }

            print(f"  page {page}/{total_pages} — {len(items)} docs ({len(seen)} unique)")

            if not meta.get("hasNextPage"):
                break
            page += 1
            time.sleep(0.5)

        time.sleep(1)

    return list(seen.values())


def fetch_dockets(docket_ids):
    dockets = {}
    all_ids = sorted(docket_ids | SEED_DOCKETS)
    for i, docket_id in enumerate(all_ids, 1):
        print(f"  docket {i}/{len(all_ids)}: {docket_id}")
        try:
            resp = requests.get(
                f"{BASE_URL}/dockets/{docket_id}",
                headers=api_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            attrs = data.get("attributes", {})
            dockets[docket_id] = {
                "docketId": docket_id,
                "title": attrs.get("title"),
                "abstract": attrs.get("docketAbstract"),
                "agencyId": attrs.get("agencyId"),
                "rin": attrs.get("rin"),
                "lastModifiedDate": attrs.get("lastModifiedDate"),
            }
        except requests.HTTPError as e:
            print(f"    ERROR: {e}")
            dockets[docket_id] = {"docketId": docket_id, "title": None, "error": str(e)}
        time.sleep(0.5)
    return dockets


def main():
    if not API_KEY:
        raise SystemExit("ERROR: REGS_API_KEY not set. Add it to .env")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("=== Fetching PFAS/TSCA documents from regulations.gov ===")
    documents = fetch_all_documents()
    print(f"\n{len(documents)} unique documents found")

    with open(DATA_DIR / "documents.json", "w") as f:
        json.dump(documents, f, indent=2)
    print("Saved static/data/documents.json")

    docket_ids = {d["docketId"] for d in documents if d.get("docketId")}
    print(f"\n=== Fetching {len(docket_ids | SEED_DOCKETS)} dockets ===")
    dockets = fetch_dockets(docket_ids)

    with open(DATA_DIR / "dockets.json", "w") as f:
        json.dump(dockets, f, indent=2)
    print("Saved static/data/dockets.json")

    types = Counter(d.get("documentType") for d in documents)
    terms = Counter(t for d in documents for t in d.get("searchTerms", []))
    open_comment = sum(1 for d in documents if d.get("openForComment"))

    print("\n=== Summary ===")
    print(f"Total:            {len(documents)}")
    print(f"Total dockets:    {len(dockets)}")
    print(f"Open for comment: {open_comment}")
    print("\nDocument types:")
    for t, n in types.most_common():
        print(f"  {n:4d}  {t}")
    print("\nSearch term hits:")
    for t, n in terms.most_common():
        print(f"  {n:4d}  {t}")


if __name__ == "__main__":
    main()
