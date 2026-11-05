from types import SimpleNamespace

import pytest

from app.services import llm
from app.services.document_layout import finalize_blocks
from app.services.form_reference_audit import audit_references
from app.services.posting_extract import extract_structured
from app.services.scholarship_readiness import readiness, form_fingerprint, VALIDATION_VERSION
from app.models.scholarship import PostingStatus
from tests.test_form_grid import parsed_grid


def fixture(count=3):
    blocks = []
    headings = [("개인정보 수집·이용", "수집·이용 동의 여부"), ("개인정보 제공·조회", "제공·조회 동의 여부"),
        ("주민등록번호 처리", "동의 여부")]
    for index, (heading, label) in enumerate(headings[:count]):
        for text, row, col in ((heading, 0, 0), (label, 1, 0), ("동의함 □ 동의하지 않음 □", 1, 1)):
            blocks.append({"text": text, "table": f"section-{index}", "row": row, "column": col, "column_span": 1,
                "row_span": 1, "method": "native", "location": f"section-{index}/r{row}/c{col}",
                "cell_location": f"section-{index}/r{row}/c{col}"})
    return finalize_blocks([*blocks, *parsed_grid()])


@pytest.mark.parametrize("count", [0, 1, 3])
def test_previous_controls_are_candidates_never_auto_inherited(count):
    blocks = fixture(count)
    audit = audit_references(blocks, {"sha256": "hash"})
    assert audit["unresolved"] == 1
    check = audit["controls"][0]
    assert len(check["candidates"]) == count
    assert check["automatic_inheritance"] is False and check["target_field_ids"] == []
    assert check["subject_source"]["evidence"] == "신청자"
    assert check["status"] == "requires_original_review"
    if count == 3:
        assert [c["section"] for c in check["candidates"]] == ["collection_use", "provision_inquiry", "identity_processing"]
    body = "\n".join(b["text"] for b in blocks)
    proofs = [check["source"], check["subject_source"]]
    for candidate in check["candidates"]:
        proofs.extend([candidate["source"], candidate["section_source"], *candidate["label_sources"], *candidate["option_sources"]])
    assert all(body[p["text_start"]:p["text_end"]] == p["evidence"] for p in proofs)


def test_later_controls_are_not_reference_candidates():
    blocks = fixture()
    grid = [b for b in blocks if not b["table"].startswith("section-")]
    earlier = [b for b in blocks if b["table"].startswith("section-")]
    assert audit_references(finalize_blocks([*grid, *earlier]), {})["controls"][0]["candidates"] == []


@pytest.mark.parametrize("problem", ["ocr", "broken_grid", "different_subject"])
def test_reference_survives_unproven_grid_and_person_changes(problem):
    blocks = fixture()
    reference = next(b for b in blocks if b["text"] == "위 표시와 같음")
    if problem == "ocr":
        reference["method"] = "ocr"
    elif problem == "broken_grid":
        next(b["table_structure"] for b in blocks if b.get("table_structure"))["cells"].pop()
    else:
        next(b for b in blocks if b["text"] == "신청자")["text"] = "다른 사람"
    audit = audit_references(blocks, {})
    assert audit["unresolved"] == 1 and not audit["controls"][0]["automatic_inheritance"]
    if problem == "ocr":
        assert "reference_source_not_native" in audit["controls"][0]["issues"]


def test_no_reference_does_not_create_artificial_issue():
    assert audit_references(parsed_grid(reference=False), {})["unresolved"] == 0


def test_independent_reference_gate_survives_cleared_errors_and_constraints(monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: True)
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **kw: '{"application_fields": []}')
    blocks = fixture()
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "hash"}, blocks)
    assert "native_references_require_original_review" in result["issues"]
    audit = result["coverage"]["native_reference_audit"]
    audit["unresolved"] = 0
    for field in result["application_fields"]:
        field.pop("constraint_issues", None)
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="hash", eligibility={
        "application_fields": result["application_fields"], "form_exclusions": [], "extraction_issues": [],
        "form_coverage": [{"native_reference_audit": audit}], "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert not readiness(row)["writing_ready"]
    assert not any(f.get("source", {}).get("location") == audit["controls"][0]["source"]["location"] for f in result["application_fields"])
