"""Reviewable source inventory and gap audit; never a full-form accuracy score.

Replay only saved official bytes and predictions. No API calls or database use.
The label targets below were read from the source, not generated from predictions.
"""
import hashlib
import json
from pathlib import Path

from app.services.form_control_audit import audit_controls

BASE = Path(__file__).resolve().parent
ROOT = BASE / "scholarship-improvement-20261006-whole-form-inventory"
PREFIX = "scholarship-improvement-20261006-native-grid-final-r"
# Source record / input kind / independently visible components. These are source
# groups, not an asserted number of physical atomic fields or confirmed requireds.
TARGETS = [
    (40, "single", []), (49, "single", []), (63, "compound", ["학년", "학기"]), (68, "single", []),
    (232, "compound", ["직전학기 성적", "4.5 또는 4.3 기준"]), (317, "conditional", ["26년 2학기 소득구간"]),
    (415, "choice", ["있음", "없음"]), (434, "conditional", []), (439, "conditional", []), (444, "conditional", []),
    (455, "single", []), (464, "compound", ["주민등록번호 앞부분", "주민등록번호 뒷부분"]),
    (474, "single", []), (483, "single", []), (492, "single", []),
    (501, "compound_choice", ["주거 형태", "자가 금액", "전세 금액", "월세 월 금액", "기타 내용"]),
    (520, "compound", ["대학교", "캠퍼스", "과(부)", "학년", "학기"]),
    (539, "single", []), (544, "compound", ["직전학기 평점", "평점 기준"]),
    (549, "single", []), (554, "compound", ["전체 평균평점", "평점 기준"]),
    (601, "subject_input", []), (610, "subject_input", []), (619, "subject_input", []), (628, "subject_input", []),
    (642, "multiple_marks", ["조부", "조모", "부", "모"]),
    (651, "compound", ["형제자매 남 수", "형제자매 여 수", "본인 순서"]), (656, "conditional", []),
    (666, "compound", ["본인 포함 동거가족 월평균수입", "세후 만원"]),
    (685, "compound", ["정부지원 원", "근로수입 원", "후원금 원"]),
    (704, "compound", ["첫 돌봄대상자 이름", "본인과의 관계"]), (718, "subject_input", []),
    (727, "compound_multiple_choice", ["장기요양 인정자", "등급", "중증장애인", "정신건강 관련 질환으로 치료 중"]),
    (749, "compound", ["두 번째 돌봄대상자 이름", "본인과의 관계"]), (763, "subject_input", []),
    (772, "compound_multiple_choice", ["장기요양 인정자", "등급", "중증장애인", "정신건강 관련 질환으로 치료 중"]),
    (794, "text", []), (806, "choice", ["수급자", "차상위"]), (820, "conditional", []),
    (843, "repeating_column", []), (848, "repeating_column", []), (853, "repeating_column", []),
    (858, "repeating_column", []), (867, "repeating_column", []), (872, "repeating_column", []),
    (877, "repeating_column", []), (1035, "date", ["월", "일"]), (1042, "manual_signature", []),
    (1070, "essay", []), (1083, "essay", []), (1111, "essay", []), (1120, "essay", []),
    (1207, "consent", []), (1262, "consent", []), (1370, "consent", []),
    (1435, "unresolved_reference", []), (1493, "date", ["월", "일"]),
]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def section(record):
    if record < 79:
        return "cover"
    if record < 395:
        return "submission_checklist"
    if record < 1052:
        return "application"
    if record < 1131:
        return "essay"
    return "privacy_consent"


def main():
    ROOT.mkdir(exist_ok=True)
    run = BASE / f"{PREFIX}1" / "notice-new-555354"
    capture = read(run / "capture.json")
    doc = next(d for d in capture["documents"] if d.get("filename", "").endswith(".hwp"))
    blocks = doc["blocks"]
    assert len(blocks) == 239 and all(b["method"] == "native" for b in blocks)
    asset = run / doc["local_file"]
    if not asset.exists():
        asset = next(run.glob("asset-2-*.bin"))
    assert hashlib.sha256(asset.read_bytes()).hexdigest() == doc["sha256"]
    body = "\n".join(b["text"] for b in blocks)
    assert all(body[b["start"]:b["end"]] == b["text"] for b in blocks)
    records = {int(b["location"].rsplit("record:", 1)[1]): b for b in blocks}
    targets = []
    for record, kind, components in TARGETS:
        b = records[record]
        targets.append({"record": record, "kind": kind, "components_to_verify": components,
                        "source": {"sha256": doc["sha256"], "location": b["location"],
                                   "text_start": b["start"], "text_end": b["end"], "evidence": b["text"]}})
    tables = [b["table_structure"] for b in blocks if b.get("table_structure")]
    inventory = {"source_sha256": doc["sha256"], "complete_atomic_gold": False,
                 "review_status": "source_text_reviewed_visual_and_independent_review_pending",
                 "scope": "all_preserved_native_text_and_table_metadata_not_all_original_objects",
                 "blocks": [{"section": section(record), **b} for record, b in records.items()],
                 "tables": tables, "source_input_groups": targets,
                 "unresolved": ["atomic_destinations_in_merged_tables", "submission_checklist_blank_marks",
                                "compound_choice_values", "repeating_scholarship_rows",
                                "original_consent_reference", "dates", "independent_visual_review"]}
    frozen = ROOT / "source-inventory.json"
    if frozen.exists():
        assert read(frozen) == inventory, "Do not replace frozen inventory with prediction-derived labels"
    else:
        save(frozen, inventory)
    results = []
    for index in range(1, 4):
        path = BASE / f"{PREFIX}{index}" / "notice-new-555354"
        prediction = read(path / "prediction.json")
        predicted = next(d for d in prediction["documents"] if d.get("sha256") == doc["sha256"])
        fields = predicted["structured"]["application_fields"]
        groups = []
        for target in targets:
            location = target["source"]["location"]
            bound = [f["id"] for f in fields if f.get("source", {}).get("location") == location]
            groups.append({"record": target["record"], "source_label": target["source"]["evidence"],
                           "source_field_ids": bound,
                           "status": "label_source_present_not_atomic_completeness" if bound else "no_field_label_bound_to_source",
                           "components_still_require_review": target["components_to_verify"]})
        result = {"run": path.parent.name, "measurement": "saved_prediction_audit_no_new_model_call",
                  "field_count": len(fields), "source_groups": groups,
                  "controls": audit_controls(blocks, fields, {"sha256": doc["sha256"], "url": doc["url"]}),
                  "source_groups_with_label": sum(bool(g["source_field_ids"]) for g in groups),
                  "whole_form_complete": False}
        save(ROOT / f"r{index}-audit.json", result)
        results.append(result)
    by_location = [{f["source"]["location"] for f in next(d for d in read(BASE / f"{PREFIX}{i}" / "notice-new-555354/prediction.json")["documents"] if d.get("sha256") == doc["sha256"])["structured"]["application_fields"]} for i in range(1, 4)]
    missing = sorted(by_location[0] - by_location[2])
    summary = {"inventory_sha256": hashlib.sha256(frozen.read_bytes()).hexdigest(),
               "native_blocks": len(blocks), "native_tables": len(tables),
               "source_input_groups": len(targets), "complete_atomic_gold": False,
               "field_counts": [r["field_count"] for r in results],
               "source_groups_with_label": [r["source_groups_with_label"] for r in results],
               "unresolved_native_choice_cells": [r["controls"]["unresolved"] for r in results],
               "r3_missing_label_sources_relative_to_r1": [{"location": loc, "text": next(b["text"] for b in blocks if b["location"] == loc)} for loc in missing],
               "source_proof_checks": len(blocks), "source_proof_failures": 0,
               "database_written": False, "new_llm_calls": 0,
               "limits": ["Label presence does not prove correct type or components", "No full-form accuracy denominator", "Visual and independent review pending"]}
    save(ROOT / "summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "r3_missing_label_sources_relative_to_r1"}))


if __name__ == "__main__":
    main()
