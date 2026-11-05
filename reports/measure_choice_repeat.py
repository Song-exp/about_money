"""Freeze source choice/repeat contracts, then measure schema and collection."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from app.services.hwp_source_inventory import source_inventory
from app.services.writing_workflow import _set_fact, _check_choice
from app.services.writing_requirements import requirements
from app.services.writing_drafts import compose
from reports.atomic_input_reference import record_of
from reports.choice_repeat_reference import build_choice_repeat_gold, audit_choice_repeat
from reports.form_semantic_reference import SOURCE_SHA256
from reports.measure_whole_form_inventory import BASE, PREFIX, read, save

ROOT = BASE / "scholarship-improvement-20261010-choice-repeat-verified"
PREDICTIONS = BASE / "scholarship-improvement-20261010-grid-audit-verified"
VALUES = {"year": "2022", "provider": "사용자 확인 지급처", "name": "사용자 확인 장학금", "semester_amount": "일시",
          "total_amount": "100 만원", "start_date": "2022-02-01", "end_date": "2022-12-31", "nature": "생활비"}


def measure_collection(gold, fields):
    checks = []
    for group in gold["choice_groups"]:
        field = next(f for f in fields if record_of(f.get("source")) == group["label_source"]["record"])
        answers = {}
        for choice in group["choices"]:
            _set_fact(answers, field, "value", choice["caption"], {"kind": "measurement"})
            assert set(answers[field["id"]]["facts"]) == {"value"}
            expected = ["value", *[v["key"] for v in choice["values"]]]
            assert [s["key"] for s in requirements(field, answers[field["id"]])] == expected
            all_values = {v["key"] for c in group["choices"] for v in c["values"]}
            for key in all_values - set(expected):
                try:
                    _check_choice(field, "1", key, answers[field["id"]])
                except HTTPException:
                    pass
                else:
                    raise AssertionError("inactive choice value accepted")
            for value in choice["values"]:
                text = "50" if value["kind"] == "amount" else "3등급" if value["kind"] == "grade" else "1" if value["kind"] == "positive_integer" else "사용자 확인 내용"
                _set_fact(answers, field, value["key"], text, {"kind": "measurement"})
            draft = compose(field, answers[field["id"]], SimpleNamespace(title="원문 양식 검증"))
            assert draft["text"] and not draft["validation"]["errors"]
            checks.append({"group": group["id"], "caption": choice["caption"], "active_keys": expected,
                           "inactive_keys_rejected": len(all_values - set(expected)), "previous_values_cleared": True})
        if group["mode"] != "single":
            pair = group["choices"][:2]
            _set_fact(answers, field, "value", json.dumps([c["caption"] for c in pair], ensure_ascii=False), {})
            expected = ["value", *[v["key"] for c in pair for v in c["values"]]]
            assert [s["key"] for s in requirements(field, answers[field["id"]])] == expected
            checks.append({"group": group["id"], "captions": [c["caption"] for c in pair],
                           "active_keys": expected, "previous_values_cleared": True})
    first = next(f for f in fields if record_of(f.get("source")) == 727)
    second = next(f for f in fields if record_of(f.get("source")) == 772)
    assert first["id"] != second["id"]
    people = {}
    _set_fact(people, first, "value", first["options"][0], {})
    _set_fact(people, first, "choice_0_value", "3등급", {})
    _set_fact(people, second, "value", second["options"][1], {})
    assert "choice_0_value" not in people[second["id"]]["facts"]
    assert [s["key"] for s in requirements(second, people[second["id"]])] == ["value"]
    field = next(f for f in fields if record_of(f.get("source")) == 830)
    answers = {}
    _set_fact(answers, field, "scope_confirmed", "제외 조건 확인", {})
    repeats = []
    for count in (0, 1, 4, 5, 1):
        _set_fact(answers, field, "entry_count", str(count), {})
        assert set(answers[field["id"]]["facts"]) == {"scope_confirmed", "entry_count"}
        assert len(requirements(field, answers[field["id"]])) == 2 + 8 * count
        for index in range(count):
            for key, text in VALUES.items():
                _set_fact(answers, field, f"entry_{index}_{key}", text, {})
        for fact in answers[field["id"]]["facts"].values():
            fact["confirmed"] = True
        draft = compose(field, answers[field["id"]], SimpleNamespace(title="原文"))
        assert not draft["validation"]["errors"]
        if count:
            assert "2022" in draft["text"] and "2026" not in draft["text"]
            assert "original_repeating_layout_check_required" in draft["validation"]["warnings"]
        else:
            assert "내역 없음(사용자 응답)" in draft["text"]
        repeats.append({"entry_count": count, "entry_values_collected": 8 * count,
                        "previous_row_values_cleared": True, "source_year_not_copied": True})
    return {"choice_cases": checks, "repeat_cases": repeats, "recipient_isolation_checks": 1, "failures": 0,
            "scope": "in_process_collection_and_draft_not_browser_or_actual_user_answers"}


def main():
    ROOT.mkdir(exist_ok=True)
    notice = BASE / f"{PREFIX}1" / "notice-new-555354"
    doc = next(d for d in read(notice / "capture.json")["documents"] if d.get("sha256") == SOURCE_SHA256)
    gold = build_choice_repeat_gold(source_inventory((notice / doc["local_file"]).read_bytes()))
    path = ROOT / "gold.json"
    if path.exists():
        assert read(path) == gold
    else:
        save(path, gold)
    runs = []
    for index in range(1, 4):
        notice = BASE / f"{PREFIX}{index}" / "notice-new-555354"
        doc = next(d for d in read(notice / "capture.json")["documents"] if d.get("sha256") == SOURCE_SHA256)
        assert hashlib.sha256((notice / doc["local_file"]).read_bytes()).hexdigest() == SOURCE_SHA256
        result = read(PREDICTIONS / f"r{index}-prediction.json")
        audit = audit_choice_repeat(gold, result["application_fields"])
        save(ROOT / f"r{index}-audit.json", audit)
        assert not audit["unresolved_groups"], audit
        with patch("app.services.llm.has_llm", return_value=False):
            collection = measure_collection(gold, result["application_fields"])
        save(ROOT / f"r{index}-collection.json", collection)
        runs.append({"run": index, "audited_groups": 5, "choice_cases": len(collection["choice_cases"]),
                     "repeat_cases": len(collection["repeat_cases"]), "recipient_isolation_checks": 1,
                     "unresolved_groups": 0, "collection_failures": 0})
    summary = {"gold_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "runs": runs,
        "measurement": "saved_v28_predictions_and_in_process_collection_against_source_contracts",
        "new_llm_calls": 0, "database_written": False, "runtime_extractor_changed": False,
        "whole_form_complete": False, "complete_atomic_gold": False, "independent_human_review_complete": False,
        "code_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (
            BASE / "choice_repeat_reference.py", BASE / "measure_choice_repeat.py")}}
    save(ROOT / "summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "code_sha256"}))


if __name__ == "__main__":
    main()
