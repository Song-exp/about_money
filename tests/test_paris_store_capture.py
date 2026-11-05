import hashlib
import json
import sys

import httpx
import pytest

from app.scripts import probe_paris_stores as collector


POLICY = 'User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/admin-ajax.php\n'


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(collector.time, 'sleep', lambda _: None)


def test_only_documented_read_only_search_is_requested_and_receipted():
    requests = []
    raw = '<ul><li>공식 검색 응답</li></ul>'

    def respond(request):
        requests.append(request)
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text=POLICY)
        assert request.method == 'GET'
        assert request.url.path == '/wp-admin/admin-ajax.php'
        assert request.url.params['action'] == 'pb_store_get_list'
        assert request.url.params['keyword'] == '경희'
        assert request.url.params['type'] == '#search'
        return httpx.Response(200, text=raw)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        content, receipt = collector.capture_paris('경희', client=client)
    assert len(requests) == 2 and content == raw
    assert receipt['complete'] is False
    assert receipt['response_sha256'] == hashlib.sha256(raw.encode()).hexdigest()


@pytest.mark.parametrize('response', [httpx.Response(403), httpx.Response(503),
    httpx.Response(302, headers={'location': 'https://example.com/'})])
def test_failed_robots_fetch_never_calls_search(response):
    requests = []

    def respond(request):
        requests.append(request)
        return response

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            collector.capture_paris('경희', client=client)
    assert len(requests) == 1


def test_disallowed_search_is_not_requested():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text='User-agent: *\nDisallow: /wp-admin/')

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match='source_disallows_fetch'):
            collector.capture_paris('경희', client=client)
    assert len(requests) == 1


def test_search_http_failure_is_not_a_successful_empty_capture():
    def respond(request):
        return httpx.Response(200, text=POLICY) if request.url.path == '/robots.txt' else httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            collector.capture_paris('경희', client=client)


def test_download_stops_when_stream_exceeds_size_limit(monkeypatch):
    monkeypatch.setattr(collector, 'MAX_RESPONSE_BYTES', 10)
    consumed = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(10):
                consumed.append(index)
                yield b'123456'

    def respond(request):
        return httpx.Response(200, text=POLICY) if request.url.path == '/robots.txt' else httpx.Response(200, stream=Stream())

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match='too_large'):
            collector.capture_paris('경희', client=client)
    assert consumed == [0, 1]


def test_cli_failure_preserves_prior_success_and_records_separate_failure(tmp_path, monkeypatch):
    old_raw, old_receipt = '<ul>prior</ul>', '{"captured_at":"prior"}'
    (tmp_path / 'search.html').write_text(old_raw, encoding='utf-8')
    (tmp_path / 'receipt.json').write_text(old_receipt, encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['probe', '--query', '경희', '--output', str(tmp_path)])

    def timeout(*args):
        raise httpx.ReadTimeout('sensitive request diagnostics must not be printed')

    monkeypatch.setattr(collector, 'capture_paris', timeout)
    with pytest.raises(SystemExit) as stopped:
        collector.main()
    assert stopped.value.code == 1
    result = json.loads((tmp_path / 'failure.json').read_text(encoding='utf-8'))
    assert result['status'] == 'failed' and result['database_changed'] is False
    assert result['reason'] == 'source_http_error' and 'captured_at' not in result
    assert 'sensitive' not in json.dumps(result)
    assert (tmp_path / 'search.html').read_text(encoding='utf-8') == old_raw
    assert (tmp_path / 'receipt.json').read_text(encoding='utf-8') == old_receipt


@pytest.mark.parametrize('raw,status,records', [
    ('<ul data-total-count="0"></ul>', 'parsed', 0),
    ('\ufeff\t', 'needs_review', None),
    ('<h1>Page changed</h1>', 'needs_review', None)])
def test_capture_reports_semantic_status_without_inventing_empty_results(raw, status, records):
    def respond(request):
        return httpx.Response(200, text=POLICY) if request.url.path == '/robots.txt' else httpx.Response(200, text=raw)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        content, metadata = collector.capture_paris('경희', client=client)
    assert content == raw
    assert metadata['parse_status'] == status and metadata['records'] == records
