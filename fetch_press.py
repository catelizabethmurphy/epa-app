#!/usr/bin/env python3
"""
fetch_press.py — Scrape EPA PFAS press releases.

Primary source: https://www.epa.gov/pfas/press-releases-related-pfas
  EPA's own curated list of all PFAS-related press releases.

Fallback seeds cover known releases that might not appear on that page.

Individual pages are fetched live from epa.gov.
Releases before START_DATE are silently skipped (return PRE_START sentinel).

Usage:
    python3 fetch_press.py
    python3 fetch_press.py --seeds-only

Writes:
    static/data/press_releases.json
"""
import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://www.epa.gov"
PFAS_LISTING_URL = "https://www.epa.gov/pfas/press-releases-related-pfas"
DATA_DIR = Path("static/data")
START_DATE = "2025-01-20"

PFAS_KEYWORDS = [
    "pfas", "pfoa", "pfos", "pfna", "pfhxs", "pfbs",
    "perfluoro", "polyfluoro", "genx", "hfpo",
    "forever chemical", "forever chemicals",
    "maximum contaminant level",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 pfas-tracker/1.0 academic-research"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
}

# Sentinel — returned when date < START_DATE
PRE_START = "PRE_START"

# Extra releases not always on the curated page
SEED_URLS = [
    "/newsreleases/trump-epa-announces-next-steps-regulatory-pfoa-and-pfos-cleanup-efforts-provides",
    "/newsreleases/epa-announces-it-will-keep-maximum-contaminant-levels-pfoa-pfos",
    "/newsreleases/epa-proposes-changes-make-pfas-reporting-requirements-more-practical-and-0",
    "/newsreleases/epa-releases-pfas-action-plan-program-update",
]


def get(url, retries=2):
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if attempt == retries:
                raise
            time.sleep(3)


def is_pfas_relevant(title, body=""):
    text = (title + " " + body).lower()
    return any(kw in text for kw in PFAS_KEYWORDS)


def parse_date(raw):
    if not raw:
        return None
    raw = re.sub(r'\s+', ' ', raw).strip()
    m = re.match(r'(\d{4}-\d{2}-\d{2})', raw)
    if m:
        return m.group(1)
    for fmt in ["%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%d %B %Y", "%B %Y"]:
        try:
            return datetime.strptime(raw[:20], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _infer_signal(title, body):
    text = (title + " " + body).lower()
    if any(w in text for w in ["reconsider", "withdraw", "repeal", "suspend",
                                "rollback", "rescind", "revoke"]):
        return "rollback"
    if any(w in text for w in ["delay", "extend", "postpone", "pause"]):
        return "delay"
    if any(w in text for w in ["protect", "enforce", "cleanup", "remediat",
                                "drinking water standard", "mcl", "designat",
                                "finalize", "final rule", "requires"]):
        return "protection"
    return "rhetoric"


def scrape_individual(path):
    """
    Scrape one press release page.

    Returns:
      - dict      PFAS-relevant and date >= START_DATE
      - PRE_START date < START_DATE
      - None      not PFAS-relevant or error
    """
    clean_path = path.split("?")[0].rstrip("/")
    slug = clean_path.split("/")[-1]
    url = BASE + clean_path if clean_path.startswith("/") else clean_path

    try:
        resp = get(url)
    except Exception as e:
        print(f"    SKIP {slug[:60]}: {e}", flush=True)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    h1 = soup.find("h1", class_="page-title") or soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    date_iso = None
    t = soup.find("time", {"datetime": True})
    if t:
        date_iso = parse_date(t["datetime"])
    if not date_iso:
        meta = (soup.find("meta", {"property": "DC.date.created"}) or
                soup.find("meta", {"name": "DC.date.created"}))
        if meta:
            date_iso = parse_date(meta.get("content", ""))
    if not date_iso:
        for sel in [".field--name-field-press-release-date",
                    ".date-display-single", "span.date", ".submitted"]:
            el = soup.select_one(sel)
            if el:
                date_iso = parse_date(el.get_text(strip=True))
                if date_iso:
                    break

    if date_iso and date_iso < START_DATE:
        return PRE_START

    body_el = (
        soup.find("div", class_=re.compile(r"field--name-body")) or
        soup.find("div", class_=re.compile(r"node__content")) or
        soup.find("article") or
        soup.find("main")
    )
    body = ""
    if body_el:
        paras = body_el.find_all("p")
        body = " ".join(p.get_text(" ", strip=True) for p in paras[:8])

    if not is_pfas_relevant(title, body):
        return None

    return {
        "pressId": slug,
        "title": title,
        "date": date_iso,
        "url": url,
        "body": body[:3000],
        "source": "epa.gov",
        "signalType": _infer_signal(title, body),
    }


def discover_from_pfas_listing():
    """
    Scrape EPA's curated PFAS press releases page and return all
    /newsreleases/ paths found there.
    """
    print(f"  Fetching {PFAS_LISTING_URL}…", flush=True)
    try:
        resp = get(PFAS_LISTING_URL)
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    seen = set()
    paths = []
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].rstrip("/")
        # Normalise to root-relative
        if href.startswith("http"):
            if "epa.gov/newsreleases/" in href:
                href = "/" + href.split("epa.gov/", 1)[1]
            else:
                continue
        if href.startswith("/newsreleases/") and len(href) > 20 and href not in seen:
            seen.add(href)
            paths.append(href)

    print(f"  Found {len(paths)} unique press release paths", flush=True)
    return paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds-only", action="store_true",
                        help="Skip curated listing, only scrape seed URLs")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    results = {}
    seen = set()
    pre_count = 0

    if not args.seeds_only:
        # ── Primary: EPA curated PFAS listing ─────────────────────────────────
        print("=== Discovering from EPA PFAS press releases page ===", flush=True)
        paths = discover_from_pfas_listing()

        print(f"\n=== Scraping {len(paths)} releases ===", flush=True)
        for path in paths:
            slug = path.rstrip("/").split("/")[-1]
            if slug in seen:
                continue
            seen.add(slug)
            pr = scrape_individual(path)
            if pr is None:
                pass
            elif pr == PRE_START:
                pre_count += 1
            else:
                results[pr["pressId"]] = pr
                print(f"  + [{pr.get('date', '?')}] {pr['title'][:72]}", flush=True)
            time.sleep(1.2)

    # ── Seed URLs — safety net ─────────────────────────────────────────────
    print("\n=== Seeded press releases ===", flush=True)
    for path in SEED_URLS:
        slug = path.rstrip("/").split("/")[-1]
        if slug in seen:
            continue
        seen.add(slug)
        pr = scrape_individual(path)
        if pr is None:
            print(f"  - {slug[:60]} (not PFAS or error)", flush=True)
        elif pr == PRE_START:
            pre_count += 1
            print(f"  - {slug[:60]} (pre-{START_DATE})", flush=True)
        else:
            results[pr["pressId"]] = pr
            print(f"  + [{pr.get('date', '?')}] {pr['title'][:72]}", flush=True)
        time.sleep(1.2)

    output = sorted(results.values(), key=lambda x: x.get("date") or "", reverse=True)

    out_path = DATA_DIR / "press_releases.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n=== Results ===", flush=True)
    print(f"Pre-{START_DATE} releases skipped: {pre_count}", flush=True)
    print(f"PFAS-relevant saved:               {len(output)}", flush=True)
    print(f"Saved: {out_path}", flush=True)

    if output:
        print("\nAll saved releases:", flush=True)
        for pr in output:
            print(f"  [{pr.get('date','?')}] ({pr.get('signalType','?'):10s}) {pr['title'][:70]}", flush=True)


if __name__ == "__main__":
    main()
