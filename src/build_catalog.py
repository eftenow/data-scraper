import argparse
import csv
import json
import re
from dataclasses import dataclass
from html import escape as html_escape
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup


def canonicalize_url(url: str) -> str:
    cleaned, _fragment = urldefrag(url)
    return cleaned.strip()


def read_jsonl(path: Path) -> list[dict]:
    items: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def read_video_manifest(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def is_same_domain(url: str, seed: str) -> bool:
    return urlparse(url).netloc == urlparse(seed).netloc


def rel_from_data(path_in_data: str) -> str:
    return re.sub(r"^data/", "", path_in_data)


def extract_intro_sections(intro_html: str, intro_url: str) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """
    Returns:
      url_to_section: page URL -> section title
      url_to_linktext: page URL -> anchor text
      section_order: ordered unique section titles as they appear
    """
    soup = BeautifulSoup(intro_html, "lxml")
    url_to_section: dict[str, str] = {}
    url_to_linktext: dict[str, str] = {}
    section_order: list[str] = []

    for a in soup.find_all("a", href=True):
        href = canonicalize_url(urljoin(intro_url, a["href"]))
        if not href.startswith("http"):
            continue

        text = a.get_text(" ", strip=True) or ""

        section = None
        for prev in a.previous_elements:
            if getattr(prev, "name", None) in {"h2", "h3", "h4"}:
                section = prev.get_text(" ", strip=True)
                break
        if not section:
            section = "Other"

        if section not in section_order:
            section_order.append(section)

        if href not in url_to_section:
            url_to_section[href] = section
        if href not in url_to_linktext and text:
            url_to_linktext[href] = text

    return url_to_section, url_to_linktext, section_order


@dataclass
class PageItem:
    url: str
    title: str
    raw_html_rel: str
    section: str
    link_text: str
    video_urls: list[str]
    downloaded_files_rel: list[str]


def build_catalog(out_dir: Path, seed_url: str) -> dict:
    parsed_path = out_dir / "parsed_records.jsonl"
    video_manifest_path = out_dir / "video_manifest.csv"
    if not parsed_path.exists():
        raise SystemExit(f"Missing {parsed_path}")
    if not video_manifest_path.exists():
        raise SystemExit(f"Missing {video_manifest_path}")

    pages = read_jsonl(parsed_path)
    page_by_url = {canonicalize_url(p["url"]): p for p in pages if p.get("url")}

    seed_url = canonicalize_url(seed_url)
    seed = page_by_url.get(seed_url)
    if not seed:
        raise SystemExit(f"Seed URL not found in parsed records: {seed_url}")

    seed_raw_path = Path(seed["raw_html_path"])
    intro_html = seed_raw_path.read_text(encoding="utf-8")
    url_to_section, url_to_linktext, section_order = extract_intro_sections(intro_html, seed_url)

    manifest = read_video_manifest(video_manifest_path)
    downloaded_by_page: dict[str, list[str]] = {}
    for row in manifest:
        page_url = canonicalize_url(row.get("page_url") or "")
        downloaded = (row.get("downloaded_file_path") or "").strip()
        if page_url and downloaded:
            downloaded_by_page.setdefault(page_url, []).append(rel_from_data(downloaded))

    items: list[PageItem] = []
    for url, p in page_by_url.items():
        if not is_same_domain(url, seed_url):
            continue

        raw_rel = rel_from_data(str(p.get("raw_html_path") or ""))
        title = (p.get("title") or "").strip() or url
        section = url_to_section.get(url, "Other")
        link_text = url_to_linktext.get(url, "")
        video_urls = list(p.get("video_urls") or [])
        downloaded_rel = downloaded_by_page.get(url, [])

        items.append(
            PageItem(
                url=url,
                title=title,
                raw_html_rel=raw_rel,
                section=section,
                link_text=link_text,
                video_urls=video_urls,
                downloaded_files_rel=downloaded_rel,
            )
        )

    all_sections = list(section_order)
    for it in items:
        if it.section not in all_sections:
            all_sections.append(it.section)

    sections: dict[str, list[PageItem]] = {s: [] for s in all_sections}
    for it in items:
        sections.setdefault(it.section, []).append(it)

    for s in list(sections.keys()):
        sections[s] = sorted(
            sections[s],
            key=lambda x: (
                0 if x.downloaded_files_rel else 1,
                (x.link_text or x.title).lower(),
            ),
        )

    return {
        "seed_url": seed_url,
        "sections": {
            s: [
                {
                    "url": it.url,
                    "title": it.title,
                    "link_text": it.link_text,
                    "raw_html_rel": it.raw_html_rel,
                    "video_urls": it.video_urls,
                    "downloaded_files_rel": it.downloaded_files_rel,
                }
                for it in sections.get(s, [])
            ]
            for s in all_sections
        },
    }


def render_index_html(catalog: dict) -> str:
    seed_url = catalog["seed_url"]
    catalog_json = json.dumps(catalog, ensure_ascii=False).replace("<", "\\u003c")

    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Scrape Catalog</title>
  <style>
    :root {
      --bg: #0b0f14;
      --card: rgba(255,255,255,0.06);
      --text: #e9eef5;
      --muted: rgba(233,238,245,0.7);
      --line: rgba(233,238,245,0.14);
      --accent: #60a5fa;
      --accent2: #34d399;
      --warn: #fbbf24;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
    }
    body {
      margin: 0;
      font-family: var(--sans);
      background: radial-gradient(1200px 800px at 20% -10%, rgba(96,165,250,0.22), transparent 55%),
                  radial-gradient(900px 700px at 85% 0%, rgba(52,211,153,0.16), transparent 50%),
                  var(--bg);
      color: var(--text);
    }
    header {
      position: sticky;
      top: 0;
      z-index: 10;
      backdrop-filter: blur(10px);
      background: rgba(11,15,20,0.72);
      border-bottom: 1px solid var(--line);
    }
    .wrap { max-width: 1100px; margin: 0 auto; padding: 16px; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
    h1 { font-size: 18px; margin: 0; letter-spacing: 0.2px; }
    .seed { font-family: var(--mono); font-size: 12px; color: var(--muted); word-break: break-all; }
    input[type="search"] {
      flex: 1;
      min-width: 220px;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.04);
      color: var(--text);
      outline: none;
    }
    .pill {
      font-family: var(--mono);
      font-size: 12px;
      padding: 6px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: rgba(255,255,255,0.03);
    }
    main { padding: 16px; }
    details {
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(255,255,255,0.03);
      margin-bottom: 12px;
      overflow: hidden;
    }
    summary {
      list-style: none;
      cursor: pointer;
      padding: 12px 14px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      border-bottom: 1px solid rgba(255,255,255,0.0);
    }
    details[open] summary { border-bottom: 1px solid var(--line); }
    summary::-webkit-details-marker { display:none; }
    .section-title { font-weight: 600; }
    .section-meta { font-family: var(--mono); font-size: 12px; color: var(--muted); }
    .list { padding: 12px 14px; }
    .item {
      padding: 10px 10px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: rgba(255,255,255,0.03);
      margin-bottom: 10px;
    }
    .item-head { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
    .item-title { font-weight: 650; }
    .item-sub { font-family: var(--mono); font-size: 12px; color: var(--muted); word-break: break-all; }
    .links { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 8px; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .badge {
      font-family: var(--mono);
      font-size: 12px;
      padding: 3px 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      color: var(--muted);
      background: rgba(255,255,255,0.02);
    }
    .badge.video { color: rgba(52,211,153,0.92); border-color: rgba(52,211,153,0.4); }
    .badge.embed { color: rgba(251,191,36,0.95); border-color: rgba(251,191,36,0.45); }
    video { width: 100%; margin-top: 8px; border-radius: 12px; background: rgba(0,0,0,0.5); }
    .muted { color: var(--muted); font-size: 13px; }
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <div class="row">
        <div style="min-width: 260px;">
          <h1>Scrape Catalog</h1>
          <div class="seed">__SEED__</div>
        </div>
        <input id="q" type="search" placeholder="Search titles, URLs, link text..." />
        <span id="stats" class="pill">...</span>
      </div>
      <div class="muted" style="margin-top: 8px;">
        Tips: search for a lesson name. Local playable videos show up inline.
      </div>
    </div>
  </header>
  <main class="wrap">
    <div id="app"></div>
  </main>

  <script id="catalog-json" type="application/json">__CATALOG__</script>
  <script>
    const CATALOG = JSON.parse(document.getElementById('catalog-json').textContent);

    function normalize(s) { return (s || '').toLowerCase(); }
    function escapeHtml(s) {
      return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    function hasDirectDownload(item) {
      return item.downloaded_files_rel && item.downloaded_files_rel.length > 0;
    }
    function isEmbedOnly(item) {
      if (!item.video_urls || item.video_urls.length === 0) return false;
      if (hasDirectDownload(item)) return false;
      return item.video_urls.every(u => !(/\.(mp4|webm|m4v|mov|ogv)(\?|$)/i.test(u)));
    }

    function render(q) {
      const root = document.getElementById('app');
      const qn = normalize(q);

      let total = 0;
      let shown = 0;
      let sectionsShown = 0;

      const parts = [];
      for (const sectionTitle of Object.keys(CATALOG.sections)) {
        const items = CATALOG.sections[sectionTitle] || [];
        total += items.length;
        const filtered = items.filter(it => {
          if (!qn) return true;
          const hay = [it.title, it.url, it.link_text, (it.video_urls || []).join(' ')].join(' ');
          return normalize(hay).includes(qn);
        });
        if (filtered.length === 0) continue;
        sectionsShown += 1;
        shown += filtered.length;

        parts.push(
          `<details open>` +
          `<summary>` +
          `<span class="section-title">${escapeHtml(sectionTitle)}</span>` +
          `<span class="section-meta">${filtered.length} / ${items.length}</span>` +
          `</summary>` +
          `<div class="list">`
        );

        for (const it of filtered) {
          const badges = [];
          if (hasDirectDownload(it)) badges.push(`<span class="badge video">video</span>`);
          else if (isEmbedOnly(it)) badges.push(`<span class="badge embed">embed</span>`);

          parts.push(
            `<div class="item">` +
            `<div class="item-head">` +
            `<span class="item-title">${escapeHtml(it.link_text || it.title)}</span>` +
            `${badges.join(' ')}` +
            `</div>` +
            `<div class="item-sub">${escapeHtml(it.url)}</div>` +
            `<div class="links">` +
            `<a href="${escapeHtml(it.raw_html_rel)}" target="_blank" rel="noopener">Open local page</a>` +
            `<a href="${escapeHtml(it.url)}" target="_blank" rel="noopener">Open online</a>` +
            `</div>` +
            (hasDirectDownload(it) ? it.downloaded_files_rel.map(p => `<video controls preload="metadata" src="${escapeHtml(p)}"></video>`).join('') : '') +
            `</div>`
          );
        }

        parts.push(`</div></details>`);
      }

      root.innerHTML = parts.join("\n");
      document.getElementById('stats').textContent = `${shown} results in ${sectionsShown} sections (total pages: ${total})`;
    }

    const q = document.getElementById('q');
    q.addEventListener('input', () => render(q.value));
    render('');
  </script>
</body>
</html>
""".replace("__SEED__", html_escape(seed_url)).replace("__CATALOG__", html_escape(catalog_json))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build a structured catalog + offline index.html for scraped data.")
    p.add_argument(
        "--out-dir",
        default="data",
        help="Directory containing parsed_records.jsonl and video_manifest.csv (default: data).",
    )
    p.add_argument(
        "--seed-url",
        required=True,
        help="The intro/seed page URL used to derive section grouping.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    catalog = build_catalog(out_dir=out_dir, seed_url=args.seed_url)
    (out_dir / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "index.html").write_text(render_index_html(catalog), encoding="utf-8")
    print(f"Wrote {out_dir / 'catalog.json'}")
    print(f"Wrote {out_dir / 'index.html'}")


if __name__ == "__main__":
    main()
