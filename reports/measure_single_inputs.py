"""Measure bounded original inputs, preserving an unproven address destination."""
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import patch
import olefile
from app.services.hwp_source_inventory import source_inventory, inflate
from app.services.hwp_layout import section_blocks
from app.services.document_layout import finalize_blocks
from app.services.form_basic_inputs import native_basic_inputs, audit_basic_inputs
from app.services.writing_requirements import requirements
from app.services.writing_workflow import _set_fact
from app.services.writing_drafts import compose
from reports.atomic_input_reference import record_of
from reports.form_semantic_reference import SOURCE_SHA256
from reports.measure_whole_form_inventory import BASE, PREFIX, read, save
from reports.single_input_reference import build_single_gold, audit_single_inputs, reconcile_scopes

ROOT = BASE / "scholarship-improvement-20261010-single-inputs-verified"
PREDICTIONS = BASE / "scholarship-improvement-20261010-grid-audit-verified"


def original_bytes(index=1):
    notice = BASE / f"{PREFIX}{index}" / "notice-new-555354"
    doc = next(d for d in read(notice / "capture.json")["documents"] if d.get("sha256") == SOURCE_SHA256)
    content = (notice / doc["local_file"]).read_bytes()
    assert hashlib.sha256(content).hexdigest() == SOURCE_SHA256
    return content


def fresh_basic_fields(content):
    with olefile.OleFileIO(io.BytesIO(content)) as ole:
        header = ole.openstream("FileHeader").read()
        data, _ = inflate(ole.openstream("BodyText/Section0").read(), bool(int.from_bytes(header[36:40], "little") & 1))
    blocks = finalize_blocks(section_blocks(data, "BodyText/Section0"))
    fields = [f for g in native_basic_inputs(blocks, {"sha256": SOURCE_SHA256}) for f in g["fields"]]
    return blocks, fields


def measure_collection(gold, fields):
    answers, cases = {}, []
    for group in gold["groups"]:
        if group["independent_geometry_proven"] is False:
            continue  # Never claim successful original placement for record 495.
        field = next(f for f in fields if record_of(f.get("source")) == group["label_source"]["record"])
        assert [s["key"] for s in requirements(field)] == [a["key"] for a in group["atoms"]]
        for atom in group["atoms"]:
            value = f"사용자확인_{group['id']}_{atom['key']}"
            _set_fact(answers, field, atom["key"], value, {})
        for fact in answers[field["id"]]["facts"].values():
            fact["confirmed"] = True
        draft = compose(field, answers[field["id"]], SimpleNamespace(title="원문 단일 입력 확인"))
        assert all(fact["text"] in draft["text"] for fact in answers[field["id"]]["facts"].values())
        assert not draft["validation"]["errors"]
        if group["label_source"]["record"] == 794:
            assert "병원 동행" not in draft["text"]  # Printed example is not the user's care activity.
        cases.append({"group": group["id"], "values_collected": len(group["atoms"]), "user_answers_only": True})
    ids = [next(f["id"] for f in fields if record_of(f.get("source")) == record) for record in (68, 455, 601)]
    assert len(set(ids)) == 3 and len({answers[fid]["text"] for fid in ids}) == 3
    return {"cases": cases, "groups_collected": len(cases), "values_collected": sum(c["values_collected"] for c in cases),
        "cover_applicant_guardian_name_isolation": True, "address_placement_not_claimed": True,
        "care_example_not_used_as_answer": True, "failures": 0}


def main():
    ROOT.mkdir(exist_ok=True)
    gold = build_single_gold(source_inventory(original_bytes()))
    path = ROOT / "gold.json"
    if path.exists():
        assert read(path) == gold, "frozen source reference changed"
    else:
        save(path, gold)
    runs = []
    for index in range(1, 4):
        original_bytes(index)
        fields = read(PREDICTIONS / f"r{index}-prediction.json")["application_fields"]
        audit = audit_single_inputs(gold, fields)
        save(ROOT / f"r{index}-audit.json", audit)
        unresolved = [c for c in audit["checks"] if c["issues"]]
        assert [(c["id"], c["issues"]) for c in unresolved] == [("single.492", ["source_cell_geometry_requires_review"])]
        scope = reconcile_scopes(gold, fields)
        save(ROOT / f"r{index}-scope.json", scope)
        assert not scope["missing_entries"] and not scope["unexpected_entries"] and not scope["duplicate_entries"]
        with patch("app.services.llm.has_llm", return_value=False):
            collection = measure_collection(gold, fields)
        save(ROOT / f"r{index}-collection.json", collection)
        runs.append({"run": index, "groups": audit["groups"], "semantic_values": audit["atoms"],
            "unproven_destination_groups": 1, "other_contract_failures": 0,
            "collected_groups": collection["groups_collected"], "collected_values": collection["values_collected"]})
    blocks, fresh = fresh_basic_fields(original_bytes())
    audit = audit_basic_inputs(blocks, fresh, {"sha256": SOURCE_SHA256})
    address = next(f for f in fresh if record_of(f["source"]) == 492)
    assert address["input_cell_source"]["geometry_proven"] is False
    assert "basic_input_geometry_requires_original_review" in address["constraint_issues"] and audit["unresolved"] == 1
    save(ROOT / "fresh-v29-basic-fields.json", fresh)
    save(ROOT / "fresh-v29-basic-audit.json", audit)
    summary = {"gold_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "runs": runs,
        "source_catalog_groups": len(gold["scope_catalog"]), "contract_scope_groups": 52, "pending_contract_scope_groups": 8,
        "unproven_address_record": 495, "fresh_v29_address_review_required": True,
        "measurement": "saved_v28_schema_and_collection_plus_fresh_v29_native_basic_restore",
        "new_llm_calls": 0, "database_written": False, "runtime_extractor_changed": True,
        "complete_atomic_gold": False, "whole_form_complete": False, "independent_human_review_complete": False}
    save(ROOT / "summary.json", summary)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
