import json
from datetime import datetime

import pytest

from app.services import llm
from app.services.posting_extract import extract_structured, KST
from app.services.posting_documents import merge_posting_documents, structure_documents
from tests.test_posting_documents import raw_posting
from app.crawlers.registry import _enrich_from_body, run_crawler
from tests.test_daily_pipeline import FakeCrawler
from app.models.scholarship import Scholarship, PostingStatus
from sqlalchemy import select


@pytest.mark.parametrize('second,expected', [
    ('2026.9.21.(월) ~ 10.9.(금)', '2026-10-09'),
    ('2026.9.21.(월) ~ 2026.10.9.(금)', '2026-10-09'),
    ('2026.9.21.(월) ~ 10.10.(토)', None),
    ('2026.9.22.(화) ~ 10.9.(금)', None),
    ('2026.9.21.(월) ~ 10.9.(금) 발표 2026.10.15', None),
])
def test_repeated_table_periods_must_be_identical(monkeypatch, second, expected):
    monkeypatch.setattr(llm, 'has_llm', lambda: False)
    text = f'학생신청 및 2026.9.21.(월) ~ 10.9.(금)   {second} - 신청기간 통일하여 운영'
    result = extract_structured(text)
    actual = result.get('deadline_at')
    assert (actual.date().isoformat() if actual else None) == expected
    if expected:
        assert result['deadline_source']['evidence'] == text


def test_source_deadline_survives_model_omission(monkeypatch):
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    monkeypatch.setattr(llm, 'chat_complete', lambda *a, **k: '{"application_fields":[]}')
    result = extract_structured('학생 신청기간: 2026.9.21.(월) ~ 10.9.(금)')
    assert result['deadline_at'].date().isoformat() == '2026-10-09'


@pytest.mark.parametrize('body,expected,explicit', [
    ('접수기간: 2026.9.24.(월) ~ 2026.10.8.(목) 18:00', '18:00', True),
    ('신청기간: 2026.9.28.(월) 09:00 ~ 10.30.(금) 18:00', '18:00', True),
    ('신청기간: 2026.9.28.(월) 09:00 ~ 10.30.(금)', '23:59', False),
    ('접 수 기간: 2026 년 9 월 28 일 ~ 2026 년 10 월 30 일 (금) 18 : 00', '18:00', True),
    ('접수기간\x00:\x002026.9.24.(월) ~\x002026.10.8.(목)\x0018:00', '18:00', True),
    ('신청마감: 2026.10.8.(목) 오후 6시 30분', '18:30', True),
    ('신청마감: 2026.10.8. 오전 12:00', '00:00', True),
    ('신청마감: 2026.10.8. 오후 12:00', '12:00', True),
    ('신청마감: 2026.10.8. 23:59', '23:59', True),
])
def test_only_endpoint_time_and_raw_proof_are_preserved(monkeypatch, body, expected, explicit):
    monkeypatch.setattr(llm, 'has_llm', lambda: False)
    result = extract_structured(body)
    assert result['deadline_at'].strftime('%H:%M') == expected
    assert result['deadline_at'].tzinfo == KST
    assert result['deadline_source']['time_explicit'] is explicit
    assert result['deadline_source']['evidence'] == body


@pytest.mark.parametrize('suffix', ['25:00', '18:99', '오후 13:00', '18:00 또는 17:00'])
def test_invalid_or_conflicting_clock_does_not_default_to_end_of_day(monkeypatch, suffix):
    monkeypatch.setattr(llm, 'has_llm', lambda: False)
    result = extract_structured(f'신청마감: 2026.10.8. {suffix}')
    assert 'deadline_at' not in result
    assert any('deadline_time' in issue for issue in result['issues'])


def result(day='2026-10-08', time='18:00', explicit=True):
    return {'deadline_at': f'{day}T{time}:00+09:00', 'deadline_source': {
        'evidence': f'신청마감: {day} {time}' if explicit else f'신청마감: {day}',
        'time_explicit': explicit, 'precision': 'minute' if explicit else 'day'}}


@pytest.mark.parametrize('reverse', [False, True])
def test_explicit_time_survives_date_only_duplicate_and_json_serialization(reverse):
    raw = raw_posting()
    documents = [result(), result(time='23:59', explicit=False)]
    raw.source_documents = [{'structured': r} for r in (documents[::-1] if reverse else documents)]
    merged = merge_posting_documents(raw)
    assert merged['deadline_at'] == datetime(2026, 10, 8, 18, tzinfo=KST)
    assert merged['deadline_source']['time_explicit'] is True
    assert len(merged['deadline_source']['alternative_sources']) == 1


@pytest.mark.parametrize('other,issue', [
    (result(time='17:00'), 'conflicting_student_deadline_times'),
    (result(day='2026-10-09'), 'conflicting_student_deadlines'),
    (result(time='23:59'), 'conflicting_student_deadline_times'),
])
def test_different_explicit_dates_or_times_are_withheld(other, issue):
    raw = raw_posting()
    raw.source_documents = [{'structured': result()}, {'structured': other}]
    merged = merge_posting_documents(raw)
    assert 'deadline_at' not in merged and issue in merged['issues']


def test_file_structure_to_posting_merge_keeps_time(monkeypatch):
    monkeypatch.setattr(llm, 'has_llm', lambda: False)
    raw = raw_posting()
    raw.source_documents = [{'url': 'https://example.test/a', 'kind': 'pdf', 'content': b'raw',
                            'extracted_text': '접수기간: 2026.9.24 ~ 2026.10.8 18:00'}]
    structure_documents(raw)
    assert raw.source_documents[0]['structured']['deadline_at'].endswith('18:00:00+09:00')
    assert merge_posting_documents(raw)['deadline_at'].hour == 18


def test_model_cannot_supply_unquoted_clock(monkeypatch):
    body = '신청마감: 2026.10.8'
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    monkeypatch.setattr(llm, 'chat_complete', lambda *a, **kw: json.dumps({
        'deadline': '2026-10-08', 'deadline_evidence': body, 'deadline_time': '18:00'}))
    assert extract_structured(body)['deadline_at'].hour == 23


def test_recommendation_clock_is_not_used(monkeypatch):
    monkeypatch.setattr(llm, 'has_llm', lambda: False)
    body = '신청마감: 2026.10.8 18:00\n학교 추천마감: 2026.10.8 09:00'
    assert extract_structured(body)['deadline_at'].hour == 18


def test_conflict_cannot_restore_listing_deadline_in_crawl(db):
    raw = raw_posting()
    raw.deadline_at = datetime(2027, 10, 8, 23, 59, tzinfo=KST)
    raw.deadline_raw = '2027.10.8'
    raw.source_documents = [{'structured': result(day='2027-10-08')},
                            {'structured': result(day='2027-10-08', time='17:00')}]
    run_crawler(FakeCrawler('test', [raw]), db)
    row = db.scalar(select(Scholarship))
    assert row.deadline_at is None and row.status == PostingStatus.NEEDS_REVIEW
    assert 'conflicting_student_deadline_times' in row.eligibility['extraction_issues']


def test_enrichment_keeps_verified_deadline_clock():
    raw = raw_posting()
    raw.deadline_at = datetime(2026, 10, 8, 23, 59, tzinfo=KST)
    raw.source_documents = [{'structured': result()}]
    _enrich_from_body(raw)
    assert raw.deadline_at.hour == 18
    assert raw.eligibility['deadline_source']['time_explicit'] is True
