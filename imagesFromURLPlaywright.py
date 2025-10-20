#!/usr/bin/env python3
"""
Gallery-style scraper (Playwright or HTTP-only) — streaming downloads + resume + per-download progress bars
with robust skip-if-existing logic (no re-downloads)

Features
- Downloads immediately per post (no per-page batching)
- Per-download progress bars (bytes + ETA; works with/without Content-Length)
- Breadcrumbs to resume: <out>/<source>/<username>/.state.jsonl
- Skips already-downloaded media by:
    1) state file (url recorded), OR
    2) filesystem presence of the base filename OR any _<n> variant (e.g., name.jpg, name_1.jpg)
- Waits for full list render (paginator-aware: "Showing 1 - 50 of 383")
- Pulls full-res images from .post__files figure > a.fileThumb.image-link (TRUSTED)
- Pulls videos from .post__files <video><source> and Fluid Player fallbacks
- Verbose per-post logging, optional URL echoing
- Optional size filter (min-bytes) for NON-TRUSTED links
- Saves into <out>/<source>/<username>/{images,videos}

Example:
  poetry run python imagesFromURLPlaywright.py \
    "https://example.site/onlyfans/user/USERNAME?o=0" \
    --page-template "https://example.site/onlyfans/user/USERNAME?o={offset}" \
    --offset-step 50 --offset-max 350 \
    --verbose --print-urls --min-bytes 120000
"""

import argparse
import json
import os
import re
import sys
import time
import glob
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Set
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup

# Playwright optional if --http-mode is set
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DEFAULT_TIMEOUT_MS = 45000
ACCEPTABLE_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}
ACCEPTABLE_VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov"}

# ---------------- util ----------------

def dprint(enabled: bool, *args):
    if enabled:
        print("[debug]", *args, file=sys.stderr, flush=True)

def vlog(enabled: bool, *args):
    if enabled:
        print(*args, flush=True)

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

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def filename_from_url(u: str) -> str:
    q = parse_qs(urlparse(u).query)
    if "f" in q and q["f"]:
        return q["f"][0]
    name = os.path.basename(urlparse(u).path)
    return name or "file.bin"

def split_base_ext(name: str) -> Tuple[str, str]:
    base, ext = os.path.splitext(name)
    return base, ext

def extract_username_from_url(u: str) -> Optional[str]:
    path = urlparse(u).path
    m = re.search(r"/user/([^/?#]+)/?", path)
    return m.group(1) if m else None

def extract_source_from_url(u: str) -> Optional[str]:
    path = urlparse(u).path.strip("/")
    parts = path.split("/")
    for i in range(len(parts) - 1):
        if parts[i] and parts[i+1] == "user":
            return parts[i].lower()
    return parts[0].lower() if parts else None

def output_subdir(root_out: str, source: str, username: str, media_url: str) -> str:
    leaf = "images" if is_image_url(media_url) else "videos" if is_video_url(media_url) else "other"
    return os.path.join(root_out, source, username, leaf)

def human_bytes(n: float) -> str:
    if n is None:
        return "?"
    if n < 1024:
        return f"{int(n)} B"
    units = ["KB", "MB", "GB", "TB"]
    i = 0
    n /= 1024.0
    while n >= 1024 and i < len(units)-1:
        n /= 1024.0
        i += 1
    return f"{n:.1f} {units[i]}"

def head_content_length(url: str, session: requests.Session, referer: Optional[str]) -> Optional[int]:
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    if referer:
        headers["Referer"] = referer
    try:
        r = session.head(url, allow_redirects=True, timeout=20, headers=headers)
        cl = r.headers.get("content-length")
        return int(cl) if cl and cl.isdigit() else None
    except requests.RequestException:
        return None

def safe_download_path(outdir: str, name: str) -> str:
    path = os.path.join(outdir, name)
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(path):
        path = f"{base}_{i}{ext}"
        i += 1
    return path

def any_variant_exists(outdir: str, name: str) -> Optional[str]:
    """
    Check if 'name' or any 'name_*.ext' exists in outdir.
    Returns the path of an existing variant if found, else None.
    """
    candidate = os.path.join(outdir, name)
    if os.path.exists(candidate):
        return candidate
    base, ext = os.path.splitext(candidate)
    pattern = f"{base}_*{ext}"
    matches = glob.glob(pattern)
    return matches[0] if matches else None

# -------- progress bar for downloads --------

class DownloadBar:
    def __init__(self, label: str, total: Optional[int], enabled: bool = True, width: int = 28):
        self.label = label
        self.total = total if (isinstance(total, int) and total >= 0) else None
        self.enabled = enabled
        self.width = width
        self.start = time.time()
        self.last_print = 0.0
        self.downloaded = 0

    def _line(self, end: str = ""):
        elapsed = max(1e-6, time.time() - self.start)
        speed = self.downloaded / elapsed
        if self.total:
            frac = min(1.0, self.downloaded / self.total)
            filled = int(frac * self.width)
            bar = "#" * filled + "-" * (self.width - filled)
            pct = f"{int(frac*100):3d}%"
            return f"\r{self.label} [{bar}] {pct}  {human_bytes(self.downloaded)}/{human_bytes(self.total)}  {human_bytes(speed)}/s{end}"
        else:
            dots = (int(self.downloaded / (1 << 18)) % (self.width + 1))
            bar = "•" * min(dots, self.width)
            return f"\r{self.label} [{bar:<{self.width}}]  {human_bytes(self.downloaded)}  {human_bytes(speed)}/s{end}"

    def update(self, n: int):
        if not self.enabled:
            return
        self.downloaded += n
        now = time.time()
        if now - self.last_print >= 0.05:
            sys.stdout.write(self._line())
            sys.stdout.flush()
            self.last_print = now

    def finish(self, ok: bool):
        if not self.enabled:
            return
        end = "" if ok else " (failed)"
        sys.stdout.write(self._line(end=end))
        sys.stdout.write("\n")
        sys.stdout.flush()

def download_file(u: str, outdir: str, session: requests.Session, referer: Optional[str] = None, show_bar: bool = True) -> Optional[str]:
    ensure_dir(outdir)
    name = filename_from_url(u)

    # NEW: skip if any variant already present
    existing = any_variant_exists(outdir, name)
    if existing:
        return existing  # treat as success/skip

    path = safe_download_path(outdir, name)
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    if referer:
        headers["Referer"] = referer

    total = head_content_length(u, session, referer)
    label = f"  ⇩ {os.path.basename(path)}"
    bar = DownloadBar(label, total, enabled=show_bar)

    tmp = path + ".part"
    try:
        with session.get(u, headers=headers, stream=True, timeout=90) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 15):
                    if not chunk:
                        continue
                    f.write(chunk)
                    bar.update(len(chunk))
        os.replace(tmp, path)
        bar.finish(True)
        return path
    except requests.RequestException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        bar.finish(False)
        return None

# --------- media extraction tuned to your markup ---------

TRUSTED_ANCHORS = (
    ".post__files .post__thumbnail figure a.fileThumb.image-link[href], "
    ".post__files a[href$='.mp4'], .post__files a[href*='.mp4']"
)
VIDEO_SOURCE_FALLBACKS = (
    ".post__files video source[src], "
    ".js-fluid-player source[src], "
    ".post__video source[src], "
    "video source[src]"
)

def collect_media_pairs(html: str, base_url: str) -> List[Tuple[str, bool]]:
    soup = BeautifulSoup(html, "lxml")
    pairs: List[Tuple[str, bool]] = []

    for a in soup.select(TRUSTED_ANCHORS):
        href = a.get("href")
        if not href:
            continue
        u = norm_url(urljoin(base_url, href))
        if is_media_url(u):
            pairs.append((u, True))

    for s in soup.select(VIDEO_SOURCE_FALLBACKS):
        src = s.get("src")
        if not src:
            continue
        u = norm_url(urljoin(base_url, src))
        if is_video_url(u):
            pairs.append((u, False))

    if not pairs:
        box = soup.select_one(".post__files")
        if box:
            for img in box.find_all("img"):
                for attr in ["data-full", "data-original", "data-large", "data-image", "data-src", "src"]:
                    val = img.get(attr)
                    if not val:
                        continue
                    u = norm_url(urljoin(base_url, val))
                    if is_image_url(u):
                        pairs.append((u, False))
                        break

    # de-dupe; keep trusted=True if any duplicate was trusted
    seen: Dict[str, bool] = {}
    for u, t in pairs:
        seen[u] = seen.get(u, False) or t
    return [(u, seen[u]) for u in seen.keys()]

def size_filter_with_trust(pairs: List[Tuple[str, bool]], session: requests.Session, min_bytes: int, verbose: bool, debug: bool, referer: Optional[str]) -> List[str]:
    if min_bytes <= 0:
        return [u for (u, _) in pairs]
    kept: List[str] = []
    for (u, trusted) in pairs:
        if trusted:
            kept.append(u)
            continue
        sz = head_content_length(u, session, referer)
        if sz is None or sz >= min_bytes:
            kept.append(u)
        else:
            vlog(verbose, f"  └─ skip small {sz} B (< {min_bytes} B): {u}")
            dprint(debug, f"skip small ({sz} B < {min_bytes} B): {u}")
    return kept

def count_kinds(urls: List[str]) -> Tuple[int, int]:
    return sum(1 for u in urls if is_image_url(u)), sum(1 for u in urls if is_video_url(u))

# ---------------- HTTP helpers ----------------

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
    seen = set(); uniq = []
    for u in links:
        if u not in seen:
            seen.add(u); uniq.append(u)
    return uniq

# ---------------- Paginator helpers ----------------

def parse_paginator_summary_text(text: str) -> Tuple[Optional[int], Optional[int]]:
    m = re.search(r"Showing\s+(\d+)\s*-\s*(\d+)\s*of\s*(\d+)", text, flags=re.I)
    if not m:
        return None, None
    start, end, total = map(int, [m.group(1), m.group(2), m.group(3)])
    page_size = end - start + 1 if end >= start else None
    return page_size, total

def wait_until_expected_posts(page, post_selector: str, paginator_summary_selector: str, selector_timeout_ms: int, debug: bool):
    try:
        txt = page.locator(paginator_summary_selector).first.inner_text().strip()
        page_size, _ = parse_paginator_summary_text(txt) if txt else (None, None)
    except Exception:
        page_size = None

    if not page_size:
        return

    try:
        page.wait_for_function(
            """([sel, expected]) => document.querySelectorAll(sel).length >= expected""",
            arg=[post_selector, page_size],
            timeout=selector_timeout_ms
        )
    except Exception:
        dprint(debug, f"wait_until_expected_posts: timeout before reaching expected count ({page_size})")

def get_article_post_links(page, post_selector: str, article_anchor_selector: str, paginator_summary_selector: str, selector_timeout_ms: int, debug: bool) -> List[str]:
    try:
        page.wait_for_selector(post_selector, timeout=selector_timeout_ms)
        wait_until_expected_posts(page, post_selector, paginator_summary_selector, selector_timeout_ms, debug)
    except Exception:
        dprint(debug, f"post selector not found within timeout: {post_selector}")

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
    seen = set(); uniq = []
    for href in links:
        u = norm_url(href)
        if u not in seen:
            seen.add(u); uniq.append(u)
    return uniq

# ---------------- Breadcrumbs (resume) ----------------

class Breadcrumbs:
    def __init__(self, root_out: str, source: str, username: str, enable: bool = True):
        self.enable = enable
        self.dir = os.path.join(root_out, source, username)
        ensure_dir(self.dir)
        self.path = os.path.join(self.dir, ".state.jsonl")
        self.visited_posts: Set[str] = set()
        self.downloaded_urls: Set[str] = set()
        if enable:
            self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    t = rec.get("type")
                    if t == "post_visited" and rec.get("post"):
                        self.visited_posts.add(rec["post"])
                    elif t == "media_downloaded" and rec.get("url"):
                        self.downloaded_urls.add(rec["url"])
        except Exception:
            pass

    def _append(self, rec: dict):
        if not self.enable:
            return
        rec = {"ts": datetime.utcnow().isoformat(timespec="seconds") + "Z", **rec}
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass

    def mark_post(self, url: str):
        self.visited_posts.add(url)
        self._append({"type": "post_visited", "post": url})

    def mark_media(self, url: str, path: str):
        self.downloaded_urls.add(url)
        self._append({"type": "media_downloaded", "url": url, "path": path})

# tiny inline progress bar for posts
def progress_draw(prefix: str, i: int, total: int, width: int = 30):
    if total <= 0:
        bar = "-" * width; pct = "0%"
    else:
        filled = int((i / total) * width)
        bar = "#" * filled + "-" * (width - filled)
        pct = f"{int((i/total)*100):3d}%"
    sys.stdout.write(f"\r{prefix} [{bar}] {i}/{total} {pct}")
    sys.stdout.flush()

def progress_done():
    sys.stdout.write("\n"); sys.stdout.flush()

# ---------------- Common helpers ----------------

def set_query_param(u: str, key: str, value: str) -> str:
    p = urlparse(u); q = parse_qs(p.query); q[key] = [str(value)]
    new_query = urlencode(q, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, ""))

# ---------------- Crawl (Playwright) ----------------

def crawl_playwright(
    start_url: str,
    dry_run: bool,
    out_root: str,
    source: str,
    username: str,
    max_pages: int,
    post_selector: str,
    article_anchor_selector: str,
    page_template: str,
    paginator_summary_selector: str,
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
    min_bytes: int,
    verbose: bool,
    print_urls: bool,
    max_posts_per_page: int,
    resume: bool,
    show_download_progress: bool,
):
    posts_per_page: List[int] = []
    media_per_page: List[int] = []
    total_downloaded = 0
    total_posts = 0
    total_media = 0

    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    crumbs = Breadcrumbs(out_root, source, username, enable=resume)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        context = p.chromium.launch_persistent_context(
            user_data_dir="",
            headless=not headful,
            user_agent=UA,
        ) if headful else browser.new_context(user_agent=UA)

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
                wait_until_expected_posts(page, post_selector, paginator_summary_selector, selector_timeout_ms, debug)
            except Exception:
                pass

        goto_and_wait(start_url)
        page_index = 1

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
            for attempt in range(1, retry_pages + 1):
                post_links = get_article_post_links(page, post_selector, article_anchor_selector, paginator_summary_selector, selector_timeout_ms, debug)
                if post_links:
                    break
                dprint(debug, f"no posts found (attempt {attempt}/{retry_pages}); retrying")
                time.sleep(0.6); goto_and_wait(page.url)

            if max_posts_per_page > 0 and len(post_links) > max_posts_per_page:
                vlog(verbose, f"⚠️  Capping posts on this page to --max-posts-per-page={max_posts_per_page} (found {len(post_links)})")
                post_links = post_links[:max_posts_per_page]

            posts_count = len(post_links)
            posts_per_page.append(posts_count)
            total_posts += posts_count

            if show_progress:
                progress_draw(f"page {page_index} visiting posts", 0, posts_count)

            page_media_count = 0

            for idx, post_url in enumerate(post_links, start=1):
                if resume and post_url in crumbs.visited_posts:
                    vlog(verbose, f"[page {page_index} post {idx}/{posts_count}] Skipping already-visited: {post_url}")
                    if show_progress:
                        progress_draw(f"page {page_index} visiting posts", idx, posts_count)
                    continue

                vlog(verbose, f"[page {page_index} post {idx}/{posts_count}] Visiting: {post_url}")
                crumbs.mark_post(post_url)

                try:
                    post = context.new_page()
                    post.goto(post_url, wait_until="domcontentloaded", timeout=selector_timeout_ms)
                    if sleep_after_goto > 0: time.sleep(sleep_after_goto)
                    html = post.content()

                    pairs = collect_media_pairs(html, post_url)
                    media_urls_all = [u for (u, _) in pairs]
                    imgs, vids = count_kinds(media_urls_all)
                    vlog(verbose, f"  ├─ found {len(media_urls_all)} media ({imgs} images, {vids} videos)")
                    if verbose and print_urls:
                        for u in media_urls_all:
                            vlog(True, f"  │   {u}")

                    media_urls = size_filter_with_trust(pairs, session, min_bytes, verbose, debug, referer=post_url)
                    imgs, vids = count_kinds(media_urls)
                    vlog(verbose, f"  ├─ kept {len(media_urls)} after size filter ({imgs} images, {vids} videos)")

                    seen: Set[str] = set()
                    k = 0
                    for u in media_urls:
                        if u in seen:
                            continue
                        seen.add(u)

                        if resume and u in crumbs.downloaded_urls:
                            vlog(verbose, f"  ↷ already-downloaded (state): {u}")
                            continue

                        subdir = output_subdir(out_root, source, username, u)
                        fname = filename_from_url(u)
                        # NEW: filesystem-level skip for any variant
                        existing = any_variant_exists(subdir, fname)
                        if existing:
                            vlog(verbose, f"  ↷ already exists (fs): {os.path.basename(existing)}")
                            crumbs.mark_media(u, existing)
                            continue

                        k += 1
                        local = download_file(u, subdir, session, referer=post_url, show_bar=show_download_progress)
                        if local:
                            total_downloaded += 1
                            page_media_count += 1
                            crumbs.mark_media(u, local)
                            if verbose:
                                vlog(True, f"  ✓ saved {k}/{len(media_urls)} → {os.path.join(subdir, os.path.basename(local))}")
                        else:
                            vlog(verbose, f"  ✖ failed {k}/{len(media_urls)} : {u}")

                    total_media += len(media_urls)
                    post.close()
                except Exception as e:
                    vlog(verbose, f"  └─ ERROR visiting post: {e}")
                finally:
                    if show_progress:
                        progress_draw(f"page {page_index} visiting posts", idx, posts_count)

            media_per_page.append(page_media_count)
            if show_progress:
                progress_done()

            cur_val = parse_qs(urlparse(page.url).query).get("o", ["0"])[0]
            cur_offset = int(cur_val) if cur_val.isdigit() else 0
            next_offset = cur_offset + offset_step
            if (max_pages > 0 and page_index >= max_pages) or (offset_max is not None and next_offset > offset_max):
                break
            next_url = page_template.format(offset=next_offset) if page_template else set_query_param(page.url, "o", str(next_offset))
            goto_and_wait(next_url)
            page_index += 1
            time.sleep(max(0.0, polite_delay))

        context.close(); browser.close()

    return {
        "pages": len(posts_per_page),
        "posts_per_page": posts_per_page,
        "media_per_page": media_per_page,
        "total_posts": total_posts,
        "total_media": total_media,
        "total_downloaded": total_downloaded,
    }

# ---------------- Crawl (HTTP-only) ----------------

def crawl_http(
    start_url: str,
    dry_run: bool,
    out_root: str,
    source: str,
    username: str,
    max_pages: int,
    post_selector: str,
    article_anchor_selector: str,
    page_template: str,
    offset_step: int,
    offset_max: Optional[int],
    polite_delay: float,
    debug: bool,
    show_progress: bool,
    min_bytes: int,
    verbose: bool,
    print_urls: bool,
    max_posts_per_page: int,
    resume: bool,
    show_download_progress: bool,
):
    posts_per_page: List[int] = []
    media_per_page: List[int] = []
    total_downloaded = 0
    total_posts = 0
    total_media = 0

    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    crumbs = Breadcrumbs(out_root, source, username, enable=resume)

    p = urlparse(start_url)
    start_o = 0
    q = parse_qs(p.query)
    if "o" in q and q["o"] and q["o"][0].isdigit():
        start_o = int(q["o"][0])

    offsets = [start_o]
    if offset_max is not None:
        cur = start_o
        while True:
            cur += offset_step
            if cur > offset_max: break
            offsets.append(cur)

    for i, off in enumerate(offsets, start=1):
        list_url = page_template.format(offset=off) if page_template else set_query_param(start_url, "o", str(off))
        dprint(debug, f"[HTTP] list url: {list_url}")

        html = http_get(session, list_url, debug)
        if not html: break

        post_links = http_list_post_links(html, list_url, post_selector, article_anchor_selector)

        if max_posts_per_page > 0 and len(post_links) > max_posts_per_page:
            vlog(verbose, f"⚠️  Capping posts on this page to --max-posts-per-page={max_posts_per_page} (found {len(post_links)})")
            post_links = post_links[:max_posts_per_page]

        posts_count = len(post_links)
        posts_per_page.append(posts_count)
        total_posts += posts_count

        if show_progress: progress_draw(f"page {i} visiting posts", 0, posts_count)

        page_media_count = 0
        for idx, post_url in enumerate(post_links, start=1):
            if resume and post_url in crumbs.visited_posts:
                vlog(verbose, f"[page {i} post {idx}/{posts_count}] Skipping already-visited: {post_url}")
                if show_progress: progress_draw(f"page {i} visiting posts", idx, posts_count)
                continue

            vlog(verbose, f"[page {i} post {idx}/{posts_count}] Visiting: {post_url}")
            crumbs.mark_post(post_url)

            html_post = http_get(session, post_url, debug)
            if html_post:
                pairs = collect_media_pairs(html_post, post_url)
                media_urls_all = [u for (u, _) in pairs]
                imgs, vids = count_kinds(media_urls_all)
                vlog(verbose, f"  ├─ found {len(media_urls_all)} media ({imgs} images, {vids} videos)")
                if verbose and print_urls:
                    for u in media_urls_all: vlog(True, f"  │   {u}")

                media_urls = size_filter_with_trust(pairs, session, min_bytes, verbose, debug, referer=post_url)
                imgs, vids = count_kinds(media_urls)
                vlog(verbose, f"  ├─ kept {len(media_urls)} after size filter ({imgs} images, {vids} videos)")

                seen: Set[str] = set()
                k = 0
                for u in media_urls:
                    if u in seen:
                        continue
                    seen.add(u)

                    if resume and u in crumbs.downloaded_urls:
                        vlog(verbose, f"  ↷ already-downloaded (state): {u}")
                        continue

                    subdir = output_subdir(out_root, source, username, u)
                    fname = filename_from_url(u)
                    # NEW: filesystem-level skip for any variant
                    existing = any_variant_exists(subdir, fname)
                    if existing:
                        vlog(verbose, f"  ↷ already exists (fs): {os.path.basename(existing)}")
                        crumbs.mark_media(u, existing)
                        continue

                    k += 1
                    local = download_file(u, subdir, session, referer=post_url, show_bar=show_download_progress)
                    if local:
                        total_downloaded += 1
                        page_media_count += 1
                        crumbs.mark_media(u, local)
                        if verbose:
                            vlog(True, f"  ✓ saved {k}/{len(media_urls)} → {os.path.join(subdir, os.path.basename(local))}")
                    else:
                        vlog(verbose, f"  ✖ failed {k}/{len(media_urls)} : {u}")

                total_media += len(media_urls)
            else:
                vlog(verbose, "  └─ ERROR fetching post")

            if show_progress: progress_draw(f"page {i} visiting posts", idx, posts_count)
            time.sleep(0.05)

        media_per_page.append(page_media_count)
        if show_progress: progress_done()

        if max_pages > 0 and i >= max_pages: break
        time.sleep(max(0.0, polite_delay))

    return {
        "pages": len(posts_per_page),
        "posts_per_page": posts_per_page,
        "media_per_page": media_per_page,
        "total_posts": total_posts,
        "total_media": total_media,
        "total_downloaded": total_downloaded,
    }

# ---------------- CLI ----------------

def main():
    ap = argparse.ArgumentParser(description="Gallery scraper — streaming downloads + per-file progress + resume (no re-downloads).")
    ap.add_argument("url", help="Starting list URL (e.g., ...?o=0)")
    ap.add_argument("--dry-run", action="store_true", help="Only report counts")

    ap.add_argument("--out", default="downloads", help="Parent output directory")
    ap.add_argument("--username", default="", help="Folder under --out/<source>/; if omitted, derived from '/user/<name>' in URL")
    ap.add_argument("--source", default="", help="Top-level under --out; if omitted, derived from path segment before '/user'")

    ap.add_argument("--max-pages", type=int, default=0, help="Limit number of list pages processed (0 = no limit)")
    ap.add_argument("--max-posts-per-page", type=int, default=0, help="Only process first N posts per page (debug)")

    # List-page structure
    ap.add_argument("--post-selector", default=".card-list__items article", help="CSS for post entries on the list page")
    ap.add_argument("--article-anchor-selector", default="a[href]", help="CSS inside each article that links to the post page")

    # Pagination via offset
    ap.add_argument("--page-template", default="", help='Template with {offset}; e.g. "...?o={offset}"')
    ap.add_argument("--offset-step", type=int, default=50, help="Offset increment")
    ap.add_argument("--offset-max", type=int, default=350, help="Stop when offset > this; use -1 to skip cap/auto")

    # Playwright tuning
    ap.add_argument("--headful", action="store_true", help="Visible browser (debug)")
    ap.add_argument("--block-list-media", action="store_true", help="Block heavy assets on list pages")
    ap.add_argument("--retry-pages", type=int, default=2, help="Retries for extracting a list page")
    ap.add_argument("--selector-timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS, help="Timeout for navigation/selectors (ms)")
    ap.add_argument("--sleep-after-goto", type=float, default=0.3, help="Sleep after goto (s)")
    ap.add_argument("--polite-delay", type=float, default=0.4, help="Delay between list pages (s)")
    ap.add_argument("--debug", action="store_true", help="Debug logs to stderr")

    # HTTP-only fallback
    ap.add_argument("--http-mode", action="store_true", help="Use requests/bs4 only (no Playwright)")

    # Progress / Verbosity
    ap.add_argument("--no-progress", action="store_true", help="Disable per-page post progress bar")
    ap.add_argument("--no-download-progress", action="store_true", help="Disable per-file download progress bars")
    ap.add_argument("--verbose", action="store_true", help="Per-post logs")
    ap.add_argument("--print-urls", action="store_true", help="With --verbose, print each media URL found")

    # Size filter
    ap.add_argument("--min-bytes", type=int, default=100_000, help="Skip NON-TRUSTED files smaller than this (via HEAD). 0 disables.")

    # Resume
    ap.add_argument("--no-resume", action="store_true", help="Disable state file and resume behavior")

    args = ap.parse_args()
    page_template = (args.page_template or None)
    show_progress = not args.no_progress
    show_download_progress = not args.no_download_progress
    use_resume = not args.no_resume

    # Resolve source + username
    resolved_source = args.source.strip() or extract_source_from_url(args.url) or "source_unknown"
    resolved_user = args.username.strip() or extract_username_from_url(args.url) or "unknown_user"

    # Ensure folders exist
    ensure_dir(os.path.join(args.out, resolved_source, resolved_user, "images"))
    ensure_dir(os.path.join(args.out, resolved_source, resolved_user, "videos"))

    common = dict(
        start_url=args.url,
        dry_run=args.dry_run,
        out_root=args.out,
        source=resolved_source,
        username=resolved_user,
        max_pages=args.max_pages,
        post_selector=args.post_selector,
        article_anchor_selector=args.article_anchor_selector,
        page_template=page_template or "",
        offset_step=args.offset_step,
        offset_max=args.offset_max if args.offset_max >= 0 else None,
        polite_delay=args.polite_delay,
        debug=args.debug,
        show_progress=show_progress,
        min_bytes=args.min_bytes,
        verbose=args.verbose,
        print_urls=args.print_urls,
        max_posts_per_page=args.max_posts_per_page,
        resume=use_resume,
        show_download_progress=show_download_progress,
    )

    if args.http_mode:
        res = crawl_http(**common)
    else:
        if not PLAYWRIGHT_AVAILABLE:
            print("Playwright is not installed. Either run with --http-mode or install:", file=sys.stderr)
            print("  poetry add playwright && poetry run playwright install chromium", file=sys.stderr)
            sys.exit(1)
        res = crawl_playwright(
            paginator_summary_selector="div.paginator small",
            headful=args.headful,
            block_list_media=args.block_list_media,
            retry_pages=args.retry_pages,
            selector_timeout_ms=args.selector_timeout_ms,
            sleep_after_goto=args.sleep_after_goto,
            **common,
        )

    # Summary
    if args.dry_run:
        print("Dry run summary")
        print(f"Output root: {os.path.join(args.out, resolved_source, resolved_user)}")
        print(f"Pages: {res['pages']}")
        print(f"Posts per page: {res['posts_per_page']}")
        print(f"Media per page: {res['media_per_page']}")
        print(f"Total posts: {res['total_posts']}")
        print(f"Total media: {res['total_media']}")
    else:
        print("Download summary")
        print(f"Output root: {os.path.join(args.out, resolved_source, resolved_user)}")
        print(f"Pages crawled: {res['pages']}")
        print(f"Posts per page: {res['posts_per_page']}")
        print(f"Media per page: {res['media_per_page']}")
        print(f"Total posts: {res['total_posts']}")
        print(f"Total media found: {res['total_media']}")
        print(f"Total media downloaded: {res['total_downloaded']}")

if __name__ == "__main__":
    main()
