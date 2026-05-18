from pathlib import Path
from flask_frozen import Freezer
from app import (
    app,
    get_archives,
    get_water_summary,
    _slugify,
    STATE_ABBR,
)

freezer = Freezer(app)


@freezer.register_generator
def archive():
    for slug in get_archives():
        yield {"slug": slug}


@freezer.register_generator
def state_tracker():
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


@freezer.register_generator
def pfas_programs():
    yield {}


if __name__ == "__main__":
    freezer.freeze()
