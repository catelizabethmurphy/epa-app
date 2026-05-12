#!/usr/bin/env python3
"""
enrich.py — Post-process documents.json and press_releases.json with derived fields.

Adds to each document:
  category       — PFAS/TSCA program category
  compounds      — list of PFAS compounds mentioned (PFOA, PFOS, GenX, etc.)
  program        — regulatory program (SDWA, CERCLA, TSCA, RCRA, etc.)
  tscaSection    — TSCA section if applicable ("6", "8(a)", etc.)
  signalType     — rollback | protection | delay | litigation | rhetoric | other
  mahaRelevant   — bool: relates to health effects, drinking water, communities
  isExtension    — bool: deadline/comment-period extension

Usage:
    python3 enrich.py
    python3 enrich.py --comments    # also fetch real comment counts (slow)
"""
import argparse
import json
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("REGS_API_KEY")
DATA_DIR = Path("static/data")


# ── Era assignment ────────────────────────────────────────────────────────────

def assign_era(date_str):
    """Classify a document date into the regulatory era that produced it."""
    d = (date_str or "")[:10]
    if not d:
        return "unknown"
    if d < "2021-01-20":
        return "trump1"
    if d < "2025-01-20":
        return "biden"
    return "trump2"


# ── PFAS strict relevance gate ────────────────────────────────────────────────
# Documents must mention at least one PFAS compound to be kept.

PFAS_STRICT_RE = re.compile(
    r'\bpfas\b|pfoa|pfos|pfna|pfhxs|pfbs|pfba|pfda'
    r'|perfluoro|polyfluoro|genx|hfpo|forever chemical',
    re.IGNORECASE
)


def is_pfas_relevant(text):
    return bool(PFAS_STRICT_RE.search(text or ""))


# ── Compound detection ────────────────────────────────────────────────────────

COMPOUND_PATTERNS = [
    ("PFOA",   r'\bpfoa\b|perfluorooctanoic acid'),
    ("PFOS",   r'\bpfos\b|perfluorooctane sulfon'),
    ("GenX",   r'\bgenx\b|hfpo[-\s]?da|hexafluoropropylene oxide'),
    ("PFHxS",  r'\bpfhxs\b|perfluorohexane sulfon'),
    ("PFNA",   r'\bpfna\b|perfluorononanoic acid'),
    ("PFBS",   r'\bpfbs\b|perfluorobutane sulfon'),
    ("PFBA",   r'\bpfba\b|perfluorobutanoic acid'),
    ("PFDA",   r'\bpfda\b|perfluorodecanoic acid'),
    ("PFAS",   r'\bpfas\b|per[-\s]?and polyfluoro|polyfluoroalkyl'),
]
COMPOUND_RES = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in COMPOUND_PATTERNS]


def detect_compounds(text):
    text = text or ""
    found = [name for name, rx in COMPOUND_RES if rx.search(text)]
    # Deduplicate while preserving order; PFAS general only if no specific found
    if found and found[-1] == "PFAS" and len(found) > 1:
        found = [f for f in found if f != "PFAS"] or ["PFAS"]
    return found or ["PFAS"]


# ── Program detection ─────────────────────────────────────────────────────────

PROGRAM_RULES = [
    ("SDWA",    [r'\bsdwa\b', r'safe drinking water', r'maximum contaminant level',
                 r'\bmcl\b', r'national primary drinking water', r'\bnpdwr\b']),
    ("CERCLA",  [r'\bcercla\b', r'superfund', r'hazardous substance',
                 r'cerclis', r'national priorities']),
    ("TSCA",    [r'\btsca\b', r'toxic substances control', r'section 8\(a\)',
                 r'section 6\b', r'section 4\b', r'section 5\b',
                 r'reporting and recordkeeping']),
    ("RCRA",    [r'\brcra\b', r'resource conservation and recovery',
                 r'solid waste', r'hazardous waste']),
    ("CAA",     [r'\bcaa\b', r'clean air act', r'air emission', r'incineration pfas']),
    ("SDWA",    [r'drinking water']),  # fallback
]

PROGRAM_RES = [
    (prog, [re.compile(pat, re.IGNORECASE) for pat in pats])
    for prog, pats in PROGRAM_RULES
]


def detect_program(text):
    text = text or ""
    for prog, rxs in PROGRAM_RES:
        if any(rx.search(text) for rx in rxs):
            return prog
    return "TSCA"  # default for PFAS actions without clear program signal


# ── TSCA section detection ────────────────────────────────────────────────────

TSCA_SECTION_RES = [
    ("8(a)",  re.compile(r'tsca section 8\(a\)|section 8a\b|8\(a\)\(7\)', re.IGNORECASE)),
    ("8(d)",  re.compile(r'tsca section 8\(d\)|section 8d\b', re.IGNORECASE)),
    ("6",     re.compile(r'tsca section 6\b|section 6 risk|section 6 rule', re.IGNORECASE)),
    ("5",     re.compile(r'tsca section 5\b|new chemical', re.IGNORECASE)),
    ("4",     re.compile(r'tsca section 4\b|chemical testing', re.IGNORECASE)),
]


def detect_tsca_section(text):
    text = text or ""
    for sec, rx in TSCA_SECTION_RES:
        if rx.search(text):
            return sec
    return None


# ── Category assignment ───────────────────────────────────────────────────────

CATEGORY_RULES = [
    ("Drinking Water",      [r'drinking water', r'\bmcl\b', r'maximum contaminant level',
                              r'\bnpdwr\b', r'safe drinking water act']),
    ("CERCLA / Superfund",  [r'\bcercla\b', r'superfund', r'hazardous substance designation',
                              r'cercla designation']),
    ("TSCA Reporting",      [r'section 8\(a\)', r'8a reporting', r'reporting and recordkeeping',
                              r'tsca.*report']),
    ("TSCA Risk Mgmt",      [r'section 6\b', r'risk management.*tsca', r'unreasonable risk']),
    ("TSCA New Chemicals",  [r'section 5\b', r'new chemical', r'premanufacture notice']),
    ("PFAS Disposal",       [r'disposal.*pfas', r'pfas.*disposal', r'destruction.*pfas',
                              r'pfas.*destruction', r'incineration.*pfas', r'pfas.*treatment']),
    ("Litigation",          [r'\bsettlement\b', r'consent decree', r'court order',
                              r'litigation', r'-ogc-']),
    ("Other",               []),  # catch-all
]

CATEGORY_RES = [
    (cat, [re.compile(pat, re.IGNORECASE) for pat in pats])
    for cat, pats in CATEGORY_RULES
]


def assign_category(text, docket_id=""):
    combined = (text or "") + " " + (docket_id or "")
    for cat, rxs in CATEGORY_RES:
        if not rxs:
            return cat  # catch-all
        if any(rx.search(combined) for rx in rxs):
            return cat
    return "Other"


# ── Signal type detection ─────────────────────────────────────────────────────

# Rollback: weakens, delays, or removes existing protection
ROLLBACK_RES = [re.compile(p, re.IGNORECASE) for p in [
    r'\brescind', r'\brepeal', r'\bwithdraw', r'\breconsider',
    r'\brevoke', r'\bvacate', r'\bstay\b', r'\bsuspend',
    r'less stringent', r'\bexempt', r'roll back', r'rollback',
    r'reduce.*requirement', r'narrow.*scope', r'proposed.*revisions.*reduce',
    r'modify.*to.*reduce', r'relief.*from.*requirement',
]]

# Protection: establishes, strengthens, or maintains a protection
PROTECTION_RES = [re.compile(p, re.IGNORECASE) for p in [
    r'\bdesignat', r'establish.*mcl', r'final.*rule.*pfas',
    r'\bpromulgat', r'hazardous substance.*designation',
    r'strengthen', r'enforce.*pfas', r'cleanup.*pfas',
    r'retain.*mcl', r'keep.*mcl', r'maintain.*mcl',
    r'remediat', r'hold.*polluter', r'accountability',
]]

# Delay: extends timelines without substantive change
DELAY_RES = [re.compile(p, re.IGNORECASE) for p in [
    r'extension of.*time', r'extend.*deadline', r'extend.*compliance',
    r'compliance.*date.*extend', r'postpone', r'\bdefer\b',
    r'additional time', r'delayed.*effective', r'compliance.*period',
]]

# Litigation: court filings, settlements, consent decrees
LITIGATION_RES = [re.compile(p, re.IGNORECASE) for p in [
    r'consent decree', r'settlement agreement', r'court order',
    r'litigation', r'\b-ogc-\b',
]]

# Extension fallback (same logic as original app)
_EXTENSION_TRIGGERS = ("extension", "extend",)
_EXTENSION_CONTEXT = (
    "comment period", "comment date", "compliance date", "effective date",
    "deadline", "submitt", "promulgation", "public meeting",
)


def assign_signal_type(title, docket_id="", doc_type=""):
    text = (title or "") + " " + (docket_id or "")
    tl = text.lower()

    # Litigation first (OGC dockets are always litigation)
    if any(rx.search(text) for rx in LITIGATION_RES) or "-OGC-" in (docket_id or ""):
        return "litigation"

    # Delay before rollback — "extend compliance date" is a delay, not a rollback
    if any(rx.search(text) for rx in DELAY_RES):
        return "delay"

    if any(rx.search(text) for rx in ROLLBACK_RES):
        return "rollback"

    if any(rx.search(text) for rx in PROTECTION_RES):
        return "protection"

    # Supporting materials and non-rule types default to other
    if doc_type in ("Supporting & Related Material", "Other"):
        return "other"

    return "other"


def assign_is_extension(title):
    tl = (title or "").lower()
    if not any(t in tl for t in _EXTENSION_TRIGGERS):
        return False
    return any(c in tl for c in _EXTENSION_CONTEXT)


# ── MAHA relevance ────────────────────────────────────────────────────────────

MAHA_RES = [re.compile(p, re.IGNORECASE) for p in [
    r'drinking water', r'health effect', r'cancer', r'thyroid',
    r'immune', r'develop\w+ effect', r'child', r'infant',
    r'communit', r'public health', r'exposure', r'contaminate',
    r'blood level', r'bioaccumul', r'chronic', r'toxic',
]]


def assign_maha_relevant(text):
    text = text or ""
    return any(rx.search(text) for rx in MAHA_RES)


# ── Comment counts ────────────────────────────────────────────────────────────

def fetch_comment_count(doc_id):
    url = f"https://api.regulations.gov/v4/documents/{doc_id}"
    for attempt in range(3):
        try:
            resp = requests.get(url, headers={"X-Api-Key": API_KEY}, timeout=20)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 120))
                print(f"    rate limited — waiting {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            attrs = resp.json().get("data", {}).get("attributes", {})
            return attrs.get("commentCount") or 0
        except requests.HTTPError as e:
            print(f"    HTTP error for {doc_id}: {e}")
            return 0
    return 0


# ── Main ──────────────────────────────────────────────────────────────────────

def enrich_document(doc):
    title = doc.get("title", "") or ""
    docket_id = doc.get("docketId", "") or ""
    doc_type = doc.get("documentType", "") or ""
    combined = title + " " + docket_id

    doc["compounds"] = detect_compounds(combined)
    doc["program"] = detect_program(combined)
    doc["tscaSection"] = detect_tsca_section(combined)
    doc["category"] = assign_category(combined, docket_id)
    doc["signalType"] = assign_signal_type(title, docket_id, doc_type)
    doc["mahaRelevant"] = assign_maha_relevant(combined)
    doc["isExtension"] = assign_is_extension(title)
    doc["era"] = assign_era(doc.get("postedDate") or doc.get("publicationDate"))
    return doc


def assign_press_signal(title, body):
    text = (title + " " + body).lower()
    if any(w in text for w in ["reconsider", "withdraw", "repeal", "suspend",
                                "rollback", "rescind", "revoke"]):
        return "rollback"
    if any(w in text for w in ["delay", "extend", "postpone", "pause"]):
        return "delay"
    if any(w in text for w in ["protect", "enforce", "cleanup", "remediat",
                                "drinking water standard", "mcl", "designat",
                                "finalize", "final rule"]):
        return "protection"
    return "rhetoric"


def enrich_press_release(pr):
    text = (pr.get("title", "") or "") + " " + (pr.get("body", "") or "")
    pr["compounds"] = detect_compounds(text)
    pr["program"] = detect_program(text)
    pr["tscaSection"] = detect_tsca_section(text)
    pr["category"] = assign_category(text)
    # Use existing signalType if already set by fetcher; otherwise infer from text
    if not pr.get("signalType") or pr["signalType"] == "rhetoric":
        pr["signalType"] = assign_press_signal(pr.get("title", ""), pr.get("body", ""))
    pr["mahaRelevant"] = assign_maha_relevant(text)
    pr["isExtension"] = False
    return pr


def apply_overrides(items_by_id, overrides):
    for item_id, fields in overrides.items():
        if item_id in items_by_id:
            items_by_id[item_id].update(fields)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--comments", action="store_true",
                        help="Fetch real comment counts from regulations.gov (slow)")
    args = parser.parse_args()

    # Load overrides
    override_path = DATA_DIR / "signals_override.json"
    overrides = {}
    if override_path.exists():
        data = json.load(open(override_path))
        overrides = data.get("documents", {})
        press_overrides = data.get("press_releases", {})
    else:
        press_overrides = {}

    # ── Enrich regulations.gov documents ─────────────────────────────────────
    doc_path = DATA_DIR / "documents.json"
    if doc_path.exists():
        docs = json.load(open(doc_path))
        # Strict PFAS relevance gate: filter out anything not mentioning PFAS
        before = len(docs)
        docs = [d for d in docs if is_pfas_relevant(
            (d.get("title") or "") + " " + " ".join(d.get("searchTerms", []))
        )]
        print(f"Enriching {len(docs)} regulations.gov documents… ({before - len(docs)} filtered as non-PFAS)")
        for doc in docs:
            enrich_document(doc)
        apply_overrides({d["documentId"]: d for d in docs}, overrides)

        from collections import Counter
        cats = Counter(d["category"] for d in docs)
        sigs = Counter(d["signalType"] for d in docs)
        maha = sum(1 for d in docs if d["mahaRelevant"])
        print("Categories:")
        for c, n in cats.most_common(): print(f"  {n:4d}  {c}")
        print("Signal types:")
        for s, n in sigs.most_common(): print(f"  {n:4d}  {s}")
        print(f"MAHA relevant: {maha}")

        if args.comments:
            print(f"\nFetching comment counts for {len(docs)} documents…")
            for i, doc in enumerate(docs, 1):
                count = fetch_comment_count(doc["documentId"])
                doc["commentCount"] = count
                if count:
                    print(f"  [{i}/{len(docs)}] {doc['documentId']}: {count}")
                elif i % 50 == 0:
                    print(f"  [{i}/{len(docs)}] …")
                time.sleep(0.4)

        with open(doc_path, "w") as f:
            json.dump(docs, f, indent=2)
        print(f"Saved {doc_path}")
    else:
        print("documents.json not found — run fetch_regs.py first")

    # ── Enrich Federal Register documents ────────────────────────────────────
    fr_path = DATA_DIR / "fr_documents.json"
    if fr_path.exists():
        fr_docs = json.load(open(fr_path))
        before = len(fr_docs)
        fr_docs = {k: v for k, v in fr_docs.items() if is_pfas_relevant(
            (v.get("title") or "") + " " + (v.get("abstract") or "")
        )}
        print(f"\nEnriching {len(fr_docs)} Federal Register documents… ({before - len(fr_docs)} filtered as non-PFAS)")
        for doc in fr_docs.values():
            text = (doc.get("title", "") or "") + " " + (doc.get("abstract", "") or "")
            doc["compounds"] = detect_compounds(text)
            doc["program"] = detect_program(text)
            doc["tscaSection"] = detect_tsca_section(text)
            doc["category"] = assign_category(text)
            doc["signalType"] = assign_signal_type(
                doc.get("title", ""), "", doc.get("type", ""))
            doc["mahaRelevant"] = assign_maha_relevant(text)
            doc["era"] = assign_era(doc.get("publicationDate") or doc.get("postedDate"))
        with open(fr_path, "w") as f:
            json.dump(fr_docs, f, indent=2)
        print(f"Saved {fr_path}")
    else:
        print("fr_documents.json not found — run fetch_fr.py first")

    # ── Enrich press releases ─────────────────────────────────────────────────
    press_path = DATA_DIR / "press_releases.json"
    if press_path.exists():
        press = json.load(open(press_path))
        print(f"\nEnriching {len(press)} press releases…")
        for pr in press:
            enrich_press_release(pr)
        apply_overrides({pr["pressId"]: pr for pr in press}, press_overrides)
        with open(press_path, "w") as f:
            json.dump(press, f, indent=2)
        print(f"Saved {press_path}")
    else:
        print("press_releases.json not found — run fetch_press.py first")


if __name__ == "__main__":
    main()
