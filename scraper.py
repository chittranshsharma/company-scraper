"""
IngredientsNetwork scraper
==========================
Scrapes supplier profiles from https://www.ingredientsnetwork.com/

Discovery strategy
------------------
Primary  : alphabetical supplier directory  /company/{a-z}.html + /company/0-9.html
           Enumerates all ~1,600 suppliers reliably.
Secondary: site search (empty query) as a cross-check; search uses AJAX "Show More"
           so it cannot be paginated via URL parameters (start= / from= do not work).

Site limitations (what the scraper can and cannot get)
------------------------------------------------------
The site hides Address, Website, and most Email/Telephone values behind a
login/registration wall, replacing them with blurred-image placeholders.
The actual data exists as invisible mailto: and tel: links in the DOM, which
Playwright can read from href attributes without rendering them visually.
- Telephone: always available via tel: href
- Email    : available via mailto: href (not always present)
- Address  : fully hidden; cannot be extracted without login → recorded as missing
- Website  : fully hidden; cannot be extracted without login → recorded as missing

These limitations are reported honestly in missing_required_fields.csv rather
than fabricating placeholder values.

Run modes
---------
  python scraper.py                      # full run, all companies
  python scraper.py --limit 50           # first 50 profiles (PARTIAL)
  python scraper.py --headed             # visible browser window
  python scraper.py --delay 800          # gentler crawl delay (ms)
  python scraper.py --checkpoint 10      # save raw CSV every N profiles
  python scraper.py --counts             # fetch the five assignment count questions only

Outputs (written to --output dir, default: data/)
--------------------------------------------------
  temporary_raw.csv                         – raw checkpoint; resume-safe
  ingredientsnetwork_companies.csv          – cleaned, deduplicated final dataset
  missing_required_fields.csv               – QA: profiles with ≥1 missing required field

Partial runs (--limit) produce files with a PARTIAL_ prefix and a Run_Note column.
"""

import argparse, asyncio, csv, os, re, sys
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Force UTF-8 output on Windows so Unicode characters in company names
# don't crash the checkpoint progress lines.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

BASE    = 'https://www.ingredientsnetwork.com/'
FIELDS  = [
    'Company Name', 'Company Description', 'Sales Markets',
    'Primary Business Activity', 'Categories', 'Events',
    'Address', 'Email', 'Telephone', 'Website', 'Profile URL',
]

# ── tiny helpers ──────────────────────────────────────────────────────────────

def clean(s):
    return re.sub(r'\s+', ' ', (s or '')).strip()

def uniq(xs):
    out = []; seen = set()
    for x in xs:
        x = clean(x)
        if x and x not in seen:
            seen.add(x); out.append(x)
    return out

def write_csv(path, rows, extra_fields=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (extra_fields or []) + FIELDS
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        w.writeheader(); w.writerows(rows)

def read_existing(path):
    if not path.exists():
        return []
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))

# ── browser helpers ───────────────────────────────────────────────────────────

async def accept_cookies(page):
    for label in ['Accept All Cookies', 'Accept all', 'I agree', 'Accept']:
        try:
            b = page.get_by_role('button', name=re.compile(label, re.I)).first
            if await b.is_visible(timeout=700):
                await b.click(); return
        except:
            pass

async def txt(page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                t = clean(await loc.inner_text(timeout=1500))
                if t: return t
        except:
            pass
    return ''

# ── DOM extraction helpers ────────────────────────────────────────────────────

async def section_after_heading(page, heading_regex):
    """
    Return text following the first heading whose text matches heading_regex,
    up to the next heading.  Works for H1-H6, DT, STRONG, B elements.
    """
    try:
        val = await page.evaluate(r'''(pat) => {
            const rx = new RegExp(pat, 'i');
            const tags = 'h1,h2,h3,h4,h5,h6,dt,strong,b';
            const all  = [...document.querySelectorAll(tags)];
            const h    = all.find(e => rx.test((e.innerText || '').trim()));
            if (!h) return '';
            if (h.tagName === 'DT') {
                const dd = h.nextElementSibling;
                return dd ? (dd.innerText || '') : '';
            }
            let out = []; let n = h.nextElementSibling;
            while (n && !/^H[1-6]$/.test(n.tagName)) {
                out.push(n.innerText || ''); n = n.nextElementSibling;
            }
            if (out.join(' ').trim()) return out.join(' ');
            const p = h.parentElement;
            if (p) {
                let sib = p.nextElementSibling; out = [];
                while (sib && !sib.querySelector?.('h1,h2,h3,h4,h5,h6')
                       && !/^H[1-6]$/.test(sib.tagName)) {
                    out.push(sib.innerText || ''); sib = sib.nextElementSibling;
                }
                return out.join(' ');
            }
            return '';
        }''', heading_regex)
        return clean(val)
    except:
        return ''

async def quickfacts_field(page, label):
    """
    Extract a value from the Quick Facts table (<table class="quickfacts">).
    Rows are <tr><th>label</th><td>value</td></tr>.
    """
    try:
        val = await page.evaluate(r'''(label) => {
            const norm = s => (s || '').replace(/\s+/g, ' ').trim();
            const ths  = [...document.querySelectorAll('table.quickfacts th')];
            const th   = ths.find(e => norm(e.innerText).toLowerCase() === label.toLowerCase());
            if (!th) return '';
            const td = th.nextElementSibling;
            return td ? norm(td.innerText) : '';
        }''', label)
        return clean(val)
    except:
        return ''

async def extract_categories(page):
    """
    Extract company-affiliated categories from div.content-block.company-categories.
    This div contains an H2 'Categories affiliated with {name}' followed by
    div.category elements with <a> links (top-level) and ul.subcategories > li > a (sub-items).
    Falls back to the 3 subcategory links in the quickfacts table when the section is absent.
    """
    try:
        val = await page.evaluate(r'''() => {
            const norm = s => (s || '').replace(/\s+/g, ' ').trim();
            const container = document.querySelector('div.content-block.company-categories');
            if (!container) return '';
            const links = [...container.querySelectorAll('a[href*="code"]')];
            return links.map(a => norm(a.innerText)).filter(Boolean).join('; ');
        }''')
        if val:
            return clean(val)
    except:
        pass
    # Fallback: quickfacts table subcategory links (only 3, but better than nothing)
    try:
        val = await page.evaluate(r'''() => {
            const norm = s => (s || '').replace(/\s+/g, ' ').trim();
            const ths  = [...document.querySelectorAll('table.quickfacts th')];
            const th   = ths.find(e => /affiliated categories/i.test(norm(e.innerText)));
            if (!th) return '';
            const td = th.nextElementSibling;
            if (!td) return '';
            const links = [...td.querySelectorAll('a.subcategory')];
            return links.map(a => norm(a.innerText)).filter(Boolean).join('; ');
        }''')
        return clean(val)
    except:
        return ''

async def extract_events(page):
    """Collect event text from 'Recently at' and 'Upcoming events' sections."""
    events = []
    for hdr in ['Recently at', 'Upcoming events']:
        sec = await section_after_heading(page, f'^{hdr}$')
        if sec:
            events.append(f'{hdr}: {sec}')
    return events

# ── profile extractor ─────────────────────────────────────────────────────────

async def extract_profile(page, url):
    await page.goto(url, wait_until='domcontentloaded', timeout=60000)
    await accept_cookies(page)
    try:
        await page.wait_for_load_state('networkidle', timeout=8000)
    except:
        pass

    name = await txt(page, ['h1'])

    # Company description
    desc = await section_after_heading(page, r'^Company description$')
    if not desc:
        desc = await section_after_heading(page, r'^About$')

    # Sales Markets and Primary Business Activity are in the Quick Facts table
    # (<table class="quickfacts"><tr><th>label</th><td>value</td></tr>).
    # They are present on manufacturer/supplier profiles; absent on service/marketing companies.
    sales    = await quickfacts_field(page, 'Sales markets')
    activity = await quickfacts_field(page, 'Primary business activity')

    # Categories
    cats = await extract_categories(page)

    # Events
    events = await extract_events(page)

    # ── Contact information ──────────────────────────────────────────────────
    # The site hides Address, Website, and most contact values behind a
    # login wall (replaced with <img class="phone-blurred">).
    # Telephone and Email are accessible only via invisible tel:/mailto: hrefs.
    # Address and Website cannot be extracted without logging in.

    # Email: invisible mailto: link in the DOM
    email_val = ''
    try:
        mail = page.locator('a[href^="mailto:"]').first
        if await mail.count():
            raw = await mail.get_attribute('href')
            email_val = clean(raw.replace('mailto:', '').split('?')[0])
    except:
        pass

    # Telephone: invisible tel: link in the DOM
    telephone = ''
    try:
        tel = page.locator('a[href^="tel:"]').first
        if await tel.count():
            raw = await tel.get_attribute('href')
            telephone = clean(raw.replace('tel:', ''))
    except:
        pass

    # Address: blocked by login wall → always empty for unauthenticated scrape
    address = ''

    # Website: blocked by login wall → always empty for unauthenticated scrape
    website = ''

    return {
        'Company Name'              : name,
        'Company Description'       : desc,
        'Sales Markets'             : sales,
        'Primary Business Activity' : activity,
        'Categories'                : cats,
        'Events'                    : ' | '.join(uniq(events)),
        'Address'                   : address,
        'Email'                     : email_val,
        'Telephone'                 : telephone,
        'Website'                   : website,
        'Profile URL'               : url,
    }

# ── discovery ─────────────────────────────────────────────────────────────────

async def discover_directory(page):
    """
    Enumerate all supplier profile URLs from the alphabetical directory.
    Pages: /company/a.html … /company/z.html + /company/0-9.html  (~1,600 total).
    Sub-pages (aa.html, ab.html …) do not exist on this site and are not attempted.
    Each page is retried up to 3 times on transient network errors.
    """
    urls  = []
    chars = list('abcdefghijklmnopqrstuvwxyz') + ['0-9']
    for c in chars:
        u = f'{BASE}company/{c}.html'
        new_links = []
        for attempt in range(1, 4):          # up to 3 attempts
            try:
                await page.goto(u, wait_until='domcontentloaded', timeout=40000)
                hrefs = await page.locator('a[href*="-comp"]').evaluate_all(
                    'els => els.map(a => a.href)'
                )
                new_links = [h.split('#')[0] for h in hrefs if re.search(r'-comp\d+', h)]
                break                        # success – exit retry loop
            except Exception as e:
                if attempt < 3:
                    print(f'[warn] directory {c} attempt {attempt} failed: {e} – retrying…',
                          file=sys.stderr)
                    await page.wait_for_timeout(2000 * attempt)
                else:
                    print(f'[warn] directory {c}: all 3 attempts failed – skipping',
                          file=sys.stderr)
        urls.extend(new_links)
        print(f'[directory] {c.upper():>3}: +{len(new_links):>3} links  '
              f'({len(uniq(urls)):>4} unique so far)')
    return uniq(urls)

async def discover_via_search(page):
    """
    Fallback: collect profile URLs from the search page.
    Returns only the initial ~27 results (AJAX 'Show More' cannot be automated
    via URL parameters; start= and from= params have no effect).
    """
    search_url = (BASE + 'live/search/searchresults46v2.jsp'
                  '?site=47&SugType_val=&RecordId_val=&searchtype=all&name=')
    try:
        await page.goto(search_url, wait_until='domcontentloaded', timeout=60000)
        await accept_cookies(page)
        try:
            await page.wait_for_load_state('networkidle', timeout=8000)
        except:
            pass
        hrefs = await page.locator('a[href*="-comp"]').evaluate_all(
            'els => els.map(a => a.href)'
        )
        found = uniq([h.split('#')[0] for h in hrefs if re.search(r'-comp\d+', h)])
        print(f'[search-fallback] {len(found)} profiles '
              f'(NOTE: search is AJAX-only; use directory for full enumeration)')
        return found
    except Exception as e:
        print(f'[warn] search fallback failed: {e}', file=sys.stderr)
        return []

# ── cleaning & deduplication ──────────────────────────────────────────────────

def cleanse(rows):
    cleaned = []
    for r in rows:
        x = {k: clean(r.get(k, '')) for k in FIELDS}
        for k in ['Sales Markets', 'Primary Business Activity', 'Categories', 'Events']:
            x[k] = re.sub(r'\s*;\s*', '; ', x[k])
        cleaned.append(x)
    # Deduplicate by Profile URL; keep the row with the most filled-in fields
    best = {}
    for r in cleaned:
        key = r['Profile URL'] or r['Company Name'].lower()
        if not key:
            continue
        if key not in best or (
            sum(bool(v) for v in r.values()) >
            sum(bool(v) for v in best[key].values())
        ):
            best[key] = r
    return list(best.values())

# ── five-count helper ─────────────────────────────────────────────────────────

async def fetch_category_counts(page):
    """
    Read the site's own PRODUCTS(N) / SUPPLIERS(N) counters from category pages.
    These are rendered server-side and accessible without login.
    """
    targets = {
        'Ingredients (taxonomy root)'  : BASE + 'ingredients/',
        'Finished Products (root)'     : BASE + 'finished-products/',
        'Herbs, Spices'                : BASE + 'herbs-spices-code016020.html',
        'Physical Formats'             : BASE + 'physical-formats-code016198.html',
        'Cognitive & Mental Health'    : BASE + 'cognitive-mental-health-code016162.html',
    }
    results = {}
    for label, url in targets.items():
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await accept_cookies(page)
            try:
                await page.wait_for_load_state('networkidle', timeout=6000)
            except:
                pass
            body = await page.locator('body').inner_text(timeout=5000)
            prod_m = re.search(r'PRODUCTS\s*\(([\d,+]+)\)', body)
            supp_m = re.search(r'SUPPLIERS\s*\(([\d,+]+)\)', body)
            results[label] = {
                'products'  : prod_m.group(1) if prod_m else 'n/a',
                'suppliers' : supp_m.group(1) if supp_m else 'n/a',
                'url'       : url,
            }
        except Exception as e:
            results[label] = {
                'products': 'error', 'suppliers': 'error',
                'url': url, 'error': str(e),
            }
    return results

# ── main ──────────────────────────────────────────────────────────────────────

async def main(args):
    is_partial = bool(args.limit)
    out_dir    = Path(args.output)
    prefix     = 'PARTIAL_' if is_partial else ''

    raw_path     = out_dir / 'temporary_raw.csv'
    final_path   = out_dir / f'{prefix}ingredientsnetwork_companies.csv'
    missing_path = out_dir / f'{prefix}missing_required_fields.csv'
    out_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # Use Playwright's bundled Chromium; fall back to system Chrome if absent
        launch_opts = {'headless': not args.headed}
        chrome_path = (
            Path(os.environ.get('PROGRAMFILES', r'C:\Program Files'))
            / 'Google' / 'Chrome' / 'Application' / 'chrome.exe'
        )
        if chrome_path.exists():
            launch_opts['executable_path'] = str(chrome_path)

        browser = await p.chromium.launch(**launch_opts)
        ctx = await browser.new_context(
            viewport    = {'width': 1440, 'height': 1000},
            user_agent  = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 Chrome/154 Safari/537.36'),
        )
        page = await ctx.new_page()

        # ── counts-only mode ──────────────────────────────────────────────────
        if args.counts:
            print('[counts] Fetching category counts from IngredientsNetwork…')
            counts = await fetch_category_counts(page)
            print('\n=== FIVE COUNT QUESTIONS ===')
            print()
            for label, data in counts.items():
                print(f'  {label}')
                print(f'    Products  : {data["products"]}')
                print(f'    Suppliers : {data["suppliers"]}')
                print(f'    Source    : {data["url"]}')
                if 'error' in data:
                    print(f'    Error     : {data["error"]}')
                print()
            await browser.close()
            return

        # ── discovery ─────────────────────────────────────────────────────────
        print('[info] Enumerating company profiles via alphabetical directory…')
        urls = await discover_directory(page)
        print(f'[info] Directory enumeration complete: {len(urls)} unique profile URLs')

        if not urls:
            print('[warn] Directory returned no URLs. Trying search fallback.',
                  file=sys.stderr)
            urls = await discover_via_search(page)

        if args.limit:
            urls = urls[:args.limit]
            print(f'[info] --limit {args.limit} applied -- THIS IS A PARTIAL RUN '
                  f'({args.limit} of ~{len(urls) + (len(urls))} companies scraped). '
                  f'Output files carry the PARTIAL_ prefix.')

        print(f'[info] {len(urls)} profiles queued for extraction')

        # ── resume from checkpoint ────────────────────────────────────────────
        rows = read_existing(raw_path)
        # Only URLs with a non-empty Profile URL are counted as done.
        # Rows without a URL (failed extractions) are not skipped so the URL
        # can be retried.
        done = {r['Profile URL'] for r in rows if r.get('Profile URL')}
        if done:
            print(f'[resume] {len(done)} profiles already in checkpoint; skipping them.')

        # ── scrape ────────────────────────────────────────────────────────────
        for i, u in enumerate(urls, 1):
            if u in done:
                print(f'[{i}/{len(urls)}] skip (checkpoint): {u}')
                continue
            try:
                row = await extract_profile(page, u)
                rows.append(row)
                done.add(u)
                print(f'[{i}/{len(urls)}] {row["Company Name"] or u}')
            except Exception as e:
                print(f'[error] {u}: {e}', file=sys.stderr)

            if len(rows) % args.checkpoint == 0:
                write_csv(raw_path, rows)
                print(f'[checkpoint] {len(rows)} rows -> {raw_path}')

            await page.wait_for_timeout(args.delay)

        # Final checkpoint write
        write_csv(raw_path, rows)
        print(f'[info] Raw CSV written: {raw_path}  ({len(rows)} rows)')

        # ── clean & deduplicate ───────────────────────────────────────────────
        cleaned = cleanse(rows)

        if is_partial:
            note = (
                f'PARTIAL RUN — only first {args.limit} of ~1,614 companies scraped. '
                f'Re-run without --limit for the complete dataset.'
            )
            for r in cleaned:
                r['Run_Note'] = note
            extra = ['Run_Note']
        else:
            extra = []

        write_csv(final_path, cleaned, extra_fields=extra)

        # ── missing-field QA ──────────────────────────────────────────────────
        required_fields = [f for f in FIELDS if f != 'Profile URL']
        bad = [r for r in cleaned if any(not r.get(k) for k in required_fields)]
        write_csv(missing_path, bad, extra_fields=extra)

        await browser.close()

    # ── summary ───────────────────────────────────────────────────────────────
    print()
    print('=== RUN COMPLETE ===')
    if is_partial:
        print(f'  WARNING: PARTIAL RUN -- {args.limit} profiles only.')
        print(f'  WARNING: Do not submit PARTIAL_ files as the final dataset.')
    print(f'  Raw checkpoint : {raw_path}')
    print(f'  Final CSV      : {final_path}   ({len(cleaned)} rows)')
    print(f'  Missing fields : {missing_path}  ({len(bad)} rows)')
    print()
    print('Site limitations reflected in the data:')
    print('  * Address  -- hidden behind login wall; always blank in this scrape.')
    print('  * Website  -- hidden behind login wall; always blank in this scrape.')
    print('  * Email    -- extracted from invisible mailto: href; present when exposed.')
    print('  * Telephone-- extracted from invisible tel: href; present when exposed.')
    print('  * Sales Markets, Primary Business Activity -- only on manufacturer profiles.')
    print()
    print('To fetch the five assignment count questions, run:')
    print('  python scraper.py --counts')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Scrape supplier profiles from IngredientsNetwork.com'
    )
    ap.add_argument('--output',     default='data',
                    help='Output directory (default: data)')
    ap.add_argument('--headed',     action='store_true',
                    help='Run with a visible browser window')
    ap.add_argument('--limit',      type=int, default=0,
                    help='Scrape only the first N profiles (0 = all). '
                         'Output files are prefixed PARTIAL_ when this is set.')
    ap.add_argument('--delay',      type=int, default=350,
                    help='Delay between profile requests in milliseconds (default: 350)')
    ap.add_argument('--checkpoint', type=int, default=25,
                    help='Save raw CSV every N completed profiles (default: 25)')
    ap.add_argument('--counts',     action='store_true',
                    help='Fetch the five assignment category counts and exit')
    asyncio.run(main(ap.parse_args()))
