#!/usr/bin/env python3
"""
update_publications.py — Fetch new Ackels lab publications and generate
HTML cards ready for content/publications/index.md.

Data sources (in priority order):
  1. PubMed / NCBI E-utilities  (structured, reliable, free)
  2. OpenAlex API               (open, DOI-indexed, no key needed)
  3. Google Scholar via scholarly (scraping, may be rate-limited)

Usage:
    pip install scholarly requests
    python scripts/update_publications.py

The script prints NEW publication cards to stdout.  Copy-paste them into the
correct year section of content/publications/index.md.
"""

from __future__ import annotations

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests

# ── Configuration ────────────────────────────────────────────────────────
AUTHOR_QUERY = "Ackels T"              # PubMed search term
SCHOLAR_ID = "Wni3Z2gAAAAJ"           # Google Scholar profile ID
OPENALEX_AUTHOR = "A5062092491"         # OpenAlex author ID for Tobias Ackels
PUBLICATIONS_FILE = Path(__file__).resolve().parent.parent / "content" / "publications" / "index.md"
NCBI_TOOL = "ackels-lab-pub-updater"
NCBI_EMAIL = "ackelslab@example.com"    # Replace with a real contact email
REQUEST_DELAY = 0.4                     # polite delay between API calls (s)

# ── Helpers ──────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None, **kw) -> requests.Response:
    """GET with a polite delay and basic error handling."""
    time.sleep(REQUEST_DELAY)
    r = requests.get(url, params=params, timeout=30, **kw)
    r.raise_for_status()
    return r


def extract_existing_dois(md_path: Path) -> set[str]:
    """Return all DOIs already present in the publications markdown file."""
    text = md_path.read_text(encoding="utf-8")
    # Match href="https://doi.org/..." patterns
    return {m.lower() for m in re.findall(r'https?://doi\.org/(10\.\S+?)(?=["\s<>])', text)}


# ── PubMed / NCBI E-utilities ───────────────────────────────────────────

def pubmed_search(query: str, retmax: int = 100) -> list[str]:
    """Search PubMed and return a list of PMIDs."""
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": retmax,
        "retmode": "json",
        "tool": NCBI_TOOL,
        "email": NCBI_EMAIL,
    }
    data = _get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params).json()
    return data.get("esearchresult", {}).get("idlist", [])


def pubmed_fetch(pmids: list[str]) -> list[dict[str, Any]]:
    """Fetch article metadata for a list of PMIDs (efetch XML)."""
    if not pmids:
        return []
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "tool": NCBI_TOOL,
        "email": NCBI_EMAIL,
    }
    xml_text = _get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", params).text
    root = ET.fromstring(xml_text)
    results = []
    for article in root.findall(".//PubmedArticle"):
        try:
            results.append(_parse_pubmed_article(article))
        except Exception as exc:
            pmid_el = article.find(".//PMID")
            pmid = pmid_el.text if pmid_el is not None else "?"
            print(f"  ⚠ Could not parse PMID {pmid}: {exc}", file=sys.stderr)
    return results


def _parse_pubmed_article(article: ET.Element) -> dict[str, Any]:
    """Extract structured fields from a PubmedArticle XML element."""
    mc = article.find(".//MedlineCitation")
    pmid = mc.findtext("PMID", "")
    art = mc.find("Article")

    # Title
    title = art.findtext("ArticleTitle", "").rstrip(".")

    # Authors
    authors = []
    for au in art.findall(".//Author"):
        last = au.findtext("LastName", "")
        initials = au.findtext("Initials", "")
        if last:
            authors.append(f"{last} {initials}".strip())

    # Journal / volume / pages / year
    journal_el = art.find("Journal")
    journal = journal_el.findtext("Title", "") if journal_el is not None else ""
    ji = journal_el.findtext("ISOAbbreviation", journal) if journal_el is not None else journal
    volume = journal_el.findtext(".//Volume", "") if journal_el is not None else ""
    issue = journal_el.findtext(".//Issue", "") if journal_el is not None else ""
    pages = art.findtext(".//Pagination/MedlinePgn", "")

    # Year
    year = ""
    pub_date = journal_el.find(".//PubDate") if journal_el is not None else None
    if pub_date is not None:
        year = pub_date.findtext("Year", "")

    # DOI
    doi = ""
    for eid in art.findall(".//ELocationID"):
        if eid.get("EIdType") == "doi":
            doi = eid.text or ""

    return {
        "pmid": pmid,
        "title": title,
        "authors": authors,
        "journal": ji,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "year": year,
        "doi": doi,
    }


# ── OpenAlex API ─────────────────────────────────────────────────────────

def openalex_fetch(author_id: str, per_page: int = 50) -> list[dict[str, Any]]:
    """Fetch publications for an OpenAlex author ID."""
    url = "https://api.openalex.org/works"
    params = {
        "filter": f"authorships.author.id:{author_id}",
        "per_page": per_page,
        "sort": "publication_date:desc",
    }
    data = _get(url, params).json()
    results = []
    for work in data.get("results", []):
        doi_url = work.get("doi", "") or ""
        doi = doi_url.replace("https://doi.org/", "")
        authors = []
        for a in work.get("authorships", []):
            name = a.get("author", {}).get("display_name", "")
            if name:
                authors.append(name)
        loc = work.get("primary_location") or {}
        source = loc.get("source") or {}
        biblio = work.get("biblio") or {}
        results.append({
            "title": work.get("display_name", ""),
            "authors": authors,
            "journal": source.get("display_name", ""),
            "volume": biblio.get("volume", ""),
            "issue": biblio.get("issue", ""),
            "pages": f"{biblio.get('first_page', '')}-{biblio.get('last_page', '')}".strip("-"),
            "year": str(work.get("publication_year", "")),
            "doi": doi,
            "pmid": (work.get("ids", {}).get("pmid") or "").replace("https://pubmed.ncbi.nlm.nih.gov/", "").rstrip("/"),
        })
    return results


# ── Google Scholar (via scholarly) ───────────────────────────────────────

def scholar_fetch(scholar_id: str) -> list[dict[str, Any]]:
    """Fetch publications from Google Scholar. Requires `pip install scholarly`."""
    try:
        from scholarly import scholarly
    except ImportError:
        print("  ℹ scholarly not installed — skipping Google Scholar.", file=sys.stderr)
        return []

    try:
        author = scholarly.search_author_id(scholar_id)
        author = scholarly.fill(author, sections=["publications"])
    except Exception as exc:
        print(f"  ⚠ Google Scholar lookup failed: {exc}", file=sys.stderr)
        return []

    results = []
    for pub in author.get("publications", []):
        bib = pub.get("bib", {})
        title = bib.get("title", "")
        authors_str = bib.get("author", "")
        year = str(bib.get("pub_year", ""))
        venue = bib.get("venue", "") or bib.get("journal", "")
        results.append({
            "title": title,
            "authors_str": authors_str,
            "year": year,
            "venue": venue,
            "doi": "",  # Scholar doesn't reliably provide DOIs
            "pmid": "",
        })
    return results


# ── Card generation ──────────────────────────────────────────────────────

def _format_author(name: str) -> str:
    """Bold Ackels T. in the author list."""
    if re.search(r"\bAckels\b", name, re.I):
        return f"<strong>{name}</strong>"
    return name


def _authors_to_str(authors: list[str]) -> str:
    """Convert a list of author names into a nicely formatted string."""
    formatted = [_format_author(a) for a in authors]
    if len(formatted) <= 2:
        return " and ".join(formatted)
    return ", ".join(formatted[:-1]) + " and " + formatted[-1]


def format_venue(pub: dict[str, Any]) -> str:
    """Build a venue string like 'Nature, 593, 558–563 (2021)'."""
    parts = [pub.get("journal", "")]
    vol = pub.get("volume", "")
    issue = pub.get("issue", "")
    pages = pub.get("pages", "")
    year = pub.get("year", "")
    if vol:
        if issue:
            parts.append(f"{vol}({issue})")
        else:
            parts.append(vol)
    if pages and pages != "-":
        parts.append(pages.replace("-", "–"))
    venue = ", ".join(p for p in parts if p)
    if year:
        venue += f" ({year})"
    return venue


def generate_card(pub: dict[str, Any]) -> str:
    """Generate an HTML pub-card from a publication dict."""
    doi = pub.get("doi", "")
    doi_url = f"https://doi.org/{doi}" if doi else ""
    title = pub.get("title", "Untitled")
    if doi_url:
        title_html = f'<a href="{doi_url}">{title}</a>'
    else:
        title_html = title

    if "authors_str" in pub and pub["authors_str"]:
        authors_html = pub["authors_str"]
    elif pub.get("authors"):
        authors_html = _authors_to_str(pub["authors"])
    else:
        authors_html = ""

    venue = pub.get("venue") or format_venue(pub)

    lines = [
        '<div class="pub-card">',
        f'<div class="pub-title">{title_html}</div>',
        f'<div class="pub-authors">{authors_html}</div>',
        f'<div class="pub-venue">{venue}</div>',
    ]

    links = []
    if doi_url:
        links.append(f'  <a class="link-doi" href="{doi_url}">DOI</a>')
    pmid = pub.get("pmid", "")
    if pmid:
        links.append(f'  <a class="link-pubmed" href="https://pubmed.ncbi.nlm.nih.gov/{pmid}/">PubMed</a>')

    if links:
        lines.append('<div class="pub-links">')
        lines.extend(links)
        lines.append('</div>')

    lines.append('</div>')
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("Ackels Lab — Publication Updater")
    print("=" * 60)

    # 1. Parse existing DOIs
    if PUBLICATIONS_FILE.exists():
        existing = extract_existing_dois(PUBLICATIONS_FILE)
        print(f"\n✔ Found {len(existing)} existing DOIs in {PUBLICATIONS_FILE.name}")
    else:
        existing = set()
        print(f"\n⚠ {PUBLICATIONS_FILE} not found — treating all pubs as new.")

    # 2. Gather from PubMed
    print("\n── PubMed search ──")
    pmids = pubmed_search(f'"{AUTHOR_QUERY}"[Author]')
    print(f"  Found {len(pmids)} PMIDs")
    pubmed_pubs = pubmed_fetch(pmids)

    # 3. Gather from OpenAlex
    print("\n── OpenAlex search ──")
    openalex_pubs = openalex_fetch(OPENALEX_AUTHOR)
    print(f"  Found {len(openalex_pubs)} works")

    # 4. Optionally gather from Google Scholar
    print("\n── Google Scholar search ──")
    scholar_pubs = scholar_fetch(SCHOLAR_ID)
    print(f"  Found {len(scholar_pubs)} works")

    # 5. Merge — use DOI as dedup key
    seen_dois: set[str] = set(existing)
    new_pubs: list[dict[str, Any]] = []

    for pub in pubmed_pubs + openalex_pubs:
        doi = pub.get("doi", "").lower()
        if doi and doi in seen_dois:
            continue
        if doi:
            seen_dois.add(doi)
        new_pubs.append(pub)

    if not new_pubs:
        print("\n✔ No new publications found. The page is up to date!")
        return

    # Sort newest first
    new_pubs.sort(key=lambda p: p.get("year", "0"), reverse=True)

    print(f"\n{'=' * 60}")
    print(f"  {len(new_pubs)} NEW publication(s) to add")
    print(f"{'=' * 60}\n")

    for pub in new_pubs:
        print(generate_card(pub))
        print()

    print("─" * 60)
    print("Copy the cards above into the appropriate year section of")
    print(f"  {PUBLICATIONS_FILE}")
    print("─" * 60)


if __name__ == "__main__":
    main()
