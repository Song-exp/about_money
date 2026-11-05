from pathlib import Path
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select,func

from app.models.card_collection import CardCollectionAttempt,CardSourceSnapshot
from app.models.card import Card
from app.services.card_collection import collection_error_code,extract_document,capture_document,register_source,collect_due,TOSS_KPASS_URL
from tests.test_card_collection import NOW


def body():
    data=Path('tests/fixtures/toss-kpass-hydration.json').read_bytes()
    return Path('tests/fixtures/toss-kpass-root.html').read_bytes()+b'<script id="__NEXT_DATA__" type="application/json">'+data+b'</script>'


def test_actual_kpass_root_retains_sections_beyond_hero_and_excludes_navigation():
    with pytest.raises(ValueError,match='incomplete_card_document'): extract_document(body())
    text,mime=extract_document(body(),source_url=TOSS_KPASS_URL)
    assert mime=='text/html' and len(text)>400
    assert '2천원 추가 캐시백' in text and '상품 안내' in text
    assert 'excluded navigation' not in text and 'excluded footer' not in text


@pytest.mark.parametrize('data', [b'<div id="root">wrong product</div>',
    '<main>토스뱅크 K-패스 체크카드</main>'.encode()])
def test_changed_kpass_layout_cannot_fall_back_to_whole_page(data):
    with pytest.raises(ValueError,match='toss_kpass_layout_changed'):
        extract_document(data,source_url=TOSS_KPASS_URL)


def test_network_errors_do_not_persist_sensitive_exception_text():
    error=httpx.ConnectError('secret proxy password in message')
    error.__cause__=OSError(10013,'private network detail')
    assert collection_error_code(error)=='network_access_denied'
    assert collection_error_code(httpx.ConnectError('secret'))=='network_connection_failed'
    assert collection_error_code(httpx.ProxyError('secret'))=='proxy_connection_failed'
    assert collection_error_code(httpx.ReadTimeout('secret'))=='network_timeout'


@pytest.mark.parametrize('url,status,expected',[
    ('https://www.bccard.com/robots.txt',302,'robots_redirect_requires_configuration'),
    ('https://www.bccard.com/robots.txt',403,'robots_http_403'),
    ('https://www.bccard.com/app/card/contents/CardSub.do',302,'source_redirect_requires_configuration'),
    ('https://www.bccard.com/app/card/contents/CardSub.do',429,'source_http_429')])
def test_http_stage_is_preserved(url,status,expected):
    request=httpx.Request('GET',url)
    response=httpx.Response(status,request=request,headers={'location':'https://secret.invalid/?token=secret'})
    error=httpx.HTTPStatusError('secret exception text',request=request,response=response)
    assert collection_error_code(error)==expected


def test_successful_recovery_resets_failures_without_approving_card(db,tmp_path):
    source=register_source(db,TOSS_KPASS_URL,provider_key='tossbank',now=NOW)
    source.failure_count=9
    source.last_error='ConnectError'
    source.next_retry_at=NOW-timedelta(minutes=1)
    db.commit()
    paths=[]
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(404) if request.url.path=='/robots.txt' else httpx.Response(200,content=body())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report=collect_due(db,client=client,now=NOW,archive_root=tmp_path,request_interval=0,trigger='test')
    assert report['captured']==1 and report['failed']==report['auto_approved']==0
    db.refresh(source)
    assert source.failure_count==0 and source.last_error is None and source.next_retry_at is None
    snapshot=db.scalar(select(CardSourceSnapshot))
    assert snapshot.parser_version=='toss_kpass_root_v1' and '2천원 추가 캐시백' in snapshot.extracted_text
    assert db.scalar(select(func.count()).select_from(Card))==0


def test_robots_redirect_is_not_followed_or_counted_as_a_document(db,tmp_path):
    source=register_source(db,'https://www.bccard.com/app/card/contents/CardSub.do',provider_key='bc',now=NOW)
    db.commit()
    paths=[]
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(302,headers={'location':'/common/default-error-page/error.html'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report=collect_due(db,client=client,now=NOW,archive_root=tmp_path,request_interval=0,trigger='test')
    db.refresh(source)
    assert paths==['/robots.txt'] and report['failed']==1
    assert source.last_error=='robots_redirect_requires_configuration'
    assert db.scalar(select(CardCollectionAttempt.http_status))==302
    assert db.scalar(select(func.count()).select_from(CardSourceSnapshot))==0


def test_source_filter_never_bypasses_retry_time_or_fetches_unselected_source(db,tmp_path):
    target=register_source(db,TOSS_KPASS_URL,provider_key='tossbank',now=NOW)
    register_source(db,'https://www.tossbank.com/card/benefits',provider_key='tossbank',now=NOW)
    target.next_retry_at=NOW+timedelta(hours=1)
    db.commit()
    def handler(request):
        raise AssertionError('No requests while target is backing off')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        outcome=collect_due(db,client=client,now=NOW,archive_root=tmp_path,only_source_ids=[target.id])
    assert outcome['status']=='idle'


@pytest.mark.parametrize('ids',[[],[True],[0],[-1],['1']])
def test_invalid_source_filter_is_rejected(db,ids):
    with pytest.raises(ValueError,match='invalid_card_source_filter'):
        collect_due(db,only_source_ids=ids)
