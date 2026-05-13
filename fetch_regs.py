#!/usr/bin/env python3
"""
fetch_regs.py — Pull PFAS and TSCA documents from regulations.gov.

Two-phase architecture:

  Phase 1  Keyword search across regulations.gov for Rules, Proposed Rules,
           and substantive Notices mentioning PFAS compounds. Collects
           document IDs and their docket IDs. Only documents whose docket
           passes should_include_docket() are kept.

  Phase 2  Full docket fetch for ANCHOR_DOCKETS — every non-comment document
           in each anchor docket, regardless of keyword match. Documents found
           only here get isPrimary=False; Phase 1 documents get isPrimary=True.

  Phase 3  Fetch docket metadata for all discovered dockets.

Usage:
    python3 fetch_regs.py              # full run (Phase 1 + 2 + 3)
    python3 fetch_regs.py --phase1-only   # skip anchor docket fetch
    python3 fetch_regs.py --anchors-only  # skip keyword search

Requires REGS_API_KEY in .env
"""
import argparse
import json
import os
import time
from collections import Counter
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("REGS_API_KEY")
BASE_URL = "https://api.regulations.gov/v4"
START_DATE = "2017-01-20"   # Full Trump 1 → Biden → Trump 2 regulatory arc
DATA_DIR = Path("static/data")

# ---------------------------------------------------------------------------
# Search terms (Phase 1)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Anchor dockets (Phase 2) — always do a full document fetch for these
# ---------------------------------------------------------------------------

ANCHOR_DOCKETS = {
    "EPA-HQ-OW-2022-0114",    # PFAS drinking water MCLs
    "EPA-HQ-OW-2019-0583",    # Earlier PFAS regulatory determinations
    "EPA-HQ-OLEM-2019-0341",  # PFOA/PFOS CERCLA designation
    "EPA-HQ-OLEM-2020-0527",  # Additional CERCLA/PFAS
    "EPA-HQ-OPPT-2020-0549",  # TSCA Section 8(a)(7) reporting rule
    "EPA-HQ-OPPT-2013-0225",  # TSCA SNUR
    "EPA-HQ-TRI-2020-0142",   # TRI PFAS reporting
    "EPA-HQ-TRI-2019-0375",   # TRI PFAS reporting (older)
}

# Document types to include when doing a full anchor-docket fetch.
# "Public Submission" (public comments) is intentionally excluded.
FULL_DOCKET_TYPES = {"Rule", "Proposed Rule", "Notice", "Supporting & Related Material", "Other"}

# Document types considered substantive enough to surface from keyword search.
SUBSTANTIVE_TYPES = {"Rule", "Proposed Rule", "Notice"}

# Notice title keywords that mark a notice as non-primary (meetings, advisory panels, etc.)
_MEETING_KEYWORDS = (
    "meetings:",
    "meeting;",
    "advisory council",
    "community engagement",
    "national drinking water advisory",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def api_headers():
    return {"X-Api-Key": API_KEY}


def should_include_docket(docket_id):
    """Include water, TSCA, land/remediation, and OAR dockets.
    Exclude pesticides (OPP), litigation (OGC), and unrelated offices."""
    if not docket_id:
        return False
    if docket_id in ANCHOR_DOCKETS:
        return True
    if docket_id == "EPA_FRDOC_0001":
        return True
    for office in ("-OPP-", "-OGC-"):
        if office in docket_id:
            return False
    for office in ("-OW-", "-OPPT-", "-OLEM-", "-OAR-", "-TRI-"):
        if office in docket_id:
            return True
    return False


def _is_primary_doc(doc_type, title):
    """Return True if this document should be marked isPrimary.

    Rules / Proposed Rules are always primary.
    Notices are primary unless the title signals a public meeting or advisory
    council session.
    Everything else (Supporting & Related Material, Other, etc.) is not primary.
    """
    if doc_type in ("Rule", "Proposed Rule"):
        return True
    if doc_type == "Notice":
        title_lower = (title or "").lower()
        return not any(kw in title_lower for kw in _MEETING_KEYWORDS)
    return False


def _make_doc_record(doc_id, attrs, search_terms, is_primary):
    """Build a normalised document dict from a regulations.gov attributes block."""
    return {
        "documentId": doc_id,
        "docketId": attrs.get("docketId"),
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
        "searchTerms": list(search_terms),
        "isPrimary": is_primary,
    }


def _api_get(url, params, label="request"):
    """GET with retry on 429 and up to 3 attempts."""
    for attempt in range(3):
        resp = requests.get(url, headers=api_headers(), params=params, timeout=30)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 120))
            wait = max(retry_after, 60)
            print(f"  rate limited — waiting {wait // 60}m {wait % 60}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Failed after 3 retries for {label}")


# ---------------------------------------------------------------------------
# Phase 1 — keyword search
# ---------------------------------------------------------------------------

def phase1_keyword_search():
    """Search regulations.gov by keyword; return dict keyed by documentId."""
    seen = {}  # documentId -> doc record

    for term in SEARCH_TERMS:
        print(f"\n  Keyword: '{term}'")
        page = 1
        while True:
            data = _api_get(
                f"{BASE_URL}/documents",
                params={
                    "filter[agencyId]": "EPA",
                    "filter[postedDate][ge]": START_DATE,
                    "filter[searchTerm]": term,
                    "filter[documentType]": "Rule,Proposed Rule,Notice",
                    "page[size]": 250,
                    "page[number]": page,
                    "sort": "postedDate",
                },
                label=f"keyword '{term}' page {page}",
            )
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
                    is_primary = _is_primary_doc(doc_type, attrs.get("title"))
                    seen[doc_id] = _make_doc_record(doc_id, attrs, [term], is_primary)

            print(f"    page {page}/{total_pages} — {len(items)} docs ({len(seen)} unique so far)")

            if not meta.get("hasNextPage"):
                break
            page += 1
            time.sleep(0.5)

        time.sleep(1)  # brief pause between search terms

    return seen


# ---------------------------------------------------------------------------
# Phase 2 — full anchor-docket fetch
# ---------------------------------------------------------------------------

def _fetch_docket_documents(docket_id):
    """Return all non-comment documents in a single docket as a list of (id, attrs)."""
    results = []
    page = 1
    while True:
        data = _api_get(
            f"{BASE_URL}/documents",
            params={
                "filter[docketId]": docket_id,
                "page[size]": 250,
                "page[number]": page,
                "sort": "postedDate",
            },
            label=f"docket {docket_id} page {page}",
        )
        items = data.get("data", [])
        meta = data.get("meta", {})
        total_pages = meta.get("totalPages", "?")

        for item in items:
            attrs = item.get("attributes", {})
            doc_type = attrs.get("documentType", "")
            if doc_type == "Public Submission":
                continue  # skip public comments — too voluminous
            if doc_type not in FULL_DOCKET_TYPES:
                continue
            results.append((item["id"], attrs))

        print(f"    page {page}/{total_pages} — {len(items)} raw docs")

        if not meta.get("hasNextPage"):
            break
        page += 1
        time.sleep(0.5)

    return results


def phase2_anchor_dockets(phase1_docs):
    """Full fetch of each anchor docket; merge into phase1_docs dict.

    Documents already in phase1_docs keep their isPrimary value and gain no
    extra searchTerms. New documents are added with isPrimary=False and
    searchTerms=[].

    Returns the updated dict (same object, modified in place).
    """
    new_count = 0
    for docket_id in sorted(ANCHOR_DOCKETS):
        print(f"\n  Anchor docket: {docket_id}")
        doc_pairs = _fetch_docket_documents(docket_id)
        print(f"    -> {len(doc_pairs)} non-comment docs")

        for doc_id, attrs in doc_pairs:
            if doc_id in phase1_docs:
                # Already known from keyword search — leave as-is
                continue
            # New doc found only via anchor fetch
            phase1_docs[doc_id] = _make_doc_record(doc_id, attrs, [], False)
            new_count += 1

        time.sleep(1)

    print(f"\n  Phase 2 added {new_count} supplementary documents")
    return phase1_docs


# ---------------------------------------------------------------------------
# Phase 3 — docket metadata
# ---------------------------------------------------------------------------

def phase3_fetch_dockets(docket_ids):
    """Fetch metadata for all discovered dockets plus all anchor dockets."""
    all_ids = sorted(docket_ids | ANCHOR_DOCKETS)
    dockets = {}
    for i, docket_id in enumerate(all_ids, 1):
        print(f"  docket {i}/{len(all_ids)}: {docket_id}")
        try:
            data = _api_get(
                f"{BASE_URL}/dockets/{docket_id}",
                params={},
                label=f"docket metadata {docket_id}",
            )
            attrs = data.get("data", {}).get("attributes", {})
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch PFAS documents from regulations.gov")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--phase1-only",
        action="store_true",
        help="Run keyword search only; skip full anchor-docket fetch (Phase 2)",
    )
    mode.add_argument(
        "--anchors-only",
        action="store_true",
        help="Run anchor docket fetch only; skip keyword search (Phase 1)",
    )
    args = parser.parse_args()

    if not API_KEY:
        raise SystemExit("ERROR: REGS_API_KEY not set. Add it to .env")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Phase 1
    # ------------------------------------------------------------------
    if args.anchors_only:
        print("=== Skipping Phase 1 (--anchors-only) ===")
        docs = {}
    else:
        print("=== Phase 1: Keyword search ===")
        docs = phase1_keyword_search()
        print(f"\nPhase 1 complete: {len(docs)} unique substantive documents")

    # ------------------------------------------------------------------
    # Phase 2
    # ------------------------------------------------------------------
    if args.phase1_only:
        print("\n=== Skipping Phase 2 (--phase1-only) ===")
    else:
        print("\n=== Phase 2: Full anchor-docket fetch ===")
        docs = phase2_anchor_dockets(docs)

    # ------------------------------------------------------------------
    # Save documents
    # ------------------------------------------------------------------
    documents = list(docs.values())
    with open(DATA_DIR / "documents.json", "w") as f:
        json.dump(documents, f, indent=2)
    print(f"\nSaved {DATA_DIR}/documents.json ({len(documents)} documents)")

    # ------------------------------------------------------------------
    # Phase 3 — docket metadata
    # ------------------------------------------------------------------
    docket_ids = {d["docketId"] for d in documents if d.get("docketId")}
    print(f"\n=== Phase 3: Docket metadata ({len(docket_ids | ANCHOR_DOCKETS)} dockets) ===")
    dockets = phase3_fetch_dockets(docket_ids)

    with open(DATA_DIR / "dockets.json", "w") as f:
        json.dump(dockets, f, indent=2)
    print(f"Saved {DATA_DIR}/dockets.json ({len(dockets)} dockets)")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    primary_docs = [d for d in documents if d.get("isPrimary")]
    supplementary_docs = [d for d in documents if not d.get("isPrimary")]
    types = Counter(d.get("documentType") for d in documents)
    terms = Counter(t for d in documents for t in d.get("searchTerms", []))
    open_comment = sum(1 for d in documents if d.get("openForComment"))

    print("\n=== Summary ===")
    print(f"Total documents:      {len(documents)}")
    print(f"  Primary:            {len(primary_docs)}")
    print(f"  Supplementary:      {len(supplementary_docs)}")
    print(f"Total dockets:        {len(dockets)}")
    print(f"Open for comment:     {open_comment}")
    print("\nDocument types:")
    for t, n in types.most_common():
        print(f"  {n:4d}  {t}")
    if terms:
        print("\nSearch term hits (Phase 1):")
        for t, n in terms.most_common():
            print(f"  {n:4d}  {t}")


if __name__ == "__main__":
    main()
