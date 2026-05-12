#!/usr/bin/env python3
"""
fetch_agenda.py — Pull EPA PFAS/TSCA upcoming rules from the Unified Regulatory Agenda.

The Unified Agenda (published by OIRA/reginfo.gov twice yearly, Spring and Fall)
lists every agency's planned regulatory actions — the government's forward calendar.
This script fetches EPA entries relevant to PFAS and TSCA to show what's coming
down the pike.

Usage:
    python3 fetch_agenda.py

Writes:
    static/data/agenda.json   — list of planned EPA actions, keyed by RIN
"""
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

DATA_DIR = Path("static/data")

# Fetch the last two terms plus current
AGENDA_TERMS = [
    (2025, "Spring"),
    (2025, "Fall"),
    (2026, "Spring"),
]

PFAS_RE = re.compile(
    r'\bpfas\b|pfoa|pfos|perfluoro|polyfluoro|genx|hfpo|forever chemical'
    r'|maximum contaminant level|tsca|toxic substances control'
    r'|hazardous substance.*pfas|pfas.*hazardous|safe drinking water',
    re.IGNORECASE
)

STAGE_MAP = {
    "Proposed Rule Stage": "proposed",
    "Final Rule Stage": "final",
    "Long-term Actions": "long_term",
    "Completed Actions": "completed",
    "Prerule Stage": "prerule",
}

# Human-readable action code labels
ACTION_LABELS = {
    "NPRM": "Proposed Rule",
    "FR": "Final Rule",
    "IFR": "Interim Final Rule",
    "ANPRM": "Advance Proposed Rule",
    "NPRM-Com": "Comment Period",
    "FR-Com": "Final Rule Comment Period",
    "SNPRM": "Supplemental Proposed Rule",
    "Withdraw": "Withdrawn",
    "Final Action": "Final Action",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 pfas-tracker/1.0 academic-research-bot",
    "Accept": "application/xml,text/xml,*/*",
}


def _text(el, *tags):
    for tag in tags:
        child = el.find(tag)
        if child is not None and child.text:
            return child.text.strip()
    return None


def fetch_agenda_xml(year, term):
    url = f"https://www.reginfo.gov/public/do/eAgendaXml?year={year}&term={term}"
    try:
        print(f"  GET {url}")
        resp = requests.get(url, headers=HEADERS, timeout=120)
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as e:
        print(f"  Failed: {e}")
        return None


def parse_agenda_xml(xml_bytes, year, term):
    if not xml_bytes:
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"  XML parse error: {e}")
        return []

    entries = []

    # The Unified Agenda XML wraps each rule in <REGTEXT>
    for regtext in root.iter("REGTEXT"):
        agency = _text(regtext, "AGENCY", "Agency") or ""
        if "Environmental Protection Agency" not in agency and "EPA" not in agency:
            continue

        rin = _text(regtext, "RIN", "Rin") or ""
        title = _text(regtext, "TITLE", "Title") or ""
        abstract = _text(regtext, "ABSTRACT", "Abstract") or ""
        stage_raw = _text(regtext, "RINSTAGE", "Stage") or ""
        subagency = _text(regtext, "SUBAGENCY", "Subagency") or ""
        priority = _text(regtext, "PRIORITY") or ""
        cfr = _text(regtext, "CFR_SECTION") or ""

        if not PFAS_RE.search(f"{title} {abstract}"):
            continue

        # Parse timetable — try both TTROWS and direct TIMETABLE children
        timetable = []
        for container in list(regtext.iter("TTROWS")) + list(regtext.iter("TIMETABLE")):
            action = _text(container, "TTACTION") or ""
            date_raw = _text(container, "TTDATE") or ""
            if action and {"action": action, "date": date_raw} not in timetable:
                timetable.append({
                    "action": action,
                    "actionLabel": ACTION_LABELS.get(action, action),
                    "date": date_raw,
                })

        # Normalize next action (first non-completed entry)
        next_entry = timetable[0] if timetable else {}

        entries.append({
            "rin": rin,
            "title": title,
            "abstract": abstract[:1200],
            "stage": STAGE_MAP.get(stage_raw, stage_raw or "unknown"),
            "subagency": subagency,
            "priority": priority,
            "cfr": cfr,
            "nextAction": next_entry.get("action"),
            "nextActionLabel": next_entry.get("actionLabel"),
            "nextDate": next_entry.get("date"),
            "timetable": timetable[:6],
            "agendaYear": year,
            "agendaTerm": term,
            "reginfoUrl": (
                f"https://www.reginfo.gov/public/do/eAgendaViewRule?pubId=&RIN={rin}"
                if rin else None
            ),
        })

    return entries


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    all_entries = {}  # RIN → entry; last write wins (newer term beats older)

    for year, term in AGENDA_TERMS:
        print(f"\n=== Unified Agenda {year} {term} ===")
        xml_bytes = fetch_agenda_xml(year, term)
        if not xml_bytes:
            continue

        entries = parse_agenda_xml(xml_bytes, year, term)
        print(f"  {len(entries)} EPA PFAS/TSCA entries found")

        for entry in entries:
            key = entry["rin"] or entry["title"][:60]
            all_entries[key] = entry  # newer term overwrites older

        time.sleep(3)  # be polite to reginfo.gov

    output = sorted(
        all_entries.values(),
        key=lambda x: (x.get("agendaYear", 0), x.get("rin") or ""),
        reverse=True,
    )

    out_path = DATA_DIR / "agenda.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nSaved {out_path} ({len(output)} upcoming EPA PFAS/TSCA actions)")

    if output:
        print("\nSample entries:")
        for e in output[:5]:
            print(f"  [{e.get('rin', '—')}] {e['title'][:70]}")
            if e.get("nextAction"):
                print(f"    → {e['nextActionLabel']} ({e['nextDate']})")


if __name__ == "__main__":
    main()
