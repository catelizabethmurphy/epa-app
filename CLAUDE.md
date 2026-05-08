# epa-app

A news application tracking EPA air quality regulatory actions under the Trump administration since January 20, 2025, focused on State Implementation Plans (SIPs) and Tribal Implementation Plans (TIPs).

## Editorial Context

On March 12, 2025, the EPA announced what it called the "biggest deregulatory action in U.S. history." The press release named SIPs and TIPs explicitly, describing:

- A claimed "backlog" of SIP/TIP submissions left unresolved by the prior administration
- Repeal of the **Good Neighbor Plan** (cross-state air pollution rule)
- Reconsideration of **PM 2.5 NAAQS** (National Ambient Air Quality Standards for particulate matter)
- Restructuring of the **Regional Haze Program** (interstate visibility rule)
- Reconsideration of **MATS** (Mercury and Air Toxics Standards for coal plants)
- Reconsideration of **Exceptional Events** rulemaking (prescribed fire exemptions in SIPs/TIPs)

This app pulls the actual regulations.gov docket record to show what was filed — and what wasn't — since January 20.

## Architecture

Flask + Frozen-Flask (same pattern as ghostapps/first-news-app-umd).

`fetch.py` hits the regulations.gov API and writes three JSON files to `static/data/`. The Flask app reads those files at build time. Frozen-Flask bakes everything to static HTML in `build/` for deployment to GitHub Pages.

```
epa-app/
├── app.py                    # Flask routes
├── fetch.py                  # Data pipeline (regulations.gov API)
├── fetch_text.py             # Federal Register text enrichment
├── enrich.py                 # Category, isExtension, comment counts
├── freeze.py                 # Frozen-Flask config
├── requirements.txt
├── Makefile
├── .env                      # REGS_API_KEY (gitignored)
├── .env.example
├── static/
│   ├── css/style.css
│   └── data/
│       ├── documents.json    # All fetched documents (with category, isExtension)
│       ├── dockets.json      # Docket metadata keyed by docketId
│       ├── by_state.json     # Document IDs grouped by parsed state
│       ├── fr_text.json      # Federal Register metadata keyed by documentId
│       └── text/             # Plain-text files, one per documentId
└── templates/
    ├── base.html
    ├── index.html            # Homepage: stats + recent activity
    ├── browse.html           # Full list with JS filtering (category, state, type)
    ├── docket.html           # One docket + its documents
    └── document.html         # Single document detail + FR links + abstract
```

## Data Source

**regulations.gov API** — the federal government's public rulemaking portal.

- API docs: https://open.gsa.gov/api/regulationsgov/
- Register for a free key: https://api.data.gov/signup/
- Auth: `X-Api-Key` request header
- Rate limit: 1,000 requests/hour; `fetch.py` sleeps 0.5s between calls

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set REGS_API_KEY
```

## Running the Data Pipeline

```bash
python3 fetch.py        # Step 1: fetch document/docket metadata from regulations.gov
python3 fetch_text.py   # Step 2: fetch full text for rules and proposed rules
python3 enrich.py       # Step 3: add category, isExtension fields
# or together:
make fetch-all
```

**fetch.py** — Fetches all matching EPA documents and dockets from regulations.gov, writes three JSON files to `static/data/`. Takes 5–10 minutes. Safe to re-run. Respects `Retry-After` header on 429.

**fetch_text.py** — Enriches rules/proposed rules with Federal Register data. Saves `static/data/fr_text.json` (abstracts + URLs) and downloads full plain text to `static/data/text/*.txt`. Takes 5–10 minutes. Use `--skip-xml` to fetch metadata only without downloading full text.

**enrich.py** — Post-processes `documents.json` in place, adding:
- `category` — primary program category, assigned by priority (first match wins)
- `isExtension` — boolean, true when title indicates a deadline/comment-period extension
- Use `--comments` flag to also fetch real comment counts from the regulations.gov detail endpoint

Results (as of May 2026): 1,106 documents, 417+ rules/proposed rules, 403 full-text files.

### What fetch.py queries

15 passes through `/v4/documents` with `agencyId=EPA` and `postedDate >= 2025-01-20`:

| Search term | What it captures |
|-------------|-----------------|
| `state implementation plan` | Core SIP filings and EPA actions on them |
| `tribal implementation plan` | TIP filings from tribal nations |
| `approvals and promulgations` | Older-format SIP titles |
| `good neighbor` | Good Neighbor Plan repeal docket |
| `regional haze` | Regional Haze Program restructuring |
| `exceptional events` | Prescribed fire exemptions in SIPs/TIPs |
| `national ambient air quality` | All NAAQS actions |
| `particulate matter` | PM2.5 standard reconsideration |
| `ozone` | Ozone NAAQS and nonattainment |
| `mercury air toxics` | MATS (coal plant mercury rules) |
| `hazardous air pollutants` | NESHAPs (broad industrial standards) |
| `new source performance` | NSPS (power plants, oil & gas) |
| `greenhouse gas` | Vehicle GHG standards |
| `PM2.5` | Fine particulate matter (abbreviation form) |
| `consent decree` | CAA citizen suit settlements (OGC dockets) |

Documents matching multiple terms are tagged with all matching terms in the `searchTerms` field. Deduplication is by `documentId`.

### Docket filtering

Documents in excluded dockets are silently skipped during fetch:

- **Included**: Dockets containing `-OAR-` (Office of Air and Radiation) or `-OGC-` (Office of General Counsel / consent decrees). Also `EPA_FRDOC_0001` (always included).
- **Excluded**: Water (`-OW-`), pesticides (`-OPP-`), land management (`-OLEM-`), and all other non-air offices.
- All included dockets are allowed regardless of age — the `postedDate >= 2025-01-20` filter is the activity gate. Old NESHAP/NSPS dockets with recent documents are intentional.

### Document categories (enrich.py)

Assigned in priority order (first match wins):

| Category | Trigger |
|----------|---------|
| Consent Decree | searchTerm "consent decree" OR `-OGC-` in docketId |
| Good Neighbor | searchTerm "good neighbor" |
| Regional Haze | searchTerm "regional haze" |
| Exceptional Events | searchTerm "exceptional events" |
| MATS | searchTerm "mercury air toxics" |
| GHG / Climate | searchTerm "greenhouse gas" |
| NESHAP | searchTerm "hazardous air pollutants" |
| NSPS | searchTerm "new source performance" |
| NAAQS / PM2.5 | searchTerms "national ambient air quality", "particulate matter", or "PM2.5" |
| Ozone | searchTerm "ozone" |
| SIP / TIP | searchTerms "state implementation plan", "tribal implementation plan", or "approvals and promulgations" |
| Other | catch-all |

### State parsing

State names are parsed from document titles by string matching against all 50 states + D.C. Documents with "tribal," "tribe," or "nation" in the title are tagged `"Tribal"` (checked before state matching). National rulemakings without a state in the title will have `state: null`.

## Running Locally

```bash
python app.py
```

Visit http://localhost:5000

## Building the Static Site

```bash
python freeze.py
```

Output goes to `build/`. Test it with `cd build && python -m http.server`.

## Deployment

```bash
make deploy
```

Pushes `build/` to the `gh-pages` branch. Site publishes at `https://ghostapps.github.io/epa-app/`.

## Makefile Targets

```
make fetch     # Run data pipeline (writes static/data/*.json)
make run       # Start Flask dev server
make freeze    # Build static site to build/
make deploy    # Push build/ to gh-pages
```

## Data Files

| File | Source | Contents |
|------|--------|----------|
| `static/data/documents.json` | regulations.gov | All 503 documents (metadata) |
| `static/data/dockets.json` | regulations.gov | 199 docket records, keyed by docketId |
| `static/data/by_state.json` | parsed from titles | Document IDs grouped by state |
| `static/data/fr_text.json` | Federal Register API | 312 rules/proposed rules: abstract, html_url, pdf_url, full_text_xml_url, action |
| `static/data/text/*.txt` | Federal Register XML | 306 full plain-text files, one per documentId |

## Key Data Fields

**Document** (each item in `documents.json`):

| Field | Description |
|-------|-------------|
| `documentId` | e.g. `EPA-HQ-OAR-2024-0333-0001` |
| `docketId` | Parent docket, e.g. `EPA-HQ-OAR-2024-0333` |
| `title` | Full document title |
| `documentType` | `Proposed Rule`, `Rule`, `Supporting & Related Material`, `Notice`, `Other` |
| `postedDate` | ISO 8601 timestamp |
| `lastModifiedDate` | ISO 8601 — when the record was last updated in regulations.gov |
| `commentStartDate` | ISO 8601 or null |
| `commentEndDate` | ISO 8601 or null |
| `commentCount` | Integer |
| `openForComment` | Boolean |
| `withdrawn` | Boolean |
| `frDocNum` | Federal Register document number, or null |
| `state` | Parsed state name, `"Tribal"`, or null |
| `searchTerms` | List of search terms that returned this document |

**Docket** (values in `dockets.json`, keyed by `docketId`):

| Field | Description |
|-------|-------------|
| `docketId` | e.g. `EPA-HQ-OAR-2024-0333` |
| `title` | Docket title |
| `abstract` | Full docket description |
| `rin` | Regulatory Information Number |
| `lastModifiedDate` | ISO 8601 timestamp |

## Named Deregulatory Actions

The app should call these out by name, not just raw docket IDs. These correspond to the March 2025 press release:

- **Good Neighbor Plan** — cross-state air pollution (ozone) rule, targeted for full repeal
- **PM 2.5 NAAQS** — particulate matter air quality standards under reconsideration
- **Regional Haze** — interstate visibility program, being restructured
- **Exceptional Events** — rulemaking that governs when states can exclude wildfire/prescribed-fire pollution from SIP calculations

## Limitations

- regulations.gov search is keyword-based; some SIP actions may use different language and won't surface
- State parsing from titles is imperfect; national rulemakings have no state
- The API caps results at 5,000 per query; if any single search term returns more, `fetch.py` will only get the first 5,000 (sorted by `postedDate`)
- Docket abstracts and full document text aren't included; PDFs link out to regulations.gov
