#!/usr/bin/env python3
"""
Concurrent gallery scraper - Playwright or HTTP-only - with:

- Fast producer/consumer pipeline (N download workers)
- Live "pinned" progress pane at the bottom (no screen flooding)
- One bar per active download; finished bars disappear (configurable)
- Resume via breadcrumbs; skip existing files; size filter
- End-of-run verification and auto-retry
- Global duplicate tracking across posts (with per-URL list of post pages)
- Download stats so we can say: crawler saw 164 videos but only 103 landed
- NEW: failed downloads are persisted to .state.jsonl and retried on the next run

Output layout:
  <out>/<source>/<username>/{images,videos}/...

State layout (.state.jsonl):
  {"type":"post_visited", ...}
  {"type":"media_downloaded", ...}
  {"type":"media_failed", "url":..., "referer":..., "attempts":1}

On next run we read media_failed and try them again (up to --failed-max-attempts).
"""

import argparse
import glob
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Set
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup

# Optional Playwright (used unless --http-mode)
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_MS = 45000
ACCEPTABLE_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}
ACCEPTABLE_VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov"}

# --------------- logging / helpers ---------------

def dprint(enabled: bool, *args):
    if enabled:
        print("[debug]", *args, file=sys.stderr, flush=True)

def vlog(enabled: bool, *args):
    if enabled:
        print(*args, file=sys.stderr, flush=True)

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

def extract_username_from_url(u: str) -> Optional[str]:
    m = re.search(r"/user/([^/?#]+)/?", urlparse(u).path)
    return m.group(1) if m else None

def extract_source_from_url(u: str) -> Optional[str]:
    path = urlparse(u).path.strip("/")
    parts = path.split("/")
    for i in range(len(parts) - 1):
        if parts[i + 1] == "user":
            return parts[i].lower()
    return parts[0].lower() if parts else None

def output_subdir(root_out: str, source: str, username: str, media_url: str) -> str:
    leaf = "images" if is_image_url(media_url) else "videos" if is_video_url(media_url) else "other"
    return os.path.join(root_out, source, username, leaf)

def human_bytes(n: Optional[int]) -> str:
    if n is None:
        return "?"
    v = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if v < 1024.0 or unit == "TB":
            return f"{v:.1f} {unit}" if unit != "B" else f"{int(v)} B"
        v /= 1024.0
    return f"{v:.1f} PB"

def set_query_param(u: str, key: str, value: str) -> str:
    p = urlparse(u)
    q = parse_qs(p.query)
    q[key] = [str(value)]
    return urlunparse(
        (p.scheme, p.netloc, p.path, p.params, urlencode(q, doseq=True), "")
    )

def count_files_with_exts(root: str, exts: set[str]) -> int:
    exts = tuple(e.lower() for e in exts)
    total = 0
    for base, _, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith(exts):
                total += 1
    return total

# --------------- HTTP util ---------------

def head_content_length(url: str, session: requests.Session, referer: Optional[str]) -> Optional[int]:
    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
    if referer:
        headers["Referer"] = referer
    try:
        r = session.head(url, allow_redirects=True, timeout=20, headers=headers)
        cl = r.headers.get("content-length")
        return int(cl) if cl and cl.isdigit() else None
    except requests.RequestException:
        return None

def http_get(session: requests.Session, url: str, debug: bool) -> Optional[str]:
    try:
        r = session.get(url, timeout=45, headers={"User-Agent": UA})
        r.raise_for_status()
        return r.text
    except Exception as e:
        dprint(debug, f"http get failed {url}: {e}")
        return None

# --------------- extraction ---------------

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
        if href:
            u = norm_url(urljoin(base_url, href))
            if is_media_url(u):
                pairs.append((u, True))

    for s in soup.select(VIDEO_SOURCE_FALLBACKS):
        src = s.get("src")
        if src:
            u = norm_url(urljoin(base_url, src))
            if is_video_url(u):
                pairs.append((u, False))

    if not pairs:
        box = soup.select_one(".post__files")
        if box:
            for img in box.find_all("img"):
                for attr in [
                    "data-full",
                    "data-original",
                    "data-large",
                    "data-image",
                    "data-src",
                    "src",
                ]:
                    val = img.get(attr)
                    if val:
                        u = norm_url(urljoin(base_url, val))
                        if is_image_url(u):
                            pairs.append((u, False))
                            break

    seen: Dict[str, bool] = {}
    for u, t in pairs:
        seen[u] = seen.get(u, False) or t
    return [(u, seen[u]) for u in seen.keys()]

def count_kinds(urls: List[str]) -> Tuple[int, int]:
    return sum(1 for u in urls if is_image_url(u)), sum(1 for u in urls if is_video_url(u))

def http_list_post_links(html: str, base_url: str, post_selector: str, article_anchor_selector: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    posts = soup.select(post_selector) if post_selector else soup.find_all("article")
    links: List[str] = []
    for n in posts:
        for a in n.select(article_anchor_selector):
            href = a.get("href")
            if href:
                links.append(norm_url(urljoin(base_url, href)))
    uniq = []
    seen = set()
    for u in links:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq

# --------------- paginator helpers ---------------

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
            timeout=selector_timeout_ms,
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
        }}""",
    )
    uniq = []
    seen = set()
    for href in links:
        u = norm_url(href)
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq

# --------------- skip/paths/resume ---------------

def any_variant_exists(outdir: str, name: str) -> Optional[str]:
    candidate = os.path.join(outdir, name)
    if os.path.exists(candidate):
        return candidate
    base, ext = os.path.splitext(candidate)
    matches = glob.glob(f"{base}_*{ext}")
    return matches[0] if matches else None

def safe_download_path(outdir: str, name: str) -> str:
    path = os.path.join(outdir, name)
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(path):
        path = f"{base}_{i}{ext}"
        i += 1
    return path

class Breadcrumbs:
    """
    State file: <out>/<source>/<user>/.state.jsonl

    Records:
      - {"type": "post_visited", "post": URL}
      - {"type": "media_downloaded", "url": URL, "path": PATH, "referer": POST_URL, "size": BYTES?}
      - {"type": "media_failed", "url": URL, "referer": POST_URL, "attempts": INT}
    """
    def __init__(self, root_out: str, source: str, username: str, enable: bool = True):
        self.enable = enable
        self.dir = os.path.join(root_out, source, username)
        ensure_dir(self.dir)
        self.path = os.path.join(self.dir, ".state.jsonl")
        self.visited_posts: Set[str] = set()
        self.downloaded_urls: Set[str] = set()
        # url -> {"path":..., "referer":..., "size": int|None}
        self.url_records: Dict[str, Dict[str, Optional[int]]] = {}
        # url -> {"referer":..., "attempts": int}
        self.failed_urls: Dict[str, Dict[str, Optional[int]]] = {}
        if enable:
            self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    t = rec.get("type")
                    if t == "post_visited" and rec.get("post"):
                        self.visited_posts.add(rec["post"])
                    elif t == "media_downloaded" and rec.get("url"):
                        self.downloaded_urls.add(rec["url"])
                        entry = {
                            "path": rec.get("path", ""),
                            "referer": rec.get("referer", ""),
                            "size": rec.get("size", None),
                        }
                        self.url_records[rec["url"]] = entry
                    elif t == "media_failed" and rec.get("url"):
                        self.failed_urls[rec["url"]] = {
                            "referer": rec.get("referer", ""),
                            "attempts": int(rec.get("attempts", 1)),
                        }
        except Exception:
            pass

    def _append(self, rec: dict):
        if not self.enable:
            return
        rec = {
            "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            **rec,
        }
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass

    def mark_post(self, url: str):
        self.visited_posts.add(url)
        self._append({"type": "post_visited", "post": url})

    def mark_media(self, url: str, path: str, referer: Optional[str] = "", size: Optional[int] = None):
        self.downloaded_urls.add(url)
        self.url_records[url] = {
            "path": path,
            "referer": referer or "",
            "size": size,
        }
        self._append(
            {
                "type": "media_downloaded",
                "url": url,
                "path": path,
                "referer": referer or "",
                "size": size,
            }
        )

    def mark_failed_media(self, url: str, referer: Optional[str], attempts: int):
        self.failed_urls[url] = {
            "referer": referer or "",
            "attempts": attempts,
        }
        self._append(
            {
                "type": "media_failed",
                "url": url,
                "referer": referer or "",
                "attempts": attempts,
            }
        )

    def bump_failed_media(self, url: str, referer: Optional[str]):
        prev = self.failed_urls.get(url)
        if prev:
            attempts = prev.get("attempts", 0) + 1
        else:
            attempts = 1
        self.mark_failed_media(url, referer, attempts)

    def clear_failed_media(self, url: str):
        if url in self.failed_urls:
            del self.failed_urls[url]
        # we do NOT rewrite the file here, we just stop emitting it in memory

# --------------- progress pane ---------------

class BarState:
    __slots__ = ("label", "total", "downloaded", "ok", "failed", "start_ts", "last_update")

    def __init__(self, label: str, total: Optional[int]):
        self.label = label
        self.total = total
        self.downloaded = 0
        self.ok = False
        self.failed = False
        self.start_ts = time.time()
        self.last_update = 0.0

def human_speed(downloaded: int, start_ts: float) -> str:
    elapsed = max(1e-6, time.time() - start_ts)
    return f"{human_bytes(int(downloaded / elapsed))}/s"

class ProgressRegistry:
    def __init__(self, leave_completed: bool, display_limit: int):
        self._lock = threading.Lock()
        self._bars: Dict[int, BarState] = {}
        self._meta: Dict[int, float] = {}
        self._next_id = 1
        self.leave_completed = leave_completed
        self.display_limit = max(1, display_limit)

    def new_bar(self, label: str, total: Optional[int]) -> int:
        with self._lock:
            bar_id = self._next_id
            self._next_id += 1
            self._bars[bar_id] = BarState(label, total)
            self._meta[bar_id] = time.time()
            return bar_id

    def update(self, bar_id: int, incr: int):
        with self._lock:
            b = self._bars.get(bar_id)
            if not b:
                return
            b.downloaded += incr
            b.last_update = time.time()

    def finish(self, bar_id: int, ok: bool):
        with self._lock:
            b = self._bars.get(bar_id)
            if not b:
                return
            if ok:
                b.ok = True
            else:
                b.failed = True
            b.last_update = time.time()
            if not self.leave_completed:
                del self._bars[bar_id]
                self._meta.pop(bar_id, None)

    def snapshot(self) -> List[Tuple[int, BarState]]:
        with self._lock:
            pairs = list(self._bars.items())
            pairs.sort(
                key=lambda kv: (kv[1].last_update or self._meta.get(kv[0], 0)),
                reverse=True,
            )
            return pairs[: self.display_limit]

def render_line(b: BarState, width: int = 30) -> str:
    total = b.total
    if total and total > 0:
        frac = min(1.0, b.downloaded / total)
        filled = int(frac * width)
        bar = "#" * filled + "-" * (width - filled)
        pct = f"{int(frac * 100):3d}%"
        status = "✓" if b.ok else "✖" if b.failed else "…"
        return (
            f"[{bar}] {pct}  {human_bytes(b.downloaded)}/{human_bytes(total)}  "
            f"{human_speed(b.downloaded, b.start_ts)} {status} {b.label}"
        )
    else:
        dots = min(width, int((b.downloaded / (1 << 18)) % (width + 1)))
        bar = "•" * dots + " " * (width - dots)
        status = "✓" if b.ok else "✖" if b.failed else "…"
        return (
            f"[{bar}]  {human_bytes(b.downloaded)}  "
            f"{human_speed(b.downloaded, b.start_ts)} {status} {b.label}"
        )

class PinnedRenderer(threading.Thread):
    def __init__(self, reg: ProgressRegistry, title: str = "downloads", live: bool = True, interval: float = 0.06, ansi: bool = True):
        super().__init__(daemon=True, name="PinnedRenderer")
        self.reg = reg
        self.live = live
        self.interval = interval
        self.ansi = ansi
        self._stop_evt = threading.Event()
        self._initialized = False
        self._title = title

    def stop(self):
        self._stop_evt.set()

    def _init_pane(self):
        if not self.ansi:
            return
        sys.stdout.write("\n" + "=" * 16 + f" {self._title} " + "=" * 16 + "\n")
        sys.stdout.write("\x1b[s")
        sys.stdout.flush()
        self._initialized = True

    def run(self):
        if not self.live:
            return
        try:
            if self.ansi and not self._initialized:
                self._init_pane()
            while not self._stop_evt.is_set():
                snap = self.reg.snapshot()
                if self.ansi:
                    sys.stdout.write("\x1b[u")
                    sys.stdout.write("\x1b[J")
                    if not snap:
                        sys.stdout.write("(idle)\n")
                    else:
                        for _, b in snap:
                            sys.stdout.write(render_line(b) + "\n")
                    sys.stdout.flush()
                else:
                    sys.stdout.write("== downloads ==\n")
                    for _, b in snap:
                        sys.stdout.write(render_line(b) + "\n")
                    sys.stdout.flush()
                time.sleep(self.interval)
        finally:
            pass

# --------------- download stats ---------------

class DownloadStats:
    def __init__(self, max_failed_urls: int = 200):
        self.lock = threading.Lock()
        self.succeeded_images = 0
        self.succeeded_videos = 0
        self.failed_images = 0
        self.failed_videos = 0
        self.failed_urls: List[str] = []
        self.max_failed_urls = max_failed_urls

    def mark_success(self, url: str):
        with self.lock:
            if is_image_url(url):
                self.succeeded_images += 1
            elif is_video_url(url):
                self.succeeded_videos += 1

    def mark_failure(self, url: str):
        with self.lock:
            if is_image_url(url):
                self.failed_images += 1
            elif is_video_url(url):
                self.failed_videos += 1
            if len(self.failed_urls) < self.max_failed_urls:
                self.failed_urls.append(url)

# --------------- download worker pool ---------------

class DownloadTask:
    __slots__ = ("url", "referer", "outdir")

    def __init__(self, url: str, referer: Optional[str], outdir: str):
        self.url = url
        self.referer = referer
        self.outdir = outdir

def download_worker(
    name: str,
    q: "queue.Queue[Optional[DownloadTask]]",
    reg: ProgressRegistry,
    session: requests.Session,
    crumbs: Breadcrumbs,
    show_bar: bool,
    stats: DownloadStats,
):
    while True:
        task = q.get()
        if task is None:
            q.task_done()
            return
        url = task.url
        outdir = task.outdir
        referer = task.referer

        fname = filename_from_url(url)
        existing = any_variant_exists(outdir, fname)
        if existing:
            try:
                size_on_disk = os.path.getsize(existing)
            except Exception:
                size_on_disk = None
            crumbs.mark_media(url, existing, referer=referer or "", size=size_on_disk)
            crumbs.clear_failed_media(url)
            stats.mark_success(url)
            q.task_done()
            continue

        ensure_dir(outdir)
        path = safe_download_path(outdir, fname)

        headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        if referer:
            headers["Referer"] = referer

        total = None
        if not is_video_url(url):
            try:
                r_head = session.head(url, headers=headers, allow_redirects=True, timeout=20)
                cl = r_head.headers.get("content-length")
                if cl and cl.isdigit():
                    total = int(cl)
            except requests.RequestException:
                pass

        bar_id = reg.new_bar(os.path.basename(path), total) if show_bar else None
        tmp = path + ".part"
        ok = False
        try:
            with session.get(url, headers=headers, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 15):
                        if not chunk:
                            continue
                        f.write(chunk)
                        if bar_id is not None:
                            reg.update(bar_id, len(chunk))
            os.replace(tmp, path)
            ok = True
            try:
                size_on_disk = os.path.getsize(path)
            except Exception:
                size_on_disk = None
            crumbs.mark_media(url, path, referer=referer or "", size=size_on_disk)
            crumbs.clear_failed_media(url)
            stats.mark_success(url)
        except requests.RequestException:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            crumbs.bump_failed_media(url, referer)
            stats.mark_failure(url)
        finally:
            if bar_id is not None:
                reg.finish(bar_id, ok)
        q.task_done()

# --------------- size filter with trust ---------------

def size_filter_with_trust(
    pairs: List[Tuple[str, bool]],
    session: requests.Session,
    min_bytes: int,
    verbose: bool,
    debug: bool,
    referer: Optional[str],
) -> List[str]:
    if min_bytes <= 0:
        return [u for (u, _) in pairs]
    kept: List[str] = []
    for (u, trusted) in pairs:
        if is_video_url(u):
            kept.append(u)
            continue
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

# --------------- crawl stats ---------------

class CrawlTally:
    def __init__(self):
        self.discovered_images = 0
        self.discovered_videos = 0
        self.enqueued_images = 0
        self.enqueued_videos = 0

    @property
    def discovered_total(self) -> int:
        return self.discovered_images + self.discovered_videos

    @property
    def enqueued_total(self) -> int:
        return self.enqueued_images + self.enqueued_videos

# --------------- Crawl (Playwright) ---------------

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
    worker_q: "queue.Queue[Optional[DownloadTask]]",
    reg: ProgressRegistry,
    session: requests.Session,
    crumbs: Breadcrumbs,
    show_download_bars: bool,
    global_seen_urls: Set[str],
    url_posts: Dict[str, Set[str]],
    tally: CrawlTally,
):
    posts_per_page: List[int] = []
    media_per_page: List[int] = []
    total_posts = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        context = (
            p.chromium.launch_persistent_context(
                user_data_dir="",
                headless=not headful,
                user_agent=UA,
            )
            if headful
            else browser.new_context(user_agent=UA)
        )

        if block_list_media:
            def _route(route, request):
                url = request.url.lower()
                if any(
                    url.endswith(ext)
                    for ext in [
                        ".png",
                        ".jpg",
                        ".jpeg",
                        ".gif",
                        ".webp",
                        ".mp4",
                        ".webm",
                        ".mkv",
                        ".mov",
                        ".css",
                        ".woff",
                        ".woff2",
                        ".ttf",
                    ]
                ):
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
                wait_until_expected_posts(
                    page,
                    post_selector,
                    paginator_summary_selector,
                    selector_timeout_ms,
                    debug,
                )
            except Exception:
                pass

        goto_and_wait(start_url)
        page_index = 1

        if offset_max is None and paginator_summary_selector:
            try:
                txt = page.locator(paginator_summary_selector).first.inner_text().strip()
                page_size, total = (
                    parse_paginator_summary_text(txt) if txt else (None, None)
                )
                if page_size and total:
                    steps = ((total - 1) // offset_step)
                    offset_max = steps * offset_step
                    dprint(debug, f"derived offset_max={offset_max} (page_size={page_size}, total={total})")
            except Exception:
                pass

        visited_posts: Set[str] = set(crumbs.visited_posts) if resume else set()

        while True:
            for attempt in range(1, retry_pages + 1):
                post_links = get_article_post_links(
                    page,
                    post_selector,
                    article_anchor_selector,
                    paginator_summary_selector,
                    selector_timeout_ms,
                    debug,
                )
                if post_links:
                    break
                dprint(debug, f"no posts found (attempt {attempt}/{retry_pages}); retrying")
                time.sleep(0.6)
                goto_and_wait(page.url)

            if max_posts_per_page > 0 and len(post_links) > max_posts_per_page:
                vlog(
                    verbose,
                    f"⚠️  Capping posts on this page to --max-posts-per-page={max_posts_per_page} (found {len(post_links)})",
                )
                post_links = post_links[:max_posts_per_page]

            posts_count = len(post_links)
            posts_per_page.append(posts_count)
            total_posts += posts_count

            if show_progress:
                sys.stderr.write(f"[page {page_index}] visiting {posts_count} posts...\n")
                sys.stderr.flush()

            page_media_enqueued = 0

            for idx, post_url in enumerate(post_links, start=1):
                if resume and post_url in visited_posts:
                    vlog(verbose, f"[page {page_index} post {idx}/{posts_count}] Skipping already-visited: {post_url}")
                    continue

                vlog(verbose, f"[page {page_index} post {idx}/{posts_count}] Visiting: {post_url}")
                crumbs.mark_post(post_url)
                visited_posts.add(post_url)

                try:
                    post = context.new_page()
                    post.goto(post_url, wait_until="domcontentloaded", timeout=selector_timeout_ms)
                    if sleep_after_goto > 0:
                        time.sleep(sleep_after_goto)
                    html = post.content()

                    pairs = collect_media_pairs(html, post_url)
                    media_urls_all = [u for (u, _) in pairs]
                    imgs_all, vids_all = count_kinds(media_urls_all)
                    vlog(verbose, f"  ├─ found {len(media_urls_all)} media ({imgs_all} images, {vids_all} videos)")
                    if verbose and print_urls:
                        for u in media_urls_all:
                            vlog(True, f"  │   {u}")

                    media_urls = size_filter_with_trust(
                        pairs,
                        session,
                        min_bytes,
                        verbose,
                        debug,
                        referer=post_url,
                    )
                    imgs, vids = count_kinds(media_urls)
                    tally.discovered_images += imgs
                    tally.discovered_videos += vids
                    vlog(verbose, f"  ├─ kept {len(media_urls)} after size filter ({imgs} images, {vids} videos)")

                    seen_in_post: Set[str] = set()
                    for u in media_urls:
                        if u in seen_in_post:
                            continue
                        seen_in_post.add(u)

                        url_posts.setdefault(u, set()).add(post_url)

                        if u in global_seen_urls:
                            dprint(debug, f"  └─ global-dup (skip enqueue): {u}")
                            continue

                        global_seen_urls.add(u)
                        if not dry_run:
                            subdir = output_subdir(out_root, source, username, u)
                            worker_q.put(DownloadTask(u, post_url, subdir))
                            page_media_enqueued += 1
                        if is_image_url(u):
                            tally.enqueued_images += 1
                        elif is_video_url(u):
                            tally.enqueued_videos += 1

                    post.close()
                except Exception as e:
                    vlog(verbose, f"  └─ ERROR visiting post: {e}")

            media_per_page.append(page_media_enqueued)

            cur_val = parse_qs(urlparse(page.url).query).get("o", ["0"])[0]
            cur_offset = int(cur_val) if cur_val.isdigit() else 0
            next_offset = cur_offset + offset_step
            if (max_pages > 0 and page_index >= max_pages) or (
                offset_max is not None and next_offset > offset_max
            ):
                break
            next_url = (
                page_template.format(offset=next_offset)
                if page_template
                else set_query_param(page.url, "o", str(next_offset))
            )
            goto_and_wait(next_url)
            page_index += 1
            time.sleep(max(0.0, polite_delay))

        context.close()
        browser.close()

    return {
        "pages": len(posts_per_page),
        "posts_per_page": posts_per_page,
        "media_per_page": media_per_page,
        "total_posts": total_posts,
    }

# --------------- Crawl (HTTP-only) ---------------

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
    worker_q: "queue.Queue[Optional[DownloadTask]]",
    reg: ProgressRegistry,
    session: requests.Session,
    crumbs: Breadcrumbs,
    show_download_bars: bool,
    global_seen_urls: Set[str],
    url_posts: Dict[str, Set[str]],
    tally: CrawlTally,
):
    posts_per_page: List[int] = []
    media_per_page: List[int] = []
    total_posts = 0

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
            if cur > offset_max:
                break
            offsets.append(cur)

    visited_posts: Set[str] = set(crumbs.visited_posts) if resume else set()

    for i, off in enumerate(offsets, start=1):
        list_url = (
            page_template.format(offset=off)
            if page_template
            else set_query_param(start_url, "o", str(off))
        )
        dprint(debug, f"[HTTP] list url: {list_url}")

        html = http_get(session, list_url, debug)
        if not html:
            break

        post_links = http_list_post_links(
            html, list_url, post_selector, article_anchor_selector
        )

        if max_posts_per_page > 0 and len(post_links) > max_posts_per_page:
            vlog(
                verbose,
                f"⚠️  Capping posts on this page to --max-posts-per-page={max_posts_per_page} (found {len(post_links)})",
            )
            post_links = post_links[:max_posts_per_page]

        posts_count = len(post_links)
        posts_per_page.append(posts_count)
        total_posts += posts_count

        if show_progress:
            sys.stderr.write(f"[page {i}] visiting {posts_count} posts...\n")
            sys.stderr.flush()

        page_media_enqueued = 0

        for idx, post_url in enumerate(post_links, start=1):
            if resume and post_url in visited_posts:
                vlog(verbose, f"[page {i} post {idx}/{posts_count}] Skipping already-visited: {post_url}")
                continue

            vlog(verbose, f"[page {i} post {idx}/{posts_count}] Visiting: {post_url}")
            crumbs.mark_post(post_url)
            visited_posts.add(post_url)

            html_post = http_get(session, post_url, debug)
            if html_post:
                pairs = collect_media_pairs(html_post, post_url)
                media_urls_all = [u for (u, _) in pairs]
                imgs_all, vids_all = count_kinds(media_urls_all)
                vlog(verbose, f"  ├─ found {len(media_urls_all)} media ({imgs_all} images, {vids_all} videos)")
                if verbose and print_urls:
                    for u in media_urls_all:
                        vlog(True, f"  │   {u}")

                media_urls = size_filter_with_trust(
                    pairs,
                    session,
                    min_bytes,
                    verbose,
                    debug,
                    referer=post_url,
                )
                imgs, vids = count_kinds(media_urls)
                tally.discovered_images += imgs
                tally.discovered_videos += vids
                vlog(verbose, f"  ├─ kept {len(media_urls)} after size filter ({imgs} images, {vids} videos)")

                seen_in_post: Set[str] = set()
                for u in media_urls:
                    if u in seen_in_post:
                        continue
                    seen_in_post.add(u)

                    url_posts.setdefault(u, set()).add(post_url)

                    if u in global_seen_urls:
                        dprint(debug, f"  └─ global-dup (skip enqueue): {u}")
                        continue

                    global_seen_urls.add(u)

                    if not dry_run:
                        subdir = output_subdir(out_root, source, username, u)
                        worker_q.put(DownloadTask(u, post_url, subdir))
                        page_media_enqueued += 1
                    if is_image_url(u):
                        tally.enqueued_images += 1
                    elif is_video_url(u):
                        tally.enqueued_videos += 1
            else:
                vlog(verbose, "  └─ ERROR fetching post")

            time.sleep(0.02)

        media_per_page.append(page_media_enqueued)

        if max_pages > 0 and i >= max_pages:
            break
        time.sleep(max(0.0, polite_delay))

    return {
        "pages": len(posts_per_page),
        "posts_per_page": posts_per_page,
        "media_per_page": media_per_page,
        "total_posts": total_posts,
    }

# --------------- verification + retry ---------------

def file_exists_for_url(out_root: str, source: str, username: str, url: str) -> Optional[str]:
    subdir = output_subdir(out_root, source, username, url)
    name = filename_from_url(url)
    return any_variant_exists(subdir, name)

def retry_missing_downloads(
    missing_urls: List[str],
    out_root: str,
    source: str,
    username: str,
    session: requests.Session,
    crumbs: Breadcrumbs,
    verbose: bool,
    stats: Optional[DownloadStats] = None,
) -> Tuple[List[str], List[str]]:
    ok: List[str] = []
    bad: List[str] = []
    for u in missing_urls:
        subdir = output_subdir(out_root, source, username, u)
        ensure_dir(subdir)
        fname = filename_from_url(u)
        if any_variant_exists(subdir, fname):
            ok.append(u)
            crumbs.clear_failed_media(u)
            if stats:
                stats.mark_success(u)
            continue
        headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        referer = (crumbs.url_records.get(u) or {}).get("referer") or ""
        if referer:
            headers["Referer"] = referer
        path = safe_download_path(subdir, fname)
        tmp = path + ".part"
        try:
            if verbose:
                vlog(True, f"  ↻ retrying: {u}")
            with session.get(u, headers=headers, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 15):
                        if chunk:
                            f.write(chunk)
            os.replace(tmp, path)
            try:
                size_on_disk = os.path.getsize(path)
            except Exception:
                size_on_disk = None
            crumbs.mark_media(u, path, referer=referer, size=size_on_disk)
            crumbs.clear_failed_media(u)
            ok.append(u)
            if stats:
                stats.mark_success(u)
        except Exception as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            bad.append(u)
            crumbs.bump_failed_media(u, referer)
            if stats:
                stats.mark_failure(u)
            vlog(True, f"  ✖ retry failed: {u}  ({e})")
    return ok, bad

# --------------- small audit helpers ---------------

def to_media_rel(path: str) -> Optional[str]:
    path = path.replace("\\", "/")
    m = re.search(r"(images|videos)/.+", path)
    if not m:
        return None
    return m.group(0)

def load_state_paths(state_path: str) -> Set[str]:
    seen: Set[str] = set()
    if not os.path.exists(state_path):
        return seen
    with open(state_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "media_downloaded":
                continue
            p = rec.get("path") or ""
            rel = to_media_rel(p)
            if rel:
                seen.add(rel)
    return seen

def list_disk_media(root_dir: str) -> Set[str]:
    disk: Set[str] = set()
    for base, _, files in os.walk(root_dir):
        for fn in files:
            full = os.path.join(base, fn).replace("\\", "/")
            rel = to_media_rel(full)
            if rel:
                disk.add(rel)
    return disk

# --------------- CLI ---------------

def main():
    ap = argparse.ArgumentParser(
        description="Concurrent gallery scraper with pinned pane, resume, verification, audit, and persisted failed downloads."
    )
    ap.add_argument("url", help="Starting list URL (for example ...?o=0)")
    ap.add_argument("--dry-run", action="store_true", help="Only report counts (no downloads)")

    ap.add_argument("--out", default="downloads", help="Parent output directory")
    ap.add_argument("--username", default="", help="Folder under --out/<source>/")
    ap.add_argument("--source", default="", help="Top-level under --out")

    ap.add_argument("--max-pages", type=int, default=0, help="Limit number of list pages processed (0 means no limit)")
    ap.add_argument("--max-posts-per-page", type=int, default=0, help="Only process first N posts per page")

    ap.add_argument("--post-selector", default=".card-list__items article", help="CSS for post entries on the list page")
    ap.add_argument("--article-anchor-selector", default="a[href]", help="CSS inside each article that links to the post page")

    ap.add_argument("--page-template", default="", help="Template with {offset}, for example ...?o={offset}")
    ap.add_argument("--offset-step", type=int, default=50, help="Offset increment")
    ap.add_argument("--offset-max", type=int, default=350, help="Stop when offset is greater than this; use -1 to skip")

    ap.add_argument("--headful", action="store_true", help="Visible browser")
    ap.add_argument("--block-list-media", action="store_true", help="Block heavy assets on list pages")
    ap.add_argument("--retry-pages", type=int, default=2, help="Retries for extracting a list page")
    ap.add_argument("--selector-timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS, help="Timeout for navigation/selectors (ms)")
    ap.add_argument("--sleep-after-goto", type=float, default=0.3, help="Sleep after goto in seconds")
    ap.add_argument("--polite-delay", type=float, default=0.4, help="Delay between list pages (seconds)")
    ap.add_argument("--debug", action="store_true", help="Debug logs to stderr")

    ap.add_argument("--http-mode", action="store_true", help="Use requests/bs4 only")

    ap.add_argument("--verbose", action="store_true", help="Per-post logs to stderr")
    ap.add_argument("--print-urls", action="store_true", help="With --verbose, print each media URL found")
    ap.add_argument("--no-live-pane", action="store_true", help="Disable live pinned progress pane")
    ap.add_argument("--no-ansi", action="store_true", help="Disable ANSI control in renderer")
    ap.add_argument("--pane-lines", type=int, default=8, help="Max number of active bars shown in the pane")
    ap.add_argument("--leave-completed", action="store_true", help="Keep completed bars visible")

    ap.add_argument("--min-bytes", type=int, default=100_000, help="Skip NON-TRUSTED files smaller than this via HEAD")

    ap.add_argument("--no-resume", action="store_true", help="Disable state file and resume behavior")

    ap.add_argument("--workers", type=int, default=6, help="Number of parallel download workers")

    ap.add_argument("--failed-max-attempts", type=int, default=3, help="Retry previously failed URLs up to this many times over multiple runs")

    args = ap.parse_args()
    page_template = args.page_template or None
    use_resume = not args.no_resume

    resolved_source = (
        args.source.strip()
        or extract_source_from_url(args.url)
        or "source_unknown"
    )
    resolved_user = (
        args.username.strip()
        or extract_username_from_url(args.url)
        or "unknown_user"
    )

    ensure_dir(os.path.join(args.out, resolved_source, resolved_user, "images"))
    ensure_dir(os.path.join(args.out, resolved_source, resolved_user, "videos"))

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    crumbs = Breadcrumbs(args.out, resolved_source, resolved_user, enable=use_resume)
    prev_downloaded = set(crumbs.downloaded_urls)

    reg = ProgressRegistry(
        leave_completed=args.leave_completed,
        display_limit=args.pane_lines,
    )
    renderer: Optional[PinnedRenderer] = None
    try:
        renderer = PinnedRenderer(
            reg,
            title="downloads",
            live=not args.no_live_pane,
            ansi=not args.no_ansi,
        )
        renderer.start()
    except RuntimeError as e:
        sys.stderr.write(f"[warn] live pane disabled: {e}\n")
        renderer = None

    qdl: "queue.Queue[Optional[DownloadTask]]" = queue.Queue(maxsize=args.workers * 2)
    stats = DownloadStats()
    workers: List[threading.Thread] = []
    for i in range(args.workers):
        t = threading.Thread(
            target=download_worker,
            args=(
                f"W{i+1}",
                qdl,
                reg,
                session,
                crumbs,
                not args.no_live_pane,
                stats,
            ),
            daemon=True,
        )
        t.start()
        workers.append(t)

    # enqueue previously failed media (from past runs)
    if not args.dry_run and crumbs.failed_urls:
        for url, meta in crumbs.failed_urls.items():
            attempts = int(meta.get("attempts", 1))
            if attempts >= args.failed_max_attempts:
                continue
            subdir = output_subdir(args.out, resolved_source, resolved_user, url)
            qdl.put(DownloadTask(url, meta.get("referer") or "", subdir))

    global_seen_urls: Set[str] = set()
    url_posts: Dict[str, Set[str]] = {}
    tally = CrawlTally()

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
        show_progress=True,
        min_bytes=args.min_bytes,
        verbose=args.verbose,
        print_urls=args.print_urls,
        max_posts_per_page=args.max_posts_per_page,
        resume=use_resume,
        worker_q=qdl,
        reg=reg,
        session=session,
        crumbs=crumbs,
        show_download_bars=not args.no_live_pane,
        global_seen_urls=global_seen_urls,
        url_posts=url_posts,
        tally=tally,
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

    if not args.dry_run:
        for _ in workers:
            qdl.put(None)
        qdl.join()
        for t in workers:
            t.join(timeout=1)

    if renderer is not None:
        renderer.stop()
        try:
            renderer.join(timeout=1)
        except RuntimeError:
            pass

    duplicate_urls = {
        u: sorted(list(posts))
        for u, posts in url_posts.items()
        if len(posts) > 1
    }
    duplicate_url_count = len(duplicate_urls)
    duplicate_occurrences = tally.discovered_total - tally.enqueued_total

    output_root = os.path.join(args.out, resolved_source, resolved_user)

    if args.dry_run:
        print("Dry run summary")
        print(f"Output root: {output_root}")
        print(f"Pages: {res['pages']}")
        print(f"Posts per page: {res['posts_per_page']}")
        print(f"Media enqueued per page: {res['media_per_page']}")
        print(f"Total posts: {res['total_posts']}")
        print(f"Total media found: {tally.discovered_total}  (images: {tally.discovered_images}, videos: {tally.discovered_videos})")
        print(f"Total unique media enqueued: {tally.enqueued_total}  (images: {tally.enqueued_images}, videos: {tally.enqueued_videos})")
        print(f"Duplicates across posts (urls): {duplicate_url_count}  (duplicate occurrences skipped: {duplicate_occurrences})")

        state_path = os.path.join(output_root, ".state.jsonl")
        state_media = load_state_paths(state_path)
        disk_media = list_disk_media(output_root)
        extra_on_disk = sorted(disk_media - state_media)
        missing_on_disk = sorted(state_media - disk_media)
        print("\nAudit (dry run)")
        print("---------------")
        print(f"Tracked in state: {len(state_media)}")
        print(f"Found on disk:    {len(disk_media)}")
        print(f"Extra on disk (not in state): {len(extra_on_disk)}")
        for f in extra_on_disk:
            print("   ", f)
        print(f"Missing on disk (in state but not on disk): {len(missing_on_disk)}")
        for f in missing_on_disk[:200]:
            print("   ", f)

        # also show failed still in state
        if crumbs.failed_urls:
            print("\nFailed media still in state:")
            for u, meta in crumbs.failed_urls.items():
                print(f"  {u}  (attempts: {meta.get('attempts', 1)}, referer: {meta.get('referer','')})")
        return

    print("Download summary")
    print(f"Output root: {output_root}")
    print(f"Pages crawled: {res['pages']}")
    print(f"Posts per page: {res['posts_per_page']}")
    print(f"Media enqueued per page: {res['media_per_page']}")
    print(f"Total posts: {res['total_posts']}")
    print(f"Total media found: {tally.discovered_total}  (images: {tally.discovered_images}, videos: {tally.discovered_videos})")
    print(f"Total unique media enqueued: {tally.enqueued_total}  (images: {tally.enqueued_images}, videos: {tally.enqueued_videos})")
    print(f"Duplicates across posts (urls): {duplicate_url_count}  (duplicate occurrences skipped: {duplicate_occurrences})")

    images_dir = os.path.join(output_root, "images")
    videos_dir = os.path.join(output_root, "videos")

    all_dl = set(crumbs.downloaded_urls)
    all_expected_images = sum(1 for u in all_dl if is_image_url(u))
    all_expected_videos = sum(1 for u in all_dl if is_video_url(u))

    new_dl = all_dl - prev_downloaded
    run_expected_images = sum(1 for u in new_dl if is_image_url(u))
    run_expected_videos = sum(1 for u in new_dl if is_video_url(u))

    actual_images = count_files_with_exts(images_dir, ACCEPTABLE_IMAGE_EXT)
    actual_videos = count_files_with_exts(videos_dir, ACCEPTABLE_VIDEO_EXT)

    print("\nVerification")
    print("------------")
    print("This run expected:")
    print(f"  images: {run_expected_images}")
    print(f"  videos: {run_expected_videos}")
    print("All-time expected (per breadcrumbs):")
    print(f"  images: {all_expected_images}")
    print(f"  videos: {all_expected_videos}")
    print("On disk now:")
    print(f"  images: {actual_images}  ({images_dir})")
    print(f"  videos: {actual_videos}  ({videos_dir})")

    # show gap between enqueued vs actually recorded
    missing_from_download_layer_images = max(0, tally.enqueued_images - run_expected_images)
    missing_from_download_layer_videos = max(0, tally.enqueued_videos - run_expected_videos)
    if missing_from_download_layer_images or missing_from_download_layer_videos:
        print("\nDownload gap (crawler vs actual downloaded):")
        print(f"  images missing: {missing_from_download_layer_images} (crawler saw {tally.enqueued_images}, breadcrumbs got {run_expected_images})")
        print(f"  videos missing: {missing_from_download_layer_videos} (crawler saw {tally.enqueued_videos}, breadcrumbs got {run_expected_videos})")

    missing_urls: List[str] = []
    for u in all_dl:
        if not file_exists_for_url(args.out, resolved_source, resolved_user, u):
            missing_urls.append(u)

    if missing_urls:
        print(f"\n⚠️  Missing files detected: {len(missing_urls)}")
        max_show = 100
        for i, u in enumerate(missing_urls[:max_show], start=1):
            print(f"  {i:3d}. {u}")
        if len(missing_urls) > max_show:
            print(f"  ... and {len(missing_urls) - max_show} more")

        print("\nAttempting automatic re-download of missing files...")
        ok_urls, bad_urls = retry_missing_downloads(
            missing_urls,
            out_root=args.out,
            source=resolved_source,
            username=resolved_user,
            session=session,
            crumbs=crumbs,
            verbose=True,
            stats=stats,
        )

        actual_images = count_files_with_exts(images_dir, ACCEPTABLE_IMAGE_EXT)
        actual_videos = count_files_with_exts(videos_dir, ACCEPTABLE_VIDEO_EXT)
        print("\nPost-retry folder counts:")
        print(f"  images: {actual_images}  ({images_dir})")
        print(f"  videos: {actual_videos}  ({videos_dir})")

        if bad_urls:
            print(f"\n❌ Still missing after retry: {len(bad_urls)}")
            for i, u in enumerate(bad_urls[:max_show], start=1):
                print(f"  {i:3d}. {u}")
            if len(bad_urls) > max_show:
                print(f"  ... and {len(bad_urls) - max_show} more")
            print("   (These may be transient CDN issues, removed sources, or blocked by the remote host.)")
        else:
            print("\n✅ All previously missing files were recovered.")
    else:
        print("\n✅ Folder contents match breadcrumbs. Looks tight.")

    # final audit against disk
    state_path = os.path.join(output_root, ".state.jsonl")
    state_media = load_state_paths(state_path)
    disk_media = list_disk_media(output_root)
    extra_on_disk = sorted(disk_media - state_media)
    missing_on_disk = sorted(state_media - disk_media)

    print("\nAudit")
    print("------")
    print(f"Extra files on disk (not in state): {len(extra_on_disk)}")
    for f in extra_on_disk[:200]:
        print("   ", f)
    print(f"Missing files on disk (in state but not on disk): {len(missing_on_disk)}")
    for f in missing_on_disk[:200]:
        print("   ", f)

    if crumbs.failed_urls:
        print("\nFailed media still in state:")
        for u, meta in crumbs.failed_urls.items():
            print(f"  {u}  (attempts: {meta.get('attempts', 1)}, referer: {meta.get('referer','')})")

    if duplicate_url_count:
        print(f"\nDuplicate URLs across posts: {duplicate_url_count}  (occurrences skipped: {duplicate_occurrences})")
        max_urls = 50
        max_posts = 5
        for i, (u, posts) in enumerate(list(duplicate_urls.items())[:max_urls], start=1):
            print(f"    {i}. {u}")
            sposts = list(posts)
            for j, purl in enumerate(sposts[:max_posts], start=1):
                print(f"       - post {j}: {purl}")
            morep = len(sposts) - len(sposts[:max_posts])
            if morep > 0:
                print(f"       ... and {morep} more post(s)")
    else:
        print("\nDuplicate URLs across posts: 0")

    # download stats
    print("\nDownload stats")
    print("--------------")
    print(f"succeeded images: {stats.succeeded_images}")
    print(f"succeeded videos: {stats.succeeded_videos}")
    print(f"failed images:    {stats.failed_images}")
    print(f"failed videos:    {stats.failed_videos}")
    if stats.failed_urls:
        print("failed urls (sample):")
        for u in stats.failed_urls[:100]:
            print("   ", u)

if __name__ == "__main__":
    main()
