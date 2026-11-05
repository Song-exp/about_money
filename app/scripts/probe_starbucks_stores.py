"""Capture public store-search results through Starbucks' own browser UI."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright
from urllib.parse import parse_qs, urlparse


def is_starbucks_search_response(response, query):
    parsed = urlparse(response.url)
    return (parsed.scheme == 'https' and parsed.netloc == 'www.starbucks.co.kr'
        and parsed.path == '/store/getStore.do' and response.request.method == 'POST'
        and parse_qs(response.request.post_data or '').get('search_text') == [query])


def launch_official_browser(playwright):
    # Local Windows uses the installed Edge; containers use Playwright Chromium.
    options = {'headless': True}
    if os.name == 'nt':
        options['channel'] = 'msedge'
    return playwright.chromium.launch(**options)


def capture_starbucks(query, source_url):
    parsed = urlparse(source_url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    if (not query.strip() or len(query) > 50 or parsed.scheme != 'https'
            or parsed.netloc != 'www.starbucks.co.kr' or parsed.path != '/store/store_map.do'
            or parsed.fragment or set(params) - {'in_biz_cd'}
            or ('in_biz_cd' in params and (len(params['in_biz_cd']) != 1 or not params['in_biz_cd'][0].isdigit()))):
        raise ValueError('invalid_official_store_request')
    with sync_playwright() as p:
        browser = launch_official_browser(p)
        try:
            page = browser.new_page()
            page.on('dialog', lambda dialog: dialog.dismiss())
            page.goto(source_url, wait_until='domcontentloaded', timeout=45000)
            page.wait_for_selector('#quickSearchText', timeout=30000)
            page.locator('#quickSearchText').fill(query)
            with page.expect_response(lambda r: is_starbucks_search_response(r, query), timeout=30000) as pending:
                page.locator('#quickSearchText').press('Enter')
            response = pending.value
            if response.status != 200:
                raise RuntimeError('official_store_search_failed')
            if len(response.body()) > 2_000_000:
                raise ValueError('official_response_too_large')
            return json.dumps(response.json(), ensure_ascii=False)
        finally:
            browser.close()


def main():
    root = Path('reports/map-improvement-20261013-brand-locations')
    root.mkdir(parents=True, exist_ok=True)
    captured = []
    with sync_playwright() as p:
        browser = launch_official_browser(p)
        page = browser.new_page()
        page.on('dialog', lambda dialog: dialog.dismiss())

        def record(response):
            if '/store/getStore.do' not in response.url:
                return
            try:
                data = response.json()
                captured.append({'status': response.status,
                    'request': response.request.post_data, 'response': data})
            except Exception:
                pass

        page.on('response', record)
        page.goto('https://www.starbucks.co.kr/store/store_map.do', wait_until='domcontentloaded', timeout=45000)
        page.wait_for_selector('#quickSearchText', timeout=30000)
        page.locator('#quickSearchText').fill('경희대')
        page.locator('#quickSearchText').press('Enter')
        page.wait_for_timeout(4000)
        diagnostics = page.evaluate('''() => ({
            search: typeof $search === 'object' ? $search : null,
            searchButtons: Array.from(document.querySelectorAll('a,button')).filter(e=>
                (e.className+' '+e.id).toLowerCase().includes('quick')).map(e=>
                ({id:e.id,className:e.className,text:e.textContent.trim()}))
        })''')
        (root/'starbucks-browser.json').write_text(json.dumps({'captures': captured, 'diagnostics': diagnostics},
            ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'captures':len(captured),'counts':[len(x['response'].get('list',[])) for x in captured],
                          'search_buttons':diagnostics['searchButtons']}, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()
