from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.models.scholarship import PostingStatus
from app.services import llm, posting_extract
from app.services.form_grid import native_grids
from app.services.form_grid_audit import audit_grid_conditions
from app.services.scholarship_readiness import readiness, form_fingerprint, VALIDATION_VERSION
from tests.test_form_grid import parsed_grid

SOURCE = {"sha256": "source"}


def fixture():
    blocks = parsed_grid(reference=False)
    fields = native_grids(blocks, SOURCE)[0]["fields"]
    extra = [f for f in fields if f["layout"]["row"] == 3]
    return blocks, fields, extra


def test_audit_reads_source_without_using_restoration(monkeypatch):
    blocks, fields, extra = fixture()
    monkeypatch.setattr("app.services.form_grid.native_grids", lambda *a: pytest.fail("audit must not use restoration"))
    before = deepcopy((blocks, fields))
    audit = audit_grid_conditions(blocks, fields, SOURCE)
    assert audit["unresolved"] == 0 and len(audit["controls"]) == 1
    assert audit["controls"][0]["field_ids"] == [f["id"] for f in extra]
    assert audit["visual_review_complete"] is False
    assert (blocks, fields) == before


@pytest.mark.parametrize("problem", ["missing_condition", "different_condition_id", "split_condition", "wrong_evidence",
    "wrong_source_hash", "wrong_source_location", "wrong_mode", "keep_previous_person", "different_subject",
    "wrong_subject_hash", "wrong_destination", "wrong_layout", "missing_field", "duplicate_field", "signature_as_text",
    "consent_as_attachment", "invented_options", "auto_choice", "forced_required", "applicant_role", "manual_only",
    "missing_instructions", "condition_on_other_row"])
def test_condition_loss_person_changes_and_collection_bypasses_are_detected(problem):
    blocks, fields, extra = fixture()
    field = extra[0]
    # The shared runtime condition is copied before altering one field.
    field["applicability"] = deepcopy(field["applicability"])
    if problem == "missing_condition":
        for item in extra:
            item.pop("applicability")
    elif problem == "different_condition_id":
        for item in extra:
            item["applicability"] = {**item["applicability"], "id": "wrong"}
    elif problem == "split_condition":
        field["applicability"]["id"] = "another-condition"
    elif problem == "wrong_evidence":
        field["applicability"]["evidence"] = "자동으로 해당"
    elif problem in ("wrong_source_hash", "wrong_source_location"):
        key = "sha256" if problem == "wrong_source_hash" else "location"
        field["applicability"]["source"] = {**field["applicability"]["source"], key: "wrong"}
    elif problem == "wrong_mode":
        field["applicability"]["mode"] = "automatic"
    elif problem == "keep_previous_person":
        field["applicability"]["discard_facts_on_change"] = False
    elif problem == "different_subject":
        field["subject"] = {**field["subject"], "label": "신청자"}
    elif problem == "wrong_subject_hash":
        field["subject"] = {**field["subject"], "source": {**field["subject"]["source"], "sha256": "another-document"}}
    elif problem == "wrong_destination":
        field["input_cell_source"] = {**field["input_cell_source"], "row": 2}
    elif problem == "wrong_layout":
        field["layout"] = {**field["layout"], "row": 2}
    elif problem == "missing_field":
        fields.remove(extra[-1])
    elif problem == "duplicate_field":
        fields.append(deepcopy(field))
    elif problem == "signature_as_text":
        extra[-1]["field_type"] = "text"
    elif problem == "consent_as_attachment":
        extra[2]["field_type"] = "attachment"
    elif problem == "invented_options":
        extra[2]["options"].append("동의하지 않음")
    elif problem == "auto_choice":
        extra[2]["selection_mode"] = "automatic"
    elif problem == "forced_required":
        field["required"] = True
    elif problem == "applicant_role":
        field["role"] = "applicant"
    elif problem == "manual_only":
        field["manual_spec"] = {"mode": "original_only"}
    elif problem == "missing_instructions":
        field["instruction_sources"] = []
    else:
        fields[0]["applicability"] = deepcopy(field["applicability"])
    audit = audit_grid_conditions(blocks, fields, SOURCE)
    assert audit["unresolved"] == 1 and audit["controls"][0]["status"] == "requires_original_review"
    if problem == "condition_on_other_row":
        assert "conditional_grid_condition_leaks_to_other_subject" in audit["controls"][0]["issues"]


@pytest.mark.parametrize("problem", ["ocr", "missing_shape", "damaged_shape", "missing_cell", "bad_text_pointer", "object", "missing_consent_header"])
def test_unproven_source_keeps_condition_for_review(problem):
    blocks, fields, _ = fixture()
    owner = next(b for b in blocks if b.get("table_structure"))
    shape = owner["table_structure"]
    if problem == "ocr":
        next(b for b in blocks if b.get("row") == 3 and b.get("column") == 0)["method"] = "ocr"
    elif problem == "missing_shape":
        owner.pop("table_structure")
    elif problem == "damaged_shape":
        shape["issues"].append("ambiguous")
    elif problem == "missing_cell":
        shape["cells"].pop()
    elif problem == "bad_text_pointer":
        shape["cells"][-2]["text_locations"] = ["wrong"]
    elif problem == "missing_consent_header":
        blocks.remove(next(b for b in blocks if b.get("row") == 0 and b.get("column") == 3))
    else:
        shape["cells"][-1]["has_control"] = True
    assert audit_grid_conditions(blocks, fields, SOURCE)["unresolved"] == 1


def test_unrelated_conditional_text_does_not_become_consent_row():
    assert audit_grid_conditions([{"text": "추가 대상자(해당 시)", "column": 0, "row": 1, "table": "other"}], [], SOURCE)["controls"] == []


def test_extraction_and_individual_audit_record_block_approval_even_if_summary_cleared(monkeypatch):
    blocks, _, _ = fixture()
    original = posting_extract.native_grids
    def faulty_restoration(*args):
        groups = original(*args)
        for group in groups:
            for field in group["fields"]:
                field.pop("applicability", None)
        return groups
    monkeypatch.setattr(posting_extract, "native_grids", faulty_restoration)
    monkeypatch.setattr(llm, "has_llm", lambda: False)
    result = posting_extract.extract_structured("\n".join(b["text"] for b in blocks), SOURCE, blocks)
    assert "native_grid_conditions_require_review" in result["issues"]
    audit = result["coverage"]["native_grid_condition_audit"]
    assert audit["unresolved"] == 1
    audit["unresolved"] = 0
    row = SimpleNamespace(status=PostingStatus.OPEN, deadline_at=None, content_hash="source", eligibility={
        "application_fields": result["application_fields"], "form_exclusions": [], "extraction_issues": [],
        "form_coverage": [{"native_grid_condition_audit": audit}], "validation_version": VALIDATION_VERSION})
    row.eligibility["form_review"] = {"fingerprint": form_fingerprint(row)}
    state = readiness(row)
    assert not state["writing_ready"] and "application_form_conflicts" in state["reasons"]
