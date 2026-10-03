# 🌿 IngredientsNetwork Scraper

A production-ready web scraper for [IngredientsNetwork.com](https://www.ingredientsnetwork.com/) — a B2B supplier directory for the food, nutrition, and nutraceutical industry. Built with Python and Playwright.

## 📦 What It Does

Scrapes **all ~1,600 supplier company profiles** from IngredientsNetwork.com and outputs a clean, structured CSV with 10 fields per company. Handles pagination, network errors, Unicode, rate limiting, and resume-on-crash automatically.

## 🗂️ Output Fields

| Field | Description |
|---|---|
| `Company Name` | Official company name |
| `Company Description` | Full company bio / about text |
| `Sales Markets` | Geographic regions the company sells to |
| `Primary Business Activity` | e.g. Manufacturer, Distributor, Raw Material Supplier |
| `Categories` | All ingredient/product categories the company is affiliated with |
| `Events` | Trade shows attended or upcoming (e.g. Vitafoods, Fi Europe) |
| `Address` | Registered company address |
| `Email` | Contact email |
| `Telephone` | Contact phone number |
| `Website` | Company website URL |
| `Profile URL` | Source URL on IngredientsNetwork.com |

## 🚀 Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Run the scraper

```bash
# Full run — all ~1,614 companies (2-3 hours)
python scraper.py

# Test run — first 50 companies only
python scraper.py --limit 50

# Visible browser window (useful for debugging)
python scraper.py --headed

# Get category counts without scraping profiles
python scraper.py --counts
```

## ⚙️ CLI Options

| Flag | Default | Description |
|---|---|---|
| `--limit N` | 0 (all) | Scrape only the first N companies |
| `--delay N` | 350 | Milliseconds between requests |
| `--headed` | false | Show browser window |
| `--checkpoint N` | 25 | Save progress every N profiles |
| `--output DIR` | `data/` | Output directory |
| `--counts` | — | Fetch category counts and exit |

## 📁 Output Files

```
data/
├── temporary_raw.csv              # Live checkpoint — updated every 25 profiles
├── ingredientsnetwork_companies.csv   # Final cleaned & deduplicated dataset
└── missing_required_fields.csv    # QA file — profiles with missing fields
```

Partial runs (`--limit`) produce files with a `PARTIAL_` prefix and a `Run_Note` column.

## 🔄 Resume Support

If the scraper crashes or you stop it manually, just run it again — it automatically picks up from the last checkpoint. No data is lost.

```bash
# Interrupted at 600 companies? Just re-run:
python scraper.py
# → [resume] 600 profiles already in checkpoint; skipping them.
```

## 🧠 How It Works

### Discovery Strategy

IngredientsNetwork's search page uses **AJAX pagination** — the "Show More" button has `href="#"` and is JavaScript-driven. There are no URL parameters that control pagination, making it impossible to enumerate all companies via search.

Instead, the scraper uses the **alphabetical supplier directory**:
- `/company/a.html` through `/company/z.html`
- `/company/0-9.html`

This reliably enumerates all ~1,614 company profile URLs before scraping begins.

### Contact Field Extraction

The site hides Address and Website fields behind a login wall (blurred image placeholders for unauthenticated users). Email and Telephone are extracted from invisible `mailto:` and `tel:` href attributes in the DOM, which Playwright can read without rendering them visually.

### Data Cleaning

- Whitespace normalized with regex
- Duplicates removed by Profile URL (keeps most complete record)
- Semi-colon delimited fields (Categories, Sales Markets) cleaned consistently
- Raw checkpoint saved before final clean pass

## 🛡️ Robustness Features

- **Retry logic** — 3 attempts per directory page with exponential backoff
- **Error isolation** — a failed profile is logged and skipped, not a crash
- **Unicode handling** — `sys.stdout.reconfigure(encoding='utf-8')` for Windows compatibility
- **Cookie banners** — auto-dismissed on page load
- **Configurable delay** — default 350ms between requests (polite crawling)

## 📊 Category Count Queries

Run `python scraper.py --counts` to read the site's own SUPPLIERS(N) counters from category pages:

```
=== CATEGORY COUNTS ===

  Ingredients (taxonomy root)
  Finished Products (root)
  Herbs & Spices          → 399 suppliers
  Physical Formats        → 764 suppliers
  Cognitive & Mental Health → 590 suppliers
```

## 🧰 Tech Stack

- **Python 3.9+**
- **Playwright** — browser automation (Chromium)
- **csv** — stdlib CSV handling
- **re** — text cleaning and URL pattern matching
- **argparse** — CLI interface

## 📋 Requirements

```
playwright>=1.40.0
```

Install with:
```bash
pip install -r requirements.txt
playwright install chromium
```

## ⚠️ Known Limitations

| Limitation | Reason |
|---|---|
| `Address` always blank | Hidden behind login wall |
| `Website` always blank | Hidden behind login wall |
| `Email` ~40% coverage | Only present when exposed in DOM |
| `Telephone` ~50% coverage | Only present when exposed in DOM |
| `Sales Markets` ~55% coverage | Only on manufacturer-type profiles |
| `Primary Business Activity` ~45% coverage | Only on manufacturer-type profiles |

These are site-level restrictions, not scraper bugs.

## 📜 License

MIT — use it, fork it, build on it.
