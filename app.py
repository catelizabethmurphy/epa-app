import csv
import html
import json
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from flask import Flask, render_template, abort, send_from_directory, redirect, url_for

app = Flask(__name__)
app.config['FREEZER_RELATIVE_URLS'] = True

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


def get_agenda():
    return _load("agenda.json", [])


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
    agenda = [a for a in get_agenda() if a.get("stage") not in ("completed", "long_term")]
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

    return render_template("index.html",
                           stats=stats,
                           status=status,
                           calendar=calendar,
                           sources=sources,
                           agenda=agenda[:10],
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
                           timeline=page.get("timeline", []),
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
    return timeline_page("pfas-reporting")


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


@app.route("/glossary/")
def glossary():
    return render_template("glossary.html")


@app.route("/states/")
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

    summary = {
        "states": len(states),
        "bills": len(items),
        "adopted": sum(s["counts"]["adopted"] for s in states),
        "introduced": sum(s["counts"]["introduced"] for s in states),
    }

    return render_template("state_tracker.html",
                           states=states,
                           summary=summary,
                           state_lookup=state_lookup,
                           state_summaries=state_summaries)


@app.route("/search/")
def search():
    return render_template("search.html")


@app.route("/explore/")
def explore():
    documents = get_all_documents()
    press_releases = get_press_releases()
    court_cases = get_court_actions()

    items = []

    for doc in documents:
        items.append({
            "itemType": "regulation",
            "typeLabel": doc.get("documentType") or "Document",
            "id": doc["documentId"],
            "title": doc.get("title") or doc["documentId"],
            "date": (doc.get("postedDate") or "")[:10],
            "signalType": doc.get("signalType") or "other",
            "category": doc.get("category") or "",
            "compounds": doc.get("compounds", []),
            "program": doc.get("program") or "",
            "era": doc.get("era") or "unknown",
            "mahaRelevant": doc.get("mahaRelevant", False),
            "isPrimary": doc.get("isPrimary", True),
        })

    for pr in press_releases:
        items.append({
            "itemType": "press",
            "typeLabel": "Statement",
            "id": pr["pressId"],
            "title": pr.get("title") or pr["pressId"],
            "date": pr.get("date", ""),
            "signalType": pr.get("signalType") or "rhetoric",
            "category": pr.get("category") or "",
            "compounds": pr.get("compounds", []),
            "program": pr.get("program") or "",
            "era": pr.get("era") or "trump2",
            "mahaRelevant": pr.get("mahaRelevant", False),
            "isPrimary": True,
        })

    for case in court_cases:
        items.append({
            "itemType": "court",
            "typeLabel": "Court Case",
            "id": case["courtId"],
            "title": case.get("caseTitle") or case["courtId"],
            "date": case.get("filed", ""),
            "signalType": "litigation",
            "category": "Litigation",
            "compounds": ["PFAS"],
            "program": "",
            "era": "biden" if (case.get("filed") or "") < "2025-01-20" else "trump2",
            "mahaRelevant": False,
            "isPrimary": True,
        })

    items.sort(key=lambda x: x.get("date") or "", reverse=True)
    categories = sorted({i["category"] for i in items if i.get("category")})
    compounds = sorted({c for i in items for c in i.get("compounds", [])})
    eras = [e for e in ["trump1", "biden", "trump2"] if any(i.get("era") == e for i in items)]

    return render_template("explore.html",
                           items=items,
                           categories=categories,
                           compounds=compounds,
                           eras=eras)


@app.route("/what-are-pfas/")
def what_are_pfas():
    return render_template("what_are_pfas.html")


@app.route("/pagefind/<path:filename>")
def pagefind(filename):
    return send_from_directory("build/pagefind", filename)


if __name__ == "__main__":
    app.run(debug=True, use_reloader=True)
