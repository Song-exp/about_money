"""Freeze source contracts, compare old predictions and corrected v30 metadata."""
import hashlib
import itertools
import json
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import HTTPException
from app.services.hwp_source_inventory import source_inventory
from app.services.form_checklist_values import native_checklist_values
from app.services.writing_workflow import _set_fact, _check_choice
from app.services.writing_requirements import requirements
from app.services.writing_drafts import compose
from reports.atomic_input_reference import record_of
from reports.form_semantic_reference import SOURCE_SHA256
from reports.measure_single_inputs import original_bytes, fresh_basic_fields
from reports.measure_whole_form_inventory import BASE, read, save
from reports.remaining_branch_reference import build_branch_gold, audit_branches

ROOT = BASE / "scholarship-improvement-20261010-remaining-branches-verified"
PREDICTIONS = BASE / "scholarship-improvement-20261010-grid-audit-verified"


def updated_fields(fields):
    blocks, _ = fresh_basic_fields(original_bytes())
    corrected = [g["fields"][0] for g in native_checklist_values(blocks, {"sha256": SOURCE_SHA256})]
    assert {record_of(f["source"]) for f in corrected} == {232, 317}
    return [next((g for g in corrected if record_of(g["source"]) == record_of(f["source"])), f) for f in fields]


def measure_collection(fields):
    get = lambda r: next(f for f in fields if record_of(f["source"]) == r)
    cases = []
    for record in (637, 806):
        field, answers = get(record), {}
        options = field["options"]
        for count in range(len(options) + 1):
            for selected in itertools.combinations(options, count):
                text = json.dumps(selected, ensure_ascii=False) if selected else "표시하지 않음"
                _set_fact(answers, field, "value", text, {})
                assert set(answers[field["id"]]["facts"]) == {"value"}
                keys = [s["key"] for s in requirements(field, answers[field["id"]])]
                expected = ["value"]
                if record == 806:
                    expected += ["recipient_evidence" if option == "①수급자" else "near_poverty_evidence" for option in selected] if selected else ["support_bracket"]
                assert keys == expected
                for key in {"recipient_evidence", "near_poverty_evidence", "support_bracket"} - set(keys):
                    if record != 806:
                        continue
                    try:
                        _check_choice(field, "2", key, answers[field["id"]])
                    except HTTPException as exc:
                        assert exc.status_code == 422
                    else:
                        raise AssertionError("inactive economic branch value accepted")
                for key in keys[1:]:
                    _set_fact(answers, field, key, "2" if key == "support_bracket" else "제출 가능", {})
                draft = compose(field, answers[field["id"]], SimpleNamespace(title="원문 분기"))
                assert not draft["validation"]["errors"]
                cases.append({"record": record, "selected": list(selected), "keys": keys, "old_branch_values_cleared": True})
    grade, income, support = get(232), get(317), get(806)
    answers = {}
    _set_fact(answers, get(529), "gpa", "4.1", {})
    _set_fact(answers, get(529), "scale", "4.3", {})
    _set_fact(answers, support, "value", "표시하지 않음", {})
    _set_fact(answers, support, "support_bracket", "2", {})
    assert grade["id"] not in answers and income["id"] not in answers
    grades = []
    for value, scale in (("0", "4.5"), ("3.8", "4.5"), ("4.3", "4.3")):
        current = {}
        _set_fact(current, grade, "gpa", value, {})
        _set_fact(current, grade, "scale", scale, {})
        draft = compose(grade, current[grade["id"]], SimpleNamespace(title="체크리스트"))
        assert draft["text"] == f"{value} / {scale}"
        grades.append({"gpa": value, "scale": scale})
    brackets = []
    for value in ("0", "1", "2"):
        current = {}
        _set_fact(current, income, "income_bracket", value, {})
        assert compose(income, current[income["id"]], SimpleNamespace(title="체크리스트"))["text"] == f"{value} 분위"
        brackets.append(value)
    rejected = 0
    for field, key, value in ((income, "income_bracket", "3"), (income, "income_bracket", "-1"),
        (income, "income_bracket", "1.5"), (support, "support_bracket", "3"),
        (support, "recipient_evidence", "제출 불가"), (grade, "scale", "4.0")):
        current = {"facts": {"value": {"text": "①수급자" if key == "recipient_evidence" else "표시하지 않음"}}}
        try:
            _check_choice(field, value, key, current)
        except HTTPException as exc:
            assert exc.status_code == 422
            rejected += 1
        else:
            raise AssertionError("unsupported original value accepted")
    return {"branch_cases": cases, "grade_cases": grades, "bracket_cases": brackets,
        "invalid_values_rejected": rejected, "body_grade_and_bracket_not_copied": True, "failures": 0}


def main():
    ROOT.mkdir(exist_ok=True)
    gold = build_branch_gold(source_inventory(original_bytes()))
    path = ROOT / "gold.json"
    if path.exists():
        assert read(path) == gold, "frozen original contract changed"
    else:
        save(path, gold)
    runs = []
    for index in range(1, 4):
        original_bytes(index)
        before = read(PREDICTIONS / f"r{index}-prediction.json")["application_fields"]
        old = audit_branches(gold, before)
        save(ROOT / f"r{index}-before-audit.json", old)
        assert [(c["id"], c["issues"]) for c in old["checks"] if c["issues"]] == [
            ("prompt.317", ["deselected_document_retains_old_answer"])]
        fields = updated_fields(before)
        audit = audit_branches(gold, fields)
        save(ROOT / f"r{index}-after-audit.json", audit)
        save(ROOT / f"r{index}-updated-fields.json", [f for f in fields if record_of(f["source"]) in (232,317,637,806)])
        assert not audit["unresolved_groups"], audit
        with patch("app.services.llm.has_llm", return_value=False):
            collection = measure_collection(fields)
        save(ROOT / f"r{index}-collection.json", collection)
        runs.append({"run": index, "groups": 4, "old_retained_answer_policy_gaps": 1, "corrected_contract_failures": 0,
                     "branch_cases": len(collection["branch_cases"]), "grade_cases": 3, "bracket_cases": 3, "invalid_values_rejected": 6})
    summary = {"gold_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "runs": runs,
        "new_llm_calls": 0, "database_written": False, "runtime_extractor_version": "structured-v30",
        "measurement": "saved_v28_predictions_with_fresh_native_checklist_restore_not_new_model_runs",
        "remaining_detailed_contract_groups": [1070,1083,1111,1120], "complete_atomic_gold": False,
        "whole_form_complete": False, "independent_human_review_complete": False}
    save(ROOT / "summary.json", summary)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
