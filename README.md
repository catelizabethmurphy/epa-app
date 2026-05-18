# PFAS Forever Chemicals Tracker

A static news app tracking EPA regulatory actions on **PFAS** (per- and polyfluoroalkyl substances, a.k.a. "forever chemicals") across the Trump 1, Biden, and Trump 2 administrations.

The editorial premise: PFAS contaminate the drinking water of an estimated 176 million Americans. Biden issued the first binding federal drinking water limits in April 2024. The second Trump administration has since rescinded four of those limits, weakened TSCA reporting, and contested the rule in court — even as the MAHA Commission named PFAS a chronic-disease driver. This app tracks the gap between what the EPA says and what it files.

Built on the same Flask + Frozen-Flask pattern as [ghostapps/first-news-app-umd](https://github.com/ghostapps/first-news-app-umd). Deploys to GitHub Pages as a fully static site.

## Architecture

```
epa-app/
├── app.py                # Flask routes
├── freeze.py             # Frozen-Flask build + Pagefind indexing
├── Makefile
├── requirements.txt
├── .env                  # REGS_API_KEY (gitignored)
├── .env.example
│
├── fetch_fr.py           # Federal Register API → fr_documents.json
├── fetch_regs.py         # regulations.gov API → documents.json, dockets.json
├── fetch_press.py        # Scrapes epa.gov press releases → press_releases.json
├── fetch_agenda.py       # Unified Agenda XML → agenda.json
├── enrich.py             # Adds signalType, category, compounds, era, etc.
├── embed.py              # sentence-transformers similarity index
│
├── static/
│   ├── css/style.css
│   └── data/
│       ├── documents.json         # regs.gov documents
│       ├── fr_documents.json      # Federal Register documents (primary source)
│       ├── dockets.json           # docket metadata
│       ├── press_releases.json    # EPA press releases
│       ├── agenda.json            # planned rules from Unified Agenda
│       ├── events.json            # hand-curated timeline events
│       ├── status.json            # status cards for the three core rules
│       ├── sources.json           # curated outside reading
│       ├── pfas_context.json      # narrative stats for the homepage
│       ├── trump1_context.json    # Trump 1 PFAS Action Plan context
│       ├── signals_override.json  # manual signal type overrides
│       ├── embeddings.json        # local embeddings for related-doc surface
│       ├── similarities.json      # precomputed top-5 neighbors per doc
│       ├── pfas-bill-tracker.csv  # state-level PFAS legislation
│       ├── text/*.txt             # full plain text of FR rules (gitignored)
│       └── timelines/
│           ├── drinking-water-limits.json
│           ├── hazardous-substance-designation.json
│           └── pfas-programs.json
│
└── templates/
    ├── base.html
    ├── index.html              # Homepage: scrolly intro + status + timeline
    ├── browse.html             # Filterable database view
    ├── explore.html            # Pagefind search + timeline/database tabs
    ├── docket.html             # Single docket, primary + supplementary docs
    ├── document.html           # Single document with full text + related
    ├── press.html              # Single press release
    ├── topic.html              # Curated timeline page (drinking water, etc.)
    ├── court.html              # Single court case
    ├── glossary.html           # Compounds + regulatory terms
    ├── state_tracker.html      # State PFAS legislation map
    └── search.html
```

## Data sources

| Source | Script | Key needed | Notes |
|---|---|---|---|
| **Federal Register API** | `fetch_fr.py` | none | Primary source. Rich metadata (abstracts, action, effective dates) + full-text XML. |
| **regulations.gov API** | `fetch_regs.py` | `REGS_API_KEY` | Supplements FR with docket metadata + comment counts. Free key at [api.data.gov/signup](https://api.data.gov/signup/). |
| **EPA press releases** | `fetch_press.py` | none | Scrapes `epa.gov/pfas/press-releases-related-pfas` — EPA's own curated PFAS list. |
| **Unified Regulatory Agenda** | `fetch_agenda.py` | none | XML feed from `reginfo.gov`. Forward-looking — what EPA *plans* to do. |

Document deduplication: regs.gov and FR records that share a `frDocNum` are merged in `app.get_all_documents()`; FR-only records get the synthetic ID `FR-{documentNumber}`.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and add your REGS_API_KEY
```

## Running the data pipeline

```bash
make fetch-all       # FR → regs.gov → press → agenda → enrich → embed
make fetch-quick     # Metadata only, no full-text or press scrape
make run             # Flask dev server on :5000
make freeze          # Build static site to build/ + Pagefind index
make deploy          # Push build/ to gh-pages
```

Individual stages:

```bash
make fetch-fr        # Federal Register (no key needed)
make fetch-regs      # regulations.gov (needs REGS_API_KEY)
make fetch-press     # EPA press releases
make fetch-agenda    # Unified Agenda
make enrich          # Signal/category classification + overrides
make embed           # Local sentence-transformers similarity index
```

`make freeze` also runs `npx pagefind` to build the client-side search index used by `/explore/`.

## Editorial scope

**Strict PFAS-only.** Not general TSCA, RCRA, or air-rule coverage. The relevance gate lives in `enrich.py`. Documents that don't mention a PFAS compound or PFAS-adjacent program are dropped during enrichment.

**Three core regulatory threads** the app calls out by name:

- **Drinking Water MCLs** — Biden's April 2024 limits; Trump 2 rescissions of GenX, PFHxS, PFNA, PFBS; D.C. Circuit litigation
- **CERCLA Hazardous Substance Designation** — PFOA/PFOS Superfund listing; cleanup liability
- **TSCA Section 8(a)(7) Reporting** — manufacturer reporting requirements; rollback to "more practical" version

**Signal classification** (`signalType`): each doc/press release is tagged `rollback`, `protection`, `delay`, `rhetoric`, `litigation`, or `other`. Overrides live in `static/data/signals_override.json` for press releases whose headline doesn't match their actual effect (e.g. "EPA will keep PFOA/PFOS MCLs" simultaneously rescinds four other MCLs → tagged `rollback`).

**Era badges:** `trump1`, `biden`, `trump2` — assigned by `postedDate`.

## Deployment

Push `build/` to the `gh-pages` branch. The site publishes at `https://ghostapps.github.io/epa-app/`.

## Limitations

- Federal Register and regulations.gov are both keyword-based searches; PFAS actions that don't mention a known compound in title/abstract may be missed.
- Press release signal classification is heuristic with manual overrides; see `signals_override.json`.
- The `commentEndDate >= 2026-01-01` heuristic in `app.normalize_fr_doc` will need to be updated as time passes.
- Full-text downloads (`static/data/text/*.txt`) are gitignored and fetched fresh each pipeline run.
