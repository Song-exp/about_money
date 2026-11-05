"""Admin HTML against the real approval API in an isolated database."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright, expect
from sqlalchemy import select, func

from app.models.card_publication import CardProgramPublication
from app.models.map_verification import MapReviewEvent
from tests.browser_e2e_scholarships import static_site
from tests.test_card_publication import draft
from tests.test_card_document_crosscheck import documents
from tests.test_deep_oil_extraction import draft_deep_oil
from tests.test_card_usage_cap_calculation import scenario as usage_scenario
from tests.test_shinhan_disclosure import attach_deep_oil_leaflet
from tests.test_card_program_changes import next_version
from tests.test_card_publication import publish, NOW
from datetime import timedelta


def test_admin_review_approval_renewal_revocation_and_blocked_source(db, draft, static_site):
    version, source, snapshot, _ = draft
    output = Path('reports/card-review-20261015')
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(viewport={'width': 1300, 'height': 1000})
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(static_site + '/card-program-review.html', wait_until='domcontentloaded')
        page.get_by_label('관리자 토큰').fill('wrong')
        page.get_by_role('button', name='목록 새로고침').click()
        expect(page.locator('#message')).to_contain_text('관리자 토큰')
        page.get_by_label('관리자 토큰').fill('test-admin-token')

        def load():
            page.get_by_role('button', name='목록 새로고침').click()
            page.locator('#queue button').click()
            expect(page.locator('#detail')).to_be_visible()

        def complete_review():
            page.get_by_label('검토자', exact=True).fill('browser-reviewer')
            page.get_by_label('검토 사유', exact=True).fill('격리 테스트 원문과 제외 조건 전체를 대조했습니다.')
            for checkbox in page.locator('#review-form input[type=checkbox]').all():
                checkbox.check()

        load()
        expect(page.locator('#approve')).to_be_disabled()
        assert page.get_by_label('보관한 원문 텍스트', exact=True).input_value() == snapshot.extracted_text
        assert db.scalar(select(func.count()).select_from(CardProgramPublication)) == 0
        complete_review()
        expect(page.locator('#approve')).to_be_enabled()
        page.locator('#approve').click()
        expect(page.locator('#message')).to_contain_text('검토를 저장했습니다. 상태: 게시 가능')
        db.expire_all()
        assert db.scalar(select(func.count()).select_from(CardProgramPublication)) == 1
        assert db.scalar(select(func.count()).select_from(MapReviewEvent)) == 1
        expect(page.locator('#approve')).to_have_text('재검토 후 승인')
        complete_review()
        page.get_by_label('재검토까지 시간').fill('1')
        page.locator('#approve').click()
        expect(page.locator('#message')).to_contain_text('검토를 저장했습니다.')
        page.get_by_label('검토자', exact=True).fill('browser-reviewer')
        page.get_by_label('검토 사유', exact=True).fill('철회 동작과 감사 기록을 검증합니다.')
        page.locator('#revoke').click()
        expect(page.locator('#message')).to_have_text('게시를 철회했습니다.')
        db.expire_all()
        assert db.scalar(select(func.count()).select_from(MapReviewEvent)) == 3
        assert db.get(CardProgramPublication, version.program_key).status == 'revoked'
        source.last_error = 'test_source_failure'
        db.commit()
        load()
        expect(page.locator('#blocker')).to_contain_text('출처 이용 불가')
        complete_review()
        expect(page.locator('#approve')).to_be_disabled()
        assert page.evaluate('localStorage.length') == 0
        assert page.evaluate('sessionStorage.length') == 0
        assert errors == []
        page.screenshot(path=str(output / 'blocked-review.png'), full_page=False)
        (output / 'browser-result.json').write_text(json.dumps({'scope': 'isolated_approval_fixture',
            'admin_auth_checked': True, 'approval_renewal_revocation': True,
            'blocked_source_cannot_approve': True, 'token_not_stored': True}, indent=2), encoding='utf-8')
        browser.close()


def test_conflicting_leaflet_is_visible_and_cannot_be_approved(db, documents, static_site):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page()
        page.goto(static_site + '/card-program-review.html')
        page.get_by_label('관리자 토큰').fill('test-admin-token')
        page.get_by_role('button', name='목록 새로고침').click()
        page.locator('#queue button').click()
        expect(page.locator('#supporting')).to_be_visible()
        expect(page.locator('#supporting-result')).to_contain_text('discount_exclusions.아파트관리비')
        expect(page.locator('#supporting-result')).to_contain_text('conflict')
        page.get_by_label('검토자', exact=True).fill('browser-reviewer')
        page.get_by_label('검토 사유', exact=True).fill('공식 원문 사이 제외 조건 충돌을 확인했습니다.')
        for checkbox in page.locator('#review-form input[type=checkbox]').all():
            checkbox.check()
        expect(page.locator('#approve')).to_be_disabled()
        expect(page.locator('#policy-review')).to_be_visible()
        assert '1,000.5원' in page.locator('#policy-inquiry').input_value()
        with page.expect_download() as download:
            page.locator('#download-inquiry').click()
        output=Path('reports/card-policy-review-20261016')
        output.mkdir(parents=True,exist_ok=True)
        download.value.save_as(str(output/'day-inquiry.txt'))
        inquiry=(output/'day-inquiry.txt').read_text(encoding='utf-8')
        assert '아파트관리비' in inquiry and 'test-admin-token' not in inquiry
        page.get_by_text('실적·한도 계산 점검 (관리자 전용)', exact=True).click()
        page.locator('#scenario').fill(json.dumps({'amount':10000,'brand':'스타벅스',
            'purchase_at':'2026-10-15T03:00:00+00:00','payment_path':'card','channel':'offline'},ensure_ascii=False))
        page.get_by_role('button', name='계산 조건 점검', exact=True).click()
        expect(page.locator('#preview-result')).to_contain_text('admin_scenario_diagnostic_not_public_estimate')
        expect(page.locator('#preview-result')).to_contain_text('원 단위 계산 정책 미확정')
        assert db.scalar(select(func.count()).select_from(CardProgramPublication)) == 0
        browser.close()


def test_deep_oil_uses_separate_principal_cap_preview_without_publication(db, static_site):
    version = draft_deep_oil(db)
    attach_deep_oil_leaflet(db)
    output = Path('reports/card-usage-cap-20261015')
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(static_site + '/card-program-review.html')
        page.locator('#token').fill('test-admin-token')
        page.locator('#load').click()
        page.locator('#queue button').click()
        expect(page.locator('#calculation')).to_be_visible()
        expect(page.locator('#supporting-result')).to_contain_text('spend.higher_label')
        expect(page.locator('#supporting-result')).to_contain_text('outside_scope.cinema.brand')
        page.locator('#calculation summary').click()
        expect(page.locator('#calculation-basis')).to_contain_text('할인 대상 결제금액')
        default = json.loads(page.locator('#scenario').input_value())
        assert 'remaining_eligible_payment_caps' in default and 'remaining_caps' not in default
        page.locator('#scenario').fill(json.dumps(usage_scenario().model_dump(mode='json'),ensure_ascii=False))
        with page.expect_response(lambda r: r.url.endswith(f'/{version.id}/preview-usage-cap')) as response:
            page.locator('#preview').click()
        assert response.value.status==200
        expect(page.locator('#preview-result')).to_contain_text('eligible_payment_krw')
        expect(page.locator('#preview-result')).to_contain_text('가맹점 채널 공식 적용 정책 미확인')
        result = response.value.json()
        assert result['scenario_result']['estimated_saving'] is None
        assert result['public_estimated_saving'] is None
        assert not result['publication_readiness']['can_approve']
        expect(page.locator('#approve')).to_be_disabled()
        assert db.scalar(select(func.count()).select_from(CardProgramPublication))==0
        assert db.scalar(select(func.count()).select_from(MapReviewEvent))==0
        assert errors==[]
        page.screenshot(path=str(output / 'admin-preview.png'), full_page=True)
        (output / 'browser-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()


def test_changed_candidate_shows_difference_from_previous_extraction(db,draft,static_site):
    version=next_version(db,draft[0],draft[2],new_source=True)
    version.extractor_version=draft[0].extractor_version
    db.commit()
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(channel='msedge',headless=True)
        page=browser.new_page()
        page.goto(static_site+'/card-program-review.html')
        page.locator('#token').fill('test-admin-token')
        page.locator('#load').click()
        page.locator('#queue button').first.click()
        expect(page.locator('#title')).to_contain_text(f'버전 {version.id}')
        expect(page.locator('#changes-result')).to_contain_text('previous_extraction')
        expect(page.locator('#changes-result')).to_contain_text('rate_bps')
        expect(page.locator('#changes-result')).to_contain_text('changed notice')
        expect(page.locator('#approve')).to_be_disabled()
        assert db.scalar(select(func.count()).select_from(CardProgramPublication))==0
        assert db.scalar(select(func.count()).select_from(MapReviewEvent))==0
        browser.close()


def test_operations_alert_opens_expired_review_without_renewal(db,draft,static_site):
    publication,_=publish(db,draft)
    publication.recheck_at=NOW-timedelta(minutes=1)
    draft[1].last_error='ConnectError'
    draft[1].failure_count=3
    db.commit()
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(channel='msedge',headless=True)
        page=browser.new_page()
        page.goto(static_site+'/card-program-review.html')
        page.locator('#token').fill('test-admin-token')
        page.locator('#load').click()
        expect(page.locator('#operations-status')).to_contain_text('P0 1')
        expect(page.locator('#operations')).to_contain_text('기존 승인 혜택 현재 이용 불가')
        expect(page.locator('#operations')).to_contain_text('3회 이상 연속 수집 실패')
        page.locator('#operations button').first.click()
        expect(page.locator('#title')).to_contain_text(f'버전 {draft[0].id}')
        expect(page.locator('#approve')).to_be_disabled()
        assert db.scalar(select(func.count()).select_from(MapReviewEvent))==1
        page.locator('#token').fill('other')
        expect(page.locator('#operations li')).to_have_count(0)
        browser.close()
