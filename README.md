# Ingredients Network Scraper

Scrapes supplier/company profiles from IngredientsNetwork and creates:

- `data/temporary_raw.csv` — raw extraction **before cleaning**
- `data/ingredientsnetwork_companies.csv` — cleaned, deduplicated final dataset
- `data/missing_required_fields.csv` — QA report for profiles where the website did not expose one or more required values

Fields: Company Name, Company Description, Sales Markets, Primary Business Activity, Categories, Events, Address, Email, Telephone, Website (+ Profile URL for auditability).

## Setup

```bash
python -m venv .venv
# Windows
.venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Test first

```bash
python scraper.py --limit 10 --headed
```

Then inspect the CSVs in `data/`.

## Full run

```bash
python scraper.py
```

The scraper follows the requested home-page Search flow first, paginates result pages, and extracts profile links. If the site's search UI changes or refuses an empty search, it falls back to the site's alphabetical supplier directory so the crawl can still enumerate suppliers.

It checkpoints `temporary_raw.csv` every 25 completed profiles and resumes from that file after interruption.

## Important accuracy rule

The scraper never invents missing values. If Ingredients Network does not publicly expose a requested field for a supplier, that profile is added to `missing_required_fields.csv`. This is safer than filling inaccurate contact information simply to avoid blank cells.

## Useful options

```bash
python scraper.py --limit 50       # first 50 profiles
python scraper.py --headed         # visible browser
python scraper.py --delay 800      # gentler crawl delay
python scraper.py --checkpoint 10  # save raw CSV more often
```
