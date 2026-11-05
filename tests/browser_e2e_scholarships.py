"""Explicit browser suite: built React + real local HTTP FastAPI, isolated DB.

Run this file explicitly. Source-bound subsets are not whole-form approval.
"""
import json
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit
import socket
import time
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import pytest
from playwright.sync_api import sync_playwright, expect

from app.core.config import settings
from app.services.scholarship_readiness import form_fingerprint
from tests.test_scholarship_validation import stored_form
from tests.test_form_spreadsheet_repeat import fields as repeat_fields
from tests.test_form_spreadsheet_recommendation import fixture as recommendation_fixture
from app.services.form_spreadsheet_recommendation import native_recommendation, SOURCE_SHA


@pytest.fixture(scope='module')
def static_site():
    from app.main import app
    local = FastAPI()
    local.mount('/api', app)
    local.mount('/', StaticFiles(directory='frontend/dist', html=True))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
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
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def source_fields(kind):
    if kind == 'xls':
        return [repeat_fields()[0]]
    if kind == 'xlsx':
        p = recommendation_fixture()
        return [g['field'] for g in native_recommendation(p['blocks'], {'sha256': SOURCE_SHA}, p['spreadsheet_inventory'])[0]]
    root = Path('reports/scholarship-improvement-20261010-v31-actual-r1')
    predictions = list(root.glob('notice-*/prediction.json'))
    assert predictions
    for path in predictions:
        data = json.loads(path.read_text(encoding='utf-8'))
        for f in data['eligibility']['application_fields']:
            if f.get('input_spec', {}).get('version') == 'native-basic-inputs-v1' and not f.get('constraint_issues'):
                return [f]
    raise AssertionError('actual HWP basic input unavailable')


@pytest.mark.parametrize('kind', ['xls', 'xlsx', 'hwp'])
def test_browser_card_collect_reload_review_save_export_revision(kind, auth_client, db, monkeypatch, static_site):
    monkeypatch.setattr(settings, 'chat_rate_limit_per_min', 1000)
    assert auth_client.put('/auth/profile', json={'gender': 'female', 'preferred_pay_methods': ['kakao']}).status_code == 200
    row, _ = stored_form(db)
    fields = source_fields(kind)
    row.title = '브라우저 검증 ' + kind
    row.eligibility = {**row.eligibility, 'application_fields': fields, 'form_groups': [], 'form_exclusions': [], 'form_coverage': [], 'extraction_issues': []}
    row.eligibility = {**row.eligibility, 'form_review': {'fingerprint': form_fingerprint(row)}}
    db.commit()
    root = Path('reports/scholarship-completion-20261011/browser')
    root.mkdir(parents=True, exist_ok=True)
    observed = []
    current = {}

    def observe(response):
        req = response.request
        url = urlsplit(response.url)
        if not url.path.startswith('/api/'):
            return
        path = url.path.removeprefix('/api') + ('?' + url.query if url.query else '')
        observed.append({'method': req.method, 'path': path, 'status': response.status})
        if path.startswith('/writing-sessions') and response.status == 200 and 'application/json' in response.headers.get('content-type', ''):
            value = response.json()
            if isinstance(value, dict) and value.get('fields'):
                current.clear()
                current.update(value)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        browser_context = browser.new_context(viewport={'width': 1280, 'height': 900})
        browser_context.add_cookies([{'name': name, 'value': value, 'url': static_site, 'httpOnly': True}
                                     for name, value in auth_client.cookies.items()])
        page = browser_context.new_page()
        page.on('response', observe)
        def wait_version(previous):
            # Real HTTP completes asynchronously after the click; wait for the
            # actual response, rather than an absent spinner before rendering.
            for _ in range(200):
                if current.get('version', 0) > previous:
                    return
                page.wait_for_timeout(50)
            raise AssertionError('writing response did not advance: ' + repr(observed[-4:]))
        page.goto(static_site)
        page.get_by_role('tab', name='장학금·정책').click()
        page.get_by_label('장학금 조건 입력').fill('소득 3분위 학점 3.8 서울 거주')
        page.get_by_role('button', name='보내기', exact=False).click()
        page.get_by_role('button', name='브라우저 검증 ' + kind, exact=False).click()
        expect(page.get_by_role('heading', name=row.title, exact=True)).to_be_visible()
        assert '?writing_session=' in page.url
        for _ in range(40):
            action = current.get('next_action')
            if not action:
                break
            version = current['version']
            if action['kind'] == 'confirm':
                page.get_by_role('button', name='이 문항의 정보가 본인 사실과 맞습니다', exact=True).click()
            else:
                key = action.get('slot_key')
                value = {'entry_count': '1', 'entry_0_name': '검증용 자격', 'entry_0_level': '1급', 'entry_0_date': '2024-02-29',
                         'entry_0_issuer': '검증 기관', 'birth_date': '2003-05-04', 'age': '23', 'grade': '3', 'semester': '2'}.get(key, '검증용 실제 정보')
                if action['kind'] == 'qualification':
                    value = '네, 공고 조건에 해당합니다.'
                page.locator('textarea').first.fill(value)
                page.get_by_role('button', name='보내기', exact=True).click()
            wait_version(version)
            assert current['version'] > version
        assert current['state'] == 'ready'
        session_id = current['id']
        page.reload()
        expect(page.get_by_role('button', name='확인한 정보로 초안 구성')).to_be_visible()
        assert current['id'] == session_id
        page.get_by_role('button', name='확인한 정보로 초안 구성').click()
        page.get_by_label('전체 문항의 내용이 사실과 맞는지 검토했습니다.').check()
        manual = page.get_by_label('원문이 요청한 세부 내용·전체 분량·서식과 원본에서 직접 작성·첨부·표시할 항목을 확인했습니다.')
        if manual.count():
            manual.check()
        page.get_by_role('button', name='전체 초안 검토 완료', exact=True).click()
        page.get_by_role('button', name='내 신청서 이력에 저장', exact=True).click()
        expect(page.get_by_text('내 신청서 이력에 저장했습니다.', exact=True)).to_be_visible()
        for fmt in ('TXT', 'DOCX'):
            with page.expect_download() as download:
                page.get_by_role('button', name=fmt + ' 작성 자료', exact=True).click()
            download.value.save_as(root / f'{kind}-draft.{fmt.lower()}')
        page.screenshot(path=str(root / f'{kind}-saved.png'), full_page=True)
        page.get_by_role('button', name='정보 수정', exact=True).first.click()
        page.get_by_label('내용 수정').fill('0' if kind == 'xls' else '수정된 검증 정보')
        version = current['version']
        page.get_by_role('button', name='수정 저장', exact=True).click()
        wait_version(version)
        assert current['drafts'] == [] and not current['draft_reviewed']
        row.content_hash = 'browser-modified-original-' + kind
        db.commit()
        page.reload()
        expect(page.get_by_text('공고 또는 양식이 변경되었습니다. 최신 공고를 다시 선택하세요. 이전 입력은 보관됩니다.', exact=True)).to_be_visible()
        refreshed = page.evaluate("async id => (await fetch('/api/writing-sessions/' + id, {credentials: 'include'})).json()", session_id)
        assert refreshed['state'] == 'stale'
        page.screenshot(path=str(root / f'{kind}-stale.png'), full_page=True)
        browser.close()
    (root / f'{kind}-requests.json').write_text(json.dumps({'transport': 'real_browser_to_local_http_uvicorn_isolated_db',
         'source_scope': 'source_bound_subset_not_whole_form', 'requests': observed}, ensure_ascii=False, indent=2), encoding='utf-8')
