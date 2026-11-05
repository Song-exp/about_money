"""Two consultations, real local API, isolated data, desktop and mobile layout."""
from pathlib import Path
import json

from playwright.sync_api import expect, sync_playwright

from app.models.scholarship import Category
from tests.conftest import make_scholarship, verified_policy_fixture
from tests.browser_e2e_scholarships import static_site


def test_switch_scope_restore_history_and_policy_detail_without_writing(auth_client, db, static_site):
    assert auth_client.put('/auth/profile', json={'gender': 'female', 'preferred_pay_methods': ['kakao']}).status_code == 200
    db.add(make_scholarship(content_key='school-ui', title='재단 대학생 장학금', source_platform='khu_janghak'))
    policy_title = '광양시 대학생 생활비 장학금 지원 및 청년 주거비 지원 정책 안내'
    db.add(verified_policy_fixture(make_scholarship(content_key='policy-ui', title=policy_title, category=Category.GOV_BENEFIT,
        source_platform='onjungchoungnyeon', deadline_at=None,
        body_text='[지원내용] 생활 지원\n[신청방법] 운영기관 홈페이지 온라인 접수\n[제출서류] 재학증명서')))
    db.commit()
    output = Path('reports/consultation-split-20261024')
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.route('https://dapi.kakao.com/**', lambda route: route.abort())
        context.add_cookies([{'name': name, 'value': value, 'url': static_site, 'httpOnly': True}
                            for name, value in auth_client.cookies.items()])
        page = context.new_page()
        requests, errors = [], []
        page.on('request', lambda req: requests.append(req.url))
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(static_site)
        page.get_by_role('tab', name='장학금·정책', exact=True).click()
        expect(page.get_by_role('tab', name='장학금 상담', exact=True)).to_have_attribute('aria-selected', 'true')
        page.get_by_label('장학금 조건 입력').fill('장학금 찾아줘')
        page.get_by_role('button', name='보내기', exact=False).click()
        expect(page.locator('.scholarship-card-copy strong')).to_have_text(['재단 대학생 장학금'])
        expect(page.get_by_role('button', name='과거 신청서 파일 첨부')).to_be_visible()
        page.get_by_role('tab', name='청년정책 상담', exact=True).click()
        expect(page.get_by_label('청년정책 조건 입력')).to_be_enabled()
        expect(page.get_by_role('button', name='과거 신청서 파일 첨부')).to_have_count(0)
        page.get_by_label('청년정책 조건 입력').fill('청년정책 찾아줘')
        page.get_by_role('button', name='보내기', exact=False).click()
        expect(page.locator('.bubble.bot').last.locator('.scholarship-card-copy strong')).to_have_text([policy_title])
        expect(page.locator('.bubble.bot').last.locator('.scholarship-card-copy').get_by_text('신청 기간 확인 필요', exact=True)).to_be_visible()
        expect(page.get_by_text('양식 확인 대기', exact=False)).to_have_count(0)
        page.locator('.bubble.bot').last.locator('.scholarship-card').first.click()
        expect(page.locator('.bubble.bot .text').last).to_contain_text('운영기관 홈페이지 온라인 접수')
        assert 'writing_session' not in page.url
        assert not any('/writing-sessions' in url for url in requests)
        page.screenshot(path=str(output / 'policy-desktop.png'), full_page=True)
        for width in (390, 360):
            page.set_viewport_size({'width': width, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            expect(page.get_by_role('tab', name='청년정책 상담', exact=True)).to_be_visible()
            expect(page.get_by_label('청년정책 조건 입력')).to_be_visible()
        page.screenshot(path=str(output / 'policy-mobile.png'), full_page=True)
        page.get_by_role('tab', name='장학금 상담', exact=True).click()
        expect(page.locator('.scholarship-card-copy strong')).to_have_text(['재단 대학생 장학금'])
        expect(page.get_by_role('button', name='과거 신청서 파일 첨부')).to_be_visible()
        page.get_by_role('tab', name='청년정책 상담', exact=True).click()
        expect(page.locator('.bubble.bot .text').last).to_contain_text('운영기관 홈페이지 온라인 접수')
        page.reload()
        page.get_by_role('tab', name='장학금·정책', exact=True).click()
        expect(page.get_by_role('tab', name='청년정책 상담', exact=True)).to_have_attribute('aria-selected', 'true')
        expect(page.locator('.bubble.bot .text').last).to_contain_text('운영기관 홈페이지 온라인 접수')
        assert not errors, errors
        browser.close()


def test_policy_open_generates_questions_before_user_message(auth_client, db, static_site, monkeypatch):
    from app.services import llm
    auth_client.put('/auth/profile', json={'gender': 'female', 'preferred_pay_methods': ['kakao']})
    row = make_scholarship(content_key='initial-intake-ui', title='청년 주거 지원',
        category=Category.GOV_BENEFIT, body_text='독립 거주 중인 청년의 전세 또는 월세 계약을 지원합니다.')
    verified_policy_fixture(row)
    db.add(row)
    db.commit()
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    def complete(system, payload, **kwargs):
        data = json.loads(payload)
        assert data['task'] == 'policy_intake'
        return json.dumps({'questions': [{'text': '자취 중이신가요? 전세인가요, 월세인가요?',
                          'policy_id': row.id, 'quote': row.body_text}], 'shortlist': []}, ensure_ascii=False)
    monkeypatch.setattr(llm, 'chat_complete', complete)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(viewport={'width': 390, 'height': 844})
        context.route('https://dapi.kakao.com/**', lambda route: route.abort())
        context.add_cookies([{'name': name, 'value': value, 'url': static_site, 'httpOnly': True}
                            for name, value in auth_client.cookies.items()])
        page = context.new_page()
        page.goto(static_site + '/?consultation=youth_policy')
        page.get_by_role('tab', name='장학금·정책', exact=True).click()
        expect(page.get_by_label('청년정책 조건 입력')).to_be_enabled(timeout=15000)
        expect(page.locator('.bubble.bot .text').last).to_contain_text('자취 중이신가요? 전세인가요, 월세인가요?')
        expect(page.locator('.bubble.user')).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        output = Path('reports/policy-intake-20261024')
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / 'first-questions-mobile.png'), full_page=True)
        browser.close()
