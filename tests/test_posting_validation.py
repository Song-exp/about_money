import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.crawlers.base import BaseCrawler, RawPosting
from app.crawlers.registry import run_crawler
from app.models.scholarship import Category, PostingStatus, Scholarship, SourceType
from app.services import llm, posting_documents
from app.services.posting_validation import validate_posting, candidate_fingerprint
from app.services.scholarship_readiness import readiness


def raw_posting():
    text = '서울 거주 대학생에게 100만원 지원. 중복수혜 불가.'
    return RawPosting(title='검증 실험', source_url='https://example.test/notice',
        source_platform='khu_janghak', source_type=SourceType.PUBLIC, category=Category.SCHOLARSHIP,
        body_text=text, benefit={'amount_desc': '100만원'},
        deadline_at=datetime.now(timezone.utc) + timedelta(days=5),
        source_documents=[{'url': 'https://example.test/source.pdf', 'filename': '공고.pdf',
                           'content': b'public-original', 'extracted_text': text, 'status': 'parsed',
                           'kind': 'attachment', 'blocks': [{'text': text, 'start': 0, 'end': len(text), 'location': 'page:1'}]}])


def mock_model(monkeypatch, *, missing=False, contradiction=False, hallucinate=False, omit_checked=False):
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    def complete(system, prompt, **kwargs):
        data = json.loads(prompt)
        if 'candidate' not in data:
            return json.dumps({'requirements': [
                {'category': 'eligibility', 'statement': '서울 거주 대학생',
                 'quote': '없는 원문' if hallucinate else '서울 거주 대학생'},
                {'category': 'benefit', 'statement': '100만원 지원', 'quote': '100만원 지원'}]})
        return json.dumps({'complete': not missing, 'consistent': not contradiction,
            'checked_ids': [] if omit_checked else [r['id'] for r in data['requirements']],
            'missing': [{'category': 'exclusion', 'statement': '중복수혜 불가', 'quote': '중복수혜 불가'}] if missing else [],
            'problems': ['금액 불일치'] if contradiction else []})
    monkeypatch.setattr(llm, 'chat_complete', complete)


def test_supported_and_complete_requirements_pass_with_source_location(monkeypatch):
    mock_model(monkeypatch)
    raw = raw_posting()
    result = validate_posting(raw)
    assert result['status'] == 'passed'
    assert result['coverage'][0]['end'] == len(raw.body_text)
    assert result['requirements'][0]['source']['location'] == 'page:1'
    assert result['requirements'][0]['source']['sha256']


def test_missing_requirement_is_retained_as_review_reason(monkeypatch):
    mock_model(monkeypatch, missing=True)
    result = validate_posting(raw_posting())
    assert result['status'] == 'needs_review'
    assert result['issues'][0]['missing'][0]['statement'] == '중복수혜 불가'


def test_wrong_value_or_ungrounded_quote_never_passes(monkeypatch):
    mock_model(monkeypatch, contradiction=True)
    assert validate_posting(raw_posting())['issues'][0]['code'] == 'source_information_mismatch'
    mock_model(monkeypatch, hallucinate=True)
    assert validate_posting(raw_posting())['status'] == 'needs_review'
    mock_model(monkeypatch, omit_checked=True)
    assert validate_posting(raw_posting())['status'] == 'needs_review'


def test_ocr_failure_is_not_overridden_by_model(monkeypatch):
    raw = raw_posting()
    raw.source_documents[0].update(status='needs_review', error='ocr_required', ocr_error='ocr_low_confidence')
    monkeypatch.setattr(llm, 'chat_complete', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not call model')))
    result = validate_posting(raw)
    assert result['issues'][0]['code'] == 'source_unreadable_or_unresolved'
    raw.source_documents[0]['error'] = 'structured_validation_failed'
    assert validate_posting(raw)['issues'][0]['code'] == 'source_unreadable_or_unresolved'


def test_provider_unavailable_and_malformed_output_fail_closed(monkeypatch):
    mock_model(monkeypatch)
    monkeypatch.setattr(llm, 'chat_complete', lambda *a, **k: 'not json')
    assert validate_posting(raw_posting())['status'] == 'needs_review'
    monkeypatch.setattr(llm, 'has_llm', lambda: False)
    assert validate_posting(raw_posting())['issues'][0]['code'] == 'validation_model_not_configured'


def test_pass_cache_invalidates_on_source_and_saved_value_change(monkeypatch):
    mock_model(monkeypatch)
    raw = raw_posting()
    result = validate_posting(raw)
    monkeypatch.setattr(llm, 'chat_complete', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline')))
    assert validate_posting(raw, result) is result
    raw.benefit = {'amount_desc': '200만원'}
    assert validate_posting(raw, result)['status'] == 'needs_review'


def test_all_source_characters_are_covered(monkeypatch):
    raw = raw_posting()
    text = raw.body_text + (' 설명' * 4000)
    raw.source_documents[0]['extracted_text'] = text
    mock_model(monkeypatch)
    # Keep only the first chunk meaningful; other chunks may have no requirements.
    original = llm.chat_complete
    def complete(system, prompt, **kwargs):
        data = json.loads(prompt)
        if '서울 거주 대학생' in data['source_text']:
            return original(system, prompt, **kwargs)
        return json.dumps({'requirements': []} if 'candidate' not in data else
                          {'complete': True, 'consistent': True, 'checked_ids': [], 'missing': [], 'problems': []})
    monkeypatch.setattr(llm, 'chat_complete', complete)
    result = validate_posting(raw)
    assert result['status'] == 'passed'
    intervals = [(x['start'], x['end']) for x in result['coverage']]
    assert intervals[0][0] == 0 and intervals[-1][1] == len(text)
    assert all(b[0] <= a[1] for a, b in zip(intervals, intervals[1:]))


class ArchivedCrawler(BaseCrawler):
    platform = 'khu_janghak'
    archives_sources = True
    include_pending_attempts = False
    def __init__(self, raw):
        self.raw = raw
    def preflight(self, **kwargs):
        pass
    def fetch(self):
        return [self.raw]


def test_collector_publishes_verified_facts_while_form_remains_blocked(db, monkeypatch):
    from app.crawlers import registry
    raw = raw_posting()
    raw.fetch_status = 'partial'
    raw.source_documents[0].update(status='needs_review', error='structured_validation_failed')
    raw.eligibility = {'extraction_issues': ['native_final_destinations_require_review']}
    mock_model(monkeypatch)
    monkeypatch.setattr(posting_documents, 'structure_documents', lambda *a, **k: None)
    monkeypatch.setattr(registry, '_enrich_from_body', lambda *a, **k: None)
    run_crawler(ArchivedCrawler(raw), db)
    row = db.scalar(select(Scholarship))
    assert row.status == PostingStatus.OPEN
    assert readiness(row)['recommendation_ready']
    assert not readiness(row)['writing_ready']
    from app.services.matching import evaluate_eligibility
    from app.schemas.schemas import UserProfile
    assert evaluate_eligibility(row, UserProfile())['status'] == 'unknown'
    assert candidate_fingerprint(row) == row.eligibility['document_validation']['candidate_fingerprint']
    row.benefit = {'amount_desc': '999만원'}
    assert not readiness(row)['recommendation_ready']


def test_collector_stores_missing_requirement_and_blocks_recommendation(db, monkeypatch):
    from app.crawlers import registry
    raw = raw_posting()
    mock_model(monkeypatch, missing=True)
    monkeypatch.setattr(posting_documents, 'structure_documents', lambda *a, **k: None)
    monkeypatch.setattr(registry, '_enrich_from_body', lambda *a, **k: None)
    run_crawler(ArchivedCrawler(raw), db)
    row = db.scalar(select(Scholarship))
    assert row.status == PostingStatus.NEEDS_REVIEW
    assert row.eligibility['document_validation']['issues'][0]['code'] == 'requirements_missing'
    assert not readiness(row)['recommendation_ready']
