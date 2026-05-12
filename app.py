import json
from collections import Counter, defaultdict
from pathlib import Path
from flask import Flask, render_template, abort, send_from_directory

app = Flask(__name__)
app.config['FREEZER_RELATIVE_URLS'] = True

DATA_DIR = Path("static/data")

SUBSTANTIVE_TYPES = {"Rule", "Proposed Rule", "Notice"}


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
            "era": "trump2",  # press releases are Trump 2 era by definition (post 2025-01-20)
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
    documents = get_all_documents()
    press_releases = get_press_releases()
    items = all_items_timeline(documents, press_releases)
    categories = sorted({i.get("category") for i in items if i.get("category")})
    compounds = sorted({c for i in items for c in i.get("compounds", [])})
    eras = [e for e in ["trump1", "biden", "trump2"] if any(i.get("era") == e for i in items)]
    return render_template("signals.html",
                           items=items,
                           categories=categories,
                           compounds=compounds,
                           eras=eras)


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
    documents = sorted(
        [d for d in get_all_documents() if d.get("docketId") == docket_id],
        key=lambda d: d.get("postedDate") or "",
        reverse=True,
    )
    if not documents:
        abort(404)
    dockets = get_dockets()
    docket_data = dockets.get(docket_id) or {"docketId": docket_id, "title": docket_id}
    return render_template("docket.html",
                           docket=docket_data,
                           documents=documents)


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


@app.route("/search/")
def search():
    return render_template("search.html")


@app.route("/pagefind/<path:filename>")
def pagefind(filename):
    return send_from_directory("build/pagefind", filename)


if __name__ == "__main__":
    app.run(debug=True, use_reloader=True)
