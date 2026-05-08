import json
from collections import Counter, defaultdict
from pathlib import Path
from flask import Flask, render_template, abort, send_from_directory

app = Flask(__name__)
app.config['FREEZER_RELATIVE_URLS'] = True

DATA_DIR = Path("static/data")


def get_documents():
    with open(DATA_DIR / "documents.json") as f:
        return json.load(f)


def get_dockets():
    with open(DATA_DIR / "dockets.json") as f:
        return json.load(f)


def get_fr_text():
    p = DATA_DIR / "fr_text.json"
    if not p.exists():
        return {}
    with open(p) as f:
        return json.load(f)


def get_doc_text(document_id):
    """Return full plain text for a document, or None."""
    p = DATA_DIR / "text" / f"{document_id}.txt"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None


SUBSTANTIVE_TYPES = {"Rule", "Proposed Rule", "Notice"}


def build_stats(documents):
    cats = Counter(d.get("category") for d in documents if d.get("category"))
    states = Counter(d.get("state") for d in documents if d.get("state"))
    types = Counter(d.get("documentType") for d in documents if d.get("documentType"))
    open_comment = sum(1 for d in documents if d.get("openForComment"))
    extensions = sum(1 for d in documents if d.get("isExtension"))
    substantive = sum(1 for d in documents if d.get("documentType") in SUBSTANTIVE_TYPES)
    return {
        "total": len(documents),
        "substantive": substantive,
        "supporting": len(documents) - substantive,
        "open_comment": open_comment,
        "extensions": extensions,
        "categories": cats.most_common(),
        "states": states.most_common(20),
        "types": types.most_common(),
    }


@app.route("/")
def index():
    documents = get_documents()
    dockets = get_dockets()
    substantive = [d for d in documents if d.get("documentType") in ("Rule", "Proposed Rule", "Notice")]
    recent = sorted(substantive, key=lambda d: d.get("postedDate") or "", reverse=True)[:25]
    stats = build_stats(documents)
    return render_template("index.html", documents=documents, dockets=dockets,
                           recent=recent, stats=stats)


@app.route("/browse/")
def browse():
    documents = get_documents()
    documents = sorted(documents, key=lambda d: d.get("postedDate") or "", reverse=True)
    categories = sorted({d.get("category") for d in documents if d.get("category")})
    # states: collect all entries from the states list (multi-state support)
    all_states = set()
    for d in documents:
        for s in d.get("states") or []:
            all_states.add(s)
    states = sorted(all_states)
    regions = sorted({d.get("epaRegion") for d in documents if d.get("epaRegion")})
    doc_types = sorted({d.get("documentType") for d in documents if d.get("documentType")})
    return render_template("browse.html", documents=documents,
                           categories=categories, states=states,
                           regions=regions, doc_types=doc_types)


@app.route("/docket/<docket_id>/")
def docket(docket_id):
    dockets = get_dockets()
    if docket_id not in dockets:
        abort(404)
    documents = [d for d in get_documents() if d.get("docketId") == docket_id]
    documents = sorted(documents, key=lambda d: d.get("postedDate") or "", reverse=True)
    return render_template("docket.html", docket=dockets[docket_id], documents=documents)


@app.route("/search/")
def search():
    return render_template("search.html")


@app.route("/calendar/")
def calendar():
    documents = get_documents()

    by_date = defaultdict(list)
    deadlines = defaultdict(list)

    for doc in documents:
        if doc.get("postedDate"):
            day = doc["postedDate"][:10]
            by_date[day].append({
                "id": doc["documentId"],
                "title": (doc.get("title") or doc["documentId"])[:100],
                "type": doc.get("documentType") or "",
                "category": doc.get("category") or "",
            })
        if doc.get("commentEndDate"):
            day = doc["commentEndDate"][:10]
            deadlines[day].append({
                "id": doc["documentId"],
                "title": (doc.get("title") or doc["documentId"])[:100],
            })

    all_days = list(by_date) + list(deadlines)
    min_date = min(all_days) if all_days else "2025-01-20"
    max_date = max(all_days) if all_days else "2025-01-20"

    SUBSTANTIVE = {"Rule", "Proposed Rule", "Notice"}

    calendar_data = {
        "by_date": {
            d: {"count": len([x for x in v if x["type"] in SUBSTANTIVE]),
                "docs": [x for x in v if x["type"] in SUBSTANTIVE]}
            for d, v in sorted(by_date.items())
            if any(x["type"] in SUBSTANTIVE for x in v)
        },
        "deadlines": {d: v for d, v in sorted(deadlines.items())},
        "min_date": min_date,
        "max_date": max_date,
    }
    return render_template("calendar.html", calendar_data=calendar_data)


@app.route("/pagefind/<path:filename>")
def pagefind(filename):
    """Serve pagefind assets from build/ in dev mode. Frozen site uses build/pagefind/ directly."""
    return send_from_directory("build/pagefind", filename)


@app.route("/document/<path:document_id>/")
def document(document_id):
    for doc in get_documents():
        if doc["documentId"] == document_id:
            dockets = get_dockets()
            docket_data = dockets.get(doc.get("docketId"))
            fr = get_fr_text().get(document_id, {})
            full_text = get_doc_text(document_id)
            return render_template("document.html", document=doc,
                                   docket=docket_data, fr=fr, full_text=full_text)
    abort(404)


if __name__ == "__main__":
    app.run(debug=True, use_reloader=True)
