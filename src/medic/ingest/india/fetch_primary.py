"""Fetch India CDSCO approval PDFs from the primary listing page.

CDSCO publishes year-by-year PDFs at:
https://cdsco.gov.in/opencms/opencms/en/Approval_new/Approved-New-Drugs/

The page is rendered server-side with a table whose rows contain:
  - title text (e.g. "List of New Drugs approved in year 2025 to till date")
  - release date
  - JSP download URL like /opencms/opencms/system/modules/CDSCO.WEB/elements/download_file_division.jsp?num_id=MTM1NTA=

We parse the listing table and download each PDF.
"""

from __future__ import annotations

import logging
import re
import ssl
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

LANDING_URL = "https://cdsco.gov.in/opencms/opencms/en/Approval_new/Approved-New-Drugs/"
RAW_DIR = Path("data/raw/india/primary")
DOMAIN = "https://cdsco.gov.in"


def _client() -> httpx.Client:
    """Return an httpx.Client; CDSCO sometimes presents an old TLS chain."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return httpx.Client(timeout=120, follow_redirects=True, verify=False)


def fetch_landing_page(force: bool = False) -> str:
    cache = RAW_DIR / "landing.html"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists() and not force:
        return cache.read_text(encoding="utf-8")
    with _client() as client:
        resp = client.get(LANDING_URL)
        resp.raise_for_status()
    cache.write_text(resp.text, encoding="utf-8")
    return resp.text


_ROW_PATTERN = re.compile(
    r"<tr>\s*<td>\d+</td>\s*<td>([^<]+)</td>\s*<td>([^<]+)</td>\s*"
    r"<td>\s*<a\s+href='([^']+)'",
    re.IGNORECASE | re.DOTALL,
)


def extract_pdf_entries(html: str) -> list[dict]:
    """Return list of {title, release_date, url, year} from the listing table."""
    entries: list[dict] = []
    for m in _ROW_PATTERN.finditer(html):
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        release_date = m.group(2).strip()
        href = m.group(3).strip()
        if not href:
            continue
        if not href.startswith("http"):
            href = f"{DOMAIN}{href}"
        # Extract year from title
        year_m = re.search(r"(19|20)\d{2}", title)
        year = int(year_m.group(0)) if year_m else 0
        entries.append({
            "title": title,
            "release_date": release_date,
            "url": href,
            "year": year,
        })
    return entries


_IFRAME_PATTERN = re.compile(r"<iframe[^>]*src=['\"]([^'\"]+\.pdf)['\"]", re.IGNORECASE)


def _resolve_pdf_url(jsp_url: str) -> str:
    """The CDSCO JSP wrapper returns HTML containing an iframe to the actual PDF.

    Returns the absolute URL of the underlying PDF.
    """
    with _client() as client:
        resp = client.get(jsp_url)
        resp.raise_for_status()
    text = resp.text
    if text[:5].strip().startswith("%PDF") or resp.headers.get("content-type", "").startswith("application/pdf"):
        return jsp_url
    m = _IFRAME_PATTERN.search(text)
    if not m:
        raise ValueError(f"No iframe PDF URL found at {jsp_url}")
    pdf_path = m.group(1)
    if pdf_path.startswith("http"):
        return pdf_path
    return f"{DOMAIN}{pdf_path}"


def download_pdf(url: str, dest: Path, force: bool = False) -> Path:
    if dest.exists() and not force:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    pdf_url = _resolve_pdf_url(url)
    with _client() as client:
        resp = client.get(pdf_url)
        resp.raise_for_status()
    if not resp.content[:5].lstrip().startswith(b"%PDF"):
        raise ValueError(f"Resolved URL did not return PDF: {pdf_url}")
    dest.write_bytes(resp.content)
    return dest


def fetch_all_pdfs(force: bool = False) -> list[dict]:
    """Download all CDSCO approval PDFs. Returns entry dicts with `path` added."""
    html = fetch_landing_page(force=force)
    entries = extract_pdf_entries(html)
    out = []
    # The listing carries more entries than distinct years — 39 entries over 32 years at the
    # time of writing, because some years have a main list plus an "additional approvals"
    # supplement. Naming purely by year made those collide, so the second download
    # overwrote the first and the supplement was lost. Suffix the duplicates.
    used: dict[str, int] = {}
    for entry in entries:
        year = entry["year"] or 0
        stem = f"india_{year}" if year else f"india_{abs(hash(entry['url'])) % 10**8}"
        n = used.get(stem, 0)
        used[stem] = n + 1
        filename = f"{stem}.pdf" if n == 0 else f"{stem}_{n + 1}.pdf"
        dest = RAW_DIR / filename
        try:
            download_pdf(entry["url"], dest, force=force)
            entry["path"] = dest
            out.append(entry)
            logger.info("[%s] %s -> %s", year, entry["title"][:60], dest)
        except Exception as e:
            logger.warning("Failed %s (%s): %s", entry["title"][:60], entry["url"], e)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    pdfs = fetch_all_pdfs()
    for p in pdfs:
        print(f"{p['year']}\t{p['path']}")
