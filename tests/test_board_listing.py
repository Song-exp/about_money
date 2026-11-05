"""전체 목록의 정상 종료와 부분/반복 응답을 구분한다."""
import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.crawlers.public import KhuScholarshipCrawler
from app.crawlers.registry import run_crawler
from app.models.scholarship import PostingStatus, Scholarship
from tests.conftest import make_scholarship


def page_html(post_id=None):
    if post_id is None:
        return '<table><tbody><tr><td colspan="5">등록된 게시글이 없습니다.</td></tr></tbody></table>'
    return f'''<table><tbody><tr><td>1</td><td>공통_교외장학</td>
      <td><a href="javascript:view('{post_id}');">장학금 {post_id}</a></td>
      <td></td><td>2026-10-01</td></tr></tbody></table>'''


def mocked_crawler(monkeypatch, respond):
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(settings, "crawl_default_delay_sec", 0)
    crawler = KhuScholarshipCrawler()
    monkeypatch.setattr(crawler, "preflight", lambda **kwargs: None)
    return crawler


def test_scans_beyond_three_pages_and_reports_completion(monkeypatch):
    requested = []
    def respond(request):
        page = int(request.url.params["pageIndex"])
        requested.append(page)
        return httpx.Response(200, text=page_html(page if page <= 5 else None))

    crawler = mocked_crawler(monkeypatch, respond)
    assert len(crawler.fetch()) == 5
    assert requested == [1, 2, 3, 4, 5, 6]
    assert crawler.last_listing_pages == 6


@pytest.mark.parametrize("problem", ["http", "structure", "repeat", "row"])
def test_partial_listing_is_failure_and_does_not_change_existing(db, monkeypatch, problem):
    scholarship = make_scholarship(title="기존 정상 공고", source_platform="khu_janghak")
    db.add(scholarship)
    db.commit()

    def respond(request):
        page = int(request.url.params["pageIndex"])
        if page == 1:
            return httpx.Response(200, text=page_html(1))
        if problem == "http":
            return httpx.Response(503)
        if problem == "structure":
            return httpx.Response(200, text="<html>점검중</html>")
        if problem == "repeat":
            return httpx.Response(200, text=page_html(1))
        return httpx.Response(200, text='<table><tbody><tr><td>달라진 목록</td></tr></tbody></table>')

    crawler = mocked_crawler(monkeypatch, respond)
    report = run_crawler(crawler, db)
    assert report["skipped"] and not report["listing_complete"]
    assert report["listing_pages"] == 2
    assert db.get(Scholarship, scholarship.id).status == PostingStatus.OPEN
    assert len(list(db.scalars(select(Scholarship)))) == 1


def test_safety_limit_is_not_treated_as_complete(monkeypatch):
    monkeypatch.setattr(settings, "crawl_board_max_pages", 2)
    crawler = mocked_crawler(monkeypatch, lambda req: httpx.Response(
        200, text=page_html(int(req.url.params["pageIndex"])),
    ))
    with pytest.raises(RuntimeError, match="limit reached"):
        crawler.fetch()


def test_empty_board_is_a_valid_complete_listing(monkeypatch):
    crawler = mocked_crawler(monkeypatch, lambda req: httpx.Response(200, text=page_html()))
    assert crawler.fetch() == []
    assert crawler.last_listing_pages == 1
