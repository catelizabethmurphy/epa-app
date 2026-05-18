# CLAUDE.md — epa-app

Project-specific notes for Claude Code. For human-facing docs, see `README.md`.

## What this is

A PFAS regulatory tracker. Flask + Frozen-Flask, deployed as a static site to GitHub Pages. Tracks EPA actions on per-/polyfluoroalkyl substances across Trump 1, Biden, and Trump 2.

## Editorial guardrails

- **Scope is strictly PFAS.** Not general TSCA, not RCRA, not unrelated CAA rules. Filtering happens at the curation layer — `static/data/timelines/pfas-programs.json` is hand-edited.
- **Editorial tension is central:** rhetoric (press statements) vs. regulatory record (FR filings, court orders). Both surfaces must stay visible.
- **One source of truth for the timeline:** `pfas-programs.json` is the only timeline file the live site reads. Older per-program files (`drinking-water-limits.json` etc.) and `events.json` were deleted in the archive-pipeline rewrite.

## Architecture at a glance

The site has three data inputs, two routes that serve them, and a static freeze step.

```
static/data/timelines/pfas-programs.json   ← hand-curated timeline
static/data/archives.json                  ← index of locally-stored sources
static/data/archives/<slug>.{pdf,html}     ← actual archived files (~30MB)
static/data/pws_*.csv, zcta_population.json ← water-testing data
static/data/pfas-bill-tracker.csv          ← state-legislation tracker
```

`archive.py` reads the timeline, downloads every referenced source (FR PDFs, EPA press pages, AMWA court filings, etc.) into `static/data/archives/`, and writes a slim `archives.json` keyed by slug. Flask reads the curated JSON + archives.json at request time and serves PDFs from the static dir.

## Adding a new event

1. Add an entry to the `timeline` array in `static/data/timelines/pfas-programs.json`. Required: `date`, `title`, `era`, `signalType`, plus an `externalUrl` (or `documentId`/`docketId`/`courtId` for legacy items).
2. Run `make archive`. The script auto-generates an `archiveSlug` for the new item, downloads the source into `static/data/archives/`, and adds an entry to `archives.json`.
3. For pages that block scrapers (regs.gov, congress.gov), `archive.py` falls back to a synthesized HTML stub with metadata + a "View original" link. The stub still renders in-app; readers click through for the full source.

## Routes

| Route | Template | Notes |
|---|---|---|
| `/` | `index.html` | Scrolly intro + three nav cards |
| `/federal-regulations/` | `topic.html` | Renders `pfas-programs.json` timeline |
| `/archive/<slug>/` | `archive.html` | In-app viewer for one archived source; PDFs embed via `<iframe>`, HTML renders inline |
| `/state-legislation/` | `state_tracker.html` | Backed by `pfas-bill-tracker.csv` |
| `/water-testing/` | `water_testing.html` | Top-states ranking + worst-25 systems |
| `/water-testing/<state_slug>/` | `water_state.html` | Per-state utility breakdown |
| `/data/pws-search.json` | — | JSON endpoint for client-side water-system search |

## Key data fields

**Timeline item** (`pfas-programs.json`):

| Field | Notes |
|---|---|
| `date` | Free text — accepts `2024-04-26` or `April 2024` or `2024` |
| `title`, `description` | Editorial |
| `era` | `trump1` \| `biden` \| `trump2` |
| `signalType` | `rollback`, `protection`, `delay`, `litigation`, `rhetoric`, plus modifiers like `partial_rollback`, `proposed_protection`, `commitment_to_protection`, `under_reconsideration` |
| `force` | `final` \| `proposed` \| `announced` \| `withdrawn` (drives chip treatment) |
| `programSlug` | `drinking-water`, `superfund`, `both` |
| `typeLabel` | Free text — appears in the entry's meta line |
| `externalUrl` | Source URL — `archive.py` downloads it |
| `archiveSlug` | Auto-written by `archive.py`; the route is `/archive/<slug>/` |
| `documentId`, `docketId`, `courtId` | Legacy editorial ids — preserved for context, not used for routing |

**Archive entry** (`archives.json`, keyed by slug):

| Field | Notes |
|---|---|
| `kind` | `pdf` \| `html` |
| `title`, `sourceLabel`, `sourceUrl` | Display metadata |
| `archivePath` | Path under `static/` — e.g. `data/archives/<slug>.pdf` |
| `publishedDate`, `abstract` | Optional, used in landing page header |
| `documentId`, `docketId`, `courtId` | Mirrored from the timeline item, for editorial reference |
| `synthesized` + `fetchError` | True if the live fetch failed and a metadata stub was generated |

## Population formatting

Use the `|pop` Jinja filter for any served-population value. Ladder:
- `< 1,000` → nearest 10
- `1,000–4,999` → nearest 50
- `5,000–99,999` → nearest 100
- `100,000–999,999` → nearest 1,000
- `≥ 1,000,000` → `X.X million`

## Visual conventions

- Typography: PT Sans + Libre Baskerville.
- Signal colors: rollback red `#c0202a`, protection green `#1a6b35`, delay orange `#b85c00`, litigation purple `#5c3480`.
- Era badges: trump1 amber, biden blue, trump2 red.
- Timeline entries on mobile get a tinted card background by signal type (`.tl-entry[class*="rollback"]`, etc.) — the `[class*=]` selector matches modifier variants like `partial_rollback`.

## Build & run

```bash
make archive          # refetch missing archives (idempotent)
make archive-refresh  # force re-download everything
make run              # Flask dev server
make freeze           # static build to build/
make deploy           # push build/ to gh-pages
```

## Limitations / gotchas

- `archive.py` uses a single User-Agent string and a 0.5s delay between fetches. Sites that block scrapers (regulations.gov, congress.gov) get a synthesized stub instead of a real archive. Editorial value is preserved (title, abstract, source link); the live page isn't embedded.
- Slugs are truncated to 60 chars and aren't human-edited by default. If you want a prettier URL, set `archiveSlug` explicitly on the timeline item before running `archive.py`.
- Per-PFAS testing dataset CSVs (`pws_data.csv`, `pws_summaries.csv`, `pws_summary_stats.csv`) are excluded from the freeze copy by `FREEZER_STATIC_IGNORE` — they're inputs only.
- The `inhance-v-epa-23-60620` court case has no source URL in the curated timeline (yet); the archive step skips it. Add an `externalUrl` if you want it to land on `/archive/<slug>/`.
