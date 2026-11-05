import json
from copy import deepcopy

from sqlalchemy import select

from app.crawlers.public import OnjungchoungnyeonCrawler
from app.crawlers.registry import run_crawler
from app.models.scholarship import Scholarship, PostingStatus, Category
from app.models.source_document import SourceDocument, SourceFetchAttempt
from app.services import llm, policy_sources
from app.services.policy_validation import queue, is_verified, run_batch, validate_policy
from app.services.scholarship_readiness import readiness
from tests.conftest import make_scholarship, verified_policy_fixture
from tests.test_youthcenter import _item


def raw_policy(**kwargs):
    return OnjungchoungnyeonCrawler()._to_raw(_item(aplyYmd='20260101 ~ 20991231',
        plcySprtCn='월세 지원', addAplyQlfcCndCn='서울 거주 청년', **kwargs))


class API(OnjungchoungnyeonCrawler):
    def __init__(self, rows):
        self.rows = rows
    def preflight(self, **kwargs):
        pass
    def fetch(self):
        return self.rows


def model(monkeypatch, *, missing=False, bad_quote=False):
    calls = []
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    def complete(system, prompt, **kwargs):
        assert '청년정책' in system
        data = json.loads(prompt)
        calls.append(data)
        if 'candidate' not in data:
            eligibility = next(u['unit_id'] for u in data['source_units'] if '서울 거주 청년' in u['text'])
            benefit = next(u['unit_id'] for u in data['source_units'] if '월세 지원' in u['text'])
            return json.dumps({'requirements': [
                {'category': 'eligibility', 'statement': '서울 거주 청년', 'source_ids': ['unknown'] if bad_quote else [eligibility]},
                {'category': 'benefit', 'statement': '월세 지원', 'source_ids': [benefit]}]})
        benefit = next(u['unit_id'] for u in data['source_units'] if '월세 지원' in u['text'])
        return json.dumps({'complete': not missing, 'consistent': True,
            'checked_ids': [r['id'] for r in data['requirements']], 'problems': [],
            'missing': [{'category': 'benefit', 'statement': '월세 지원', 'source_ids': [benefit]}] if missing else []})
    monkeypatch.setattr(llm, 'chat_complete', complete)
    return calls


def test_api_keeps_full_response_and_all_qualification_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(policy_sources, 'CODEBOOK', tmp_path / 'absent.json')
    raw = raw_policy(sprtTrgtMinAge='19', sprtTrgtMaxAge='34', jobCd='0013001',
                     ptcpPrpTrgtCn='중복수혜자 제외', earnEtcCn='가구 소득 확인 필요')
    response, semantic = raw.source_documents
    payload = json.loads(response['content'])
    assert payload['sprtTrgtMinAge'] == '19' and payload['jobCd'] == '0013001'
    assert '중복수혜자 제외' in semantic['extracted_text']
    assert '가구 소득 확인 필요' in raw.body_text
    assert raw.eligibility['policy_source']['unresolved_codes'] == [{'field': 'jobCd', 'code': '0013001'}]


def test_ingestion_archives_and_queues_without_claiming_verification(db, monkeypatch):
    monkeypatch.setattr(llm, 'chat_complete', lambda *a, **k: (_ for _ in ()).throw(AssertionError('ingestion must queue')))
    raw = raw_policy()
    run_crawler(API([raw]), db)
    row = db.scalar(select(Scholarship))
    assert row.eligibility['policy_validation']['status'] == 'pending'
    assert row.status == PostingStatus.NEEDS_REVIEW
    assert len(db.scalars(select(SourceDocument)).all()) == 2
    assert db.scalar(select(SourceFetchAttempt)).documents[0]['kind'] == 'policy_api_response'
    assert not readiness(row)['recommendation_ready']


def test_batch_pass_allows_chat_but_never_writing(db, monkeypatch):
    calls = model(monkeypatch)
    run_crawler(API([raw_policy()]), db)
    report = run_batch(db, limit=1)
    row = db.scalar(select(Scholarship))
    assert report['passed'] == 1 and len(calls) == 2
    assert row.status == PostingStatus.OPEN and is_verified(row)
    assert readiness(row)['recommendation_ready'] and not readiness(row)['writing_ready']
    assert row.eligibility['policy_validation']['requirements'][0]['source']['sha256']


def test_missing_or_unsupported_condition_remains_review(db, monkeypatch):
    model(monkeypatch, missing=True)
    run_crawler(API([raw_policy()]), db)
    report = run_batch(db, limit=1)
    row = db.scalar(select(Scholarship))
    assert report['needs_review'] == 1
    assert row.eligibility['policy_validation']['issues'][0]['code'] == 'requirements_missing'
    assert not readiness(row)['recommendation_ready']
    model(monkeypatch, bad_quote=True)
    assert validate_policy(raw_policy())['status'] == 'needs_review'


def test_volatile_api_metadata_preserves_pass_but_semantic_change_invalidates(db, monkeypatch):
    model(monkeypatch)
    run_crawler(API([raw_policy(inqCnt='1')]), db)
    run_batch(db, limit=1)
    row = db.scalar(select(Scholarship))
    run_crawler(API([raw_policy(inqCnt='99', lastMdfcnDt='2026-10-27 01:00:00')]), db)
    assert is_verified(row)
    changed = raw_policy(ptcpPrpTrgtCn='추가 제외조건')
    run_crawler(API([changed]), db)
    assert row.eligibility['policy_validation']['status'] == 'pending'
    assert not readiness(row)['recommendation_ready']


def test_legacy_policy_is_not_implicitly_verified(client, db):
    row = make_scholarship(category=Category.GOV_BENEFIT, source_platform='onjungchoungnyeon')
    db.add(row)
    db.commit()
    assert not readiness(row)['recommendation_ready']
    reply = client.post('/policy-chat/message', json={'message': '정책 찾아줘'}).json()
    assert reply['candidates'] == [] and '청년정책 자료가 없어요' in reply['message']


def test_bounded_batch_does_not_claim_unprocessed_policies_passed(db, monkeypatch):
    model(monkeypatch)
    run_crawler(API([raw_policy(plcyNo=f'policy-{i}') for i in range(3)]), db)
    report = run_batch(db, limit=1)
    assert report['processed'] == report['passed'] == 1 and report['pending'] == 2


def test_unknown_code_and_source_tampering_cannot_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(policy_sources, 'CODEBOOK', tmp_path / 'absent.json')
    monkeypatch.setattr(llm, 'chat_complete', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not call')))
    assert validate_policy(raw_policy(jobCd='0013999'))['issues'][0]['code'] == 'unresolved_condition_codes'
    raw = raw_policy()
    raw.source_documents[1]['content'] = b'tampered'
    assert validate_policy(raw)['issues'][0]['code'] == 'policy_source_integrity_mismatch'


def test_model_outage_never_becomes_passed(db, monkeypatch):
    model(monkeypatch)
    monkeypatch.setattr(llm, 'chat_complete', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline')))
    run_crawler(API([raw_policy()]), db)
    assert run_batch(db, limit=1)['needs_review'] == 1
    assert not readiness(db.scalar(select(Scholarship)))['recommendation_ready']


def test_changed_values_invalidate_verified_catalog_entry():
    row = verified_policy_fixture(make_scholarship(category=Category.GOV_BENEFIT))
    assert is_verified(row)
    row.eligibility = {**row.eligibility, 'application_period': {'kind': 'unknown'}}
    assert not is_verified(row)


def test_codebook_ignores_unknown_or_conflicting_labels():
    html = b'<input id="a" value="0013001"><label for="a">A</label><input id="b" value="0013001"><label for="b">B</label><option value="0049010">Any</option>'
    assert policy_sources.parse_codebook(html) == {'0049010': 'Any'}


def test_official_display_rules_decode_age_income_without_assuming_flags(monkeypatch, tmp_path):
    path = tmp_path / 'codes.json'
    path.write_text(json.dumps({'codes': {}, 'rules': {'income': {'version': 'official-income-display-v1'},
                                                     'age': {'version': 'official-age-display-v1'}}}), encoding='utf-8')
    monkeypatch.setattr(policy_sources, 'CODEBOOK', path)
    raw = raw_policy(sprtTrgtAgeLmtYn='Y', sprtTrgtMinAge='0', sprtTrgtMaxAge='0', earnCndSeCd='0043001')
    assert '연령 제한없음' in raw.body_text and '소득 무관' in raw.body_text
    assert not raw.eligibility['policy_source']['unresolved_codes']
    bad = raw_policy(sprtTrgtAgeLmtYn='N', sprtTrgtMinAge='40', sprtTrgtMaxAge='20',
                     earnCndSeCd='0043002', earnMinAmt='5000', earnMaxAmt='1000')
    assert len(bad.eligibility['policy_source']['unresolved_codes']) == 2


def test_date_format_and_thousands_separator_do_not_create_fake_numeric_mismatch():
    from app.services.posting_validation import policy_numbers, bind_policy_requirement, PolicyRequirement
    assert policy_numbers('2026년 1월 1일 ~ 2026년 12월 31일') <= policy_numbers('20260101 ~ 20261231')
    assert policy_numbers('5000만원') == policy_numbers('5,000만원')
    assert policy_numbers('11110') <= policy_numbers('11110,11140,11170')
    assert policy_numbers('1111011140') != policy_numbers('11110,11140')
    req = PolicyRequirement(category='benefit', statement='200만원 지원', source_ids=['amount'])
    units = [{'unit_id': 'amount', 'text': '100만원 지원', 'start': 0, 'end': 8}]
    import pytest
    with pytest.raises(ValueError, match='inventory_number'):
        bind_policy_requirement(req, units, '100만원 지원')


def test_archive_rebuild_keeps_collection_time(db, monkeypatch, tmp_path):
    raw = raw_policy()
    run_crawler(API([raw]), db)
    row = db.scalar(select(Scholarship))
    captured = row.crawled_at
    original_hash = row.eligibility['policy_source']['response_sha256']
    # Change the visible decoder field name to simulate a renderer upgrade.
    monkeypatch.setitem(policy_sources.LABELS, 'plcyNm', '정책명(업데이트)')
    from app.services.policy_validation import rebuild_archived_snapshots
    assert rebuild_archived_snapshots(db)['rebuilt'] == 1
    db.refresh(row)
    assert row.crawled_at == captured
    assert row.eligibility['policy_source']['response_sha256'] == original_hash
    assert row.eligibility['policy_source']['response_captured_at']
    assert row.eligibility['policy_validation']['status'] == 'pending'


def test_changed_during_validation_does_not_save_stale_approval(db, monkeypatch):
    model(monkeypatch)
    run_crawler(API([raw_policy()]), db)
    row = db.scalar(select(Scholarship))
    from app.services import policy_validation
    actual = policy_validation.validate_policy
    def changed(raw, previous):
        review = actual(raw, previous)
        row.body_text += '\n추가 조건'
        db.commit()
        return review
    monkeypatch.setattr(policy_validation, 'validate_policy', changed)
    report = run_batch(db, limit=1)
    assert report['stale'] == 1 and report['passed'] == 0
    assert not readiness(row)['recommendation_ready']
