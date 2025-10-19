#!/usr/bin/env python3
"""
hires_scraper_playwright_offset.py  (timeout-hardened + per-page progress)

Targets Coomer-like list pages:
- List page: .card-list__items > article a[href] -> post URLs
- Post page: collect ALL images/mp4s (prefers largest srcset)
- Pagination: offset param ?o=0,50,... via --page-template "...?o={offset}" and --offset-step 50
- Robustness:
    * Uses wait_until="domcontentloaded" instead of "networkidle"
    * Explicit wait_for_selector for post list
    * Retries per page (--retry-pages)
    * Optional headful (--headful) to debug visually
    * Blocks heavy resources on list pages only (--block-list-media)
    * Optional HTTP-only mode (--http-mode) using requests/bs4 (no JS)
- UX:
    * Per-page progress bar while visiting posts on that page (no extra deps)
"""

import argparse
import os
import re
import sys
import time
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup

# Playwright is optional if --http-mode is set
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False

UA = "hires-scraper-playwright/2.2 (+https://example.invalid)"
DEFAULT_TIMEOUT_MS = 45000  # selector/navigation timeout
ACCEPTABLE_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}
ACCEPTABLE_VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov"}

# ---------------- util ----------------

def dprint(enabled: bool, *args):
    if enabled:
        print("[debug]", *args, file=sys.stderr)

def norm_url(u: str) -> str:
    p = urlparse(u)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))

def is_image_url(u: str) -> bool:
    _, ext = os.path.splitext(urlparse(u).path.lower())
    return ext in ACCEPTABLE_IMAGE_EXT

def is_video_url(u: str) -> bool:
    _, ext = os.path.splitext(urlparse(u).path.lower())
    return ext in ACCEPTABLE_VIDEO_EXT

def is_media_url(u: str) -> bool:
    return is_image_url(u) or is_video_url(u)

def pick_largest_from_srcset(srcset_raw: str, base: str) -> Optional[str]:
    if not srcset_raw:
        return None
    best = None
    best_w = -1
    for part in srcset_raw.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        cand = urljoin(base, bits[0])
        w = -1
        if len(bits) >= 2 and bits[1].endswith("w"):
            try:
                w = int(bits[1][:-1])
            except:
                w = -1
        if w > best_w or (w == -1 and best_w == -1):
            best = cand
            best_w = w
    return best

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def filename_from_url(u: str) -> str:
    name = os.path.basename(urlparse(u).path)
    if not name:
        name = re.sub(r"\W+", "_", urlparse(u).netloc) + ".bin"
    return name

def download_file(u: str, outdir: str, session: requests.Session) -> Optional[str]:
    ensure_dir(outdir)
    name = filename_from_url(u)
    path = os.path.join(outdir, name)
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(path):
        path = f"{base}_{i}{ext}"
        i += 1
    try:
        with session.get(u, stream=True, timeout=45) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return path
    except requests.RequestException:
        return None

def collect_media_from_html(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    results: List[str] = []

    for img in soup.find_all("img"):
        u = None
        for attr in ["srcset", "data-srcset"]:
            if img.has_attr(attr):
                best = pick_largest_from_srcset(img.get(attr), base_url)
                if best:
                    u = best
                    break
        if not u:
            for attr in ["data-src", "data-original", "data-large", "data-image", "data-full", "data-url", "src"]:
                if img.has_attr(attr):
                    cand = urljoin(base_url, img.get(attr))
                    if is_image_url(cand):
                        u = cand
                        break
        if not u:
            parent = img.find_parent("a", href=True)
            if parent:
                cand = urljoin(base_url, parent["href"])
                if is_media_url(cand):
                    u = cand
        if u:
            results.append(norm_url(u))

    for v in soup.find_all("video"):
        for s in v.find_all("source"):
            if s.has_attr("src"):
                cand = urljoin(base_url, s["src"])
                if is_video_url(cand):
                    results.append(norm_url(cand))
        if v.has_attr("src"):
            cand = urljoin(base_url, v["src"])
            if is_video_url(cand):
                results.append(norm_url(cand))

    for a in soup.find_all("a", href=True):
        cand = urljoin(base_url, a["href"])
        if is_media_url(cand):
            results.append(norm_url(cand))

    seen = set()
    out = []
    for u in results:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

# simple, dependency-free progress bar
def progress_draw(prefix: str, i: int, total: int, width: int = 30):
    if total <= 0:
        bar = "-" * width
        pct = "0%"
    else:
        filled = int((i / total) * width)
        bar = "#" * filled + "-" * (width - filled)
        pct = f"{int((i/total)*100):3d}%"
    sys.stdout.write(f"\r{prefix} [{bar}] {i}/{total} {pct}")
    sys.stdout.flush()

def progress_done():
    sys.stdout.write("\n")
    sys.stdout.flush()

# ---------------- HTTP-only mode (no Playwright) ----------------

def http_get(session: requests.Session, url: str, debug: bool) -> Optional[str]:
    try:
        r = session.get(url, timeout=45, headers={"User-Agent": UA})
        r.raise_for_status()
        return r.text
    except Exception as e:
        dprint(debug, f"http get failed {url}: {e}")
        return None

def http_list_post_links(html: str, base_url: str, post_selector: str, article_anchor_selector: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    posts = soup.select(post_selector) if post_selector else soup.find_all("article")
    links: List[str] = []
    for n in posts:
        for a in n.select(article_anchor_selector):
            href = a.get("href")
            if href:
                links.append(norm_url(urljoin(base_url, href)))
    # de-dupe
    seen = set()
    uniq = []
    for u in links:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq

# ---------------- Playwright helpers ----------------

def get_article_post_links(page, post_selector: str, article_anchor_selector: str, debug: bool, selector_timeout_ms: int) -> List[str]:
    try:
        page.wait_for_selector(post_selector, timeout=selector_timeout_ms)
    except Exception:
        dprint(debug, f"post selector not found within timeout: {post_selector}")
        return []
    links = page.eval_on_selector_all(
        post_selector,
        f"""(nodes) => {{
            const out = [];
            for (const n of nodes) {{
                const anchors = n.querySelectorAll("{article_anchor_selector}");
                anchors.forEach(a => {{
                    const href = a.getAttribute("href");
                    if (href) out.push(new URL(href, location.href).href);
                }});
            }}
            return out;
        }}"""
    )
    seen = set()
    uniq = []
    for href in links:
        u = norm_url(href)
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq

def parse_paginator_summary_text(text: str) -> Tuple[Optional[int], Optional[int]]:
    m = re.search(r"Showing\s+(\d+)\s*-\s*(\d+)\s*of\s*(\d+)", text, flags=re.I)
    if not m:
        return None, None
    start = int(m.group(1))
    end = int(m.group(2))
    total = int(m.group(3))
    page_size = end - start + 1 if end >= start else None
    return page_size, total

def get_query_param(u: str, key: str) -> Optional[str]:
    q = parse_qs(urlparse(u).query)
    vals = q.get(key)
    return vals[0] if vals else None

def set_query_param(u: str, key: str, value: str) -> str:
    p = urlparse(u)
    q = parse_qs(p.query)
    q[key] = [value]
    new_query = urlencode(q, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, ""))

# ---------------- Core crawl (Playwright) ----------------

def crawl_playwright(
    start_url: str,
    dry_run: bool,
    outdir: str,
    max_pages: int,
    post_selector: str,
    article_anchor_selector: str,
    detail_media_selector: str,
    page_template: str,
    paginator_summary_selector: str,
    offset_param: str,
    offset_step: int,
    offset_max: Optional[int],
    polite_delay: float,
    debug: bool,
    headful: bool,
    block_list_media: bool,
    retry_pages: int,
    selector_timeout_ms: int,
    sleep_after_goto: float,
    show_progress: bool,
):
    from playwright.sync_api import sync_playwright

    posts_per_page: List[int] = []
    media_per_page: List[int] = []
    total_downloaded = 0
    total_posts = 0
    total_media = 0

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        context = p.chromium.launch_persistent_context(
            user_data_dir="",  # ephemeral
            headless=not headful,
            user_agent=UA,
        ) if headful else browser.new_context(user_agent=UA)

        # Optionally block heavy resources on **list pages** only
        if block_list_media:
            def _route(route, request):
                url = request.url.lower()
                if any(url.endswith(ext) for ext in [".png",".jpg",".jpeg",".gif",".webp",".mp4",".webm",".mkv",".mov",".css",".woff",".woff2",".ttf"]):
                    return route.abort()
                return route.continue_()
            context.route("**/*", _route)

        page = context.new_page()

        def goto_and_wait(url: str):
            dprint(debug, f"goto: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=selector_timeout_ms)
            if sleep_after_goto > 0:
                time.sleep(sleep_after_goto)
            try:
                page.wait_for_selector(post_selector, timeout=selector_timeout_ms)
            except Exception:
                pass

        # Determine mode (offset template is what we want for Coomer)
        mode = "offset" if (page_template and "{offset}" in page_template) else "numeric" if (page_template and "{page}" in page_template) else "offset"

        goto_and_wait(start_url)
        page_index = 1
        visited_posts = set()

        # derive offset_max from paginator summary if not provided
        if offset_max is None and paginator_summary_selector:
            try:
                txt = page.locator(paginator_summary_selector).first.inner_text().strip()
                page_size, total = parse_paginator_summary_text(txt) if txt else (None, None)
                if page_size and total:
                    steps = ((total - 1) // offset_step)
                    offset_max = steps * offset_step
                    dprint(debug, f"derived offset_max={offset_max} (page_size={page_size}, total={total})")
            except Exception:
                pass

        while True:
            # Retry loop for list page extraction
            for attempt in range(1, retry_pages + 1):
                post_links = get_article_post_links(page, post_selector, article_anchor_selector, debug, selector_timeout_ms)
                if post_links:
                    break
                dprint(debug, f"no posts found (attempt {attempt}/{retry_pages}); retrying after short sleep")
                time.sleep(0.6)
                goto_and_wait(page.url)

            posts_count = len(post_links)
            posts_per_page.append(posts_count)
            total_posts += posts_count
            dprint(debug, f"page {page_index}: posts={posts_count}")

            # progress bar for this page
            if show_progress:
                progress_draw(f"page {page_index} visiting posts", 0, posts_count)

            page_media_urls: List[str] = []
            for idx, href in enumerate(post_links, start=1):
                if href in visited_posts:
                    if show_progress:
                        progress_draw(f"page {page_index} visiting posts", idx, posts_count)
                    continue
                visited_posts.add(href)
                try:
                    post = context.new_page()
                    post.goto(href, wait_until="domcontentloaded", timeout=selector_timeout_ms)
                    if sleep_after_goto > 0:
                        time.sleep(sleep_after_goto)
                    html = post.content()
                    # Bias to selected nodes if provided
                    if detail_media_selector:
                        soup = BeautifulSoup(html, "lxml")
                        subset = soup.select(detail_media_selector)
                        if subset:
                            mini = "<html><body>" + "".join(str(x) for x in subset) + "</body></html>"
                            media_urls = collect_media_from_html(mini, href)
                        else:
                            media_urls = collect_media_from_html(html, href)
                    else:
                        media_urls = collect_media_from_html(html, href)

                    # de-dupe per post
                    seen = set()
                    uniq = []
                    for u in media_urls:
                        if u not in seen:
                            seen.add(u)
                            uniq.append(u)
                    page_media_urls.extend(uniq)
                    post.close()
                except Exception as e:
                    dprint(debug, f"post fetch failed {href}: {e}")
                finally:
                    if show_progress:
                        progress_draw(f"page {page_index} visiting posts", idx, posts_count)

            if show_progress:
                progress_done()

            # de-dupe per list page
            seen = set()
            page_media_unique = []
            for u in page_media_urls:
                if u not in seen:
                    seen.add(u)
                    page_media_unique.append(u)

            media_count = len(page_media_unique)
            media_per_page.append(media_count)
            total_media += media_count
            dprint(debug, f"page {page_index}: media={media_count}")

            if not dry_run:
                # (Optional) add a second progress bar here for downloads if you want
                for u in page_media_unique:
                    if download_file(u, outdir, session):
                        total_downloaded += 1

            # stop by page limit
            if max_pages > 0 and page_index >= max_pages:
                break

            # OFFSET pagination (Coomer)
            if mode == "offset":
                cur_val = get_query_param(page.url, "o")
