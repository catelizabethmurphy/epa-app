import json
import subprocess
from pathlib import Path
from flask_frozen import Freezer
from app import (
    app,
    get_all_documents,
    get_press_releases,
    get_court_actions,
    get_water_summary,
    _slugify,
    STATE_ABBR,
)

freezer = Freezer(app)


@freezer.register_generator
def docket():
    seen = set()
    for doc in get_all_documents():
        did = doc.get("docketId")
        if did and did not in seen:
            seen.add(did)
            yield {"docket_id": did}


@freezer.register_generator
def document():
    for doc in get_all_documents():
        yield {"document_id": doc["documentId"]}


@freezer.register_generator
def press():
    for pr in get_press_releases():
        yield {"press_id": pr["pressId"]}


@freezer.register_generator
def topic():
    timelines_dir = Path("static/data/timelines")
    alias_slugs = ["drinking-water-mcl", "cercla-designation", "tsca-reporting"]
    if timelines_dir.exists():
        for path in timelines_dir.glob("*.json"):
            yield {"topic_id": path.stem}
    for slug in alias_slugs:
        yield {"topic_id": slug}


@freezer.register_generator
def court():
    for c in get_court_actions():
        yield {"court_id": c["courtId"]}


@freezer.register_generator
def signals():
    yield {}


@freezer.register_generator
def explore():
    yield {}


@freezer.register_generator
def what_are_pfas():
    yield {}


@freezer.register_generator
def search():
    yield {}


@freezer.register_generator
def glossary():
    yield {}


@freezer.register_generator
def state_tracker():
    yield {}


@freezer.register_generator
def timeline_page():
    timelines_dir = Path("static/data/timelines")
    if timelines_dir.exists():
        for path in timelines_dir.glob("*.json"):
            yield {"slug": path.stem}


@freezer.register_generator
def drinking_water_limits():
    yield {}


@freezer.register_generator
def hazardous_substance_designation():
    yield {}


@freezer.register_generator
def pfas_reporting():
    yield {}


@freezer.register_generator
def water_testing():
    yield {}


@freezer.register_generator
def pws_search_data():
    yield {}


@freezer.register_generator
def water_testing_state():
    abbr_to_name = {v: k for k, v in STATE_ABBR.items()}
    for st in get_water_summary()["states_ranked"]:
        name = abbr_to_name.get(st["state"], st["state"])
        slug = _slugify(name)
        if slug:
            yield {"state_slug": slug}


if __name__ == "__main__":
    freezer.freeze()
    print("Running Pagefind indexer…")
    subprocess.run(
        ["npx", "--yes", "pagefind", "--site", "build/", "--output-path", "build/pagefind"],
        check=True,
    )
    print("Pagefind index built.")
