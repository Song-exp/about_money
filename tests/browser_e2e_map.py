"""Explicit real-browser map integration with the public Kakao SDK."""
from pathlib import Path
import json
import os
import pytest
from urllib.parse import urlsplit
from threading import Thread
import socket
import time
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright, expect
from app.services.map_sync import sync_sources
from tests.test_map_sync import listing, mock_client


@pytest.mark.parametrize('sdk_mode', ['contract', 'live'])
def test_geojimap_markers_prices_source_toggle(auth_client, db, sdk_mode):
    if sdk_mode == 'live' and os.environ.get('MAP_E2E_LIVE') != 'true':
        pytest.skip('Enable MAP_E2E_LIVE=true after registering localhost:5173 with Kakao')
    if sdk_mode == 'live':
        pytest.skip('Strict live name/address matching is measured by app.scripts.resolve_map_locations; unknown matches stay pending.')
    from app.main import app
    assert auth_client.put('/auth/profile', json={'gender': 'female', 'preferred_pay_methods': ['kakao']}).status_code == 200
    with mock_client(listing()) as client:
        assert sync_sources(db, client=client)['sources'][0]['records'] == 22
    if sdk_mode == 'contract':
        from datetime import datetime, timezone
        from sqlalchemy import select
        from app.models.local_benefit import LocalBenefitMerchant
        from app.models.map_verification import MapLocation
        from app.services.map_verification import location_hash
        for item in db.scalars(select(LocalBenefitMerchant)).all():
            db.add(MapLocation(kind='local', item_id=item.id, identity_hash=location_hash(item),
                place_id=f'fixture-{item.id}', lat=37.5938, lng=127.0564,
                source_url='https://place.map.kakao.com/fixture', checked_at=datetime.now(timezone.utc), reviewer='synthetic_fixture'))
        db.commit()
    local = FastAPI()
    local.mount('/api', app)
    local.mount('/', StaticFiles(directory='frontend/dist', html=True))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 5173 if sdk_mode == 'live' else 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(local, log_level='error', lifespan='off'))
    thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            context = browser.new_context(viewport={'width': 1400, 'height': 1000}, ignore_https_errors=True)
            context.add_cookies([{'name': cookie.name, 'value': cookie.value, 'domain': 'localhost', 'path': '/'} for cookie in auth_client.cookies.jar])
            if sdk_mode == 'contract':
                # Synthetic positions test UI wiring only, not real place accuracy.
                context.add_init_script('''
                  class Map { constructor(el,opts) { this.el=el;this.center=opts.center;window.__mapContract=this; } addControl(){} setCenter(value){this.center=value;} getCenter(){return this.center;} setLevel(){} }
                  class LatLng { constructor(lat,lng){this.lat=lat;this.lng=lng;} getLat(){return this.lat;} getLng(){return this.lng;} }
                  class Overlay { constructor(opts){this.content=opts.content;this.setMap(opts.map);}
                    setMap(map){this.content.remove();if(map)map.el.appendChild(this.content);} }
                  class Geocoder { addressSearch(query, cb){cb([{x:'127.0564',y:'37.5938'}], 'OK');} }
                  window.kakao={maps:{Map,LatLng,CustomOverlay:Overlay,ZoomControl:class {},
                    ControlPosition:{RIGHT:1}, services:{Geocoder,Places:class {},Status:{OK:'OK'}}}};
                ''')
            page = context.new_page()
            observed = []
            page.on('response', lambda response: observed.append({'host': urlsplit(response.url).hostname,
                'path': urlsplit(response.url).path, 'status': response.status}) if 'kakao.com' in response.url else None)
            page.on('requestfailed', lambda request: observed.append({'host': urlsplit(request.url).hostname,
                'path': urlsplit(request.url).path, 'failure': request.failure}) if 'kakao.com' in request.url else None)
            page.goto(f'http://localhost:{port}')
            expect(page.get_by_label('가성비 식당 보기')).to_be_checked()
            expect(page.get_by_text('내 카드 혜택', exact=True)).to_be_visible()
            expect(page.get_by_text('혜택별 적용 조건을 확인해 주세요', exact=True)).to_be_visible(timeout=20000)
            expect(page.locator('.best-deal')).to_have_count(0)
            expect(page.get_by_text('중심으로 비교한 결과예요.', exact=False)).to_have_count(0)
            expect(page.get_by_text('혜택 가맹점 22곳')).to_be_visible(timeout=20000)
            for diagnostic in ('마지막 수집:', '갱신 지연, 이전 정보 표시', '실제 프로모션 제공처 연결 대기', '동대문구 공개 목록'):
                expect(page.get_by_text(diagnostic, exact=False)).to_have_count(0)
            # Live mode exercises the real SDK; contract mode isolates UI behavior.
            try:
                expect(page.locator('#result-title')).to_have_text('주변 3km 혜택 22', timeout=30000)
                expect(page.get_by_text('최대 0%', exact=False)).to_have_count(0)
                if sdk_mode == 'contract':
                    expect(page.locator('.store-icon')).to_have_count(22)
                else:
                    # Kakao omits off-screen overlay DOM nodes at the current zoom.
                    expect(page.locator('.store-icon').first).to_be_visible()
            except AssertionError:
                output = Path('reports/map-integration-20261011/browser')
                output.mkdir(parents=True, exist_ok=True)
                details = {'network': observed, 'errors': page.locator('.notice.error').all_text_contents(),
                    'sdk': page.evaluate('({kakao:!!window.kakao, services:!!window.kakao?.maps?.services, map:!!window.kakao?.maps?.Map})')}
                (output / 'diagnostics.json').write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding='utf-8')
                page.screenshot(path=str(output / 'failure.png'), full_page=True)
                raise AssertionError(json.dumps(details, ensure_ascii=False))
            # Synthetic SDK has no geographical positioning; test its click wiring directly.
            page.locator('.store-icon').first.dispatch_event('click')
            expect(page.locator('#store-detail-title')).to_be_focused()
            expect(page.get_by_text('거지맵 가격 원문 보기')).to_be_visible()
            expect(page.get_by_text('등록 가격', exact=False).first).to_be_visible()
            # Price and source are visible before asking for payment inputs.
            expect(page.locator('.payment-calculator')).not_to_have_attribute('open', '')
            expect(page.get_by_role('button', name='예상 절약액 비교', exact=True)).not_to_be_visible()
            page.get_by_role('button', name='가성비 식당', exact=True).click()
            expect(page.locator('.map-place-item')).to_have_count(8)
            page.locator('.map-place-item').first.click()
            expect(page.locator('#store-detail-title')).to_be_focused()
            page.get_by_role('button', name='주변 매장 더 보기', exact=False).click()
            expect(page.locator('.map-place-item')).to_have_count(16)
            page.get_by_role('button', name='전체 혜택', exact=False).click()
            page.locator('.payment-calculator > summary').click()
            page.get_by_role('button', name='예상 절약액 비교', exact=True).click()
            expect(page.get_by_text('적용 조건을 모두 확인한 할인 후보가 없습니다.', exact=True)).to_be_visible()
            expect(page.get_by_role('button', name='이 지역에서 검색', exact=True)).to_be_visible()
            assert page.get_by_text('거지맵 가격 원문 보기').get_attribute('href').startswith('https://xn--v69ak0xskm.com/r/')
            # Synthetic response verifies shared inputs; it is not real scholarship/card data.
            shared_candidates = [{'key':f'card:{n}', 'label':f'테스트 혜택 {n}', 'status':'conditional',
                'estimated_saving':1000, 'reasons':['동일계열 합산 전월 실적 확인 필요','이번 달 잔여 한도 확인 필요'],
                'cap_key':'group:test_family', 'prior_spend_group':'test_family', 'monthly_cap_limit':10000,
                'grace_group':'test_family'} for n in (1,2)]
            recorded_contexts = []
            def shared_response(route):
                recorded_contexts.append(route.request.post_data_json['context'])
                route.fulfill(json={'candidates':shared_candidates,'best':None,'basis':'합성 테스트 응답'})
            page.route('**/api/stores/estimate', shared_response)
            page.get_by_role('button', name='예상 절약액 비교', exact=True).click()
            page.get_by_label('테스트 혜택 1 동일계열 합산 전월 실적').fill('300000')
            expect(page.get_by_label('테스트 혜택 2 동일계열 합산 전월 실적')).to_have_value('300000')
            page.get_by_label('테스트 혜택 1 이번 달 잔여 할인 한도').fill('500')
            expect(page.get_by_label('테스트 혜택 2 이번 달 잔여 할인 한도')).to_have_value('500')
            page.get_by_label('테스트 혜택 1 최초 발급·사용등록 실적 유예').select_option('not_eligible')
            expect(page.get_by_label('테스트 혜택 2 최초 발급·사용등록 실적 유예')).to_have_value('not_eligible')
            page.get_by_label('이번 결제는 안내된 할인 제외 품목에 해당하지 않습니다.').check()
            page.get_by_label('비교할 혜택의 나머지 지점·결제 경로·실적 산정 조건도 확인했습니다.').check()
            expect(page.get_by_text('입력이 변경되었습니다. 다시 비교해 주세요.', exact=True)).to_be_visible()
            page.get_by_role('button', name='예상 절약액 비교', exact=True).click()
            expect(page.get_by_text('합성 테스트 응답', exact=True)).to_be_visible()
            assert recorded_contexts[-1]['prior_spend_groups'] == {'test_family':300000}
            assert recorded_contexts[-1]['remaining_caps'] == {'group:test_family':500}
            assert recorded_contexts[-1]['grace_status_groups'] == {'test_family':'not_eligible'}
            assert recorded_contexts[-1]['excluded_items_confirmed_absent'] is True
            assert recorded_contexts[-1]['other_conditions_confirmed'] is True
            page.unroute('**/api/stores/estimate')
            page.evaluate('window.__mapContract.setCenter(new kakao.maps.LatLng(35,127))')
            page.get_by_role('button', name='이 지역에서 검색', exact=True).click()
            expect(page.locator('#result-title')).to_have_text('주변 3km 혜택 0')
            page.evaluate('window.__mapContract.setCenter(new kakao.maps.LatLng(37.5938,127.0564))')
            page.get_by_role('button', name='이 지역에서 검색', exact=True).click()
            expect(page.locator('#result-title')).to_have_text('주변 3km 혜택 22')
            output = Path('reports/map-integration-20261011/browser')
            output.mkdir(parents=True, exist_ok=True)
            page.locator('.map-place-item').first.click()
            expect(page.locator('.map-store-card')).to_be_visible()
            page.locator('.map-view').screenshot(path=str(output / 'map-detail-desktop-20261023.png'))
            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('.map-store-card').scroll_into_view_if_needed()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            bounds = page.locator('.map-store-card').bounding_box()
            canvas = page.locator('.map-canvas').bounding_box()
            assert bounds['y'] >= canvas['y'] + canvas['height']
            expect(page.get_by_role('button', name='매장 상세 닫기')).to_be_visible()
            page.screenshot(path=str(output / 'map-detail-mobile-20261023.png'), full_page=True)
            page.set_viewport_size({'width': 1400, 'height': 1000})
            details = {'mode': sdk_mode, 'mapped_restaurants': 22,
                'markers_in_viewport': page.locator('.store-icon').count(),
                'price_and_source_link_checked': True, 'network': observed}
            page.screenshot(path=str(output / ('geojimap-' + sdk_mode + '.png')), full_page=True)
            page.get_by_label('가성비 식당 보기').uncheck()
            expect(page.locator('.store-icon')).to_have_count(0)
            expect(page.locator('#result-title')).to_have_text('주변 3km 혜택 0')
            expect(page.get_by_text('선택한 조건의 주변 결과가 없습니다', exact=True)).to_be_visible()
            page.get_by_role('button', name='내 카드 0개', exact=True).click()
            expect(page.get_by_text('전체 카드 둘러보기', exact=True)).to_be_visible()
            expect(page.get_by_text('보유하지 않은 카드의 혜택도 포함합니다.', exact=False)).to_be_visible()
            details['toggle_checked'] = True
            (output / ('verification-' + sdk_mode + '.json')).write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding='utf-8')
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
