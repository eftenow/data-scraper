#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import re
import time
from collections import deque
from html import unescape as html_unescape
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse, urldefrag

import requests
from bs4 import BeautifulSoup

try:
    from build_catalog import build_catalog, render_index_html, write_catalog_csvs
except Exception:
    build_catalog = None
    render_index_html = None
    write_catalog_csvs = None

VIDEO_EXTENSIONS = (".mp4", ".webm", ".m4v", ".mov", ".ogv")
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
TRACKING_QUERY_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
}


def canonicalize_url(url: str, keep_query_params: bool = False) -> str:
    cleaned, _fragment = urldefrag(url)
    parsed = urlparse(cleaned.strip())

    query = ""
    if keep_query_params and parsed.query:
        pairs = [(k, v) for k, v in parse_qsl(parsed.query,
                                              keep_blank_values=True) if k not in TRACKING_QUERY_PARAMS]
        query = urlencode(pairs, doseq=True)

    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")

    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"}


def same_domain(url: str, root_domain: str) -> bool:
    return urlparse(url).netloc == root_domain


def to_filename(url: str, suffix: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"{digest}{suffix}"


def looks_like_video_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in VIDEO_EXTENSIONS)


def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    ordered = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def extract_video_urls(soup: BeautifulSoup, page_url: str) -> list[str]:
    found = []

    for node in soup.find_all(attrs={"data-attributes": True}):
        raw = node.get("data-attributes")
        if not raw:
            continue
        try:
            payload = json.loads(html_unescape(raw))
        except json.JSONDecodeError:
            continue

        source = payload.get("source")
        if isinstance(source, str) and source.strip():
            found.append(canonicalize_url(
                urljoin(page_url, source.strip()), keep_query_params=True))
        elif isinstance(source, list):
            for s in source:
                if isinstance(s, str) and s.strip():
                    found.append(canonicalize_url(
                        urljoin(page_url, s.strip()), keep_query_params=True))

    for node in soup.find_all("video"):
        src = node.get("src")
        if src:
            found.append(canonicalize_url(
                urljoin(page_url, src), keep_query_params=True))

    for node in soup.find_all("source"):
        src = node.get("src")
        src_type = (node.get("type") or "").lower()
        if src and (src_type.startswith("video/") or looks_like_video_url(src)):
            found.append(canonicalize_url(
                urljoin(page_url, src), keep_query_params=True))

    for node in soup.find_all("a", href=True):
        href = canonicalize_url(
            urljoin(page_url, node["href"]), keep_query_params=True)
        if looks_like_video_url(href) or any(
            provider in href for provider in ("youtube.com", "youtu.be", "vimeo.com", "dailymotion.com")
        ):
            found.append(href)

    for node in soup.find_all("iframe"):
        src = node.get("src") or node.get(
            "data-src") or node.get("data-lazy-src")
        if not src:
            continue
        absolute = canonicalize_url(
            urljoin(page_url, src), keep_query_params=True)
        if any(provider in absolute for provider in ("youtube.com", "youtu.be", "vimeo.com", "dailymotion.com")):
            found.append(absolute)

    return [url for url in dedupe_preserve_order(found) if is_http_url(url)]


def parse_html(url: str, html: str, keep_query_params: bool) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    desc_tag = soup.find(
        "meta", attrs={"name": re.compile(r"^description$", re.I)})
    description = (desc_tag.get("content") or "").strip() if desc_tag else ""
    h1 = [node.get_text(" ", strip=True) for node in soup.find_all("h1")]
    h2 = [node.get_text(" ", strip=True) for node in soup.find_all("h2")]
    text = soup.get_text("\n", strip=True)

    links = []
    for node in soup.find_all("a", href=True):
        absolute = canonicalize_url(
            urljoin(url, node["href"]), keep_query_params=keep_query_params)
        if is_http_url(absolute):
            links.append(absolute)

    video_urls = extract_video_urls(soup, url)

    return {
        "url": url,
        "title": title,
        "description": description,
        "h1": h1,
        "h2": h2,
        "text_chars": len(text),
        "links_count": len(links),
        "links": links,
        "video_urls": video_urls,
        "video_count": len(video_urls),
    }


def should_visit(
    url: str,
    root_domain: str,
    include_pattern: Optional[re.Pattern],
    exclude_pattern: Optional[re.Pattern],
    skip_noise: bool,
) -> bool:
    if not is_http_url(url):
        return False
    if not same_domain(url, root_domain):
        return False
    if include_pattern and not include_pattern.search(url):
        return False
    if exclude_pattern and exclude_pattern.search(url):
        return False

    if skip_noise:
        path = urlparse(url).path.lower()
        if any(
            token in path
            for token in (
                "/wp-admin",
                "/wp-login.php",
                "/xmlrpc.php",
                "/feed",
                "/comments",
                "/logout",
                "/replytocom",
            )
        ):
            return False
    return True


def apply_cookie_string(session: requests.Session, cookie_string: str) -> None:
    cookie = SimpleCookie()
    cookie.load(cookie_string)
    for morsel in cookie.values():
        session.cookies.set(morsel.key, morsel.value)


def fetch_with_retries(
    session: requests.Session,
    url: str,
    timeout_seconds: float,
    max_retries: int,
    retry_backoff: float,
) -> tuple[Optional[int], str, str]:
    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            response = session.get(url, timeout=timeout_seconds)
            status_code = response.status_code
            if status_code in RETRYABLE_STATUS_CODES and attempt < max_retries:
                time.sleep(retry_backoff * (2**attempt))
                continue
            response.raise_for_status()
            return status_code, response.text, ""
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < max_retries:
                time.sleep(retry_backoff * (2**attempt))
                continue
            return None, "", last_error

    return None, "", last_error


def infer_video_suffix(video_url: str, content_type: str) -> str:
    path = urlparse(video_url).path.lower()
    for ext in VIDEO_EXTENSIONS:
        if path.endswith(ext):
            return ext
    if "webm" in content_type:
        return ".webm"
    if "ogg" in content_type:
        return ".ogv"
    if "quicktime" in content_type:
        return ".mov"
    return ".mp4"


def download_video(
    session: requests.Session,
    video_url: str,
    videos_dir: Path,
    timeout_seconds: float,
    max_video_mb: int,
    max_retries: int,
    retry_backoff: float,
) -> tuple[str, str]:
    max_bytes = max_video_mb * 1024 * 1024
    last_error = ""

    for attempt in range(max_retries + 1):
        try:
            with session.get(video_url, timeout=timeout_seconds, stream=True) as response:
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < max_retries:
                    time.sleep(retry_backoff * (2**attempt))
                    continue
                response.raise_for_status()
                content_type = (response.headers.get(
                    "Content-Type") or "").lower()
                suffix = infer_video_suffix(video_url, content_type)
                output_path = videos_dir / to_filename(video_url, suffix)

                total = 0
                with output_path.open("wb") as out_f:
                    for chunk in response.iter_content(chunk_size=1024 * 64):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            out_f.close()
                            output_path.unlink(missing_ok=True)
                            return "", f"video exceeds max size ({max_video_mb} MB)"
                        out_f.write(chunk)

                return str(output_path), ""
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < max_retries:
                time.sleep(retry_backoff * (2**attempt))
                continue
            return "", last_error

    return "", last_error


def load_existing_visited(index_path: Path) -> set[str]:
    visited = set()
    if not index_path.exists():
        return visited

    with index_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                url = obj.get("url")
                if url:
                    visited.add(url)
            except json.JSONDecodeError:
                continue
    return visited


def run_crawl(
    start_url: str,
    out_dir: Path,
    max_pages: int,
    max_depth: int,
    delay_seconds: float,
    timeout_seconds: float,
    max_retries: int,
    retry_backoff: float,
    include_regex: Optional[str],
    exclude_regex: Optional[str],
    user_agent: str,
    cookie_string: Optional[str],
    cookie_file: Optional[Path],
    keep_query_params: bool,
    skip_noise_urls: bool,
    resume: bool,
    download_videos: bool,
    max_videos_per_page: int,
    max_video_mb: int,
    auto_build_catalog: bool,
    catalog_seed_url: Optional[str],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "raw"
    videos_dir = out_dir / "videos"
    raw_dir.mkdir(exist_ok=True)
    if download_videos:
        videos_dir.mkdir(exist_ok=True)

    index_path = out_dir / "crawl_index.jsonl"
    parsed_jsonl_path = out_dir / "parsed_records.jsonl"
    parsed_csv_path = out_dir / "parsed_records.csv"
    video_manifest_path = out_dir / "video_manifest.csv"

    root_domain = urlparse(start_url).netloc
    include_pattern = re.compile(include_regex) if include_regex else None
    exclude_pattern = re.compile(exclude_regex) if exclude_regex else None

    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    if cookie_file:
        cookie_text = cookie_file.read_text(encoding="utf-8").strip()
        if cookie_text:
            apply_cookie_string(session, cookie_text)
    if cookie_string:
        apply_cookie_string(session, cookie_string)

    start_url = canonicalize_url(
        start_url, keep_query_params=keep_query_params)
    queue = deque([(start_url, 0)])
    enqueued = {start_url}
    visited = load_existing_visited(index_path) if resume else set()
    crawled = 0

    mode = "a" if resume else "w"
    write_headers = not (resume and parsed_csv_path.exists()
                         and video_manifest_path.exists())

    stats = {
        "queued": 1,
        "skipped_depth": 0,
        "skipped_filter": 0,
        "errors": 0,
        "video_found": 0,
        "video_downloaded": 0,
    }

    with index_path.open(mode, encoding="utf-8") as index_f, parsed_jsonl_path.open(
        mode, encoding="utf-8"
    ) as parsed_jsonl_f, parsed_csv_path.open(mode, encoding="utf-8", newline="") as parsed_csv_f, video_manifest_path.open(
        mode, encoding="utf-8", newline=""
    ) as video_manifest_f:
        csv_writer = csv.DictWriter(
            parsed_csv_f,
            fieldnames=[
                "url",
                "status_code",
                "title",
                "description",
                "h1_count",
                "h2_count",
                "text_chars",
                "links_count",
                "video_count",
                "downloaded_video_count",
                "raw_html_path",
                "error",
            ],
        )
        video_writer = csv.DictWriter(
            video_manifest_f,
            fieldnames=[
                "page_url",
                "video_url",
                "download_attempted",
                "downloaded_file_path",
                "error",
            ],
        )

        if write_headers:
            csv_writer.writeheader()
            video_writer.writeheader()

        while queue and crawled < max_pages:
            url, depth = queue.popleft()
            if url in visited:
                continue
            visited.add(url)

            if depth > max_depth:
                stats["skipped_depth"] += 1
                continue

            if not should_visit(url, root_domain, include_pattern, exclude_pattern, skip_noise_urls):
                stats["skipped_filter"] += 1
                continue

            status_code, html, error = fetch_with_retries(
                session=session,
                url=url,
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
            )

            raw_file = raw_dir / to_filename(url, ".html")
            if html:
                raw_file.write_text(html, encoding="utf-8")

            record = {
                "url": url,
                "status_code": status_code,
                "raw_html_path": str(raw_file),
                "error": error,
                "scraped_at_unix": int(time.time()),
                "depth": depth,
            }
            index_f.write(json.dumps(record, ensure_ascii=False) + "\n")

            parsed = {
                "url": url,
                "status_code": status_code,
                "title": "",
                "description": "",
                "h1": [],
                "h2": [],
                "text_chars": 0,
                "links_count": 0,
                "links": [],
                "video_urls": [],
                "video_count": 0,
                "downloaded_video_count": 0,
                "raw_html_path": str(raw_file),
                "error": error,
            }

            if html:
                parsed.update(parse_html(
                    url, html, keep_query_params=keep_query_params))
                stats["video_found"] += parsed["video_count"]

                next_depth = depth + 1
                if next_depth <= max_depth:
                    for next_url in parsed["links"]:
                        if next_url not in enqueued and next_url not in visited:
                            queue.append((next_url, next_depth))
                            enqueued.add(next_url)
                            stats["queued"] += 1

                downloadable_videos = {
                    video_url for video_url in parsed["video_urls"] if looks_like_video_url(video_url)}
                for idx, video_url in enumerate(parsed["video_urls"]):
                    should_download = download_videos and idx < max_videos_per_page and video_url in downloadable_videos
                    downloaded_file_path = ""
                    video_error = ""
                    if should_download:
                        downloaded_file_path, video_error = download_video(
                            session=session,
                            video_url=video_url,
                            videos_dir=videos_dir,
                            timeout_seconds=timeout_seconds,
                            max_video_mb=max_video_mb,
                            max_retries=max_retries,
                            retry_backoff=retry_backoff,
                        )
                        if downloaded_file_path:
                            parsed["downloaded_video_count"] += 1
                            stats["video_downloaded"] += 1

                    video_writer.writerow(
                        {
                            "page_url": url,
                            "video_url": video_url,
                            "download_attempted": should_download,
                            "downloaded_file_path": downloaded_file_path,
                            "error": video_error,
                        }
                    )

            if error:
                stats["errors"] += 1

            parsed_jsonl_f.write(json.dumps(parsed, ensure_ascii=False) + "\n")
            csv_writer.writerow(
                {
                    "url": parsed["url"],
                    "status_code": parsed["status_code"],
                    "title": parsed["title"],
                    "description": parsed["description"],
                    "h1_count": len(parsed["h1"]),
                    "h2_count": len(parsed["h2"]),
                    "text_chars": parsed["text_chars"],
                    "links_count": parsed["links_count"],
                    "video_count": parsed["video_count"],
                    "downloaded_video_count": parsed["downloaded_video_count"],
                    "raw_html_path": parsed["raw_html_path"],
                    "error": parsed["error"],
                }
            )

            crawled += 1
            print(
                f"[{crawled}/{max_pages}] depth={depth} {url} status={status_code} videos={parsed['video_count']} "
                f"downloaded={parsed['downloaded_video_count']} error={bool(error)}"
            )
            time.sleep(delay_seconds)

    print("\nDone.")
    print(f"Crawl index:         {index_path}")
    print(f"Parsed JSONL:        {parsed_jsonl_path}")
    print(f"Parsed CSV:          {parsed_csv_path}")
    print(f"Video manifest CSV:  {video_manifest_path}")
    print(f"Raw pages:           {raw_dir}")
    if download_videos:
        print(f"Downloaded videos:   {videos_dir}")
    print("\nSummary:")
    print(f"  pages_crawled:     {crawled}")
    print(f"  urls_queued:       {stats['queued']}")
    print(f"  skipped_depth:     {stats['skipped_depth']}")
    print(f"  skipped_filter:    {stats['skipped_filter']}")
    print(f"  errors:            {stats['errors']}")
    print(f"  videos_found:      {stats['video_found']}")
    print(f"  videos_downloaded: {stats['video_downloaded']}")

    if auto_build_catalog:
        if build_catalog is None or render_index_html is None or write_catalog_csvs is None:
            print("\nCatalog generation skipped: build_catalog module is not available.")
            return

        try:
            seed_for_catalog = canonicalize_url(
                catalog_seed_url or start_url, keep_query_params=keep_query_params)
            catalog = build_catalog(out_dir=out_dir, seed_url=seed_for_catalog)
            catalog_json_path = out_dir / "catalog.json"
            catalog_index_path = out_dir / "index.html"
            catalog_json_path.write_text(json.dumps(
                catalog, ensure_ascii=False, indent=2), encoding="utf-8")
            catalog_index_path.write_text(
                render_index_html(catalog), encoding="utf-8")
            pages_csv, videos_csv = write_catalog_csvs(catalog, out_dir)
            print("\nCatalog:")
            print(f"  index_html:         {catalog_index_path}")
            print(f"  catalog_json:       {catalog_json_path}")
            print(f"  catalog_pages_csv:  {pages_csv}")
            print(f"  catalog_videos_csv: {videos_csv}")
        except Exception as exc:
            print(f"\nCatalog generation failed: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl a site and output raw HTML + parsed fields for visualization."
    )
    parser.add_argument("start_url", help="Seed URL to start crawling from.")
    parser.add_argument(
        "--out-dir",
        default="data",
        help="Output directory for crawl artifacts (default: data).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=100,
        help="Max number of pages to crawl (default: 100).",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="Max link depth from seed page (default: 3).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Delay between requests in seconds (default: 0.5).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP timeout per request in seconds (default: 15).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Retry attempts for request failures (default: 2).",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=1.0,
        help="Base backoff in seconds for retries (default: 1.0).",
    )
    parser.add_argument(
        "--include-regex",
        default=None,
        help="Only crawl URLs matching this regex.",
    )
    parser.add_argument(
        "--exclude-regex",
        default=None,
        help="Skip URLs matching this regex.",
    )
    parser.add_argument(
        "--user-agent",
        default="DataScraperBot/1.0 (+https://example.local)",
        help="Custom user agent for requests.",
    )
    parser.add_argument(
        "--cookie",
        default=None,
        help="Cookie header string to use for authenticated crawling.",
    )
    parser.add_argument(
        "--cookie-file",
        default=None,
        help="Path to a file containing a Cookie header string.",
    )
    parser.add_argument(
        "--keep-query-params",
        action="store_true",
        help="Keep query parameters in crawled page URLs (default strips most to reduce duplicates).",
    )
    parser.add_argument(
        "--no-skip-noise-urls",
        action="store_true",
        help="Disable built-in filtering for obvious noise URLs (login/logout/feed/wp-admin/comment links).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Append to existing outputs and skip already indexed URLs.",
    )
    parser.add_argument(
        "--auto-build-catalog",
        action="store_true",
        help="Generate data/index.html, data/catalog.json, and sectioned CSVs after crawl.",
    )
    parser.add_argument(
        "--catalog-seed-url",
        default=None,
        help="Seed URL used for catalog section grouping (defaults to start_url).",
    )
    parser.add_argument(
        "--download-videos",
        action="store_true",
        help="Download direct video files (.mp4/.webm/etc.) when found.",
    )
    parser.add_argument(
        "--max-videos-per-page",
        type=int,
        default=2,
        help="Max number of videos to download per page (default: 2).",
    )
    parser.add_argument(
        "--max-video-mb",
        type=int,
        default=200,
        help="Skip downloads larger than this many MB (default: 200).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_crawl(
        start_url=canonicalize_url(
            args.start_url, keep_query_params=args.keep_query_params),
        out_dir=Path(args.out_dir),
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        delay_seconds=args.delay,
        timeout_seconds=args.timeout,
        max_retries=args.max_retries,
        retry_backoff=args.retry_backoff,
        include_regex=args.include_regex,
        exclude_regex=args.exclude_regex,
        user_agent=args.user_agent,
        cookie_string=args.cookie,
        cookie_file=Path(args.cookie_file) if args.cookie_file else None,
        keep_query_params=args.keep_query_params,
        skip_noise_urls=not args.no_skip_noise_urls,
        resume=args.resume,
        download_videos=args.download_videos,
        max_videos_per_page=args.max_videos_per_page,
        max_video_mb=args.max_video_mb,
        auto_build_catalog=args.auto_build_catalog,
        catalog_seed_url=args.catalog_seed_url,
    )


if __name__ == "__main__":
    main()
