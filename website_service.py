"""
Hackathon website scraper.

Uses Playwright (headless Chromium) for the seed URL so that JavaScript-rendered
content — including React accordion FAQ answers — is captured after the browser
expands all collapsed panels. Sub-pages discovered via link crawling use the
lightweight requests + trafilatura path.

Falls back to requests + trafilatura if Playwright is unavailable or fails.

Environment variables:
  WEBSITE_MAX_PAGES   integer, default 10
  WEBSITE_MAX_CHARS   integer, default 60000 (total across all pages)
"""

from __future__ import annotations

import asyncio
import os
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests
import trafilatura
from bs4 import BeautifulSoup

_DEFAULT_MAX_PAGES = 10
_DEFAULT_MAX_CHARS = 60_000
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


def _config() -> tuple[int, int]:
    try:
        max_pages = int(os.environ.get("WEBSITE_MAX_PAGES", _DEFAULT_MAX_PAGES))
    except ValueError:
        max_pages = _DEFAULT_MAX_PAGES
    try:
        max_chars = int(os.environ.get("WEBSITE_MAX_CHARS", _DEFAULT_MAX_CHARS))
    except ValueError:
        max_chars = _DEFAULT_MAX_CHARS
    return max(1, max_pages), max(1000, max_chars)


def _same_domain(base: str, link: str) -> bool:
    return urlparse(base).netloc == urlparse(link).netloc


class _LinkExtractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self._base = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag != "a":
            return
        for name, val in attrs:
            if name == "href" and val:
                absolute = urljoin(self._base, val)
                clean = urlparse(absolute)._replace(fragment="", query="").geturl()
                if _same_domain(self._base, clean) and clean not in self.links:
                    self.links.append(clean)


def _extract_links(html: str, base_url: str) -> list[str]:
    parser = _LinkExtractor(base_url)
    parser.feed(html)
    return parser.links


# Tags whose content should always be discarded (scripts, styles, and navigation
# chrome that trafilatura normally handles — kept here for the fallback path).
_DISCARD_TAGS = {
    "script", "style", "noscript", "head", "nav", "header", "footer",
    "aside", "iframe", "svg", "form",
}


def _bs4_extract(html: str) -> str:
    """Full-DOM text extraction via BeautifulSoup.

    Used as a supplement when trafilatura's readability pass misses content
    that is present in the HTML but hidden via CSS or ARIA attributes (e.g.
    closed accordion panels, FAQ answers, collapsed `<details>` blocks).
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_DISCARD_TAGS):
        tag.decompose()
    raw = soup.get_text(separator="\n")
    # Collapse runs of blank lines down to a single blank line.
    text = re.sub(r"\n{3,}", "\n\n", raw)
    return text.strip()


async def _playwright_fetch_async(url: str) -> str:
    """Render *url* with a real browser, expand all accordions, return body HTML.

    Strategy for exclusive accordions (only one panel open at a time):
    click each toggle button individually and capture the page's visible text
    after each click, then union all unique lines.  This way even if opening
    panel B closes panel A, we still have A's answer in our snapshot.
    """
    from playwright.async_api import async_playwright  # noqa: PLC0415

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page(user_agent=_USER_AGENT)
            await page.goto(url, wait_until="networkidle", timeout=30_000)

            # Open every <details> element (CSS-driven, non-JS disclosure).
            await page.evaluate(
                "() => document.querySelectorAll('details').forEach(d => { d.open = true; })"
            )

            # Capture a baseline snapshot (text visible before any clicks).
            snapshots: list[str] = [await page.inner_text("body")]

            # For each aria-expanded accordion button, click it and snapshot the
            # visible text.  inner_text() only returns currently-visible text so
            # even exclusive accordions (open one → close others) are handled:
            # we collect the answer while its panel is open, then move to the next.
            handles = await page.query_selector_all('[aria-expanded]')
            for handle in handles:
                try:
                    expanded = await handle.get_attribute("aria-expanded")
                    if expanded == "false":
                        await handle.click(timeout=500)
                        await page.wait_for_timeout(250)
                        snapshots.append(await page.inner_text("body"))
                except Exception:  # noqa: BLE001
                    pass

            # Merge snapshots: keep every unique non-empty line while preserving
            # the order it first appeared (baseline ordering dominates).
            seen: set[str] = set()
            merged: list[str] = []
            for snapshot in snapshots:
                for line in snapshot.splitlines():
                    stripped = line.strip()
                    if stripped and stripped not in seen:
                        seen.add(stripped)
                        merged.append(stripped)

            # Return as plain text; caller wraps in BS4 cleanup.
            return "\n".join(merged)
        finally:
            await browser.close()


def _playwright_fetch(url: str) -> str | None:
    """Synchronous wrapper around `_playwright_fetch_async`; returns None on failure."""
    try:
        return asyncio.run(_playwright_fetch_async(url))
    except Exception:  # noqa: BLE001
        return None


def _fetch(session: requests.Session, url: str) -> str | None:
    try:
        resp = session.get(url, timeout=12, allow_redirects=True)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "html" not in ct.lower():
            return None
        return resp.text
    except Exception:
        return None


def scrape_website(url: str) -> dict:
    """
    Crawl the hackathon website and return a dict ready for LLM consumption:

      {
        "url":          seed URL,
        "pages_scraped": int,
        "page_urls":    [list of scraped URLs],
        "text":         combined clean text (page sections separated by ---),
        "char_count":   int,
        "truncated":    bool,
      }

    Raises ValueError for an empty/invalid URL.
    Raises requests.exceptions.RequestException if the seed URL is unreachable.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("Website URL is empty.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    max_pages, max_chars = _config()

    http = requests.Session()
    http.headers["User-Agent"] = _USER_AGENT

    visited: set[str] = set()
    queue: list[str] = [url]
    pages: list[dict[str, str]] = []
    total_chars = 0
    truncated = False

    while queue and len(pages) < max_pages and not truncated:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        # Use Playwright for the seed URL so JS-rendered content (React accordions,
        # FAQ answers, countdown timers, etc.) is captured after the browser runs.
        # Sub-pages use the lightweight requests path.
        used_playwright = False
        raw_html_for_links: str | None = None

        if current == url:
            playwright_text = _playwright_fetch(current)
            if playwright_text:
                html = playwright_text
                used_playwright = True
                # We still need the raw HTML for link extraction.
                raw_html_for_links = _fetch(http, current)
            else:
                html = _fetch(http, current)
                raw_html_for_links = html
        else:
            html = _fetch(http, current)
        if html is None:
            continue

        # Follow same-domain links found on the seed page only (avoids deep crawl).
        if current == url and raw_html_for_links:
            for link in _extract_links(raw_html_for_links, url):
                if link not in visited:
                    queue.append(link)

        if used_playwright:
            # html is already merged plain text from Playwright (not raw HTML).
            # Collapse any remaining blank lines and use directly.
            text = re.sub(r"\n{3,}", "\n\n", html).strip()
        else:
            # favor_recall=True makes trafilatura less aggressive about discarding
            # content that looks "hidden" or peripheral.
            text = trafilatura.extract(
                html,
                include_links=False,
                include_images=False,
                include_tables=True,
                no_fallback=False,
                favor_recall=True,
            ) or ""
            text = text.strip()
            if not text:
                text = _bs4_extract(html)

        if not text:
            continue

        remaining = max_chars - total_chars
        if len(text) > remaining:
            text = text[:remaining]
            pages.append({"url": current, "text": text})
            total_chars += len(text)
            truncated = True
            break

        pages.append({"url": current, "text": text})
        total_chars += len(text)

    combined = "\n\n---\n\n".join(
        f"[Source: {p['url']}]\n{p['text']}" for p in pages
    )

    return {
        "url": url,
        "pages_scraped": len(pages),
        "page_urls": [p["url"] for p in pages],
        "text": combined,
        "char_count": total_chars,
        "truncated": truncated,
    }
