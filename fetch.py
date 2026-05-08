#!/usr/bin/env python3
"""
fetch.py — Pull Clean Air Act documents from regulations.gov and save as JSON.

Covers SIPs/TIPs, NAAQS, MATS, NESHAPs, NSPS, vehicle emissions, and other
CAA regulatory actions since January 20, 2025.

Usage:
    python3 fetch.py

Requires REGS_API_KEY in .env
"""
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("REGS_API_KEY")
BASE_URL = "https://api.regulations.gov/v4"
START_DATE = "2025-01-20"
DATA_DIR = Path("static/data")

STATES = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming", "District of Columbia",
}

# Each term is one API pass. Results are deduplicated by documentId; each
# document is tagged with every term that returned it.
SEARCH_TERMS = [
    # ── SIP / TIP ────────────────────────────────────────────────────────────
    "state implementation plan",        # core SIP documents
    "tribal implementation plan",       # TIPs
    "approvals and promulgations",      # older-format SIP titles missed by above

    # ── Named deregulatory actions (EPA March 2025 press release) ────────────
    "good neighbor",                    # Good Neighbor Plan / cross-state ozone
    "regional haze",                    # Regional Haze Program restructuring
    "exceptional events",               # prescribed fire exemptions in SIPs/TIPs

    # ── NAAQS (air quality standards) ────────────────────────────────────────
    "national ambient air quality",     # all NAAQS actions
    "particulate matter",               # PM2.5 standard reconsideration
    "ozone",                            # ozone NAAQS and nonattainment

    # ── Major emission standards ──────────────────────────────────────────────
    "mercury air toxics",               # MATS (coal plant mercury rules)
    "hazardous air pollutants",         # NESHAPs (broad industrial standards)
    "new source performance",           # NSPS (power plants, oil & gas)

    # ── Vehicle and transportation ────────────────────────────────────────────
    "greenhouse gas",                   # vehicle GHG standards

    # ── PM2.5 (abbreviation form misses "particulate matter" searches) ────────
    "PM2.5",                            # fine particulate matter NAAQS

    # ── Legal / enforcement ───────────────────────────────────────────────────
    "consent decree",                   # CAA citizen suit settlements (OGC dockets)
]


def api_headers():
    return {"X-Api-Key": API_KEY}


def should_include_docket(docket_id):
    """Include OAR (air office) and OGC (legal/consent decrees) dockets.
    Exclude water, pesticides, land management, and other offices.
    All included dockets pass the activity filter — documents were posted after Jan 20 2025."""
    if docket_id == "EPA_FRDOC_0001":
        return True
    if "-OAR-" in docket_id:
        return True
    if "-OGC-" in docket_id:
        return True
    return False


_TRIBAL_RE = re.compile(r'\b(tribal|tribe|nation|nations|rancheria|indian\s+band)\b', re.IGNORECASE)


def parse_state(title):
    """Return state name from document title, 'Tribal' for tribal docs, or None."""
    if not title:
        return None
    if _TRIBAL_RE.search(title):
        return "Tribal"
    title_lower = title.lower()
    for state in STATES:
        if state.lower() in title_lower:
            return state
    return None


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
            mins = wait // 60
            print(f"  rate limited — waiting {mins}m {wait % 60}s (Retry-After: {retry_after}s)")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Failed after 3 retries for term '{search_term}' page {page_num}")


def fetch_all_documents():
    """Fetch all documents across all search terms, deduped by documentId."""
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
                        "state": parse_state(attrs.get("title", "")),
                        "searchTerms": [term],
                    }

            print(f"  page {page}/{total_pages} — {len(items)} docs ({len(seen)} total unique)")

            if not meta.get("hasNextPage"):
                break
            page += 1
            time.sleep(0.5)

        time.sleep(1)

    return list(seen.values())


def fetch_dockets(docket_ids):
    """Fetch docket metadata for a set of docket IDs."""
    dockets = {}
    ids = sorted(docket_ids)
    for i, docket_id in enumerate(ids, 1):
        print(f"  docket {i}/{len(ids)}: {docket_id}")
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


def print_summary(documents, dockets):
    types = Counter(d.get("documentType") for d in documents if d.get("documentType"))
    states = Counter(d.get("state") for d in documents if d.get("state"))
    terms = Counter(t for d in documents for t in d.get("searchTerms", []))
    open_comment = sum(1 for d in documents if d.get("openForComment"))

    print("\n=== Summary ===")
    print(f"Total documents: {len(documents)}")
    print(f"Total dockets:   {len(dockets)}")
    print(f"Open for comment: {open_comment}")

    print("\nDocument types:")
    for t, n in types.most_common():
        print(f"  {n:4d}  {t}")

    print("\nTop states/tribal:")
    for s, n in states.most_common(15):
        print(f"  {n:4d}  {s}")

    print("\nSearch term hits:")
    for t, n in terms.most_common():
        print(f"  {n:4d}  {t}")


def main():
    if not API_KEY:
        raise SystemExit("ERROR: REGS_API_KEY not set. Add it to .env")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("=== Fetching documents ===")
    documents = fetch_all_documents()
    print(f"\n{len(documents)} unique documents found")

    with open(DATA_DIR / "documents.json", "w") as f:
        json.dump(documents, f, indent=2)
    print(f"Saved static/data/documents.json")

    docket_ids = {d["docketId"] for d in documents if d.get("docketId")}
    print(f"\n=== Fetching {len(docket_ids)} dockets ===")
    dockets = fetch_dockets(docket_ids)

    with open(DATA_DIR / "dockets.json", "w") as f:
        json.dump(dockets, f, indent=2)
    print(f"Saved static/data/dockets.json")

    by_state = defaultdict(list)
    for doc in documents:
        if doc.get("state"):
            by_state[doc["state"]].append(doc["documentId"])
    with open(DATA_DIR / "by_state.json", "w") as f:
        json.dump(dict(by_state), f, indent=2)
    print(f"Saved static/data/by_state.json")

    print_summary(documents, dockets)


if __name__ == "__main__":
    main()
