"""Freeze original contracts before inspecting saved predictions; no network/DB."""
import hashlib
import json
from types import SimpleNamespace
from fastapi import HTTPException
from app.services.hwp_source_inventory import source_inventory
from app.services.writing_workflow import _set_fact, _field, _check_choice
from app.services.writing_requirements import requirements
from reports.atomic_input_reference import record_of
from reports.form_semantic_reference import SOURCE_SHA256
from reports.measure_whole_form_inventory import BASE, PREFIX, read, save
from reports.submission_consent_reference import build_submission_consent_gold, audit_submission_consent

ROOT = BASE / "scholarship-improvement-20261010-submission-consent-verified"
PREDICTIONS = BASE / "scholarship-improvement-20261010-grid-audit-verified"


def measure_collection(gold, fields):
    cases, answers = [], {}
    for item in gold["checklist"]:
        field = next(f for f in fields if record_of(f.get("source")) == item["labels"][0]["record"])
        for status in item["status_options"]:
            _set_fact(answers, field, "preparation_status", status, {})
            assert set(answers[field["id"]]["facts"]) == {"preparation_status"}
            keys = [s["key"] for s in requirements(field, answers[field["id"]])]
            assert keys == ["preparation_status"] + (["rules_ack"] if status == "준비됨" else [])
            if status == "준비됨":
                _set_fact(answers, field, "rules_ack", "원문 조건 확인", {}, confirmed=True)
            for invalid in ("O", "√", "제출 완료"):
                try:
                    _set_fact(answers, field, "preparation_status", invalid, {})
                except HTTPException as exc:
                    assert exc.status_code == 422
                else:
                    raise AssertionError("original mark treated as verified preparation")
            cases.append({"id": item["id"], "status": status, "required_slots": keys,
                          "original_mark_and_submission_completion_rejected": True})
    consent_answers = {}
    for item, value in zip(gold["consents"], ("동의함", "동의하지 않음", "동의함")):
        field = next(f for f in fields if record_of(f.get("source")) == item["labels"][0]["record"])
        _check_choice(field, value)
        _set_fact(consent_answers, field, "value", value, {})
        assert consent_answers[field["id"]]["facts"]["value"]["text"] == value
    assert len(consent_answers) == 3  # No footer/person response was inherited.
    manual_rejected = []
    manual_fields = [f for f in fields if record_of(f.get("source")) in (464, 1035, 1042, 1493) or
                     (f.get("layout", {}).get("table") == "BodyText/Section0/table:1393" and f.get("field_type") == "signature")]
    for field in manual_fields:
        try:
            _field(SimpleNamespace(fields=fields, workflow={}), field["id"])
        except HTTPException as exc:
            assert exc.status_code == 422
            manual_rejected.append(field["id"])
        else:
            raise AssertionError("manual original field accepted chat fact")
    assert len(manual_rejected) == 7
    return {"checklist_cases": cases, "consent_sections_collected_separately": 3,
        "refusal_preserved": True, "consent_not_inherited_to_people": True,
        "original_only_and_signature_inputs_rejected": len(manual_rejected), "failures": 0,
        "scope": "in_process_collection_not_browser_or_actual_submission"}


def main():
    ROOT.mkdir(exist_ok=True)
    notice = BASE / f"{PREFIX}1" / "notice-new-555354"
    doc = next(d for d in read(notice / "capture.json")["documents"] if d.get("sha256") == SOURCE_SHA256)
    gold = build_submission_consent_gold(source_inventory((notice / doc["local_file"]).read_bytes()))
    path = ROOT / "gold.json"
    if path.exists():
        assert read(path) == gold, "frozen reference changed"
    else:
        save(path, gold)
    runs = []
    for index in range(1, 4):
        notice = BASE / f"{PREFIX}{index}" / "notice-new-555354"
        doc = next(d for d in read(notice / "capture.json")["documents"] if d.get("sha256") == SOURCE_SHA256)
        assert hashlib.sha256((notice / doc["local_file"]).read_bytes()).hexdigest() == SOURCE_SHA256
        prediction = read(PREDICTIONS / f"r{index}-prediction.json")
        fields = prediction["application_fields"]
        audit = audit_submission_consent(gold, fields, prediction["coverage"])
        save(ROOT / f"r{index}-audit.json", audit)
        assert not audit["unresolved_contracts"], audit
        collection = measure_collection(gold, fields)
        save(ROOT / f"r{index}-collection.json", collection)
        runs.append({"run": index, "item_contracts": 25, "reference_policy_checks": 2,
            "unresolved_contracts": 0, "preparation_cases": len(collection["checklist_cases"]),
            "consent_sections": 3, "manual_inputs_rejected": 7, "collection_failures": 0})
    summary = {"gold_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "runs": runs,
        "measurement": "saved_v28_predictions_and_in_process_collection_against_source_contracts",
        "new_llm_calls": 0, "database_written": False, "runtime_extractor_changed": False,
        "whole_form_complete": False, "complete_atomic_gold": False, "independent_human_review_complete": False,
        "printed_reference_still_requires_review": True,
        "code_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (
            BASE / "submission_consent_reference.py", BASE / "measure_submission_consent.py")}}
    save(ROOT / "summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "code_sha256"}))


if __name__ == "__main__":
    main()
