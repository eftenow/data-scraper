#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import re
import time
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urldefrag, urlparse

import requests
from bs4 import BeautifulSoup


def canonicalize_url(url: str) -> str:
    cleaned, _fragment = urldefrag(url)
    return cleaned.strip()


def is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"}


def same_domain(url: str, root_domain: str) -> bool:
    return urlparse(url).netloc == root_domain


def to_filename(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"{digest}.html"


def parse_html(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    description = (desc_tag.get("content") or "").strip() if desc_tag else ""
    h1 = [node.get_text(" ", strip=True) for node in soup.find_all("h1")]
    h2 = [node.get_text(" ", strip=True) for node in soup.find_all("h2")]
    text = soup.get_text("\n", strip=True)

    links = []
    for a in soup.find_all("a", href=True):
        absolute = canonicalize_url(urljoin(url, a["href"]))
        if is_http_url(absolute):
            links.append(absolute)

    return {
        "url": url,
        "title": title,
        "description": description,
        "h1": h1,
        "h2": h2,
        "text_chars": len(text),
        "links_count": len(links),
        "links": links,
    }


def should_visit(
    url: str,
    root_domain: str,
    include_pattern: Optional[re.Pattern],
    exclude_pattern: Optional[re.Pattern],
) -> bool:
    if not is_http_url(url):
        return False
    if not same_domain(url, root_domain):
        return False
    if include_pattern and not include_pattern.search(url):
        return False
    if exclude_pattern and exclude_pattern.search(url):
        return False
    return True


def run_crawl(
    start_url: str,
    out_dir: Path,
    max_pages: int,
    delay_seconds: float,
    timeout_seconds: float,
    include_regex: Optional[str],
    exclude_regex: Optional[str],
    user_agent: str,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(exist_ok=True)

    index_path = out_dir / "crawl_index.jsonl"
    parsed_jsonl_path = out_dir / "parsed_records.jsonl"
    parsed_csv_path = out_dir / "parsed_records.csv"

    root_domain = urlparse(start_url).netloc
    include_pattern = re.compile(include_regex) if include_regex else None
    exclude_pattern = re.compile(exclude_regex) if exclude_regex else None

    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})

    queue = deque([canonicalize_url(start_url)])
    enqueued = {canonicalize_url(start_url)}
    visited = set()
    crawled = 0

    with index_path.open("w", encoding="utf-8") as index_f, parsed_jsonl_path.open(
        "w", encoding="utf-8"
    ) as parsed_jsonl_f, parsed_csv_path.open("w", encoding="utf-8", newline="") as parsed_csv_f:
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
                "raw_html_path",
                "error",
            ],
        )
        csv_writer.writeheader()

        while queue and crawled < max_pages:
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)

            if not should_visit(url, root_domain, include_pattern, exclude_pattern):
                continue

            status_code = None
            error = ""
            html = ""

            try:
                response = session.get(url, timeout=timeout_seconds)
                status_code = response.status_code
                response.raise_for_status()
                html = response.text
            except requests.RequestException as exc:
                error = str(exc)

            raw_file = raw_dir / to_filename(url)
            if html:
                raw_file.write_text(html, encoding="utf-8")

            record = {
                "url": url,
                "status_code": status_code,
                "raw_html_path": str(raw_file),
                "error": error,
                "scraped_at_unix": int(time.time()),
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
                "raw_html_path": str(raw_file),
                "error": error,
            }
            if html:
                parsed.update(parse_html(url, html))
                for next_url in parsed["links"]:
                    if next_url not in enqueued and next_url not in visited:
                        queue.append(next_url)
                        enqueued.add(next_url)

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
                    "raw_html_path": parsed["raw_html_path"],
                    "error": parsed["error"],
                }
            )

            crawled += 1
            print(f"[{crawled}/{max_pages}] {url} status={status_code} error={bool(error)}")
            time.sleep(delay_seconds)

    print("\nDone.")
    print(f"Crawl index: {index_path}")
    print(f"Parsed JSONL: {parsed_jsonl_path}")
    print(f"Parsed CSV:   {parsed_csv_path}")
    print(f"Raw pages:    {raw_dir}")


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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_crawl(
        start_url=canonicalize_url(args.start_url),
        out_dir=Path(args.out_dir),
        max_pages=args.max_pages,
        delay_seconds=args.delay,
        timeout_seconds=args.timeout,
        include_regex=args.include_regex,
        exclude_regex=args.exclude_regex,
        user_agent=args.user_agent,
    )


if __name__ == "__main__":
    main()
