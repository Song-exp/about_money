"""Exercise conditional people through the actual writing-session API."""
import uuid
from copy import deepcopy
import pytest

from app.core.config import settings

from app.models.writing_session import WritingSession
from app.schemas.schemas import UserProfile
from app.services.form_grid import native_grids
from app.services.scholarship_readiness import form_fingerprint
from app.services.writing_chat import start_session
from tests.test_form_grid import parsed_grid
from tests.test_writing_workflow import action, setup_writing


@pytest.fixture(autouse=True)
def allow_accelerated_api_conversation(monkeypatch):
    # A real conversation spans minutes; these tests exercise it in seconds.
    monkeypatch.setattr(settings, 'chat_rate_limit_per_min', 1000)


def grid_session(client, db):
    row, initial = setup_writing(client, db)
    owner = db.get(WritingSession, uuid.UUID(initial['id'])).user_id
    fields = native_grids(parsed_grid(reference=False), {'sha256': 'source'})[0]['fields']
    row.eligibility = {**row.eligibility, 'application_fields': fields, 'form_groups': [],
                       'form_exclusions': [], 'form_coverage': [], 'extraction_issues': []}
    row.eligibility = {**row.eligibility, 'form_review': {'fingerprint': form_fingerprint(row)}}
    db.commit()
    session = start_session(db, owner, row, UserProfile())
    if session['next_action']['kind'] == 'qualification':
        session = action(client, session, 'messages', {'message': '네, 공고 조건을 확인했고 해당합니다.'})
    return session


def fill(client, session, include=True):
    while session['next_action']:
        step = session['next_action']
        if step['kind'] == 'applicability':
            session = action(client, session, f"conditions/{step['condition_id']}", {'applies': include})
        elif step['kind'] == 'confirm':
            session = action(client, session, f"fields/{step['field_id']}/confirm", {'confirmed': True})
        else:
            assert step['kind'] == 'field'
            field = next(f for f in session['fields'] if f['id'] == step['field_id'])
            value = '표시하지 않음' if field['field_type'] == 'checkbox' else ('추가대상자응답' if field['layout']['row'] == 3 else '기존대상자응답')
            session = action(client, session, 'messages', {'message': value})
    return session


def test_condition_ids_cannot_couple_different_source_documents():
    blocks = parsed_grid()
    first = native_grids(blocks, {'sha256': 'first'})[0]['fields']
    second = native_grids(deepcopy(blocks), {'sha256': 'second'})[0]['fields']
    a = {f['applicability']['id'] for f in first if f.get('applicability')}
    b = {f['applicability']['id'] for f in second if f.get('applicability')}
    assert len(a) == len(b) == 1 and a.isdisjoint(b)


def test_unknown_and_excluded_people_cannot_be_answered_or_auto_filled(auth_client, db):
    session = grid_session(auth_client, db)
    extra = [f for f in session['fields'] if f['layout']['row'] == 3]
    name = next(f for f in extra if f['label'] == '성명')
    action(auth_client, session, f"fields/{name['id']}/value", {'message': '추가대상자응답'}, method='put', expected=409)
    session = fill(auth_client, session, include=False)
    assert not any(f['id'] in session['answers'] for f in extra)
    session = action(auth_client, session, 'draft')
    assert not any(d['field_id'] in {f['id'] for f in extra} for d in session['drafts'])
    assert not any(m['field_id'] in {f['id'] for f in extra} for m in session['manual_items'])
    action(auth_client, session, f"fields/{name['id']}/value", {'message': '추가대상자응답'}, method='put', expected=409)


def test_condition_change_discards_only_extra_person_and_invalidates_exports(auth_client, db):
    session = fill(auth_client, grid_session(auth_client, db))
    extra = [f for f in session['fields'] if f['layout']['row'] == 3]
    ids = {f['id'] for f in extra}
    cid = extra[0]['applicability']['id']
    retained = {k: deepcopy(v) for k, v in session['answers'].items() if k not in ids}
    consent = next(f for f in extra if f['field_type'] == 'checkbox')
    assert session['answers'][consent['id']]['facts']['value']['selection_state'] == 'unchecked'
    session = action(auth_client, session, 'draft')
    session = action(auth_client, session, 'review', {'confirmed': True, 'manual_checks_confirmed': True})
    url = f"/writing-sessions/{session['id']}/export?format=txt"
    assert '추가대상자응답' in auth_client.get(url).content.decode('utf-8-sig')
    old = session
    session = action(auth_client, session, f'conditions/{cid}', {'applies': False})
    assert not session['drafts'] and not session['draft_reviewed']
    assert session['answers'] == retained
    assert auth_client.get(url).status_code == 409
    action(auth_client, old, f'conditions/{cid}', {'applies': True}, expected=409)
    session = action(auth_client, session, 'draft')
    session = action(auth_client, session, 'review', {'confirmed': True, 'manual_checks_confirmed': True})
    assert '추가대상자응답' not in auth_client.get(url).content.decode('utf-8-sig')
    session = action(auth_client, session, f'conditions/{cid}', {'applies': True})
    assert session['next_action']['kind'] == 'field'
    assert session['answers'] == retained and consent['id'] not in session['answers']
    reloaded = auth_client.get(f"/writing-sessions/{session['id']}").json()
    assert reloaded['answers'] == retained and reloaded['next_action'] == session['next_action']


def test_reconfirm_same_condition_keeps_answers_and_review(auth_client, db):
    session = fill(auth_client, grid_session(auth_client, db))
    session = action(auth_client, session, 'draft')
    session = action(auth_client, session, 'review', {'confirmed': True, 'manual_checks_confirmed': True})
    condition = session['conditions'][0]['id']
    again = action(auth_client, session, f'conditions/{condition}', {'applies': True})
    assert again['answers'] == session['answers'] and again['drafts'] == session['drafts']
    assert again['draft_reviewed'] and again['version'] == session['version'] + 1
