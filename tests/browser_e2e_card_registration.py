"""Built UI + real HTTP API, isolated approval fixture, mocked map geometry."""
import json
from datetime import timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

from tests.browser_e2e_scholarships import static_site
from tests.test_card_publication import draft, publish, NOW, KST


def test_store_registration_retry_estimate_and_mypage_persistence(auth_client, db, draft, static_site):
    publication, _ = publish(db, draft)
    assert auth_client.put('/auth/profile', json={'gender': 'female', 'preferred_pay_methods': ['kakao']}).status_code == 200
    output = Path('reports/card-registration-20261015')
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1100})
        context.route('https://dapi.kakao.com/**', lambda route: route.abort())
        context.add_cookies([{'name': name, 'value': value, 'url': static_site, 'httpOnly': True}
            for name, value in auth_client.cookies.items()])
        context.add_init_script('''
          class Map { constructor(el,opts) { this.el=el;this.center=opts.center; } addControl(){} setCenter(v){this.center=v;} getCenter(){return this.center;} setLevel(){} }
          class LatLng { constructor(lat,lng){this.lat=lat;this.lng=lng;} getLat(){return this.lat;} getLng(){return this.lng;} }
          class Overlay { constructor(opts){this.content=opts.content;this.setMap(opts.map);} setMap(map){this.content.remove();if(map)map.el.appendChild(this.content);} }
          class Geocoder { addressSearch(query, cb){cb([{x:'127.05',y:'37.59'}], 'OK');} }
          window.kakao={maps:{Map,LatLng,CustomOverlay:Overlay,ZoomControl:class {},ControlPosition:{RIGHT:1},services:{Geocoder,Places:class {},Status:{OK:'OK'}}}};
        ''')
        page = context.new_page()
        page.goto(static_site, wait_until='domcontentloaded')
        page.get_by_role('button', name='내 카드 0개', exact=True).click()
        expect(page.locator('.store-icon')).to_have_count(1)
        page.locator('.store-icon').click()
        register = page.get_by_role('button', name='토스뱅크 체크카드 보유 카드로 등록', exact=True)
        expect(register).to_be_visible()
        attempts = []
        def register_response(route):
            attempts.append(route.request.method)
            if len(attempts) == 1:
                route.fulfill(status=503, json={'detail': '테스트: 일시적인 저장 오류입니다.'})
            else:
                route.continue_()
        page.route(f'**/api/auth/cards/{publication.card_id}', register_response)
        register.click()
        expect(page.get_by_role('alert')).to_contain_text('일시적인 저장 오류')
        assert auth_client.get('/auth/me').json()['card_ids'] == []
        expect(register).to_be_enabled()
        register.click()
        expect(page.get_by_text('등록된 보유 카드 · 적용 조건을 확인해 주세요.', exact=True)).to_be_visible()
        assert attempts == ['POST', 'POST']
        assert auth_client.get('/auth/me').json()['card_ids'] == [publication.card_id]
        page.locator('.payment-calculator > summary').click()
        page.get_by_role('button', name='예상 절약액 비교', exact=True).click()
        expect(page.get_by_text('적용 조건을 모두 확인한 할인 후보가 없습니다.', exact=True)).to_be_visible()
        page.get_by_label('현재 적용 중인 캐시백 모드').select_option('offline')
        day = (NOW.astimezone(KST) - timedelta(days=1)).date().isoformat()
        page.get_by_label('이 모드가 적용된 날짜 (한국 시간 0시)').fill(day)
        page.get_by_label('오늘 이 영역에서 받은 횟수 (모르면 비워두세요)').fill('0')
        page.get_by_label('이번 달 이 영역에서 받은 횟수 (모르면 비워두세요)').fill('0')
        page.get_by_label('오늘 이 영역의 첫 결제인가요?').select_option('true')
        page.get_by_label('이번 달 이 영역의 남은 캐시백 금액 (모르면 비워두세요)').fill('5000')
        page.get_by_label('이번 결제는 안내된 할인 제외 품목에 해당하지 않습니다.').check()
        page.get_by_label('비교할 혜택의 나머지 지점·결제 경로·실적 산정 조건도 확인했습니다.').check()
        page.get_by_role('button', name='예상 절약액 비교', exact=True).click()
        expect(page.get_by_text('입력 조건 기준 최대 예상 절약액: 500원', exact=True)).to_be_visible()
        page.screenshot(path=str(output / 'registered-estimate.png'), full_page=True)
        page.get_by_role('tab', name='마이페이지', exact=True).click()
        page.get_by_role('tab', name='보유 카드', exact=True).click()
        expect(page.get_by_text('토스뱅크 체크카드', exact=True)).to_be_visible()
        page.reload(wait_until='domcontentloaded')
        expect(page.get_by_text('토스뱅크 체크카드', exact=True)).to_be_visible()
        assert page.evaluate("JSON.parse(localStorage.getItem('tmi.profile.v1')).cardIds") == [publication.card_id]
        (output / 'browser-result.json').write_text(json.dumps({
            'scope': 'isolated_fixture_not_live_card_approval', 'retry_after_failure': True,
            'unknown_conditions_no_estimate': True, 'explicit_conditions_estimate_krw': 500,
            'mypage_and_reload_persisted': True}, indent=2), encoding='utf-8')
        browser.close()
