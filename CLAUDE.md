# CLAUDE.md — epa-app

Project-specific notes for Claude Code. For human-facing docs, see `README.md`.

## What this is

A PFAS regulatory tracker. Flask + Frozen-Flask, deployed as a static site to GitHub Pages. Tracks EPA actions on per-/polyfluoroalkyl substances across Trump 1, Biden, and Trump 2.

## Editorial guardrails

- **Scope is strictly PFAS.** Not general TSCA, not RCRA, not unrelated CAA rules. The relevance gate is in `enrich.py`; do not loosen it without explicit instruction.
- **Editorial tension is central:** rhetoric (press releases, MAHA framing) vs. regulatory record (FR filings, dockets). Both surfaces must stay visible.
- **Three named rules** the UI calls out: drinking water MCLs, CERCLA hazardous substance designation, TSCA §8(a)(7) reporting. These map to the three `static/data/timelines/*.json` files.

## Architecture at a glance

Data pipeline writes JSON to `static/data/`. Flask reads JSON at request time. Frozen-Flask bakes everything in `build/` for GitHub Pages. Client-side search uses Pagefind, indexed at freeze time.

```
fetch_fr.py     →  fr_documents.json      (Federal Register, primary)
fetch_regs.py   →  documents.json, dockets.json   (regulations.gov, supplement)
fetch_press.py  →  press_releases.json    (scraped from epa.gov)
fetch_agenda.py →  agenda.json            (Unified Agenda XML)
enrich.py       →  edits the above in place; adds signalType/category/compounds/era
embed.py        →  embeddings.json, similarities.json (local sentence-transformers)
```

The merged document list lives in `app.get_all_documents()` — regs.gov + FR docs, deduped by `frDocNum`. FR-only records use the synthetic ID `FR-{documentNumber}`. All routes consume the merged list.

## Key data fields

**Document** (regs.gov shape; FR shape normalized to match via `normalize_fr_doc`):

| Field | Notes |
|---|---|
| `documentId` | e.g. `EPA-HQ-OW-2024-0114-0001` or `FR-2025-12345` |
| `docketId` | Parent docket |
| `documentType` | `Rule`, `Proposed Rule`, `Notice` only (filtered in `SUBSTANTIVE_TYPES`) |
| `postedDate`, `commentEndDate`, `commentCount`, `openForComment`, `withdrawn` | regs.gov metadata |
| `frDocNum` | Used for FR ↔ regs.gov dedup |
| `signalType` | `rollback` \| `protection` \| `delay` \| `rhetoric` \| `litigation` \| `other` |
| `category`, `compounds`, `program` | Assigned in `enrich.py` |
| `era` | `trump1` \| `biden` \| `trump2` (from `postedDate`) |
| `mahaRelevant` | True when the doc intersects MAHA framing |
| `isExtension` | Deadline/comment-window extension |

**Press release** is similar but uses `pressId`. Manual signal corrections live in `static/data/signals_override.json` keyed by `documentId` or press URL slug.

## Routes

| Route | Template | Notes |
|---|---|---|
| `/` | `index.html` | Scrolly intro + status cards + curated timeline |
| `/browse/` | `browse.html` | Full filterable list |
| `/explore/` | redirects to `/` | (Currently disabled — `explore.html` still exists.) |
| `/signals/` | redirects to `/explore/?view=timeline` | |
| `/docket/<id>/` | `docket.html` | Primary + supplementary split via `_is_primary_doc` |
| `/document/<id>/` | `document.html` | Pulls full text from `static/data/text/` if present |
| `/press/<id>/` | `press.html` | |
| `/topic/<id>/` | `topic.html` | Aliases: `drinking-water-mcl`, `cercla-designation`, `tsca-reporting` |
| `/drinking-water-limits/`, `/hazardous-substance-designation/`, `/pfas-reporting/`, `/federal-regulations/` | `topic.html` | The three core program timelines |
| `/court/<id>/` | `court.html` | |
| `/glossary/`, `/what-are-pfas/` | `glossary.html` | (`what-are-pfas` also renders glossary.html) |
| `/state-legislation/` | `state_tracker.html` | Backed by `pfas-bill-tracker.csv` |
| `/search/` | `search.html` | Pagefind UI |

## Visual conventions

- Typography: PT Sans + Libre Baskerville.
- Signal colors: rollback red `#c0202a`, protection green `#1a6b35`, delay orange `#b85c00`.
- Era badges: trump1 amber, biden blue, trump2 red.
- Dark scrolly intro on homepage (`#111` background). Pull-quotes via `.scrolly-pull` — serif italic, newsy/informative tone, **not** giant-number data-viz.

## Press release signal overrides — known slugs

These have intentional manual overrides because the headline misrepresents the filing:

- `epa-announces-it-will-keep-maximum-contaminant-levels-pfoa-pfos` → **rollback** (simultaneously rescinds 4 other MCLs)
- `trump-epa-announces-next-steps-regulatory-pfoa-and-pfos-cleanup-efforts-provides` → **protection** (CERCLA retention Sep 2025)
- `administrator-zeldin-announces-major-epa-actions-combat-pfas-contamination` → **rhetoric**
- `trump-epa-highlights-major-year-one-pfas-actions-combat-risks-and-make-america-healthy` → **rhetoric**
- `epa-proposes-changes-make-pfas-reporting-requirements-more-practical-and-0` → **rollback** (TSCA data rollback)

Watch slugs exactly — they are long and easy to typo.

## Hand-curated files (do not regenerate)

- `static/data/status.json` — status of the three core rules
- `static/data/events.json` — manually selected timeline events
- `static/data/sources.json` — outside reading
- `static/data/pfas_context.json`, `trump1_context.json` — narrative stats
- `static/data/signals_override.json` — signal-type corrections
- `static/data/timelines/*.json` — the three program timeline pages

## Build & run

```bash
make fetch-all   # full pipeline (~10–15 min)
make fetch-quick # metadata only
make run         # Flask dev server
make freeze      # static build to build/ (+ Pagefind index)
make deploy      # push build/ to gh-pages
```

`freeze.py` runs `npx --yes pagefind` after Frozen-Flask. Node is required at build time.

## Limitations / gotchas

- The `openForComment` heuristic for FR docs in `app.normalize_fr_doc` compares `commentsCloseDate >= "2026-01-01"`. This date is hard-coded and will need to advance.
- regs.gov API caps at 5,000 results per query. Watch for keyword searches that hit the cap.
- Full-text `.txt` files are gitignored. The site renders fine without them; they enable the document-detail full-text view and improve similarity quality.
- Press release scraping is fragile — EPA changes page structure occasionally. Run `fetch_press.py` with verbose output before assuming a release was actually missed.
