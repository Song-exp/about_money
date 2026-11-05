"""Collect a bounded public Shinhan query via its UI, without login or publication."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.robotparser import RobotFileParser
from urllib.parse import parse_qs
import httpx
from playwright.sync_api import sync_playwright, expect
from app.services.issuer_merchant_matching import DIRECTORY_URL, LOOKUP_URL, parse_shinhan_directory


def query_fields(raw):
    """Retain only public search fields; never archive request headers or cookies."""
    found = []
    def visit(value, depth=0):
        if depth > 6:
            return
        if isinstance(value, str):
            try:
                visit(json.loads(value), depth+1)
            except (ValueError, RecursionError):
                if '=' in value:
                    visit(parse_qs(value,keep_blank_values=True),depth+1)
        elif isinstance(value,list):
            for item in value:
                visit(item,depth+1)
        elif isinstance(value, dict):
            if 'NXT_QY_KEY' in value and 'mchtNm' in value:
                found.append({k: value.get(k) for k in ('NXT_QY_KEY','mchtNm','siDo','siGunGu','areaCd','pntMctYn')})
            else:
                for item in value.values():
                    visit(item, depth+1)
    visit(raw)
    if len(found) != 1:
        raise ValueError('issuer_lookup_request_unrecognized')
    return found[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keyword', required=True)
    parser.add_argument('--max-pages', type=int, default=5, choices=range(1,11))
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    if not 2 <= len(args.keyword) <= 30:
        parser.error('keyword must have 2 to 30 characters')
    if (args.output_dir/'capture.json').exists():
        parser.error('capture.json already exists; choose a new output directory')
    with httpx.Client(trust_env=False,timeout=30,follow_redirects=False) as client:
        robots = client.get('https://www.shinhancard.com/robots.txt')
        if robots.status_code != 404:
            robots.raise_for_status()
            if robots.is_redirect:
                raise ValueError('robots_redirect_requires_configuration')
            policy = RobotFileParser()
            policy.parse(robots.text.splitlines())
            if not all(policy.can_fetch('PayPickMapSync/1.0',url) for url in (DIRECTORY_URL,LOOKUP_URL)):
                raise ValueError('source_disallows_fetch')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    manifest = {'schema_version':1,'issuer_key':'shinhan','source_url':DIRECTORY_URL,'lookup_url':LOOKUP_URL,
        'keyword':args.keyword,'region':'서울 동대문구','complete':False,'pages':[],
        'capture_method':'anonymous_public_browser','public_approval':False}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge',headless=True)
        try:
            page = browser.new_page(viewport={'width':390,'height':844},is_mobile=True)
            page.goto(DIRECTORY_URL,wait_until='networkidle',timeout=60000)
            page.get_by_role('tab',name='신한카드',exact=True).click(timeout=15000)
            page.locator('#selAreaTxt1').click(timeout=15000)
            page.get_by_text('서울',exact=True).click(timeout=15000)
            page.get_by_text('동대문구',exact=True).click(timeout=15000)
            page.get_by_role('button',name='확인',exact=True).click(timeout=15000)
            expect(page.locator('#selAreaTxt1')).not_to_have_value('',timeout=10000)
            page.locator('#searchText1').press_sequentially(args.keyword,delay=100)
            page.locator('#searchText1').press('End')
            cursor = ''
            for index in range(args.max_pages):
                if index:
                    page.wait_for_timeout(2000)
                with page.expect_response(lambda r:r.url.split('?')[0]==LOOKUP_URL,timeout=30000) as pending:
                    button = page.locator('#searchBtn1') if index==0 else page.locator('#tab-panel1-1 button[data-bind-click="more()"]')
                    button.click(timeout=15000)
                response = pending.value
                if response.status != 200:
                    raise ValueError('issuer_lookup_http_error')
                raw = response.body()
                parsed = parse_shinhan_directory(raw)
                request = query_fields(response.request.post_data or '')
                if request['NXT_QY_KEY'] != cursor or request['mchtNm'] != args.keyword or request['pntMctYn'] != 'N':
                    raise ValueError('issuer_lookup_query_changed')
                filename = f'page-{index:03}.json'
                (args.output_dir/filename).write_bytes(raw)
                manifest['pages'].append({'raw_ref':filename,'raw_sha256':hashlib.sha256(raw).hexdigest(),
                    'received_at':datetime.now(timezone.utc).isoformat(),'http_status':200,'request':request})
                manifest['complete'] = parsed['complete']
                (args.output_dir/'capture.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
                if parsed['complete']:
                    break
                next_cursor = json.loads(raw)['mbw_json']['nxtQyKey']
                if next_cursor == cursor:
                    raise ValueError('issuer_lookup_cursor_repeated')
                cursor = next_cursor
        finally:
            browser.close()
    print(json.dumps({'pages':len(manifest['pages']),'complete':manifest['complete'],
        'keyword':args.keyword,'manifest':str(args.output_dir/'capture.json'),'public_approval':False},ensure_ascii=False))


if __name__ == '__main__':
    main()
