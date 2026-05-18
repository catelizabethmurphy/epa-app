#!/usr/bin/env python3
"""
archive.py — Download every doc referenced by the curated timelines into
static/data/archives/ so the site can render them in-app without depending on
regs.gov, federalregister.gov, EPA's CMS, etc. staying online or stable.

For each curated timeline item that links to a source (documentId, docketId,
courtId, or externalUrl) the script:

  1. Picks/derives a stable slug
  2. Fetches the source — PDF for FR rules, HTML for everything else
  3. Saves the bytes to static/data/archives/<slug>.{pdf,html}
  4. Records an entry in static/data/archives.json with title, source label,
     captured-at timestamp, original URL, and the local archive path

Idempotent: re-running skips files that already exist on disk unless
--refresh is passed. The first pass over the timelines also writes any
auto-generated archiveSlug back into the timeline JSONs so subsequent passes
have stable slugs to work with.

Usage:
    python3 archive.py                # fetch only missing items
    python3 archive.py --refresh      # re-download everything
    python3 archive.py --dry-run      # just print what would happen
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

DATA      = Path(__file__).parent / "static" / "data"
ARCHIVES  = DATA / "archives"
INDEX     = DATA / "archives.json"
# Only the federal regulations master timeline is surfaced to the visitor.
# Per-program timelines (drinking-water-limits.json, etc.) and events.json are
# orphaned data — deleted in the Phase 3 cleanup, not archived now.
TIMELINES = [DATA / "timelines" / "pfas-programs.json"]

USER_AGENT = "epa-app-archiver/1.0 (PFAS regulatory tracker; +https://github.com/)"
TIMEOUT    = 30


# ── Source classification ─────────────────────────────────────────────────────

DOMAIN_LABELS = {
    "www.epa.gov":                     "EPA",
    "19january2021snapshot.epa.gov":   "EPA (Trump-I archive)",
    "www.federalregister.gov":         "Federal Register",
    "www.regulations.gov":             "regulations.gov",
    "www.whitehouse.gov":              "White House",
    "trumpwhitehouse.archives.gov":    "White House (Trump-I archive)",
    "www.congress.gov":                "Congress.gov",
    "www.amwa.net":                    "AMWA",
    "www.uschamber.com":               "U.S. Chamber of Commerce",
    "cdn.govexec.com":                 "Government Executive",
}


def label_for(url):
    host = urlparse(url).netloc
    return DOMAIN_LABELS.get(host, host)


# ── Slug helpers ──────────────────────────────────────────────────────────────

def slugify(text, maxlen=60):
    """Slugify text → ascii kebab. Used for externalUrl items that lack a slug."""
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:maxlen].rstrip("-") or "untitled"


def slug_from_url(url, title=None):
    """Generate a slug for an externalUrl.

    Prefer the page title (if supplied) since it's the most readable; fall back
    to the last meaningful path segment of the URL.
    """
    if title:
        s = slugify(title)
        if s and s != "untitled":
            return s
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p and p not in {"pfas", "news", "press"}]
    base = parts[-1] if parts else urlparse(url).netloc
    return slugify(re.sub(r"\.(html?|pdf|aspx)$", "", base, flags=re.I))


# ── Timeline loading ──────────────────────────────────────────────────────────

def load_timeline_items():
    """Return a flat list of (timeline_path, item_dict, mutable_root) tuples.

    Schema:
        events.json             → list of items at top level
        timelines/*.json        → dict with a `timeline` key holding the list

    mutable_root is the in-memory object loaded from the JSON file; we mutate
    items in place to add archiveSlug fields, then write the root back.
    """
    out = []
    for path in TIMELINES:
        if not path.exists():
            continue
        root = json.loads(path.read_text())
        if isinstance(root, list):
            items = root
        elif isinstance(root, dict):
            items = root.get("timeline") or []
        else:
            items = []
        for it in items:
            if isinstance(it, dict):
                out.append((path, it, root))
    return out


def save_timelines_with_slugs(touched_roots):
    """Write timeline JSON files that had archiveSlug added back to disk."""
    for path, root in touched_roots.items():
        path.write_text(json.dumps(root, indent=2, ensure_ascii=False) + "\n")
        print(f"  updated slugs in {path.name}")


# ── Existing-data lookups ─────────────────────────────────────────────────────

def load_legacy(name, default):
    fp = DATA / name
    return json.loads(fp.read_text()) if fp.exists() else default


def build_doc_lookups():
    """Index existing documents.json + fr_documents.json + dockets.json so we
    can pull metadata for the few items those files cover."""
    docs    = {d["documentId"]: d for d in load_legacy("documents.json", [])}
    fr_docs = load_legacy("fr_documents.json", {})
    dockets = load_legacy("dockets.json", {})
    return docs, fr_docs, dockets


# ── Resolve a reference → fetch plan ──────────────────────────────────────────

class FetchPlan:
    """One thing to download into static/data/archives/."""
    def __init__(self, *, slug, kind, source_url, title, source_label,
                 published_date=None, abstract=None, document_id=None,
                 docket_id=None, court_id=None, refs=(), fallback_url=None,
                 timeline_date=None):
        self.slug          = slug
        self.kind          = kind                    # "pdf" | "html"
        self.source_url    = source_url
        self.title         = title
        self.source_label  = source_label
        self.published_date= published_date
        self.abstract      = abstract
        self.document_id   = document_id
        self.docket_id     = docket_id
        self.court_id      = court_id
        self.fallback_url  = fallback_url            # used if source_url 4xx's
        self.timeline_date = timeline_date           # the date copy on the curated timeline
        self.refs          = list(refs)              # timeline items that link to this

    def archive_relpath(self):
        # Relative to static/, so url_for('static', filename=...) resolves to
        # /static/data/archives/<slug>.<ext>.
        return f"data/archives/{self.slug}.{self.kind}"

    def archive_abspath(self):
        return ARCHIVES / f"{self.slug}.{self.kind}"


def plan_for_item(item, docs, fr_docs, dockets):
    """Return a FetchPlan for one timeline item, or None if it can't be resolved."""
    # 1) regulations.gov document — prefer FR PDF if we have one, otherwise
    #    pull the PDF directly from downloads.regulations.gov (the CDN where
    #    the SPA's "Download" button points). Falls through to a synthesized
    #    stub only if both fail.
    did = item.get("documentId")
    if did:
        doc = docs.get(did) or {}
        fr  = fr_docs.get(doc.get("frDocNum")) if doc.get("frDocNum") else None
        if fr and fr.get("pdfUrl"):
            return FetchPlan(
                slug          = slugify(did),
                kind          = "pdf",
                source_url    = fr["pdfUrl"],
                title         = doc.get("title") or fr.get("title") or did,
                source_label  = "Federal Register",
                published_date= fr.get("publicationDate") or doc.get("postedDate"),
                abstract      = fr.get("abstract"),
                document_id   = did,
                docket_id     = doc.get("docketId"),
            )
        return FetchPlan(
            slug          = slugify(did),
            kind          = "pdf",
            source_url    = f"https://downloads.regulations.gov/{did}/content.pdf",
            title         = doc.get("title") or item.get("title") or did,
            source_label  = "regulations.gov",
            published_date= doc.get("postedDate"),
            document_id   = did,
            docket_id     = doc.get("docketId"),
            # If the regs.gov CDN can't find the doc (bad id, deprecated, etc.)
            # fall back to whatever externalUrl the curated item provides.
            fallback_url  = item.get("externalUrl"),
        )

    # 2) regulations.gov docket
    dkid = item.get("docketId")
    if dkid:
        dk = dockets.get(dkid) or {}
        return FetchPlan(
            slug          = slugify(dkid),
            kind          = "html",
            source_url    = f"https://www.regulations.gov/docket/{dkid}",
            title         = dk.get("title") or dkid,
            source_label  = "regulations.gov",
            published_date= dk.get("lastModifiedDate"),
            abstract      = dk.get("abstract"),
            docket_id     = dkid,
        )

    # 3) Court ruling — no source data in repo; archive whatever URL the
    #    curated item provides, otherwise skip.
    cid = item.get("courtId")
    if cid:
        url = item.get("externalUrl") or item.get("courtUrl")
        if not url:
            return None  # caller will warn
        kind = "pdf" if url.lower().endswith(".pdf") else "html"
        return FetchPlan(
            slug          = slugify(cid),
            kind          = kind,
            source_url    = url,
            title         = item.get("title") or cid,
            source_label  = label_for(url),
            published_date= item.get("date"),
            court_id      = cid,
        )

    # 4) Plain externalUrl item
    url = item.get("externalUrl")
    if url:
        slug = item.get("archiveSlug") or slug_from_url(url, item.get("title"))

        # Rewrite regulations.gov SPA URLs → CDN PDF, so we archive the real
        # filing instead of an empty SPA shell. Pattern:
        #   https://www.regulations.gov/document/<id>
        #   → https://downloads.regulations.gov/<id>/content.pdf
        m = re.match(r"https?://www\.regulations\.gov/document/([^/?#]+)", url)
        if m:
            return FetchPlan(
                slug          = slug,
                kind          = "pdf",
                source_url    = f"https://downloads.regulations.gov/{m.group(1)}/content.pdf",
                title         = item.get("title") or url,
                source_label  = "regulations.gov",
                published_date= item.get("date"),
                document_id   = m.group(1),
                fallback_url  = url,
            )

        kind = "pdf" if url.lower().endswith(".pdf") else "html"
        return FetchPlan(
            slug          = slug,
            kind          = kind,
            source_url    = url,
            title         = item.get("title") or url,
            source_label  = label_for(url),
            published_date= item.get("date"),
        )

    return None


# ── Fetching ──────────────────────────────────────────────────────────────────

def fetch(url, *, allow_pdf=True):
    """GET a URL with timeout + UA. Return (bytes, content_type).

    The regulations.gov CDN blocks bare-UA requests but accepts traffic that
    looks like it came from the regulations.gov SPA — add Referer + Origin +
    a browser-like UA when hitting that host.
    """
    headers = {"User-Agent": USER_AGENT}
    if "downloads.regulations.gov" in url or "regulations.gov" in url:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36",
            "Referer": "https://www.regulations.gov/",
            "Origin":  "https://www.regulations.gov",
        }
    r = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    return r.content, (r.headers.get("Content-Type") or "").lower()


def clean_html(raw, source_url):
    """Strip scripts/nav/footer chrome from a page and return a self-contained
    HTML fragment. Keeps just the main article body so the archived page is
    small + renderable in our chrome.
    """
    soup = BeautifulSoup(raw, "html.parser")

    # Drop noise
    for sel in ("script", "style", "noscript", "iframe", "form", "nav",
                "header", "footer", "[role=banner]", "[role=navigation]",
                "[role=contentinfo]", ".breadcrumb", ".breadcrumbs",
                ".usa-banner", ".usa-footer", ".cookie", ".region-header",
                ".region-footer"):
        for tag in soup.select(sel):
            tag.decompose()

    # Pick the densest body — try common containers first
    candidates = []
    for sel in ("article", "main", "[role=main]", "#main-content",
                ".main-content", ".node--type-article", ".region-content",
                ".content", "#content"):
        for el in soup.select(sel):
            candidates.append(el)
    body = max(candidates, key=lambda c: len(c.get_text(" ", strip=True)),
               default=soup.body or soup)

    # Strip inline event handlers + on*= attributes that could be reflected XSS
    for tag in body.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    elif soup.h1:
        title = soup.h1.get_text(" ", strip=True)

    return str(body), title


def synthesize_stub(plan, fetch_error):
    """Build a self-contained HTML fragment from metadata when the live fetch
    fails (regs.gov + congress.gov block scrapers; some URLs 404). The page
    has the title, abstract if we know one, and a button back to the source.
    """
    bits = []
    bits.append(f"<h1>{_escape_html(plan.title)}</h1>")
    meta = []
    if plan.published_date:
        meta.append(f"<dt>Date</dt><dd>{_escape_html(plan.published_date)}</dd>")
    if plan.document_id:
        meta.append(f"<dt>Document ID</dt><dd>{_escape_html(plan.document_id)}</dd>")
    if plan.docket_id:
        meta.append(f"<dt>Docket</dt><dd>{_escape_html(plan.docket_id)}</dd>")
    if plan.court_id:
        meta.append(f"<dt>Case</dt><dd>{_escape_html(plan.court_id)}</dd>")
    meta.append(f"<dt>Source</dt><dd>{_escape_html(plan.source_label)}</dd>")
    bits.append("<dl>" + "".join(meta) + "</dl>")
    if plan.abstract:
        bits.append(f"<h2>Summary</h2><p>{_escape_html(plan.abstract)}</p>")
    bits.append(
        '<p style="margin-top:1.5rem;"><a href="'
        + _escape_html(plan.source_url)
        + '" target="_blank" rel="noopener">View original on '
        + _escape_html(plan.source_label)
        + " ↗</a></p>"
    )
    bits.append(
        '<p style="font-size:0.78rem;color:#888;margin-top:1.5rem;">'
        "This page was generated from metadata because the live source "
        "could not be archived ("
        + _escape_html(fetch_error.split(":", 1)[0])
        + "). Click through to read the original."
        "</p>"
    )
    return "".join(bits)


def _escape_html(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def write_pdf(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def write_html(path, fragment, *, original_url, title, captured_at):
    path.parent.mkdir(parents=True, exist_ok=True)
    page = (
        "<!DOCTYPE html>\n"
        f"<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{title}</title>"
        f"<meta name=\"archive:source\" content=\"{original_url}\">"
        f"<meta name=\"archive:captured-at\" content=\"{captured_at}\">"
        "</head><body>" + fragment + "</body></html>\n"
    )
    path.write_text(page, encoding="utf-8")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="Re-download files even if they already exist")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plans, don't download anything")
    ap.add_argument("--limit", type=int, default=None,
                    help="Stop after N fetches (debugging)")
    args = ap.parse_args()

    docs, fr_docs, dockets = build_doc_lookups()
    items = load_timeline_items()

    # First pass: build plans, dedupe by slug, attribute back-references
    plans = {}
    touched_roots = {}
    skipped       = []
    for path, item, root in items:
        plan = plan_for_item(item, docs, fr_docs, dockets)
        if not plan:
            if item.get("courtId") or item.get("documentId"):
                skipped.append((item.get("title") or "?", "no source data"))
            continue

        # Stamp every resolved item with its archiveSlug so the template can
        # route all timeline links to /archive/<slug>/ uniformly.
        if item.get("archiveSlug") != plan.slug:
            item["archiveSlug"] = plan.slug
            touched_roots[path] = root

        # Always prefer the curated timeline's date over whatever upstream
        # publication date we found — the editorial date is what we display.
        if not plan.timeline_date:
            plan.timeline_date = item.get("date")

        existing = plans.get(plan.slug)
        if existing:
            existing.refs.append({"timeline": path.name,
                                  "title":    item.get("title")})
            if not existing.timeline_date:
                existing.timeline_date = item.get("date")
            continue
        plan.refs.append({"timeline": path.name, "title": item.get("title")})
        plans[plan.slug] = plan

    print(f"Found {len(plans)} unique sources across {len(items)} timeline items")
    if skipped:
        print(f"Skipped {len(skipped)}: {skipped}")
    if args.dry_run:
        for p in plans.values():
            print(f"  [{p.kind}] {p.slug:<45} ← {p.source_url}")
        return

    if touched_roots:
        save_timelines_with_slugs(touched_roots)

    ARCHIVES.mkdir(parents=True, exist_ok=True)

    # Existing index (preserve entries for plans we skip this run)
    index = json.loads(INDEX.read_text()) if INDEX.exists() else {}

    fetched = 0
    errors  = []
    for slug, plan in sorted(plans.items()):
        out = plan.archive_abspath()
        if out.exists() and not args.refresh:
            index[slug] = _entry_for(plan)
            continue
        if args.limit is not None and fetched >= args.limit:
            break
        try:
            print(f"  ↓ {plan.kind:<4} {slug} ← {plan.source_url}")
            try:
                raw, ctype = fetch(plan.source_url)
            except requests.HTTPError as primary_err:
                if plan.fallback_url and plan.fallback_url != plan.source_url:
                    print(f"    ↻ retry via {plan.fallback_url}")
                    raw, ctype = fetch(plan.fallback_url)
                    plan.source_url = plan.fallback_url
                    plan.source_label = label_for(plan.fallback_url)
                    plan.kind = "pdf" if plan.fallback_url.lower().endswith(".pdf") else "html"
                    out = plan.archive_abspath()
                else:
                    raise primary_err
            captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if plan.kind == "pdf":
                write_pdf(out, raw)
            else:
                fragment, title = clean_html(raw, plan.source_url)
                if title and not plan.title:
                    plan.title = title
                write_html(out, fragment,
                           original_url = plan.source_url,
                           title        = plan.title,
                           captured_at  = captured_at)
            entry = _entry_for(plan)
            entry["capturedAt"] = captured_at
            index[slug] = entry
            fetched += 1
            time.sleep(0.5)  # don't hammer
        except Exception as e:
            # Fallback: synthesize an HTML stub from metadata we already have.
            # Useful for SPAs (regs.gov, congress.gov) that block scrapers and
            # for the occasional dead URL.
            try:
                captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                stub = synthesize_stub(plan, str(e))
                # Force .html extension if the original was .pdf — we couldn't
                # get the PDF, so we're writing a metadata page instead.
                plan.kind = "html"
                out = plan.archive_abspath()
                write_html(out, stub,
                           original_url = plan.source_url,
                           title        = plan.title,
                           captured_at  = captured_at)
                entry = _entry_for(plan)
                entry["capturedAt"] = captured_at
                entry["synthesized"] = True
                entry["fetchError"]  = str(e).split("\n")[0][:200]
                index[slug] = entry
                fetched += 1
                print(f"    ⚠ synthesized stub ({e.__class__.__name__})")
            except Exception as e2:
                errors.append((slug, f"{e} / synthesis failed: {e2}"))
                print(f"    ✗ {e}")

    INDEX.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")
    print(f"\nFetched {fetched}; index entries: {len(index)}; errors: {len(errors)}")
    for slug, msg in errors:
        print(f"  ✗ {slug}: {msg}")


def _entry_for(plan):
    return {
        "slug":          plan.slug,
        "kind":          plan.kind,
        "title":         plan.title,
        "sourceLabel":   plan.source_label,
        "sourceUrl":     plan.source_url,
        "archivePath":   plan.archive_relpath(),
        "timelineDate":  plan.timeline_date,
        "abstract":      plan.abstract,
        "documentId":    plan.document_id,
        "docketId":      plan.docket_id,
        "courtId":       plan.court_id,
        "refs":          plan.refs,
    }


if __name__ == "__main__":
    main()
