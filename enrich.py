#!/usr/bin/env python3
"""
enrich.py — Post-process documents.json with derived fields.

Adds/updates fields on each document (in place):
  category     — primary program category (string)
  isExtension  — deadline/comment-period extension (bool)
  state        — primary state/territory, or "Tribal" (corrected word-boundary)
  states       — all states/territories found in title (list)
  epaRegion    — "Region N" or "National" (from docket ID)
  location     — most specific sub-state geographic name found in title (str|null)
  commentCount — real count from regs.gov detail endpoint (--comments flag)

Usage:
    python3 enrich.py                  # all derived fields except comment counts
    python3 enrich.py --comments       # also fetch real comment counts (~1 req/doc)

Reads and overwrites static/data/documents.json and static/data/by_state.json.
"""
import argparse
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
DATA_DIR = Path("static/data")

# ── Geography ────────────────────────────────────────────────────────────────

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

# Territories EPA regulates under the Clean Air Act
TERRITORIES = {
    "guam": "Guam",
    "puerto rico": "Puerto Rico",
    "virgin islands": "Virgin Islands",
    "usvi": "Virgin Islands",
    "american samoa": "American Samoa",
    "northern mariana": "Northern Mariana Islands",
}

_TRIBAL_RE = re.compile(r'\b(tribal|tribe|nation|nations|rancheria|indian\s+band)\b', re.IGNORECASE)

# Phrase → (resolved_state, display_location) for air districts, counties, metro
# Longer/more-specific phrases must appear before shorter ones if they overlap.
# Keyed in lowercase; matched as substrings of the lowercased title.
LOCATION_ALIASES = {
    # ── California air districts ──────────────────────────────────────────────
    "antelope valley aqmd":               ("California", "Antelope Valley AQMD"),
    "antelope valley air quality":        ("California", "Antelope Valley AQMD"),
    "bay area aqmd":                      ("California", "Bay Area AQMD"),
    "bay area air quality":               ("California", "Bay Area AQMD"),
    "coachella valley":                   ("California", "Coachella Valley"),
    "el dorado county apcd":              ("California", "El Dorado County"),
    "great basin unified":                ("California", "Great Basin Unified APCD"),
    "imperial county":                    ("California", "Imperial County"),
    "kern county apcd":                   ("California", "Kern County"),
    "kern county air":                    ("California", "Kern County"),
    "mojave desert aqmd":                 ("California", "Mojave Desert AQMD"),
    "mojave desert air quality":          ("California", "Mojave Desert AQMD"),
    "northern sierra aqmd":               ("California", "Northern Sierra AQMD"),
    "sacramento metropolitan":            ("California", "Sacramento Metro AQMD"),
    "sacramento metro":                   ("California", "Sacramento Metro AQMD"),
    "san diego county apcd":              ("California", "San Diego County"),
    "san diego county air":               ("California", "San Diego County"),
    "san joaquin valley":                 ("California", "San Joaquin Valley"),
    "south coast aqmd":                   ("California", "South Coast AQMD"),
    "south coast air quality management": ("California", "South Coast AQMD"),
    "south coast air basin":              ("California", "South Coast AQMD"),
    "ventura county apcd":                ("California", "Ventura County"),
    "yolo-solano aqmd":                   ("California", "Yolo-Solano AQMD"),
    "yolo solano aqmd":                   ("California", "Yolo-Solano AQMD"),
    # State agencies that confirm California
    "carb":                               ("California", "California"),
    "california air resources board":     ("California", "California"),
    "calenviroscreen":                    ("California", "California"),
    # ── Arizona ───────────────────────────────────────────────────────────────
    "maricopa county":                    ("Arizona", "Maricopa County"),
    "pima county":                        ("Arizona", "Pima County"),
    "yuma county":                        ("Arizona", "Yuma County"),
    # ── Colorado ──────────────────────────────────────────────────────────────
    "denver metropolitan":                ("Colorado", "Denver Metro"),
    "denver metro":                       ("Colorado", "Denver Metro"),
    "northern front range":               ("Colorado", "Northern Front Range"),
    # ── Georgia ───────────────────────────────────────────────────────────────
    "atlanta":                            ("Georgia", "Atlanta"),
    # ── Illinois ──────────────────────────────────────────────────────────────
    "cook county":                        ("Illinois", "Cook County"),
    "chicago area":                       ("Illinois", "Chicago Area"),
    # ── Michigan ──────────────────────────────────────────────────────────────
    "wayne county":                       ("Michigan", "Wayne County"),
    "detroit":                            ("Michigan", "Detroit"),
    # ── Nevada ────────────────────────────────────────────────────────────────
    "clark county":                       ("Nevada", "Clark County"),
    "las vegas":                          ("Nevada", "Las Vegas"),
    # ── New Jersey ────────────────────────────────────────────────────────────
    "njdep":                              ("New Jersey", "New Jersey"),
    # ── New York ──────────────────────────────────────────────────────────────
    "nysdec":                             ("New York", "New York"),
    "new york city":                      ("New York", "New York City"),
    # ── Ohio ──────────────────────────────────────────────────────────────────
    "cuyahoga county":                    ("Ohio", "Cuyahoga County"),
    # ── Oregon ────────────────────────────────────────────────────────────────
    "portland":                           ("Oregon", "Portland"),
    # ── Pennsylvania ─────────────────────────────────────────────────────────
    "allegheny county":                   ("Pennsylvania", "Allegheny County"),
    # ── Texas ─────────────────────────────────────────────────────────────────
    "dallas-fort worth":                  ("Texas", "Dallas-Fort Worth"),
    "dallas fort worth":                  ("Texas", "Dallas-Fort Worth"),
    "harris county":                      ("Texas", "Harris County"),
    "houston-galveston":                  ("Texas", "Houston-Galveston"),
    "houston galveston":                  ("Texas", "Houston-Galveston"),
    "tarrant county":                     ("Texas", "Tarrant County"),
    "travis county":                      ("Texas", "Travis County"),
    # ── Washington state ──────────────────────────────────────────────────────
    "puget sound":                        ("Washington", "Puget Sound"),
    "seattle":                            ("Washington", "Seattle"),
    # ── Guam specific sub-locations ──────────────────────────────────────────
    "piti-cabras":                        ("Guam", "Piti-Cabras Power Plant"),
    "guam epa":                           ("Guam", "Guam"),
    "guam environmental protection":      ("Guam", "Guam"),
}

# EPA Region codes in docket IDs → display name
EPA_REGION_RE = re.compile(r'EPA-(R(\d+)|HQ)-', re.IGNORECASE)
EPA_REGION_NAMES = {
    "1":  "Region 1 (New England)",
    "2":  "Region 2 (NY/NJ/PR/VI)",
    "3":  "Region 3 (Mid-Atlantic)",
    "4":  "Region 4 (Southeast)",
    "5":  "Region 5 (Great Lakes)",
    "6":  "Region 6 (South-Central)",
    "7":  "Region 7 (Great Plains)",
    "8":  "Region 8 (Mountain West)",
    "9":  "Region 9 (Pacific Southwest)",
    "10": "Region 10 (Pacific Northwest)",
}


def parse_epa_region(docket_id):
    """Extract EPA region string from docket ID."""
    if not docket_id:
        return "National"
    m = EPA_REGION_RE.search(docket_id)
    if not m:
        return "National"
    if m.group(2):
        num = str(int(m.group(2)))  # strip leading zero
        return EPA_REGION_NAMES.get(num, f"Region {num}")
    return "National"  # HQ


def parse_locations(title, docket_id=""):
    """
    Return (states_list, primary_state, location_str).

    states_list  — all states/territories found, deduped, ordered by first appearance
    primary_state— states_list[0] or "Tribal" or None
    location_str — most specific sub-state name found (or None)
    """
    if not title:
        return [], None, None

    # 1. Tribal check (word-boundary)
    if _TRIBAL_RE.search(title):
        return ["Tribal"], "Tribal", None

    title_lower = title.lower()
    found_states = []
    location = None

    # 2. Territories
    for phrase, name in TERRITORIES.items():
        if phrase in title_lower:
            if name not in found_states:
                found_states.append(name)
            if location is None:
                location = name

    # 3. State names (check all, collect all)
    for state in STATES:
        if state.lower() in title_lower:
            if state not in found_states:
                found_states.append(state)

    # 4. Location aliases (air districts, counties, cities)
    # Sorted by length desc so longer/more-specific phrases match first
    for phrase in sorted(LOCATION_ALIASES, key=len, reverse=True):
        if phrase in title_lower:
            resolved_state, loc_name = LOCATION_ALIASES[phrase]
            if resolved_state not in found_states:
                found_states.append(resolved_state)
            # Only set location if it's more specific than a bare state name
            if location is None and loc_name != resolved_state:
                location = loc_name
            break  # use the most specific alias that matched

    primary = found_states[0] if found_states else None
    return found_states, primary, location


# ── Extension detection ───────────────────────────────────────────────────────

_EXTENSION_TRIGGERS = ("extension", "extend",)
_EXTENSION_CONTEXT = (
    "comment period", "comment date", "compliance date", "effective date",
    "deadline", "submitt", "promulgation", "public meeting",
)


def assign_is_extension(doc):
    title = (doc.get("title") or "").lower()
    if not any(t in title for t in _EXTENSION_TRIGGERS):
        return False
    return any(c in title for c in _EXTENSION_CONTEXT)


# ── Category assignment ───────────────────────────────────────────────────────

def _terms(doc):
    return set(doc.get("searchTerms", []))


CATEGORY_RULES = [
    ("Consent Decree",    lambda d: "consent decree" in _terms(d) or "-OGC-" in (d.get("docketId") or "")),
    ("Good Neighbor",     lambda d: "good neighbor" in _terms(d)),
    ("Regional Haze",     lambda d: "regional haze" in _terms(d)),
    ("Exceptional Events",lambda d: "exceptional events" in _terms(d)),
    ("MATS",              lambda d: "mercury air toxics" in _terms(d)),
    ("GHG / Climate",     lambda d: "greenhouse gas" in _terms(d)),
    ("NESHAP",            lambda d: "hazardous air pollutants" in _terms(d)),
    ("NSPS",              lambda d: "new source performance" in _terms(d)),
    ("NAAQS / PM2.5",     lambda d: bool(_terms(d) & {"national ambient air quality", "particulate matter", "PM2.5"})),
    ("Ozone",             lambda d: "ozone" in _terms(d)),
    ("SIP / TIP",         lambda d: bool(_terms(d) & {"state implementation plan", "tribal implementation plan", "approvals and promulgations"})),
    ("Other",             lambda d: True),
]


def assign_category(doc):
    for label, test in CATEGORY_RULES:
        if test(doc):
            return label
    return "Other"


# ── Comment counts ────────────────────────────────────────────────────────────

def fetch_comment_count(doc_id):
    url = f"https://api.regulations.gov/v4/documents/{doc_id}"
    for attempt in range(4):
        try:
            resp = requests.get(url, headers={"X-Api-Key": API_KEY}, timeout=20)
            if resp.status_code == 429:
                retry = int(resp.headers.get("Retry-After", 120))
                print(f"    rate limited — waiting {retry}s")
                time.sleep(retry)
                continue
            resp.raise_for_status()
            attrs = resp.json().get("data", {}).get("attributes", {})
            return attrs.get("commentCount") or 0
        except requests.HTTPError as e:
            print(f"    HTTP error for {doc_id}: {e}")
            return 0
    return 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--comments", action="store_true",
                        help="Fetch real comment counts from regulations.gov (slow)")
    args = parser.parse_args()

    path = DATA_DIR / "documents.json"
    docs = json.load(open(path))
    print(f"Loaded {len(docs)} documents")

    for doc in docs:
        title = doc.get("title", "")
        docket_id = doc.get("docketId", "")

        doc["category"] = assign_category(doc)
        doc["isExtension"] = assign_is_extension(doc)

        states_list, primary, location = parse_locations(title, docket_id)
        doc["state"] = primary
        doc["states"] = states_list
        doc["location"] = location
        doc["epaRegion"] = parse_epa_region(docket_id)

    cats = Counter(d["category"] for d in docs)
    print("\nCategories:")
    for cat, n in cats.most_common():
        print(f"  {n:4d}  {cat}")

    states_tagged = sum(1 for d in docs if d.get("state"))
    regions = Counter(d["epaRegion"] for d in docs)
    exts = sum(1 for d in docs if d["isExtension"])

    print(f"\nState/territory tagged: {states_tagged} (was {sum(1 for d in docs if d.get('state'))})")
    print(f"Extensions flagged: {exts}")

    print("\nEPA Region distribution:")
    for r, n in sorted(regions.items(), key=lambda x: x[0]):
        print(f"  {n:4d}  {r}")

    # Sample improved locations
    with_loc = [(d["location"], d.get("state"), d.get("title", "")[:70])
                for d in docs if d.get("location")]
    if with_loc:
        print(f"\nSample locations resolved ({len(with_loc)} total):")
        for loc, state, title in with_loc[:10]:
            print(f"  [{state}] {loc} — {title}")

    if args.comments:
        print(f"\n=== Fetching comment counts for {len(docs)} documents ===")
        for i, doc in enumerate(docs, 1):
            count = fetch_comment_count(doc["documentId"])
            doc["commentCount"] = count
            if count:
                print(f"  [{i}/{len(docs)}] {doc['documentId']}: {count} comments")
            elif i % 50 == 0:
                print(f"  [{i}/{len(docs)}] ...")
            time.sleep(0.4)

    with open(path, "w") as f:
        json.dump(docs, f, indent=2)
    print(f"\nSaved {path}")

    # Rebuild by_state index from updated states
    by_state = defaultdict(list)
    for doc in docs:
        for s in doc.get("states", []):
            if doc["documentId"] not in by_state[s]:
                by_state[s].append(doc["documentId"])
    with open(DATA_DIR / "by_state.json", "w") as f:
        json.dump(dict(by_state), f, indent=2)
    print(f"Saved static/data/by_state.json ({len(by_state)} states/groups)")


if __name__ == "__main__":
    main()
