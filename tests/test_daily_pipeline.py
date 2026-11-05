"""일간 파이프라인: 게시판 상세 재확인, API 모집중 목록 동기화."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.crawlers.base import BaseCrawler, RawPosting
from app.crawlers.registry import run_crawler
from app.models.crawl_run import CrawlRun, CrawlRunStatus
from app.models.scholarship import Category, PostingStatus, Scholarship, SourceType
from app.models.scholarship_revision import ScholarshipRevision
from app.schemas.schemas import UserProfile
from app.services.matching import match_scholarships
from tests.conftest import KST, make_scholarship


class FakeCrawler(BaseCrawler):
    tos_allows_crawling = True

    def __init__(self, platform, posts, full_sync=False):
        self.platform, self.posts, self.full_sync = platform, posts, full_sync
        self.detailed = []

    def fetch(self):
        return list(self.posts)

    def fetch_detail(self, raw):
        self.detailed.append(raw.title)


def _raw(title, platform, deadline=None, posted=None):
    return RawPosting(
        title=title, source_url=f"https://x.test/{title}", source_platform=platform,
        source_type=SourceType.PUBLIC, category=Category.SCHOLARSHIP,
        deadline_at=deadline, posted_at=posted,
    )


def _titles(db, platform):
    return set(db.scalars(select(Scholarship.title).where(Scholarship.source_platform == platform)))


def test_board_rechecks_existing_and_older_posts(db):
    now = datetime.now(KST)
    posts = [
        _raw("모집중", "board", deadline=now + timedelta(days=5), posted=now),
        _raw("이미마감", "board", deadline=now - timedelta(days=1), posted=now),
        _raw("날짜없음-최근", "board", posted=now - timedelta(days=3)),
        _raw("날짜없음-오래됨", "board", posted=now - timedelta(days=60)),
    ]
    first = FakeCrawler("board", posts)
    assert run_crawler(first, db)["saved"] == 4
    assert len(first.detailed) == 4
    assert _titles(db, "board") == {p.title for p in posts}
    statuses = {row.title: row.status for row in db.scalars(select(Scholarship))}
    assert statuses["모집중"] == PostingStatus.OPEN
    assert statuses["이미마감"] == PostingStatus.CLOSED
    assert statuses["날짜없음-최근"] == statuses["날짜없음-오래됨"] == PostingStatus.NEEDS_REVIEW

    utc_now = datetime.now(timezone.utc)
    db.add(CrawlRun(
        trigger="scheduled", status=CrawlRunStatus.SUCCESS, started_at=utc_now,
        finished_at=utc_now, per_platform=[{"platform": "board", "skipped": False}],
    ))
    db.commit()

    yesterday = _raw("어제글", "board", deadline=now + timedelta(days=5), posted=now - timedelta(days=1))
    second = FakeCrawler("board", posts + [yesterday])
    run_crawler(second, db)
    # 날짜 체크포인트나 기존 DB 존재로 수정/실패 복구를 건너뛰지 않는다.
    assert second.detailed == [p.title for p in posts] + ["어제글"]


def test_complete_board_marks_missing_posts_for_review_without_deleting(db):
    now = datetime.now(KST)
    db.add(make_scholarship(title="오래된공지", source_platform="board", deadline_at=None,
                            posted_at=now - timedelta(days=60)))
    db.add(make_scholarship(title="최근공지", source_platform="board", deadline_at=None,
                            posted_at=now - timedelta(days=3)))
    db.commit()

    crawler = FakeCrawler("board", [])
    crawler.complete_listing = True
    report = run_crawler(crawler, db)
    assert report["needs_review"] == 2 and report["removed"] == 0
    assert _titles(db, "board") == {"최근공지", "오래된공지"}


def test_full_sync_closes_postings_missing_from_source(db):
    db.add(make_scholarship(title="내려간정책", source_platform="api"))
    db.add(make_scholarship(title="다른소스", source_platform="board"))
    db.commit()

    kept = _raw("유지정책", "api", deadline=datetime.now(KST) + timedelta(days=5))
    crawler = FakeCrawler("api", [kept], full_sync=True)
    report = run_crawler(crawler, db)

    assert report["closed"] == 1 and report["removed"] == 0 and crawler.detailed == []
    assert _titles(db, "api") == {"유지정책", "내려간정책"}
    assert db.scalar(select(Scholarship).where(Scholarship.title == "내려간정책")).status == PostingStatus.CLOSED
    assert _titles(db, "board") == {"다른소스"}


def test_empty_complete_api_closes_all_source_rows_and_can_reopen(db):
    now = datetime.now(KST)
    raw = _raw("정책", "api", deadline=now + timedelta(days=5))
    run_crawler(FakeCrawler("api", [raw], full_sync=True), db)
    row = db.scalar(select(Scholarship))
    original_id = row.id
    report = run_crawler(FakeCrawler("api", [], full_sync=True), db)
    assert report["closed"] == 1
    assert row.status == PostingStatus.CLOSED
    assert match_scholarships(db, UserProfile()) == []
    assert run_crawler(FakeCrawler("api", [], full_sync=True), db)["closed"] == 0
    run_crawler(FakeCrawler("api", [raw], full_sync=True), db)
    assert row.id == original_id and row.status == PostingStatus.OPEN


def test_api_metadata_changes_are_versioned_even_if_body_is_same(db):
    raw = _raw("정책", "api", deadline=datetime.now(KST) + timedelta(days=5))
    raw.benefit = {"amount_krw": 100000}
    run_crawler(FakeCrawler("api", [raw], full_sync=True), db)
    raw.benefit = {"amount_krw": 200000}
    run_crawler(FakeCrawler("api", [raw], full_sync=True), db)
    row = db.scalar(select(Scholarship))
    assert row.benefit["amount_krw"] == 200000
    revisions = list(db.scalars(select(ScholarshipRevision).where(ScholarshipRevision.scholarship_id == row.id)))
    assert {r.snapshot["benefit"]["amount_krw"] for r in revisions} == {100000, 200000}
    count = len(revisions)
    run_crawler(FakeCrawler("api", [raw], full_sync=True), db)
    assert len(list(db.scalars(select(ScholarshipRevision)))) == count
    db.expire_all()
    from app.services.scholarship_history import record_revision
    record_revision(db, db.get(Scholarship, row.id))
    assert len(list(db.scalars(select(ScholarshipRevision)))) == count


def test_unknown_board_deadline_is_preserved_but_not_recommended(db):
    raw = _raw("최근 공고", "board", posted=datetime.now(KST))
    run_crawler(FakeCrawler("board", [raw]), db)
    assert db.scalar(select(Scholarship)).status == PostingStatus.NEEDS_REVIEW
    assert match_scholarships(db, UserProfile()) == []
