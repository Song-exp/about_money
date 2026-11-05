import json
from app.services.scholarship_evaluation import evaluate


def write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_missing_predictions_are_counted_and_duplicate_roles_not_overcounted(tmp_path):
    folder = tmp_path / "notice-1"; folder.mkdir()
    write(folder / "prediction.json", {"id": 1, "deadline_at": "2026-10-08T23:59:00+09:00", "eligibility": {
        "gpa_min": 3.5, "application_fields": [{"label": "성 명", "source": {"sha256": "abc"}}]}})
    write(folder / "capture.json", {"id": 1, "fetch_status": "partial", "expected_attachments": [{"url": "a"}, {"url": "b"}],
          "documents": [{"url": "html", "kind": "html", "status": "parsed", "local_file": "x"},
                        {"url": "a", "kind": "attachment", "status": "parsed", "local_file": "y"},
                        {"url": "b", "kind": "attachment", "status": "failed", "error": "timeout"},
                        {"url": "image", "kind": "external_asset", "status": "unresolved", "error": "host"}]})
    gold = {"deadlines": [{"id": 1, "day": "2026-10-08", "time": "18:00"}, {"id": 2, "day": "2026-10-09"}],
            "facts": [{"id": 1, "path": "eligibility.gpa_min", "value": 3.5}, {"id": 2, "path": "eligibility.gpa_min", "value": 3.5}],
            "forms": [{"id": 1, "scope": "two roles", "source_sha_prefixes": ["abc"], "fields": [
                {"key": "student", "labels": ["성명"]}, {"key": "guardian", "labels": ["성명"]}]}]}
    result = evaluate(tmp_path, gold)
    assert result["deadline_day_recall"]["rate"] == 0.5
    assert result["deadline_explicit_time_recall"]["rate"] == 0
    assert result["reviewed_fact_recall"]["rate"] == 0.5
    assert result["field_label_recall_upper_bound"]["rate"] == 0.5
    assert result["forms_with_all_reviewed_fields"]["correct"] == 0
    assert result["filedown_attachment_retrieval"]["rate"] == 0.5
    assert result["linked_asset_retrieval"]["rate"] == 1 / 3
    assert result["complete_detail_fetches"]["correct"] == 0


def test_empty_evaluation_does_not_claim_perfect_accuracy(tmp_path):
    result = evaluate(tmp_path, {})
    assert result["deadline_day_recall"]["rate"] is None
    assert result["filedown_attachment_retrieval"]["rate"] is None
    assert result["forms_with_all_reviewed_fields"]["rate"] is None


def test_role_metric_does_not_credit_a_student_field_as_guardian(tmp_path):
    folder = tmp_path / "notice-1"
    folder.mkdir()
    write(folder / "prediction.json", {"id": 1, "eligibility": {"application_fields": [
        {"label": "성명", "role": "student", "source": {"sha256": "abc"}},
        {"label": "성명", "role": "student", "source": {"sha256": "abc"}}]}})
    gold = {"forms": [{"id": 1, "scope": "two roles", "source_sha_prefixes": ["abc"], "fields": [
        {"key": "student.name", "labels": ["성명"]}, {"key": "guardian.name", "labels": ["성명"]}]}]}
    result = evaluate(tmp_path, gold)
    assert result["field_label_recall_upper_bound"]["correct"] == 2
    assert result["field_role_label_recall"]["correct"] == 1
    assert result["forms"][0]["role_missing"][0]["key"] == "guardian.name"


def test_corrupt_source_proof_is_rejected(tmp_path):
    import pytest
    (tmp_path / "proof.txt").write_text("original", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        evaluate(tmp_path, {"facts": [{"id": 1, "path": "benefit.amount_krw", "value": 1, "proof": "proof.txt", "proof_sha256": "wrong"}]})


def test_multiple_tbody_real_listing_structure_replays_current_parser(tmp_path):
    html = b"<table><tbody></tbody><tbody><tr><td>normal</td><td>outside</td><td><a href=\"javascript:view('123');\">title</a></td><td>file</td><td>2026-10-02</td></tr></tbody></table>"
    (tmp_path / "listing-1.html").write_bytes(html)
    url = "https://janghak.khu.ac.kr/janghak/user/bbs/BMSR00040/view.do?menuNo=12300032&boardId=123"
    write(tmp_path / "reference-listing.json", {"since": "2026-09-29", "until": "2026-10-05", "pages": 1, "rows": [{"source_url": url}]})
    result = evaluate(tmp_path, {})
    assert result["listing"]["current_parser_window_recall"]["rate"] == 1


def test_capture_only_scores_retrieval_and_keeps_missing_reference_asset_in_denominator(tmp_path):
    folder = tmp_path / "notice-1"
    folder.mkdir()
    write(folder / "capture.json", {"id": 1, "fetch_status": "partial", "expected_attachments": [],
          "expected_related_assets": ["image-a", "image-b"],
          "documents": [{"url": "image-a", "kind": "image", "status": "needs_review", "local_file": "a"}]})
    result = evaluate(tmp_path, {}, include_unstructured_captures=True)
    assert result["linked_asset_retrieval"]["rate"] == 0.5
    assert result["deadline_day_recall"]["rate"] is None
    assert result["prediction_count"] == 0
