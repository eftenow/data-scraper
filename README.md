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
- `data/crawl_index.jsonl`: crawl-level metadata
- `data/parsed_records.jsonl`: parsed per-page records
- `data/parsed_records.csv`: tabular export for BI tools


## Visualize scrped data

### Spreadsheet / BI
Open `data/parsed_records.csv` in Google Sheets, Excel, Tableau, Power BI, or Looker Studio.


