import json

from app.models.scholarship import PostingStatus
from app.services import llm
from tests.conftest import make_scholarship
from tests.test_information_search import send


def test_detail_and_compare_retrieve_full_original_records_without_research(client, db):
    rows = [make_scholarship(content_key=f'tool-{i}', title=f'실제 장학 {i}',
                body_text=f'원문 상세 설명 {i}', required_documents=['재학증명서'],
                benefit={'amount_desc': '생활비 지원'}) for i in range(2)]
    db.add_all(rows)
    db.commit()
    first = send(client, '장학금 2개 보여줘')
    second_id = first['candidates'][1]['id']
    detail = send(client, '2번 공고 자세히 설명해줘', first['session_id'])
    assert [c['id'] for c in detail['candidates']] == [second_id]
    from app.services.chat import _SESSIONS
    session = _SESSIONS[first['session_id']]
    assert session.last_tool_result['tool'] == 'detail'
    assert len(session.candidates) == 2
    compared = send(client, '두 공고 비교해줘', first['session_id'])
    assert len(compared['candidates']) == 2
    assert session.last_tool_result['tool'] == 'compare'


def test_detail_evidence_contains_body_documents_and_only_requested_candidate(client, db, monkeypatch):
    row = make_scholarship(body_text='원문에 적힌 지급 방식', required_documents=['재학증명서'])
    db.add(row)
    db.commit()
    first = send(client, '장학금 찾아줘')
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    def complete(system, prompt, **kwargs):
        data = json.loads(prompt)
        if data.get('task') == 'search_plan':
            return json.dumps({'intent': 'detail', 'tool': {'name': 'detail', 'ids': [row.id]}})
        evidence = data['evidence']
        assert evidence['tool_result']['tool'] == 'detail'
        assert evidence['candidates'][0]['body_excerpt'] == row.body_text
        assert evidence['candidates'][0]['required_documents'] == ['재학증명서']
        return json.dumps({'message': '원문에서는 재학증명서를 준비하도록 안내하고 있어요.', 'citations': [row.id]}, ensure_ascii=False)
    monkeypatch.setattr(llm, 'chat_complete', complete)
    assert send(client, '이 공고 준비 서류는?', first['session_id'])['answer_source'] == 'llm'


def test_detail_rechecks_closed_postings(client, db):
    row = make_scholarship()
    db.add(row)
    db.commit()
    first = send(client, '장학금 찾아줘')
    row.status = PostingStatus.CLOSED
    db.commit()
    assert send(client, '1번 공고 자세히 알려줘', first['session_id'])['candidates'] == []


def test_query_is_applied_before_limit_and_not_only_in_prose(client, db, monkeypatch):
    db.add_all([make_scholarship(content_key='other', title='다른 장학금'),
                make_scholarship(content_key='wanted', title='한빛재단 장학금')])
    db.commit()
    monkeypatch.setattr(llm, 'has_llm', lambda: True)
    def complete(system, prompt, **kwargs):
        if json.loads(prompt).get('task') == 'search_plan':
            return json.dumps({'query': '한빛재단', 'tool': {'name': 'search', 'ids': []}}, ensure_ascii=False)
        return json.dumps({'message': '한빛재단 공고를 찾았어요.', 'citations': []}, ensure_ascii=False)
    monkeypatch.setattr(llm, 'chat_complete', complete)
    found = send(client, '한빛재단 공고 찾아줘')
    assert [c['title'] for c in found['candidates']] == ['한빛재단 장학금']
