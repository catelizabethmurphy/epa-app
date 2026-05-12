import json
import subprocess
from pathlib import Path
from flask_frozen import Freezer
from app import app, get_all_documents, get_press_releases

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
def signals():
    yield {}


@freezer.register_generator
def search():
    yield {}


if __name__ == "__main__":
    freezer.freeze()
    print("Running Pagefind indexer…")
    subprocess.run(
        ["npx", "--yes", "pagefind", "--site", "build/", "--output-path", "build/pagefind"],
        check=True,
    )
    print("Pagefind index built.")
