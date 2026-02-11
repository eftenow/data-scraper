# Data Scraper Starter

A minimal Python scraper 


## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python src/scraper.py https://example.com --max-pages 50 --delay 1.0
```

Useful options:

```bash
python src/scraper.py https://example.com \
  --max-pages 200 \
  --delay 0.8 \
  --include-regex '/blog/' \
  --exclude-regex '/tag/|/author/' \
  --out-dir data
```

## Output

- `data/raw/*.html`: raw page HTML
- `data/crawl_index.jsonl`:  metadata
- `data/parsed_records.jsonl`: parsed per-page records
- `data/parsed_records.csv`: tabular export for BI tools
- `data/videos`: videos contained

## Visualize scrped data

### Spreadsheet / BI
Open `data/parsed_records.csv` in Google Sheets, Excel, Tableau, Power BI, or Looker Studio.


For authenticated crawls (auth-gated pages):

- create a `auth_cookie.txt` file, and paste the cookie header value inside, so that the scraper can read it and use it, example use:

```
python src/scraper.py https://test.com/ \
  --max-pages 200 \
  --delay 1.0 \
  --download-videos \
  --cookie-file auth_cookie.txt
```
