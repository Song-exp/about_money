"""Real admin UI/API workflow with an isolated DB and mocked external lookup."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright, expect
from sqlalchemy import delete, select, func

from app.models.map_verification import MapReviewEvent, MapLocation
from app.services import store_directory_revalidation as lookup
from tests.browser_e2e_scholarships import static_site
from test_store_directory_publication import prepared, change
from test_store_directory_revalidation import document


def test_review_revalidate_preview_apply_in_browser(db, prepared, static_site, monkeypatch):
    change, store, _ = prepared
    db.execute(delete(MapReviewEvent).where(MapReviewEvent.action.in_([
        'store_directory_reviewed', 'store_directory_place_check'])))
    db.commit()
    doc = document(change)
    monkeypatch.setattr(lookup, 'lookup_place', lambda candidate, client: (candidate['name'], [doc], 'a' * 64))
    output = Path('reports/store-directory-ui-20261020')
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1200, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(static_site + '/store-directory-review.html')
        page.get_by_label('관리자 토큰', exact=True).fill('wrong')
        page.get_by_role('button', name='변경 목록 새로고침').click()
        expect(page.locator('#message')).to_contain_text('관리자 토큰')
        page.get_by_label('관리자 토큰', exact=True).fill('test-admin-token')
        page.locator('#sources').click()
        expect(page.locator('#source-status')).to_contain_text('placeRevalidation')
        page.get_by_role('button', name='변경 목록 새로고침').click()
        page.locator('#queue button').click()
        expect(page.locator('#check')).to_be_disabled()
        page.get_by_label('공식 변경 검토 메모').fill('격리 원문과 전체 주소를 확인했습니다.')
        page.locator('#acknowledge').click()
        expect(page.locator('#review-state')).to_contain_text('검토 완료')
        page.locator('#deltas button').click()
        page.locator('#check').click()
        expect(page.locator('#check-state')).to_contain_text('장소 대조 통과')
        page.get_by_label('반영할 기존 매장').select_option(str(store.id))
        page.locator('#preview').click()
        expect(page.locator('#before')).to_contain_text('4층')
        expect(page.locator('#after')).to_contain_text('5층')
        expect(page.locator('#apply')).to_be_disabled()
        page.get_by_label('검토자', exact=True).fill('browser-reviewer')
        page.get_by_label('지도 반영 메모').fill('공식 ID와 변경 전후를 확인했습니다.')
        page.get_by_label('변경 전후와 대상 매장을 확인했습니다.').check()
        expect(page.locator('#apply')).to_be_enabled()
        page.screenshot(path=str(output / 'preview.png'), full_page=True)
        page.locator('#apply').click()
        expect(page.locator('#message')).to_contain_text('지도 반영 완료')
        expect(page.locator('#apply')).to_be_disabled()
        db.expire_all()
        assert store.address.endswith('5층')
        assert db.get(MapLocation, ('store', store.id)).place_id == '123'
        assert db.scalar(select(func.count()).select_from(MapReviewEvent).where(
            MapReviewEvent.action == 'store_directory_applied')) == 1
        assert page.evaluate('localStorage.length') == 0 and page.evaluate('sessionStorage.length') == 0
        assert not errors
        (output / 'browser-result.json').write_text(json.dumps({'scope': 'isolated_db_mocked_kakao',
            'review_revalidation_preview_apply': True, 'token_not_stored': True,
            'console_errors': errors}, indent=2), encoding='utf-8')
        browser.close()


def test_pending_lookup_and_stale_target_cannot_apply(db, prepared, static_site, monkeypatch):
    change, store, _ = prepared
    monkeypatch.setattr(lookup, 'lookup_place', lambda candidate, client: (candidate['name'], [], 'a' * 64))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page()
        page.goto(static_site + '/store-directory-review.html')
        page.locator('#token').fill('test-admin-token')
        page.locator('#reload').click()
        page.locator('#queue button').click()
        page.locator('#deltas button').click()
        page.locator('#target').select_option(str(store.id))
        page.locator('#preview').click()
        expect(page.locator('#preview-panel')).to_be_visible()
        # An actual DB edit after preview must be rejected by the real apply API.
        store.lat += .0001
        db.commit()
        page.locator('#reviewer').fill('browser-reviewer')
        page.locator('#apply-note').fill('stale target check')
        page.locator('#confirm').check()
        page.locator('#apply').click()
        expect(page.locator('#message')).to_contain_text('매장 정보가 바뀌었습니다')
        expect(page.locator('#preview-panel')).to_be_hidden()
        page.locator('#check').click()
        expect(page.locator('#check-state')).to_contain_text('보류')
        expect(page.locator('#preview')).to_be_disabled()
        expect(page.locator('#apply')).to_be_disabled()
        db.expire_all()
        assert store.address.endswith('4층')
        assert db.scalar(select(func.count()).select_from(MapReviewEvent).where(
            MapReviewEvent.action == 'store_directory_applied')) == 0
        browser.close()
