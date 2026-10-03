import argparse, asyncio, csv, os, re, sys
from pathlib import Path
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE='https://www.ingredientsnetwork.com/'
FIELDS=['Company Name','Company Description','Sales Markets','Primary Business Activity','Categories','Events','Address','Email','Telephone','Website','Profile URL']


def clean(s):
    return re.sub(r'\s+',' ', (s or '')).strip()

def uniq(xs):
    out=[]; seen=set()
    for x in xs:
        x=clean(x)
        if x and x not in seen: seen.add(x); out.append(x)
    return out

def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        w=csv.DictWriter(f, fieldnames=FIELDS); w.writeheader(); w.writerows(rows)

def read_existing(path):
    if not path.exists(): return []
    with path.open(encoding='utf-8-sig', newline='') as f: return list(csv.DictReader(f))

async def accept_cookies(page):
    for label in ['Accept All Cookies','Accept all','I agree','Accept']:
        try:
            b=page.get_by_role('button', name=re.compile(label,re.I)).first
            if await b.is_visible(timeout=700): await b.click(); return
        except: pass

async def txt(page, selectors):
    for sel in selectors:
        try:
            loc=page.locator(sel).first
            if await loc.count():
                t=clean(await loc.inner_text(timeout=1200))
                if t: return t
        except: pass
    return ''

async def section_after_heading(page, heading_regex, stop_headings=True):
    # Extract text following a heading/label until the next heading. Works even if classes change.
    try:
        val=await page.evaluate('''(pat)=>{
          const rx=new RegExp(pat,'i'); const all=[...document.querySelectorAll('h1,h2,h3,h4,h5,h6,dt,strong,b')];
          const h=all.find(e=>rx.test((e.innerText||'').trim())); if(!h) return '';
          if(h.tagName==='DT'){ const dd=h.nextElementSibling; return dd ? dd.innerText : ''; }
          let out=[]; let n=h.nextElementSibling;
          while(n && !/^H[1-6]$/.test(n.tagName)){ out.push(n.innerText||''); n=n.nextElementSibling; }
          if(out.join(' ').trim()) return out.join(' ');
          const p=h.parentElement; if(p){
            let sib=p.nextElementSibling; out=[];
            while(sib && !sib.querySelector?.('h1,h2,h3,h4,h5,h6') && !/^H[1-6]$/.test(sib.tagName)){out.push(sib.innerText||''); sib=sib.nextElementSibling;}
            return out.join(' ');
          } return '';
        }''', heading_regex)
        return clean(val)
    except: return ''

async def label_value(page, label):
    # DOM text label fallback
    try:
        v=await page.evaluate('''(label)=>{
          const norm=s=>(s||'').replace(/\\s+/g,' ').trim();
          const els=[...document.querySelectorAll('dt,h3,h4,strong,b,div,span')];
          const e=els.find(x=>norm(x.innerText).toLowerCase()===label.toLowerCase());
          if(!e) return '';
          if(e.tagName==='DT' && e.nextElementSibling) return norm(e.nextElementSibling.innerText);
          let n=e.nextElementSibling; if(n) return norm(n.innerText);
          return '';
        }''', label)
        return clean(v)
    except: return ''

async def extract_profile(page, url):
    await page.goto(url, wait_until='domcontentloaded', timeout=60000)
    await accept_cookies(page)
    try: await page.wait_for_load_state('networkidle', timeout=8000)
    except: pass

    name=await txt(page,['h1'])
    desc=await section_after_heading(page,'^Company description$')
    if not desc: desc=await section_after_heading(page,'^About$')

    sales=await label_value(page,'Sales markets')
    activity=await label_value(page,'Primary business activity')
    cats=await label_value(page,'Affiliated categories:') or await label_value(page,'Affiliated categories')
    # Expand categories section if available
    cat_section=await section_after_heading(page,'^Categories affiliated with')
    if cat_section: cats=cat_section

    # Events: capture event blocks around Upcoming events / Recently at.
    events=[]
    for hdr in ['Upcoming events','Recently at']:
        sec=await section_after_heading(page,f'^{hdr}$')
        if sec: events.append(f'{hdr}: {sec}')

    address=await label_value(page,'Address')
    telephone=await label_value(page,'Telephone')
    website=await label_value(page,'Website')
    email=await label_value(page,'Email')

    # Semantic href fallbacks for contact fields
    try:
        mail=page.locator('a[href^="mailto:"]').first
        if await mail.count(): email=clean((await mail.get_attribute('href')).replace('mailto:','').split('?')[0])
    except: pass
    try:
        tel=page.locator('a[href^="tel:"]').first
        if await tel.count(): telephone=clean((await tel.get_attribute('href')).replace('tel:',''))
    except: pass

    # Website: external link in/near contact section, avoiding IN social/site links.
    if not website:
        try:
            links=await page.locator('a[href^="http"]').evaluate_all("els=>els.map(a=>[a.innerText,a.href])")
            for text,href in links:
                host=urlparse(href).netloc.lower()
                if 'ingredientsnetwork.com' not in host and not any(x in host for x in ['facebook.com','linkedin.com','twitter.com','x.com','youtube.com','instagram.com']):
                    if re.search(r'website|visit', text or '', re.I): website=href; break
        except: pass

    return {'Company Name':name,'Company Description':desc,'Sales Markets':sales,
            'Primary Business Activity':activity,'Categories':cats,'Events':' | '.join(uniq(events)),
            'Address':address,'Email':email,'Telephone':telephone,'Website':website,'Profile URL':url}

async def discover_via_search(page, max_pages=0):
    """Follow assignment flow: home -> Search -> collect company profile links -> pagination."""
    await page.goto(BASE, wait_until='domcontentloaded', timeout=60000); await accept_cookies(page)
    # Click visible Search button. Empty search is intentional: challenge asks to click Search to enumerate cards.
    clicked=False
    for candidate in [page.get_by_role('button',name=re.compile('^Search$',re.I)),page.get_by_role('link',name=re.compile('^Search$',re.I)),page.locator('input[type="submit"][value*="Search" i]')]:
        try:
            if await candidate.first.is_visible(timeout=1500): await candidate.first.click(); clicked=True; break
        except: pass
    if not clicked: print('[warn] Search control not found; using supplier directory fallback.', file=sys.stderr); return []
    try: await page.wait_for_load_state('domcontentloaded',timeout=30000)
    except: pass

    found=[]; seen_pages=set(); pno=0
    while True:
        pno+=1; cur=page.url
        if cur in seen_pages: break
        seen_pages.add(cur)
        hrefs=await page.locator('a[href*="-comp"]').evaluate_all('els=>els.map(a=>a.href)')
        new=uniq([h.split('#')[0] for h in hrefs if re.search(r'-comp\d+\.html',h)])
        found.extend(new); found=uniq(found)
        print(f'[discover] page {pno}: +{len(new)} links, {len(found)} unique')
        if max_pages and pno>=max_pages: break
        nxt=None
        for loc in [page.get_by_role('link',name=re.compile(r'^(Next|›|»)$',re.I)),page.locator('a[rel="next"]'),page.locator('.pagination a:has-text("Next")')]:
            try:
                if await loc.first.is_visible(timeout=700): nxt=loc.first; break
            except: pass
        if not nxt: break
        old=page.url
        try:
            await nxt.click(); await page.wait_for_load_state('domcontentloaded',timeout=30000)
            if page.url==old: await page.wait_for_timeout(1500)
        except: break
    return found

async def discover_directory(page):
    """Robust fallback: Ingredients Network exposes alphabetical supplier directory pages."""
    urls=[]
    for c in 'abcdefghijklmnopqrstuvwxyz':
        u=urljoin(BASE,f'company/{c}.html')
        try:
            await page.goto(u,wait_until='domcontentloaded',timeout=30000)
            hrefs=await page.locator('a[href*="-comp"]').evaluate_all('els=>els.map(a=>a.href)')
            urls.extend([h.split('#')[0] for h in hrefs if re.search(r'-comp\d+\.html',h)])
            # Follow prefix directory links (e.g. CA, CB...)
            prefixes=await page.locator('a[href*="/company/"]').evaluate_all('els=>els.map(a=>a.href)')
            for pu in uniq(prefixes):
                if pu==u or not re.search(r'/company/[a-z0-9]{2,}\.html$',pu,re.I): continue
                try:
                    await page.goto(pu,wait_until='domcontentloaded',timeout=30000)
                    hs=await page.locator('a[href*="-comp"]').evaluate_all('els=>els.map(a=>a.href)')
                    urls.extend([h.split('#')[0] for h in hs if re.search(r'-comp\d+\.html',h)])
                except: pass
            print(f'[directory] {c.upper()}: {len(uniq(urls))} unique')
        except Exception as e: print(f'[warn] directory {c}: {e}')
    return uniq(urls)

def cleanse(rows):
    cleaned=[]
    for r in rows:
        x={k:clean(r.get(k,'')) for k in FIELDS}
        # Normalize semicolon/list-like whitespace without inventing missing data.
        for k in ['Sales Markets','Primary Business Activity','Categories','Events']:
            x[k]=re.sub(r'\s*;\s*','; ',x[k])
        cleaned.append(x)
    # de-dupe profiles, preserving first complete scrape
    best={}
    for r in cleaned:
        key=r['Profile URL'] or r['Company Name'].lower()
        if key not in best or sum(bool(v) for v in r.values())>sum(bool(v) for v in best[key].values()): best[key]=r
    return list(best.values())

async def main(args):
    out=Path(args.output); raw=out/'temporary_raw.csv'; final=out/'ingredientsnetwork_companies.csv'; missing=out/'missing_required_fields.csv'
    out.mkdir(parents=True,exist_ok=True)
    async with async_playwright() as p:
        # Prefer Playwright's managed Chromium, but use an installed Chrome when
        # its browser download is unavailable on the machine.
        launch_options={'headless': not args.headed}
        chrome=Path(os.environ.get('PROGRAMFILES', r'C:\\Program Files')) / 'Google' / 'Chrome' / 'Application' / 'chrome.exe'
        if chrome.exists(): launch_options['executable_path']=str(chrome)
        browser=await p.chromium.launch(**launch_options)
        ctx=await browser.new_context(viewport={'width':1440,'height':1000}, user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36')
        page=await ctx.new_page()
        urls=await discover_via_search(page,args.max_pages)
        if not urls: urls=await discover_directory(page)
        if args.limit: urls=urls[:args.limit]
        print(f'[info] {len(urls)} company profiles discovered')
        rows=read_existing(raw); done={r.get('Profile URL') for r in rows}
        for i,u in enumerate(urls,1):
            if u in done: continue
            try:
                row=await extract_profile(page,u); rows.append(row); done.add(u)
                print(f'[{i}/{len(urls)}] {row["Company Name"] or u}')
            except Exception as e: print(f'[error] {u}: {e}',file=sys.stderr)
            if len(rows)%args.checkpoint==0: write_csv(raw,rows)
            await page.wait_for_timeout(args.delay)
        write_csv(raw,rows)
        cleaned=cleanse(rows); write_csv(final,cleaned)
        required=FIELDS[:-1]
        bad=[r for r in cleaned if any(not r[k] for k in required)]
        write_csv(missing,bad)
        await browser.close()
    print(f'Raw:   {raw}\nFinal: {final}\nRows with missing required fields: {len(bad)} -> {missing}')
    if bad:
        print('NOTE: The site does not necessarily publish every contact field for every supplier. Missing values are reported rather than fabricated.')

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--output',default='data'); ap.add_argument('--headed',action='store_true')
    ap.add_argument('--limit',type=int,default=0,help='0 = all discovered companies')
    ap.add_argument('--max-pages',type=int,default=0,help='limit search pagination for testing')
    ap.add_argument('--delay',type=int,default=350,help='delay between company pages, milliseconds')
    ap.add_argument('--checkpoint',type=int,default=25)
    asyncio.run(main(ap.parse_args()))
