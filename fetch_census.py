"""Fetch ZCTA population from the Census ACS 5-year API.

Run once (or whenever you want to refresh population data):

    CENSUS_API_KEY=xxx python3 fetch_census.py

Writes static/data/zcta_population.json — a small dict mapping each 5-digit
ZIP Code Tabulation Area to its ACS 2022 total population.

Get a free API key at https://api.census.gov/data/key_signup.html — keys are
issued instantly and have generous rate limits.
"""
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

OUT_PATH = Path("static/data/zcta_population.json")
API = "https://api.census.gov/data/2022/acs/acs5"

def main():
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        sys.exit(
            "CENSUS_API_KEY not set. Sign up free at "
            "https://api.census.gov/data/key_signup.html "
            "and run: CENSUS_API_KEY=xxx python3 fetch_census.py"
        )

    url = (
        f"{API}?get=B01003_001E"
        "&for=zip%20code%20tabulation%20area:*"
        f"&key={key}"
    )
    print(f"Requesting {API} (ZCTA population)…")
    req = Request(url, headers={"User-Agent": "epa-app/1.0"})
    with urlopen(req, timeout=120) as resp:
        raw = json.load(resp)

    # First row is the header: ["B01003_001E", "zip code tabulation area"]
    header = raw[0]
    pop_i = header.index("B01003_001E")
    zip_i = header.index("zip code tabulation area")

    population = {}
    for row in raw[1:]:
        zcta = row[zip_i]
        try:
            pop = int(row[pop_i])
        except (TypeError, ValueError):
            continue
        if pop > 0:
            population[zcta] = pop

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(population, f, separators=(",", ":"))
    print(f"Wrote {len(population):,} ZCTAs → {OUT_PATH}")


if __name__ == "__main__":
    main()
