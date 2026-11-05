from types import SimpleNamespace

import pytest

from app.models.scholarship import PostingStatus
from app.services.document_layout import finalize_blocks
from app.services.form_control_audit import audit_controls
from app.services.form_semantics import block_source
from app.services.scholarship_readiness import readiness, form_fingerprint, VALIDATION_VERSION
from app.services.posting_extract import extract_structured
from app.services import llm
from app.services.form_consent import original_options


def fixture(texts, multiple=False):
    blocks = [{"text": text, "location": f"t/c1/p{i}", "method": "native", "cell_location": "t/c1",
               "table": "t", "row": 1, "column": 1} for i, text in enumerate(texts)]
    if multiple:
        blocks.append({"text": "※ 중복 체크 가능", "location": "t/c0/p0", "method": "native",
                       "cell_location": "t/c0", "table": "t", "row": 1, "column": 0})
    return finalize_blocks(blocks)


def field(blocks, options):
    return {"id": "choice", "label": "선택", "field_type": "checkbox", "options": options,
            "evidence": blocks[0]["text"], "source": block_source(blocks[0], {"sha256": "hash"}),
            "option_sources": [block_source(b, {"sha256": "hash"}) for b in blocks if b.get("column") == 1]}


def test_model_omits_controls_even_without_fallback_label(monkeypatch):
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    blocks = fixture(["□ 장기요양 인정자(등급:    )", "□ 중증장애인"], True)
    result = extract_structured("\n".join(b["text"] for b in blocks), {"sha256": "hash"}, blocks)
    control = result["coverage"]["native_control_audit"]["controls"][0]
    assert "native_controls_require_review" in result["issues"]
    assert "native_choice_missing_or_ambiguous" in control["issues"]
    assert "choice_inline_values_require_review" in control["issues"]
    assert "multiple_choice_collection_requires_review" in control["issues"]
    assert len(control["sources"]) == 2 and len(control["multiple_selection_sources"]) == 1


@pytest.mark.parametrize("problem", ["missing_paragraph", "wrong_sha", "wrong_offsets", "wrong_options", "duplicate"])
def test_same_row_or_partial_or_wrong_source_does_not_bind_choice(problem):
    blocks = fixture(["□ 있음", "□ 없음"])
    item = field(blocks, ["있음", "없음"])
    fields = [item]
    if problem == "missing_paragraph":
        item["option_sources"].pop()
    elif problem == "wrong_sha":
        item["option_sources"][-1]["sha256"] = "different"
    elif problem == "wrong_offsets":
        item["option_sources"][-1]["text_start"] += 1
    elif problem == "wrong_options":
        item["options"] = ["있음"]
    else:
        fields.append({**item, "id": "duplicate"})
    assert audit_controls(blocks, fields, {"sha256": "hash"})["unresolved"] == 1


def test_simple_consent_can_bind_but_inline_blank_and_multiple_cannot():
    blocks = fixture(["동의함 □"])
    assert audit_controls(blocks, [field(blocks, ["동의함"])], {"sha256": "hash"})["unresolved"] == 0
    blocks = fixture(["□ 자가(      만원) □ 전세(      만원)"])
    audit = audit_controls(blocks, [field(blocks, ["자가(      만원)", "전세(      만원)"])], {"sha256": "hash"})
    assert audit["controls"][0]["issues"] == ["choice_inline_values_require_review"]


def test_wrapped_consent_choices_are_not_mistaken_for_an_inline_value():
    blocks = fixture(["위와 같이 동의합니다. ( 동의함  □   동의하지 않음  □ )"])
    audit = audit_controls(blocks, [field(blocks, ["동의함", "동의하지 않음"])], {"sha256": "hash"})
    assert audit["unresolved"] == 0


def test_choice_amount_and_grade_parentheses_survive_original_parsing():
    assert original_options("□ 자가(      만원) □ 기타(          )") == ["자가(      만원)", "기타(          )"]
    assert original_options("□ 장기요양 인정자(등급:    )") == ["장기요양 인정자(등급:    )"]


def test_clearing_general_issues_and_matching_review_cannot_bypass_source_gap():
    blocks = fixture(["□ 있음 □ 없음"])
    item = field(blocks, ["있음", "없음"])
    audit = audit_controls(blocks, [], {"sha256": "hash"})
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="hash", eligibility={
        "application_fields": [item], "form_coverage": [{"native_control_audit": audit}],
        "validation_version": VALIDATION_VERSION, "extraction_issues": []})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert readiness(row)["recommendation_ready"] is True
    assert readiness(row)["writing_ready"] is False
    assert "application_form_conflicts" in readiness(row)["reasons"]
    audit["unresolved"] = 0
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    assert readiness(row)["writing_ready"] is False


def test_ocr_and_prose_do_not_get_native_checkbox_guarantee():
    blocks = fixture(["□ 있음 □ 없음"])
    blocks[0]["method"] = "ocr"
    assert audit_controls(blocks, [], {"sha256": "hash"})["controls"] == []
    assert audit_controls(finalize_blocks([{"text": "선택을 확인하세요", "location": "p", "method": "native"}]), []) ["controls"] == []
