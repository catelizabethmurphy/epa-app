import json
import subprocess
from pathlib import Path
from flask_frozen import Freezer
from app import app, get_documents, get_dockets

freezer = Freezer(app)


@freezer.register_generator
def docket():
    dockets = get_dockets()
    for docket_id in dockets:
        yield {"docket_id": docket_id}


@freezer.register_generator
def document():
    for doc in get_documents():
        yield {"document_id": doc["documentId"]}


@freezer.register_generator
def search():
    yield {}


@freezer.register_generator
def calendar():
    yield {}


if __name__ == "__main__":
    freezer.freeze()
    print("Running Pagefind indexer...")
    subprocess.run(
        ["npx", "--yes", "pagefind", "--site", "build/", "--output-path", "build/pagefind"],
        check=True,
    )
    print("Pagefind index built.")
