from datetime import datetime

import httpx
import pytest

from app.core.config import settings
from app.crawlers.base import RawPosting
from app.crawlers.public import KST
from app.models.scholarship import Category, SourceType
from app.scripts.run_collection_once import WindowedScholarships


def posting(key, day):
    return RawPosting(title=key, source_url=f'https://example.test/{key}', source_platform='khu_janghak',
                      category=Category.SCHOLARSHIP, source_type=SourceType.PUBLIC,
                      posted_at=datetime.fromisoformat(day).replace(tzinfo=KST) if day else None)


def setup(monkeypatch, pages):
    real = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: real(transport=httpx.MockTransport(lambda req: httpx.Response(200))))
    monkeypatch.setattr(settings, 'crawl_default_delay_sec', 0)
    monkeypatch.setattr(settings, 'crawl_board_max_pages', 3)
    crawler = WindowedScholarships()
    crawler.since = datetime(2026, 9, 28, tzinfo=KST)
    crawler.retained = []
    iterator = iter(pages)
    monkeypatch.setattr(crawler, 'parse_listing_page', lambda *a: next(iterator))
    return crawler


def test_date_window_keeps_boundary_and_existing_notices_without_full_listing_claim(monkeypatch):
    fresh, boundary, old = posting('fresh', '2026-10-24'), posting('boundary', '2026-09-28'), posting('old', '2026-09-01')
    crawler = setup(monkeypatch, [([fresh, boundary, old], False), ([old], False)])
    crawler.retained = [old, fresh]
    assert [x.title for x in crawler.fetch()] == ['fresh', 'boundary', 'old']
    assert crawler.last_listing_pages == 2
    assert crawler.complete_listing is False and crawler.full_sync is False


def test_undated_rows_do_not_stop_the_window_early(monkeypatch):
    unknown = posting('unknown', None)
    crawler = setup(monkeypatch, [([unknown], False), ([], True)])
    assert crawler.fetch() == [unknown]
    assert crawler.last_listing_pages == 2


def test_truncated_listing_is_a_failure(monkeypatch):
    fresh = posting('fresh', '2026-10-24')
    crawler = setup(monkeypatch, [([fresh], False)] * 3)
    with pytest.raises(RuntimeError, match='pagination limit'):
        crawler.fetch()
