"""The UI must display the backend-generated reply, including zero-result turns."""
import json

from playwright.sync_api import expect, sync_playwright

from app.services import llm
from tests.browser_e2e_scholarships import static_site


def test_backend_ai_message_and_fallback_are_shown_distinctly(auth_client, monkeypatch, static_site):
    assert auth_client.put('/auth/profile', json={
        'gender': 'female', 'preferred_pay_methods': ['kakao'],
    }).status_code == 200
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    answer = '서울 거주 조건을 확인했어요. 현재 수집된 공고가 없어 후보를 안내할 수는 없지만, 신청 준비에 대해 함께 이야기할 수 있어요.'
    monkeypatch.setattr(llm, 'chat_complete', lambda *args, **kwargs: json.dumps({
        'message': answer, 'citations': [],
    }, ensure_ascii=False))
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 900})
        context.route('https://dapi.kakao.com/**', lambda route: route.abort())
        context.add_cookies([{'name': name, 'value': value, 'url': static_site, 'httpOnly': True}
                            for name, value in auth_client.cookies.items()])
        page = context.new_page()
        page.goto(static_site)
        page.get_by_role('tab', name='장학금·정책', exact=True).click()
        message = page.locator('.message-input input')
        message.fill('소득분위 3분위, 학점 3.8, 서울거주')
        with page.expect_response(lambda response: response.url.endswith('/api/chat/message')) as event:
            message.press('Enter')
        payload = event.value.json()
        assert payload['answer_source'] == 'llm'
        expect(page.locator('.bubble.bot .text').last).to_have_text(payload['message'])
        expect(page.get_by_text('AI 답변 · 검색 공고 기반', exact=True)).to_be_visible()
        page.reload()
        page.get_by_role('tab', name='장학금·정책', exact=True).click()
        expect(page.locator('.bubble.bot .text').last).to_have_text(answer)
        expect(page.get_by_text('AI 답변 · 검색 공고 기반', exact=True)).to_be_visible()
        def unavailable(*args, **kwargs):
            raise TimeoutError('fixture provider outage')
        monkeypatch.setattr(llm, 'chat_complete', unavailable)
        message.fill('아까 말한 조건으로 다시 설명해줘')
        message.press('Enter')
        expect(page.get_by_text('AI 답변을 받지 못해 검색 결과로 안내합니다.', exact=True)).to_be_visible()
        page.get_by_role('button', name='새 대화', exact=True).click()
        expect(page.get_by_text(answer, exact=True)).to_have_count(0)
        assert auth_client.get('/chat/session').json()['messages'] == []
        browser.close()
