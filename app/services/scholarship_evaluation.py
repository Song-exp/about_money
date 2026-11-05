"""다운로드 성공률과 정답 대조를 구분한다. 실패·누락도 분모에 포함한다."""
import json
import hashlib
import re
from collections import Counter
from pathlib import Path


def _normalize(value):
    return re.sub(r"\s+", "", value)


def _metric(correct, total):
    return {"correct": correct, "total": total, "rate": correct / total if total else None}


def evaluate(root, gold, *, include_unstructured_captures=False):
    root = Path(root)
    for expected in [*gold.get("deadlines", []), *gold.get("facts", []), *gold.get("forms", [])]:
        if expected.get("proof_sha256") and hashlib.sha256((root / expected["proof"]).read_bytes()).hexdigest() != expected["proof_sha256"]:
            raise ValueError("gold proof checksum mismatch")
    predictions, captures = {}, {}
    for folder in root.glob("notice-*"):
        path = folder / "prediction.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            predictions[str(data["id"])] = data
        capture_path = folder / "capture.json"
        if capture_path.exists() and (path.exists() or include_unstructured_captures):
            capture = json.loads(capture_path.read_text(encoding="utf-8"))
            captures[str(capture["id"])] = capture
            for document in capture["documents"]:
                if document.get("local_file") and document.get("sha256") and hashlib.sha256((folder / document["local_file"]).read_bytes()).hexdigest() != document["sha256"]:
                    raise ValueError("captured source checksum mismatch")
    deadline_results = []
    for expected in gold.get("deadlines", []):
        actual = predictions.get(str(expected["id"]), {}).get("deadline_at")
        deadline_results.append({**expected, "actual": actual, "day_correct": bool(actual) and actual[:10] == expected["day"],
                                 "time_correct": bool(actual) and actual[11:16] == expected.get("time") if expected.get("time") else None})
    facts = []
    for expected in gold.get("facts", []):
        value = predictions.get(str(expected["id"]), {})
        for key in expected["path"].split("."):
            value = value.get(key) if isinstance(value, dict) else None
        correct = sorted(value) == sorted(expected["value"]) if isinstance(value, list) and isinstance(expected["value"], list) else value == expected["value"]
        facts.append({**expected, "actual": value, "correct": correct})
    forms = []
    for form in gold.get("forms", []):
        fields = predictions.get(str(form["id"]), {}).get("eligibility", {}).get("application_fields", [])
        fields = [f for f in fields if any(f.get("source", {}).get("sha256", "").startswith(p) for p in form["source_sha_prefixes"])]
        available = list(fields)
        matched, missing = [], []
        for expected in form["fields"]:
            aliases = {_normalize(s) for s in expected["labels"]}
            index = next((i for i, field in enumerate(available) if _normalize(field["label"]) in aliases), None)
            if index is None:
                missing.append(expected)
            else:
                matched.append({"expected_key": expected["key"], "actual": available.pop(index)})
        role_available = list(fields)
        role_matches, role_missing = [], []
        for expected in form["fields"]:
            role = expected["key"].split(".")[0]
            if role not in ("student", "guardian", "applicant"):
                continue
            aliases = {_normalize(s) for s in expected["labels"]}
            index = next((i for i, field in enumerate(role_available)
                          if _normalize(field["label"]) in aliases and field.get("role") == role), None)
            if index is None:
                role_missing.append(expected)
            else:
                role_matches.append({"expected_key": expected["key"], "actual": role_available.pop(index)})
        forms.append({"id": form["id"], "scope": form["scope"], "matched": matched, "missing": missing,
                      "unmatched_predictions": available, "label_recall_upper_bound": _metric(len(matched), len(form["fields"])),
                      "role_matched": role_matches, "role_missing": role_missing,
                      "role_label_recall": _metric(len(role_matches), len(role_matches) + len(role_missing)),
                      "unmatched_prediction_count": len(available),
                      "all_reviewed_fields_recovered": not missing})
    attachments_total = attachments_saved = assets_total = assets_saved = fully_parsed = complete = 0
    failures = Counter()
    for capture in captures.values():
        documents = capture["documents"]
        retrieved = {d["url"] for d in documents if d.get("local_file")}
        attachments_total += len(capture["expected_attachments"])
        attachments_saved += sum(e["url"] in retrieved for e in capture["expected_attachments"])
        # 원문 DOM의 fileDown 첨부 외 이미지/외부 파일도 포함한다. ZIP 내부 멤버는 원격 자산으로 중복 계산하지 않는다.
        assets = {d["url"] for d in documents if d.get("kind") != "html" and not d.get("member_path")}
        assets.update(capture.get("expected_related_assets", []))
        assets_total += len(assets)
        assets_saved += len(assets & retrieved)
        fully_parsed += len({d["url"] for d in documents if d.get("status") == "parsed" and d.get("kind") != "html" and not d.get("member_path")})
        complete += capture["fetch_status"] == "complete"
        failures.update(d["error"] for d in documents if d.get("error"))
    timed = [r for r in deadline_results if r["time_correct"] is not None]
    listing = json.loads((root / "listing-metrics.json").read_text(encoding="utf-8")) if (root / "listing-metrics.json").exists() else None
    reference_path = root / "reference-listing.json"
    if reference_path.exists():
        # 현재 운영 목록 파서에 실제 보관 페이지를 입력한다. 전체 게시판의 종료 탐지는 이 지표에서 검증하지 않는다.
        from app.crawlers.public import KhuScholarshipCrawler
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        crawler, seen, actual_urls = KhuScholarshipCrawler(), set(), set()
        for page in range(1, reference["pages"] + 1):
            records, _ = crawler.parse_listing_page((root / f"listing-{page}.html").read_bytes(), seen)
            actual_urls.update(r.source_url for r in records if r.posted_at and reference["since"] <= r.posted_at.date().isoformat() <= reference["until"])
        expected_urls = {r["source_url"] for r in reference["rows"]}
        listing = {**(listing or {}), "current_parser_window_recall": _metric(len(expected_urls & actual_urls), len(expected_urls)),
                   "current_parser_extra_urls": sorted(actual_urls - expected_urls),
                   "parser_scope": f"Replay of {reference['pages']} saved real listing pages in the stated date window; not full-board termination or database ingestion"}
        if (root / "database-before.json").exists():
            baseline = json.loads((root / "database-before.json").read_text(encoding="utf-8"))
            baseline_urls = {r["source_url"] for r in baseline if r["source_platform"] == "khu_janghak"}
            recent = [r for r in reference["rows"] if r["posted_at"] >= "2026-09-29"]
            listing["recent_notice_baseline_coverage"] = _metric(sum(r["source_url"] in baseline_urls for r in recent), len(recent))
            listing["recent_scope_since"] = "2026-09-29"
    return {"reviewer": gold.get("reviewer"), "sampling": gold.get("sampling"), "prediction_count": len(predictions),
            "listing": listing,
            "filedown_attachment_retrieval": _metric(attachments_saved, attachments_total),
            "linked_asset_retrieval": _metric(assets_saved, assets_total),
            "linked_asset_parsed_without_issue": _metric(fully_parsed, assets_total),
            "complete_detail_fetches": _metric(complete, len(captures)), "download_failure_types": dict(failures),
            "deadline_day_recall": _metric(sum(r["day_correct"] for r in deadline_results), len(deadline_results)),
            "deadline_explicit_time_recall": _metric(sum(r["day_correct"] and r["time_correct"] for r in timed), len(timed)),
            "reviewed_fact_recall": _metric(sum(r["correct"] for r in facts), len(facts)),
            "field_label_recall_upper_bound": _metric(sum(len(f["matched"]) for f in forms), sum(len(f["matched"]) + len(f["missing"]) for f in forms)),
            "field_role_label_recall": _metric(sum(len(f["role_matched"]) for f in forms), sum(len(f["role_matched"]) + len(f["role_missing"]) for f in forms)),
            "unmatched_form_predictions": sum(len(f["unmatched_predictions"]) for f in forms),
            "forms_with_all_reviewed_fields": _metric(sum(f["all_reviewed_fields_recovered"] for f in forms), len(forms)),
            "deadlines": deadline_results, "facts": facts, "forms": forms,
            "limits": ["No random or service-wide accuracy estimate", "Missing predictions count as failures",
                       "Field label matching is an upper bound: repeated roles, type, constraints and layout are not validated by this rate",
                       "Role-label metric covers only gold keys explicitly starting student/guardian/applicant; repeated sections and type/constraints still need review",
                       "Unmatched predictions are candidates for duplicates/excess, not a fully reviewed false-positive count",
                       "No automatic approval of forms from these metrics", "No independent second human review of gold yet"]}
