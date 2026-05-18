import csv
import html
import json
import re
from datetime import datetime
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from flask import Flask, render_template, abort, send_from_directory, redirect, url_for, jsonify

app = Flask(__name__)
app.config['FREEZER_RELATIVE_URLS'] = True
# Some referenced documentIds (from similarities / press-release links) point to
# regs.gov records that aren't in our local snapshot — skip-bake them instead of
# aborting the whole freeze.
app.config['FREEZER_IGNORE_404_NOT_FOUND'] = True
# Skip the large source CSVs + full-text dumps when copying static/ into build/.
# They're consumed by the Python loaders at freeze time and aren't needed by the
# deployed static site, which only serves the rendered HTML and the slim
# pws-search.json. Keeping them out keeps the GH Pages push under GitHub's
# packfile size limits.
app.config['FREEZER_STATIC_IGNORE'] = [
    'data/pws_data.csv',
    'data/pws_summaries.csv',
    'data/pws_summary_stats.csv',
    'data/text/*',
    '.DS_Store',
    '*/.DS_Store',
]

DATA_DIR = Path("static/data")

SUBSTANTIVE_TYPES = {"Rule", "Proposed Rule", "Notice"}

STATE_ABBR = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "District of Columbia": "DC",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
}


# ── Data loaders ──────────────────────────────────────────────────────────────

def _load(path, default=None):
    p = DATA_DIR / path
    if not p.exists():
        return default if default is not None else {}
    with open(p) as f:
        return json.load(f)


def get_documents():
    return _load("documents.json", [])


def get_dockets():
    return _load("dockets.json", {})


def get_fr_documents():
    return _load("fr_documents.json", {})


def get_press_releases():
    return _load("press_releases.json", [])


def get_similarities():
    return _load("similarities.json", {})


def get_events():
    return _load("events.json", [])


def get_status():
    return _load("status.json", [])


def get_sources():
    return _load("sources.json", [])


def get_pfas_context():
    return _load("pfas_context.json", {})


def get_timeline_page(slug):
    return _load(f"timelines/{slug}.json", {})


def get_court_actions():
    return _load("court_actions.json", [])


def _clean_text(value):
    if value is None:
        return ""
    text = html.unescape(value)
    text = " ".join(text.split())
    return text.strip()


def _slugify(value):
    text = re.sub(r"[^a-zA-Z0-9\s-]", "", value or "")
    text = re.sub(r"\s+", "-", text.strip().lower())
    return text


@lru_cache(maxsize=1)
def get_state_bills():
    path = DATA_DIR / "pfas-bill-tracker.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        items = []
        for row in reader:
            items.append({
                "state": _clean_text(row.get("State")),
                "status": _clean_text(row.get("Status")),
                "year": _clean_text(row.get("Year")),
                "bill": _clean_text(row.get("Bill")),
                "chemicals": _clean_text(row.get("Toxic Chemical")),
                "issue": _clean_text(row.get("Issue/Sector")),
                "solution": _clean_text(row.get("Safer Solution")),
                "description": _clean_text(row.get("Description")),
            })
    return items


# EPA MCL for PFOA and PFOS under the 2024 final rule (parts per trillion).
PFAS_MCL_PPT = 4.0

# Per-contaminant maximum contaminant levels (parts per trillion) from the
# Biden EPA's April 2024 National Primary Drinking Water Regulation.
PFAS_MCL_BY_CONTAMINANT = {
    "PFOA": 4.0,
    "PFOS": 4.0,
    "PFHxS": 10.0,
    "PFNA": 10.0,
    "HFPO-DA": 10.0,
}


# Tokens that should stay uppercase when title-casing water-system names.
_UPPERCASE_TOKENS = {
    "PWS", "PWD", "PSD", "MWD", "WSD", "WD", "WSC", "MUD", "WS",
    "WTP", "WWTP", "WWTF", "WSA", "PUD", "MUA", "MHP", "RV",
    "USA", "US", "USAF", "USMC", "USN", "USAG", "VA", "DOD",
    "I", "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII",
    "HOA", "POA", "LLC", "LP", "INC", "CO",
    "NW", "NE", "SW", "SE",
}
_LOWERCASE_TOKENS = {"and", "of", "the", "at", "in", "on", "for", "to", "de", "la", "del"}


def _titlecase_name(value):
    if not value:
        return ""
    out = []
    for i, raw in enumerate(value.split()):
        # Strip surrounding punctuation but remember it so we can re-attach.
        lead = ""
        trail = ""
        token = raw
        while token and not token[0].isalnum():
            lead += token[0]
            token = token[1:]
        while token and not token[-1].isalnum():
            trail = token[-1] + trail
            token = token[:-1]
        if not token:
            out.append(raw)
            continue
        upper = token.upper()
        lower = token.lower()
        if upper in _UPPERCASE_TOKENS:
            cased = upper
        elif i > 0 and lower in _LOWERCASE_TOKENS:
            cased = lower
        elif "-" in token:
            cased = "-".join(p.capitalize() for p in token.split("-"))
        elif "'" in token and not token.upper().startswith("O'"):
            # Handle names like Murphy's — capitalize first letter only.
            cased = token[0].upper() + token[1:].lower()
        elif token.upper().startswith(("MC", "MAC")) and len(token) > 2:
            prefix_len = 2 if token.upper().startswith("MC") else 3
            cased = token[:prefix_len].capitalize() + token[prefix_len:].capitalize()
        else:
            cased = token.capitalize()
        out.append(f"{lead}{cased}{trail}")
    return " ".join(out)


@lru_cache(maxsize=1)
def get_pws_rows():
    """Per-PWS/contaminant rows from UCMR-style summary."""
    path = DATA_DIR / "pws_summaries.csv"
    if not path.exists():
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            state_raw = (r.get("state") or "").strip().upper()
            # Tribal water systems use a numeric EPA region code instead of a state
            # abbreviation. Keep those rows in the search index (their ZIPs are still
            # useful for the ZIP-code lookup) but leave their state blank so they don't
            # land in any state aggregate.
            if state_raw.isdigit():
                state = ""
            elif len(state_raw) == 2 and state_raw.isalpha():
                state = state_raw
            else:
                continue
            try:
                n_samples = int(r.get("n_samples") or 0)
                n_over = int(r.get("n_samples_over_limit") or 0)
                max_res = float(r.get("max_result") or 0)
                max_times = float(r.get("max_result_times_over_limit") or 0)
            except ValueError:
                continue
            rows.append({
                "pwsid": (r.get("pwsid") or "").strip(),
                "name": _titlecase_name(_clean_text(r.get("pws_name"))),
                "size": (r.get("size") or "").strip(),
                "state": state,
                "n_samples": n_samples,
                "n_samples_over_limit": n_over,
                "max_result": max_res,
                "max_result_times_over_limit": max_times,
                "first_collection_date": (r.get("first_collection_date") or "")[:10],
                "last_collection_date": (r.get("last_collection_date") or "")[:10],
                "contaminant": (r.get("contaminant") or "").strip(),
                "over_limit": (r.get("over_limit") or "").strip().upper() == "TRUE",
                "zips": [z.strip() for z in (r.get("zips_served") or "").split(",") if z.strip()],
            })
    return rows


@lru_cache(maxsize=1)
def get_pws_state_stats():
    """Per-state, per-contaminant counts (n_pws, n_pws_over_limit, pct)."""
    path = DATA_DIR / "pws_summary_stats.csv"
    if not path.exists():
        return []
    items = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            state = (r.get("state") or "").strip().upper()
            if len(state) != 2:
                continue
            try:
                n_pws = int(r.get("n_pws") or 0)
                n_over = int(r.get("n_pws_over_limit") or 0)
                pct = float(r.get("pct_pws_over_limit") or 0)
            except ValueError:
                continue
            items.append({
                "state": state,
                "contaminant": (r.get("contaminant") or "").strip().upper(),
                "n_pws": n_pws,
                "n_pws_over_limit": n_over,
                "pct_pws_over_limit": pct,
            })
    return items


@lru_cache(maxsize=1)
def get_water_summary():
    """Build a national + per-state water-testing rollup from the two CSVs."""
    rows = get_pws_rows()
    stats = get_pws_state_stats()

    # Per-state: combine PFOA and PFOS into a single row, plus track each separately.
    by_state = {}
    for s in stats:
        st = by_state.setdefault(s["state"], {
            "state": s["state"],
            "n_pws": 0,                 # unique PWS in the state (max of contaminants)
            "by_contaminant": {},       # 'PFOA' -> {n_pws, n_over, pct}
            "n_pws_any_exceed": 0,      # filled from rows below
        })
        st["by_contaminant"][s["contaminant"]] = {
            "n_pws": s["n_pws"],
            "n_pws_over_limit": s["n_pws_over_limit"],
            "pct": s["pct_pws_over_limit"],
        }
        # Each contaminant covers the same set of PWS — use max as the unique count.
        st["n_pws"] = max(st["n_pws"], s["n_pws"])

    # Walk rows to compute "any contaminant exceeded" per PWS (PFOA OR PFOS).
    pws_any = {}  # (state, pwsid) -> bool
    for r in rows:
        key = (r["state"], r["pwsid"])
        pws_any[key] = pws_any.get(key, False) or r["over_limit"]
    for (state, _pwsid), exceeded in pws_any.items():
        if exceeded and state in by_state:
            by_state[state]["n_pws_any_exceed"] += 1

    # Derived pct for "any contaminant"
    for st in by_state.values():
        st["pct_any_exceed"] = (
            100.0 * st["n_pws_any_exceed"] / st["n_pws"]
            if st["n_pws"] else 0.0
        )

    states_ranked = sorted(
        by_state.values(),
        key=lambda s: (-s["pct_any_exceed"], -s["n_pws_any_exceed"]),
    )

    # Top exceeding systems nationally — rank by how many times over the limit.
    detail = get_pws_detail()
    worst_systems = []
    for r in sorted(
        (r for r in rows if r["over_limit"] and r["max_result_times_over_limit"] > 0),
        key=lambda r: r["max_result_times_over_limit"],
        reverse=True,
    )[:25]:
        d = detail.get(r["pwsid"])
        peak = (d or {}).get("peak_by_contaminant", {}).get(r["contaminant"], {}) if d else {}
        pop, matched = estimate_population_for_zips(r["zips"])
        worst_systems.append({
            **r,
            "peak_ppt": (peak.get("ppt") or r["max_result"] * 1000.0),
            "peak_date": peak.get("date", ""),
            "water_type": (d or {}).get("water_type", ""),
            "water_type_label": (d or {}).get("water_type_label", ""),
            "served_population": pop,
            "served_zip_match_count": matched,
        })

    # National totals — use unique physical samples derived from pws_data.csv,
    # not the analytical-result count (which counts each sample once per PFAS).
    total_pws = len({(r["state"], r["pwsid"]) for r in rows})
    total_pws_any_exceed = sum(1 for v in pws_any.values() if v)
    total_samples = sum(d["n_samples"] for d in detail.values()) if detail else 0
    total_samples_over = sum(d["n_samples_over_limit"] for d in detail.values()) if detail else 0

    # Date range
    dates = [r["last_collection_date"] for r in rows if r["last_collection_date"]]
    first_dates = [r["first_collection_date"] for r in rows if r["first_collection_date"]]
    date_range = {
        "earliest": min(first_dates) if first_dates else "",
        "latest": max(dates) if dates else "",
    }

    return {
        "national": {
            "total_pws": total_pws,
            "total_pws_any_exceed": total_pws_any_exceed,
            "pct_pws_any_exceed": (100.0 * total_pws_any_exceed / total_pws) if total_pws else 0.0,
            "total_samples": total_samples,
            "total_samples_over": total_samples_over,
            "states_covered": len(by_state),
            "date_range": date_range,
            "mcl_ppt": PFAS_MCL_PPT,
        },
        "states_ranked": states_ranked,
        "by_state": by_state,
        "worst_systems": worst_systems,
    }


_MEETING_TITLE_KEYWORDS = ("meetings:", "meeting;", "advisory council",
                           "community engagement", "national drinking water advisory")


def _is_primary_doc(doc):
    """True for docs that belong in the main regulatory timeline (not supplementary)."""
    t = doc.get("documentType", "")
    if t in ("Rule", "Proposed Rule"):
        return True
    if t == "Notice":
        title = (doc.get("title") or "").lower()
        return not any(kw in title for kw in _MEETING_TITLE_KEYWORDS)
    return False


def get_trump1_context():
    return _load("trump1_context.json", {})


def events_by_month(events):
    """Group events list into [(year_month_label, [events]), ...]"""
    from collections import OrderedDict
    groups = OrderedDict()
    import datetime
    month_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    for ev in sorted(events, key=lambda e: e.get("date") or "", reverse=True):
        d = ev.get("date", "")
        if not d:
            continue
        key = d[:7]
        if key not in groups:
            try:
                y, m = int(d[:4]), int(d[5:7])
                groups[key] = {"label": f"{month_names[m-1]} {y}", "events": []}
            except (ValueError, IndexError):
                groups[key] = {"label": key, "events": []}
        groups[key]["events"].append(ev)
    return list(groups.values())


def get_doc_text(item_id, prefix=""):
    """Return plain text for a document, or None."""
    name = f"{prefix}{item_id}.txt" if prefix else f"{item_id}.txt"
    p = DATA_DIR / "text" / name
    return p.read_text(encoding="utf-8") if p.exists() else None


FR_TYPE_MAP = {
    "RULE": "Rule",
    "PRORULE": "Proposed Rule",
    "NOTICE": "Notice",
    "PRESDOCU": "Presidential Document",
}


def normalize_fr_doc(fr_doc):
    """Convert an FR document dict to the same shape as a regs.gov document."""
    doc_num = fr_doc.get("documentNumber", "")
    doc_type = FR_TYPE_MAP.get(fr_doc.get("type", ""), fr_doc.get("type", ""))
    docket_ids = fr_doc.get("docketIds") or []
    docket_id = docket_ids[0] if docket_ids else None
    comment_end = fr_doc.get("commentsCloseDate") or ""
    return {
        "documentId": f"FR-{doc_num}",
        "docketId": docket_id,
        "title": fr_doc.get("title"),
        "documentType": doc_type,
        "postedDate": fr_doc.get("publicationDate", ""),
        "commentEndDate": comment_end[:10] if comment_end else None,
        "openForComment": bool(comment_end and comment_end >= "2026-01-01"),  # rough heuristic
        "withdrawn": False,
        "frDocNum": doc_num,
        "signalType": fr_doc.get("signalType", "other"),
        "category": fr_doc.get("category"),
        "compounds": fr_doc.get("compounds", []),
        "program": fr_doc.get("program"),
        "era": fr_doc.get("era", "unknown"),
        "mahaRelevant": fr_doc.get("mahaRelevant", False),
        "isExtension": False,
        "externalUrl": fr_doc.get("htmlUrl"),
        "_source": "fr",
    }


def get_all_documents():
    """Merge regulations.gov docs + Federal Register docs, deduplicated."""
    regs_docs = get_documents()
    fr_raw = get_fr_documents()

    # Index regs docs by frDocNum so we can skip FR dupes
    regs_fr_nums = {d.get("frDocNum") for d in regs_docs if d.get("frDocNum")}

    merged = list(regs_docs)
    for doc_num, fr_doc in fr_raw.items():
        if doc_num not in regs_fr_nums:
            merged.append(normalize_fr_doc(fr_doc))

    return merged


# ── Shared helpers ────────────────────────────────────────────────────────────

def build_stats(documents, press_releases):
    substantive = [d for d in documents if d.get("documentType") in SUBSTANTIVE_TYPES]
    sigs = Counter(d.get("signalType") for d in substantive)
    cats = Counter(d.get("category") for d in substantive if d.get("category"))
    open_comment = sum(1 for d in documents if d.get("openForComment"))
    extensions = sum(1 for d in documents if d.get("isExtension"))
    maha_docs = sum(1 for d in substantive if d.get("mahaRelevant"))
    return {
        "total_docs": len(documents),
        "substantive": len(substantive),
        "press_count": len(press_releases),
        "open_comment": open_comment,
        "extensions": extensions,
        "rollbacks": sigs.get("rollback", 0),
        "protections": sigs.get("protection", 0),
        "delays": sigs.get("delay", 0),
        "maha_relevant": maha_docs,
        "categories": cats.most_common(),
    }


def all_items_timeline(documents, press_releases):
    """Merge documents + press releases into a date-sorted unified list."""
    items = []

    for doc in documents:
        if doc.get("documentType") not in SUBSTANTIVE_TYPES:
            continue
        items.append({
            "id": doc["documentId"],
            "itemType": "regulation",
            "title": doc.get("title") or doc["documentId"],
            "date": (doc.get("postedDate") or "")[:10],
            "signalType": doc.get("signalType", "other"),
            "category": doc.get("category"),
            "compounds": doc.get("compounds", []),
            "program": doc.get("program"),
            "documentType": doc.get("documentType"),
            "openForComment": doc.get("openForComment", False),
            "commentEndDate": (doc.get("commentEndDate") or "")[:10],
            "mahaRelevant": doc.get("mahaRelevant", False),
            "docketId": doc.get("docketId"),
            "era": doc.get("era", "unknown"),
            "_doc_id": doc["documentId"],
        })

    for pr in press_releases:
        items.append({
            "id": f"PR-{pr['pressId']}",
            "itemType": "press_release",
            "title": pr.get("title") or pr["pressId"],
            "date": pr.get("date", ""),
            "signalType": pr.get("signalType", "rhetoric"),
            "category": pr.get("category"),
            "compounds": pr.get("compounds", []),
            "program": pr.get("program"),
            "documentType": None,
            "openForComment": False,
            "commentEndDate": None,
            "mahaRelevant": pr.get("mahaRelevant", False),
            "externalUrl": pr.get("url"),
            "body": (pr.get("body") or "")[:300],
            "era": pr.get("era", "trump2"),
            "_press_id": pr["pressId"],
        })

    return sorted(items, key=lambda x: x.get("date") or "", reverse=True)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    documents = get_all_documents()
    press_releases = get_press_releases()
    status = get_status()
    all_events = sorted(get_events(), key=lambda e: e.get("date") or "", reverse=True)
    sources = get_sources()
    pfas_context = get_pfas_context()
    trump1 = get_trump1_context()
    stats = build_stats(documents, press_releases)

    recent = sorted(
        [d for d in documents if d.get("documentType") in SUBSTANTIVE_TYPES],
        key=lambda d: d.get("postedDate") or "",
        reverse=True,
    )[:20]

    open_comment = sorted(
        [d for d in documents if d.get("openForComment")],
        key=lambda d: d.get("commentEndDate") or "",
    )[:10]

    calendar = events_by_month(all_events)

    topics = _build_topics()
    combined_url = url_for("pfas_programs")
    # Stitch timeline description + url into each status card (same order)
    for i, rule in enumerate(status):
        if i < len(topics):
            rule["timelineDescription"] = topics[i].get("description", "")
            # Drinking water (0) and Superfund (1) go to combined page; reporting (2) stays solo
            rule["timelineUrl"] = combined_url if i < 2 else topics[i].get("url", "")

    return render_template("index.html",
                           stats=stats,
                           status=status,
                           calendar=calendar,
                           sources=sources,
                           pfas_context=pfas_context,
                           trump1=trump1,
                           recent=recent,
                           open_comment=open_comment)


@app.route("/signals/")
def signals():
    return redirect(url_for('explore') + "?view=timeline")


@app.route("/browse/")
def browse():
    documents = sorted(
        [d for d in get_all_documents() if d.get("documentType") in SUBSTANTIVE_TYPES],
        key=lambda d: d.get("postedDate") or "",
        reverse=True,
    )
    categories = sorted({d.get("category") for d in documents if d.get("category")})
    compounds = sorted({c for d in documents for c in d.get("compounds", [])})
    programs = sorted({d.get("program") for d in documents if d.get("program")})
    doc_types = sorted({d.get("documentType") for d in documents if d.get("documentType")})
    eras = [e for e in ["trump1", "biden", "trump2"] if any(d.get("era") == e for d in documents)]
    return render_template("browse.html",
                           documents=documents,
                           categories=categories,
                           compounds=compounds,
                           programs=programs,
                           doc_types=doc_types,
                           eras=eras)


@app.route("/docket/<docket_id>/")
def docket(docket_id):
    all_docs = sorted(
        [d for d in get_all_documents() if d.get("docketId") == docket_id],
        key=lambda d: d.get("postedDate") or "",
        reverse=True,
    )
    if not all_docs:
        abort(404)
    dockets = get_dockets()
    docket_data = dockets.get(docket_id) or {"docketId": docket_id, "title": docket_id}
    primary = [d for d in all_docs if _is_primary_doc(d)]
    supplementary = [d for d in all_docs if not _is_primary_doc(d)]
    return render_template("docket.html",
                           docket=docket_data,
                           primary=primary,
                           supplementary=supplementary)


@app.route("/document/<path:document_id>/")
def document(document_id):
    for doc in get_all_documents():
        if doc["documentId"] == document_id:
            dockets = get_dockets()
            docket_data = dockets.get(doc.get("docketId"))
            fr_docs = get_fr_documents()
            fr = {}
            # For native FR docs (FR-* prefix), the fr_doc IS the doc
            if document_id.startswith("FR-"):
                fr = fr_docs.get(document_id[3:], {})
            elif doc.get("frDocNum") and doc["frDocNum"] in fr_docs:
                fr = fr_docs[doc["frDocNum"]]
            # Full text
            full_text = (get_doc_text(document_id) or
                         get_doc_text(document_id.replace("/", "-").replace("FR-", ""), prefix="FR-"))
            sims = get_similarities()
            related = sims.get(document_id, [])
            return render_template("document.html",
                                   document=doc,
                                   docket=docket_data,
                                   fr=fr,
                                   full_text=full_text,
                                   related=related)
    abort(404)


@app.route("/press/<press_id>/")
def press(press_id):
    for pr in get_press_releases():
        if pr["pressId"] == press_id:
            sims = get_similarities()
            related = sims.get(f"PR-{press_id}", [])
            return render_template("press.html",
                                   press=pr,
                                   related=related)
    abort(404)


@app.route("/topic/<topic_id>/")
def topic(topic_id):
    alias_map = {
        "drinking-water-mcl": "drinking-water-limits",
        "cercla-designation": "hazardous-substance-designation",
        "tsca-reporting": "pfas-reporting",
    }
    slug = alias_map.get(topic_id, topic_id)
    return _render_timeline_page(slug)


ERA_RANK = {"trump1": 0, "biden": 1, "trump2": 2}
_MONTHS = {m: f"{i:02d}" for i, m in enumerate(
    ["january","february","march","april","may","june",
     "july","august","september","october","november","december"], start=1)}


def _timeline_sort_key(item):
    """Sort items by administration first, then chronologically within era.

    Keeps items in the era band they belong to even when calendar dates
    overlap across an admin transition (e.g. January 2021, January 2025).
    """
    era = item.get("era") or ""
    rank = ERA_RANK.get(era, 99)
    raw = (item.get("date") or "").strip()
    if len(raw) >= 7 and raw[4] == "-":
        ym = raw[:7]
    else:
        parts = raw.lower().split()
        if len(parts) == 2 and parts[1].isdigit() and parts[0] in _MONTHS:
            ym = f"{parts[1]}-{_MONTHS[parts[0]]}"
        elif len(parts) == 1 and parts[0].isdigit():
            ym = f"{parts[0]}-00"
        else:
            ym = ""
    return (rank, ym)


def _sort_timeline(items):
    return sorted(items, key=_timeline_sort_key)


def _render_timeline_page(slug):
    page = get_timeline_page(slug)
    if not page:
        abort(404)

    topic_data = {
        "title": page.get("title", ""),
        "subtitle": page.get("subtitle", ""),
        "eyebrow": page.get("eyebrow", ""),
        "description": page.get("description", ""),
        "currentStatus": page.get("currentStatus", ""),
        "statusClass": page.get("statusClass", ""),
    }

    return render_template("topic.html",
                           topic=topic_data,
                           timeline=_sort_timeline(page.get("timeline", [])),
                           docket_ids=page.get("docketIds", []))


@app.route("/timelines/<slug>/")
def timeline_page(slug):
    return _render_timeline_page(slug)


@app.route("/drinking-water-limits/")
def drinking_water_limits():
    return timeline_page("drinking-water-limits")


@app.route("/hazardous-substance-designation/")
def hazardous_substance_designation():
    return timeline_page("hazardous-substance-designation")


@app.route("/pfas-reporting/")
def pfas_reporting():
    # No dedicated timeline JSON for the reporting program — fold into the
    # combined federal-regulations view so url_for() callers still resolve and
    # frozen-flask can bake a redirect page.
    return redirect(url_for("pfas_programs"))


@app.route("/federal-regulations/")
def pfas_programs():
    page = get_timeline_page("pfas-programs")
    if not page:
        abort(404)
    topic_data = {
        "title":         page.get("title", ""),
        "eyebrow":       page.get("eyebrow", ""),
        "description":   page.get("description", ""),
        "currentStatus": None,
        "statusClass":   "",
    }
    programs = page.get("programs", [])
    return render_template("topic.html",
                           topic=topic_data,
                           timeline=_sort_timeline(page.get("timeline", [])),
                           docket_ids=[],
                           programs=programs)


@app.route("/court/<court_id>/")
def court(court_id):
    cases = get_court_actions()
    case = next((c for c in cases if c["courtId"] == court_id), None)
    if not case:
        abort(404)
    return render_template("court.html", case=case)


def _topic_event_label(ev):
    sig = ev.get("signalType", "other")
    if sig == "litigation":
        return "Court Action"
    if sig == "rhetoric":
        return "Statement"
    if sig in ("rollback", "protection", "delay"):
        return "Regulatory Action"
    return "Milestone"


@app.route("/state-legislation/")
def state_tracker():
    items = [i for i in get_state_bills() if i.get("state")]
    by_state = defaultdict(list)
    for item in items:
        by_state[item["state"]].append(item)

    states = []
    for state, bills in sorted(by_state.items(), key=lambda x: x[0]):
        adopted = sum(1 for b in bills if b.get("status") == "Adopted")
        introduced = sum(1 for b in bills if b.get("status") == "Introduced")
        abbr = STATE_ABBR.get(state, "")
        states.append({
            "name": state,
            "slug": _slugify(state),
            "abbr": abbr,
            "bills": sorted(
                bills,
                key=lambda b: (b.get("year") or "", b.get("bill") or ""),
                reverse=True,
            ),
            "counts": {
                "total": len(bills),
                "adopted": adopted,
                "introduced": introduced,
            },
        })

    state_lookup = {s["abbr"]: s for s in states if s.get("abbr")}
    state_summaries = [
        {
            "name": s["name"],
            "slug": s["slug"],
            "count": s["counts"]["total"],
        }
        for s in states
    ]

    surge_years = {"2025", "2026"}
    summary = {
        "states": len(states),
        "bills": len(items),
        "adopted": sum(s["counts"]["adopted"] for s in states),
        "introduced": sum(s["counts"]["introduced"] for s in states),
        "surge_bills": sum(1 for i in items if i.get("year") in surge_years),
    }

    all_issues = sorted({
        part.strip()
        for i in items
        for part in (i.get("issue") or "").split(",")
        if part.strip()
    })

    bills_by_year_map = defaultdict(list)
    for state_obj in states:
        for bill in state_obj["bills"]:
            yr = bill.get("year") or ""
            if yr:
                bills_by_year_map[yr].append(dict(bill, state_slug=state_obj["slug"]))
    # Merge the two active legislative-session years into one combined section
    # so the by-year view shows "2025–26" together by default; filter to a single
    # year still works because each row carries its own data-year attribute.
    CURRENT_SESSION_YEARS = ("2025", "2026")
    combined_bills = []
    for yr in CURRENT_SESSION_YEARS:
        combined_bills.extend(bills_by_year_map.pop(yr, []))
    combined_bills.sort(
        key=lambda b: ((b.get("year") or ""), b.get("state") or ""),
        reverse=False,
    )
    bills_by_year = []
    if combined_bills:
        bills_by_year.append({
            "year": "–".join(CURRENT_SESSION_YEARS),  # display label, e.g. "2025–2026"
            "year_id": "-".join(CURRENT_SESSION_YEARS),  # URL/DOM id, e.g. "2025-2026"
            "years": list(CURRENT_SESSION_YEARS),       # data-years attr for matching
            "bills": combined_bills,
        })
    for yr in sorted(bills_by_year_map.keys(), reverse=True):
        bills_by_year.append({
            "year": yr,
            "year_id": yr,
            "years": [yr],
            "bills": sorted(bills_by_year_map[yr], key=lambda b: b.get("state") or ""),
        })

    # Dropdown still lists every year individually so a user can drill down.
    all_years_sorted = sorted(
        {b.get("year") for s in states for b in s["bills"] if b.get("year")},
        reverse=True,
    )

    bills_by_issue_map = defaultdict(list)
    for state_obj in states:
        for bill in state_obj["bills"]:
            issue_parts = [p.strip() for p in (bill.get("issue") or "").split(",") if p.strip()]
            if not issue_parts:
                issue_parts = ["Other"]
            for issue in issue_parts:
                bills_by_issue_map[issue].append(dict(bill, state_slug=state_obj["slug"]))
    bills_by_issue = [
        {
            "issue": issue,
            "slug": _slugify(issue),
            "bills": sorted(bills_by_issue_map[issue],
                            key=lambda b: (b.get("state") or "", -(int(b.get("year") or 0)))),
        }
        for issue in sorted(bills_by_issue_map.keys())
    ]

    return render_template("state_tracker.html",
                           states=states,
                           summary=summary,
                           state_lookup=state_lookup,
                           state_summaries=state_summaries,
                           all_issues=all_issues,
                           bills_by_year=bills_by_year,
                           all_years_sorted=all_years_sorted,
                           bills_by_issue=bills_by_issue)


WATER_TYPE_LABELS = {
    "GW": "Groundwater",
    "SW": "Surface water",
    "MX": "Mixed source",
    "GU": "Groundwater under surface influence",
}


@lru_cache(maxsize=1)
def get_zcta_population():
    """ZCTA → ACS 5-year total population. Empty if fetch_census.py not run."""
    path = DATA_DIR / "zcta_population.json"
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def estimate_population_for_zips(zips):
    """Sum ACS population across a system's served ZIPs.

    Returns (estimated_population, matched_zip_count). Each ZIP that has no
    ZCTA match silently drops out — common for PO-box-only ZIPs.
    """
    pop_by_zip = get_zcta_population()
    if not pop_by_zip:
        return (0, 0)
    total = 0
    matched = 0
    for z in zips or ():
        p = pop_by_zip.get(str(z).strip().zfill(5))
        if p:
            total += p
            matched += 1
    return (total, matched)


@lru_cache(maxsize=1)
def get_pws_detail():
    """Per-PWS aggregates derived from the sample-level UCMR 5 dataset.

    Yields, per PWSID:
      water_type   — single source label (most common across facilities)
      n_facilities — distinct treatment facilities
      n_samples    — total analytical results
      n_detections — results above the minimum reporting level
      peak_by_contaminant — {contaminant -> {"ppt": float, "date": "YYYY-MM-DD"}}
    """
    path = DATA_DIR / "pws_data.csv"
    if not path.exists():
        return {}

    def _norm_date(raw):
        # Source uses M/D/YYYY; normalize to YYYY-MM-DD so it sorts cleanly.
        parts = (raw or "").split("/")
        if len(parts) != 3:
            return raw or ""
        try:
            m, d, y = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            return raw or ""
        return f"{y:04d}-{m:02d}-{d:02d}"

    out = {}
    with open(path, newline="", encoding="latin-1") as f:
        for r in csv.DictReader(f):
            pwsid = (r.get("pwsid") or "").strip()
            if not pwsid:
                continue
            entry = out.setdefault(pwsid, {
                "_facility_ids": set(),
                "_water_types": Counter(),
                # Track physical samples (one per sample_id) rather than analytical
                # results (one per sample × contaminant). Each physical sample is
                # analyzed for all five PFAS, so the result-row count is ~5× the
                # actual number of water samples drawn.
                "_sample_ids": set(),
                "_detected_sample_ids": set(),
                "_overlimit_sample_ids": set(),
                "n_result_rows": 0,        # legacy: analytical-result rows
                "n_detection_rows": 0,     # legacy: result rows above MRL
                "peak_by_contaminant": {},
                "last_date_by_contaminant": {},
            })
            fid = (r.get("facility_id") or "").strip()
            if fid and fid != "NA":
                entry["_facility_ids"].add(fid)
            wt = (r.get("facility_water_type") or "").strip()
            if wt and wt != "NA":
                entry["_water_types"][wt] += 1
            entry["n_result_rows"] += 1
            sid = (r.get("sample_id") or "").strip()
            if sid:
                entry["_sample_ids"].add(sid)

            contam_name = (r.get("contaminant") or "").strip()
            sample_date = _norm_date(r.get("collection_date") or "")
            if contam_name and sample_date:
                prev_last = entry["last_date_by_contaminant"].get(contam_name)
                if prev_last is None or sample_date > prev_last:
                    entry["last_date_by_contaminant"][contam_name] = sample_date

            if (r.get("analytical_results_sign") or "").strip() == "=":
                entry["n_detection_rows"] += 1
                if sid:
                    entry["_detected_sample_ids"].add(sid)
                try:
                    val_ugl = float(r.get("analytical_result_value") or "")
                except ValueError:
                    val_ugl = None
                if val_ugl is not None:
                    contam = (r.get("contaminant") or "").strip()
                    ppt = val_ugl * 1000.0
                    date = _norm_date(r.get("collection_date") or "")
                    # Track per-sample exceedances against each contaminant's MCL.
                    limit_ppt = PFAS_MCL_BY_CONTAMINANT.get(contam)
                    if limit_ppt and ppt > limit_ppt and sid:
                        entry["_overlimit_sample_ids"].add(sid)
                    prev = entry["peak_by_contaminant"].get(contam)
                    if prev is None or ppt > prev["ppt"]:
                        entry["peak_by_contaminant"][contam] = {
                            "ppt": ppt,
                            "date": date,
                        }

    # Collapse derived counters into a stable shape.
    final = {}
    for pwsid, e in out.items():
        types = e.pop("_water_types")
        fids = e.pop("_facility_ids")
        sids = e.pop("_sample_ids")
        det_sids = e.pop("_detected_sample_ids")
        over_sids = e.pop("_overlimit_sample_ids")
        primary = types.most_common(1)[0][0] if types else ""
        e["water_type"] = primary
        e["water_type_label"] = WATER_TYPE_LABELS.get(primary, primary)
        e["n_facilities"] = len(fids)
        # Physical-sample counts — what readers expect when they hear "samples".
        e["n_samples"] = len(sids)
        e["n_detections"] = len(det_sids)
        e["n_samples_over_limit"] = len(over_sids)
        final[pwsid] = e
    return final


@lru_cache(maxsize=1)
def get_pws_index():
    """Per-PWS rollup (collapses each system's 5 contaminant rows into one entry)."""
    detail = get_pws_detail()
    index = {}
    for r in get_pws_rows():
        e = index.setdefault(r["pwsid"], {
            "id": r["pwsid"],
            "name": r["name"],
            "state": r["state"],
            "exceeded": False,
            "max_times": 0.0,
            "max_contaminant": "",
            "max_result": 0.0,
            "samples": 0,
            "first_sampled": r["first_collection_date"],
            "last_sampled": r["last_collection_date"],
            "zips": [],
            # contaminant -> {"times": float, "ppt": float, "date": "YYYY-MM-DD"}
            "exceedances": {},
        })
        e["exceeded"] = e["exceeded"] or r["over_limit"]
        if r["max_result_times_over_limit"] > e["max_times"]:
            e["max_times"] = r["max_result_times_over_limit"]
            e["max_contaminant"] = r["contaminant"]
            e["max_result"] = r["max_result"]
        e["samples"] = max(e["samples"], r["n_samples"])
        if r["last_collection_date"] > e["last_sampled"]:
            e["last_sampled"] = r["last_collection_date"]
        if r["first_collection_date"] and (
            not e["first_sampled"] or r["first_collection_date"] < e["first_sampled"]
        ):
            e["first_sampled"] = r["first_collection_date"]
        if r["over_limit"]:
            prev = e["exceedances"].get(r["contaminant"])
            if prev is None or r["max_result_times_over_limit"] > prev["times"]:
                e["exceedances"][r["contaminant"]] = {
                    "times": r["max_result_times_over_limit"],
                    "ppt": r["max_result"] * 1000.0,
                    "date": "",
                }
        if r["zips"] and not e["zips"]:
            e["zips"] = r["zips"]

    # Enrich each PWS with sample-level detail (water source, detection counts,
    # peak-sample dates per contaminant).
    for pwsid, entry in index.items():
        d = detail.get(pwsid)
        if not d:
            entry["water_type"] = ""
            entry["water_type_label"] = ""
            entry["n_facilities"] = 0
            entry["n_total_samples"] = 0
            entry["n_detections"] = 0
            entry["n_samples_over_limit"] = 0
            continue
        entry["water_type"] = d["water_type"]
        entry["water_type_label"] = d["water_type_label"]
        entry["n_facilities"] = d["n_facilities"]
        # Physical-sample counts (each water sample is analyzed for 5 PFAS, so
        # the raw result-row counts are 5× higher).
        entry["n_total_samples"] = d["n_samples"]
        entry["n_detections"] = d["n_detections"]
        entry["n_samples_over_limit"] = d["n_samples_over_limit"]
        # All five PFAS, with peak detection (if any) and the system's status
        # against each compound's EPA standard. Drives the per-row tooltip.
        per_contam = []
        for contam in ("PFOA", "PFOS", "PFHxS", "PFNA", "HFPO-DA"):
            peak = d["peak_by_contaminant"].get(contam)
            limit = PFAS_MCL_BY_CONTAMINANT.get(contam, 0)
            if peak:
                ppt = peak["ppt"]
                over = limit and ppt > limit
                per_contam.append({
                    "contaminant": contam,
                    "ppt": ppt,
                    "limit": limit,
                    "times": (ppt / limit) if (limit and ppt) else 0,
                    "date": peak["date"],
                    "detected": True,
                    "over_limit": bool(over),
                })
            else:
                per_contam.append({
                    "contaminant": contam,
                    "ppt": 0,
                    "limit": limit,
                    "times": 0,
                    "date": d.get("last_date_by_contaminant", {}).get(contam, ""),
                    "detected": False,
                    "over_limit": False,
                })
        entry["per_contaminant"] = per_contam
        # Attach the peak-sample date for each exceeding contaminant.
        for contam, ex in entry["exceedances"].items():
            peak = d["peak_by_contaminant"].get(contam)
            if peak:
                ex["date"] = peak["date"]
                # Prefer the sample-level peak in ppt if we have it (more precise).
                if peak["ppt"] > ex["ppt"]:
                    ex["ppt"] = peak["ppt"]

    # Precompute a sorted list view so templates can iterate without doing
    # nested attribute sorts on (contaminant, dict) tuples.
    for entry in index.values():
        entry["exceedances_sorted"] = sorted(
            entry["exceedances"].items(),
            key=lambda kv: kv[1]["times"],
            reverse=True,
        )
        entry["n_exceedances"] = len(entry["exceedances"])
        pop, matched = estimate_population_for_zips(entry["zips"])
        entry["served_population"] = pop
        entry["served_zip_match_count"] = matched

    return list(index.values())


def _attach_state_names(water):
    abbr_to_name = {v: k for k, v in STATE_ABBR.items()}
    for st in water["states_ranked"]:
        name = abbr_to_name.get(st["state"], st["state"])
        st["name"] = name
        st["slug"] = _slugify(name)
    return water


@app.route("/water-testing/")
def water_testing():
    water = _attach_state_names(get_water_summary())
    return render_template("water_testing.html", water=water)


@app.route("/data/pws-search.json")
def pws_search_data():
    """Search-index JSON, fetched on demand by the water-testing page.

    Only the fields the client-side search and results use, so the payload
    stays small.
    """
    slim = [
        {
            "id": p["id"],
            "name": p["name"],
            "state": p["state"],
            "zips": p["zips"],
            "n_exceedances": p["n_exceedances"],
            "served_population": p.get("served_population") or 0,
            "per_contaminant": p.get("per_contaminant") or [],
        }
        for p in get_pws_index()
    ]
    return jsonify(slim)


@app.route("/water-testing/<state_slug>/")
def water_testing_state(state_slug):
    water = _attach_state_names(get_water_summary())
    target = next((s for s in water["states_ranked"] if s["slug"] == state_slug), None)
    if not target:
        abort(404)

    systems = [p for p in get_pws_index() if p["state"] == target["state"]]
    systems.sort(key=lambda s: (-s["max_times"], s["name"]))

    return render_template(
        "water_state.html",
        water=water,
        state=target,
        systems=systems,
    )


@app.route("/search/")
def search():
    return render_template("search.html")


def _build_topics():
    """Return list of topic metadata dicts for the three main PFAS programs."""
    TOPIC_SLUGS = [
        ("drinking-water-limits",          url_for("drinking_water_limits"),           "Drinking Water"),
        ("hazardous-substance-designation", url_for("hazardous_substance_designation"), "Superfund"),
        ("pfas-reporting",                 url_for("pfas_reporting"),                  "PFAS Reporting"),
    ]
    topics = []
    for slug, route_url, program in TOPIC_SLUGS:
        page = get_timeline_page(slug)
        if not page:
            continue
        topics.append({
            "title":         page.get("title", ""),
            "eyebrow":       page.get("eyebrow", ""),
            "description":   page.get("description", ""),
            "currentStatus": page.get("currentStatus", ""),
            "statusClass":   page.get("statusClass", ""),
            "url":           route_url,
        })
    return topics


@app.route("/explore/")
def explore():
    from flask import redirect
    return redirect(url_for("index"))


@app.route("/pagefind/<path:filename>")
def pagefind(filename):
    return send_from_directory("build/pagefind", filename)


if __name__ == "__main__":
    # macOS reserves port 5000 for AirPlay Receiver; honor PORT env var
    # if set, otherwise default to 5050 to avoid the conflict.
    import os
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5050")),
            debug=True, use_reloader=True)
