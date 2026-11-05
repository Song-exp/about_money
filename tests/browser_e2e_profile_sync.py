"""Legacy demo migration uses personal settings and exposes no demo controls."""
import json
from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright, expect
from tests.browser_e2e_scholarships import static_site


@pytest.mark.parametrize('existing_new_key', [False, True])
@pytest.mark.parametrize('missing_personal', [False, True])
def test_deployed_profile_key_migration_and_personal_settings(auth_client, static_site, existing_new_key, missing_personal):
    user_id = auth_client.get('/auth/me').json()['id']
    def profile(gender):
        personal = {'gender': gender, 'cardIds': [], 'payMethods': ['kakao'],
            'telecom': 'KT', 'studentCredentials': [], 'benefitPrograms': []}
        return {'userId': user_id, 'personaId': 'demo_a', **personal,
            'cardIds': [2], 'personalProfile': personal}
    initial = {'legacy': profile('male'), 'current': profile('female') if existing_new_key else None}
    if missing_personal:
        for value in initial.values():
            if value:
                value.pop('personalProfile')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 900})
        # Profile checks do not require a map SDK on the random test origin.
        context.route('https://dapi.kakao.com/**', lambda route: route.abort())
        context.add_cookies([{'name': name, 'value': value, 'url': static_site, 'httpOnly': True}
            for name, value in auth_client.cookies.items()])
        context.add_init_script('''data => {
          if (!sessionStorage.getItem('profile-fixture-set')) {
            localStorage.setItem('paypick.profile.v1', JSON.stringify(data.legacy));
            if(data.current) localStorage.setItem('tmi.profile.v1', JSON.stringify(data.current));
            sessionStorage.setItem('profile-fixture-set', 'true');
          }
        }'''.replace('data =>', '(' + 'data =>') + ')(' + json.dumps(initial) + ');')
        page = context.new_page()
        page.goto(static_site, wait_until='domcontentloaded')
        expected_gender = None if missing_personal else 'female' if existing_new_key else 'male'
        page.wait_for_function("gender => JSON.parse(localStorage.getItem('tmi.profile.v1') || 'null')?.personalProfile?.gender === gender", arg=expected_gender)
        assert page.evaluate("JSON.parse(localStorage.getItem('paypick.profile.v1')).personaId") == 'demo_a'
        assert page.evaluate("JSON.parse(localStorage.getItem('tmi.profile.v1')).cardIds") == []
        assert auth_client.put('/auth/profile', json={'gender': 'female', 'card_ids': [5],
            'telecom': 'KT', 'preferred_pay_methods': ['kakao']}).status_code == 200
        page.reload(wait_until='domcontentloaded')
        expect(page.get_by_label('전역 페르소나 선택')).to_have_count(0)
        page.wait_for_function("JSON.parse(localStorage.getItem('tmi.profile.v1')).personaId === 'me'")
        assert page.evaluate("JSON.parse(localStorage.getItem('tmi.profile.v1')).cardIds") == [5]
        assert auth_client.get('/auth/me').json()['card_ids'] == [5]
        page.get_by_role('tab', name='마이페이지', exact=True).click()
        expect(page.get_by_role('heading', name='내 장학 기록')).to_be_visible()
        expect(page.get_by_text('누적 혜택 TOP 3', exact=True)).to_have_count(0)
        expect(page.get_by_text('DEMO PERSONAS', exact=True)).to_have_count(0)
        expect(page.get_by_text('내가 아낀 금액', exact=True)).to_be_visible()
        output = Path('reports/persona-removal-20261012/browser')
        output.mkdir(parents=True, exist_ok=True)
        (output / ('profile-' + str(existing_new_key) + '-' + str(missing_personal) + '.json')).write_text(json.dumps({
            'legacy_profile_preserved': True, 'existing_new_key': existing_new_key,
            'expected_profile_gender': expected_gender, 'demo_controls_removed': True,
            'personal_settings_restored': True, 'server_profile_preserved': True}), encoding='utf-8')
        browser.close()
