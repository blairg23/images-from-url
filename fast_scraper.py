#!/usr/bin/env python3
# fast_scraper.py
# HTTP-only, concurrency-first scraper for gallery-style list → post → media pages.

from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import re
import sys
import time
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

UA = "fast-scraper/1.0 (+https://example.invalid)"
TIMEOUT = 25  # seconds
LIST_HEADERS = {"User-Agent": UA}
POST_HEADERS = {"User-Agent": UA}
DL_CHUNK = 1 << 15  # 32 KiB

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}
VID_EXT = {".mp4", ".webm", ".mkv", ".mov"}

# ---------------------- utility ----------------------

def is_image(u: str) -> bool:
    path = urlparse(u).path.lower()
    _, ext = os.path.splitext(path)
    return ext in IMG_EXT

def is_video(u: str) -> bool:
    path = urlparse(u).path.lower()
    _, ext = os.path.splitext(path)
    return ext in VID_EXT

def is_media(u: str) -> bool:
    return is_image(u) or is_video(u)

def norm_url(u: str) -> str:
    p = urlparse(u)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))

def set_query_param(u: str, key: str, value: str) -> str:
    p = urlparse(u)
    q = parse_qs(p.query)
    q[key] = [str(value)]
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q, doseq=True), ""))

def filename_from_url(u: str) -> str:
    q = parse_qs(urlparse(u).query)
    if "f" in q and q["f"]:
        return q["f"][0]
    name = os.path.basename(urlparse(u).path)
    return name or "file.bin"

def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def extract_username_from_url(u: str) -> Optional[str]:
    m = re.search(r"/user/([^/?#]+)/?", urlparse(u).path)
    return m.group(1) if m else None

def dprint(enabled: bool, *args):
    if enabled:
        print("[debug]", *args, file=sys.stderr, flush=True)

def vlog(enabled: bool, *args):
    if enabled:
        print(*args, flush=True)

# ---------------------- parsing ----------------------

def parse_paginator_summary(html: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Looks for: <div class="paginator"><small>Showing 1 - 50 of 383</small>...</div>
    Returns (page_size, total_items) if found.
    """
    soup = BeautifulSoup(html, "lxml")
    small = soup.select_one("div.paginator small")
    if not small:
        return None, None
    m = re.search(r"Showing\s+(\d+)\s*-\s*(\d+)\s*of\s*(\d+)", small.get_text(strip=True), flags=re.I)
    if not m:
        return None, None
    start, end, total = map(int, (m.group(1), m.group(2), m.group(3)))
    page_size = end - start + 1 if end >= start else None
    return page_size, total

def list_posts_from_html(html: str, base_url: str, post_selector: str, article_anchor_selector: str) -> List[str]:
    """
    Collect post links from the list page.
    Typical: post_selector=".card-list__items article", article_anchor_selector="a[href]"
    """
    soup = BeautifulSoup(html, "lxml")
    posts = soup.select(post_selector)
    links: List[str] = []
    for n in posts:
        for a in n.select(article_anchor_selector):
            href = a.get("href")
            if href:
                links.append(norm_url(urljoin(base_url, href)))
    # de-dupe preserving order
    seen, out = set(), []
    for u in links:
        if u not in seen:
            seen.add(u); out.append(u)
    return out

def collect_media_urls_from_post(html: str, base_url: str) -> List[str]:
    """
    Strict to your markup:
      - Full-res images via anchors inside .post__files
      - Videos via <video><source> (inside .post__files and Fluid Player fallbacks)
      - Fallback to images inside .post__files if anchors absent
    """
    soup = BeautifulSoup(html, "lxml")
    urls: List[str] = []

    # 1) Full-res image anchors inside .post__files (+ any mp4 anchors)
    for a in soup.select(".post__files a.fileThumb.image-link[href], .post__files a[href$='.mp4'], .post__files a[href*='.mp4']"):
        href = a.get("href")
        if not href:
            continue
        u = urljoin(base_url, href)
        if is_media(u):
            urls.append(norm_url(u))

    # 2) Videos via <video><source> (inside .post__files first, then player fallbacks)
    for s in soup.select(".post__files video source[src], .js-fluid-player source[src], .post__video source[src], video source[src]"):
        src = s.get("src")
        if not src:
            continue
        u = urljoin(base_url, src)
        if is_video(u):
            urls.append(norm_url(u))

    # 3) If nothing found, last resort: images within .post__files via data-* or src
    if not urls:
        box = soup.select_one(".post__files")
        if box:
            for img in box.find_all("img"):
                for attr in ["data-full", "data-original", "data-large", "data-image", "data-src", "src"]:
                    val = img.get(attr)
                    if not val:
                        continue
                    u = urljoin(base_url, val)
                    if is_image(u):
                        urls.append(norm_url(u))
                        break

    # de-dupe
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
    return out

# ---------------------- network ----------------------

def http_get(session: requests.Session, url: str, timeout: int, headers: Dict[str, str]) -> Optional[str]:
    try:
        r = session.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.text
    except requests.RequestException:
        return None

def http_head_len(session: requests.Session, url: str, timeout: int, headers: Dict[str, str]) -> Optional[int]:
    try:
        r = session.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        cl = r.headers.get("content-length")
        return int(cl) if cl and cl.isdigit() else None
    except requests.RequestException:
        return None

def download_one(session: requests.Session, url: str, outdir: str, referer: Optional[str], timeout: int) -> Optional[str]:
    ensure_dir(outdir)
    fname = filename_from_url(url)
    path = os.path.join(outdir, fname)
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        path = f"{base}_{n}{ext}"
        n += 1

    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    try:
        with session.get(url, headers=headers, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=DL_CHUNK):
                    if chunk:
                        f.write(chunk)
        return path
    except requests.RequestException:
        return None

# ---------------------- core ----------------------

def build_offsets(first_url: str, first_html: str, offset_step: int, offset_max: Optional[int], max_pages: int, debug: bool) -> List[int]:
    """
    Decide which offsets to visit based on paginator (if available), --offset-max, and --max-pages.
    """
    start_offset = int(parse_qs(urlparse(first_url).query).get("o", ["0"])[0] or 0)
    page_size, total_items = parse_paginator_summary(first_html)

    offsets = [start_offset]

    # If user gave offset_max, honor it (plus max_pages if provided)
    if offset_max is not None:
        cur = start_offset
        while True:
            cur += offset_step
            if cur > offset_max:
                break
            offsets.append(cur)
        if max_pages > 0:
            offsets = offsets[:max_pages]
        return offsets

    # Else, use paginator to infer total pages (if available)
    if total_items and page_size:
        total_pages = (total_items + page_size - 1) // page_size
        if max_pages > 0:
            total_pages = min(total_pages, max_pages)
        offsets = [start_offset + i * offset_step for i in range(total_pages)]
        return offsets

    # Fallback: only the first page, unless max_pages requests more
    if max_pages > 0:
        offsets = [start_offset + i * offset_step for i in range(max_pages)]
    return offsets

def crawl_http(
    start_url: str,
    out_root: str,
    username: str,
    post_selector: str,
    article_anchor_selector: str,
    offset_step: int,
    offset_max: Optional[int],
    max_pages: int,
    workers: int,
    dl_workers: int,
    min_bytes: int,
    verbose: bool,
    print_urls: bool,
    dry_run: bool,
    debug: bool,
):
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    # Ensure output folders exist
    base_user_dir = os.path.join(out_root, username)
    img_dir = os.path.join(base_user_dir, "images")
    vid_dir = os.path.join(base_user_dir, "videos")
    ensure_dir(img_dir); ensure_dir(vid_dir)

    # 1) Decide offsets to visit
    first_html = http_get(session, start_url, TIMEOUT, LIST_HEADERS)
    if first_html is None:
        print("Failed to fetch first list page.", file=sys.stderr)
        sys.exit(2)

    offsets = build_offsets(start_url, first_html, offset_step, offset_max, max_pages, debug)
    dprint(debug, f"Offsets planned: {offsets}")

    # 2) Gather ALL post links across list pages
    list_pages: List[Tuple[int, str]] = [(off, set_query_param(start_url, "o", off)) for off in offsets]
    all_post_links: List[str] = []
    posts_per_page: List[int] = []

    for i, (off, list_url) in enumerate(list_pages, start=1):
        html = first_html if i == 1 and off == int(parse_qs(urlparse(start_url).query).get("o", ["0"])[0] or 0) else http_get(session, list_url, TIMEOUT, LIST_HEADERS)
        if html is None:
            dprint(debug, f"List fetch failed for offset {off}: {list_url}")
            posts_per_page.append(0)
            continue
        post_links = list_posts_from_html(html, list_url, post_selector, article_anchor_selector)
        posts_per_page.append(len(post_links))
        all_post_links.extend(post_links)
        vlog(verbose, f"[list offset {off}] found {len(post_links)} posts")

    # De-duplicate post links globally (some pages can overlap in edge cases)
    seen_posts, unique_posts = set(), []
    for u in all_post_links:
        if u not in seen_posts:
            seen_posts.add(u); unique_posts.append(u)

    if verbose:
        print(f"Total post pages queued: {len(unique_posts)}")

    # 3) Fetch post pages concurrently and extract media URLs
    def fetch_post_and_extract(url: str) -> Tuple[str, List[str]]:
        html = http_get(session, url, TIMEOUT, POST_HEADERS)
        if not html:
            return (url, [])
        media = collect_media_urls_from_post(html, url)
        return (url, media)

    t0 = time.time()
    media_by_post: Dict[str, List[str]] = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for url, media in ex.map(fetch_post_and_extract, unique_posts):
            media_by_post[url] = media
            imgs = sum(1 for m in media if is_image(m))
            vids = sum(1 for m in media if is_video(m))
            if verbose:
                print(f"[post] {url}")
                print(f"  ├─ found {len(media)} media ({imgs} images, {vids} videos)")
                if print_urls:
                    for m in media:
                        print(f"  │   {m}")

    # 4) Flatten + size-filter (optional) + download concurrently
    flat_media: List[Tuple[str, str]] = []  # (media_url, referer_post)
    for post_url, urls in media_by_post.items():
        for u in urls:
            flat_media.append((u, post_url))

    # Dedup media urls (keep first referer)
    seen_media, dedup_media = set(), []
    for u, ref in flat_media:
        if u not in seen_media:
            seen_media.add(u); dedup_media.append((u, ref))

    # Optional HEAD size filter
    if min_bytes > 0:
        kept: List[Tuple[str, str]] = []
        def size_ok(pair: Tuple[str, str]) -> Optional[Tuple[str, str]]:
            u, ref = pair
            headers = {"User-Agent": UA, "Referer": ref}
            sz = http_head_len(session, u, TIMEOUT, headers)
            if sz is None or sz >= min_bytes:
                return (u, ref)
            else:
                if verbose:
                    print(f"  └─ skip small {sz} B (< {min_bytes} B): {u}")
                return None
        with cf.ThreadPoolExecutor(max_workers=min(workers, 16)) as ex:
            for res in ex.map(size_ok, dedup_media):
                if res is not None:
                    kept.append(res)
        dedup_media = kept

    total_images = sum(1 for u, _ in dedup_media if is_image(u))
    total_videos = sum(1 for u, _ in dedup_media if is_video(u))

    if dry_run:
        print("Dry run summary")
        print(f"User folder: {base_user_dir}")
        print(f"Pages: {len(offsets)}")
        print(f"Posts per page: {posts_per_page}")
        print(f"Total posts discovered: {len(unique_posts)}")
        print(f"Total media: {len(dedup_media)} ({total_images} images, {total_videos} videos)")
        return

    def download_pair(pair: Tuple[str, str]) -> Tuple[str, bool]:
        u, ref = pair
        outdir = img_dir if is_image(u) else vid_dir if is_video(u) else base_user_dir
        ok = download_one(session, u, outdir, referer=ref, timeout=max(TIMEOUT, 60))
        return (u, ok is not None)

    downloaded = 0
    total = len(dedup_media)
    if verbose:
        print(f"Starting downloads: {total} files ({total_images} images, {total_videos} videos)")

    with cf.ThreadPoolExecutor(max_workers=dl_workers) as ex:
        for i, (u, success) in enumerate(ex.map(download_pair, dedup_media), start=1):
            downloaded += 1 if success else 0
            if verbose:
                status = "⇩ ok" if success else "✖ fail"
                print(f"[dl {i}/{total}] {status} :: {u}")
            else:
                # lightweight progress line
                if i == total or i % max(1, total // 50) == 0:
                    sys.stdout.write(f"\rDownloading {i}/{total}..."); sys.stdout.flush()

    if not verbose and total > 0:
        sys.stdout.write("\n"); sys.stdout.flush()

    t1 = time.time()
    print("Download summary")
    print(f"User folder: {base_user_dir}")
    print(f"Pages crawled: {len(offsets)}")
    print(f"Posts per page: {posts_per_page}")
    print(f"Total posts discovered: {len(unique_posts)}")
    print(f"Total media found: {len(dedup_media)} ({total_images} images, {total_videos} videos)")
    print(f"Total media downloaded: {downloaded}")
    print(f"Elapsed: {t1 - t0:.1f}s")

# ---------------------- CLI ----------------------

def main():
    ap = argparse.ArgumentParser(description="Fast HTTP-only scraper for gallery-style list/post pages.")
    ap.add_argument("url", help="Starting list URL (e.g., ...?o=0)")

    # Output
    ap.add_argument("--out", default="downloads", help="Parent output directory")
    ap.add_argument("--username", default="", help="Folder under --out; if omitted, derived from '/user/<name>' in URL")

    # List / posts
    ap.add_argument("--post-selector", default=".card-list__items article", help="CSS for post entries on the list page")
    ap.add_argument("--article-anchor-selector", default="a[href]", help="CSS inside each article that links to the post page")

    # Pagination
    ap.add_argument("--offset-step", type=int, default=50, help="Offset increment (e.g., 50)")
    ap.add_argument("--offset-max", type=int, default=-1, help="Max offset to stop at (e.g., 350). Omit/negative to auto.")
    ap.add_argument("--max-pages", type=int, default=0, help="Limit number of list pages (0 = auto/all)")

    # Concurrency
    ap.add_argument("--workers", type=int, default=16, help="Concurrent post fetchers")
    ap.add_argument("--dl-workers", type=int, default=8, help="Concurrent downloads")

    # Behavior
    ap.add_argument("--min-bytes", type=int, default=0, help="Skip files smaller than this via HEAD (0 disables)")
    ap.add_argument("--verbose", action="store_true", help="Per-post logs")
    ap.add_argument("--print-urls", action="store_true", help="With --verbose, print each media URL")
    ap.add_argument("--dry-run", action="store_true", help="Only count; do not download")
    ap.add_argument("--debug", action="store_true", help="Debug logs to stderr")

    args = ap.parse_args()

    resolved_user = args.username.strip() or extract_username_from_url(args.url) or "unknown_user"

    crawl_http(
        start_url=args.url,
        out_root=args.out,
        username=resolved_user,
        post_selector=args.post_selector,
        article_anchor_selector=args.article_anchor_selector,
        offset_step=args.offset_step,
        offset_max=args.offset_max if args.offset_max >= 0 else None,
        max_pages=args.max_pages,
        workers=max(1, args.workers),
        dl_workers=max(1, args.dl_workers),
        min_bytes=max(0, args.min_bytes),
        verbose=args.verbose,
        print_urls=args.print_urls,
        dry_run=args.dry_run,
        debug=args.debug,
    )

if __name__ == "__main__":
    main()
