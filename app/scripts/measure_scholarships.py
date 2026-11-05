"""실제 공식 게시판 대조·첨부 확보·현재 파서 측정 자료 수집 (DB 공고 수정 없음).

python -m app.scripts.measure_scholarships --output reports/... --since 2026-08-18
목록 정답은 운영 크롤러와 별도로 공식 HTML의 행을 읽는다. 정확도 정답은 수동 대조로 별도 작성한다.
"""
import argparse
import hashlib
import json
import re
import shutil
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from app.core.config import settings
from app.core.db import SessionLocal
from app.crawlers.base import RawPosting
from app.crawlers.public import KhuScholarshipCrawler
from app.crawlers.registry import _enrich_from_body
from app.models.scholarship import Category, Scholarship, SourceType
from app.services.posting_documents import structure_documents


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=lambda x: x.isoformat()), encoding="utf-8")


def reference_listing(output, since, until=None):
    until = until or date.today()
    crawler = KhuScholarshipCrawler()
    listing, seen = [], set()
    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": crawler.user_agent}) as client:
        for page in range(1, 101):
            response = client.get(f"{crawler.base_url}{crawler.bbs_path}/list.do", params={"menuNo": crawler.menu_no, "pageIndex": page})
            response.raise_for_status()
            (output / f"listing-{page}.html").write_bytes(response.content)
            soup = BeautifulSoup(response.content, "lxml")
            table = soup.select_one("table")
            if table is None or table.select_one("tbody") is None:
                raise RuntimeError("reference_listing_structure_missing")
            dates, found = [], 0
            for row in table.select("tbody tr"):
                text = row.get_text(" ", strip=True)
                link = row.select_one("a[href]")
                stamp = re.findall(r"20\d{2}-\d{2}-\d{2}", text)
                if not link:
                    if "없습니다" in text or "없음" in text:
                        continue
                    raise RuntimeError("reference_row_missing_link")
                match = re.search(r"view\('([0-9]+)'\)", link.get("href", ""))
                if not match or not stamp:
                    raise RuntimeError("reference_row_missing_id_or_date")
                board_id = match[1]
                if board_id in seen:
                    continue
                seen.add(board_id); found += 1
                posted = date.fromisoformat(stamp[-1]); dates.append(posted)
                if since <= posted <= until:
                    listing.append({"board_id": board_id, "title": link.get_text(" ", strip=True), "posted_at": posted.isoformat(),
                                    "source_url": f"{crawler.base_url}{crawler.bbs_path}/view.do?menuNo={crawler.menu_no}&boardId={board_id}"})
            print(f"reference page={page} window_rows={len(listing)}", flush=True)
            if not found or (dates and max(dates) < since):
                write(output / "reference-listing.json", {"since": since.isoformat(), "until": until.isoformat(), "pages": page, "window_complete": True, "rows": listing})
                return listing
            time.sleep(settings.crawl_default_delay_sec)
    raise RuntimeError("reference_window_not_complete")


def capture(output, reference, sample_ids, capture_only=False):
    baseline_path = output / "database-before.json"
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    else:
        with SessionLocal() as db:
            baseline = [{"id": r.id, "title": r.title, "source_url": r.source_url, "source_platform": r.source_platform,
                         "deadline_at": r.deadline_at.isoformat() if r.deadline_at else None, "body_text": r.body_text,
                         "eligibility": r.eligibility, "benefit": r.benefit} for r in db.scalars(select(Scholarship))]
        write(baseline_path, baseline)
    urls = {r["source_url"] for r in baseline if r["source_platform"] == "khu_janghak"}
    matched = sum(r["source_url"] in urls for r in reference)
    write(output / "listing-metrics.json", {"reference_count": len(reference), "baseline_matched": matched,
          "baseline_notice_coverage": matched / len(reference) if reference else None,
          "missing": [r for r in reference if r["source_url"] not in urls], "scope": "KHU official listing in specified publication date window; includes notices, not just currently open scholarships"})
    samples = [r for r in baseline if r["id"] in sample_ids]
    # 最新 공고도 2건 포함한다. 기존 DB 표본과 별도로 실제 누락을 확인한다.
    samples += [{**r, "id": "new-" + r["board_id"]} for r in reference if r["source_url"] not in urls][:2]
    crawler = KhuScholarshipCrawler()
    crawler.preflight(respect_robots=settings.crawl_respect_robots)
    print(f"detail samples={len(samples)} llm_configured={bool(settings.deepseek_api_key)}", flush=True)
    for row in samples:
        folder = output / f"notice-{row['id']}"
        folder.mkdir(exist_ok=True)
        if (folder / "prediction.json").exists():
            print(f"detail resume skip id={row['id']}", flush=True)
            continue
        raw = RawPosting(title=row["title"], source_url=row["source_url"], source_platform="khu_janghak",
                         source_type=SourceType.PUBLIC, category=Category.SCHOLARSHIP)
        print(f"detail start id={row['id']}", flush=True)
        crawler.fetch_detail(raw)
        expected = []
        html = next((d.get("content") for d in raw.source_documents if d.get("kind") == "html"), None)
        if html:
            soup = BeautifulSoup(html, "lxml")
            for link in soup.select('a[href*="fileDown"]'):
                url = urljoin(raw.source_url, link.get("href", ""))
                if url not in [e["url"] for e in expected]:
                    expected.append({"url": url, "filename": link.get_text(" ", strip=True)})
        docs = []
        for i, document in enumerate(raw.source_documents):
            item = {k: v for k, v in document.items() if k != "content"}
            if "content" in document:
                digest = hashlib.sha256(document["content"]).hexdigest()
                (folder / f"asset-{i}-{digest[:12]}.bin").write_bytes(document["content"])
                item["sha256"] = digest
                item["local_file"] = f"asset-{i}-{digest[:12]}.bin"
            docs.append(item)
        write(folder / "capture.json", {"id": row["id"], "title": row["title"], "source_url": row["source_url"],
              "fetch_status": raw.fetch_status, "fetch_error": raw.fetch_error, "expected_attachments": expected, "documents": docs})
        # 저장된 실제 바이트로 이후 재현할 수 있다. 결과를 다운로드 성공과 혼동하지 않는다.
        (folder / "extracted.txt").write_text(raw.body_text, encoding="utf-8")
        if capture_only:
            print(f"detail captured id={row['id']} documents={len(docs)}", flush=True)
            continue
        structure_documents(raw)
        _enrich_from_body(raw)
        write(folder / "prediction.json", {"id": row["id"], "title": row["title"], "source_url": row["source_url"],
              "deadline_at": raw.deadline_at, "eligibility": raw.eligibility, "benefit": raw.benefit,
              "required_documents": raw.required_documents, "status": raw.fetch_status,
              "documents": [{k: v for k, v in d.items() if k != "content"} for d in raw.source_documents]})
        print(f"detail end id={row['id']} documents={len(docs)} status={raw.fetch_status} fields={len(raw.eligibility.get('application_fields', []))}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--since", default="2026-08-18")
    parser.add_argument("--sample-ids", default="1,2,3,12,17,22,29,881")
    parser.add_argument("--capture-only", action="store_true")
    parser.add_argument("--reuse-listing", action="store_true", help="같은 평가 실행에서 보관한 공식 목록 사용")
    parser.add_argument("--resume", action="store_true", help="동일 설정의 미완료 실행만 이어서 처리")
    args = parser.parse_args()
    output = Path(args.output)
    if (output / "gold.json").exists():
        parser.error("평가 정답이 있는 실행은 변경할 수 없습니다. remeasure_scholarships로 별도 실행을 만드세요.")
    if output.exists() and any(output.iterdir()) and not args.resume:
        parser.error("기존 실행 경로입니다. 새 경로 또는 --resume을 사용하세요.")
    output.mkdir(parents=True, exist_ok=True)
    from app.services.ocr import runtime_status
    if not (output / "environment.json").exists():
        write(output / "environment.json", {"measured_at": datetime.now(timezone.utc), "llm_configured": bool(settings.deepseek_api_key),
              "llm_model": settings.llm_model, "document_runtime": runtime_status()})
    reference = (json.loads((output / "reference-listing.json").read_text(encoding="utf-8"))["rows"] if args.reuse_listing else reference_listing(output, date.fromisoformat(args.since)))
    capture(output, reference, {int(i) for i in args.sample_ids.split(",")}, args.capture_only)


if __name__ == "__main__":
    main()
